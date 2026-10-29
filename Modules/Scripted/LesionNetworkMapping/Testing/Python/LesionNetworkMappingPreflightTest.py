"""Tests of the preflight checks and the post-run output assertions (preflight.py; addendum §3.4, §3.10, F10).

Every check is triggered by a small synthetic study (lesions on a synthetic connectome grid with the
metric cache filled in, as the Studio session writes it); a 300-lesion study is checked in at most
1 s with fresh metrics and without reading any voxel data, also when the lesions are uncompressed
files of equal size (hashing is cached and budgeted); ``assert_run_outputs`` catches a NaN map, a
wrong N and a header mismatch on real engine, export and cohort-map outputs, and passes real glm and
cohort group records (also a null cohort result); the command line exits with 0, or 5 when something
blocks.

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingPreflightTest.py -v
"""

import contextlib
import gzip
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np

from LesionNetworkMappingLib import cohort, cohortmaps, connectome, design, engine, export, glm, jobs, nifti, preflight, runs
from LesionNetworkMappingLib.study import Study

SPACE = "MNI152NLin6Asym"


def _ids(report, level=None):
    return [check["id"] for check in report["checks"] if level is None or check["level"] == level]


def _check(report, check_id):
    for check in report["checks"]:
        if check["id"] == check_id:
            return check
    raise AssertionError(f"No check {check_id!r} in {_ids(report)}")


class _Fixture(unittest.TestCase):
    """A synthetic connectome (2 mm, 20x24x20, labelled MNI152NLin6Asym) and a study of lesions on its grid."""

    bundle_dir = None
    bundle_tmp = None

    @classmethod
    def setUpClass(cls):
        cls.bundle_tmp = tempfile.mkdtemp(prefix="lnm-preflight-bundle-")
        cls.meta = connectome.make_synthetic(os.path.join(cls.bundle_tmp, "synthetic.lnmc"), n_subjects=6, name="Synthetic")
        cls.bundle_dir = cls.meta["path"]
        connectome.set_metadata(cls.bundle_dir, space=SPACE)
        cls.bundle = connectome.ConnectomeBundle.open(cls.bundle_dir)
        cls.manifest_sha256 = export.file_sha256(os.path.join(cls.bundle_dir, "connectome.json"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.bundle_tmp, ignore_errors=True)

    def setUp(self):
        jobs.reset_cancel()
        self.tmp = tempfile.mkdtemp(prefix="lnm-preflight-test-")
        self.study = Study.create(os.path.join(self.tmp, "study"), name="Preflight", space=SPACE)
        self.counter = 0

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # Lesion files and rows

    def lesion_array(self, center=None, radius=2, values=None):
        shape = self.bundle.shape
        if center is None:
            self.counter += 1
            center = (5 + self.counter % 10, 6 + (self.counter // 10) % 12, 5 + (self.counter // 120) % 10)
        ijk = np.indices(shape).reshape(3, -1).T
        inside = (np.linalg.norm(ijk - np.asarray(center), axis=1) <= radius).reshape(shape)
        data = inside.astype(np.float32 if values is not None else np.uint8)
        if values is not None:
            data[inside] = values
        return data

    def write_lesion(self, sid, data=None, affine=None, name=None, **kwargs):
        data = self.lesion_array() if data is None else data
        path = os.path.join(self.study.path, "lesions", name or f"{sid}_space-{SPACE}_desc-lesion_mask.nii.gz")
        nifti.write_nifti(path, data, self.bundle.affine if affine is None else affine, **kwargs)
        return path

    def add(self, sid, data=None, fresh=True, affine=None, name=None, **values):
        """Add a subject with a lesion file and (fresh) cached metrics; ``values`` override row cells."""
        path = self.write_lesion(sid, data, affine, name)
        array = nifti.read_nifti(path)[0]
        voxels = float(np.asarray(array, dtype=np.float64).sum())
        row = {"lesion": self.study.relpath(path), "space": SPACE, "status": "traced", "volume_ml": round(voxels * 0.008, 6), "fractional_sum": voxels}
        if fresh:
            stat = os.stat(path)
            row.update(lesion_mtime=round(stat.st_mtime, 3), lesion_size=stat.st_size)
        row.update(values)
        self.study.add_subject(sid, row)
        return path

    def add_many(self, n, prefix="sub-", start=1, **values):
        for index in range(start, start + n):
            self.add(f"{prefix}{index:03d}", **values)

    def preflight(self, **kwargs):
        kwargs.setdefault("bundle", self.bundle)
        kwargs.setdefault("accepted_terms", True)
        return preflight.run_preflight(self.study, **kwargs)


class CleanStudyTest(_Fixture):
    def test_clean_study_passes(self):
        self.add_many(6, group="case")
        report = self.preflight()
        self.assertFalse(report["blocked"], report["checks"])
        self.assertEqual(_ids(report, "block"), [])
        self.assertEqual(_ids(report, "warn"), [])
        self.assertEqual(report["counts"]["included"], 6)
        self.assertEqual(report["excluded"], {})
        self.assertEqual(report["measured"], {})
        self.assertEqual(report["bundle"]["manifest_sha256"], self.manifest_sha256)
        json.dumps(report, allow_nan=False)  # strict JSON

    def test_without_bundle(self):
        self.add_many(3)
        self.study.save()
        report = preflight.run_preflight(self.study.path)
        self.assertEqual(report["counts"]["included"], 3)
        self.assertFalse(report["blocked"], report["checks"])
        self.assertIsNone(report["bundle"])


class SubjectLevelBlockTest(_Fixture):
    def test_missing_unreadable_not_3d_empty_subvoxel(self):
        self.add_many(3)
        self.study.add_subject("sub-missing", lesion="lesions/nothing.nii.gz", space=SPACE)
        garbage = os.path.join(self.study.path, "lesions", "sub-bad_mask.nii.gz")
        with open(garbage, "wb") as f:
            f.write(b"not a nifti file")
        self.study.add_subject("sub-bad", lesion=self.study.relpath(garbage), space=SPACE, volume_ml=1)
        self.add("sub-4d", data=np.stack([self.lesion_array(), self.lesion_array()], axis=-1))
        self.add("sub-empty", data=np.zeros(self.bundle.shape, np.uint8))
        self.add("sub-tiny", fractional_sum=0.4)
        report = self.preflight()
        expected = {"lesion_missing": "sub-missing", "lesion_unreadable": "sub-bad", "lesion_not_3d": "sub-4d", "lesion_empty": "sub-empty", "lesion_subvoxel": "sub-tiny"}
        for check_id, sid in expected.items():
            check = _check(report, check_id)
            self.assertEqual((check["level"], check["scope"], check["subjects"], check["excludes"]), ("block", "subject", [sid], True))
            self.assertIn(sid, report["excluded"])
            self.assertEqual(check["fix"]["id"], preflight.FIX_LIBRARY)
        # Subject-level blocks exclude, they do not block the run ...
        self.assertFalse(report["blocked"])
        self.assertEqual(report["counts"]["included"], 3)
        self.assertEqual(report["excluded"]["sub-missing"], "lesion file missing")
        # ... unless strict
        strict = self.preflight(strict=True)
        self.assertTrue(strict["blocked"])
        self.assertEqual({check["id"] for check in preflight.blocking_checks(strict)}, set(expected))

    def test_stale_metrics_are_measured(self):
        self.add_many(2)
        self.add("sub-stale", fresh=False)
        self.add("sub-staleempty", data=np.zeros(self.bundle.shape, np.uint8), fresh=False, volume_ml=1.0, fractional_sum=125)
        changed = self.add("sub-changed")
        nifti.write_nifti(changed, self.lesion_array(radius=3), self.bundle.affine)  # the file changed after its metrics
        os.utime(changed, ns=(time.time_ns(), time.time_ns() + 10**9))
        report = self.preflight()
        stale = _check(report, "metrics_stale")
        self.assertEqual((stale["level"], sorted(stale["subjects"])), ("warn", ["sub-changed", "sub-stale", "sub-staleempty"]))
        self.assertEqual(stale["fix"]["id"], preflight.FIX_REMEASURE)
        self.assertEqual(sorted(report["measured"]), ["sub-changed", "sub-stale", "sub-staleempty"])
        measured = report["measured"]["sub-changed"]
        voxels = float(self.lesion_array(radius=3).sum())
        self.assertAlmostEqual(measured["volume_ml"], voxels * 0.008, places=5)
        self.assertAlmostEqual(measured["fractional_sum"], voxels, places=4)  # on the connectome grid itself
        self.assertEqual(measured["lesion_size"], os.path.getsize(changed))
        # The cached volume of a stale row is not trusted: the measured (empty) lesion is excluded
        self.assertEqual(report["excluded"].get("sub-staleempty"), "empty lesion")
        # remeasure() posts the values for the session to merge (never writes participants.tsv)
        values = preflight.remeasure(self.study, ["sub-stale"], self.bundle, post=True)
        self.assertEqual(set(values), {"sub-stale"})
        inbox = os.path.join(self.study.path, ".studio", "inbox")
        self.assertEqual(len(os.listdir(inbox)), 1)
        self.study.apply_inbox(save=False)
        self.assertEqual(self.study.value("sub-stale", "lesion_size", int), os.path.getsize(self.study.abspath(self.study.subject("sub-stale")["lesion"])))
        self.assertNotIn("sub-stale", _check(self.preflight(), "metrics_stale")["subjects"])

    def test_fractional_sum_needs_the_connectome_grid(self):
        # 4 voxels of 1 mm fill half a 2 mm connectome voxel: a sum on the lesion's own grid must not hide that
        affine = np.diag([1.0, 1.0, 1.0, 1.0])
        affine[:3, 3] = self.bundle.affine[:3, 3]
        data = np.zeros((40, 48, 40), np.uint8)
        data[20:22, 20:22, 20] = 1
        self.add_many(3)
        self.add("sub-small", data=data, affine=affine, fresh=False)
        values = preflight.remeasure(self.study, ["sub-small"])
        self.assertIsNone(values["sub-small"]["fractional_sum"])
        self.assertAlmostEqual(values["sub-small"]["volume_ml"], 0.004)
        self.study.set("sub-small", values["sub-small"])
        report = self.preflight()
        self.assertNotIn("sub-small", report["measured"])  # the stored metrics are fresh ...
        self.assertEqual(_check(report, "lesion_subvoxel")["subjects"], ["sub-small"])  # ... and estimated on the connectome grid
        self.assertAlmostEqual(preflight.remeasure(self.study, ["sub-small"], self.bundle)["sub-small"]["fractional_sum"], 0.5, places=4)
        # A sum measured on a finer grid than the connectome's is capped by volume_ml / connectome voxel
        self.study.set("sub-small", fractional_sum=4.0)
        self.assertEqual(_check(self.preflight(), "lesion_subvoxel")["subjects"], ["sub-small"])


class RunLevelBlockTest(_Fixture):
    def test_terms_not_accepted(self):
        self.add_many(3)
        for accepted in (False, None, "0" * 64):
            report = self.preflight(accepted_terms=accepted)
            self.assertTrue(report["blocked"])
            check = _check(report, "terms_not_accepted")
            self.assertEqual((check["scope"], check["fix"]["id"]), ("run", preflight.FIX_ACCEPT_TERMS))
        for accepted in (True, self.manifest_sha256, [self.manifest_sha256]):
            self.assertNotIn("terms_not_accepted", _ids(self.preflight(accepted_terms=accepted)))

    def test_mixed_families_need_a_transform(self):
        self.add_many(3)
        self.add_many(2, prefix="sub-c", space="MNI152NLin2009cAsym")
        report = self.preflight(transforms=[])
        self.assertTrue(report["blocked"])
        check = _check(report, "mixed_families")
        self.assertEqual((check["level"], check["fix"]["id"]), ("block", preflight.FIX_TRANSFORM))
        self.assertEqual(check["subjects"], ["sub-c001", "sub-c002"])
        # With the 2009c -> NLin6 transform the run may go on; the family difference stays a warning
        report = self.preflight(transforms=["tpl-MNI152NLin6Asym_from-MNI152NLin2009cAsym_mode-image_xfm.h5"])
        self.assertNotIn("mixed_families", _ids(report))
        self.assertFalse(report["blocked"], report["checks"])
        family = _check(report, "space_family")
        self.assertEqual((family["level"], family["subjects"], family["fix"]["params"]["available"]), ("warn", ["sub-c001", "sub-c002"], True))
        # Without a connectome: mixed families block unless a transform links them
        self.assertIn("mixed_families", _ids(self.preflight(bundle=None, transforms=[])))
        self.assertNotIn("mixed_families", _ids(self.preflight(bundle=None, transforms=[("MNI152NLin2009cAsym", SPACE)])))

    def test_space_family_other_than_connectome(self):
        self.add_many(3, space="MNI152NLin2009cAsym")
        report = self.preflight(transforms=[])
        self.assertFalse(report["blocked"], report["checks"])
        check = _check(report, "space_family")
        self.assertEqual((check["level"], len(check["subjects"]), check["fix"]["id"]), ("warn", 3, preflight.FIX_TRANSFORM))

    def test_design_checks_are_shared(self):
        for index in range(2):
            self.add(f"sub-a{index}", group="case")
        for index in range(5):
            self.add(f"sub-b{index}", group="control")
        spec = {"test": "two_group", "levels": ["case", "control"]}
        report = self.preflight(design_spec=spec)
        self.assertTrue(report["blocked"])
        small = _check(report, "small_group")
        self.assertEqual((small["level"], small["scope"], small["detail"]), ("block", "run", {"case": 2}))
        # The design's own checks come from design.build_design on the included subjects, prefixed "design."
        built = design.build_design(self.study, spec)
        self.assertEqual(sorted(cid for cid in _ids(report) if cid.startswith("design.")), sorted(f"design.{c.code}" for c in built.checks))
        self.assertIn("design.small_group", _ids(report, "warn"))
        # A blocking design check blocks the run; a spec error is a block too
        self.study.set("sub-a0", volume_ml=0.5)
        for sid in self.study.subject_ids:
            self.study.set(sid, volume_ml=0.5)
        constant = self.preflight(design_spec=spec)
        self.assertIn("design.constant_covariate", _ids(constant, "block"))
        error = self.preflight(design_spec={"test": "two_group", "covariates": ["age"]})
        self.assertIn("design.error", _ids(error, "block"))

    def test_design_volume_covariate_uses_measured_volumes(self):
        # A study without stored volumes (as made from a BIDS dataset): the default lesion-volume covariate
        # takes the volumes this preflight measures, as the run does, instead of dropping every subject.
        for index in range(10):
            data = self.lesion_array(radius=2 + index % 3)
            self.add(f"sub-{index:02d}", data, fresh=False, group="case" if index < 5 else "control", volume_ml="n/a", fractional_sum="n/a")
        report = self.preflight(design_spec=design.DesignSpec(test="two_group").to_dict())
        self.assertEqual(design.DesignSpec(test="two_group").covariates, [design.VOLUME_COLUMN])
        self.assertEqual(len(report["measured"]), 10)
        self.assertNotIn("design.error", _ids(report), report["checks"])
        self.assertFalse(report["blocked"], [check["message"] for check in report["checks"] if check["level"] == "block"])
        self.assertTrue(all(self.study.value(sid, design.VOLUME_COLUMN) is None for sid in self.study.subject_ids))  # the study is not changed

    def test_disk_space(self):
        self.add_many(3)
        usage = shutil.disk_usage(self.tmp)
        with mock.patch.object(preflight.shutil, "disk_usage", return_value=usage._replace(free=1024)):
            report = self.preflight()
        check = _check(report, "disk_space")
        self.assertTrue(report["blocked"])
        self.assertEqual(check["detail"]["need_bytes"], preflight.disk_estimate(3, self.bundle.n_voxels))
        self.assertEqual(preflight.disk_estimate(10, 1000), 10 * 1000 * 4 * 3 * 2 + preflight.DISK_HEADROOM_BYTES)
        self.assertNotIn("disk_space", _ids(self.preflight()))

    def test_no_eligible_lesion(self):
        report = self.preflight()  # an empty study has nothing to analyse
        self.assertIn("no_eligible", _ids(report, "block"))
        self.assertTrue(report["blocked"])
        self.add_many(2, include="0")
        report = self.preflight()
        self.assertIn("no_eligible", _ids(report, "block"))
        self.assertEqual(_check(report, "include_off")["level"], "info")

    def test_include_reads_like_the_analyses(self):
        # A hand-edited participants.tsv may say no or FALSE: preflight excludes what Study.analysis_rows excludes
        self.add_many(5)
        self.study.save()
        path = os.path.join(self.study.path, "participants.tsv")
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        column = lines[0].split("\t").index("include")
        for index, value in ((3, "no"), (4, " FALSE")):
            cells = lines[index].split("\t")
            cells[column] = value
            lines[index] = "\t".join(cells)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        study = Study.open(self.study.path)
        _, excluded = study.analysis_rows()
        report = preflight.run_preflight(study, bundle=self.bundle, accepted_terms=True)
        self.assertEqual(sorted(report["excluded"]), sorted(excluded["include=0"]))
        self.assertEqual(sorted(report["excluded"]), ["sub-003", "sub-004"])
        self.assertEqual(sorted(_check(report, "include_off")["subjects"]), ["sub-003", "sub-004"])

    def test_qc_fail_excluded_by_every_analysis(self):
        # A lesion marked "QC fail" in the Library is left out by the Cohort card's analyses (Study.analysis_rows:
        # map lesions, Overlap, Contrast, Symptom, VLSM) and by preflight, as Run everything leaves it out
        from LesionNetworkMappingLib import study as studylib

        paths = {}
        for index in range(4):
            paths[f"sub-a{index}"] = self.add(f"sub-a{index}", group="case")
        for index in range(4):
            self.add(f"sub-b{index}", group="control")
        self.study.set("sub-a1", qc="fail")
        self.study.set("sub-b2", qc="fail")
        self.study.set("sub-b3", qc="pass")
        failed = ["sub-a1", "sub-b2"]
        rows, excluded = self.study.analysis_rows()
        self.assertEqual(sorted(excluded.get(studylib.EXCLUDED_QC, [])), failed)
        self.assertEqual(len(rows), 6)
        self.assertNotIn("sub-a1", [row["participant_id"] for row in self.study.analysis_rows(include_only=False)[0]])
        self.assertIn("sub-a1", [row["participant_id"] for row in self.study.analysis_rows(exclude_qc_fail=False)[0]])
        lesions, excluded = cohortmaps.study_lesions(self.study)
        self.assertEqual(sorted(sid for sid, _path, _sha in lesions), sorted(row["participant_id"] for row in rows))
        self.assertEqual(sorted(excluded.get(studylib.EXCLUDED_QC, [])), failed)
        built = design.build_design(self.study, {"test": "two_group", "levels": ["case", "control"]})
        self.assertEqual(len(built.sids), 6)
        self.assertEqual(sorted(built.excluded.get(studylib.EXCLUDED_QC, [])), failed)
        report = self.preflight()
        self.assertEqual({sid: report["excluded"][sid] for sid in failed}, dict.fromkeys(failed, studylib.EXCLUDED_QC))
        self.assertEqual(sorted(sid for sid in self.study.subject_ids if sid not in report["excluded"]), sorted(row["participant_id"] for row in rows))
        check = _check(report, "qc_fail")
        self.assertEqual((check["level"], sorted(check["subjects"]), check["fix"]["id"]), ("info", failed, preflight.FIX_LIBRARY))
        self.assertIn("QC fail", check["message"])
        self.assertFalse(report["blocked"], report["checks"])
        # A QC-failed lesion whose file is gone is reported as QC fail, not as a missing file that blocks a strict run
        os.remove(paths["sub-a1"])
        strict = self.preflight(strict=True)
        self.assertEqual(strict["excluded"]["sub-a1"], studylib.EXCLUDED_QC)
        self.assertNotIn("lesion_missing", _ids(strict))


class WarningTest(_Fixture):
    def test_xform_nonbinary_outside(self):
        self.add_many(2)
        path = self.add("sub-xform")
        # Shift the sform by 1.5 mm (qform unchanged)
        with gzip.open(path, "rb") as f:
            raw = bytearray(f.read())
        header = np.frombuffer(bytes(raw[:348]), dtype=nifti._HEADER_DTYPE)[0].copy()
        header["srow_x"][3] += 1.5
        raw[:348] = header.tobytes()
        with gzip.open(path, "wb") as f:
            f.write(bytes(raw))
        stat = os.stat(path)
        self.study.set("sub-xform", lesion_mtime=round(stat.st_mtime, 3), lesion_size=stat.st_size)
        self.add("sub-prob", qc_flags="nonbinary")
        self.add("sub-out", outside_brain_pct=35.0)
        report = self.preflight()
        self.assertFalse(report["blocked"])
        xform = _check(report, "xform")
        self.assertEqual(xform["subjects"], ["sub-xform"])
        self.assertAlmostEqual(xform["detail"]["sub-xform"], 1.5, places=3)
        nonbinary = _check(report, "nonbinary")
        self.assertEqual((nonbinary["subjects"], nonbinary["fix"]["id"]), (["sub-prob"], preflight.FIX_PROBABILISTIC))
        self.assertNotIn("nonbinary", _ids(self.preflight(probabilistic=True)))
        self.assertEqual(_check(report, "outside_mask")["subjects"], ["sub-out"])

    def test_duplicates(self):
        first = self.add("sub-001", source_dataset="d1", source_id="P7")
        self.add("sub-002", source_dataset="d1", source_id="P7")
        self.add("sub-003")
        copy = os.path.join(self.study.path, "lesions", "sub-004_copy.nii.gz")
        shutil.copyfile(first, copy)
        stat = os.stat(copy)
        self.study.add_subject("sub-004", lesion=self.study.relpath(copy), space=SPACE, volume_ml=1, fractional_sum=33, lesion_mtime=round(stat.st_mtime, 3), lesion_size=stat.st_size)
        self.add("sub-005", duplicate_of="sub-003")
        report = self.preflight()
        self.assertEqual(sorted(_check(report, "duplicate_id")["subjects"]), ["sub-001", "sub-002"])
        self.assertEqual(sorted(_check(report, "duplicate_lesion")["subjects"]), ["sub-001", "sub-004", "sub-005"])

    def test_review_gate_and_overrides(self):
        self.add_many(3, review_status="approved")
        self.add("sub-pending", review_status="pending")
        self.study.review_required = True
        report = self.preflight()
        check = _check(report, "unreviewed")
        self.assertEqual((check["level"], check["subjects"], check["excludes"], check["fix"]["id"]), ("warn", ["sub-pending"], True, preflight.FIX_INCLUDE))
        self.assertEqual(report["excluded"], {"sub-pending": "review_status != approved"})
        overridden = self.preflight(overrides=["unreviewed"])
        self.assertFalse(_check(overridden, "unreviewed")["excludes"])
        self.assertIn("sub-pending", overridden["included"])
        self.assertIn("anyway", _check(overridden, "unreviewed")["message"])
        # Automatic segmentations need a person's approval even when the study does not require review
        self.study.review_required = False
        self.add("sub-auto", trace_method="auto:nnunet", review_status="pending")
        report = self.preflight()
        self.assertEqual(_check(report, "auto_unreviewed")["subjects"], ["sub-auto"])
        self.assertIn("sub-auto", report["excluded"])
        self.assertNotIn("unreviewed", _ids(report))

    def test_native_normalization_and_template(self):
        self.add_many(3)
        self.add("sub-native", space="native")
        self.add("sub-normfail", norm_method="brainsfit-cfm:fast", norm_qc="fail")
        native = self.write_lesion("sub-normok", name="sub-normok_space-native_desc-lesion_mask.nii.gz")
        self.add("sub-normok", norm_method="brainsfit-cfm:fast", norm_qc="pass", lesion_native=self.study.relpath(native))
        record_dir = os.path.join(self.study.path, "derivatives", "normalization", "sub-normok")
        os.makedirs(record_dir)
        with open(os.path.join(record_dir, "normalization.json"), "w", encoding="utf-8") as f:
            json.dump({"format": "slicer-lnm-normalization", "inputs": {"lesion": {"path": "x", "sha256": "0" * 64}}}, f)
        self.add("sub-traced2009", trace_template="MNI152NLin2009cAsym")
        report = self.preflight()
        self.assertEqual(_check(report, "native_space")["subjects"], ["sub-native"])
        self.assertEqual(report["excluded"]["sub-native"], "native space: not normalized")
        self.assertEqual(_check(report, "norm_qc")["subjects"], ["sub-normfail"])
        self.assertEqual(_check(report, "norm_stale")["subjects"], ["sub-normok"])
        self.assertIn("sub-normok", report["included"])
        self.assertEqual(_check(report, "template_family")["subjects"], ["sub-traced2009"])
        # A matching record is not stale
        with open(os.path.join(record_dir, "normalization.json"), "w", encoding="utf-8") as f:
            json.dump({"inputs": {"lesion": {"sha256": preflight._sha256(native)}}}, f)
        self.assertNotIn("norm_stale", _ids(self.preflight()))

    def test_cohort_columns(self):
        self.add_many(3, group="case", dimensionality="2D", flipped="1", source_type="case_report", doi="10.1/x")
        self.add_many(3, prefix="sub-k", group="control", dimensionality="3D")
        self.study.set("sub-001", doi="n/a")
        self.study.review_required = False  # case reports require review by default
        for sid in self.study.subject_ids:
            self.study.set(sid, {"aphasia": 3})
        self.study.set("sub-002", aphasia="n/a")
        self.study.set_column_role("aphasia", "score")
        report = self.preflight()
        self.assertEqual(sorted(_check(report, "flipped")["subjects"]), ["sub-001", "sub-002", "sub-003"])
        self.assertEqual(_check(report, "missing_scores")["subjects"], ["sub-002"])
        self.assertEqual(_check(report, "case_report_source")["subjects"], ["sub-001"])
        self.assertNotIn("nonnumeric_scores", _ids(report))
        # A value that is present but not a number is named too (a decimal comma, a typo), not silently dropped
        self.study.set("sub-003", aphasia="8,5")
        report = self.preflight()
        self.assertEqual(_check(report, "nonnumeric_scores")["subjects"], ["sub-003"])
        self.assertEqual(_check(report, "missing_scores")["subjects"], ["sub-002"])
        self.assertIn("not a number", _check(report, "nonnumeric_scores")["message"])
        self.study.set("sub-003", aphasia=3)
        share = _check(report, "dimensionality_share")
        self.assertEqual(share["detail"], {"case": 100.0, "control": 0.0})
        # With a design the design's own dimensionality check is used (not duplicated)
        with_design = self.preflight(design_spec={"test": "two_group", "levels": ["case", "control"]})
        self.assertNotIn("dimensionality_share", _ids(with_design))

    def test_rule_checks(self):
        self.add_many(3)
        report = self.preflight(recipe_rule={"kind": "t_convention", "value": 7})
        check = _check(report, "rule_n")
        self.assertEqual((check["level"], check["fix"]["id"]), ("warn", preflight.FIX_RULE_FWE))
        self.assertNotIn("rule_n", _ids(self.preflight(recipe_rule={"kind": "fwe_bonferroni", "alpha": 0.05})))
        self.assertNotIn("rule_n", _ids(self.preflight(recipe_rule={"kind": "top_pct", "value": 10})))
        self.assertNotIn("gsr_rule", _ids(report))
        tmp = os.path.join(self.tmp, "nogsr.lnmc")
        shutil.copytree(self.bundle_dir, tmp)
        connectome.set_metadata(tmp, preprocessing={"gsr": False})
        report = self.preflight(bundle=tmp, recipe_rule={"kind": "fwe_bonferroni", "alpha": 0.05}, accepted_terms=True)
        self.assertEqual(_check(report, "gsr_rule")["message"], preflight.thresholds.GSR_BANNER)
        self.assertNotIn("gsr_rule", _ids(self.preflight(bundle=tmp, recipe_rule={"kind": "fz_abs", "value": 0.1})))


class InfoTest(_Fixture):
    def test_radiological_ambiguous_grid_and_paths(self):
        self.add_many(2)
        las = self.bundle.affine.copy()
        las[0, 0] = -las[0, 0]
        las[0, 3] = -las[0, 3]
        self.add("sub-las", data=self.lesion_array()[::-1], affine=las)
        tf = np.diag([2.0, 2.0, 2.0, 1.0])
        tf[:3, 3] = (-96.5, -132.5, -78.5)
        data = np.zeros((97, 115, 97), np.uint8)
        data[40:44, 50:54, 40:44] = 1
        self.add("sub-2009", data=data, affine=tf, space="n/a")
        report = self.preflight(transforms=[])
        self.assertEqual(_check(report, "radiological")["subjects"], ["sub-las"])
        ambiguous = _check(report, "ambiguous_grid")
        self.assertEqual((ambiguous["level"], ambiguous["subjects"]), ("info", ["sub-2009"]))
        self.assertIn("MNI152NLin2009aSym", ambiguous["message"])
        self.study.grid_answers = {"TF2009-2mm": "MNI152NLin2009cAsym"}
        self.assertNotIn("ambiguous_grid", _ids(self.preflight(transforms=[])))
        self.assertEqual(_check(self.preflight(out_dir=os.path.join(self.tmp, "résultats")), "non_ascii_path")["level"], "info")


class SaveAndCommandLineTest(_Fixture):
    def test_save_report(self):
        self.add_many(3)
        report = self.preflight()
        path = preflight.save_report(self.study, report)
        self.assertEqual(os.path.dirname(path), os.path.join(self.study.path, "derivatives", "qc"))
        self.assertRegex(os.path.basename(path), r"^preflight-\d{8}T\d{6}Z(-\d+)?\.json$")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        saved = json.loads(text)
        self.assertEqual((saved["format"], saved["run_id"]), (preflight.FORMAT_NAME, report["run_id"]))
        self.assertNotIn(self.tmp, text)  # no absolute local path (SPEC 9.7)
        record = runs.load(self.study.path)[report["run_id"]]
        self.assertEqual((record["kind"], record["status"], record["summary"]["file"]), ("preflight", "done", os.path.basename(path)))
        second = preflight.save_report(self.study.path, self.preflight())
        self.assertNotEqual(second, path)

    def _cli(self, *args):
        env = dict(os.environ, PYTHONPATH=MODULE_DIR + os.pathsep + os.environ.get("PYTHONPATH", ""))
        return subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.preflight", "--study", self.study.path, *args], cwd=MODULE_DIR, env=env, capture_output=True, text=True, timeout=120, check=False)

    def test_command_line_exit_codes(self):
        self.add_many(3)
        self.study.save()
        out = os.path.join(self.tmp, "report.json")
        ok = self._cli("--bundle", self.bundle_dir, "--accepted-terms", "--json", out, "--rule", '{"kind": "fwe_bonferroni"}')
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        events = [json.loads(line) for line in ok.stdout.splitlines() if line.strip()]
        self.assertEqual((events[0]["event"], events[-1]["event"], events[-1]["blocked"]), ("start", "done", False))
        with open(out, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["counts"]["included"], 3)
        blocked = self._cli("--bundle", self.bundle_dir, "--save")
        self.assertEqual(blocked.returncode, preflight.EXIT_BLOCKED, blocked.stdout + blocked.stderr)
        events = [json.loads(line) for line in blocked.stdout.splitlines() if line.strip()]
        self.assertIn("terms_not_accepted", [event.get("code") for event in events if event["event"] == "warning"])
        self.assertEqual(len(os.listdir(os.path.join(self.study.path, "derivatives", "qc"))), 1)
        usage = self._cli("--bundle", self.bundle_dir, "--no-such-option")
        self.assertEqual(usage.returncode, jobs.EXIT_USAGE)


class PerformanceTest(_Fixture):
    def test_300_lesions_in_one_second_without_voxel_reads(self):
        for index in range(300):
            self.add(f"sub-{index:03d}", data=self.lesion_array(radius=1 + index % 3), group="case" if index % 2 else "control", age=40 + index % 30)
        self.study.save()
        spec = {"test": "two_group", "levels": ["case", "control"]}

        def fail(*args, **kwargs):
            raise AssertionError("preflight read voxel data")

        timings = []
        with mock.patch.object(nifti, "read_nifti", side_effect=fail):
            for _ in range(3):
                started = time.perf_counter()
                study = Study.open(self.study.path)
                report = preflight.run_preflight(study, bundle=self.bundle_dir, recipe_rule={"kind": "fwe_bonferroni"}, design_spec=spec, accepted_terms=True)
                timings.append(time.perf_counter() - started)
        self.assertFalse(report["blocked"], report["checks"])
        self.assertEqual(report["counts"]["included"], 300)
        self.assertEqual(report["measured"], {})
        self.assertLessEqual(min(timings), 1.0, timings)
        print(f"\n300-lesion preflight (open + checks + design): {', '.join(f'{t * 1000:.0f} ms' for t in timings)}", file=sys.stderr)

    def test_300_uncompressed_lesions_of_equal_size(self):
        # Uncompressed lesions on one grid all have the same size: hashing them all would read every voxel
        for index in range(300):
            self.add(f"sub-{index:03d}", data=self.lesion_array(radius=1 + index % 3), name=f"sub-{index:03d}_space-{SPACE}_desc-lesion_mask.nii")
        first = self.study.abspath(self.study.subject("sub-000")["lesion"])
        shutil.copyfile(first, os.path.join(self.study.path, "lesions", "copy.nii"))
        stat = os.stat(os.path.join(self.study.path, "lesions", "copy.nii"))
        self.study.add_subject("sub-copy", lesion="lesions/copy.nii", space=SPACE, volume_ml=1, fractional_sum=33, lesion_mtime=round(stat.st_mtime, 3), lesion_size=stat.st_size)
        self.study.save()
        size = os.path.getsize(first)
        budget = 100 * size  # stands in for HASH_BUDGET_BYTES: about a third of the files per preflight
        preflight._SHA_CACHE.clear()
        hashed = []
        real_sha256 = preflight._sha256

        def counting(path, *args):
            hashed.append(path)
            return real_sha256(path, *args)

        timings, reports = [], []
        with mock.patch.object(preflight, "_sha256", side_effect=counting):
            for _ in range(4):
                preflight._SHA_CACHE.clear()  # a new process each time: only the study's hash cache remains
                started = time.perf_counter()
                reports.append(preflight.run_preflight(Study.open(self.study.path), bundle=self.bundle_dir, accepted_terms=True, hash_budget=budget))
                timings.append(time.perf_counter() - started)
        self.assertEqual(len(hashed), 301)  # every file hashed once, over the preflights, within the budget
        self.assertEqual(len(set(hashed)), 301)
        first_report, last = reports[0], reports[-1]
        deferred = _check(first_report, "hash_deferred")
        self.assertEqual((deferred["level"], len(deferred["subjects"])), ("info", 201))
        self.assertNotIn("hash_deferred", _ids(last))
        self.assertEqual(sorted(_check(last, "duplicate_lesion")["subjects"]), ["sub-000", "sub-copy"])
        self.assertTrue(os.path.isfile(os.path.join(self.study.path, *preflight.HASH_CACHE_RELPATH)))
        # With the default budget (64 MB) and a warm hash cache the preflight is fast
        with mock.patch.object(nifti, "read_nifti", side_effect=AssertionError("preflight read voxel data")), mock.patch.object(preflight, "_sha256", side_effect=AssertionError("preflight hashed a cached file")):
            preflight._SHA_CACHE.clear()
            started = time.perf_counter()
            report = preflight.run_preflight(Study.open(self.study.path), bundle=self.bundle_dir, accepted_terms=True)
            elapsed = time.perf_counter() - started
        self.assertLessEqual(elapsed, 1.0)
        self.assertEqual(report["counts"]["included"], 301)
        print(f"\n300 uncompressed lesions: {', '.join(f'{t * 1000:.0f} ms' for t in timings)} (budgeted), {elapsed * 1000:.0f} ms (cached)", file=sys.stderr)


class AssertRunOutputsTest(_Fixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.run_tmp = tempfile.mkdtemp(prefix="lnm-preflight-run-")
        lesion = os.path.join(cls.run_tmp, "sub-01_lesion.nii.gz")
        shape = cls.bundle.shape
        center = cls.meta["blobs"]["A"]["center_ijk"]
        ijk = np.indices(shape).reshape(3, -1).T
        nifti.write_nifti(lesion, (np.linalg.norm(ijk - np.asarray(center), axis=1) <= 2).reshape(shape).astype(np.uint8), cls.bundle.affine)
        cls.run_dir = os.path.join(cls.run_tmp, "run")
        with contextlib.redirect_stdout(io.StringIO()):
            code = engine.main(["--bundle", cls.bundle_dir, "--lesion", lesion, "--out", cls.run_dir, "--run-json", os.path.join(cls.run_dir, "run.json"), "--threshold-rule", '{"kind": "fwe_bonferroni"}'])
        assert code == 0
        cls.export_dir = os.path.join(cls.run_tmp, "export")
        export.export_run(cls.run_dir, cls.export_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.run_tmp, ignore_errors=True)
        super().tearDownClass()

    def copy(self, source, name):
        target = os.path.join(self.tmp, name)
        shutil.copytree(source, target)
        return target

    def run_record(self, folder):
        with open(os.path.join(folder, "run.json"), encoding="utf-8") as f:
            return json.load(f)

    def relocate_run(self, folder):
        """Point a copied run.json at the copied maps."""
        record = self.run_record(folder)
        record["maps"] = {name: {key: os.path.join(folder, os.path.basename(path)) for key, path in maps.items()} for name, maps in record["maps"].items()}
        with open(os.path.join(folder, "run.json"), "w", encoding="utf-8") as f:
            json.dump(record, f)
        return record

    def codes(self, failures):
        return sorted({failure["code"] for failure in failures})

    def test_good_outputs_pass(self):
        n = self.bundle.n_subjects
        self.assertEqual(preflight.assert_run_outputs(os.path.join(self.run_dir, "run.json"), n), [])
        self.assertEqual(preflight.assert_run_outputs(self.run_dir, n, reference_header=self.bundle), [])
        self.assertEqual(preflight.assert_run_outputs(self.export_dir, n), [])
        self.assertEqual(preflight.assert_run_outputs(os.path.join(self.export_dir, "provenance.json"), n, reference_header=os.path.join(self.bundle_dir, "mask.nii.gz")), [])

    def test_nan_map(self):
        folder = self.copy(self.run_dir, "nan")
        record = self.relocate_run(folder)
        path = next(iter(record["maps"].values()))["AvgFz"]
        data, affine, _ = nifti.read_nifti(path)
        data = np.array(data, dtype=np.float32)
        data[3, 3, 3] = np.nan
        nifti.write_nifti(path, data, affine)
        failures = preflight.assert_run_outputs(folder, self.bundle.n_subjects, reference_header=self.bundle)
        self.assertEqual(self.codes(failures), ["non_finite"])
        self.assertEqual(failures[0]["detail"]["count"], 1)
        # A constant map is caught as well
        nifti.write_nifti(path, np.zeros_like(data), affine)
        self.assertEqual(self.codes(preflight.assert_run_outputs(folder, self.bundle.n_subjects, reference_header=self.bundle)), ["constant_map"])

    def test_wrong_n(self):
        failures = preflight.assert_run_outputs(os.path.join(self.run_dir, "run.json"), 100)
        self.assertEqual(self.codes(failures), ["wrong_n"])
        self.assertEqual(failures[0]["detail"], {"n": self.bundle.n_subjects, "expected": 100})
        self.assertEqual(self.codes(preflight.assert_run_outputs(self.export_dir, 100)), ["wrong_n"])
        # A stopped run may have fewer subjects when it says partial, also in its sidecars
        folder = self.copy(self.export_dir, "partial")
        with open(os.path.join(folder, "provenance.json"), encoding="utf-8") as f:
            provenance = json.load(f)
        provenance["subset"]["partial"] = True
        with open(os.path.join(folder, "provenance.json"), "w", encoding="utf-8") as f:
            json.dump(provenance, f)
        self.assertEqual(self.codes(preflight.assert_run_outputs(folder, 100)), ["partial_not_labelled"])
        for lesion in provenance["lesions"]:
            sidecar_path = os.path.join(folder, lesion["sidecar"])
            with open(sidecar_path, encoding="utf-8") as f:
                sidecar = json.load(f)
            sidecar["subset"]["partial"] = True
            with open(sidecar_path, "w", encoding="utf-8") as f:
                json.dump(sidecar, f)
        self.assertEqual(preflight.assert_run_outputs(folder, 100), [])

    def test_header_mismatch(self):
        folder = self.copy(self.run_dir, "header")
        record = self.relocate_run(folder)
        path = next(iter(record["maps"].values()))["T"]
        data, affine, _ = nifti.read_nifti(path)
        shifted = affine.copy()
        shifted[0, 3] += 2.0
        nifti.write_nifti(path, data, shifted)
        failures = preflight.assert_run_outputs(folder, self.bundle.n_subjects, reference_header=self.bundle)
        self.assertEqual(self.codes(failures), ["header_mismatch"])
        self.assertIn("srow_x", failures[0]["detail"]["fields"])
        # Without a reference the maps must agree with each other
        self.assertEqual(self.codes(preflight.assert_run_outputs({"maps": record["maps"], "n": self.bundle.n_subjects}, self.bundle.n_subjects)), ["header_mismatch"])

    def test_missing_files_and_sidecars(self):
        folder = self.copy(self.export_dir, "sidecars")
        with open(os.path.join(folder, "provenance.json"), encoding="utf-8") as f:
            provenance = json.load(f)
        lesion = provenance["lesions"][0]
        os.remove(os.path.join(folder, lesion["maps"]["AvgR"]))
        sidecar_path = os.path.join(folder, lesion["sidecar"])
        with open(sidecar_path, encoding="utf-8") as f:
            sidecar = json.load(f)
        del sidecar["disclaimer"]
        with open(sidecar_path, "w", encoding="utf-8") as f:
            json.dump(sidecar, f)
        failures = preflight.assert_run_outputs(folder, self.bundle.n_subjects)
        self.assertEqual(self.codes(failures), ["missing_file", "sidecar_incomplete"])
        self.assertEqual([f["detail"]["missing"] for f in failures if f["code"] == "sidecar_incomplete"], [["disclaimer"]])
        os.remove(sidecar_path)
        self.assertIn("missing_sidecar", self.codes(preflight.assert_run_outputs(folder, self.bundle.n_subjects)))
        self.assertEqual(self.codes(preflight.assert_run_outputs(os.path.join(self.tmp, "nowhere"), 6)), ["missing_record", "no_outputs"])

    def test_cohortmaps_maps_tsv(self):
        folder = os.path.join(self.tmp, "cohort")
        os.makedirs(folder)
        record = self.run_record(self.run_dir)
        maps = next(iter(record["maps"].values()))
        rows = [{"participant_id": f"sub-0{index}", "lesion": "x.nii.gz", "AvgR": maps["AvgR"], "AvgFz": maps["AvgFz"], "T": maps["T"]} for index in (1, 2)]
        cohortmaps.write_maps_tsv(os.path.join(folder, cohortmaps.MAPS_TSV), rows)
        with open(os.path.join(folder, cohortmaps.COHORTMAPS_JSON), "w", encoding="utf-8") as f:
            json.dump({"format": cohortmaps.FORMAT_NAME, "status": "done", "n": self.bundle.n_subjects, "bundle": record["bundle"]}, f)
        tsv = os.path.join(folder, cohortmaps.MAPS_TSV)
        self.assertEqual(preflight.assert_run_outputs(tsv, self.bundle.n_subjects, kind="cohortmaps", expected_lesions=2), [])
        self.assertEqual(preflight.assert_run_outputs(folder, self.bundle.n_subjects, expected_lesions=2), [])
        self.assertEqual(self.codes(preflight.assert_run_outputs(tsv, self.bundle.n_subjects, expected_lesions=3)), ["wrong_lesion_count"])
        self.assertEqual(self.codes(preflight.assert_run_outputs(tsv, 50, expected_lesions=2)), ["wrong_n"])

    def test_constant_map_names_its_participant(self):
        """A constant map of a maps.tsv is named by its participant, not only by its file (a cache name)."""
        folder = os.path.join(self.tmp, "cohort constant")
        os.makedirs(folder)
        record = self.run_record(self.run_dir)
        maps = next(iter(record["maps"].values()))
        flat = {}
        for key, path in maps.items():
            flat[key] = os.path.join(folder, "lesion-5a1b45d615e2_" + os.path.basename(path))
            shutil.copyfile(path, flat[key])
        data, affine, _ = nifti.read_nifti(flat["AvgR"])
        nifti.write_nifti(flat["AvgR"], np.zeros_like(np.asarray(data, dtype=np.float32)), affine)
        rows = [{"participant_id": "sub-01", "lesion": "x.nii.gz", **maps}, {"participant_id": "sub-03", "lesion": "y.nii.gz", **flat}]
        cohortmaps.write_maps_tsv(os.path.join(folder, cohortmaps.MAPS_TSV), rows)
        with open(os.path.join(folder, cohortmaps.COHORTMAPS_JSON), "w", encoding="utf-8") as f:
            json.dump({"format": cohortmaps.FORMAT_NAME, "status": "done", "n": self.bundle.n_subjects, "bundle": record["bundle"]}, f)
        failures = preflight.assert_run_outputs(os.path.join(folder, cohortmaps.MAPS_TSV), self.bundle.n_subjects, kind="cohortmaps", expected_lesions=2)
        self.assertEqual(self.codes(failures), ["constant_map"])
        self.assertTrue(failures[0]["message"].startswith("The map of sub-03 (lesion-5a1b45d615e2_"), failures[0]["message"])


class GroupRecordTest(_Fixture):
    """assert_run_outputs on real glm.json and cohort.json records (16 lesion maps of a cohortmaps run)."""

    def setUp(self):
        super().setUp()
        rng = np.random.default_rng(7)
        maps_dir = os.path.join(self.study.path, "derivatives", "lnm", "cohortmaps-1")
        os.makedirs(maps_dir)
        lines = ["participant_id\tAvgFz\tT\tvolume_ml\tmanifest_sha256\tsubject_ids_sha256\tstatistic\tsupersample\tprobabilistic\tlibrary_version"]
        blob = np.zeros(self.bundle.shape)
        blob[6:12, 8:14, 6:12] = 1.0
        for index in range(16):
            sid, case = f"sub-{index:02d}", index < 8
            self.study.add_subject(sid, group="case" if case else "control", volume_ml=5.0 + index)
            fz = 0.3 * rng.standard_normal(self.bundle.shape) + (0.6 * blob if case else 0.0)
            fz_path = os.path.join(maps_dir, f"{sid}_desc-AvgRFz_funcmap.nii.gz")
            t_path = os.path.join(maps_dir, f"{sid}_desc-T_funcmap.nii.gz")
            nifti.write_nifti(fz_path, fz.astype(np.float32), self.bundle.affine)
            nifti.write_nifti(t_path, (10 * fz).astype(np.float32), self.bundle.affine)
            lines.append(f"{sid}\t{fz_path}\t{t_path}\t{5.0 + index}\tabc\tdef\tT\t2\tfalse\t1.0")
        self.study.save()
        self.maps_tsv = os.path.join(maps_dir, "maps.tsv")
        with open(self.maps_tsv, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        with open(os.path.join(maps_dir, "cohortmaps.json"), "w", encoding="utf-8") as f:
            json.dump({"format": "slicer-lnm-cohortmaps", "status": "done", "n": self.bundle.n_subjects, "bundle": {"name": "Synthetic"}}, f)
        self.mask = os.path.join(self.bundle_dir, "mask.nii.gz")
        self.participants = os.path.join(self.study.path, "participants.tsv")

    def run_job(self, main, argv, job):
        with contextlib.redirect_stdout(io.StringIO()):
            code = jobs.run_job(main, argv, job=job, exit=False, orphan=False, signals=False)
        jobs.reset_cancel()
        self.assertEqual(code, 0)

    def codes(self, failures):
        return sorted({failure["code"] for failure in failures})

    def test_glm_record(self):
        out = os.path.join(self.tmp, "glm")
        spec = design.DesignSpec(contrast="A>B", inference={"n_perm": 1000, "seed": 3}).save(os.path.join(self.tmp, "design.json"))
        self.run_job(glm.main, ["--maps", self.maps_tsv, "--participants", self.participants, "--design", spec, "--mask", self.mask, "--out", out, "--threads", "1", "--perm", "100"], "glm")
        record = os.path.join(out, "glm.json")
        n = self.bundle.n_subjects
        # "n" of glm.json is the number of lesions (16); N is the connectome N of the cohortmaps run
        self.assertEqual(preflight.assert_run_outputs(record, n, reference_header=self.mask, expected_lesions=16), [])
        self.assertEqual(preflight.assert_run_outputs(out, n, kind="map"), [])  # the kind comes from the record
        self.assertEqual(self.codes(preflight.assert_run_outputs(record, 1000)), ["wrong_n"])
        self.assertEqual(self.codes(preflight.assert_run_outputs(record, n, expected_lesions=17)), ["wrong_lesion_count"])
        stat = next(os.path.join(out, name) for name in os.listdir(out) if name.endswith("Tstat_map.nii.gz"))
        data, affine, _ = nifti.read_nifti(stat)
        data = np.array(data, dtype=np.float32)
        data[2, 2, 2] = np.nan
        nifti.write_nifti(stat, data, affine)
        self.assertEqual(self.codes(preflight.assert_run_outputs(record, n)), ["non_finite"])
        nifti.write_nifti(stat, np.zeros_like(data), affine)
        self.assertEqual(self.codes(preflight.assert_run_outputs(record, n)), ["constant_map"])

    def test_cohort_record_and_null_result(self):
        n = self.bundle.n_subjects
        for name, extra in (("cohort", []), ("null", ["--rule", '{"kind": "t_convention", "value": 1000}'])):
            out = os.path.join(self.tmp, name)
            self.run_job(cohort.main, ["--maps", self.maps_tsv, "--participants", self.participants, "--mask", self.mask, "--out", out, "--n", str(n), "--json", *extra], "cohort")
            record = os.path.join(out, "cohort.json")
            # cohort.json states no df (none applies) and its thresholded maps may be constant (a null result)
            self.assertEqual(preflight.assert_run_outputs(record, n, reference_header=self.mask, expected_lesions=16), [], name)
            self.assertEqual(self.codes(preflight.assert_run_outputs(record, 50)), ["wrong_n"])


if __name__ == "__main__":
    unittest.main()

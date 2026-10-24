"""Tests of the review workflow (review.py; L-43, addendum 3.10).

Transitions and invalid transitions, the audit records, a rejection that sets include = 0 (kept while a reopened
rejection waits, restored only by an approval), automatic segmentations forced to pending whatever the study setting,
assisted provenance, blinded columns, the review gate of analyses (``Study.analysis_rows``, ``cohortmaps.study_lesions``,
``review.gate_rows``) and the recorded settings and overrides.

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingReviewTest.py -v
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

from LesionNetworkMappingLib import cohortmaps, nifti, review
from LesionNetworkMappingLib.study import EXCLUDED_AUTO, EXCLUDED_INCLUDE, EXCLUDED_REVIEW, Study


class ReviewTestBase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="lnm-review-")
        self.study = Study.create(os.path.join(self.folder, "study"), space="MNI152NLin6Asym")
        for sid, group in (("sub-01", "case"), ("sub-02", "case"), ("sub-03", "control"), ("sub-04", "control")):
            self.study.add_subject(sid, group=group, status="traced", trace_method="manual")
        self.study.save()

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def audit(self, event=None):
        return review.read_audit(self.study, event=event)


class TransitionTest(ReviewTestBase):
    def test_states_match_the_registry(self):
        from LesionNetworkMappingLib import schema

        self.assertEqual(set(schema.registered("review_status").levels), set(review.STATES))
        for target, sources in ((target, [s for s, targets in review.TRANSITIONS.items() if target in targets]) for target in review.STATES):
            for source in sources:
                self.assertIn(source, review.STATES)
                self.assertIn(target, review.STATES)

    def test_full_cycle_and_audit(self):
        study = self.study
        review.apply_transition(study, "sub-01", "pending", "tracer A")
        review.apply_transition(study, "sub-01", "changes", "Dr B", comment="extend the lesion to the insula")
        row = study.subject("sub-01")
        self.assertEqual((row["review_status"], row["reviewer"], row["review_comment"]), ("changes", "Dr B", "extend the lesion to the insula"))
        review.apply_transition(study, "sub-01", "pending", "tracer A")
        self.assertEqual(study.subject("sub-01")["review_comment"], "extend the lesion to the insula")  # kept for the reviewer
        review.apply_transition(study, "sub-01", "approved", "Dr B", save=True)
        reopened = Study.open(study.path)
        self.assertEqual(reopened.subject("sub-01")["review_status"], "approved")
        self.assertEqual(reopened.subject("sub-01")["reviewer"], "Dr B")
        records = self.audit(review.AUDIT_EVENT)
        self.assertEqual([(r["from"], r["to"]) for r in records], [("none", "pending"), ("pending", "changes"), ("changes", "pending"), ("pending", "approved")])
        for record in records:
            self.assertEqual(set(record) >= {"event", "sid", "from", "to", "who", "when", "comment"}, True)
            self.assertEqual(record["sid"], "sub-01")
            self.assertTrue(record["when"].endswith("Z"))
        self.assertEqual(records[1]["comment"], "extend the lesion to the insula")
        self.assertIsNone(records[0]["comment"])
        # One JSON object per line, and nothing absolute
        with open(review.audit_path(study), encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertNotIn(self.folder, "\n".join(lines))
        self.assertEqual(review.history(study, "sub-01"), records)

    def test_invalid_transitions_change_nothing(self):
        study = self.study
        cases = [
            ("approved", {}),  # none -> approved skips the review
            ("rejected", {"reason": "duplicate"}),
            ("changes", {"comment": "x"}),
            ("bogus", {}),
        ]
        for target, kwargs in cases:
            with self.assertRaises(review.TransitionError, msg=target):
                review.apply_transition(study, "sub-02", target, "Dr B", **kwargs)
        review.apply_transition(study, "sub-02", "pending", "tracer")
        with self.assertRaises(review.TransitionError):
            review.apply_transition(study, "sub-02", "changes", "Dr B")  # no comment
        with self.assertRaises(review.TransitionError):
            review.apply_transition(study, "sub-02", "changes", "Dr B", comment="   ")
        with self.assertRaises(review.TransitionError):
            review.apply_transition(study, "sub-02", "rejected", "Dr B")  # no reason
        with self.assertRaises(review.TransitionError):
            review.apply_transition(study, "sub-02", "rejected", "Dr B", reason="ugly")
        with self.assertRaises(review.TransitionError):
            review.apply_transition(study, "sub-02", "rejected", "Dr B", reason="other")  # "other" needs a comment
        with self.assertRaises(review.TransitionError):
            review.apply_transition(study, "sub-02", "approved", "Dr B", reason="duplicate")  # only a rejection has a reason
        with self.assertRaises(review.TransitionError):
            review.apply_transition(study, "sub-02", "pending", "tracer")  # already pending
        with self.assertRaises(review.TransitionError):
            review.apply_transition(study, "sub-02", "approved", "  ")  # nobody
        with self.assertRaises(KeyError):
            review.apply_transition(study, "sub-99", "pending", "tracer")
        self.assertEqual(study.subject("sub-02")["review_status"], "pending")
        self.assertEqual(len(self.audit(review.AUDIT_EVENT)), 1)
        self.assertEqual(review.allowed_transitions(study.subject("sub-02")), ("approved", "changes", "rejected"))

    def test_reject_sets_include_0_and_only_approval_restores_it(self):
        study = self.study
        review.apply_transition(study, "sub-03", "pending", "tracer")
        record = review.apply_transition(study, "sub-03", "rejected", "Dr B", reason="not a focal lesion", comment="diffuse leukoaraiosis")
        row = study.subject("sub-03")
        self.assertEqual(row["include"], "0")
        self.assertEqual(row["review_status"], "rejected")
        self.assertEqual(row["review_comment"], "not a focal lesion: diffuse leukoaraiosis")
        self.assertEqual((record["reason"], record["include_before"]), ("not a focal lesion", "1"))
        _rows, excluded = study.analysis_rows()
        self.assertIn("sub-03", excluded[EXCLUDED_INCLUDE])
        # Reopened (the tracer's Done, or Reopen review): still excluded, and waiting in the Needs review queue
        reopened = review.apply_transition(study, "sub-03", "pending", "tracer", extra={"via": "done", "edited": True})
        self.assertEqual(study.subject("sub-03")["include"], "0")
        self.assertEqual(reopened["include_held"], "0")
        self.assertNotIn("include_restored", reopened)
        self.assertEqual(reopened["via"], "done")
        self.assertTrue(review.needs_review(study.subject("sub-03")))
        self.assertEqual(review.held_include(study, "sub-03"), "1")
        # Changes requested, traced again, rejected again: the value before the first rejection is kept
        review.apply_transition(study, "sub-03", "changes", "Dr B", comment="trace the infarct only")
        self.assertEqual(study.subject("sub-03")["include"], "0")
        review.apply_transition(study, "sub-03", "pending", "tracer")
        again = review.apply_transition(study, "sub-03", "rejected", "Dr B", reason="duplicate")
        self.assertEqual(again["include_before"], "1")
        review.apply_transition(study, "sub-03", "pending", "Dr B", extra={"via": "library-reopen"})
        # Only a reviewer's approval brings it back
        approved = review.apply_transition(study, "sub-03", "approved", "Dr B")
        self.assertEqual(study.subject("sub-03")["include"], "1")
        self.assertEqual(approved["include_restored"], "1")
        self.assertIsNone(review.held_include(study, "sub-03"))
        self.assertNotIn("sub-03", study.analysis_rows()[1].get(EXCLUDED_INCLUDE, []))
        # Approved, then traced again and approved: nothing is restored twice
        review.apply_transition(study, "sub-03", "pending", "tracer")
        self.assertNotIn("include_restored", review.apply_transition(study, "sub-03", "approved", "Dr B"))
        # A lesion that was excluded before its rejection stays excluded, even when it is approved later
        study.set("sub-04", include=0)
        review.apply_transition(study, "sub-04", "pending", "tracer")
        review.apply_transition(study, "sub-04", "rejected", "Dr B", reason="duplicate")
        review.apply_transition(study, "sub-04", "pending", "tracer")
        self.assertEqual(study.subject("sub-04")["include"], "0")
        review.apply_transition(study, "sub-04", "approved", "Dr B")
        self.assertEqual(study.subject("sub-04")["include"], "0")

    def test_tracer_done_does_not_undo_a_rejection(self):
        """A study without required review: the tracer edits a rejected lesion and presses Done; it is not analysed."""
        study = self.study
        self.assertFalse(study.review_required)
        review.apply_transition(study, "sub-01", "pending", "tracer")
        review.apply_transition(study, "sub-01", "rejected", "Dr B", reason="image quality")
        target = review.status_after_done(study, study.subject("sub-01"), "manual", edited=True)
        self.assertEqual(target, "pending")
        review.apply_transition(study, "sub-01", target, "tracer", extra={"via": "done"})
        rows, excluded = study.analysis_rows()
        self.assertNotIn("sub-01", [row["participant_id"] for row in rows])
        self.assertIn("sub-01", excluded[EXCLUDED_INCLUDE])
        self.assertNotIn("sub-01", [row["participant_id"] for row in review.gate_rows(study, rows)[0]])

    def test_auto_forced_pending_and_done_rules(self):
        study = self.study
        self.assertFalse(study.review_required)
        row = study.subject("sub-01")
        self.assertIsNone(review.status_after_done(study, row, "manual"))
        self.assertIsNone(review.status_after_done(study, row, "manual+smartfill"))  # assisted is not automatic
        self.assertEqual(review.status_after_done(study, row, "manual+auto:nnunet"), "pending")
        study.review_required = True
        self.assertEqual(review.status_after_done(study, row, "manual"), "pending")
        review.apply_transition(study, "sub-01", "pending", "tracer")
        self.assertIsNone(review.status_after_done(study, study.subject("sub-01")))
        review.apply_transition(study, "sub-01", "approved", "Dr B")
        self.assertIsNone(review.status_after_done(study, study.subject("sub-01"), edited=False))
        self.assertEqual(review.status_after_done(study, study.subject("sub-01"), edited=True), "pending")
        study.review_required = False
        # Under review once: changes requested goes back to pending even when the study does not require review
        review.apply_transition(study, "sub-01", "pending", "tracer")
        review.apply_transition(study, "sub-01", "changes", "Dr B", comment="more")
        self.assertEqual(review.status_after_done(study, study.subject("sub-01")), "pending")

    def test_tokens_and_queues(self):
        self.assertEqual(review.trace_tokens("manual+smartfill"), ["manual", "smartfill"])
        self.assertEqual(review.trace_tokens("n/a"), [])
        self.assertEqual(review.add_tokens("n/a", "smartfill"), "manual+smartfill")
        self.assertEqual(review.add_tokens("figure+smartfill", "smartfill", "review-edit"), "figure+smartfill+review-edit")
        self.assertEqual(review.auto_tokens({"trace_method": "auto:nnunet+review-edit"}), ["auto:nnunet"])
        self.assertTrue(review.is_auto_unreviewed({"trace_method": "auto:x", "review_status": "pending"}))
        self.assertFalse(review.is_auto_unreviewed({"trace_method": "auto:x", "review_status": "approved"}))
        self.assertTrue(review.needs_review({"review_status": "pending", "include": "1"}))
        self.assertFalse(review.needs_review({"review_status": "pending", "include": "0"}))
        self.assertTrue(review.needs_review({"review_status": "pending", "include": "0", "reviewer": "Dr B"}))  # a reopened rejection
        self.assertTrue(review.changes_requested({"review_status": "changes"}))
        self.assertEqual(review.status_of({"review_status": "n/a"}), "none")


class AssistTest(unittest.TestCase):
    def test_assisted_summary(self):
        counts = {"smartfill": {"voxels": 640, "applies": 3, "params": [{"Mode": "auto"}]}, "auto:nnunet": {"voxels": 100, "applies": 1}, "noop": {"voxels": 0, "applies": 0}}
        summary = review.assisted_summary(counts, total_voxels=1000)
        self.assertEqual(summary["smartfill_voxels_pct"], 64.0)
        self.assertEqual(summary["tools"], {"smartfill": {"voxels": 640, "applies": 3}, "auto:nnunet": {"voxels": 100, "applies": 1}})
        self.assertEqual(summary["auto"], ["auto:nnunet"])
        self.assertEqual(review.method_after_assist("manual", summary), "manual+smartfill+auto:nnunet")
        self.assertEqual(review.assisted_summary(counts, total_voxels=500)["smartfill_voxels_pct"], 100.0)  # capped (erased voxels)
        self.assertIsNone(review.assisted_summary(counts)["smartfill_voxels_pct"])
        self.assertEqual(review.assisted_summary({}, 10), {"smartfill_voxels_pct": None, "tools": {}, "auto": []})
        self.assertEqual(review.assisted_summary(summary, 1000)["smartfill_voxels_pct"], 64.0)  # a stored summary
        self.assertEqual(review.method_after_assist("manual", review.assisted_summary({}, 10)), "manual")


class BlindingAndGateTest(ReviewTestBase):
    def test_blinded_columns(self):
        study = self.study
        study.set("sub-01", {"moca": 21, "symptom": "aphasia", "age": 60})
        study.set_column_role("moca", "score")
        self.assertEqual(review.blinded_columns(study), [])
        record = review.set_study_setting(study, "blind_tracing", True, "PI")
        self.assertEqual((record["from"], record["to"], record["who"]), (False, True, "PI"))
        hidden = review.blinded_columns(study)
        self.assertEqual(set(hidden), {"group", "moca", "symptom"})
        self.assertEqual(hidden[0], "group")
        self.assertNotIn("age", hidden)  # a covariate is not a score
        row = review.blinded_row(study, study.subject("sub-01"))
        self.assertNotIn("group", row)
        self.assertNotIn("moca", row)
        self.assertEqual(row["participant_id"], "sub-01")
        self.assertIsNone(review.set_study_setting(study, "blind_tracing", True, "PI"))  # unchanged: no record
        review.apply_transition(study, "sub-01", "pending", "tracer")
        self.assertTrue(self.audit(review.AUDIT_EVENT)[-1]["blind"])
        self.assertTrue(review.provenance(study)["blind_tracing"])
        self.assertEqual(review.provenance(study)["setting_changes"], 1)
        self.assertIn("blind", review.gate_text(review.gate_summary(study)))
        with self.assertRaises(ValueError):
            review.set_study_setting(study, "include", True, "PI")

    def test_gate(self):
        study = self.study
        study.set("sub-02", trace_method="auto:nnunet")
        review.apply_transition(study, "sub-01", "pending", "tracer")
        review.apply_transition(study, "sub-01", "approved", "Dr B")
        review.apply_transition(study, "sub-02", "pending", "tracer")
        # Not required: the automatic lesion that no human approved is gated all the same
        rows = study.subjects
        kept, gated = review.gate_rows(study, rows)
        self.assertEqual(gated, {review.EXCLUDED_AUTO: ["sub-02"]})
        self.assertEqual(len(review.gate_rows(study, rows, include_unreviewed=True)[0]), 4)
        summary = review.gate_summary(study)
        self.assertEqual(summary["auto_unreviewed"], ["sub-02"])
        self.assertIn("1 automatic segmentation excluded", review.gate_text(summary))
        # Required: only approved lesions
        record = review.set_study_setting(study, "review_required", True, "PI")
        self.assertEqual(record["to"], True)
        rows, excluded = study.analysis_rows()
        self.assertEqual([row["participant_id"] for row in rows], ["sub-01"])
        self.assertEqual(sorted(excluded[EXCLUDED_REVIEW]), ["sub-02", "sub-03", "sub-04"])
        kept, gated = review.gate_rows(study, study.subjects)
        self.assertEqual([row["participant_id"] for row in kept], ["sub-01"])
        summary = review.gate_summary(study)
        self.assertEqual(summary["excluded"], {"pending": ["sub-02"], "none": ["sub-03", "sub-04"]})
        text = review.gate_text(summary)
        self.assertIn("3 lesions excluded as not approved", text)
        self.assertIn("1 of 4", text)
        anyway = review.gate_summary(study, include_unreviewed=True)
        self.assertEqual(anyway["included_anyway"], ["sub-02", "sub-03", "sub-04"])
        self.assertIn("included anyway", review.gate_text(anyway))
        override = review.record_override(study, ["sub-03", "sub-02"], "PI", context={"run": "cohortmaps"})
        self.assertEqual(override["sids"], ["sub-02", "sub-03"])
        self.assertEqual(self.audit(review.OVERRIDE_EVENT)[-1]["context"], {"run": "cohortmaps"})
        study.save()

    def test_cohortmaps_study_gate(self):
        """The cohort maps use approved lesions only when review is required; the override keeps them (recorded by the CLI)."""
        study = self.study
        affine = np.diag([2.0, 2.0, 2.0, 1.0])
        for index, sid in enumerate(study.subject_ids):
            data = np.zeros((4, 4, 4), np.uint8)
            data[index % 4, 1, 1] = 1
            path = os.path.join(study.path, "lesions", f"{sid}_space-MNI152NLin6Asym_desc-lesion_mask.nii.gz")
            nifti.write_nifti(path, data, affine)
            study.set(sid, lesion=study.relpath(path))
        study.review_required = True
        review.apply_transition(study, "sub-01", "pending", "tracer")
        review.apply_transition(study, "sub-01", "approved", "Dr B")
        review.apply_transition(study, "sub-02", "pending", "tracer")
        study.save()
        lesions, excluded = cohortmaps.study_lesions(study)
        self.assertEqual([sid for sid, _path, _sha in lesions], ["sub-01"])
        self.assertEqual(sorted(excluded[EXCLUDED_REVIEW]), ["sub-02", "sub-03", "sub-04"])
        lesions, excluded = cohortmaps.study_lesions(study, include_unreviewed=True)
        self.assertEqual(len(lesions), 4)
        with open(os.path.join(study.path, "study.json"), encoding="utf-8") as f:
            self.assertTrue(json.load(f)["review_required"])


class AutoGateTest(ReviewTestBase):
    def test_analyses_leave_out_unapproved_auto_lesions(self):
        """Acceptance "Auto masks cannot reach analyses unapproved", with review not required.

        ``Study.analysis_rows`` applies the rule of ``review.gate_rows`` itself, so every analysis that reads the study
        (cohort maps, lesion sets, the design, the pipeline) leaves such lesions out under the same reason.
        """
        self.assertEqual(review.EXCLUDED_AUTO, EXCLUDED_AUTO)
        study = self.study
        affine = np.diag([2.0, 2.0, 2.0, 1.0])
        for index, sid in enumerate(study.subject_ids):
            data = np.zeros((4, 4, 4), np.uint8)
            data[index % 4, 1, 1] = 1
            path = os.path.join(study.path, "lesions", f"{sid}_space-MNI152NLin6Asym_desc-lesion_mask.nii.gz")
            nifti.write_nifti(path, data, affine)
            study.set(sid, lesion=study.relpath(path))
        study.set("sub-02", trace_method="manual+auto:nnunet")
        review.apply_transition(study, "sub-02", "pending", "tracer")
        self.assertFalse(study.review_required)
        rows, excluded = study.analysis_rows()
        self.assertNotIn("sub-02", [row["participant_id"] for row in rows])
        self.assertIn("sub-02", excluded.get(review.EXCLUDED_AUTO, []))
        lesions, excluded = cohortmaps.study_lesions(study)
        self.assertNotIn("sub-02", [sid for sid, _path, _sha in lesions])
        self.assertIn("sub-02", excluded.get(review.EXCLUDED_AUTO, []))
        # The recorded override keeps it
        lesions, _excluded = cohortmaps.study_lesions(study, include_unreviewed=True)
        self.assertIn("sub-02", [sid for sid, _path, _sha in lesions])
        # Approved by a human: analysed
        review.apply_transition(study, "sub-02", "approved", "Dr B")
        self.assertIn("sub-02", [row["participant_id"] for row in study.analysis_rows()[0]])


class SidecarTest(ReviewTestBase):
    def test_sidecar_values(self):
        study = self.study
        study.blind_tracing = True
        review.apply_transition(study, "sub-01", "pending", "tracer")
        summary = review.assisted_summary({"smartfill": {"voxels": 5, "applies": 1}}, 10)
        values = review.sidecar_values(study, study.subject("sub-01"), summary)
        self.assertEqual(values["review"], {"status": "pending", "reviewer": None})
        self.assertEqual(values["assist"], {"smartfill_voxels_pct": 50.0, "tools": {"smartfill": {"voxels": 5, "applies": 1}}})
        self.assertTrue(values["blind_tracing"])
        json.dumps(values)


if __name__ == "__main__":
    unittest.main()

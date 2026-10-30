"""Tests of blind double tracing, mask-level agreement and adjudication (agreement.py; L-44, addendum 3.10, RW-11).

Metric formulas (Dice, centroid distance, volume ratio, HD95 against a brute-force surface distance on known shapes,
the 2 mm grid), the between-lesion baseline, seeded assignment, the second tracing rules (blinding, the intra-rater
interval), the consensus operations (originals kept, audit, undo), the network_r label and interrater.tsv as the
methods text reads it.

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingAgreementTest.py -v
"""

import datetime
import json
import os
import random
import shutil
import sys
import tempfile
import unittest
import unittest.mock

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np

from LesionNetworkMappingLib import agreement, geometry, methods, nifti, review, schema
from LesionNetworkMappingLib.study import Study

SHAPE = (40, 40, 40)
#: A 1 mm grid inside the MNI box (RAS), so that the 2 mm connectome grid of MNI152NLin6Asym covers it
AFFINE = np.array([[1.0, 0, 0, -20.0], [0, 1.0, 0, -30.0], [0, 0, 1.0, -10.0], [0, 0, 0, 1.0]])


def cube(center, half, shape=SHAPE):
    mask = np.zeros(shape, dtype=np.uint8)
    lo = [c - half for c in center]
    hi = [c + half + 1 for c in center]
    mask[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]] = 1
    return mask


def sphere(center, radius, shape=SHAPE):
    ijk = np.indices(shape).reshape(3, -1).T
    inside = np.linalg.norm(ijk - np.asarray(center), axis=1) <= radius
    return inside.reshape(shape).astype(np.uint8)


def brute_hd95(a, b, spacing):
    """HD95 from every pair of surface voxels (the reference for the distance-transform implementation)."""
    sa = np.argwhere(agreement._surface(a.astype(bool))) * np.asarray(spacing)
    sb = np.argwhere(agreement._surface(b.astype(bool))) * np.asarray(spacing)
    d = np.sqrt(((sa[:, None, :] - sb[None, :, :]) ** 2).sum(axis=2))
    return float(np.percentile(np.concatenate([d.min(axis=1), d.min(axis=0)]), 95))


class MetricTest(unittest.TestCase):
    def test_identical_masks(self):
        a = cube((20, 20, 20), 4)
        m = agreement.pair_metrics(a, a, AFFINE)
        self.assertEqual(m["dice"], 1.0)
        self.assertEqual(m["centroid_mm"], 0.0)
        self.assertEqual(m["volume_ratio"], 1.0)
        self.assertEqual(m["hd95_mm"], 0.0)
        self.assertAlmostEqual(m["volume_a_ml"], 9**3 / 1000.0)
        self.assertIsNone(m["grid2mm"])

    def test_shifted_cubes_formulas(self):
        a = cube((20, 20, 20), 4)  # 9^3 voxels
        b = cube((23, 20, 20), 4)  # shifted 3 voxels along i
        m = agreement.pair_metrics(a, b, AFFINE)
        self.assertAlmostEqual(m["dice"], 2 * (6 * 81) / (2 * 729))
        self.assertAlmostEqual(m["centroid_mm"], 3.0)
        self.assertEqual(m["volume_ratio"], 1.0)
        self.assertAlmostEqual(m["hd95_mm"], brute_hd95(a, b, (1, 1, 1)), places=6)
        self.assertLessEqual(m["hd95_mm"], 3.0)

    def test_volume_ratio_is_smaller_over_larger(self):
        a = cube((20, 20, 20), 4)
        b = cube((20, 20, 20), 2)
        m = agreement.pair_metrics(a, b, AFFINE)
        self.assertAlmostEqual(m["volume_ratio"], 125 / 729)
        self.assertAlmostEqual(agreement.pair_metrics(b, a, AFFINE)["volume_ratio"], 125 / 729)
        self.assertAlmostEqual(m["dice"], 2 * 125 / (125 + 729))

    def test_hd95_single_voxels_and_anisotropic_spacing(self):
        a = np.zeros(SHAPE, dtype=np.uint8)
        b = np.zeros(SHAPE, dtype=np.uint8)
        a[10, 10, 10] = 1
        b[13, 14, 10] = 1
        self.assertAlmostEqual(agreement.hd95(a, b, (1, 1, 1)), 5.0)
        self.assertAlmostEqual(agreement.hd95(a, b, (2.0, 0.5, 3.0)), float(np.hypot(6.0, 2.0)))
        affine = np.diag([2.0, 0.5, 3.0, 1.0])
        m = agreement.pair_metrics(a, b, affine)
        self.assertAlmostEqual(m["hd95_mm"], float(np.hypot(6.0, 2.0)))
        self.assertAlmostEqual(m["centroid_mm"], float(np.hypot(6.0, 2.0)))
        self.assertEqual(m["dice"], 0.0)

    def test_hd95_matches_brute_force_on_known_shapes(self):
        rng = np.random.default_rng(3)
        for spacing in ((1.0, 1.0, 1.0), (0.8, 1.2, 2.5)):
            for _trial in range(3):
                a = sphere(rng.integers(14, 26, 3), rng.uniform(3, 7))
                b = cube(tuple(rng.integers(14, 26, 3)), int(rng.integers(2, 6)))
                self.assertAlmostEqual(agreement.hd95(a, b, spacing), brute_hd95(a, b, spacing), places=6)

    def test_hd95_concentric_spheres(self):
        a = sphere((20, 20, 20), 4)
        b = sphere((20, 20, 20), 8)
        # Every surface distance is close to the 4 mm between the radii (voxelized: within one voxel)
        self.assertAlmostEqual(agreement.hd95(a, b, (1, 1, 1)), 4.0, delta=1.0)

    def test_empty_masks(self):
        a = cube((20, 20, 20), 2)
        empty = np.zeros(SHAPE, dtype=np.uint8)
        m = agreement.pair_metrics(a, empty, AFFINE)
        self.assertEqual(m["dice"], 0.0)
        self.assertIsNone(m["centroid_mm"])
        self.assertIsNone(m["hd95_mm"])
        self.assertEqual(m["volume_ratio"], 0.0)
        self.assertIsNone(agreement.pair_metrics(empty, empty, AFFINE)["dice"])

    def test_fractional_values_threshold_at_half(self):
        a = cube((20, 20, 20), 2).astype(np.float32)
        b = a * 0.4
        b[20, 20, 20] = 0.6
        m = agreement.pair_metrics(a, b, AFFINE)
        self.assertEqual(m["voxels_b"], 1)

    def test_2mm_grid(self):
        a = cube((20, 20, 20), 4)
        b = cube((23, 20, 20), 4)
        m = agreement.pair_metrics(a, b, AFFINE, grid2mm="NLin6-2mm")
        coarse = m["grid2mm"]
        self.assertIsNotNone(coarse)
        self.assertAlmostEqual(coarse["volume_a_ml"], 0.729, delta=0.25)
        self.assertGreater(coarse["dice"], 0.4)
        self.assertAlmostEqual(coarse["centroid_mm"], 3.0, delta=2.0)
        self.assertIsNotNone(coarse["hd95_mm"])
        # A lesion smaller than one 2 mm voxel keeps its fullest voxel
        tiny = np.zeros(SHAPE, dtype=np.uint8)
        tiny[5, 5, 5] = 1
        self.assertEqual(agreement.pair_metrics(tiny, tiny, AFFINE, grid2mm="NLin6-2mm")["grid2mm"]["dice"], 1.0)

    def test_second_mask_on_another_grid_is_resampled(self):
        a = cube((20, 20, 20), 4)
        # b is the same lesion stored with a flipped i axis (radiological storage)
        flipped = AFFINE.copy()
        flipped[0, 0] = -1.0
        flipped[0, 3] = AFFINE[0, 3] + (SHAPE[0] - 1)
        b = a[::-1, :, :].copy()
        m = agreement.pair_metrics(a, b, AFFINE, affine_b=flipped)
        self.assertAlmostEqual(m["dice"], 1.0)
        self.assertAlmostEqual(m["hd95_mm"], 0.0)

    def test_connectome_grid_of_spaces(self):
        self.assertEqual(agreement.connectome_grid("MNI152NLin6Asym"), "NLin6-2mm")
        self.assertEqual(agreement.connectome_grid("MNI152NLin2009cAsym"), "TF2009-2mm")
        self.assertIsNone(agreement.connectome_grid("native"))

    def test_network_r_label(self):
        rng = np.random.default_rng(0)
        x = rng.normal(size=(10, 10, 10))
        result = agreement.network_r(x, x * 2 + 1)
        self.assertAlmostEqual(result["r"], 1.0)
        self.assertEqual(result["label"], "network similarity overstates tracing agreement (van den Heuvel 2026)")
        self.assertEqual(result["n_voxels"], 1000)
        self.assertIsNone(agreement.network_r(np.zeros(5), np.zeros(5))["r"])

    def test_consensus_operations(self):
        a = cube((20, 20, 20), 2)
        b = cube((21, 20, 20), 2)
        self.assertEqual(int(agreement.consensus_mask(a, b, "A").sum()), 125)
        self.assertEqual(int(agreement.consensus_mask(a, b, "B").sum()), 125)
        self.assertEqual(int(agreement.consensus_mask(a, b, "union").sum()), 150)
        self.assertEqual(int(agreement.consensus_mask(a, b, "intersection").sum()), 100)
        with self.assertRaises(ValueError):
            agreement.consensus_mask(a, b, "edit")

    def test_rater_labels(self):
        self.assertEqual(agreement.rater_label("AB"), "AB")
        self.assertEqual(agreement.rater_label("Ana Bell"), "AB")
        self.assertEqual(agreement.rater_label("dr. j. smith"), "DJS")
        with self.assertRaises(ValueError):
            agreement.rater_label("  ")


class StudyTestBase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="lnm-agreement-")
        self.study = Study.create(os.path.join(self.folder, "study"), space="MNI152NLin6Asym")
        self.centers = {"sub-01": (12, 20, 20), "sub-02": (28, 20, 20), "sub-03": (20, 12, 20), "sub-04": (20, 28, 20), "sub-05": (20, 20, 12),
                        "sub-06": (20, 20, 28)}
        old = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=10)).isoformat(timespec="seconds")
        for index, (sid, center) in enumerate(self.centers.items()):
            relative = f"lesions/{sid}_space-MNI152NLin6Asym_desc-lesion_mask.nii.gz"
            path = os.path.join(self.study.path, relative)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            nifti.write_nifti(path, cube(center, 3), AFFINE, dtype=np.uint8)
            self.study.add_subject(sid, group="case" if index % 2 == 0 else "control", status="traced", lesion=relative, space="MNI152NLin6Asym",
                                   tracer="Ana Bell" if index < 4 else "Carl Diaz", modified=old)
        self.study.save()
        self.scan = os.path.join(self.study.path, "template.nii.gz")
        nifti.write_nifti(self.scan, np.zeros(SHAPE, dtype=np.float32), AFFINE)

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def second(self, sid, rater, mask, final=True):
        kji = np.ascontiguousarray(np.transpose(mask, (2, 1, 0)))
        return agreement.save_second_tracing(self.study, sid, kji, AFFINE, self.scan, rater, final=final)


class BaselineTest(StudyTestBase):
    def test_baseline_is_the_median_distance_between_subjects(self):
        base = agreement.between_lesion_baseline(self.study)
        centers = [np.asarray(c, float) + AFFINE[:3, 3] for c in self.centers.values()]
        expected = np.median([np.linalg.norm(a - b) for i, a in enumerate(centers) for b in centers[i + 1:]])
        self.assertEqual(base["n_lesions"], 6)
        self.assertEqual(base["n_pairs"], 15)
        self.assertAlmostEqual(base["median_mm"], float(expected))
        self.assertIn("Cotovio", base["note"])

    def test_baseline_uses_stored_centroids_and_skips_excluded(self):
        self.study.set("sub-01", centroid_x=0, centroid_y=0, centroid_z=0)
        self.study.set("sub-02", centroid_x=30, centroid_y=40, centroid_z=0)
        self.study.set("sub-03", include=0)
        base = agreement.between_lesion_baseline(self.study, sids=["sub-01", "sub-02", "sub-03"])
        self.assertEqual(base["n_lesions"], 2)
        self.assertAlmostEqual(base["median_mm"], 50.0)


class AssignTest(StudyTestBase):
    def test_seeded_assignment_is_reproducible_and_recorded(self):
        record = agreement.assign(self.study, fraction=0.5, seed=42, who="PI")
        other = Study.open(self.study.path)
        for row in other.subjects:
            other.set(row["participant_id"], rater2_status=None)
        again = agreement.assign(other, fraction=0.5, seed=42, who="PI")
        self.assertEqual(record["sids"], again["sids"])
        self.assertEqual(len(record["sids"]), 3)
        self.assertEqual(record["seed"], 42)
        self.assertEqual(self.study.metadata["agreement"]["assignments"][-1]["seed"], 42)
        for sid in record["sids"]:
            self.assertEqual(self.study.subject(sid)["rater2_status"], "assigned")
        self.study.save()
        reopened = Study.open(self.study.path)
        self.assertEqual(reopened.metadata["agreement"]["assignments"][0]["sids"], record["sids"])
        self.assertTrue(any(r.get("event") == "second_tracing_assign" and r.get("seed") == 42 for r in review.read_audit(self.study)))
        self.assertEqual(schema.registered("rater2_status").role, "bookkeeping")
        # Already assigned subjects are not eligible again
        self.assertTrue(set(record["sids"]).isdisjoint(agreement.eligible(self.study)))

    def test_new_seed_when_none_and_selected_subjects(self):
        record = agreement.assign(self.study, fraction=0.2)
        self.assertIsInstance(record["seed"], int)
        self.assertEqual(len(record["sids"]), 2)  # ceil(0.2 * 6)
        # Carl Diaz traced sub-05 and sub-06, so sub-01..sub-04 are eligible for him; the unseeded draw above takes at most two of them
        wanted = ["sub-01", "sub-02", "sub-03", "sub-04", "sub-99"]
        chosen = agreement.assign(self.study, sids=wanted, rater="Carl Diaz")
        self.assertIsNone(chosen["seed"])
        self.assertTrue(chosen["sids"])
        self.assertEqual(chosen["skipped"], [sid for sid in wanted if sid not in chosen["sids"]])
        self.assertIn("sub-99", chosen["skipped"])

    def test_selected_subjects_do_not_depend_on_the_unseeded_draw(self):
        # The unseeded draw of 2 of the 6 subjects is random: the follow-up assignment must work for every possible draw
        pool = sorted(self.centers)
        draws = {}
        for seed in range(2000):
            draws.setdefault(frozenset(random.Random(seed).sample(pool, 2)), seed)
            if len(draws) == 15:
                break
        self.assertEqual(len(draws), 15)  # all pairs of the six subjects
        for pair, seed in draws.items():
            with unittest.mock.patch.object(agreement.secrets, "randbelow", return_value=seed):
                record = agreement.assign(self.study, fraction=0.2)
            self.assertEqual((record["seed"], set(record["sids"])), (seed, set(pair)))
            wanted = ["sub-01", "sub-02", "sub-03", "sub-04", "sub-99"]
            chosen = agreement.assign(self.study, sids=wanted, rater="Carl Diaz")
            self.assertEqual(chosen["sids"], [sid for sid in wanted[:4] if sid not in pair])
            self.assertEqual(chosen["skipped"], [sid for sid in wanted if sid not in chosen["sids"]])
            agreement.unassign(self.study, [*record["sids"], *chosen["sids"]])

    def test_inter_mode_leaves_out_the_raters_own_lesions(self):
        pool = agreement.eligible(self.study, rater="Ana Bell", mode="inter")
        self.assertEqual(pool, ["sub-05", "sub-06"])
        self.assertEqual(agreement.eligible(self.study, rater="AB", mode="intra"), ["sub-01", "sub-02", "sub-03", "sub-04"])
        with self.assertRaises(ValueError):
            agreement.assign(self.study, mode="intra")
        with self.assertRaises(ValueError):
            agreement.assign(self.study, level="peek")

    def test_unassign(self):
        record = agreement.assign(self.study, sids=["sub-01"])
        self.assertEqual(agreement.unassign(self.study, record["sids"]), ["sub-01"])
        self.assertIsNone(agreement.status_of(self.study.subject("sub-01")))


class SecondTracingTest(StudyTestBase):
    def test_blind_second_tracing_saved_in_rater_folder(self):
        agreement.assign(self.study, sids=["sub-01"], rater="Carl Diaz")
        result = self.second("sub-01", "Carl Diaz", cube((13, 20, 20), 3))
        self.assertEqual(result["path"], "lesions/rater-CD/sub-01_space-MNI152NLin6Asym_desc-lesion_mask.nii.gz")
        path = self.study.abspath(result["path"])
        self.assertTrue(os.path.isfile(path))
        with open(path[:-len(".nii.gz")] + ".json", encoding="utf-8") as f:
            sidecar = json.load(f)
        self.assertEqual(sidecar["Type"], "Lesion")
        self.assertFalse(sidecar["SlicerStudio"]["primary_loaded"])
        self.assertEqual(sidecar["Sources"], ["../../template.nii.gz"])
        self.assertNotIn(self.folder, json.dumps(sidecar))
        row = self.study.subject("sub-01")
        self.assertEqual(row["rater2_status"], "done")
        self.assertAlmostEqual(float(row["rater2_dice"]), 2 * 6 * 49 / (2 * 343), places=3)
        self.assertEqual(review.read_audit(self.study, sid="sub-01", event="second_tracing")[0]["blind"], True)
        # The primary lesion is untouched
        data, _affine, _header = nifti.read_nifti(self.study.abspath(row["lesion"]))
        self.assertTrue(np.array_equal(data, cube((12, 20, 20), 3)))

    def test_rules(self):
        with self.assertRaises(agreement.AgreementError):
            self.second("sub-01", "Carl Diaz", cube((13, 20, 20), 3))  # not assigned
        agreement.assign(self.study, sids=["sub-01"])
        with self.assertRaises(agreement.AgreementError):
            self.second("sub-01", "Ana Bell", cube((13, 20, 20), 3))  # the first tracer, inter mode
        with self.assertRaises(agreement.AgreementError):
            self.second("sub-01", "Carl Diaz", np.zeros(SHAPE, dtype=np.uint8))  # empty

    def test_intra_rater_needs_seven_days(self):
        agreement.assign(self.study, sids=["sub-01"], rater="Ana Bell", mode="intra")
        self.study.set("sub-01", modified=(datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=3)).isoformat(timespec="seconds"))
        with self.assertRaisesRegex(agreement.AgreementError, "7 days"):
            self.second("sub-01", "Ana Bell", cube((13, 20, 20), 3))
        later = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=5)
        agreement.check_second_tracing(self.study, "sub-01", "Ana Bell", now=later)
        with self.assertRaises(agreement.AgreementError):
            agreement.check_second_tracing(self.study, "sub-01", "Carl Diaz", now=later)  # intra: the same rater only
        self.study.set("sub-01", modified=None)
        with self.assertRaisesRegex(agreement.AgreementError, "cannot be checked"):
            agreement.check_second_tracing(self.study, "sub-01", "Ana Bell", now=later)
        self.study.set("sub-01", modified=(datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=8)).isoformat(timespec="seconds"))
        self.assertEqual(self.second("sub-01", "Ana Bell", cube((12, 20, 21), 3))["path"], "lesions/rater-AB/sub-01_space-MNI152NLin6Asym_desc-lesion_mask.nii.gz")

    def test_planned_rater_only(self):
        agreement.assign(self.study, sids=["sub-01"], rater="Carl Diaz")
        with self.assertRaisesRegex(agreement.AgreementError, "assigned to Carl Diaz"):
            agreement.check_second_tracing(self.study, "sub-01", "Dana Eve")
        agreement.check_second_tracing(self.study, "sub-01", "CD")

    def test_work_in_progress_stays_in_the_queue(self):
        agreement.assign(self.study, sids=["sub-01", "sub-02"], rater="Carl Diaz")
        result = self.second("sub-01", "Carl Diaz", cube((30, 30, 30), 1), final=False)  # Skip / Exit with a partial tracing
        row = self.study.subject("sub-01")
        self.assertEqual(row["rater2_status"], "inprogress")
        self.assertEqual(row["rater2_dice"], "n/a")
        self.assertEqual(row["rater2_date"], "n/a")
        self.assertIsNone(result["dice"])
        self.assertTrue(agreement.second_tracing_todo(row))
        self.assertFalse(agreement.needs_adjudication(row))
        self.assertTrue(os.path.isfile(self.study.abspath(row["rater2_lesion"])))
        self.second("sub-02", "Carl Diaz", cube((28, 20, 20), 3))
        data = agreement.summary(self.study)
        self.assertEqual([pair["participant_id"] for pair in data["pairs"]], ["sub-02"])
        self.assertEqual(data["n_pairs"], 1)
        self.assertEqual(self.study.subject("sub-01")["rater2_dice"], "n/a")
        with self.assertRaises(agreement.AgreementError):
            agreement.adjudicate(self.study, "sub-01", "A", "Dr Z")
        # Another rater cannot take over a tracing in progress; its rater finishes it
        with self.assertRaises(agreement.AgreementError):
            agreement.check_second_tracing(self.study, "sub-01", "Dana Eve")
        self.second("sub-01", "Carl Diaz", cube((13, 20, 20), 3))
        row = self.study.subject("sub-01")
        self.assertEqual(row["rater2_status"], "done")
        self.assertNotEqual(row["rater2_date"], "n/a")
        self.assertFalse(agreement.second_tracing_todo(row))
        self.assertEqual(agreement.summary(self.study, write=False)["n_pairs"], 2)
        finals = [record["final"] for record in review.read_audit(self.study, sid="sub-01", event="second_tracing")]
        self.assertEqual(finals, [False, True])

    def test_queues(self):
        agreement.assign(self.study, sids=["sub-01", "sub-02"])
        self.assertTrue(agreement.second_tracing_todo(self.study.subject("sub-01")))
        self.second("sub-01", "Carl Diaz", cube((30, 30, 30), 3))  # no overlap
        self.second("sub-02", "Carl Diaz", cube((28, 20, 20), 3))  # identical
        self.assertFalse(agreement.second_tracing_todo(self.study.subject("sub-01")))
        self.assertTrue(agreement.needs_adjudication(self.study.subject("sub-01")))
        self.assertFalse(agreement.needs_adjudication(self.study.subject("sub-02")))
        self.assertTrue(agreement.needs_adjudication(self.study.subject("sub-02"), threshold=1.01))


class UnrecordedTracerTest(StudyTestBase):
    """A study as Studio leaves it before the tracer is recorded: traced rows without a tracer or rater column value."""

    def setUp(self):
        super().setUp()
        for sid in self.centers:
            self.study.set(sid, tracer=None)

    def test_intra_rater_assignment_names_the_tracer(self):
        record = agreement.assign(self.study, sids=["sub-01"], rater="Ana Bell", mode="intra")
        self.assertEqual(record["sids"], ["sub-01"])
        self.assertEqual(record["tracer_named_by_assigner"], ["sub-01"])
        self.assertEqual(agreement.eligible(self.study, rater="Ana Bell", mode="intra"), ["sub-02", "sub-03", "sub-04", "sub-05", "sub-06"])
        # The planned rater (named by the assigner) traces again, after the interval; nobody else
        agreement.check_second_tracing(self.study, "sub-01", "Ana Bell")
        with self.assertRaises(agreement.AgreementError):
            agreement.check_second_tracing(self.study, "sub-01", "Carl Diaz")
        self.study.set("sub-01", rater2=None)
        with self.assertRaisesRegex(agreement.AgreementError, "not recorded"):
            agreement.check_second_tracing(self.study, "sub-01", "Ana Bell")

    def test_tracer_from_the_review_audit(self):
        # review.py records who pressed Done when the lesion went to review (via "done")
        review.apply_transition(self.study, "sub-02", "pending", "Ana Bell", extra={"via": "done"})
        review.apply_transition(self.study, "sub-03", "pending", "unnamed", extra={"via": "done"})
        self.assertEqual(agreement.primary_rater(self.study.subject("sub-02"), self.study), "Ana Bell")
        self.assertIsNone(agreement.primary_rater(self.study.subject("sub-03"), self.study))
        self.assertIsNone(agreement.primary_rater(self.study.subject("sub-02")))
        self.assertNotIn("sub-02", agreement.eligible(self.study, rater="Ana Bell"))
        self.assertEqual(agreement.eligible(self.study, rater="AB", mode="intra"),
                         ["sub-01", "sub-02", "sub-03", "sub-04", "sub-05", "sub-06"])
        self.assertEqual(agreement.eligible(self.study, rater="Carl Diaz", mode="intra"), ["sub-01", "sub-03", "sub-04", "sub-05", "sub-06"])
        agreement.assign(self.study, sids=["sub-02"], rater=None)
        with self.assertRaisesRegex(agreement.AgreementError, "traced sub-02 first"):
            agreement.check_second_tracing(self.study, "sub-02", "Ana Bell")
        agreement.check_second_tracing(self.study, "sub-02", "Carl Diaz")

    def test_tracer_from_the_lesion_sidecar(self):
        self.study.write_lesion_sidecar("sub-04", studio={"tracer": "Ana Bell"})
        self.assertEqual(agreement.primary_rater(self.study.subject("sub-04"), self.study), "Ana Bell")
        agreement.assign(self.study, sids=["sub-04"])
        with self.assertRaises(agreement.AgreementError):
            agreement.check_second_tracing(self.study, "sub-04", "AB")

    def test_recorded_column_wins(self):
        self.study.set("sub-01", tracer="Carl Diaz")
        review.apply_transition(self.study, "sub-01", "pending", "Ana Bell", extra={"via": "done"})
        self.assertEqual(agreement.primary_rater(self.study.subject("sub-01"), self.study), "Carl Diaz")
        self.study.set("sub-01", tracer=None, rater="consensus")
        self.assertEqual(agreement.primary_rater(self.study.subject("sub-01"), self.study), "Ana Bell")


class SummaryTest(StudyTestBase):
    def test_summary_tsv_json_and_methods(self):
        agreement.assign(self.study, sids=["sub-01", "sub-02", "sub-03"], rater="Carl Diaz")
        self.second("sub-01", "Carl Diaz", cube((13, 20, 20), 3))
        self.second("sub-02", "Carl Diaz", cube((28, 20, 20), 3))
        self.second("sub-03", "Carl Diaz", cube((30, 30, 30), 2))
        data = agreement.summary(self.study)
        self.assertEqual(data["n_pairs"], 3)
        self.assertEqual(data["below_threshold"], ["sub-03"])
        self.assertAlmostEqual(data["median"]["dice"], 2 * 6 * 49 / (2 * 343), places=3)
        self.assertIsNotNone(data["median_2mm"]["dice"])
        self.assertGreater(data["baseline"]["median_mm"], data["median"]["centroid_mm"])
        self.assertIn("against a median centroid distance", data["text"])
        tsv = os.path.join(self.study.path, "derivatives", "qc", "interrater.tsv")
        with open(tsv, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn(self.folder, text)
        header = text.splitlines()[0].split("\t")
        self.assertEqual(header, list(agreement.TSV_COLUMNS))
        self.assertEqual(len(text.splitlines()), 4)
        self.assertEqual(agreement.read_summary(self.study)["n_pairs"], 3)
        self.assertNotIn("pairs", agreement.read_summary(self.study))
        # The methods text (L-18) reads interrater.tsv
        self.study.save()
        sections = methods.methods_sections(self.study.path)
        lesions = next(section for section in sections if section.id == "lesions")
        items = [item for kind, *rest in lesions.blocks if kind == "ul" for item in rest[0]]
        self.assertTrue(any(item.startswith("Inter-rater agreement: 3 pair(s); median Dice 0.8") for item in items), items)


class AdjudicateTest(StudyTestBase):
    def setUp(self):
        super().setUp()
        agreement.assign(self.study, sids=["sub-01"], rater="Carl Diaz")
        self.second("sub-01", "Carl Diaz", cube((14, 20, 20), 3))
        self.primary = self.study.abspath(self.study.subject("sub-01")["lesion"])
        with open(self.primary, "rb") as f:
            self.original = f.read()

    def read(self, path):
        return nifti.read_nifti(path)[0]

    def test_union_consensus_keeps_originals_and_audits(self):
        record = agreement.adjudicate(self.study, "sub-01", "union", "Dr Z")
        row = self.study.subject("sub-01")
        self.assertEqual(row["rater"], "consensus")
        self.assertEqual(row["rater2_status"], "adjudicated")
        self.assertEqual(row["adjudication"], "union")
        self.assertEqual(row["rater1_lesion"], "lesions/sub-01_space-MNI152NLin6Asym_desc-preadjudication_mask.nii.gz")
        expected = cube((12, 20, 20), 3) | cube((14, 20, 20), 3)
        self.assertTrue(np.array_equal(self.read(self.primary) != 0, expected.astype(bool)))
        with open(self.study.abspath(row["rater1_lesion"]), "rb") as f:
            self.assertEqual(f.read(), self.original)
        self.assertTrue(os.path.isfile(self.study.abspath(row["rater2_lesion"])))
        self.assertAlmostEqual(float(row["volume_ml"]), int(expected.sum()) / 1000.0)
        self.assertEqual(record["raters"], ["AB", "CD"])
        self.assertEqual(record["kept"][0], row["rater1_lesion"])
        sidecar = self.study.read_lesion_sidecar("sub-01")
        self.assertEqual(sidecar["SlicerStudio"]["rater"], "consensus")
        self.assertEqual(sidecar["SlicerStudio"]["adjudication"]["choice"], "union")
        # The header of the consensus is the first tracing's
        self.assertTrue(np.allclose(nifti.read_nifti_header(self.primary)["affine"], AFFINE))
        # Agreement is still the first vs the second tracing
        self.assertEqual(agreement.summary(self.study, write=False)["pairs"][0]["rater_a"], "AB")
        with self.assertRaises(agreement.AgreementError):
            agreement.adjudicate(self.study, "sub-01", "A", "Dr Z")

    def test_choices_and_undo(self):
        for choice, expected in (("A", cube((12, 20, 20), 3)), ("B", cube((14, 20, 20), 3)), ("intersection", cube((12, 20, 20), 3) & cube((14, 20, 20), 3))):
            agreement.adjudicate(self.study, "sub-01", choice, "Dr Z")
            self.assertTrue(np.array_equal(self.read(self.primary) != 0, expected.astype(bool)), choice)
            agreement.undo_adjudication(self.study, "sub-01", "Dr Z")
            row = self.study.subject("sub-01")
            self.assertEqual(row["rater2_status"], "done")
            self.assertEqual(row["rater"], "n/a")
            with open(self.primary, "rb") as f:
                self.assertEqual(f.read(), self.original)
        events = [record["event"] for record in agreement.history(self.study, "sub-01")]
        self.assertEqual(events.count("adjudicate"), 3)
        self.assertEqual(events.count("adjudicate_undo"), 3)

    def test_adjudicate_again_after_undo_and_edit_keeps_the_edited_tracing(self):
        agreement.adjudicate(self.study, "sub-01", "union", "Dr Z")
        agreement.undo_adjudication(self.study, "sub-01", "Dr Z")
        kept = self.primary.replace("desc-lesion_mask", "desc-preadjudication_mask")
        self.assertFalse(os.path.exists(kept))  # restored: the lesion is the first tracing again
        self.assertEqual(self.study.subject("sub-01")["rater1_lesion"], "n/a")
        edited = cube((16, 22, 22), 4)  # the tracer redraws the lesion
        nifti.write_nifti(self.primary, edited, AFFINE, dtype=np.uint8)
        agreement.adjudicate(self.study, "sub-01", "B", "Dr Z")
        row = self.study.subject("sub-01")
        self.assertTrue(np.array_equal(self.read(self.study.abspath(row["rater1_lesion"])) != 0, edited.astype(bool)))
        agreement.undo_adjudication(self.study, "sub-01", "Dr Z")
        self.assertTrue(np.array_equal(self.read(self.primary) != 0, edited.astype(bool)))

    def test_undo_restores_a_reopened_approval(self):
        review.apply_transition(self.study, "sub-01", "pending", "Ana Bell")
        review.apply_transition(self.study, "sub-01", "approved", "Dr R")
        agreement.adjudicate(self.study, "sub-01", "union", "Dr Z")
        record = agreement.undo_adjudication(self.study, "sub-01", "Dr Z")
        self.assertTrue(record["review_restored"])
        row = self.study.subject("sub-01")
        self.assertEqual(review.status_of(row), "approved")
        self.assertEqual(row["reviewer"], "Dr R")
        last = review.read_audit(self.study, sid="sub-01", event="review")[-1]
        self.assertEqual((last["from"], last["to"], last["via"]), ("pending", "approved", "adjudicate_undo"))
        # A review decided after the adjudication is not overridden by an undo
        agreement.adjudicate(self.study, "sub-01", "union", "Dr Z")
        review.apply_transition(self.study, "sub-01", "changes", "Dr R", comment="Too large")
        self.assertNotIn("review_restored", agreement.undo_adjudication(self.study, "sub-01", "Dr Z"))
        self.assertEqual(review.status_of(self.study.subject("sub-01")), "changes")

    def test_edit_choice_writes_the_given_mask(self):
        edited = cube((13, 20, 20), 2)
        kji = np.ascontiguousarray(np.transpose(edited, (2, 1, 0)))
        agreement.adjudicate(self.study, "sub-01", "edit", "Dr Z", array_kji=kji, ijk_to_ras=AFFINE)
        self.assertTrue(np.array_equal(self.read(self.primary) != 0, edited.astype(bool)))
        with self.assertRaises(agreement.AgreementError):
            agreement.undo_adjudication(self.study, "sub-02", "Dr Z")

    def test_approved_review_is_reopened_by_a_changed_consensus(self):
        review.apply_transition(self.study, "sub-01", "pending", "Ana Bell")
        review.apply_transition(self.study, "sub-01", "approved", "Dr R")
        record = agreement.adjudicate(self.study, "sub-01", "union", "Dr Z")
        self.assertTrue(record["review_reopened"])
        self.assertEqual(review.status_of(self.study.subject("sub-01")), "pending")

    def test_choice_a_keeps_an_approval(self):
        review.apply_transition(self.study, "sub-01", "pending", "Ana Bell")
        review.apply_transition(self.study, "sub-01", "approved", "Dr R")
        record = agreement.adjudicate(self.study, "sub-01", "A", "Dr Z")
        self.assertNotIn("review_reopened", record)
        self.assertEqual(review.status_of(self.study.subject("sub-01")), "approved")

    def test_save_and_reopen(self):
        agreement.adjudicate(self.study, "sub-01", "union", "Dr Z")
        self.study.save()
        reopened = Study.open(self.study.path)
        self.assertEqual(reopened.subject("sub-01")["adjudication"], "union")
        dictionary = reopened.dictionary
        self.assertIn("rater2_status", dictionary)


class GridTest(unittest.TestCase):
    def test_registry_grid_ids_exist(self):
        for space in ("MNI152NLin6Asym", "MNI152NLin2009cAsym"):
            self.assertIn(agreement.connectome_grid(space), geometry.GRID_BY_ID)


if __name__ == "__main__":
    unittest.main()

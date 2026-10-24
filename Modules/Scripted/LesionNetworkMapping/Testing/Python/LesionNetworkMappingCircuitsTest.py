"""Tests of circuits.py: re-seeding a binary result (parent links), damage-score formulas, the provenance guard that
refuses same-cohort associations (double dipping), target percentiles against random gray-matter spheres (a planted
high-value sphere scores a high percentile; every row is research-only and only the given coordinates appear), the
same-family rule for the gray-matter mask, and the command line.

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingCircuitsTest.py -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np

from LesionNetworkMappingLib import circuits, connectome, engine, nifti, runs, scores
from LesionNetworkMappingLib.export import file_sha256
from LesionNetworkMappingLib.study import Study, read_tsv

SHAPE = (12, 12, 12)
AFFINE = np.diag([2.0, 2.0, 2.0, 1.0])
AFFINE[:3, 3] = -11.0
SPACE = "MNI152NLin2009cAsym"


def _quiet(*args, **kwargs):
    return None


def _absolute_strings(value):
    """The strings of a JSON value that are absolute local paths (shared records hold none, SPEC 9.7 rule 10)."""
    if isinstance(value, dict):
        return [item for child in value.values() for item in _absolute_strings(child)]
    if isinstance(value, list):
        return [item for child in value for item in _absolute_strings(child)]
    return [value] if isinstance(value, str) and os.path.isabs(value) else []


def _env():
    return dict(os.environ, PYTHONPATH=MODULE_DIR + os.pathsep + os.environ.get("PYTHONPATH", ""))


def _run_cli(*args):
    result = subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.circuits", *args], check=False, capture_output=True, text=True, env=_env(), timeout=300)
    return result.returncode, [json.loads(line) for line in result.stdout.splitlines() if line.strip().startswith("{")], result.stderr


class CohortFixture:
    """A cohort laid out as cohortmaps writes it: mask.nii.gz, one cache-like folder per lesion (T map + weights.npz)
    and maps.tsv (participant_id, lesion_sha256, T, volume_ml), without running the engine.
    """

    def __init__(self, folder, n=8, seed=0, study=False):
        rng = np.random.default_rng(seed)
        self.folder = folder
        mask = np.zeros(SHAPE, dtype=np.uint8)
        mask[1:-1, 1:-1, 1:-1] = 1
        self.mask_path = os.path.join(folder, "mask.nii.gz")
        nifti.write_nifti(self.mask_path, mask, AFFINE)
        self.indices = np.flatnonzero(mask.reshape(-1))
        V = self.indices.size
        self.circuit = rng.standard_normal(V)
        self.circuit_path = self.write_circuit("circuit-test_T.nii.gz", self.circuit)
        self.study_path = None
        if study:
            self.study_path = os.path.join(folder, "study")
            created = Study.create(self.study_path, name="study")
            for index in range(n):
                created.add_subject(f"sub-{index + 1:02d}", {"group": "case" if index % 2 == 0 else "control", "score": float(index)})
            created.save()
            self.maps_dir = os.path.join(self.study_path, "derivatives", "lnm", "cohort")
        else:
            self.maps_dir = os.path.join(folder, "cohort")
        os.makedirs(self.maps_dir, exist_ok=True)
        self.occupancy = []
        self.volume = []
        lines = ["participant_id\tlesion_sha256\tT\tvolume_ml"]
        for index in range(n):
            size = 5 + index * 3
            chosen = np.sort(rng.choice(V, size=size, replace=False))
            values = rng.uniform(0.2, 1.0, size=size).astype(np.float32)
            entry = os.path.join(self.maps_dir, "cache", f"entry{index}")
            os.makedirs(entry)
            np.savez(os.path.join(entry, "weights.npz"), indices=chosen.astype(np.int32), values=values, n_voxels=np.int64(V))
            t_path = os.path.join(entry, "map_desc-T_funcmap.nii.gz")
            nifti.write_nifti(t_path, np.zeros(SHAPE, dtype=np.float32), AFFINE)
            volume = round(float(values.sum()) * 8.0 / 1000.0, 4)
            self.occupancy.append((chosen, values.astype(np.float64)))
            self.volume.append(volume)
            lines.append(f"sub-{index + 1:02d}\t{index:064x}\tcache/entry{index}/map_desc-T_funcmap.nii.gz\t{volume}")
        self.maps_tsv = os.path.join(self.maps_dir, "maps.tsv")
        with open(self.maps_tsv, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.sha = [f"{index:064x}" for index in range(n)]

    def write_circuit(self, name, vector, folder=None):
        volume = np.zeros(int(np.prod(SHAPE)), dtype=np.float32)
        volume[self.indices] = vector
        path = os.path.join(folder or self.folder, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        nifti.write_nifti(path, volume.reshape(SHAPE), AFFINE)
        return path

    def write_circuit_record(self, folder, sha256s, participants=(), name="A"):
        """A circuit.json (as seed_this writes it) whose source lesions are ``sha256s``."""
        record = {"format": circuits.FORMAT_CIRCUIT, "version": 1, "name": name, "run_id": None, "parents": [],
                  "source": {"lesion_sha256": list(sha256s), "participants": list(participants), "maps_tsv": None, "study": None, "known": True}}
        with open(os.path.join(folder, circuits.CIRCUIT_JSON), "w", encoding="utf-8") as f:
            json.dump(record, f)


class DamageScoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-circuits-")
        self.fixture = CohortFixture(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_formulas(self):
        """Sum, mean and % in the circuit equal the hand formulas; the residualised scores are OLS residuals on volume."""
        f = self.fixture
        result = circuits.damage_scores(f.maps_tsv, f.circuit_path, threshold=0.5, mask=f.mask_path, emit=_quiet)
        self.assertEqual(len(result["rows"]), len(f.occupancy))
        circuit = nifti.read_nifti(f.circuit_path)[0].reshape(-1)[f.indices].astype(np.float64)
        sums = []
        for row, (indices, values) in zip(result["rows"], f.occupancy, strict=True):
            inside = circuit[indices]
            expected_sum = float(values @ inside)
            self.assertAlmostEqual(row["sum"], expected_sum, places=6)
            self.assertAlmostEqual(row["mean"], expected_sum / float(values.sum()), places=6)
            self.assertAlmostEqual(row["pct"], 100.0 * float(values[np.abs(inside) >= 0.5].sum()) / float(values.sum()), places=6)
            self.assertAlmostEqual(row["sum"], scores.damage_score((indices, values), circuit), places=9)
            sums.append(expected_sum)
        volume = np.array(f.volume)
        design = np.column_stack([np.ones(len(volume)), volume])
        beta = np.linalg.lstsq(design, np.array(sums), rcond=None)[0]
        expected = np.array(sums) - design @ beta
        np.testing.assert_allclose([row["sum_resid"] for row in result["rows"]], expected, atol=1e-9)
        self.assertAlmostEqual(float(np.sum(expected * volume)), 0.0, places=6)  # orthogonal to volume

    def test_signed_threshold_and_binary_default(self):
        f = self.fixture
        pos = circuits.damage_scores(f.maps_tsv, f.circuit_path, threshold=0.5, sign="pos", mask=f.mask_path, emit=_quiet)
        circuit = nifti.read_nifti(f.circuit_path)[0].reshape(-1)[f.indices].astype(np.float64)
        indices, values = f.occupancy[3]
        self.assertAlmostEqual(pos["rows"][3]["pct"], 100.0 * float(values[circuit[indices] >= 0.5].sum()) / float(values.sum()), places=6)
        # Without a threshold: a continuous circuit has no % (n/a); a binary circuit counts its voxels
        unthresholded = circuits.damage_scores(f.maps_tsv, f.circuit_path, mask=f.mask_path, emit=_quiet)
        self.assertTrue(all(np.isnan(row["pct"]) for row in unthresholded["rows"]))
        binary = (np.abs(circuit) > 1.0).astype(np.float64)
        binary_path = f.write_circuit("circuit-bin.nii.gz", binary)
        result = circuits.damage_scores(f.maps_tsv, binary_path, mask=f.mask_path, emit=_quiet)
        self.assertTrue(result["binary"])
        self.assertAlmostEqual(result["rows"][3]["pct"], 100.0 * float(values[binary[indices] > 0].sum()) / float(values.sum()), places=6)

    def test_circuit_off_grid_is_refused(self):
        f = self.fixture
        other = np.diag([3.0, 3.0, 3.0, 1.0])
        path = os.path.join(self.tmp, "offgrid.nii.gz")
        nifti.write_nifti(path, np.ones(SHAPE, dtype=np.float32), other)
        with self.assertRaisesRegex(ValueError, "not on the grid"):
            circuits.damage_scores(f.maps_tsv, path, mask=f.mask_path, emit=_quiet)

    def test_mask_of_another_connectome_is_refused(self):
        """A mask with as many voxels as the cohort's but other voxels is refused (cohortmaps.json names its mask)."""
        f = self.fixture
        mask = nifti.read_nifti(f.mask_path)[0] != 0
        with open(os.path.join(f.maps_dir, "cohortmaps.json"), "w", encoding="utf-8") as handle:
            json.dump({"bundle": {"name": "Cohort", "mask_sha256": connectome.mask_content_sha256(mask), "shape": list(SHAPE), "affine": AFFINE.tolist()}}, handle)
        self.assertEqual(len(circuits.damage_scores(f.maps_tsv, f.circuit_path, mask=f.mask_path, emit=_quiet)["rows"]), len(f.occupancy))
        other = mask.copy()
        other[1, 1, 1], other[0, 0, 0] = False, True  # the same voxel count, one voxel moved
        other_path = os.path.join(self.tmp, "other_mask.nii.gz")
        nifti.write_nifti(other_path, other.astype(np.uint8), AFFINE)
        with self.assertRaisesRegex(ValueError, "not the mask of Cohort"):
            circuits.damage_scores(f.maps_tsv, f.circuit_path, mask=other_path, emit=_quiet)

    def test_outputs_have_no_absolute_paths(self):
        f = self.fixture
        out = os.path.join(self.tmp, "damage")
        result = circuits.damage_scores(f.maps_tsv, f.circuit_path, mask=f.mask_path, out_dir=out, name="Test", emit=_quiet)
        with open(result["record"], encoding="utf-8") as handle:
            record = json.load(handle)
        self.assertEqual(_absolute_strings(record), [])
        columns, rows = read_tsv(result["tsv"])
        self.assertIn("provenance", columns)
        self.assertEqual({row["provenance"] for row in rows}, {"same_cohort"})
        self.assertEqual(record["columns"]["sum"], "dmg_Test")
        self.assertEqual(circuits.default_name("circuit-Conj_conn-GSP_desc-T_funcmap.nii.gz"), "Conj")
        self.assertEqual(circuits.default_name("dlpfc_map.nii"), "dlpfc")
        self.assertEqual(circuits.damage_scores(f.maps_tsv, f.circuit_path, mask=f.mask_path, emit=_quiet)["columns"]["mean"], "dmg_test_mean")
        registry = runs.load(out)
        self.assertEqual([item["kind"] for item in registry.values()], ["circuits"])


class ProvenanceGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-circuits-")
        self.fixture = CohortFixture(self.tmp, n=10, study=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_same_cohort_circuit_is_refused(self):
        """A circuit made from some of the scored lesions is same_cohort; its association is refused."""
        f = self.fixture
        folder = os.path.join(self.tmp, "circuitA")
        path = f.write_circuit("circuit-A_T.nii.gz", f.circuit, folder)
        f.write_circuit_record(folder, f.sha[:3])
        out = os.path.join(self.tmp, "damageA")
        result = circuits.damage_scores(f.maps_tsv, path, mask=f.mask_path, out_dir=out, study=f.study_path, emit=_quiet)
        self.assertEqual(result["provenance"]["provenance"], "same_cohort")
        self.assertEqual(result["provenance"]["overlap"], 3)
        with self.assertRaises(circuits.CircularityError) as caught:
            circuits.association(result["record"], f.study_path, "group")
        self.assertIn("Validate", str(caught.exception))
        self.assertIn("frozen", str(caught.exception))
        with self.assertRaises(circuits.CircularityError):
            circuits.check_association(result)
        # The scores reach the study through the inbox, with their provenance in the audit record
        self.assertTrue(result["inbox"] and os.path.isfile(result["inbox"]))
        with open(result["inbox"], encoding="utf-8") as handle:
            posted = json.load(handle)
        self.assertEqual(posted["audit"][0]["provenance"], "same_cohort")
        self.assertIn("dmg_A_mean", next(iter(posted["rows"].values())))
        study = Study.open(f.study_path)
        study.apply_inbox()
        self.assertIn("dmg_A", study.columns)

    def test_declared_frozen_with_overlap_stays_same_cohort(self):
        f = self.fixture
        folder = os.path.join(self.tmp, "circuitB")
        path = f.write_circuit("circuit-B_T.nii.gz", f.circuit, folder)
        f.write_circuit_record(folder, f.sha[-1:])
        events = []
        result = circuits.damage_scores(f.maps_tsv, path, mask=f.mask_path, frozen_sha256=file_sha256(path), emit=lambda event, **fields: events.append((event, fields)))
        self.assertEqual(result["provenance"]["provenance"], "same_cohort")
        self.assertTrue(any(event == "warning" and fields.get("code") == "provenance" for event, fields in events))
        with self.assertRaises(circuits.CircularityError):
            circuits.check_association(result)

    def test_undeclared_circuit_without_record_is_not_independent(self):
        f = self.fixture
        result = circuits.damage_scores(f.maps_tsv, f.circuit_path, mask=f.mask_path, emit=_quiet)
        self.assertEqual(result["provenance"]["provenance"], "same_cohort")
        self.assertEqual(result["provenance"]["basis"], circuits.NOT_ESTABLISHED)

    def test_same_study_participants_count_as_overlap(self):
        """A re-traced lesion has another sha256, but the same participant of the same study is the same cohort."""
        f = self.fixture
        folder = os.path.join(self.tmp, "circuitC")
        path = f.write_circuit("circuit-C_T.nii.gz", f.circuit, folder)
        record = {"format": circuits.FORMAT_CIRCUIT, "version": 1, "name": "C", "source": {"lesion_sha256": ["f" * 64], "participants": ["sub-02"], "study": os.path.relpath(f.study_path, folder), "known": True}}
        with open(os.path.join(folder, circuits.CIRCUIT_JSON), "w", encoding="utf-8") as handle:
            json.dump(record, handle)
        result = circuits.damage_scores(f.maps_tsv, path, mask=f.mask_path, emit=_quiet)
        self.assertEqual(result["provenance"]["provenance"], "same_cohort")
        self.assertEqual(result["provenance"]["overlap"], 1)

    def test_moved_study_is_recognised_by_its_identity(self):
        """circuit.json names a study outside the shared folder by its identity (study.json name and creation time)."""
        f = self.fixture
        folder = os.path.join(self.tmp, "circuitE")
        path = f.write_circuit("circuit-E_T.nii.gz", f.circuit, folder)
        record = {"format": circuits.FORMAT_CIRCUIT, "version": 1, "name": "E", "source": {"lesion_sha256": ["f" * 64], "participants": ["sub-03"], "study": "study-moved",
                                                                                        "study_key": circuits.study_key(f.study_path), "known": True}}
        with open(os.path.join(folder, circuits.CIRCUIT_JSON), "w", encoding="utf-8") as handle:
            json.dump(record, handle)
        result = circuits.damage_scores(f.maps_tsv, path, mask=f.mask_path, emit=_quiet)
        self.assertEqual(result["provenance"]["provenance"], "same_cohort")
        self.assertEqual(result["provenance"]["overlap"], 1)

    def test_non_numeric_outcome_is_named(self):
        f = self.fixture
        folder = os.path.join(self.tmp, "circuitF")
        path = f.write_circuit("circuit-F_T.nii.gz", f.circuit, folder)
        f.write_circuit_record(folder, ["e" * 64])
        result = circuits.damage_scores(f.maps_tsv, path, mask=f.mask_path, out_dir=os.path.join(self.tmp, "damageF"), emit=_quiet)
        participants = os.path.join(self.tmp, "participants.tsv")
        with open(participants, "w", encoding="utf-8") as handle:
            handle.write("participant_id\tsite\n" + "".join(f"sub-{index + 1:02d}\t{'abc'[index % 3]}\n" for index in range(10)))
        with self.assertRaisesRegex(ValueError, "binary .two values. or numeric"):
            circuits.association(result["record"], participants, "site", n_boot=10)

    def test_frozen_circuit_association_runs(self):
        """A circuit from other lesions (or declared frozen with its sha256) may be tested; a wrong sha256 is refused."""
        f = self.fixture
        folder = os.path.join(self.tmp, "circuitD")
        path = f.write_circuit("circuit-D_T.nii.gz", f.circuit, folder)
        f.write_circuit_record(folder, ["e" * 64])
        out = os.path.join(self.tmp, "damageD")
        result = circuits.damage_scores(f.maps_tsv, path, mask=f.mask_path, out_dir=out, emit=_quiet)
        self.assertEqual(result["provenance"]["provenance"], "frozen")
        found = circuits.association(result["record"], f.study_path, "group", n_boot=50)
        self.assertEqual(found["outcome_type"], "binary")
        self.assertEqual(found["n"], 10)
        self.assertIn("auc", found["metrics"])
        continuous = circuits.association(result["record"], f.study_path, "score", n_boot=50)
        self.assertIn("r", continuous["metrics"])
        # Declared frozen without a record
        declared = circuits.damage_scores(f.maps_tsv, f.circuit_path, mask=f.mask_path, frozen_sha256=file_sha256(f.circuit_path).upper(), emit=_quiet)
        self.assertEqual(declared["provenance"]["provenance"], "frozen")
        with self.assertRaisesRegex(ValueError, "not the frozen"):
            circuits.damage_scores(f.maps_tsv, f.circuit_path, mask=f.mask_path, frozen_sha256="0" * 64, emit=_quiet)


class TargetScoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-circuits-")
        self.shape = (30, 30, 30)
        self.affine = np.diag([2.0, 2.0, 2.0, 1.0])
        self.affine[:3, 3] = -29.0
        rng = np.random.default_rng(3)
        self.circuit = rng.standard_normal(self.shape) * 0.5
        self.center = (8.0, -6.0, 4.0)
        self.sphere = circuits.sphere_mask(self.center, 6.0, self.shape, self.affine)
        self.circuit[self.sphere] += 4.0  # a planted high-value sphere
        self.circuit_path = os.path.join(self.tmp, "circuit.nii.gz")
        nifti.write_nifti(self.circuit_path, self.circuit.astype(np.float32), self.affine)
        gm = np.zeros(self.shape, dtype=np.float32)
        gm[3:-3, 3:-3, 3:-3] = 0.8
        gm[3:-3, 3:-3, 3:6] = 0.3  # below 0.5: not gray matter
        self.gm_path = os.path.join(self.tmp, f"tpl-{SPACE}_res-02_label-GM_probseg.nii.gz")
        nifti.write_nifti(self.gm_path, gm, self.affine)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_planted_sphere_scores_high(self):
        targets = [("planted", *self.center, 6.0), ("elsewhere", -14.0, 12.0, -10.0, 6.0)]
        result = circuits.score_targets(targets, self.circuit_path, self.gm_path, k=500, seed=1, circuit_space=SPACE)
        planted, elsewhere = result["rows"]
        self.assertGreater(planted["percentile_mean"], 99.0)
        self.assertGreater(planted["percentile_peak"], 95.0)
        self.assertEqual(planted["sign"], "+")
        self.assertLess(elsewhere["percentile_mean"], 99.0)
        self.assertEqual(planted["k"], 500)
        self.assertEqual(result["family"], "2009")
        # Deterministic with the seed
        again = circuits.score_targets(targets, self.circuit_path, self.gm_path, k=500, seed=1, circuit_space=SPACE)
        self.assertEqual(planted["percentile_mean"], again["rows"][0]["percentile_mean"])

    def test_research_only_and_no_new_coordinates(self):
        csv_path = os.path.join(self.tmp, "targets.csv")
        with open(csv_path, "w", encoding="utf-8") as handle:
            handle.write("label,x,y,z,radius\nDLPFC-L,-38,44,26,10\nplanted,8,-6,4,\n")
        out = os.path.join(self.tmp, "targets")
        result = circuits.score_targets(csv_path, self.circuit_path, self.gm_path, k=200, seed=0, circuit_space=SPACE, default_radius=6.0, out_dir=out)
        columns, rows = read_tsv(result["tsv"])
        self.assertEqual([row["label"] for row in rows], ["DLPFC-L", "planted"])
        self.assertEqual({row["use"] for row in rows}, {circuits.RESEARCH_ONLY})
        self.assertEqual([(float(row["x"]), float(row["y"]), float(row["z"])) for row in rows], [(-38.0, 44.0, 26.0), (8.0, -6.0, 4.0)])
        self.assertEqual(float(rows[1]["radius_mm"]), 6.0)
        self.assertNotIn("recommended", " ".join(columns).lower())
        with open(result["record"], encoding="utf-8") as handle:
            record = json.load(handle)
        self.assertEqual(record["use"], circuits.RESEARCH_ONLY)
        self.assertIn("no stimulation site is proposed", record["caption"])
        self.assertEqual(_absolute_strings(record), [])

    def test_gm_family_enforced(self):
        """The GM mask must be of the circuit's template family; an unknown space is refused too."""
        with self.assertRaises(circuits.FamilyError):
            circuits.score_targets([("t", 0, 0, 0, 6)], self.circuit_path, self.gm_path, k=50, circuit_space="MNI152NLin6Asym")
        nlin6 = os.path.join(self.tmp, "tpl-MNI152NLin6Asym_res-02_label-GM_probseg.nii.gz")
        shutil.copy(self.gm_path, nlin6)
        with self.assertRaises(circuits.FamilyError):
            circuits.score_targets([("t", 0, 0, 0, 6)], self.circuit_path, nlin6, k=50, circuit_space=SPACE)
        unnamed = os.path.join(self.tmp, "gm.nii.gz")
        shutil.copy(self.gm_path, unnamed)
        with self.assertRaises(circuits.FamilyError):
            circuits.score_targets([("t", 0, 0, 0, 6)], self.circuit_path, unnamed, k=50, circuit_space=SPACE)
        result = circuits.score_targets([("t", 0, 0, 0, 6)], self.circuit_path, unnamed, k=50, circuit_space=SPACE, gm_space="MNI152NLin2009cSym")
        self.assertEqual(result["family"], "2009")

    def test_null_spheres_are_centred_in_gray_matter(self):
        offsets = circuits.sphere_offsets(self.affine, 0.1)  # the centre voxel only
        self.assertEqual(offsets.tolist(), [[0, 0, 0]])
        gm, resampled = circuits.gm_mask_on_grid(self.gm_path, self.shape, self.affine)
        self.assertFalse(resampled)
        self.assertFalse(gm[:, :, 3:6].any())
        flag = np.where(gm, 1.0, -1.0)
        result = circuits.score_targets([("t", 0.0, 0.0, 0.0, 0.1)], (flag, self.affine), self.gm_path, k=300, circuit_space=SPACE)
        self.assertEqual(result["rows"][0]["percentile_mean"], 50.0)  # every null centre is in GM: all values are 1

    def test_sphere_stats_in_chunks(self):
        """Large radii are handled in chunks of centres with the same results."""
        support = np.isfinite(self.circuit)
        centres = np.argwhere(self.sphere)[:40]
        offsets = circuits.sphere_offsets(self.affine, 6.0)
        whole = circuits.sphere_stats(self.circuit, support, centres, offsets)
        saved = circuits.SPHERE_CHUNK_VOXELS
        circuits.SPHERE_CHUNK_VOXELS = len(offsets) * 3  # three centres at a time
        try:
            chunked = circuits.sphere_stats(self.circuit, support, centres, offsets)
        finally:
            circuits.SPHERE_CHUNK_VOXELS = saved
        for expected, found in zip(whole, chunked, strict=True):
            np.testing.assert_allclose(expected, found, rtol=1e-12, atol=0)  # nansum may add in another order

    def test_files_outside_the_shared_folder_are_named_only(self):
        """A GM prior outside the run root (e.g. a download cache) is stored by file name and sha256, never as ../../ paths."""
        cache = os.path.join(self.tmp, "cache")
        os.makedirs(cache)
        gm = shutil.copy(self.gm_path, cache)
        root = os.path.join(self.tmp, "root")
        out = os.path.join(root, "circuit", "targets")
        result = circuits.score_targets([("t", *self.center, 6.0)], self.circuit_path, gm, k=50, circuit_space=SPACE, out_dir=out, run_root=root)
        with open(result["record"], encoding="utf-8") as handle:
            record = json.load(handle)
        self.assertEqual(record["gm_mask"]["file"], os.path.basename(gm))
        self.assertEqual(record["gm_mask"]["sha256"], file_sha256(gm))
        self.assertEqual(record["circuit"]["file"], os.path.basename(self.circuit_path))
        inside = shutil.copy(self.gm_path, os.path.join(root, "circuit"))
        record = circuits.score_targets([("t", *self.center, 6.0)], self.circuit_path, inside, k=50, circuit_space=SPACE, out_dir=out, run_root=root)["record"]
        with open(record, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["gm_mask"]["file"], "../" + os.path.basename(inside))

    def test_read_slicer4_fcsv_and_markups_json(self):
        """A Slicer 4.x .fcsv says "CoordinateSystem = 1" for LPS (0 is RAS); a .mrk.json lists its control points."""
        lps = os.path.join(self.tmp, "old.fcsv")
        with open(lps, "w", encoding="utf-8") as handle:
            handle.write("# Markups fiducial file version = 4.10\n# CoordinateSystem = 1\n# columns = id,x,y,z,ow,ox,oy,oz,vis,sel,lock,label,desc,associatedNodeID\n")
            handle.write("vtkMRMLMarkupsFiducialNode_0,38,-44,26,0,0,0,1,1,1,0,F-1,,\n")
        self.assertEqual([(t["x"], t["y"], t["z"]) for t in circuits.read_targets(lps)], [(-38.0, 44.0, 26.0)])
        ras = os.path.join(self.tmp, "ras.fcsv")
        with open(lps, encoding="utf-8") as source, open(ras, "w", encoding="utf-8") as handle:
            handle.write(source.read().replace("CoordinateSystem = 1", "CoordinateSystem = 0"))
        self.assertEqual([(t["x"], t["y"], t["z"]) for t in circuits.read_targets(ras)], [(38.0, -44.0, 26.0)])
        mrk = os.path.join(self.tmp, "points.mrk.json")
        with open(mrk, "w", encoding="utf-8") as handle:
            json.dump({"markups": [{"type": "Fiducial", "coordinateSystem": "LPS", "controlPoints": [{"label": "S1", "position": [12.0, -8.0, 6.0]}, {"label": "", "position": [1.0, 2.0, 3.0]}]}]}, handle)
        self.assertEqual(circuits.read_targets(mrk, default_radius=4.0), [{"label": "S1", "x": -12.0, "y": 8.0, "z": 6.0, "radius_mm": 4.0},
                                                                         {"label": "target2", "x": -1.0, "y": -2.0, "z": 3.0, "radius_mm": 4.0}])

    def test_read_fcsv_targets(self):
        path = os.path.join(self.tmp, "points.fcsv")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# Markups fiducial file version = 4.11\n# CoordinateSystem = LPS\n# columns = id,x,y,z,ow,ox,oy,oz,vis,sel,lock,label,desc,associatedNodeID\n")
            handle.write("vtkMRMLMarkupsFiducialNode_0,38,-44,26,0,0,0,1,1,1,0,F-1,,\n")
        targets = circuits.read_targets(path, default_radius=7.0)
        self.assertEqual(targets, [{"label": "F-1", "x": -38.0, "y": 44.0, "z": 26.0, "radius_mm": 7.0}])


class SeedThisTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lnm-circuits-seed-")
        cls.meta = connectome.make_synthetic(os.path.join(cls.tmp, "syn.lnmc"), shape=(16, 18, 16), n_subjects=4, runs=(30,), seed=2, name="Syn")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _parent(self, name):
        """A cohort folder with a conjunction map, cohort.json (run id, maps.tsv) and the maps.tsv of two lesions."""
        folder = os.path.join(self.tmp, name)
        os.makedirs(folder)
        bundle = connectome.ConnectomeBundle.open(self.meta["path"])
        blob = np.zeros(bundle.n_voxels, dtype=np.float32)
        blob[self.meta["blobs"]["A"]["mask"]] = 1.0
        parent = os.path.join(folder, "group_conn-Syn_desc-conj_map.nii.gz")
        nifti.write_nifti(parent, engine.unmask(blob, bundle), bundle.affine)
        with open(os.path.join(folder, "maps.tsv"), "w", encoding="utf-8") as handle:
            handle.write("participant_id\tlesion_sha256\n" + f"sub-01\t{'a' * 64}\nsub-02\t{'b' * 64}\n")
        root = os.path.join(self.tmp, f"{name}-root")
        run_id = runs.register(root, "glm", folder, status="done")
        with open(os.path.join(folder, "cohort.json"), "w", encoding="utf-8") as handle:
            json.dump({"run_id": run_id, "maps_tsv": "maps.tsv"}, handle)
        return parent, root, run_id

    def test_seed_links_parent(self):
        parent, root, run_id = self._parent("cohortA")
        out = os.path.join(self.tmp, "circuits", "circuit-ConjA")
        record = circuits.seed_this(self.meta["path"], out, "Conj A", roi=parent, parent=parent, run_root=root, emit=_quiet)
        self.assertEqual(record["name"], "ConjA")
        self.assertIn(run_id, record["parents"])
        self.assertEqual(record["parent"]["sha256"], file_sha256(parent))
        self.assertEqual(record["source"]["lesion_sha256"], ["a" * 64, "b" * 64])
        for key in ("T", "AvgFz", "AvgR"):
            self.assertTrue(os.path.isfile(record["maps"][key]), key)
            self.assertIn("circuit-ConjA_conn-Syn_desc-", os.path.basename(record["maps"][key]))
        self.assertTrue(os.path.basename(record["maps"]["AvgFz"]).endswith("_desc-AvgRFz_funcmap.nii.gz"))
        # The run registry links the circuit to its parent run
        registered = runs.load(root)[record["run_id"]]
        self.assertEqual(registered["kind"], "circuits")
        self.assertEqual(registered["status"], "done")
        self.assertIn(run_id, registered["parents"])
        self.assertEqual([item["id"] for item in runs.lineage(root, record["run_id"])], [run_id])
        # The file holds relative paths, and read_circuit resolves them
        with open(os.path.join(out, circuits.CIRCUIT_JSON), encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertEqual(_absolute_strings({key: value for key, value in stored.items() if key != "software"}), [])
        self.assertEqual(circuits.read_circuit(record["maps"]["T"])["maps"]["T"], record["maps"]["T"])
        # The seeded network: the T map is high in the seed blob A and its partner A2, and negative in B
        bundle = connectome.ConnectomeBundle.open(self.meta["path"])
        t = nifti.read_nifti(record["maps"]["T"])[0].reshape(-1)[bundle.mask_indices]
        blobs = self.meta["blobs"]
        self.assertGreater(t[blobs["A2"]["mask"]].mean(), 3.0 * abs(np.median(t)))
        self.assertLess(t[blobs["B"]["mask"]].mean(), 0.0)
        self.assertEqual(circuits.find_circuits(os.path.dirname(out))[0]["name"], "ConjA")

    def test_signed_conjunction_seeds_one_sign(self):
        """A +1 / -1 conjunction map seeds its positive or its negative region, never their union unless asked."""
        bundle = connectome.ConnectomeBundle.open(self.meta["path"])
        signed = np.zeros(bundle.n_voxels, dtype=np.float32)
        signed[self.meta["blobs"]["A"]["mask"]] = 1.0
        signed[self.meta["blobs"]["B"]["mask"]] = -1.0
        path = os.path.join(self.tmp, "signed_conj.nii.gz")
        nifti.write_nifti(path, engine.unmask(signed, bundle).astype(np.int8), bundle.affine)
        n_pos, n_neg = int((signed > 0).sum()), int((signed < 0).sum())
        for sign, expected in (("pos", n_pos), ("neg", n_neg), ("both", n_pos + n_neg)):
            record = circuits.seed_this(bundle, os.path.join(self.tmp, f"signed-{sign}"), f"Conj{sign}", roi=path, sign=sign, register=False, emit=_quiet)
            self.assertEqual(record["seed"]["sign"], sign)
            self.assertEqual(int(nifti.read_nifti(record["roi"])[0].sum()), expected, sign)
        # The negative region's circuit is the anti-correlated network: high T in B, negative in A
        neg = circuits.read_circuit(os.path.join(self.tmp, "signed-neg"))
        t = nifti.read_nifti(neg["maps"]["T"])[0].reshape(-1)[bundle.mask_indices]
        self.assertLess(t[self.meta["blobs"]["A2"]["mask"]].mean(), 0.0)

    def test_peak_sphere_seed(self):
        out = os.path.join(self.tmp, "peak")
        record = circuits.seed_this(self.meta["path"], out, "PeakA", peak=self.meta["blobs"]["A"]["center_world"], radius_mm=4.0, emit=_quiet)
        self.assertEqual(record["seed"]["kind"], "peak")
        self.assertEqual(record["seed"]["radius_mm"], 4.0)
        self.assertGreater(record["seed"]["voxels_in_mask"], 0)
        self.assertFalse(record["source"]["known"])
        roi = nifti.read_nifti(record["roi"])[0]
        self.assertEqual(int(roi.sum()), record["seed"]["voxels_source"])
        with self.assertRaisesRegex(ValueError, "not both"):
            circuits.seed_this(self.meta["path"], out, "x", roi=record["roi"], peak=(0, 0, 0), radius_mm=4.0, emit=_quiet)

    def test_command_line(self):
        parent, root, _run_id = self._parent("cohortCli")
        out = os.path.join(self.tmp, "cli-seed")
        code, events, stderr = _run_cli("seed", "--bundle", self.meta["path"], "--out", out, "--name", "Cli", "--roi", parent, "--parent", parent, "--run-root", root, "--threads", "1")
        self.assertEqual(code, 0, stderr + json.dumps(events[-1:]))
        self.assertEqual(events[0]["event"], "start")
        self.assertEqual(events[-1]["event"], "done")
        self.assertTrue(any(event["event"] == "progress" for event in events))
        code, events, stderr = _run_cli("seed", "--bundle", self.meta["path"], "--out", out, "--name", "Cli", "--peak", "0", "0", "0")
        self.assertEqual(code, jobs_exit_usage())
        self.assertEqual(events[-1]["code"], "usage")


def jobs_exit_usage():
    from LesionNetworkMappingLib import jobs

    return jobs.EXIT_USAGE


class CommandLineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-circuits-cli-")
        self.fixture = CohortFixture(self.tmp, n=8, study=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_damage_refuses_same_cohort_outcome(self):
        f = self.fixture
        out = os.path.join(self.tmp, "damage")
        code, events, stderr = _run_cli("damage", "--maps", f.maps_tsv, "--circuit", f.circuit_path, "--out", out, "--mask", f.mask_path, "--study", f.study_path, "--no-post")
        self.assertEqual(code, 0, stderr + json.dumps(events[-1:]))
        result = next(event for event in events if event["event"] == "result")
        self.assertEqual(result["provenance"], "same_cohort")
        code, events, stderr = _run_cli("damage", "--maps", f.maps_tsv, "--circuit", f.circuit_path, "--out", out, "--mask", f.mask_path, "--study", f.study_path, "--no-post", "--outcome", "group")
        self.assertEqual(code, 1)
        self.assertEqual(events[-1]["event"], "error")
        self.assertEqual(events[-1]["code"], "circular")
        self.assertFalse(os.path.isdir(os.path.join(f.study_path, ".studio", "inbox")) and os.listdir(os.path.join(f.study_path, ".studio", "inbox")))

    def test_targets(self):
        f = self.fixture
        csv_path = os.path.join(self.tmp, "t.csv")
        with open(csv_path, "w", encoding="utf-8") as handle:
            handle.write("label,x,y,z\nA,0,0,0\n")
        gm = np.full(SHAPE, 0.9, dtype=np.float32)
        gm_path = os.path.join(self.tmp, f"tpl-{SPACE}_res-02_label-GM_probseg.nii.gz")
        nifti.write_nifti(gm_path, gm, AFFINE)
        out = os.path.join(self.tmp, "targets")
        code, events, stderr = _run_cli("targets", "--targets", csv_path, "--circuit", f.circuit_path, "--gm-mask", gm_path, "--out", out, "--k", "50", "--circuit-space", SPACE, "--radius", "3")
        self.assertEqual(code, 0, stderr + json.dumps(events[-1:]))
        self.assertTrue(os.path.isfile(os.path.join(out, circuits.TARGETS_TSV)))
        result = next(event for event in events if event["event"] == "result")
        self.assertEqual(result["use"], circuits.RESEARCH_ONLY)
        code, events, stderr = _run_cli("targets", "--targets", csv_path, "--circuit", f.circuit_path, "--gm-mask", gm_path, "--out", out, "--circuit-space", "MNI152NLin6Asym")
        self.assertEqual(code, 1)
        self.assertEqual(events[-1]["code"], "family")


if __name__ == "__main__":
    unittest.main()

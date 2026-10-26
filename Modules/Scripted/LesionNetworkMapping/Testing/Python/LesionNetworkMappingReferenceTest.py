"""Tests of the connectome reference maps (reference.py, specification lnm-reference-v1).

Every map is compared with a brute-force computation on dense matrices built from the bundle's
stored time series: mu_r (exact), mu_z, sigma_r and hubness (same sampled seeds), the PCs and
eigenvalues (dense eigendecomposition of the column-centred group r-matrix) and EV1. Then the
loader's staleness rules, resume after a Stop during the PC stage, the hubness source and the CLI.

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingReferenceTest.py -v
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
from scipy import linalg

from LesionNetworkMappingLib import connectome, jobs, nifti, reference, runs

SHAPE = (20, 24, 20)
RMAX = 0.999999
SEEDS = 300


def _env():
    return dict(os.environ, PYTHONPATH=MODULE_DIR + os.pathsep + os.environ.get("PYTHONPATH", ""))


def _write_bundle(path, seed=7):
    """A (20, 24, 20) bundle of 6 subjects with three planted networks of distinct strength.

    Subject 6 has a single run (the others two); voxel ZERO_RUN has zero variance in the first run
    of subject 2 and voxel ZERO_ALL in every run of subject 3.
    """
    rng = np.random.default_rng(seed)
    affine = np.diag([2.0, 2.0, 2.0, 1.0])
    affine[:3, 3] = -(np.array(SHAPE) - 1)
    ijk = np.indices(SHAPE).reshape(3, -1).T
    centre = (np.array(SHAPE) - 1) / 2.0
    mask = (((ijk - centre) / (np.array(SHAPE) / 2.2)) ** 2).sum(axis=1) <= 1.0
    voxels = ijk[mask]

    def blob(fraction, radius=4.0):
        return np.linalg.norm(voxels - np.round(np.array(fraction) * (np.array(SHAPE) - 1)), axis=1) <= radius

    networks = [(3.0, blob((0.25, 0.3, 0.5)), blob((0.72, 0.7, 0.5))), (2.2, blob((0.5, 0.5, 0.2)), None), (1.6, blob((0.5, 0.25, 0.78)), None)]
    offsets = 100.0 + 10.0 * rng.random(len(voxels))
    zero_run, zero_all = int(np.flatnonzero(networks[1][1])[0]), len(voxels) // 2
    writer = connectome.ConnectomeWriter(path, mask.reshape(SHAPE), affine, name="ReferenceTest", overwrite=True)
    for s in range(6):
        subject_runs = []
        for run, n_tr in enumerate((50,) if s == 5 else (40, 40)):
            x = rng.standard_normal((len(voxels), n_tr))
            for amplitude, positive, negative in networks:
                latent = rng.standard_normal(n_tr)
                x[positive] += amplitude * latent
                if negative is not None:
                    x[negative] -= amplitude * latent
            x += offsets[:, np.newaxis]
            if (s == 1 and run == 0) or s == 2:
                x[zero_all if s == 2 else zero_run] = offsets[zero_all if s == 2 else zero_run]
            subject_runs.append(x)
        writer.add_subject(f"sub-{s + 1:03d}", subject_runs)
    writer.finalize()
    return path, zero_run, zero_all


def _dense(bundle, seeds):
    """Brute force from the stored rows (float64): C, mu_r and the sampled maps of the given seeds."""
    n_voxels = bundle.n_voxels
    is_seed = np.zeros(n_voxels, dtype=bool)
    is_seed[seeds] = True
    group_c = np.zeros((n_voxels, n_voxels))
    mu_r = np.zeros(n_voxels)
    sum_z = np.zeros((n_voxels, len(seeds)))
    sum_r = np.zeros((n_voxels, len(seeds)))
    sum_abs_z = np.zeros((n_voxels, len(seeds)))
    ids = bundle.subject_ids
    for subject_id in ids:
        subject = bundle.load_subject(subject_id)
        x = subject.data.astype(np.float64)
        bounds = np.cumsum((0, *subject.runs))
        n_runs = len(subject.runs)
        seed_rows = np.zeros((len(seeds), x.shape[1]))
        for run in range(n_runs):
            xr = x[:, bounds[run]:bounds[run + 1]]
            gram = xr @ xr.T
            group_c += gram / n_runs / len(ids)
            valid = np.diag(gram) > 0
            off_diagonal = gram[:, valid].sum(axis=1) - np.diag(gram)
            mu_r += np.where(valid, off_diagonal / (valid.sum() - 1), 0.0) / n_runs / len(ids)
            # Seed time course of a one-voxel lesion: demeaned and unit norm per run, / nRuns
            s = xr[seeds] - xr[seeds].mean(axis=1, keepdims=True)
            norm = np.linalg.norm(s, axis=1, keepdims=True)
            seed_rows[:, bounds[run]:bounds[run + 1]] = np.divide(s, norm, out=np.zeros_like(s), where=norm > 0) / n_runs
        r = np.clip(x @ seed_rows.T, -RMAX, RMAX)
        sum_r += r
        sum_z += np.arctanh(r)
        sum_abs_z += np.abs(np.arctanh(r))
    self_entry = np.zeros((n_voxels, len(seeds)), dtype=bool)
    self_entry[seeds, np.arange(len(seeds))] = True
    keep = ~self_entry
    n = len(ids)
    mean_r = sum_r / n
    sigma = np.array([np.std(mean_r[v][keep[v]], ddof=1) for v in range(n_voxels)])
    mu_z = np.array([sum_z[v][keep[v]].mean() for v in range(n_voxels)]) / n
    hub = np.array([sum_abs_z[v][keep[v]].mean() for v in range(n_voxels)]) / n
    return {"C": group_c, "mu_r": mu_r, "mu_z": mu_z, "sigma_r": sigma, "hub": hub}


def _pearson(a, b):
    a = np.asarray(a, dtype=np.float64) - np.mean(a)
    b = np.asarray(b, dtype=np.float64) - np.mean(b)
    return float(a @ b / np.sqrt((a @ a) * (b @ b)))


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lnm-reference-test-")
        cls.bundle_path, cls.zero_run, cls.zero_all = _write_bundle(os.path.join(cls.tmp, "ref.lnmc"))
        cls.bundle = connectome.ConnectomeBundle.open(cls.bundle_path)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def copy_bundle(self, name):
        path = os.path.join(self.tmp, name)
        shutil.copytree(self.bundle_path, path, ignore=shutil.ignore_patterns("derived", "derivatives"))
        return path

    @staticmethod
    def quiet_build(*args, events=None, **kwargs):
        sink = [] if events is None else events
        return reference.build(*args, emit=lambda event, **fields: sink.append((event, fields)), **kwargs)


class ExactnessTest(_Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.events = []
        cls.record = cls.quiet_build(cls.bundle, n_seeds=SEEDS, ev1=True, events=cls.events)
        cls.ref = reference.load_reference(cls.bundle)
        cls.seeds = np.sort(np.random.default_rng(0).choice(cls.bundle.n_voxels, size=SEEDS, replace=False))
        cls.dense = _dense(cls.bundle, cls.seeds)

    def test_fixture(self):
        # The fixture exercises a one-run subject and zero-variance voxels
        self.assertEqual(self.bundle.runs("sub-006"), (50,))
        self.assertEqual(float(np.abs(self.bundle.load_subject("sub-002").data[self.zero_run, :40]).max()), 0.0)
        self.assertEqual(float(np.abs(self.bundle.load_subject("sub-003").data[self.zero_all]).max()), 0.0)
        self.assertIsNotNone(self.ref)
        self.assertEqual(self.ref.path, os.path.join(self.bundle_path, "derived", "reference"))

    def test_mu_r_exact(self):
        np.testing.assert_allclose(self.ref.maps["mu_r"], self.dense["mu_r"], rtol=0, atol=1e-6)
        self.assertEqual(self.ref.maps["mu_r"].dtype, np.float32)

    def test_sampled_maps_match_brute_force_on_the_same_seeds(self):
        self.assertEqual(self.record["seeds"]["sha256"], reference.seeds_sha256(self.seeds))
        np.testing.assert_array_equal(reference.select_seeds(self.bundle.n_voxels, SEEDS, 0), self.seeds)
        for key, dense_key in (("mu_z", "mu_z"), ("sigma_r", "sigma_r"), ("hub", "hub")):
            with self.subTest(key=key):
                np.testing.assert_allclose(self.ref.maps[key], self.dense[dense_key], rtol=0, atol=1e-6)
        self.assertEqual(self.record["hub"]["source"], "computed")

    def test_pcs_match_dense_decomposition(self):
        group_c = self.dense["C"]
        n_voxels = group_c.shape[0]
        centred = group_c - group_c.mean(axis=0, keepdims=True)
        values, vectors = linalg.eigh(centred @ centred.T, subset_by_index=[n_voxels - 3, n_voxels - 1])
        order = np.argsort(values)[::-1]
        for index, column in enumerate(order):
            pc = self.ref.maps[f"pc{index + 1}"]
            expected = vectors[:, column] * np.sign(_pearson(vectors[:, column], self.dense["mu_r"]))
            with self.subTest(pc=index + 1):
                self.assertAlmostEqual(float(np.linalg.norm(pc)), 1.0, places=5)
                self.assertGreater(_pearson(pc, expected), 0.9999)
                self.assertGreaterEqual(_pearson(pc, self.ref.maps["mu_r"]), 0.0)  # sign convention
                self.assertLess(abs(self.ref.eigvals[index] / (values[column] / (n_voxels - 1)) - 1.0), 1e-3)
        convergence = self.record["pca"]["convergence"]
        self.assertEqual(len(convergence["history"]), 2 * reference.DEFAULT_POWER_ITERS + 2)
        self.assertLess(convergence["max_rel_change"], 1e-3)

    def test_ev1_matches_dense_leading_eigenvector(self):
        values, vectors = linalg.eigh(self.dense["C"], subset_by_index=[self.bundle.n_voxels - 1, self.bundle.n_voxels - 1])
        self.assertGreater(abs(_pearson(self.ref.maps["ev1"], vectors[:, 0])), 0.9999)
        self.assertGreaterEqual(_pearson(self.ref.maps["ev1"], self.ref.maps["mu_r"]), 0.0)
        self.assertLess(abs(self.record["ev1"]["eigenvalue"] / values[0] - 1.0), 1e-3)

    def test_reference_json_describes_definitions_and_inputs(self):
        with open(os.path.join(self.ref.path, reference.REFERENCE_JSON), encoding="utf-8") as f:
            record = json.load(f)
        self.assertEqual(record["definition"], reference.DEFINITION)
        for key in ("mu_r", "mu_z", "sigma_r", "hub_abs_z", "pcs", "eigvals", "ev1"):
            item = record["maps"][key]
            self.assertTrue(item["definition"])
            self.assertIn(item["exactness"], ("exact", "sampled", "randomized"))
            self.assertEqual(item["sha256"], reference._file_sha256(os.path.join(self.ref.path, item["file"])))
        self.assertEqual(record["maps"]["mu_r"]["label"], "mean r profile (voxelwise)")
        self.assertEqual(record["maps"]["hub_abs_z"]["label"], "hubness")
        self.assertEqual(record["maps"]["pcs"]["shape"], [3, self.bundle.n_voxels])
        self.assertEqual(record["subjects"]["n"], 6)
        self.assertEqual(record["subjects"]["sha256"], connectome.subjects_sha256(self.bundle.subject_ids))
        self.assertEqual(record["manifest_sha256"], reference._file_sha256(os.path.join(self.bundle_path, "connectome.json")))
        self.assertEqual(record["mask_sha256"], self.bundle.mask_sha256)
        for key, value in (("K_seeds", SEEDS), ("seed", 0), ("k", 3), ("power_iters", 3), ("oversample", 7)):
            self.assertEqual(record[key], value)
        self.assertTrue(record["created"])
        self.assertGreaterEqual(record["elapsed"], 0.0)
        self.assertEqual(self.ref.sha256, reference._file_sha256(os.path.join(self.ref.path, reference.REFERENCE_JSON)))
        self.assertFalse(os.path.exists(os.path.join(self.ref.path, reference.WORK_DIR)))
        names = [event for event, _ in self.events]
        self.assertEqual(names[0], "start")
        self.assertEqual(names[-2:], ["result", "done"])
        self.assertEqual([fields["name"] for event, fields in self.events if event == "stage"], list(reference.STAGES))
        start = self.events[0][1]
        self.assertIsNotNone(start["estimate"]["seconds"])
        self.assertGreater(start["estimate"]["memory_bytes"], 0)
        progress = [fields for event, fields in self.events if event == "progress"]
        self.assertEqual(progress[-1]["done"], progress[-1]["total"])
        self.assertTrue(all(fields["eta"] is not None for fields in progress))

    def test_restrict_and_covariates(self):
        voxels = np.arange(0, self.bundle.n_voxels, 7)
        restricted = self.ref.restrict(voxels)
        np.testing.assert_array_equal(restricted.maps["pc2"], self.ref.maps["pc2"][voxels])
        self.assertEqual(self.ref.covariates().shape, (2, self.bundle.n_voxels))


class LoaderTest(_Base):
    def test_stale_and_missing_references_are_rejected(self):
        path = self.copy_bundle("stale.lnmc")
        self.assertIsNone(reference.load_reference(path))
        self.assertEqual(reference.reference_status(path)[0]["status"], "missing")
        self.quiet_build(path, n_seeds=20, power_iters=1)
        self.assertIsNotNone(reference.load_reference(path))
        all_sha = connectome.subjects_sha256(connectome.ConnectomeBundle.open(path).subject_ids)
        self.assertIsNotNone(reference.load_reference(path, subjects_sha=all_sha))
        self.assertIsNone(reference.load_reference(path, subjects_sha=connectome.subjects_sha256(["sub-001"])))

        # Descriptive metadata: still valid, except in strict mode
        connectome.set_metadata(path, description="edited", subsets={"user:first": ["sub-001", "sub-002"]})
        self.assertIsNotNone(reference.load_reference(path))
        self.assertIsNone(reference.load_reference(path, strict=True))

        # The preprocessing record (for example GSR) describes the data: stale
        connectome.set_metadata(path, preprocessing={"gsr": True})
        self.assertIsNone(reference.load_reference(path))
        self.assertIn("preprocessing", reference.reference_status(path)[0]["reason"])
        connectome.set_metadata(path, preprocessing=None)
        self.assertIsNotNone(reference.load_reference(path))

        # A manifest change of the data (one subject fewer): stale
        manifest = os.path.join(path, "connectome.json")
        with open(manifest, encoding="utf-8") as f:
            original = f.read()
        metadata = json.loads(original)
        metadata["subjects"] = metadata["subjects"][:-1]
        metadata.pop("subjectsSha256", None)
        with open(manifest, "w", encoding="utf-8") as f:
            json.dump(metadata, f)
        self.assertIsNone(reference.load_reference(path))
        self.assertEqual(reference.reference_status(path)[0]["status"], "stale")
        with open(manifest, "w", encoding="utf-8") as f:
            f.write(original)
        self.assertIsNotNone(reference.load_reference(path))

        # Another mask with the same voxel count: stale
        mask_path = os.path.join(path, "mask.nii.gz")
        shutil.copy(mask_path, mask_path + ".orig")
        data, affine, _ = nifti.read_nifti(mask_path)
        data = data.reshape(SHAPE).copy()
        inside = np.flatnonzero(data.reshape(-1))
        outside = np.flatnonzero(data.reshape(-1) == 0)
        flat = data.reshape(-1)
        flat[inside[0]], flat[outside[0]] = 0, 1
        nifti.write_nifti(mask_path, data, affine)
        self.assertIsNone(reference.load_reference(path))
        os.replace(mask_path + ".orig", mask_path)
        self.assertIsNotNone(reference.load_reference(path))

        # A map file that does not match its sha256: corrupt
        mu_path = os.path.join(path, "derived", "reference", "mu_r.npy")
        np.save(mu_path, np.zeros(self.bundle.n_voxels, dtype=np.float32))
        self.assertIsNone(reference.load_reference(path))
        self.assertEqual(reference.reference_status(path)[0]["status"], "corrupt")
        self.assertIsNotNone(reference.load_reference(path, verify=False))

    def test_bundle_converted_again_with_other_data_is_stale(self):
        # Same grid, mask, subjects, runs and dtype (identical connectome.json): only the time series differ
        path = self.copy_bundle("reconverted.lnmc")
        self.quiet_build(path, n_seeds=20, power_iters=1)
        with open(os.path.join(path, "connectome.json"), encoding="utf-8") as f:
            manifest = f.read()
        _write_bundle(path, seed=99)
        with open(os.path.join(path, "connectome.json"), encoding="utf-8") as f:
            self.assertEqual(f.read(), manifest)
        self.assertIsNone(reference.load_reference(path))
        self.assertIsNone(reference.load_reference(path, strict=True))
        status = reference.reference_status(path)[0]
        self.assertEqual(status["status"], "stale")
        self.assertIn("time series", status["reason"])
        record = self.quiet_build(path, n_seeds=20, power_iters=1)
        self.assertEqual(record["subjects"]["data_sha256"], reference.subjects_data_sha256(path, record["subjects"]["ids"]))
        self.assertIsNotNone(reference.load_reference(path))

    def test_malformed_reference_json_is_corrupt(self):
        path = self.copy_bundle("malformed.lnmc")
        self.quiet_build(path, n_seeds=20, power_iters=1)
        json_path = os.path.join(path, "derived", "reference", reference.REFERENCE_JSON)
        with open(json_path, encoding="utf-8") as f:
            record = json.load(f)
        broken = {**record["maps"], "mu_r": "mu_r.npy"}
        for maps in (broken, {**record["maps"], "mu_r": {"file": "../../connectome.json"}}, {"pcs": record["maps"]["pcs"]}, []):
            with self.subTest(maps=maps):
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump({**record, "maps": maps}, f)
                self.assertEqual(reference.reference_status(path)[0]["status"], "corrupt")
                self.assertIsNone(reference.load_reference(path))

    def test_subject_list_folders(self):
        path = self.copy_bundle("subsets.lnmc")
        record = self.quiet_build(path, subject_list="sub-001, sub-003,sub-002", n_seeds=20, power_iters=1)
        ids = ["sub-001", "sub-002", "sub-003"]
        sha = connectome.subjects_sha256(ids)
        folder = os.path.join(path, "derived", "reference", f"subj-{sha[:12]}")
        self.assertTrue(os.path.isfile(os.path.join(folder, reference.REFERENCE_JSON)))
        self.assertEqual(record["subjects"]["ids"], ids)
        self.assertEqual(record["subjects"]["list_source"], "ids")
        self.assertEqual(reference.load_reference(path, subjects_sha=sha).path, folder)
        self.assertEqual(reference.load_reference(path).path, folder)  # the only valid one
        self.quiet_build(path, subjects=4, n_seeds=20, power_iters=1)
        four = reference.load_reference(path)
        self.assertEqual(four.metadata["subjects"]["n"], 4)  # the one built from the most subjects
        self.quiet_build(path, n_seeds=20, power_iters=1)
        self.assertEqual(reference.load_reference(path).path, os.path.join(path, "derived", "reference"))  # all subjects preferred
        self.assertEqual(reference.load_reference(path, subjects_sha=sha).path, folder)


class ResumeTest(_Base):
    def test_resume_after_stop_in_pc_stage_gives_identical_outputs(self):
        full = os.path.join(self.tmp, "full")
        full_record = self.quiet_build(self.bundle, out_dir=full, n_seeds=40, ev1=True, register=False)
        stopped = os.path.join(self.tmp, "stopped")
        events = []
        n = self.bundle.n_subjects
        # Stop at the first subject of PC step 3 (after the maps stage and 2 PC steps)
        cancel = lambda: any(event == "progress" and fields.get("stage") == "pcs" and fields["done"] >= 3 * n for event, fields in events)  # noqa: E731
        with self.assertRaises(jobs.Cancelled):
            self.quiet_build(self.bundle, out_dir=stopped, n_seeds=40, ev1=True, cancel=cancel, events=events, run_root=self.tmp)
        self.assertFalse(os.path.exists(os.path.join(stopped, reference.REFERENCE_JSON)))
        state = jobs.ResumeState(os.path.join(stopped, reference.WORK_DIR, reference.RESUME_NAME))
        self.assertEqual(state.done, ["maps", "pc-step-1", "pc-step-2"])
        self.assertEqual(runs.latest(self.tmp, "reference")["status"], "cancelled")
        # A hard kill after step 3 saved its basis but before it was marked done leaves its file behind:
        # the resumed step 3 starts again from the basis of step 2
        work = os.path.join(stopped, reference.WORK_DIR)
        np.save(os.path.join(work, reference._q_name("pc", 3)), np.ones((self.bundle.n_voxels, 10)))

        events = []
        record = self.quiet_build(self.bundle, out_dir=stopped, n_seeds=40, ev1=True, resume=True, events=events, run_root=self.tmp)
        self.assertEqual(events[0][1]["resumed"], ["maps", "pc-step-1", "pc-step-2"])
        # reference.json describes the whole build, including the convergence of the steps before the Stop
        self.assertEqual(len(record["pca"]["convergence"]["history"]), 2 * reference.DEFAULT_POWER_ITERS + 2)
        self.assertEqual(record["pca"]["convergence"], full_record["pca"]["convergence"])
        self.assertEqual(record["ev1"], full_record["ev1"])
        self.assertFalse(any(fields.get("stage") == "maps" for event, fields in events if event == "progress"))
        self.assertEqual(runs.latest(self.tmp, "reference")["status"], "done")
        for name in ("mu_r.npy", "mu_z.npy", "sigma_r.npy", "hub_abs_z.npy", "pcs.npy", "eigvals.npy", "ev1.npy"):
            with self.subTest(name=name):
                np.testing.assert_array_equal(np.load(os.path.join(stopped, name)), np.load(os.path.join(full, name)))

        # Other parameters: the saved progress is not reused
        events = []
        with self.assertRaises(jobs.Cancelled):
            self.quiet_build(self.bundle, out_dir=stopped, n_seeds=40, cancel=lambda: ("stage", {"name": "pcs", "index": 2, "total": 3}) in events, events=events, register=False)
        events = []
        self.quiet_build(self.bundle, out_dir=stopped, n_seeds=41, resume=True, events=events, register=False)
        self.assertIn("resume_mismatch", [fields.get("code") for event, fields in events if event == "warning"])


class HubnessSourceTest(_Base):
    def test_existing_degree_map_is_copied_when_it_is_hubness(self):
        path = self.copy_bundle("degree.lnmc")
        bundle = connectome.ConnectomeBundle.open(path)
        degree = connectome.compute_degree(bundle, n_seeds=50, measure="abs_fz", store=True)
        record = self.quiet_build(bundle, n_seeds=20, power_iters=1)
        np.testing.assert_array_equal(reference.load_reference(bundle).maps["hub"], degree)
        self.assertEqual(record["hub"]["source"], "bundle derived/degree.npy")
        self.assertEqual(record["maps"]["hub_abs_z"]["source"], "bundle derived/degree.npy")

        connectome.compute_degree(bundle, n_seeds=50, measure="r", store=True)  # signed: not hubness
        events = []
        record = self.quiet_build(bundle, n_seeds=20, power_iters=1, events=events)
        self.assertEqual(record["hub"]["source"], "computed")
        self.assertIn("hub_computed", [fields.get("code") for event, fields in events if event == "warning"])
        record = self.quiet_build(bundle, n_seeds=20, power_iters=1, hub="compute")
        self.assertEqual(record["hub"]["reason"], "requested")

        # A degree map stored between a Stop and --resume changes the hubness source: the build starts again
        out = os.path.join(self.tmp, "hub-resume")
        os.remove(os.path.join(path, "derived", "degree.npy"))
        events = []
        with self.assertRaises(jobs.Cancelled):
            self.quiet_build(bundle, out_dir=out, n_seeds=20, power_iters=1, register=False, cancel=lambda: ("stage", {"name": "pcs", "index": 2, "total": 3}) in events, events=events)
        connectome.compute_degree(bundle, n_seeds=50, measure="abs_fz", store=True)
        events = []
        record = self.quiet_build(bundle, out_dir=out, n_seeds=20, power_iters=1, register=False, resume=True, events=events)
        self.assertIn("resume_mismatch", [fields.get("code") for event, fields in events if event == "warning"])
        self.assertEqual(record["hub"]["source"], "bundle derived/degree.npy")


class ExternalMapTest(_Base):
    def test_compare_to_external_after_resampling(self):
        vector = np.linspace(-1.0, 2.0, self.bundle.n_voxels) ** 2
        volume = np.zeros(SHAPE)
        volume.reshape(-1)[self.bundle.mask_indices] = vector + 5.0  # no exact zeros inside the mask
        # The same image stored radiologically (i axis flipped): identical after resampling
        affine = self.bundle.affine.copy()
        flipped = affine @ np.array([[-1.0, 0, 0, SHAPE[0] - 1], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
        path = os.path.join(self.tmp, "external.nii.gz")
        nifti.write_nifti(path, volume[::-1].astype(np.float32), flipped)
        r, rho = reference.compare_to_external(vector, path, self.bundle)
        self.assertGreater(r, 0.9999)
        self.assertGreater(rho, 0.9999)
        on_grid = reference.external_on_grid(path, self.bundle)
        np.testing.assert_allclose(on_grid, vector + 5.0, rtol=1e-6)


class CommandLineTest(_Base):
    def run_cli(self, *args):
        result = subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.reference", *args], check=False, capture_output=True, text=True, env=_env(), cwd=self.tmp, timeout=600)
        events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        return result.returncode, events, result.stderr

    def test_build_estimate_compare_and_stop(self):
        out = os.path.join(self.tmp, "cli")
        cancel_file = os.path.join(self.tmp, "stop")
        open(cancel_file, "w").close()
        args = ["build", "--bundle", self.bundle_path, "--out", out, "--seeds", "30", "--power-iters", "1", "--threads", "1", "--run-root", self.tmp, "--cancel-file", cancel_file]
        code, events, stderr = self.run_cli(*args)
        self.assertEqual(code, jobs.EXIT_CANCELLED, stderr)
        self.assertEqual(events[-1]["event"], "cancelled")
        os.remove(cancel_file)
        code, events, stderr = self.run_cli(*args, "--resume")
        self.assertEqual(code, 0, stderr)
        names = [event["event"] for event in events]
        self.assertEqual(names[0], "start")
        self.assertEqual(names[-1], "done")
        self.assertIn("result", names)
        self.assertEqual(events[0]["job"], "reference")
        self.assertIn("seconds", events[0]["estimate"])
        self.assertIsNotNone(events[-1]["run_id"])
        record = runs.load(self.tmp)[events[-1]["run_id"]]
        self.assertEqual((record["kind"], record["status"]), ("reference", "done"))
        ref = reference.load_reference(self.bundle, path=out)
        self.assertIsNotNone(ref)

        code, events, stderr = self.run_cli("estimate", "--bundle", self.bundle_path, "--subjects", "3", "--seeds", "500")
        self.assertEqual(code, 0, stderr)
        estimate = next(event for event in events if event["event"] == "result")
        self.assertEqual((estimate["n_subjects"], estimate["n_seeds"], estimate["passes"]), (3, 500, 8))
        self.assertGreater(estimate["seconds"], 0)

        volume = np.zeros(SHAPE, dtype=np.float32)
        volume.reshape(-1)[self.bundle.mask_indices] = ref.maps["mu_r"] + 10.0
        external = os.path.join(self.tmp, "degree_like.nii.gz")
        nifti.write_nifti(external, volume, self.bundle.affine)
        code, events, stderr = self.run_cli("compare", "--bundle", self.bundle_path, "--external", external, "--reference", out)
        self.assertEqual(code, 0, stderr)
        result = next(event for event in events if event["event"] == "result")
        self.assertGreater(result["comparisons"]["mu_r"]["r"], 0.9999)
        self.assertEqual(set(result["comparisons"]), {"mu_r", "mu_z", "sigma_r", "hub", "pc1", "pc2", "pc3"})

        code, events, _ = self.run_cli("compare", "--bundle", self.bundle_path, "--external", external, "--reference", os.path.join(self.tmp, "nothing"))
        self.assertEqual((code, events[-1]["code"]), (1, "no_reference"))
        code, events, _ = self.run_cli("build", "--bundle", self.bundle_path, "--subjects", "2", "--subject-list", "sub-001")
        self.assertEqual((code, events[-1]["code"]), (jobs.EXIT_USAGE, "usage"))


if __name__ == "__main__":
    unittest.main()

"""Tests of the engine v2 additions (engine.py; addendum §3.6, §3.7, §3.12).

Label (parcel) seeds against dense lesion weights, the mean-only float32 accumulator against the
Welford mean, the parcel matrix C and parcel maps against a brute-force computation from the raw
time series, split-half sums, the reference QA in qa.json, the 4D null Fz maps, the job protocol
(--cancel-file, "t" in every event, volume_ml in the lesion qc), the lesion paths of cohort-map
files, and the byte-identity of the outputs of the engine command line as it was before these
additions (golden sha256 recorded before engine.py changed).

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingEngineV2Test.py -v
"""

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np

from LesionNetworkMappingLib import cohortmaps, connectome, engine, geometry, jobs, nifti, qa, reference

AFFINE = np.diag([2.0, 2.0, 2.0, 1.0])
REFERENCE_QA_KEYS = {
    "mu_r", "mu_rho", "pc1_r", "pc1_rho", "pc2_r", "pc2_rho", "pc3_r", "pc3_rho", "hub_r", "hub_rho",
    "r2_ref", "partial_pc1_mu", "mu_r_sample", "percentile", "k", "resolution", "level", "text", "null_kind",
}  # fmt: skip

# sha256 of the engine command line outputs of _golden_outputs(), recorded with engine.py as it was
# before the v2 additions (L-16). Bitwise results depend on the BLAS kernels: they are compared only
# on the platform where they were recorded (GOLDEN_PLATFORM); elsewhere that comparison is skipped.
GOLDEN_PLATFORM = "linux|x86_64|numpy 2.4.6|Intel(R) Xeon(R) Processor @ 2.80GHz"
GOLDEN = {
    "maps": {
        "patient01_lesion_conn-Golden_desc-AvgRFz_funcmap.nii.gz": "760e0870fd8971529b5cd118f737170f8c55221569b4be59565909358e3f8ec1",
        "patient01_lesion_conn-Golden_desc-AvgR_funcmap.nii.gz": "8284bbf2816f92071b99efa6b6c24c4e99685b020c16924e0cf4f722d4236342",
        "patient01_lesion_conn-Golden_desc-T_funcmap.nii.gz": "fa19a9836cca2c64e57c2aacf0ccdbc513c6b593fdaad17b881557696e22ad14",
        "qa.json": "1d56453f18ed0536516059534507dca6e924711472e467056079748af2c40b22",
        "sub-02_conn-Golden_desc-AvgRFz_funcmap.nii.gz": "b16a8e5b63db8f5146d31e0e4e125307edf9c93ab7dafc4d10dbd9ae39b92410",
        "sub-02_conn-Golden_desc-AvgR_funcmap.nii.gz": "6f5aa8273e33650ddee3bab681fdf33fa4b128db6062f26b1bb3a19100333cee",
        "sub-02_conn-Golden_desc-T_funcmap.nii.gz": "6383e30eca2f3af31973396d2ebb160db6ebb3acb188648a832db701ec6f04ef",
    },
    "null": {
        "qa.json": "b13246dc12dee64fa3a483a7f3154ac5ca9e1cb2fb0b7070438344e9ec0189ac",
        "sub-02_conn-Golden_desc-AvgRFz_funcmap.nii.gz": "08c20d6db359d362f454ddc7df86151cf4c27b01631755ae93bcaaad5905827b",
        "sub-02_conn-Golden_desc-AvgR_funcmap.nii.gz": "a0bb77154244b0677a6afa37a8320d44fd59e90a8c28d8998457952198648d81",
        "sub-02_conn-Golden_desc-RelocatedT_funcmap.nii.gz": "b4513455a76d528e426204549bb76ea29ea1a6adff17e7388aa90620d635f114",
        "sub-02_conn-Golden_desc-T_funcmap.nii.gz": "9ec8940a307d4456d40ba2d521e254d9c335984c6d02fe0302a5888caf83c438",
    },
    "sample": {
        "patient01_lesion_conn-Golden_desc-AvgRFzPreviewN3_funcmap.nii.gz": "dd414b7fdb4fcd990d57aaa78449fec35323fd70a76e1c23efb98000d97d9bda",
        "patient01_lesion_conn-Golden_desc-AvgRPreviewN3_funcmap.nii.gz": "ba42e7c86ec0eb4495681c065483523475a4145abe92dd9000b79a808221ab48",
        "patient01_lesion_conn-Golden_desc-TPreviewN3_funcmap.nii.gz": "c866fdb03c4a956baffa3b09ddfff63ea015746df58e8f49ab6a76cc8965a1d4",
        "qa.json": "64efde668e9c3452d60778205ba228711b641076a78cd91d97ca98de070c071b",
    },
}
# Files that only the v2 engine writes in the golden runs (--write-null also writes the null mean Fz maps)
GOLDEN_NEW_FILES = {"null": {"sub-02_conn-Golden_desc-RelocatedFz_funcmap.nii.gz"}}


def _platform_key():
    cpu = platform.processor()
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            cpu = next((line.split(":", 1)[1].strip() for line in f if line.startswith("model name")), cpu)
    except OSError:
        pass
    return f"{sys.platform}|{platform.machine()}|numpy {np.__version__}|{cpu}"


def _env():
    return dict(os.environ, PYTHONPATH=MODULE_DIR + os.pathsep + os.environ.get("PYTHONPATH", ""))


def _run_engine(*args, cwd=None):
    """(exit code, events, stderr) of the engine command line run as a module."""
    result = subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.engine", *args], check=False, capture_output=True, text=True, env=_env(), cwd=cwd, timeout=300)
    return result.returncode, [json.loads(line) for line in result.stdout.splitlines() if line.strip()], result.stderr


def _sphere_lesion(path, center_world, radius=3.0):
    """A sphere drawn on a 1 mm radiological grid, saved as NIfTI."""
    lesion_affine = np.diag([-1.0, 1.0, 1.0, 1.0])
    lesion_affine[:3, 3] = [25.0, -25.0, -25.0]
    grid = geometry.Grid((51, 51, 51), lesion_affine)
    world = grid.voxel_to_world(np.indices(grid.shape).reshape(3, -1).T)
    lesion = (np.linalg.norm(world - np.asarray(center_world), axis=1) <= radius).reshape(grid.shape)
    nifti.write_nifti(path, lesion.astype(np.uint8), lesion_affine)
    return path


def _golden_outputs(folder):
    """{run name: {file name: sha256}} of the engine command line outputs (maps and qa.json) on a fixed synthetic bundle."""
    meta = connectome.make_synthetic(os.path.join(folder, "golden.lnmc"), n_subjects=4, seed=7, name="Golden")
    lesion_a = _sphere_lesion(os.path.join(folder, "patient01_lesion.nii.gz"), meta["blobs"]["A"]["center_world"])
    lesion_b = _sphere_lesion(os.path.join(folder, "sub-02_space-MNI_desc-lesion_mask.nii.gz"), meta["blobs"]["B"]["center_world"], radius=4.0)
    runs = {
        "maps": ["--lesion", lesion_a, "--lesion", lesion_b, "--snapshot-every", "2"],
        "null": ["--lesion", lesion_b, "--null-relocations", "3", "--null-seed", "1", "--write-null"],
        "sample": ["--lesion", lesion_a, "--subjects", "3", "--random", "--seed", "2", "--null-relocations", "2", "--null-voxels", "300", "--desc-suffix", "PreviewN3"],
    }
    out = {}
    for name, args in runs.items():
        run_dir = os.path.join(folder, name)
        code, events, stderr = _run_engine("--bundle", meta["path"], "--out", run_dir, "--threads", "1", *args, cwd=folder)
        if code != 0:
            raise RuntimeError(f"{events} {stderr}")
        files = {}
        for file_name in sorted(os.listdir(run_dir)):
            with open(os.path.join(run_dir, file_name), "rb") as f:
                files[file_name] = hashlib.sha256(f.read()).hexdigest()
        out[name] = files
    return out


def _raw_subjects(rng, n_voxels, n_subjects, runs, groups):
    """Raw time series (baselines, noise and one shared signal per voxel group) of n_subjects subjects."""
    subjects = []
    for _ in range(n_subjects):
        subject = []
        for length in runs:
            latent = rng.standard_normal((4, length))
            loadings = rng.uniform(-0.9, 0.9, size=(n_voxels, 4)) * (groups[:, np.newaxis] == np.arange(4))
            subject.append(50.0 + rng.uniform(-5, 5, size=(n_voxels, 1)) + loadings @ latent + rng.standard_normal((n_voxels, length)))
        subjects.append(subject)
    return subjects


def _write_bundle(path, raw_subjects, mask, dtype="float32"):
    writer = connectome.ConnectomeWriter(path, mask, AFFINE, name="EngineV2", dtype=dtype, overwrite=True)
    for index, runs in enumerate(raw_subjects):
        writer.add_subject(f"s{index:02d}", runs)
    return connectome.ConnectomeBundle.open(writer.finalize())


def _pearson(a, b):
    """Pearson r between the rows of a (m, T) and the rows of b (n, T): (m, n)."""
    a = a - a.mean(axis=1, keepdims=True)
    b = b - b.mean(axis=1, keepdims=True)
    a /= np.linalg.norm(a, axis=1, keepdims=True)
    b /= np.linalg.norm(b, axis=1, keepdims=True)
    return a @ b.T


def _fz(r):
    return np.arctanh(np.clip(r, -engine.RMAX, engine.RMAX))


class _TempDirTestCase(unittest.TestCase):
    def setUp(self):
        jobs.reset_cancel()
        self.tmp = tempfile.mkdtemp(prefix="lnm-engine-v2-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)


class SeedsAndAccumulatorTest(_TempDirTestCase):
    """In-process numerics on a float32 bundle written from known raw time series."""

    def setUp(self):
        super().setUp()
        self.rng = np.random.default_rng(16)
        self.mask = np.zeros((7, 8, 9), dtype=bool)
        self.mask[1:6, 1:7, 1:8] = True
        self.n_voxels = int(self.mask.sum())
        self.groups = self.rng.integers(0, 5, self.n_voxels)  # 4: no shared signal
        self.runs = (40, 35)
        self.raw = _raw_subjects(self.rng, self.n_voxels, 6, self.runs, self.groups)
        self.bundle = _write_bundle(self.path("v2.lnmc"), self.raw, self.mask)
        self.n_labels = 7
        labels = self.rng.integers(0, self.n_labels - 1, self.n_voxels)  # label 6 has no voxel
        labels[self.rng.choice(self.n_voxels, 20, replace=False)] = -1  # voxels outside every parcel
        self.labels = labels

    def tearDown(self):
        self.bundle.close()
        super().tearDown()

    def one_hot(self):
        weights = np.zeros((self.n_labels, self.n_voxels), dtype=np.float32)
        inside = self.labels >= 0
        weights[self.labels[inside], np.flatnonzero(inside)] = 1.0
        return weights

    def test_label_seeds_equal_dense_weights(self):
        seeds = engine._LabelSeeds(self.labels, self.n_labels)
        summed = engine._LabelSeeds(self.labels, self.n_labels)
        summed.MAX_GEMM_LABELS = 0  # every chunk sums its labels with np.add.reduceat
        dense = engine._Seeds(self.one_hot())
        np.testing.assert_array_equal(seeds.counts, self.one_hot().sum(axis=1))
        for subject in self.bundle.iter_subjects(prefetch=False):
            for chunk in (7, 64, engine.DEFAULT_CHUNK):  # label groups split across chunks
                expected = dense.matrix(subject.data, subject.norms, subject.runs, chunk)
                for label_seeds in (seeds, summed):
                    labelled = label_seeds.matrix(subject.data, subject.norms, subject.runs, chunk)
                    np.testing.assert_allclose(labelled, expected, rtol=0, atol=1e-6)
                    self.assertFalse(np.any(labelled[:, self.n_labels - 1]))  # a label without voxels is a zero seed
        by_labels = engine.compute_network_maps(self.bundle, None, labels=self.labels, n_labels=self.n_labels, chunk=50)
        by_weights = engine.compute_network_maps(self.bundle, self.one_hot(), chunk=50)
        for key in ("avg_r", "avg_fz", "t"):
            np.testing.assert_allclose(getattr(by_labels, key), getattr(by_weights, key), rtol=1e-5, atol=1e-5)
        self.assertEqual(by_labels.c_fz.shape, (self.n_labels, self.n_labels))
        self.assertIsNone(by_weights.c_fz)
        # The default number of labels, and invalid labels
        self.assertEqual(engine._LabelSeeds(self.labels).n_lesions, self.n_labels - 1)
        with self.assertRaises(ValueError):
            engine._LabelSeeds(self.labels.astype(np.float64))
        with self.assertRaises(ValueError):
            engine._LabelSeeds(self.labels, n_labels=3)
        with self.assertRaises(ValueError):
            engine.compute_network_maps(self.bundle, self.one_hot(), labels=self.labels)
        with self.assertRaises(ValueError):
            engine.compute_network_maps(self.bundle, None, labels=self.labels[:-1])

    def test_mean_mode_equals_welford_mean(self):
        weights = self.one_hot()[:4] * self.rng.uniform(0.2, 1.0, (4, self.n_voxels)).astype(np.float32)
        welford = engine.compute_network_maps(self.bundle, weights, chunk=33)
        snapshots = []
        mean = engine.compute_network_maps(self.bundle, weights, chunk=33, accumulate="mean", snapshot_every=2, snapshot=snapshots.append)
        np.testing.assert_allclose(mean.avg_fz, welford.avg_fz, rtol=0, atol=1e-6)
        np.testing.assert_allclose(mean.avg_r, welford.avg_r, rtol=0, atol=1e-6)
        self.assertIsNone(mean.t)
        self.assertEqual((mean.n, mean.available()), (6, ["AvgR", "AvgFz"]))
        self.assertEqual(welford.available(), ["AvgR", "AvgFz", "T"])
        self.assertIsNone(mean[0]["T"])
        # Snapshots are valid means of the subjects so far
        self.assertEqual([snapshot.n for snapshot in snapshots], [2, 4])
        np.testing.assert_allclose(snapshots[0].avg_fz, engine.compute_network_maps(self.bundle, weights, subjects=2).avg_fz, rtol=0, atol=1e-6)
        # Without r: Fz only (4 * V * L bytes)
        fz_only = engine.compute_network_maps(self.bundle, weights, accumulate="mean", keep_r=False)
        self.assertIsNone(fz_only.avg_r)
        np.testing.assert_allclose(fz_only.avg_fz, welford.avg_fz, rtol=0, atol=1e-6)
        accumulator = engine._Accumulator(self.n_voxels, 4, 16, keep_r=False, mode="mean")
        self.assertEqual((accumulator.sum_z.dtype, accumulator.sum_z.nbytes, accumulator.mean_z, accumulator.m2), (np.float32, 4 * self.n_voxels * 4, None, None))
        with self.assertRaises(ValueError):
            engine._Accumulator(self.n_voxels, 4, 16, mode="median")

    def test_parcel_matrix_and_maps_against_brute_force(self):
        maps = engine.compute_network_maps(self.bundle, None, labels=self.labels, n_labels=self.n_labels - 1, accumulate="mean", keep_r=False, chunk=40)
        members = np.stack([self.labels == label for label in range(self.n_labels - 1)]).astype(np.float64)
        c_sum = np.zeros((self.n_labels - 1,) * 2)
        fz_sum = np.zeros((self.n_labels - 1, self.n_voxels))
        for raw_runs in self.raw:
            parcel_runs = [members @ run for run in raw_runs]  # parcel time courses: sums of their voxels
            c_sum += _fz(np.mean([_pearson(p, p) for p in parcel_runs], axis=0))
            fz_sum += _fz(np.mean([_pearson(p, run) for p, run in zip(parcel_runs, raw_runs, strict=True)], axis=0))
        np.testing.assert_allclose(maps.c_fz, c_sum / len(self.raw), rtol=0, atol=1e-5)
        np.testing.assert_allclose(np.diag(maps.c_fz), np.arctanh(engine.RMAX))  # r = 1 is clipped to RMAX
        np.testing.assert_allclose(maps.avg_fz, fz_sum / len(self.raw), rtol=0, atol=1e-5)

    def test_split_half_sums_add_to_the_total(self):
        weights = self.one_hot()[:3]
        ids = self.bundle.subject_ids
        for mode in engine.ACCUMULATE_MODES:
            with self.subTest(accumulate=mode):
                maps = engine.compute_network_maps(self.bundle, weights, accumulate=mode, split_half=True)
                self.assertEqual((maps.n_odd, maps.n_even), (3, 3))
                np.testing.assert_allclose(maps.fz_odd * maps.n_odd + maps.fz_even * maps.n_even, maps.avg_fz * maps.n, rtol=0, atol=1e-5)
                # The odd half is the 1st, 3rd and 5th subject; the even half the others
                np.testing.assert_allclose(maps.fz_odd, engine.compute_network_maps(self.bundle, weights, subjects=ids[0::2]).avg_fz, rtol=0, atol=1e-6)
                np.testing.assert_allclose(maps.fz_even, engine.compute_network_maps(self.bundle, weights, subjects=ids[1::2]).avg_fz, rtol=0, atol=1e-5)
                expected = [float(np.corrcoef(odd, even)[0, 1]) for odd, even in zip(maps.fz_odd, maps.fz_even, strict=True)]
                np.testing.assert_allclose(engine.split_half_correlation(maps), expected, rtol=0, atol=1e-6)
        # The accumulator's sums: odd + even = total, in float64
        accumulator = engine._Accumulator(self.n_voxels, 3, 64, split_half=True)
        seeds = engine._Seeds(weights)
        z_sum = np.zeros((self.n_voxels, 3))
        for index, subject in enumerate(self.bundle.iter_subjects(ids[:5], prefetch=False)):
            matrix = seeds.matrix(subject.data, subject.norms, subject.runs)
            accumulator.add_subject(subject.data, matrix, 64)
            if index % 2 == 0:
                z_sum += np.arctanh(np.clip(subject.data.astype(np.float32) @ matrix, -engine.RMAX, engine.RMAX))
        total, odd = accumulator.split_sums()
        self.assertEqual((accumulator.n_odd, total.dtype, odd.dtype), (3, np.float64, np.float64))
        np.testing.assert_allclose(odd, z_sum, rtol=0, atol=1e-5)
        maps = accumulator.maps(ids[:5])
        np.testing.assert_allclose(maps.fz_odd.T * 3 + maps.fz_even.T * 2, total, rtol=0, atol=1e-4)
        # One subject: the even half is empty and has no correlation
        single = engine.compute_network_maps(self.bundle, weights, subjects=1, split_half=True)
        self.assertEqual((single.n_odd, single.n_even, engine.split_half_correlation(single)), (1, 0, [None, None, None]))
        self.assertIsNone(engine.split_half_correlation(engine.compute_network_maps(self.bundle, weights, subjects=2)))

    def test_null_fz_maps(self):
        weights = self.one_hot()[:1]
        nulls = self.one_hot()[1:4]
        maps = engine.compute_network_maps(self.bundle, weights, null_weights=nulls, null_fz=True)
        mapped = engine.compute_network_maps(self.bundle, nulls)
        self.assertEqual(maps.null_fz.shape, (3, self.n_voxels))
        np.testing.assert_allclose(maps.null_fz, mapped.avg_fz, rtol=0, atol=1e-5)
        np.testing.assert_allclose(maps.null_t, mapped.t, rtol=1e-4, atol=1e-4)
        self.assertIsNone(engine.compute_network_maps(self.bundle, weights, null_weights=nulls).null_fz)
        voxels = np.arange(0, self.n_voxels, 3)
        sampled = engine.compute_network_maps(self.bundle, weights, null_weights=nulls, null_voxels=voxels, null_fz=True)
        np.testing.assert_allclose(sampled.null_fz, mapped.avg_fz[:, voxels], rtol=0, atol=1e-5)

    def test_benchmark_modes(self):
        for kwargs in ({"accumulate": "mean"}, {"accumulate": "mean", "parcels": 12}):
            result = engine.benchmark(V=3000, T=40, L=3, n_subjects=2, **kwargs)
            self.assertGreater(result["per_subject_best_s"], 0.0)
            self.assertEqual(result["accumulate"], "mean")


class CommandLineV2Test(_TempDirTestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = tempfile.mkdtemp(prefix="lnm-engine-v2-data-")
        cls.meta = connectome.make_synthetic(os.path.join(cls.data, "syn.lnmc"), n_subjects=5, name="Synthetic", seed=3, dtype="float32")
        cls.bundle = connectome.ConnectomeBundle.open(cls.meta["path"])
        cls.lesion = _sphere_lesion(os.path.join(cls.data, "sub-01_lesion.nii.gz"), cls.meta["blobs"]["A"]["center_world"])

    @classmethod
    def tearDownClass(cls):
        cls.bundle.close()
        shutil.rmtree(cls.data, ignore_errors=True)

    def engine(self, *args):
        return _run_engine("--bundle", self.meta["path"], "--threads", "1", *args, cwd=self.tmp)

    def test_job_protocol_volume_and_cancel_file(self):
        out = self.path("out")
        cancel_file = self.path("stop")
        code, events, stderr = self.engine("--lesion", self.lesion, "--out", out, "--run-json", self.path("run.json"), "--cancel-file", cancel_file)
        self.assertEqual(code, 0, stderr)
        self.assertTrue(all("t" in event for event in events))
        self.assertEqual([event["t"] for event in events], sorted(event["t"] for event in events))
        start, done = events[0], events[-1]
        self.assertEqual((start["event"], start["job"], done["event"]), ("start", "engine", "done"))
        self.assertIsInstance(start["pid"], int)
        expected_ml = round(start["qc"][0]["volume_mm3"] / 1000.0, 4)
        self.assertGreater(expected_ml, 0.0)
        self.assertEqual(start["qc"][0]["volume_ml"], expected_ml)
        self.assertEqual(done["qc"][0]["volume_ml"], expected_ml)
        with open(self.path("run.json"), encoding="utf-8") as f:
            run = json.load(f)
        self.assertEqual(run["lesions"][0]["volume_ml"], expected_ml)
        self.assertNotIn("--cancel-file", run["argv"])  # it controls this process, not the result
        self.assertNotIn(cancel_file, run["argv"])
        # Stop: the cancel file exists
        open(cancel_file, "w").close()
        code, events, _ = self.engine("--lesion", self.lesion, "--out", self.path("stopped"), "--cancel-file", cancel_file)
        self.assertEqual(code, jobs.EXIT_CANCELLED)
        self.assertEqual((events[-1]["event"], events[-1]["reason"]), ("cancelled", "cancel file"))
        self.assertFalse(any(name.endswith(".nii.gz") for name in os.listdir(self.path("stopped"))))
        # Usage errors are JSON error events (exit code 2)
        for args in (["--out", out], ["--lesion", self.lesion, "--parcels", self.lesion, "--out", out], ["--lesion", self.lesion, "--out", out, "--threads", "0"], ["--lesion", self.lesion, "--out", out, "--accumulate", "median"]):
            with self.subTest(args=args):
                code, events, _ = self.engine(*args)
                self.assertEqual((code, [event["event"] for event in events], events[0]["code"]), (2, ["error"], "usage"))

    def test_mean_split_half_and_null_fz_outputs(self):
        code, events, stderr = self.engine("--lesion", self.lesion, "--out", self.path("welford"), "--null-relocations", "3", "--write-null")
        self.assertEqual(code, 0, stderr)
        welford = events[-1]
        code, events, stderr = self.engine("--lesion", self.lesion, "--out", self.path("mean"), "--accumulate", "mean", "--split-half", "--null-relocations", "3", "--write-null")
        self.assertEqual(code, 0, stderr)
        done = events[-1]
        # The mean accumulator writes AvgR and AvgRFz (no T), equal to the Welford ones
        self.assertEqual([os.path.basename(p) for p in done["outputs"]], [f"sub-01_conn-Synthetic_desc-{desc}_funcmap.nii.gz" for desc in ("AvgR", "AvgRFz")])
        for mean_path, welford_path in zip(done["outputs"], welford["outputs"][:2], strict=True):
            np.testing.assert_allclose(nifti.read_nifti(mean_path)[0], nifti.read_nifti(welford_path)[0], rtol=0, atol=1e-6)
        with open(done["qa_json"], encoding="utf-8") as f:
            qa_json = json.load(f)
        self.assertEqual(qa_json["statistic"], "AvgFz")
        # Split half: two maps and their spatial r, labelled
        split = done["split_half"]
        self.assertEqual((split["label"], split["n_odd"], split["n_even"]), ("connectome sampling stability", 3, 2))
        self.assertEqual([os.path.basename(p) for p in split["outputs"]], [f"sub-01_conn-Synthetic_desc-{desc}_funcmap.nii.gz" for desc in ("AvgFzOddHalf", "AvgFzEvenHalf")])
        odd, even = (nifti.read_nifti(p)[0][self.bundle.mask] for p in split["outputs"])
        self.assertAlmostEqual(split["r"]["sub-01"], float(np.corrcoef(odd, even)[0, 1]), places=5)
        self.assertGreater(split["r"]["sub-01"], 0.5)
        total = nifti.read_nifti(done["outputs"][1])[0][self.bundle.mask]
        np.testing.assert_allclose(3 * odd + 2 * even, 5 * total, rtol=0, atol=1e-4)
        self.assertEqual(qa_json["split_half"], {key: split[key] for key in ("label", "n_odd", "n_even", "r")})
        # --write-null: the 4D T maps (as before) and the 4D mean Fz maps, on all voxels
        for run in (welford, done):
            self.assertEqual([os.path.basename(p) for p in run["null_outputs"]], ["sub-01_conn-Synthetic_desc-RelocatedT_funcmap.nii.gz"])
            self.assertEqual([os.path.basename(p) for p in run["null_fz_outputs"]], ["sub-01_conn-Synthetic_desc-RelocatedFz_funcmap.nii.gz"])
            null_fz, affine, _ = nifti.read_nifti(run["null_fz_outputs"][0])
            self.assertEqual(null_fz.shape, (*self.bundle.shape, 3))
            np.testing.assert_array_equal(affine, self.bundle.affine)
            self.assertFalse(np.any(null_fz[~self.bundle.mask]))
        # The null Fz maps are the mean Fz maps of the relocated lesions
        weights, _ = engine.prepare_lesion(self.lesion, self.bundle)
        relocated = qa.relocated_lesions(weights, self.bundle, k=3, seed=0, sparse=True)
        dense = np.zeros((3, self.bundle.n_voxels), np.float32)
        for row, (positions, values) in enumerate(relocated):
            dense[row, positions] = values
        expected = engine.compute_network_maps(self.bundle, dense).avg_fz
        null_fz = nifti.read_nifti(welford["null_fz_outputs"][0])[0]
        np.testing.assert_allclose(np.moveaxis(null_fz, -1, 0)[:, self.bundle.mask], expected, rtol=0, atol=1e-5)

    def test_reference_in_qa_json(self):
        # No reference maps: no reference keys
        code, events, stderr = self.engine("--lesion", self.lesion, "--out", self.path("plain"), "--null-relocations", "4", "--null-voxels", "500")
        self.assertEqual(code, 0, stderr)
        with open(events[-1]["qa_json"], encoding="utf-8") as f:
            plain = json.load(f)
        self.assertNotIn("reference", plain)
        self.assertNotIn("reference", plain["lesions"]["sub-01"])
        self.assertNotIn("reference", events[-1])
        # A synthetic reference built for a copy of the bundle
        bundle_path = self.path("ref.lnmc")
        shutil.copytree(self.meta["path"], bundle_path)
        built = connectome.ConnectomeBundle.open(bundle_path)
        try:
            reference.build(built, n_seeds=40, k=3, power_iters=2, register=False, emit=lambda *args, **kwargs: None)
            loaded = qa.load_reference(built)
        finally:
            built.close()
        code, events, stderr = _run_engine("--bundle", bundle_path, "--threads", "1", "--lesion", self.lesion, "--out", self.path("ref"), "--null-relocations", "4", "--null-voxels", "500", cwd=self.tmp)
        self.assertEqual(code, 0, stderr)
        done = events[-1]
        with open(done["qa_json"], encoding="utf-8") as f:
            qa_json = json.load(f)
        self.assertEqual(qa_json["reference"], {"definition": reference.DEFINITION, "sha256": loaded.sha256, "subjects_sha256": loaded.subjects_sha256, "run_subjects": True})
        self.assertEqual(done["reference"], qa_json["reference"])
        values = qa_json["lesions"]["sub-01"]
        self.assertEqual(set(values), {"degree_r", "degree_rho", "degree_level", "null", "null_level", "reference"})
        self.assertEqual(set(values["reference"]), REFERENCE_QA_KEYS)
        self.assertEqual((values["reference"]["k"], values["reference"]["null_kind"]), (4, "relocated"))
        # The calibrated similarity uses the null voxel sample (the reference restricted to the null voxels)
        sample = engine.null_voxel_sample(self.bundle.n_voxels, 500, seed=0)
        t_map = nifti.read_nifti(done["outputs"][2])[0][self.bundle.mask]
        self.assertAlmostEqual(values["reference"]["mu_r_sample"], float(np.corrcoef(t_map[sample], loaded.maps["mu_r"][sample])[0, 1]), places=5)
        self.assertAlmostEqual(values["reference"]["mu_r"], float(np.corrcoef(t_map, loaded.maps["mu_r"])[0, 1]), places=5)
        # The other subjects' run: the bundle's default reference, marked as built from other subjects
        code, events, stderr = _run_engine("--bundle", bundle_path, "--threads", "1", "--lesion", self.lesion, "--out", self.path("subset"), "--subjects", "3", cwd=self.tmp)
        self.assertEqual(code, 0, stderr)
        with open(events[-1]["qa_json"], encoding="utf-8") as f:
            self.assertEqual(json.load(f)["reference"]["run_subjects"], False)

    def test_parcel_run(self):
        labels = np.zeros(self.bundle.shape, dtype=np.int16)
        ijk = np.indices(self.bundle.shape)
        labels[...] = 10 + 3 * (ijk[0] // 7) + (ijk[1] // 12)
        labels[ijk[2] < 3] = 0
        labels[0, 0, 19] = 99  # a label only outside the connectome mask
        parcels_path = self.path("parcels.nii.gz")
        nifti.write_nifti(parcels_path, labels, self.bundle.affine)
        out = self.path("parcels")
        code, events, stderr = self.engine("--parcels", parcels_path, "--out", out, "--run-json", self.path("parcels.json"))
        self.assertEqual(code, 0, stderr)
        kinds = [event["event"] for event in events]
        self.assertEqual((kinds[0], kinds[-1], kinds.count("progress")), ("warning", "done", 5))  # warnings about the inputs come first, as for lesions
        start = events[kinds.index("start")]
        self.assertEqual((start["lesions"], start["parcels"]), ([], len(set(labels[self.bundle.mask].tolist()) - {0})))
        warnings = [event for event in events if event["event"] == "warning"]
        self.assertEqual([event["code"] for event in warnings], ["parcels_outside"])
        done = events[-1]
        self.assertEqual([os.path.basename(p) for p in done["outputs"]], [engine.PARCEL_FZ_NAME, engine.PARCEL_C_NAME, engine.PARCEL_LABELS_NAME])
        with open(os.path.join(out, engine.PARCEL_LABELS_NAME), encoding="utf-8") as f:
            info = json.load(f)
        vector = labels[self.bundle.mask]
        values = sorted(set(vector.tolist()) - {0})
        self.assertEqual(info["labels"], values)
        self.assertEqual(info["voxels"], [int(np.count_nonzero(vector == value)) for value in values])
        self.assertEqual(info["missing"], [99])
        self.assertAlmostEqual(info["coverage"], np.count_nonzero(vector) / vector.size)
        self.assertEqual((info["n"], info["n_parcels"], info["source"]["file"]), (5, len(values), "parcels.nii.gz"))
        self.assertNotIn(self.tmp, json.dumps(info))
        # The files equal the in-process maps of the label seeds
        index = np.full(vector.shape, -1)
        for row, value in enumerate(values):
            index[vector == value] = row
        maps = engine.compute_network_maps(self.bundle, None, labels=index, accumulate="mean", keep_r=False)
        fz = np.load(os.path.join(out, engine.PARCEL_FZ_NAME))
        self.assertEqual((fz.dtype, fz.shape), (np.float16, (len(values), self.bundle.n_voxels)))
        np.testing.assert_allclose(fz.astype(np.float32), maps.avg_fz, rtol=2e-3, atol=2e-3)
        c_fz = np.load(os.path.join(out, engine.PARCEL_C_NAME))
        self.assertEqual((c_fz.dtype, c_fz.shape), (np.float64, (len(values), len(values))))
        np.testing.assert_allclose(c_fz, maps.c_fz, rtol=0, atol=1e-6)
        np.testing.assert_allclose(c_fz, c_fz.T, rtol=0, atol=1e-6)
        with open(os.path.join(out, engine.PARCEL_FZ_NAME), "rb") as f:
            self.assertEqual(info["files"]["fz"]["sha256"], hashlib.sha256(f.read()).hexdigest())
        with open(self.path("parcels.json"), encoding="utf-8") as f:
            run = json.load(f)
        self.assertEqual((run["status"], run["accumulate"], run["parcels"]["n"]), ("done", "mean", len(values)))
        # A label image on another grid is resampled by nearest neighbour; invalid label images are refused
        fine_affine = np.diag([1.0, 1.0, 1.0, 1.0])
        fine_affine[:3, 3] = self.bundle.affine[:3, 3] - 0.5
        fine = np.repeat(np.repeat(np.repeat(labels, 2, axis=0), 2, axis=1), 2, axis=2)
        nifti.write_nifti(self.path("fine.nii.gz"), fine, fine_affine)
        resampled = engine.parcel_labels(self.path("fine.nii.gz"), self.bundle)
        np.testing.assert_array_equal(resampled["labels"], index)
        for name, data in (("float.nii.gz", labels + 0.5), ("negative.nii.gz", labels - 20), ("empty.nii.gz", np.where(self.bundle.mask, 0, 7))):
            nifti.write_nifti(self.path(name), data.astype(np.float32), self.bundle.affine)
            with self.subTest(image=name):
                code, events, _ = self.engine("--parcels", self.path(name), "--out", self.path("bad"))
                self.assertEqual((code, events[-1]["event"]), (1, "error"))
        for option in (["--split-half"], ["--null-relocations", "2"], ["--snapshot-every", "2"], ["--accumulate", "welford"]):
            with self.subTest(option=option):
                code, events, _ = self.engine("--parcels", parcels_path, "--out", self.path("bad"), *option)
                self.assertEqual((code, events[-1]["code"]), (2, "usage"))


class GoldenOutputsTest(_TempDirTestCase):
    def test_outputs_byte_identical_to_the_engine_before_v2(self):
        outputs = _golden_outputs(self.tmp)
        self.assertEqual(set(outputs), set(GOLDEN))
        for name, files in GOLDEN.items():
            self.assertEqual(set(outputs[name]) - set(files), GOLDEN_NEW_FILES.get(name, set()), name)
            self.assertEqual(set(files) - set(outputs[name]), set(), name)
        if _platform_key() != GOLDEN_PLATFORM:
            self.skipTest(f"golden sha256 were recorded on {GOLDEN_PLATFORM}, not {_platform_key()}")
        for name, files in GOLDEN.items():
            for file_name, sha in files.items():
                self.assertEqual(outputs[name][file_name], sha, f"{name}/{file_name}")


class CohortLesionPathsTest(_TempDirTestCase):
    """Cohort-map files store lesion paths relative to themselves (SPEC 9.7 rule 10) and still read old absolute ones."""

    def test_relative_lesion_paths(self):
        meta = connectome.make_synthetic(self.path("c.lnmc"), n_subjects=3, name="Cohort", seed=4)
        bundle = connectome.ConnectomeBundle.open(meta["path"])
        os.makedirs(self.path("data"))
        good = _sphere_lesion(self.path("data", "sub-01_lesion.nii.gz"), meta["blobs"]["A"]["center_world"])
        broken = self.path("data", "sub-02_lesion.nii.gz")
        with open(broken, "wb") as f:
            f.write(b"not a nifti file")
        out = self.path("study", "derivatives", "lnm", "cohort")
        record = cohortmaps.run([("sub-01", good), ("sub-02", broken)], bundle, out, cache_root=self.path("cache"), emit=lambda *args, **kwargs: None, register=False)
        # maps.tsv: relative to its folder; read_maps_tsv returns the absolute path
        with open(os.path.join(out, cohortmaps.MAPS_TSV), encoding="utf-8") as f:
            header, row = (line.rstrip("\n").split("\t") for line in f.readlines()[:2])
        stored = dict(zip(header, row, strict=True))["lesion"]
        self.assertEqual(stored, "../../../../data/sub-01_lesion.nii.gz")
        self.assertEqual(cohortmaps.read_maps_tsv(os.path.join(out, cohortmaps.MAPS_TSV))[0]["lesion"], good)
        self.assertEqual(cohortmaps.read_maps_tsv(os.path.join(out, cohortmaps.MAPS_TSV), resolve_paths=False)[0]["lesion"], stored)
        # entry.json: relative to the entry folder; read_entry returns the absolute path
        entry = cohortmaps.read_entry(record["cache_dir"], cohortmaps.read_maps_tsv(os.path.join(out, cohortmaps.MAPS_TSV))[0]["key"])
        with open(os.path.join(entry["dir"], cohortmaps.ENTRY_JSON), encoding="utf-8") as f:
            raw_entry = json.load(f)
        self.assertFalse(os.path.isabs(raw_entry["lesion"]))
        self.assertEqual(os.path.normpath(os.path.join(entry["dir"], raw_entry["lesion"])), good)
        self.assertEqual(entry["lesion"], good)
        self.assertEqual(entry["qc"]["volume_ml"], round(entry["qc"]["volume_mm3"] / 1000.0, 4))
        # cohortmaps.json: the excluded file relative to the run folder (the returned record keeps the absolute path)
        with open(os.path.join(out, cohortmaps.COHORTMAPS_JSON), encoding="utf-8") as f:
            stored_record = json.load(f)
        detail = stored_record["excluded_details"]["sub-02"]
        self.assertEqual(detail["path"], "../../../../data/sub-02_lesion.nii.gz")
        self.assertNotIn(self.tmp, json.dumps(stored_record["excluded_details"]))
        self.assertEqual(record["excluded_details"]["sub-02"]["path"], broken)
        # Files written before this rule hold absolute paths: they still resolve
        self.assertEqual(cohortmaps.resolve_lesion_path(good, self.path("elsewhere")), good)
        self.assertEqual(cohortmaps.resolve_lesion_path("../data/sub-01_lesion.nii.gz", self.path("study")), good)
        self.assertIsNone(cohortmaps.resolve_lesion_path("n/a", out))
        with open(os.path.join(entry["dir"], cohortmaps.ENTRY_JSON), "w", encoding="utf-8") as f:
            json.dump({**raw_entry, "lesion": good}, f)
        self.assertEqual(cohortmaps.read_entry(record["cache_dir"], entry["key"])["lesion"], good)
        self.assertEqual(cohortmaps.portable_lesion_path(good, self.path("data")), "sub-01_lesion.nii.gz")
        self.assertIsNone(cohortmaps.portable_lesion_path(None, out))
        bundle.close()


if __name__ == "__main__":
    unittest.main()

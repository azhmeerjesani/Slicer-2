"""Tests of the cohort map orchestration (cohortmaps.py): batched engine passes under a memory budget,
the per-lesion map cache, maps.tsv, review gating, inbox posting, Stop and resume, and the CLI.

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingCohortMapsTest.py -v
"""

import contextlib
import gzip
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

from LesionNetworkMappingLib import cohortmaps, connectome, engine, export, jobs, nifti, runs
from LesionNetworkMappingLib.study import EXCLUDED_REVIEW, Study, read_tsv

N_LESIONS = 7
CENTERS = ((-8, -6, 0), (8, 6, 0), (0, 0, -6), (-6, 8, 4), (6, -8, -4), (0, 10, 2), (-10, 0, -2))


def _env():
    return dict(os.environ, PYTHONPATH=MODULE_DIR + os.pathsep + os.environ.get("PYTHONPATH", ""))


def _sphere_lesion(path, center_world, radius=3.5):
    """A binary sphere on a 1 mm grid (not the connectome grid, so the lesions are resampled)."""
    affine = np.diag([-1.0, 1.0, 1.0, 1.0])
    affine[:3, 3] = [20.0, -24.0, -20.0]
    shape = (41, 49, 41)
    ijk = np.indices(shape).reshape(3, -1).T
    world = ijk @ affine[:3, :3].T + affine[:3, 3]
    lesion = (np.linalg.norm(world - np.asarray(center_world, dtype=float), axis=1) <= radius).reshape(shape)
    nifti.write_nifti(path, lesion.astype(np.uint8), affine)
    return path


class _Counter:
    """Wraps engine.compute_network_maps and counts the passes and the lesion columns computed."""

    def __init__(self):
        self.passes = []
        self._original = engine.compute_network_maps

    def __call__(self, bundle, weights, *args, **kwargs):
        self.passes.append(np.atleast_2d(weights).shape[0])
        return self._original(bundle, weights, *args, **kwargs)

    @property
    def lesions(self):
        return sum(self.passes)


@contextlib.contextmanager
def counting_engine():
    counter = _Counter()
    engine.compute_network_maps = counter
    try:
        yield counter
    finally:
        engine.compute_network_maps = counter._original


class _Events:
    """Collects the events of a run (the ``emit`` callback); optional hook(event, fields)."""

    def __init__(self, hook=None):
        self.events = []
        self.hook = hook

    def __call__(self, event, **fields):
        self.events.append({"event": event, **fields})
        if self.hook:
            self.hook(event, fields)

    def of(self, kind):
        return [event for event in self.events if event["event"] == kind]


class CohortMapsTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = tempfile.mkdtemp(prefix="lnm-cohortmaps-data-")
        cls.meta = connectome.make_synthetic(os.path.join(cls.data, "synthetic.lnmc"), n_subjects=5, runs=(20, 20), seed=4)
        cls.bundle = connectome.ConnectomeBundle.open(cls.meta["path"])
        cls.lesion_paths = []
        for index, center in enumerate(CENTERS):
            cls.lesion_paths.append(_sphere_lesion(os.path.join(cls.data, f"sub-{index + 1:02d}_lesion.nii.gz"), center))
        cls.sids = [f"sub-{index + 1:02d}" for index in range(N_LESIONS)]
        # A budget that allows exactly 3 lesions per engine pass
        cls.budget3 = 3 * cohortmaps.lesion_pass_bytes(cls.bundle.n_voxels) + 100

    @classmethod
    def tearDownClass(cls):
        cls.bundle.close()
        shutil.rmtree(cls.data, ignore_errors=True)

    def setUp(self):
        jobs.reset_cancel()
        self.tmp = tempfile.mkdtemp(prefix="lnm-cohortmaps-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def lesions(self, count=N_LESIONS):
        return list(zip(self.sids[:count], self.lesion_paths[:count], strict=True))

    def run_cohort(self, out="out", cache=None, events=None, **kwargs):
        kwargs.setdefault("budget_bytes", self.budget3)
        return cohortmaps.run(kwargs.pop("lesions", None) or self.lesions(), self.bundle, self.path(out), cache_root=cache or self.path("cache"), emit=events or _Events(), **kwargs)


class BatchPlanTest(unittest.TestCase):
    def test_plan_batches(self):
        v = 228_483
        # The addendum's 36 bytes per voxel and lesion plus the engine's measured temporaries
        self.assertGreaterEqual(cohortmaps.BYTES_PER_LESION_VOXEL, 36)
        self.assertEqual(cohortmaps.BYTES_PER_LESION_VOXEL, 68)
        per_lesion = cohortmaps.BYTES_PER_LESION_VOXEL * v
        self.assertEqual(cohortmaps.lesion_pass_bytes(v), per_lesion)
        self.assertEqual(cohortmaps.plan_batches(7, 1000, 3 * cohortmaps.BYTES_PER_LESION_VOXEL * 1000), [(0, 3), (3, 6), (6, 7)])
        self.assertEqual(cohortmaps.batch_size(v, 10 * per_lesion - 1), 9)
        self.assertEqual(cohortmaps.batch_size(v, 1), 1)  # clamped to at least 1
        self.assertEqual(cohortmaps.batch_size(v, 10**15), 100)  # and at most 100
        self.assertEqual(cohortmaps.plan_batches(0, v, per_lesion), [])
        # 4 GB at the GSP1000 grid: 276 lesions by memory, 100 by the cap
        self.assertEqual(cohortmaps.batch_size(v, 4 * 1024**3), 100)
        self.assertEqual(cohortmaps.batch_size(v, 300 * 1024**2), 20)
        # Relocation nulls add 12 * K * V' bytes per lesion
        self.assertEqual(cohortmaps.lesion_pass_bytes(v, null_k=50, null_voxels=30_000), per_lesion + 12 * 50 * 30_000)
        self.assertEqual(cohortmaps.batch_size(v, 1024**3, null_k=50, null_voxels=30_000), 1024**3 // (per_lesion + 18_000_000))
        self.assertEqual(cohortmaps.batch_size(v, 1024**3, null_k=50, null_voxels=30_000), 32)

    def test_default_budget(self):
        total = cohortmaps.physical_memory_bytes()
        budget = cohortmaps.default_budget_bytes()
        self.assertLessEqual(budget, 4 * 1024**3)
        if total:
            self.assertEqual(budget, int(min(0.25 * total, 4 * 1024**3)))
        else:
            self.assertEqual(budget, cohortmaps.FALLBACK_BUDGET)

    def test_cache_key_changes_exactly_with_its_inputs(self):
        base = ("a" * 64, "b" * 64, "c" * 64, 2, False)
        key = cohortmaps.cache_key(*base)
        self.assertEqual(key, cohortmaps.cache_key(*base))
        self.assertRegex(key, r"^[0-9a-f]{64}$")
        for index, other in enumerate(("d" * 64, "e" * 64, "f" * 64, 3, True)):
            changed = list(base)
            changed[index] = other
            self.assertNotEqual(cohortmaps.cache_key(*changed), key, f"field {index}")
        self.assertEqual(cohortmaps.cache_key_fields(*base)["engine"]["library"], export.LIBRARY_VERSION)
        self.assertEqual(cohortmaps.cache_key_fields(*base)["engine"]["outputs"], [list(pair) for pair in engine.OUTPUT_KEYS])
        self.assertEqual(cohortmaps.cache_folder_name("GSP 1000 (MGH)", "0123456789abcdef"), "conn-GSP1000MGH_subj-01234567")


class CohortMapsTest(CohortMapsTestBase):
    def _single_pass(self, subjects=None):
        weights = np.stack([engine.prepare_lesion(path, self.bundle)[0] for path in self.lesion_paths])
        return engine.compute_network_maps(self.bundle, weights, subjects=subjects)

    def _read_masked(self, path):
        data = nifti.read_nifti(path)[0]
        return data.reshape(-1)[self.bundle.mask_indices]

    def assert_maps_equal(self, row, reference, index):
        """Maps of a maps.tsv row equal the reference maps of lesion ``index``: AvgR and AvgFz within 1e-6;
        T within 1e-5 of its largest magnitude. Float32 GEMMs of different widths (3 vs 7 lesion columns)
        round differently and T = mean / standard error amplifies that where Fz barely varies over subjects.
        """
        for key, attribute in (("AvgR", "avg_r"), ("AvgFz", "avg_fz"), ("T", "t")):
            expected = getattr(reference, attribute)[index]
            tolerance = 1e-5 * float(np.abs(expected).max()) if key == "T" else 1e-6
            np.testing.assert_allclose(self._read_masked(row[key]), expected, atol=tolerance, rtol=0, err_msg=f"{row['participant_id']} {key}")

    def test_batched_maps_equal_single_pass(self):
        events = _Events()
        with counting_engine() as counter:
            record = self.run_cohort(events=events)
        self.assertEqual(counter.passes, [3, 3, 1])
        self.assertEqual(record["batches"], 3)
        stages = events.of("stage")
        self.assertEqual([(stage["index"], stage["total"]) for stage in stages], [(1, 3), (2, 3), (3, 3)])
        self.assertEqual(record["lesions"]["computed"], N_LESIONS)
        reference = self._single_pass()
        rows = cohortmaps.read_maps_tsv(self.path("out", "maps.tsv"))
        self.assertEqual([row["participant_id"] for row in rows], self.sids)
        for index, row in enumerate(rows):
            self.assert_maps_equal(row, reference, index)
        # Bit for bit what the engine computes for the same batch (the cache adds no rounding)
        weights = np.stack([engine.prepare_lesion(path, self.bundle)[0] for path in self.lesion_paths[3:6]])
        second_batch = engine.compute_network_maps(self.bundle, weights)
        for index, row in enumerate(rows[3:6]):
            for key, attribute in (("AvgR", "avg_r"), ("AvgFz", "avg_fz"), ("T", "t")):
                np.testing.assert_array_equal(self._read_masked(row[key]), getattr(second_batch, attribute)[index])
        # Exact connectome header, Lead-DBS names, T with its degrees of freedom
        header = nifti.read_nifti_header(rows[0]["T"])
        np.testing.assert_allclose(header["affine"], self.bundle.affine, atol=1e-5)
        self.assertEqual(tuple(header["shape"]), tuple(self.bundle.shape))
        self.assertEqual(header["intent_code"], 3)
        self.assertEqual(header["intent_params"][0], self.bundle.n_subjects - 1)
        self.assertRegex(os.path.basename(rows[0]["AvgFz"]), r"^lesion-[0-9a-f]{12}_conn-Synthetic_desc-AvgRFz_funcmap\.nii\.gz$")
        # Progress is scaled to the whole job and reaches it
        progress = events.of("progress")
        self.assertEqual(progress[-1]["done"], progress[-1]["total"])
        self.assertEqual(progress[-1]["total"], 3 * self.bundle.n_subjects)
        self.assertEqual([event["done"] for event in progress], sorted(event["done"] for event in progress))
        # Cache entry layout
        entry = cohortmaps.read_entry(record["cache_dir"], rows[0]["key"])
        self.assertEqual(set(os.listdir(entry["dir"])), {os.path.basename(entry["maps"][key]) for key in cohortmaps.MAP_KEYS} | {"weights.npz", "entry.json"})
        self.assertEqual(entry["key_fields"]["lesion_sha256"], export.file_sha256(self.lesion_paths[0]))
        self.assertGreater(entry["qc"]["volume_mm3"], 0)
        indices, values, n_voxels = cohortmaps.read_weights(entry)
        self.assertEqual((indices.dtype, values.dtype, n_voxels), (np.dtype(np.int32), np.dtype(np.float32), self.bundle.n_voxels))
        np.testing.assert_array_equal(cohortmaps.dense_weights(entry, self.bundle.n_voxels), engine.prepare_lesion(self.lesion_paths[0], self.bundle)[0])
        self.assertEqual(os.path.dirname(entry["dir"]), cohortmaps.cache_dir_for(self.path("cache"), self.bundle.name, export.subjects_sha256(self.bundle.subject_ids)))
        # No temporary folders are left in the cache
        self.assertEqual([name for name in os.listdir(record["cache_dir"]) if name.startswith(".")], [])

    def test_second_run_hits_cache(self):
        first = self.run_cohort(out="first")
        events = _Events()
        with counting_engine() as counter:
            second = self.run_cohort(out="second", events=events)
        self.assertEqual(counter.passes, [])
        self.assertEqual(second["batches"], 0)
        self.assertEqual(second["lesions"]["cache_hits"], N_LESIONS)
        self.assertEqual(second["lesions"]["computed"], 0)
        rows_a = cohortmaps.read_maps_tsv(self.path("first", "maps.tsv"))
        rows_b = cohortmaps.read_maps_tsv(self.path("second", "maps.tsv"))
        self.assertEqual([row["key"] for row in rows_a], [row["key"] for row in rows_b])
        self.assertEqual([row["T"] for row in rows_a], [row["T"] for row in rows_b])
        self.assertEqual({row["cache"] for row in rows_b}, {"hit"})
        self.assertEqual(first["qa"], second["qa"])
        # The no-compute lookup reports the same thing
        plan = cohortmaps.lookup(self.lesions(), self.bundle, cache_root=self.path("cache"), budget_bytes=self.budget3)
        self.assertEqual((plan["n"], plan["hits"], plan["new"], plan["passes"]), (N_LESIONS, N_LESIONS, 0, 0))

    def test_different_subject_list_gives_different_key(self):
        full = self.run_cohort(out="full")
        with counting_engine() as counter:
            subset = self.run_cohort(out="subset", subject_list="sub-001,sub-002,sub-003")
        self.assertEqual(counter.lesions, N_LESIONS)  # nothing reused across subject lists
        self.assertNotEqual(full["cache_dir"], subset["cache_dir"])
        keys_full = [row["key"] for row in cohortmaps.read_maps_tsv(self.path("full", "maps.tsv"))]
        keys_subset = [row["key"] for row in cohortmaps.read_maps_tsv(self.path("subset", "maps.tsv"))]
        self.assertTrue(set(keys_full).isdisjoint(keys_subset))
        self.assertEqual(subset["n"], 3)
        self.assertEqual(subset["subjects"]["list_source"], "ids")
        self.assert_maps_equal(cohortmaps.read_maps_tsv(self.path("subset", "maps.tsv"))[2], self._single_pass(["sub-001", "sub-002", "sub-003"]), 2)
        self.assertTrue(export.check_poolable([full, subset]))  # different subject lists
        # Other resampling settings also give other keys
        plan = cohortmaps.lookup(self.lesions(), self.bundle, cache_root=self.path("cache"), supersample=3)
        self.assertEqual(plan["hits"], 0)
        plan = cohortmaps.lookup(self.lesions(), self.bundle, cache_root=self.path("cache"), probabilistic=True)
        self.assertEqual(plan["hits"], 0)

    def test_deleted_cache_file_triggers_recomputation(self):
        self.run_cohort(out="first")
        rows = cohortmaps.read_maps_tsv(self.path("first", "maps.tsv"))
        os.remove(rows[4]["AvgR"])
        truncated = rows[5]["T"]
        with open(truncated, "r+b") as f:
            f.truncate(10)
        os.remove(os.path.join(os.path.dirname(rows[6]["T"]), "weights.npz"))
        plan = cohortmaps.lookup(self.lesions(), self.bundle, cache_root=self.path("cache"), budget_bytes=self.budget3)
        self.assertEqual(plan["new"], 3)
        self.assertEqual([item["participant_id"] for item in plan["lesions"] if not item["hit"]], ["sub-05", "sub-06", "sub-07"])
        with counting_engine() as counter:
            record = self.run_cohort(out="second")
        self.assertEqual(counter.passes, [3])
        self.assertEqual(record["lesions"]["computed"], 3)
        rows_after = cohortmaps.read_maps_tsv(self.path("second", "maps.tsv"))
        self.assertEqual([row["key"] for row in rows_after], [row["key"] for row in rows])
        for row in rows_after:
            self.assertIsNotNone(cohortmaps.read_entry(os.path.dirname(os.path.dirname(row["T"])), row["key"]))
        self.assert_maps_equal(rows_after[4], self._single_pass(), 4)

    def test_maps_tsv_columns(self):
        record = self.run_cohort()
        columns, rows = read_tsv(self.path("out", "maps.tsv"))
        self.assertEqual(tuple(columns), cohortmaps.MAPS_COLUMNS)
        for required in ("participant_id", "lesion", "lesion_sha256", "key", "AvgR", "AvgFz", "T", "volume_ml", "voxels_in_mask", "fractional_sum", "manifest_sha256", "subject_ids_sha256", "statistic", "supersample", "probabilistic", "library_version"):
            self.assertIn(required, columns)
        self.assertEqual(len(rows), N_LESIONS)
        key = export.compatibility_key(record)
        for index, row in enumerate(rows):
            self.assertEqual(row["lesion"], os.path.abspath(self.lesion_paths[index]))
            self.assertEqual(row["lesion_sha256"], export.file_sha256(self.lesion_paths[index]))
            for name in ("AvgR", "AvgFz", "T"):
                self.assertTrue(os.path.isabs(row[name]) and os.path.isfile(row[name]))
            self.assertGreater(float(row["volume_ml"]), 0.1)  # a 3.5 mm sphere is about 0.18 ml
            self.assertLess(float(row["volume_ml"]), 0.25)
            self.assertGreater(int(row["voxels_in_mask"]), 0)
            self.assertGreater(float(row["fractional_sum"]), 0)
            self.assertEqual((row["manifest_sha256"], row["subject_ids_sha256"], row["statistic"], int(row["supersample"]), row["probabilistic"], row["library_version"]), (key[0], key[1], key[2], key[3], "false", key[5]))
            self.assertEqual(row["null_k"], "n/a")
            self.assertIn(row["degree_level"], ("neutral", "green", "amber", "red"))
        # cohortmaps.json is a run record for the pooling guard
        with open(self.path("out", "cohortmaps.json"), encoding="utf-8") as f:
            stored = json.load(f)
        self.assertEqual(stored["format"], "slicer-lnm-cohortmaps")
        self.assertEqual(stored["status"], "done")
        self.assertEqual(stored["subject_ids"], self.bundle.subject_ids)
        self.assertEqual(stored["cache_dir"], record["cache_dir"])
        self.assertEqual(export.check_poolable([stored, stored]), [])
        self.assertEqual(stored["compatibility_key"], list(key))
        self.assertEqual(stored["bundle"]["manifest_sha256"], export.file_sha256(os.path.join(self.bundle.path, "connectome.json")))
        # The run is registered
        registered = runs.load(self.path("out"))[stored["run_id"]]
        self.assertEqual((registered["kind"], registered["status"]), ("cohortmaps", "done"))

    def test_cancel_after_first_batch_then_resume(self):
        token = jobs.CancelToken(signals=False)

        def stop_at_second_batch(event, fields):
            if event == "stage" and fields["index"] == 2:
                token.cancel("test stop")

        events = _Events(stop_at_second_batch)
        with counting_engine() as counter, self.assertRaises(jobs.Cancelled) as raised:
            self.run_cohort(events=events, cancel=token)
        self.assertEqual(raised.exception.done, 3)
        self.assertEqual(counter.passes, [3])  # the second batch stopped before its pass
        self.assertFalse(os.path.exists(self.path("out", "maps.tsv")))
        with open(self.path("out", "cohortmaps.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["status"], "cancelled")
        state = jobs.ResumeState(self.path("out", cohortmaps.RESUME_NAME))
        self.assertEqual(len(state.done), 3)
        with counting_engine() as counter:
            record = self.run_cohort(resume=True, events=_Events())
        self.assertEqual(counter.passes, [3, 1])  # batch 1 is not recomputed
        self.assertEqual(record["lesions"]["cache_hits"], 3)
        self.assertEqual(record["lesions"]["computed"], 4)
        self.assertEqual(len(cohortmaps.read_maps_tsv(self.path("out", "maps.tsv"))), N_LESIONS)
        self.assertFalse(os.path.exists(self.path("out", cohortmaps.RESUME_NAME)))

    def test_cancel_during_a_pass_publishes_nothing_of_that_batch(self):
        token = jobs.CancelToken(signals=False)

        def stop_mid_pass(event, fields):
            if event == "progress" and fields["batch"] == 2 and fields["subject"] == 2:
                token.cancel("test stop")

        with self.assertRaises(jobs.Cancelled):
            self.run_cohort(events=_Events(stop_mid_pass), cancel=token)
        entries = [name for name in os.listdir(cohortmaps.cache_dir_for(self.path("cache"), self.bundle.name, export.subjects_sha256(self.bundle.subject_ids))) if not name.startswith(".")]
        self.assertEqual(len(entries), 3)

    def test_changed_and_missing_lesions_are_excluded(self):
        lesions = self.lesions(3)
        lesions[1] = (lesions[1][0], lesions[1][1], "0" * 64)  # expected sha256 of another file
        lesions.append(("sub-99", self.path("missing.nii.gz")))
        events = _Events()
        record = self.run_cohort(lesions=lesions, events=events)
        self.assertEqual(record["lesions"]["mapped"], 2)
        self.assertEqual(record["excluded"], {cohortmaps.EXCLUDED_SHA: ["sub-02"], cohortmaps.EXCLUDED_MISSING: ["sub-99"]})
        self.assertEqual(len([event for event in events.of("warning") if event.get("code") == "excluded"]), 2)

    def test_lesion_changed_during_the_run_is_excluded(self):
        # The lesion is hashed when the run starts and again from the bytes that are mapped
        copy = shutil.copyfile(self.lesion_paths[1], self.path("sub-02_copy.nii.gz"))

        def rewrite_after_start(event, fields):
            if event == "start":
                _sphere_lesion(copy, CENTERS[5])

        events = _Events(rewrite_after_start)
        record = self.run_cohort(lesions=[(self.sids[0], self.lesion_paths[0]), (self.sids[1], copy)], events=events)
        self.assertEqual(record["lesions"]["mapped"], 1)
        self.assertEqual(record["excluded"], {cohortmaps.EXCLUDED_CHANGED: ["sub-02"]})
        self.assertEqual([row["participant_id"] for row in cohortmaps.read_maps_tsv(self.path("out", "maps.tsv"))], ["sub-01"])
        self.assertEqual(len(os.listdir(record["cache_dir"])), 1)  # nothing cached under the old content's key

    def test_null_relocation_qa_is_added_without_touching_the_entry(self):
        self.run_cohort(out="plain")
        rows = cohortmaps.read_maps_tsv(self.path("plain", "maps.tsv"))
        entry_dir = os.path.dirname(rows[0]["T"])
        before = {name: os.stat(os.path.join(entry_dir, name)) for name in os.listdir(entry_dir)}
        with counting_engine() as counter:
            record = self.run_cohort(out="nulls", null_relocations=3, null_voxels=0, lesions=self.lesions(2), budget_bytes=10**9)
        self.assertEqual(counter.passes, [2])  # the relocations need one pass; the maps are not rewritten
        self.assertEqual(record["lesions"]["cache_hits"], 2)
        self.assertEqual(record["lesions"]["qa_computed"], 2)
        for name, stat in before.items():
            after = os.stat(os.path.join(entry_dir, name))
            self.assertEqual((after.st_ino, after.st_mtime_ns, after.st_size), (stat.st_ino, stat.st_mtime_ns, stat.st_size), name)
        self.assertEqual(len([name for name in os.listdir(entry_dir) if name.startswith("qa-")]), 1)
        rows = cohortmaps.read_maps_tsv(self.path("nulls", "maps.tsv"))
        self.assertEqual(rows[0]["null_k"], "3")
        self.assertNotEqual(rows[0]["null_r_mean_map"], "n/a")
        # The QA of these settings is now cached too
        with counting_engine() as counter:
            self.run_cohort(out="nulls2", null_relocations=3, null_voxels=0, lesions=self.lesions(2), budget_bytes=10**9)
        self.assertEqual(counter.passes, [])
        # Same relocation QA as the engine CLI's single-lesion computation
        weights = engine.prepare_lesion(self.lesion_paths[0], self.bundle)[0]
        from LesionNetworkMappingLib import qa

        relocated = qa.relocated_lesions(weights, self.bundle, k=3, seed=0, sparse=True)
        maps = engine.compute_network_maps(self.bundle, weights[np.newaxis], null_weights=relocated)
        expected = qa.lesion_qa(maps.t[0], None, maps.null_t, maps.null_voxels)["null"]["r_mean_map"]
        self.assertAlmostEqual(float(rows[0]["null_r_mean_map"]), expected, places=4)


    def _broken_lesions(self):
        """Lesion files that cannot be mapped: garbage bytes named .nii.gz, a 4D image, and two files with a
        valid header but truncated data (.nii and .nii.gz), which only fail when the data is parsed.
        """
        garbage = self.path("sub-90_lesion.nii.gz")
        with open(garbage, "wb") as f:
            f.write(b"not a nifti image" * 20)
        four_d = self.path("sub-91_lesion.nii.gz")
        data, affine, _ = nifti.read_nifti(self.lesion_paths[0])
        nifti.write_nifti(four_d, np.stack([data, data], axis=-1), affine)
        truncated = self.path("sub-92_lesion.nii")
        nifti.write_nifti(truncated, data, affine)
        with open(truncated, "r+b") as f:
            f.truncate(os.path.getsize(truncated) // 2)
        truncated_gz = self.path("sub-93_lesion.nii.gz")
        compressed = gzip.compress(nifti_bytes(data, affine, self.path("tmp.nii")))
        with open(truncated_gz, "wb") as f:
            f.write(compressed[: len(compressed) // 2])
        return [("sub-90", garbage), ("sub-91", four_d), ("sub-92", truncated), ("sub-93", truncated_gz)]

    def test_unreadable_lesions_are_excluded_and_the_cohort_continues(self):
        broken = self._broken_lesions()
        lesions = [*self.lesions(3), *broken]
        # The header check of the lookup (and of a dry run) finds the files that are not 3D NIfTI-1 images
        plan = cohortmaps.lookup(lesions, self.bundle, cache_root=self.path("cache"), budget_bytes=10**9)
        self.assertEqual(plan["excluded"], {cohortmaps.EXCLUDED_UNREADABLE: ["sub-90", "sub-91"]})
        self.assertEqual(plan["new"], 5)
        self.assertIn("3D", plan["excluded_details"]["sub-91"]["message"])
        events = _Events()
        with counting_engine() as counter:
            record = self.run_cohort(lesions=lesions, events=events, budget_bytes=10**9)
        self.assertEqual(counter.passes, [3])  # one batch: the truncated files are dropped, the rest is mapped
        self.assertEqual(record["lesions"]["mapped"], 3)
        self.assertEqual(record["excluded"], {cohortmaps.EXCLUDED_UNREADABLE: ["sub-90", "sub-91", "sub-92", "sub-93"]})
        self.assertEqual(record["lesions"]["requested"], record["lesions"]["mapped"] + record["excluded_n"])
        # Every unreadable file is named with its path and the error
        warnings = {event["participant_id"]: event for event in events.of("warning") if event.get("code") == "unreadable"}
        self.assertEqual(sorted(warnings), ["sub-90", "sub-91", "sub-92", "sub-93"])
        for sid, path in broken:
            self.assertEqual(warnings[sid]["path"], path)
            self.assertEqual(record["excluded_details"][sid]["path"], path)
            self.assertIn(path, warnings[sid]["message"])
        self.assertIn("Truncated", record["excluded_details"]["sub-92"]["message"])
        self.assertIn("EOFError", record["excluded_details"]["sub-93"]["message"])
        self.assertEqual([row["participant_id"] for row in cohortmaps.read_maps_tsv(self.path("out", "maps.tsv"))], self.sids[:3])
        self.assertEqual(len([name for name in os.listdir(record["cache_dir"]) if not name.startswith(".")]), 3)

    def test_identical_lesions_are_computed_once(self):
        copy = shutil.copyfile(self.lesion_paths[0], self.path("copy.nii.gz"))
        lesions = [*self.lesions(2), ("sub-99", copy)]
        events = _Events()
        with counting_engine() as counter:
            record = self.run_cohort(lesions=lesions, events=events)
        self.assertEqual(counter.passes, [2])
        self.assertEqual(events.of("start")[0]["to_compute"], 2)
        self.assertEqual((record["lesions"]["computed"], record["lesions"]["cache_hits"], record["lesions"]["mapped"]), (2, 1, 3))
        self.assertEqual(events.of("done")[0]["computed"], 2)
        rows = cohortmaps.read_maps_tsv(self.path("out", "maps.tsv"))
        self.assertEqual([row["cache"] for row in rows], ["new", "new", "hit"])
        self.assertEqual(rows[0]["key"], rows[2]["key"])

    def test_qa_that_cannot_be_cached_is_still_reported(self):
        # Entries created with relocation QA; later runs with other QA settings cannot add their QA file
        self.run_cohort(out="nulls2", lesions=self.lesions(2), null_relocations=2, null_voxels=0)
        original = cohortmaps._write_entry_qa

        def read_only_cache(*args, **kwargs):
            raise PermissionError(13, "Read-only cache")

        cohortmaps._write_entry_qa = read_only_cache
        try:
            runs_events = {}
            for out, options in (("plain", {}), ("nulls3", {"null_relocations": 3, "null_voxels": 0})):
                runs_events[out] = _Events()
                record = self.run_cohort(out=out, lesions=self.lesions(2), events=runs_events[out], **options)
                self.assertEqual(record["lesions"]["cache_hits"], 2)
        finally:
            cohortmaps._write_entry_qa = original
        for out in ("plain", "nulls3"):
            warnings = [event for event in runs_events[out].of("warning") if event.get("code") == "qa"]
            self.assertEqual([event["participant_id"] for event in warnings], self.sids[:2], out)
            self.assertIn("could not be saved", warnings[0]["message"])
        rows = {out: cohortmaps.read_maps_tsv(self.path(out, "maps.tsv")) for out in ("nulls2", "plain", "nulls3")}
        self.assertEqual([row["null_k"] for row in rows["nulls3"]], ["3", "3"])
        self.assertEqual([row["null_k"] for row in rows["plain"]], ["n/a", "n/a"])
        # Degree QA of hits comes from the cached T map: the same values as when the entries were created
        for out in ("plain", "nulls3"):
            self.assertEqual([(row["degree_r"], row["degree_level"]) for row in rows[out]], [(row["degree_r"], row["degree_level"]) for row in rows["nulls2"]], out)

    def test_stale_temporary_folders_are_swept(self):
        cache_dir = cohortmaps.cache_dir_for(self.path("cache"), self.bundle.name, export.subjects_sha256(self.bundle.subject_ids))
        for name in (".tmp-old", ".trash-old-0", ".tmp-recent"):
            os.makedirs(os.path.join(cache_dir, name, "inner"))
        old = os.path.getmtime(cache_dir) - 2 * cohortmaps.STALE_TMP_SECONDS
        for name in (".tmp-old", ".trash-old-0"):
            os.utime(os.path.join(cache_dir, name), (old, old))
        self.run_cohort(lesions=self.lesions(1))
        self.assertEqual(sorted(name for name in os.listdir(cache_dir) if name.startswith(".")), [".tmp-recent"])


def nifti_bytes(data, affine, scratch):
    """The bytes of an uncompressed NIfTI-1 file of ``data``."""
    nifti.write_nifti(scratch, data, affine)
    with open(scratch, "rb") as f:
        return f.read()


class StudyGatingTest(CohortMapsTestBase):
    def _study(self, approved=5, volumes=None):
        study = Study.create(self.path("study"), space="MNI152NLin6Asym")
        lesions_dir = self.path("study", "lesions")
        for index, sid in enumerate(self.sids):
            target = os.path.join(lesions_dir, f"{sid}_space-MNI152NLin6Asym_desc-lesion_mask.nii.gz")
            shutil.copyfile(self.lesion_paths[index], target)
            values = {"lesion": study.relpath(target), "review_status": "approved" if index < approved else "pending"}
            if volumes and sid in volumes:
                values["volume_ml"] = volumes[sid]
            study.add_subject(sid, values)
        study.review_required = True
        study.save()
        return Study.open(self.path("study"))

    def test_review_gating_counts(self):
        study = self._study(approved=5)
        lesions, excluded = cohortmaps.study_lesions(study)
        self.assertEqual([sid for sid, _, _ in lesions], self.sids[:5])
        self.assertEqual(excluded, {EXCLUDED_REVIEW: ["sub-06", "sub-07"]})
        events = _Events()
        record = cohortmaps.run(lesions, self.bundle, self.path("out"), study=study, excluded=excluded, budget_bytes=self.budget3, emit=events)
        self.assertEqual(record["lesions"]["mapped"], 5)
        self.assertEqual(record["excluded"], {EXCLUDED_REVIEW: ["sub-06", "sub-07"]})
        self.assertEqual(record["excluded_n"], 2)
        self.assertEqual(record["lesions"]["requested"], N_LESIONS)  # = mapped + excluded_n
        warnings = [event for event in events.of("warning") if event.get("code") == "excluded"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["participants"], ["sub-06", "sub-07"])
        # The default cache is in the study's derivatives, and the run is registered there
        self.assertTrue(record["cache_dir"].startswith(os.path.join(study.path, "derivatives", "lnm", "cache", "conn-Synthetic_subj-")))
        self.assertIn(record["run_id"], runs.load(study.path))
        # --include-unreviewed maps everyone
        lesions, excluded = cohortmaps.study_lesions(study, include_unreviewed=True)
        self.assertEqual((len(lesions), excluded), (N_LESIONS, {}))
        # A lesions TSV is gated by the same study rules; subjects unknown to the study are excluded when review is required
        rows = [*self.lesions(), ("sub-99", self.lesion_paths[0])]
        lesions, excluded = cohortmaps.study_lesions(study, lesions=rows)
        self.assertEqual([sid for sid, _, _ in lesions], self.sids[:5])
        self.assertEqual(excluded, {EXCLUDED_REVIEW: ["sub-06", "sub-07"], cohortmaps.EXCLUDED_NO_STUDY_ROW: ["sub-99"]})

    def test_missing_volume_posts_inbox_and_never_writes_participants(self):
        study = self._study(approved=N_LESIONS, volumes={"sub-01": 9.5, "sub-02": 7})
        tsv = os.path.join(study.path, "participants.tsv")
        with open(tsv, "rb") as f:
            before = f.read()
        lesions, excluded = cohortmaps.study_lesions(study)
        record = cohortmaps.run(lesions, self.bundle, self.path("out"), study=study, excluded=excluded, budget_bytes=self.budget3, emit=_Events())
        with open(tsv, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(len(record["inbox"]), 1)
        with open(record["inbox"][0], encoding="utf-8") as f:
            posted = json.load(f)
        self.assertEqual(posted["source"], "cohortmaps")
        self.assertEqual(sorted(posted["rows"]), self.sids[2:])  # sub-01 and sub-02 already had a volume
        self.assertAlmostEqual(float(posted["rows"]["sub-03"]["volume_ml"]), float(cohortmaps.read_maps_tsv(self.path("out", "maps.tsv"))[2]["volume_ml"]), places=4)
        # The session merges it
        session = Study.open(study.path)
        session.apply_inbox()
        self.assertGreater(session.value("sub-03", "volume_ml", float), 0)
        self.assertEqual(session.value("sub-01", "volume_ml", float), 9.5)
        # Probabilistic volumes (weighted sums) are never posted as the study's volume_ml
        study = Study.open(self.path("study"))
        lesions, _ = cohortmaps.study_lesions(study)
        record = cohortmaps.run(lesions[:2], self.bundle, self.path("prob"), study=study, probabilistic=True, budget_bytes=self.budget3, emit=_Events())
        self.assertEqual(record["inbox"], [])


class CommandLineTest(CohortMapsTestBase):
    def _cli(self, *args, timeout=600):
        result = subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.cohortmaps", *args], check=False, capture_output=True, text=True, env=_env(), cwd=self.tmp, timeout=timeout)
        events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        return result.returncode, events, result.stderr

    def _lesions_tsv(self, rows):
        path = self.path("lesions.tsv")
        with open(path, "w", encoding="utf-8") as f:
            f.write("participant_id\tlesion\tlesion_sha256\n")
            for sid, lesion, sha in rows:
                f.write(f"{sid}\t{lesion}\t{sha}\n")
        return path

    def test_cli_batches_nulls_dry_run_and_cancel(self):
        rows = [(sid, path, export.file_sha256(path)) for sid, path in self.lesions(4)]
        rows[3] = (rows[3][0], rows[3][1], "f" * 64)  # changed since the snapshot: excluded
        tsv = self._lesions_tsv(rows)
        budget_gb = 2 * cohortmaps.lesion_pass_bytes(self.bundle.n_voxels, null_k=2, null_voxels=self.bundle.n_voxels) / 1024**3
        common = ["--lesions-tsv", tsv, "--bundle", self.meta["path"], "--cache", self.path("cache"), "--budget-gb", f"{budget_gb * 1.001:.9f}", "--null-relocations", "2", "--null-voxels", "0", "--threads", "1"]
        # Stop requested before anything is computed: exit 3
        cancel_file = self.path("STOP")
        open(cancel_file, "w").close()
        code, events, stderr = self._cli(*common, "--out", self.path("out"), "--cancel-file", cancel_file)
        self.assertEqual(code, 3, stderr)
        self.assertEqual(events[-1]["event"], "cancelled")
        os.remove(cancel_file)
        # Full run
        code, events, stderr = self._cli(*common, "--out", self.path("out"), "--cancel-file", cancel_file, "--resume", "--parent-run", "pipeline-x")
        self.assertEqual(code, 0, stderr + json.dumps(events[-3:]))
        start = next(event for event in events if event["event"] == "start")
        self.assertEqual((start["job"], start["lesions"], start["batches"], start["batch_size"]), ("cohortmaps", 3, 2, 2))
        self.assertIsInstance(start["pid"], int)
        self.assertEqual([event["index"] for event in events if event["event"] == "stage"], [1, 2])
        progress = [event for event in events if event["event"] == "progress"]
        self.assertEqual(progress[-1]["done"], 2 * self.bundle.n_subjects)
        warnings = [event for event in events if event["event"] == "warning" and event.get("code") == "excluded"]
        self.assertEqual(warnings[0]["participants"], ["sub-04"])
        done = events[-1]
        self.assertEqual((done["event"], done["n"], done["computed"]), ("done", 3, 3))
        maps = cohortmaps.read_maps_tsv(self.path("out", "maps.tsv"))
        self.assertEqual([row["null_k"] for row in maps], ["2", "2", "2"])
        with open(self.path("out", "cohortmaps.json"), encoding="utf-8") as f:
            stored = json.load(f)
        self.assertEqual(stored["parents"], ["pipeline-x"])
        self.assertEqual(runs.load(self.path("out"))[stored["run_id"]]["parents"], ["pipeline-x"])
        # Dry run: everything is in the cache now, nothing is computed or written
        code, events, stderr = self._cli(*common, "--out", self.path("dry"), "--dry-run")
        self.assertEqual(code, 0, stderr)
        plan = next(event for event in events if event["event"] == "result" and event["kind"] == "plan")
        self.assertEqual((plan["n"], plan["hits"], plan["new"], plan["qa_missing"], plan["passes"]), (3, 3, 0, 0, 0))
        self.assertEqual(plan["excluded"], {cohortmaps.EXCLUDED_SHA: ["sub-04"]})
        self.assertFalse(os.path.exists(self.path("dry")))

    def test_cli_unreadable_lesion_is_excluded(self):
        garbage = self.path("garbage.nii.gz")
        with open(garbage, "wb") as f:
            f.write(b"garbage" * 50)
        tsv = self._lesions_tsv([*((sid, path, "n/a") for sid, path in self.lesions(2)), ("sub-bad", garbage, "n/a")])
        code, events, stderr = self._cli("--lesions-tsv", tsv, "--bundle", self.meta["path"], "--out", self.path("out"), "--cache", self.path("cache"), "--threads", "1")
        self.assertEqual(code, 0, stderr + json.dumps(events[-3:]))
        unreadable = [event for event in events if event["event"] == "warning" and event.get("code") == "unreadable"]
        self.assertEqual([(event["participant_id"], event["path"]) for event in unreadable], [("sub-bad", garbage)])
        self.assertEqual((events[-1]["event"], events[-1]["n"]), ("done", 2))
        with open(self.path("out", "cohortmaps.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["excluded"], {cohortmaps.EXCLUDED_UNREADABLE: ["sub-bad"]})

    def test_cli_usage_errors(self):
        code, events, _ = self._cli("--bundle", self.meta["path"], "--out", self.path("out"))
        self.assertEqual((code, events[-1]["code"]), (2, "usage"))
        code, events, _ = self._cli("--study", self.path("none"), "--bundle", self.meta["path"], "--out", self.path("out"), "--subjects", "2", "--subject-list", "sub-001")
        self.assertEqual(code, 2)
        code, events, _ = self._cli("--study", self.path("none"), "--bundle", self.meta["path"], "--out", self.path("out"))
        self.assertEqual((code, events[-1]["event"]), (1, "error"))


if __name__ == "__main__":
    unittest.main()

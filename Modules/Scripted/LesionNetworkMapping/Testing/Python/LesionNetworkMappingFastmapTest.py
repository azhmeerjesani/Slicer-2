"""Tests of the fast (parcel) mode (fastmap.py, addendum §3.12) on a synthetic bundle and synthetic atlases.

The tiling must cover every mask voxel (atlas parcels first, by priority; majority resampling of a finer
atlas; supervoxels cut by the cell grid with small pieces merged); the index rows must equal the exact
parcel-seed maps of the engine (float16 tolerance) and C the brute-force seed-to-seed matrix; a lesion
equal to one parcel must give that parcel's row; the parity metrics are compared with brute force, and
the gate is checked per stratum and size bin, including failing strata and a shared-degree-only case.

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingFastmapTest.py -v
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

from LesionNetworkMappingLib import connectome, engine, fastmap, fetch, jobs, nifti, reference, runs
from LesionNetworkMappingLib.atlases import Atlas

SHAPE = (20, 24, 20)
RMAX = 0.999999
N_SUBJECTS = 5


def _env():
    return dict(os.environ, PYTHONPATH=MODULE_DIR + os.pathsep + os.environ.get("PYTHONPATH", ""), SLICER_LNM_OFFLINE="1")


def _quiet(*args, **kwargs):
    pass


def _atlases(bundle):
    """Synthetic tiling atlases on (or finer than) the bundle grid: cortex (shell, 16 parcels), subcortex (two blobs,
    one overlapping the cortex), cerebellum (1 mm grid, majority), and a Harvard-Oxford-like volume of which only
    the "Brain-Stem" label is used.
    """
    shape, affine = bundle.shape, bundle.affine
    ijk = np.indices(shape).reshape(3, -1).T
    centre = (np.array(shape) - 1) / 2.0
    radius = np.linalg.norm((ijk - centre) / (np.array(shape) / 2.2), axis=1)
    inside = bundle.mask.reshape(-1) > 0
    cortex = np.zeros(ijk.shape[0], np.int32)
    shell = inside & (radius > 0.72)
    angle = np.arctan2(ijk[:, 1] - centre[1], ijk[:, 0] - centre[0])
    cortex[shell] = (np.floor((angle[shell] + np.pi) / (2 * np.pi) * 8) % 8 + 1 + 8 * (ijk[shell, 2] > centre[2])).astype(np.int32)
    cortex_names = {value: f"7Networks_{'LH' if value <= 8 else 'RH'}_{['Vis', 'SomMot', 'DorsAttn', 'SalVentAttn', 'Limbic', 'Cont', 'Default', 'Vis'][(value - 1) % 8]}_{value}" for value in range(1, 17)}
    sub = np.zeros(ijk.shape[0], np.int32)
    sub[np.linalg.norm(ijk - (centre + (3, 0, 0)), axis=1) <= 2.2] = 1
    sub[np.linalg.norm(ijk - np.array((centre[0] - 3, centre[1], centre[2] + 6.5)), axis=1) <= 2.5] = 2  # reaches into the shell
    # Cerebellum on a 1 mm grid aligned with the 2 mm grid (each 2 mm voxel holds 2 x 2 x 2 voxels)
    fine_shape = tuple(2 * n for n in shape)
    fine_affine = np.diag([1.0, 1.0, 1.0, 1.0])
    fine_affine[:3, 3] = affine[:3, 3] - 0.5
    fine = np.indices(fine_shape).reshape(3, -1).T
    fine_world = fine * 1.0 + fine_affine[:3, 3]
    world_centre = affine[:3, :3] @ centre + affine[:3, 3]
    rel = fine_world - world_centre
    cerebellum = np.zeros(fine.shape[0], np.int32)
    region = (np.linalg.norm(rel - (0, -8, -9), axis=1) <= 7.3)
    cerebellum[region & (rel[:, 0] < 0.3)] = 1
    cerebellum[region & (rel[:, 0] >= 0.3)] = 2
    brainstem = np.zeros(ijk.shape[0], np.int32)
    brainstem[np.linalg.norm(ijk - (centre + (0, 2, -3)), axis=1) <= 1.8] = 8
    brainstem[np.linalg.norm(ijk - (centre + (0, -3, 3)), axis=1) <= 1.5] = 6
    return [
        {"atlas": Atlas("cortex", cortex.reshape(shape), affine, cortex_names, networks="schaefer"), "stratum": "cortical"},
        {"atlas": Atlas("subcortex", sub.reshape(shape), affine, {1: "THA-lh", 2: "CAU-rh"}, networks="tian"), "stratum": "subcortical", "resample": "nearest"},
        {"atlas": Atlas("cerebellum", cerebellum.reshape(fine_shape), fine_affine, {1: "7Networks_1", 2: "7Networks_2"}, networks="yeo7"), "stratum": "cerebellar", "resample": "majority"},
        {"atlas": Atlas("hospa", brainstem.reshape(shape), affine, {6: "Left Pallidum", 8: "Brain-Stem"}), "stratum": "brainstem", "labels": ["Brain-Stem"], "network": "Brainstem"},
    ]


def _brute_c(bundle, labels, n_parcels):
    """Brute-force C: mean over subjects of atanh(mean over runs of the Pearson r between parcel seed time courses)."""
    total = np.zeros((n_parcels, n_parcels))
    for subject_id in bundle.subject_ids:
        subject = bundle.load_subject(subject_id)
        data = np.asarray(subject.data, dtype=np.float64)
        bounds = np.cumsum((0, *subject.runs))
        r_sum = np.zeros((n_parcels, n_parcels))
        for run in range(len(subject.runs)):
            segment = data[:, bounds[run]:bounds[run + 1]] * subject.norms[run][:, np.newaxis]
            seeds = np.zeros((n_parcels, segment.shape[1]))
            np.add.at(seeds, labels, segment)
            seeds -= seeds.mean(axis=1, keepdims=True)
            norms = np.linalg.norm(seeds, axis=1)
            seeds = np.divide(seeds, norms[:, np.newaxis], out=np.zeros_like(seeds), where=norms[:, np.newaxis] > 0)
            r_sum += seeds @ seeds.T
        total += np.arctanh(np.clip(r_sum / len(subject.runs), -RMAX, RMAX))
    return total / bundle.n_subjects


def _sphere(bundle, centre_ijk, radius):
    ijk = np.stack(np.unravel_index(bundle.mask_indices, bundle.shape), axis=1)
    return (np.linalg.norm(ijk - np.asarray(centre_ijk), axis=1) <= radius).astype(np.float32)


class FastmapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lnm-fastmap-")
        info = connectome.make_synthetic(os.path.join(cls.tmp, "Synthetic.lnmc"), shape=SHAPE, n_subjects=N_SUBJECTS, runs=(30, 30), seed=3)
        connectome.set_metadata(info["path"], licence="GSP-DUT-2014")
        cls.bundle = connectome.ConnectomeBundle.open(info["path"])
        cls.entries = _atlases(cls.bundle)
        cls.tiling = fastmap.build_tiling(cls.bundle, cls.entries)
        cls.index_dir = os.path.join(cls.tmp, "index")
        cls.index = fastmap.build_index(cls.bundle, cls.tiling, out_dir=cls.index_dir, emit=_quiet, run_root=cls.tmp)

    @classmethod
    def tearDownClass(cls):
        cls.bundle.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # Tiling

    def test_tiling_covers_every_mask_voxel(self):
        tiling = self.tiling
        self.assertEqual(tiling.n_voxels, self.bundle.n_voxels)
        self.assertTrue(np.all((tiling.labels >= 0) & (tiling.labels < tiling.n_parcels)))
        counts = np.bincount(tiling.labels, minlength=tiling.n_parcels)
        self.assertTrue(np.all(counts > 0), "every parcel has voxels")
        self.assertEqual([row["voxels"] for row in tiling.table], counts.tolist())
        self.assertEqual([row["id"] for row in tiling.table], list(range(1, tiling.n_parcels + 1)))
        volume = tiling.volume()
        np.testing.assert_array_equal(volume.reshape(-1) > 0, self.bundle.mask.reshape(-1) > 0)
        self.assertEqual(set(tiling.sources), {"cortex", "subcortex", "cerebellum", "hospa", "supervoxel"})
        self.assertGreater(tiling.stats["n_supervoxels"], 0)
        self.assertAlmostEqual(tiling.stats["atlas_fraction"] + tiling.stats["supervoxel_fraction"], 1.0)
        # Strata, networks and the label selection
        for row in tiling.table:
            expected = {"cortex": "cortical", "subcortex": "subcortical", "cerebellum": "cerebellar", "hospa": "brainstem", "supervoxel": "white-matter-supervoxel"}[row["source"]]
            self.assertEqual(row["stratum"], expected)
        hospa = [row for row in tiling.table if row["source"] == "hospa"]
        self.assertEqual([(row["name"], row["atlas_value"], row["network"]) for row in hospa], [("Brain-Stem", 8, "Brainstem")])
        self.assertEqual({row["network"] for row in tiling.table if row["source"] == "cortex"}, {"Vis", "SomMot", "DorsAttn", "SalVentAttn", "Limbic", "Cont", "Default"})
        self.assertEqual({row["network"] for row in tiling.table if row["source"] == "cerebellum"}, {"Vis", "SomMot"})

    def test_priority_and_supervoxels(self):
        tiling, bundle = self.tiling, self.bundle
        cortex = self.entries[0]["atlas"].data.reshape(-1)[bundle.mask_indices]
        sources = tiling.sources[tiling.labels]
        # Every voxel of the first atlas keeps its cortex parcel (the overlapping subcortex blob loses there)
        self.assertTrue(np.all(sources[cortex > 0] == "cortex"))
        values = np.array([row["atlas_value"] or 0 for row in tiling.table])[tiling.labels]
        np.testing.assert_array_equal(values[cortex > 0], cortex[cortex > 0])
        sub = self.entries[1]["atlas"].data.reshape(-1)[bundle.mask_indices]
        self.assertTrue(np.any((sub == 2) & (cortex > 0)), "the fixture has an overlap")
        self.assertTrue(np.all(sources[(sub > 0) & (cortex == 0)] == "subcortex"))
        # The unselected Harvard-Oxford label is covered by supervoxels
        hospa = self.entries[3]["atlas"].data.reshape(-1)[bundle.mask_indices]
        self.assertTrue(np.all(sources[(hospa == 6) & (cortex == 0) & (sub == 0)] == "supervoxel"))
        # Supervoxels: only uncovered voxels; no piece under min_piece unless isolated; each spans one or a few cells
        supervoxel = tiling.is_supervoxel
        stats = tiling.stats["supervoxels"]
        sizes = np.array([row["voxels"] for row in tiling.table])[supervoxel]
        self.assertTrue(np.all(sizes >= fastmap.DEFAULT_MIN_PIECE) or stats["isolated_small"] > 0)
        self.assertEqual(stats["cell_voxels"], [5, 5, 5])
        self.assertLessEqual(sizes.max(), 125 + fastmap.DEFAULT_MIN_PIECE * stats["merged_small"])
        ijk = np.stack(np.unravel_index(bundle.mask_indices, bundle.shape), axis=1)
        for parcel in np.flatnonzero(supervoxel)[:20]:
            cells = {tuple(c) for c in ijk[tiling.labels == parcel] // 5}
            self.assertLessEqual(len(cells), 1 + stats["merged_small"])

    def test_majority_resampling_matches_brute_force(self):
        entry = self.entries[2]
        atlas = entry["atlas"]
        grid, indices = self.bundle.grid, self.bundle.mask_indices
        got = fastmap._atlas_on_grid(atlas, grid, indices, "majority", np.array([1, 2]))
        blocks = atlas.data.reshape(SHAPE[0], 2, SHAPE[1], 2, SHAPE[2], 2).transpose(0, 2, 4, 1, 3, 5).reshape(-1, 8)
        counts = np.stack([(blocks == value).sum(axis=1) for value in (1, 2)], axis=1)
        best = counts.argmax(axis=1)
        top = counts.max(axis=1)
        background = 8 - counts.sum(axis=1)
        expected = np.where(top > background, best + 1, 0)[indices]
        np.testing.assert_array_equal(got, expected)
        self.assertGreater(np.count_nonzero(expected), 10)
        # The tiling uses exactly these labels where no earlier atlas claimed the voxel
        cerebellum = np.array([row["atlas_value"] if row["source"] == "cerebellum" else 0 for row in self.tiling.table])[self.tiling.labels]
        claimed = np.isin(self.tiling.sources[self.tiling.labels], ["cortex", "subcortex"])
        np.testing.assert_array_equal(cerebellum[~claimed], expected[~claimed])

    def test_tiling_round_trip(self):
        folder = os.path.join(self.tmp, "tiling")
        self.tiling.write(folder)
        loaded = fastmap.Tiling.load(folder)
        np.testing.assert_array_equal(loaded.labels, self.tiling.labels)
        np.testing.assert_array_equal(loaded.mask_indices, self.bundle.mask_indices)
        self.assertTrue(loaded.grid.matches(self.bundle.grid))
        self.assertEqual([[row[key] for key in fastmap.TSV_COLUMNS] for row in loaded.table], [[row[key] for key in fastmap.TSV_COLUMNS] for row in self.tiling.table])
        with open(os.path.join(folder, fastmap.LABELS_TSV), encoding="utf-8") as f:
            header = f.readline().rstrip("\n").split("\t")
            supervoxel_row = next(line for line in f if "\tsupervoxel\t" in line).rstrip("\n").split("\t")
        self.assertEqual(header, list(fastmap.TSV_COLUMNS))
        self.assertEqual(supervoxel_row[3], "n/a")
        header_info = nifti.read_nifti_header(os.path.join(folder, fastmap.LABELS_NII))
        self.assertLess(nifti.xform_disagreement(header_info), 1e-3)
        # tiling.json keeps the atlas records, statistics and parameters (base names only)
        self.assertEqual(loaded.atlases, self.tiling.atlases)
        self.assertEqual(loaded.params, self.tiling.params)
        self.assertEqual(loaded.stats, json.loads(json.dumps(self.tiling.stats)))
        with open(os.path.join(folder, fastmap.TILING_JSON), encoding="utf-8") as f:
            self.assertNotIn(self.tmp, f.read())
        # Without tiling.json the coverage statistics are recomputed from the table, and the missing atlas records noted
        os.remove(os.path.join(folder, fastmap.TILING_JSON))
        bare = fastmap.Tiling.load(folder)
        self.assertEqual(bare.atlases, [])
        self.assertIn("atlases_unknown", [notice["code"] for notice in bare.notices])
        for key in ("atlas_fraction", "supervoxel_fraction", "n_supervoxels", "n_atlas_parcels"):
            self.assertAlmostEqual(bare.stats[key], self.tiling.stats[key])
        for source, fraction in self.tiling.stats["by_source"].items():
            self.assertAlmostEqual(bare.stats["by_source"][source], fraction)
        # The atlas records of an index folder come from its index.json
        from_index = fastmap.Tiling.load(self.index_dir)
        self.assertEqual(from_index.atlases, json.loads(json.dumps(self.tiling.atlases)))
        self.assertAlmostEqual(from_index.stats["atlas_fraction"], self.tiling.stats["atlas_fraction"])
        self.assertFalse(os.path.exists(os.path.join(self.index_dir, fastmap.TILING_JSON)), "an index keeps its tiling record in index.json")

    def test_missing_atlases_are_never_downloaded(self):
        empty = os.path.join(self.tmp, "empty-cache")
        os.makedirs(empty, exist_ok=True)
        with self.assertRaises(fetch.NotCachedError) as context:
            fastmap.build_tiling(self.bundle, cache=empty)
        for entry in fastmap.DEFAULT_TILING_ATLASES:
            self.assertIn(entry["id"], str(context.exception))
        # The optional AAN entry is left out with a notice instead
        tiling = fastmap.build_tiling(self.bundle, [*self.entries, {**fastmap.AAN_ENTRY}], cache=empty)
        self.assertEqual(tiling.n_parcels, self.tiling.n_parcels)
        self.assertEqual(tiling.notices[0]["code"], "optional_missing")
        with self.assertRaises(ValueError):
            fastmap.build_tiling(self.bundle, [{**self.entries[0], "stratum": "white-matter-supervoxel"}])
        # An unknown catalog id is an input error, not an atlas to download
        with self.assertRaises(ValueError) as context:
            fastmap.build_tiling(self.bundle, [{"id": "no-such-atlas", "stratum": "cortical"}], cache=empty)
        self.assertNotIsInstance(context.exception, fetch.FetchError)
        self.assertIn("no-such-atlas", str(context.exception))
        self.assertIn("Unknown", str(context.exception))

    def test_default_atlases_follow_the_connectome_family(self):
        entries = fastmap._resolve_entries(None, aan=False)
        same, notices = fastmap._default_entries_for_family(entries, "fsl6", False)
        self.assertEqual((same, notices), (entries, []))
        variants, notices = fastmap._default_entries_for_family(entries, "2009", False)
        self.assertEqual([entry["id"] for entry in variants], ["schaefer2018-400-7n-2mm-2009c", "tian-s2-2mm-2009c"])
        self.assertEqual([entry["stratum"] for entry in variants], ["cortical", "subcortical"])
        codes = {notice["atlas"]: notice["code"] for notice in notices}
        self.assertEqual(codes["buckner2011-cerebellum-7n-1mm"], "family_mismatch")
        self.assertEqual(codes["harvard-oxford-subcortical-th25-2mm"], "family_mismatch")
        self.assertEqual(codes["tian-s2-2mm-2009c"], "family_variant")
        crossed, notices = fastmap._default_entries_for_family(entries, "2009", True)
        self.assertEqual(crossed, entries)
        self.assertEqual(fastmap._default_entries_for_family(entries, "unknown", False), (entries, []))

    def test_isolated_pockets_are_reported(self):
        data = np.zeros(self.bundle.shape, np.int32)
        flat = data.reshape(-1)
        flat[self.bundle.mask_indices] = 1
        holes = self.bundle.mask_indices[::97]
        flat[holes] = 0
        tiling = fastmap.build_tiling(self.bundle, [{"atlas": Atlas("almost", data, self.bundle.affine, {}), "stratum": "cortical"}])
        isolated = tiling.stats["supervoxels"]["isolated_small"]
        self.assertGreater(isolated, 0)
        self.assertEqual(int(np.count_nonzero(tiling.is_supervoxel)), tiling.stats["n_supervoxels"])
        notice = next(notice for notice in tiling.notices if notice["code"] == "isolated_supervoxels")
        self.assertTrue(notice["message"].startswith(f"{isolated} pocket(s)"))

    # Index

    def test_index_rows_equal_exact_parcel_maps(self):
        tiling, index = self.tiling, self.index
        weights = np.zeros((tiling.n_parcels, self.bundle.n_voxels), np.float32)
        weights[tiling.labels, np.arange(self.bundle.n_voxels)] = 1.0
        exact = engine.compute_network_maps(self.bundle, weights)
        self.assertEqual(index.P.dtype, np.float16)
        self.assertEqual(index.P.shape, (tiling.n_parcels, self.bundle.n_voxels))
        np.testing.assert_allclose(index.P.astype(np.float32), exact.avg_fz, rtol=1e-3, atol=1e-3)
        # float16 rounding of the exact value bounds the error
        bound = np.abs(exact.avg_fz) * 2.0**-11 + 2e-5
        self.assertTrue(np.all(np.abs(index.P.astype(np.float32) - exact.avg_fz) <= bound + 1e-4))
        np.testing.assert_allclose(index.C, _brute_c(self.bundle, tiling.labels, tiling.n_parcels), rtol=1e-4, atol=1e-4)
        np.testing.assert_allclose(index.C, index.C.T, atol=1e-12)

    def test_index_json_and_loader(self):
        with open(os.path.join(self.index_dir, fastmap.INDEX_JSON), encoding="utf-8") as f:
            text = f.read()
        record = json.loads(text)
        self.assertNotIn(self.tmp, text, "index.json holds no absolute local path")
        self.assertEqual(record["format"], fastmap.FORMAT_NAME)
        self.assertEqual(record["label"], "approximate z-scale")
        self.assertEqual(record["terms"], "GSP-DUT-2014")
        self.assertTrue(record["acknowledgement"])
        self.assertEqual(record["subjects"]["n"], N_SUBJECTS)
        self.assertEqual(record["subjects"]["sha256"], connectome.subjects_sha256(self.bundle.subject_ids))
        self.assertEqual(record["bundle"]["mask_sha256"], self.bundle.mask_sha256)
        self.assertEqual([a["id"] for a in record["atlases"]], ["cortex", "subcortex", "cerebellum", "hospa"])
        self.assertTrue(all(len(a["sha256"]) == 64 for a in record["atlases"]))
        self.assertAlmostEqual(record["coverage"]["atlas_fraction"] + record["coverage"]["supervoxel_fraction"], 1.0)
        self.assertGreater(record["build"]["seconds_per_subject_median"], 0)
        self.assertEqual(record["library_version"], jobs.library_version())
        loaded = fastmap.load_index(self.index_dir, self.bundle)
        np.testing.assert_array_equal(loaded.P, self.index.P)
        np.testing.assert_array_equal(loaded.C, self.index.C)
        np.testing.assert_array_equal(loaded.tiling.labels, self.tiling.labels)
        self.assertEqual(loaded.terms, "GSP-DUT-2014")
        registry = runs.load(self.tmp)
        fast_runs = [r for r in registry.values() if r["kind"] == "fastindex"]
        self.assertEqual(len(fast_runs), 1)
        self.assertEqual(fast_runs[0]["status"], "done")
        self.assertEqual(record["run_id"], fast_runs[0]["id"])
        # Another connectome is refused; a changed file fails verification
        other = connectome.make_synthetic(os.path.join(self.tmp, "Other.lnmc"), shape=(18, 22, 18), n_subjects=2, runs=(20,))
        with self.assertRaises(ValueError):
            fastmap.load_index(self.index_dir, other["path"])
        copy = os.path.join(self.tmp, "index-copy")
        shutil.copytree(self.index_dir, copy)
        np.save(os.path.join(copy, fastmap.C_NAME), self.index.C * 2)
        with self.assertRaises(ValueError):
            fastmap.load_index(copy)

    def test_resume_after_stop(self):
        out = os.path.join(self.tmp, "index-resume")
        calls = {"n": 0}

        def cancel():
            calls["n"] += 1
            return calls["n"] > 3  # stops at the 4th subject, in the second batch

        with self.assertRaises(jobs.Cancelled):
            fastmap.build_index(self.bundle, self.tiling, out_dir=out, checkpoint_every=2, cancel=cancel, emit=_quiet, register=False)
        self.assertFalse(os.path.exists(os.path.join(out, fastmap.INDEX_JSON)))
        state = jobs.ResumeState(os.path.join(out, fastmap.WORK_DIR, fastmap.RESUME_NAME))
        self.assertEqual(state.done, ["batch-0"])
        events = []
        resumed = fastmap.build_index(self.bundle, self.tiling, out_dir=out, checkpoint_every=2, resume=True, emit=lambda event, **fields: events.append((event, fields)), register=False)
        straight = fastmap.build_index(self.bundle, self.tiling, out_dir=os.path.join(self.tmp, "index-straight"), checkpoint_every=2, emit=_quiet, register=False)
        np.testing.assert_array_equal(resumed.P, straight.P)
        np.testing.assert_array_equal(resumed.C, straight.C)
        self.assertEqual(resumed.metadata["build"]["resumed"], ["batch-0"])
        progress = [fields["done"] for event, fields in events if event == "progress"]
        self.assertEqual(progress, [3, 4, 5])
        self.assertFalse(os.path.exists(os.path.join(out, fastmap.WORK_DIR)))
        np.testing.assert_allclose(resumed.P.astype(np.float32), self.index.P.astype(np.float32), atol=2e-3)

    def test_resume_after_a_kill_between_checkpoint_and_record(self):
        """A stop after the checkpoint files of a batch are written but before resume.json records the batch
        (or after the record, before the previous checkpoint is removed) must not count a batch twice.
        """
        bundle, tiling = self.bundle, self.tiling
        straight = fastmap.build_index(bundle, tiling, out_dir=os.path.join(self.tmp, "index-straight-1"), checkpoint_every=1, emit=_quiet, register=False)
        original_mark, original_remove = jobs.ResumeState.mark_done, os.remove
        for where in ("before_record", "after_record"):
            out = os.path.join(self.tmp, f"index-kill-{where}")
            calls = {"mark": 0}

            def mark_done(state, unit, save=True, where=where, calls=calls):
                calls["mark"] += 1
                if where == "before_record" and calls["mark"] == 2:
                    raise KeyboardInterrupt("killed")  # batch-1's files are written, resume.json still says batch-0
                return original_mark(state, unit, save)

            def remove(path, *args, where=where, calls=calls, **kwargs):
                if where == "after_record" and calls["mark"] == 2 and os.path.basename(path).startswith("sums-"):
                    raise KeyboardInterrupt("killed")  # batch-1 is recorded, batch-0's files are still there
                return original_remove(path, *args, **kwargs)

            jobs.ResumeState.mark_done = mark_done
            os.remove = remove
            try:
                with self.assertRaises(KeyboardInterrupt):
                    fastmap.build_index(bundle, tiling, out_dir=out, checkpoint_every=1, emit=_quiet, register=False)
            finally:
                jobs.ResumeState.mark_done = original_mark
                os.remove = original_remove
            state = jobs.ResumeState(os.path.join(out, fastmap.WORK_DIR, fastmap.RESUME_NAME))
            self.assertEqual(state.done, ["batch-0"] if where == "before_record" else ["batch-0", "batch-1"])
            events = []
            resumed = fastmap.build_index(bundle, tiling, out_dir=out, checkpoint_every=1, resume=True, emit=lambda event, events=events, **fields: events.append((event, fields)), register=False)
            self.assertEqual(resumed.metadata["subjects"]["n"], N_SUBJECTS)
            np.testing.assert_array_equal(resumed.P, straight.P, err_msg=where)
            np.testing.assert_array_equal(resumed.C, straight.C, err_msg=where)
            self.assertEqual([fields["done"] for event, fields in events if event == "progress"], list(range(len(state.done) + 1, N_SUBJECTS + 1)))
        # A record that names a checkpoint whose files are gone: the build starts again with a warning
        out = os.path.join(self.tmp, "index-kill-lost")
        polls = {"n": 0}

        def cancel():
            polls["n"] += 1
            return polls["n"] > 2  # stops at the third subject, after two checkpoints

        with self.assertRaises(jobs.Cancelled):
            fastmap.build_index(bundle, tiling, out_dir=out, checkpoint_every=1, cancel=cancel, emit=_quiet, register=False)
        work = os.path.join(out, fastmap.WORK_DIR)
        for name in os.listdir(work):
            if name.startswith("sums-"):
                os.remove(os.path.join(work, name))
        events = []
        resumed = fastmap.build_index(bundle, tiling, out_dir=out, checkpoint_every=1, resume=True, emit=lambda event, **fields: events.append((event, fields)), register=False)
        self.assertIn("resume_unreadable", [fields.get("code") for event, fields in events if event == "warning"])
        self.assertEqual(resumed.metadata["build"]["resumed"], [])
        np.testing.assert_array_equal(resumed.P, straight.P)

    # Apply

    def test_lesion_equal_to_one_parcel_gives_its_row(self):
        index = self.index
        for parcel in (0, int(np.flatnonzero(self.tiling.is_supervoxel)[0])):
            lesion = (self.tiling.labels == parcel).astype(np.float32)
            approx = fastmap.approximate_maps(lesion, index)
            np.testing.assert_array_equal(approx[0], index.P[parcel].astype(np.float32))
            sparse = fastmap.approximate_maps([(np.flatnonzero(lesion), np.full(int(lesion.sum()), 0.5))], index)
            np.testing.assert_array_equal(sparse[0], index.P[parcel].astype(np.float32))

    def test_occupancy_weighted_average_and_coverage(self):
        tiling, index = self.tiling, self.index
        cortex_parcel = 0
        supervoxel_parcel = int(np.flatnonzero(tiling.is_supervoxel)[0])
        weights = np.zeros(tiling.n_voxels, np.float32)
        a = np.flatnonzero(tiling.labels == cortex_parcel)[:4]
        b = np.flatnonzero(tiling.labels == supervoxel_parcel)[:3]
        weights[a] = 1.0
        weights[b] = (0.5, 0.25, 0.25)
        m = fastmap.parcel_occupancy(weights, index)
        self.assertEqual(m.shape, (1, tiling.n_parcels))
        self.assertAlmostEqual(m[0, cortex_parcel], 4.0)
        self.assertAlmostEqual(m[0, supervoxel_parcel], 1.0)
        self.assertAlmostEqual(m.sum(), 5.0)
        sparse = fastmap.parcel_occupancy([(np.flatnonzero(weights), weights[weights > 0]), np.zeros(tiling.n_voxels)], tiling)
        np.testing.assert_allclose(sparse[0], m[0])
        self.assertEqual(sparse[1].sum(), 0.0)
        # A tuple of two dense integer (V,) lesions is two lesions, not one (positions, weights) pair
        a_dense, b_dense = np.zeros(tiling.n_voxels, np.int64), np.zeros(tiling.n_voxels, np.int64)
        a_dense[a] = 1
        b_dense[b] = 2
        pair = fastmap.parcel_occupancy((a_dense, b_dense), tiling)
        self.assertEqual(pair.shape, (2, tiling.n_parcels))
        self.assertEqual(pair.sum(), 4.0 + 6.0)
        one = fastmap.parcel_occupancy((np.flatnonzero(a_dense), np.ones(4)), tiling)
        self.assertEqual((one.shape, one.sum()), ((1, tiling.n_parcels), 4.0))
        approx = fastmap.approximate_maps(weights, index)
        expected = (4.0 * index.P[cortex_parcel].astype(np.float64) + index.P[supervoxel_parcel].astype(np.float64)) / 5.0
        np.testing.assert_allclose(approx[0], expected, rtol=1e-6, atol=1e-6)
        # The full float32 copy gives the same maps
        index.rows32()
        np.testing.assert_allclose(fastmap.approximate_maps(weights, index)[0], approx[0], rtol=1e-6, atol=1e-6)
        index._p32 = None
        cover = fastmap.coverage(np.stack([weights, np.zeros_like(weights)]), index)
        self.assertAlmostEqual(cover[0]["atlas"], 0.8)
        self.assertAlmostEqual(cover[0]["supervoxel"], 0.2)
        self.assertEqual(cover[0]["by_source"], {"cortex": 0.8, "supervoxel": 0.2})
        self.assertIsNone(cover[1]["atlas"])
        self.assertTrue(np.all(fastmap.approximate_maps(np.zeros(tiling.n_voxels), index) == 0))
        # Stratum from the centroid's parcel; size bins
        self.assertEqual(fastmap.lesion_strata(weights, index), ["cortical"])
        self.assertEqual(fastmap.lesion_strata((tiling.labels == supervoxel_parcel).astype(np.float32), tiling), ["white-matter-supervoxel"])
        self.assertAlmostEqual(fastmap.lesion_volumes_ml(weights, index)[0], 5.0 * 8.0 / 1000.0)
        self.assertEqual([fastmap.size_bin(v) for v in (0.04, 1.0, 9.9, 10.0, 50.0, None)], ["lt1ml", "1to10ml", "1to10ml", "10to50ml", "ge50ml", None])

    # Parity

    def test_parity_metrics_match_brute_force(self):
        rng = np.random.default_rng(5)
        n_voxels = 2000
        mu, pc1 = rng.standard_normal(n_voxels), rng.standard_normal(n_voxels)
        exact = rng.standard_normal((4, n_voxels)) + 0.8 * mu
        approx = exact + 0.6 * rng.standard_normal((4, n_voxels)) + 0.3 * pc1
        report = fastmap.parity(exact, approx, {"mu_r": mu, "pc1": pc1}, ["cortical"] * 4, sizes_ml=[0.5, 2, 20, 80], names=list("abcd"))
        design = np.column_stack([np.ones(n_voxels), mu, pc1])
        for row, lesion in enumerate(report["lesions"]):
            self.assertAlmostEqual(lesion["r"], np.corrcoef(exact[row], approx[row])[0, 1], places=10)
            ra = exact[row] - design @ np.linalg.lstsq(design, exact[row], rcond=None)[0]
            rb = approx[row] - design @ np.linalg.lstsq(design, approx[row], rcond=None)[0]
            self.assertAlmostEqual(lesion["partial_r"], np.corrcoef(ra, rb)[0, 1], places=10)
            k = int(np.ceil(0.1 * n_voxels))
            top_a = set(np.argsort(-np.abs(exact[row]))[:k])
            top_b = set(np.argsort(-np.abs(approx[row]))[:k])
            self.assertAlmostEqual(lesion["top10_dice"], 2 * len(top_a & top_b) / (2 * k), places=12)
        self.assertEqual([lesion["size_bin"] for lesion in report["lesions"]], ["lt1ml", "1to10ml", "10to50ml", "ge50ml"])
        self.assertEqual(report["label"], "approximate z-scale")
        self.assertEqual(report["gate_constants"], {"partial_r": 0.80, "top10_dice": 0.60})
        self.assertFalse(report["gate"]["enough_lesions"])
        self.assertEqual(report["gate"]["allowed_strata"], [])
        self.assertEqual(fastmap.top_dice([1, 2, 3, 4], [4, 3, 2, 1], top_pct=50, sign="pos"), 0.0)
        self.assertEqual(fastmap.top_dice([1, 2, 3, 4], [-4, 3, 2, 1], top_pct=25, sign="both"), 0.0)

    def test_gate_per_stratum_with_a_failing_stratum(self):
        def lesion(name, stratum, partial, dice, size="1to10ml"):
            return {"name": name, "stratum": stratum, "size_bin": size, "volume_ml": 2.0, "r": 0.9, "partial_r": partial, "top10_dice": dice}

        lesions = [lesion(f"c{i}", "cortical", 0.9, 0.7) for i in range(5)]
        lesions += [lesion(f"b{i}", "brainstem", 0.5, 0.7) for i in range(4)]
        lesions += [lesion(f"s{i}", "subcortical", 0.95, 0.8) for i in range(2)]
        lesions += [lesion("w0", "white-matter-supervoxel", 0.9, 0.7)]
        report = {"lesions": lesions, "reference": {"definition": "lnm-reference-v1"}}
        result = fastmap.gate(report)
        report["gate"] = result
        self.assertTrue(result["enough_lesions"])
        self.assertEqual(result["allowed_strata"], ["cortical"])
        self.assertEqual(fastmap.allowed_strata(report), ["cortical"])
        self.assertEqual(result["allowed_size_bins"], ["1to10ml"])
        self.assertTrue(result["strata"]["cortical"]["passed"])
        self.assertEqual(result["strata"]["brainstem"]["reason"], "median partial r 0.50 < 0.80")
        self.assertIn("at least 3", result["strata"]["subcortical"]["reason"])
        excluded = {item["name"]: item["message"] for item in result["excluded"]}
        self.assertEqual(sorted(excluded), ["b0", "b1", "b2", "b3", "s0", "s1", "w0"])
        self.assertIn("brainstem stratum failed the parity gate", excluded["b0"])
        self.assertTrue(excluded["b0"].startswith("Excluded from fast-mode statistics"))
        self.assertTrue(any("brainstem" in message for message in result["messages"]))
        self.assertEqual(fastmap.lesion_allowed(report, "cortical", 3.0), (True, "allowed by the parity gate"))
        allowed, message = fastmap.lesion_allowed(report, "brainstem", 3.0)
        self.assertFalse(allowed)
        self.assertIn("median partial r 0.50", message)
        self.assertFalse(fastmap.lesion_allowed(report, "cortical", 30.0)[0], "a size bin without exact lesions is not allowed")
        # A failing size bin excludes even lesions of a passing stratum
        lesions_bins = [lesion(f"c{i}", "cortical", 0.9, 0.7, "1to10ml") for i in range(7)] + [lesion(f"L{i}", "cortical", 0.85, 0.3, "ge50ml") for i in range(3)]
        result = fastmap.gate({"lesions": lesions_bins, "reference": {}})
        self.assertEqual(result["allowed_strata"], ["cortical"])
        self.assertEqual(result["allowed_size_bins"], ["1to10ml"])
        self.assertEqual(sorted(item["name"] for item in result["excluded"]), ["L0", "L1", "L2"])
        self.assertIn("median top-10% Dice 0.30 < 0.60", result["size_bins"]["ge50ml"]["reason"])
        # Fewer than 10 lesions: nothing is allowed; constants can be passed for calibration
        result = fastmap.gate({"lesions": lesions[:9], "reference": {}})
        self.assertEqual(result["allowed_strata"], [])
        self.assertEqual(len(result["excluded"]), 9)
        result = fastmap.gate(report, constants={"partial_r": 0.4, "top10_dice": 0.6})
        self.assertEqual(result["allowed_strata"], ["brainstem", "cortical"])
        allowed, message = fastmap.lesion_allowed(report, None, None)
        self.assertFalse(allowed)
        self.assertNotIn("None", message)
        self.assertIn("no voxel in the connectome mask", message)
        allowed, message = fastmap.lesion_allowed(report, "cortical", None)
        self.assertFalse(allowed)
        self.assertIn("volume is unknown", message)

    def test_gate_without_lesion_volumes_is_per_stratum(self):
        rng = np.random.default_rng(8)
        n_voxels = 2000
        mu, pc1 = rng.standard_normal(n_voxels), rng.standard_normal(n_voxels)
        exact = rng.standard_normal((10, n_voxels))
        approx = exact + 0.2 * rng.standard_normal((10, n_voxels))
        report = fastmap.parity(exact, approx, {"mu_r": mu, "pc1": pc1}, ["cortical"] * 10)  # the four-argument form
        result = report["gate"]
        self.assertFalse(report["size_axis"])
        self.assertFalse(result["size_axis"])
        self.assertEqual(result["size_bins"], {})
        self.assertEqual(result["allowed_strata"], ["cortical"])
        self.assertEqual(result["excluded"], [], "the passing stratum agrees with the per-lesion verdicts")
        self.assertTrue(any("per stratum only" in message for message in result["messages"]))
        self.assertTrue(fastmap.lesion_allowed(report, "cortical", None)[0])
        self.assertFalse(fastmap.lesion_allowed(report, "brainstem", None)[0])
        # With volumes the size bins are gated too
        sized = fastmap.parity(exact, approx, {"mu_r": mu, "pc1": pc1}, ["cortical"] * 10, [2.0] * 10)
        self.assertTrue(sized["gate"]["size_axis"])
        self.assertEqual(sized["gate"]["allowed_size_bins"], ["1to10ml"])

    def test_gate_cannot_pass_through_the_shared_degree_component(self):
        rng = np.random.default_rng(11)
        n_voxels = 3000
        mu, pc1 = rng.standard_normal(n_voxels), rng.standard_normal(n_voxels)
        exact = 3.0 * mu + rng.standard_normal((12, n_voxels))  # lesion maps that share only the degree component
        approx = 3.0 * mu + rng.standard_normal((12, n_voxels))
        report = fastmap.parity(exact, approx, {"mu_r": mu, "pc1": pc1}, ["cortical"] * 12, sizes_ml=[2.0] * 12)
        self.assertGreater(min(lesion["r"] for lesion in report["lesions"]), 0.8, "the raw r looks excellent")
        self.assertLess(max(abs(lesion["partial_r"]) for lesion in report["lesions"]), 0.15)
        self.assertEqual(report["gate"]["allowed_strata"], [])
        self.assertIn("median partial r", report["gate"]["strata"]["cortical"]["reason"])
        # Without reference maps the partial r is missing and nothing passes
        report = fastmap.parity(exact, exact, None, ["cortical"] * 12, sizes_ml=[2.0] * 12)
        self.assertIsNone(report["lesions"][0]["partial_r"])
        self.assertEqual(report["gate"]["allowed_strata"], [])
        self.assertTrue(any("No reference maps" in message for message in report["gate"]["messages"]))

    def test_parity_on_exact_engine_maps(self):
        bundle, index = self.bundle, self.index
        ref = reference.build(bundle, out_dir=os.path.join(self.tmp, "reference"), n_seeds=60, emit=_quiet, register=False)
        self.assertEqual(ref["definition"], "lnm-reference-v1")
        loaded_ref = reference.load_reference(bundle, path=os.path.join(self.tmp, "reference"))
        rng = np.random.default_rng(2)
        ijk = np.stack(np.unravel_index(bundle.mask_indices, bundle.shape), axis=1)
        centres = ijk[rng.choice(len(ijk), 12, replace=False)]
        lesions = np.stack([_sphere(bundle, centre, 1.6) for centre in centres])
        exact = engine.compute_network_maps(bundle, lesions)
        approx = fastmap.approximate_maps(lesions, index)
        strata = fastmap.lesion_strata(lesions, index)
        report = fastmap.parity(exact.avg_fz, approx, loaded_ref, strata, fastmap.lesion_volumes_ml(lesions, index))
        self.assertEqual(report["n"], 12)
        self.assertEqual(report["reference"]["definition"], "lnm-reference-v1")
        for lesion in report["lesions"]:
            self.assertIn(lesion["stratum"], fastmap.STRATA)
            self.assertTrue(-1 <= lesion["r"] <= 1 and -1 <= lesion["partial_r"] <= 1 and 0 <= lesion["top10_dice"] <= 1)
        self.assertTrue(report["gate"]["enough_lesions"])
        json.dumps(report)  # every value is JSON-serializable (it goes to provenance)

    # External parcel matrix

    def test_import_parcel_matrix(self):
        from scipy import io

        rng = np.random.default_rng(1)
        a = rng.standard_normal((6, 6))
        matrix = (a + a.T) / 2
        path = os.path.join(self.tmp, fastmap.GSP_PARCEL_MATRIX)
        io.savemat(path, {"FCz": matrix, "names": np.array(["x"] * 6, dtype=object), "small": np.eye(3), "rect": np.ones((2, 5))})
        imported = fastmap.import_parcel_matrix(path)
        self.assertEqual(imported["variable"], "FCz")
        self.assertEqual(imported["terms"], "GSP-DUT-2014")
        self.assertEqual(imported["file"], fastmap.GSP_PARCEL_MATRIX)
        np.testing.assert_allclose(imported["matrix"], matrix)
        self.assertAlmostEqual(fastmap.compare_parcel_matrices(matrix, imported["matrix"])["r"], 1.0)
        order = np.array([5, 4, 3, 2, 1, 0])
        self.assertAlmostEqual(fastmap.compare_parcel_matrices(matrix[np.ix_(order, order)], matrix, order=order)["rho"], 1.0)
        with self.assertRaises(ValueError):
            fastmap.compare_parcel_matrices(matrix, np.eye(5))
        block, rows = fastmap.atlas_block(self.index, "cortex")
        self.assertEqual(block.shape, (16, 16))
        np.testing.assert_array_equal(rows, np.arange(16))
        empty = os.path.join(self.tmp, "empty.mat")
        io.savemat(empty, {"rect": np.ones((2, 5))})
        with self.assertRaises(ValueError):
            fastmap.import_parcel_matrix(empty)

    # Command line

    def _run(self, *args):
        process = subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.fastmap", *args], capture_output=True, text=True, env=_env(), cwd=MODULE_DIR, timeout=600, check=False)
        events = [json.loads(line) for line in process.stdout.splitlines() if line.startswith("{")]
        return process.returncode, events, process.stderr

    def test_command_line(self):
        root = os.path.join(self.tmp, "cli")
        tiling_dir = os.path.join(root, "tiling")
        self.tiling.write(tiling_dir)
        out = os.path.join(root, "index")
        code, events, stderr = self._run("build", "--bundle", self.bundle.path, "--tiling", tiling_dir, "--out", out, "--run-root", root, "--checkpoint-every", "2", "--threads", "1")
        self.assertEqual(code, 0, stderr + json.dumps(events[-1:]))
        kinds = [event["event"] for event in events]
        self.assertEqual(kinds[0], "start")
        self.assertEqual(kinds[-1], "done")
        self.assertEqual(kinds.count("progress"), N_SUBJECTS)
        index = fastmap.load_index(out, self.bundle)
        np.testing.assert_allclose(index.P.astype(np.float32), self.index.P.astype(np.float32), atol=2e-3)
        self.assertEqual(index.metadata["tiling"]["sha256"], self.tiling.sha256())
        # The atlas records and coverage statistics of the written tiling reach index.json
        self.assertEqual([a["id"] for a in index.metadata["atlases"]], ["cortex", "subcortex", "cerebellum", "hospa"])
        self.assertTrue(all(len(a["sha256"]) == 64 for a in index.metadata["atlases"]))
        coverage = index.metadata["coverage"]
        self.assertAlmostEqual(coverage["atlas_fraction"], self.tiling.stats["atlas_fraction"])
        self.assertAlmostEqual(coverage["atlas_fraction"] + coverage["supervoxel_fraction"], 1.0)
        self.assertEqual(set(coverage["by_source"]), {"cortex", "subcortex", "cerebellum", "hospa"})
        # Exact maps of 12 lesions with the engine CLI, then parity and apply
        rng = np.random.default_rng(4)
        ijk = np.stack(np.unravel_index(self.bundle.mask_indices, self.bundle.shape), axis=1)
        lesion_paths = []
        for number, centre in enumerate(ijk[rng.choice(len(ijk), 12, replace=False)]):
            path = os.path.join(root, f"sub-{number + 1:02d}_lesion.nii.gz")
            nifti.write_nifti(path, engine.unmask(_sphere(self.bundle, centre, 1.6), self.bundle), self.bundle.affine)
            lesion_paths.append(path)
        maps_dir = os.path.join(root, "maps")
        run_json = os.path.join(maps_dir, "run.json")
        engine_args = [sys.executable, "-m", "LesionNetworkMappingLib.engine", "--bundle", self.bundle.path, "--out", maps_dir, "--run-json", run_json, "--threads", "1"]
        for path in lesion_paths:
            engine_args += ["--lesion", path]
        process = subprocess.run(engine_args, capture_output=True, text=True, env=_env(), cwd=MODULE_DIR, timeout=600, check=False)
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout[-2000:])
        reference.build(self.bundle, n_seeds=60, emit=_quiet, register=False)
        parity_dir = os.path.join(root, "parity")
        code, events, stderr = self._run("parity", "--bundle", self.bundle.path, "--index", out, "--engine-run", run_json, "--out", parity_dir, "--run-root", root)
        self.assertEqual(code, 0, stderr + json.dumps(events[-2:]))
        with open(os.path.join(parity_dir, fastmap.PARITY_JSON), encoding="utf-8") as f:
            text = f.read()
        report = json.loads(text)
        self.assertNotIn(self.tmp, text)
        self.assertEqual(report["n"], 12)
        self.assertEqual(report["terms"], "GSP-DUT-2014")
        self.assertEqual(report["reference"]["definition"], "lnm-reference-v1")
        self.assertEqual(report["lesions"][0]["file"], "sub-01_lesion.nii.gz")
        self.assertIn("coverage", report["lesions"][0])
        registry = runs.load(root)
        parity_runs = [r for r in registry.values() if r["kind"] == "parity"]
        self.assertEqual(len(parity_runs), 1)
        self.assertEqual(parity_runs[0]["parents"], [index.metadata["run_id"]])
        apply_dir = os.path.join(root, "fast")
        code, events, stderr = self._run("apply", "--index", out, "--lesion", lesion_paths[0], "--lesion", lesion_paths[1], "--out", apply_dir, "--parity", os.path.join(parity_dir, fastmap.PARITY_JSON))
        self.assertEqual(code, 0, stderr + json.dumps(events[-2:]))
        with open(os.path.join(apply_dir, "fastmaps.json"), encoding="utf-8") as f:
            record = json.load(f)
        self.assertEqual(record["label"], "approximate z-scale")
        self.assertEqual(record["terms"], "GSP-DUT-2014")
        self.assertIn("allowed", record["lesions"][0])
        data, affine, _ = nifti.read_nifti(os.path.join(apply_dir, record["lesions"][0]["map"]))
        np.testing.assert_allclose(affine, self.bundle.affine, atol=1e-5)
        weights, _qc = engine.prepare_lesion(lesion_paths[0], self.bundle)
        expected = fastmap.approximate_maps(weights, index)[0]
        np.testing.assert_allclose(data.reshape(-1)[self.bundle.mask_indices], expected, rtol=1e-6, atol=1e-6)
        self.assertTrue(record["lesions"][0]["map"].endswith("_desc-FastApproxFz_funcmap.nii.gz"))
        # Usage errors and atlases that are not cached
        code, events, _ = self._run("build")
        self.assertEqual(code, jobs.EXIT_USAGE)
        empty = os.path.join(root, "empty-cache")
        code, events, _ = self._run("build", "--bundle", self.bundle.path, "--out", os.path.join(root, "x"), "--cache", empty)
        self.assertEqual(code, 1)
        self.assertEqual(events[-1]["code"], "not_cached")
        code, events, _ = self._run("build", "--bundle", self.bundle.path, "--atlas", "massp20-1mm")
        self.assertEqual(code, jobs.EXIT_USAGE)
        code, events, _ = self._run("build", "--bundle", self.bundle.path, "--out", os.path.join(root, "x"), "--atlas", "foo:cortical", "--cache", empty)
        self.assertEqual(code, 1)
        self.assertEqual(events[-1]["code"], "input")
        self.assertIn("Unknown tiling atlas", events[-1]["message"])


if __name__ == "__main__":
    unittest.main()

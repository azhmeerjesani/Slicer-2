"""Tests of the reference-map library and the comparison matrix (refmaps.py, L-52).

Registry round trip (add, reload, files relative, remove), resampling onto the connectome grid (same grid and a finer
grid, against a brute-force average), the partial r of the matrix (against similarity.partial_spatial_correlation,
also with maps that do not cover every voxel), permutation p only for pairs built from in-study raw inputs (calibrated
under the null, also for two correlated scores of the same participants and for a score and its own dichotomy,
small for a planted association; participants permuted jointly), no p for external maps, the cross-condition note threshold, the
NeuroVault and van den Heuvel additions offline (licence shown, terms required, never redistributed) and the outputs
(no local paths, heat-map figure); removing a downloaded entry deletes its file so adding it again downloads it again.

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingRefmapsTest.py -v
"""

import json
import os
import shutil
import sys
import tempfile
import types
import unittest

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np

from LesionNetworkMappingLib import fetch, geometry, leaddbs, nifti, refmaps, similarity

SHAPE = (12, 14, 10)


def _bundle():
    """A light stand-in for a connectome bundle: a 2 mm grid and an ellipsoid mask."""
    affine = np.diag([2.0, 2.0, 2.0, 1.0])
    affine[:3, 3] = -(np.array(SHAPE) - 1)
    ijk = np.indices(SHAPE).reshape(3, -1).T
    centre = (np.array(SHAPE) - 1) / 2.0
    mask = (((ijk - centre) / (np.array(SHAPE) / 2.1)) ** 2).sum(axis=1) <= 1.0
    grid = geometry.Grid(SHAPE, affine)
    indices = np.flatnonzero(mask)
    return types.SimpleNamespace(grid=grid, mask_indices=indices, n_voxels=int(indices.size), shape=SHAPE, affine=affine)


def _smooth_field(rng, shape, scale=3.0):
    from scipy import ndimage

    return ndimage.gaussian_filter(rng.standard_normal(shape), scale)


class RefmapsTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-refmaps-")
        self.root = os.path.join(self.tmp, "ReferenceMaps")
        self.bundle = _bundle()
        self.rng = np.random.default_rng(3)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def writeMap(self, name, data, affine=None):
        path = os.path.join(self.tmp, name)
        nifti.write_nifti(path, np.asarray(data, dtype=np.float32), self.bundle.affine if affine is None else affine)
        return path


class RegistryTest(RefmapsTestBase):
    def test_round_trip(self):
        path = self.writeMap("pain_map.nii.gz", _smooth_field(self.rng, SHAPE))
        entry = refmaps.add_file(path, title="Pain map", licence="CC-BY-4.0", citation="Doe 2025", condition="pain", root=self.root)
        self.assertTrue(entry["id"].startswith("file-pain_map-"))
        self.assertEqual(entry["badge"], "green")
        self.assertFalse(os.path.isabs(entry["file"]))
        self.assertTrue(entry["file"].startswith("files/"))
        loaded = refmaps.entries(self.root)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0], entry)
        self.assertEqual(refmaps.get_entry(entry["id"], self.root)["condition"], "pain")
        copied = refmaps.entry_path(entry, self.root)
        self.assertTrue(os.path.isfile(copied))
        self.assertNotEqual(os.path.abspath(copied), os.path.abspath(path))
        self.assertEqual(refmaps._sha256(copied), entry["sha256"])
        # The registry holds no absolute path
        with open(os.path.join(self.root, refmaps.REGISTRY_JSON), encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn(self.tmp, text)
        # The folder can be moved: entries resolve relative to the new root
        moved = os.path.join(self.tmp, "moved")
        shutil.move(self.root, moved)
        self.assertTrue(os.path.isfile(refmaps.entry_path(refmaps.entries(moved)[0], moved)))
        # Unknown licence: grey badge; a second add gets a new id
        other = refmaps.add_file(path, root=moved)
        self.assertEqual(other["badge"], "grey")
        self.assertNotEqual(other["id"], entry["id"])
        refmaps.remove_entry(entry["id"], root=moved)
        self.assertEqual([item["id"] for item in refmaps.entries(moved)], [other["id"]])
        with self.assertRaises(refmaps.RefmapsError):
            refmaps.get_entry(entry["id"], moved)

    def test_registry_root(self):
        self.assertEqual(refmaps.registry_root(data_directory=self.tmp), os.path.join(self.tmp, "ReferenceMaps"))
        self.assertEqual(refmaps.registry_root(self.root), self.root)

    def test_newer_registry_refused(self):
        os.makedirs(self.root)
        with open(os.path.join(self.root, refmaps.REGISTRY_JSON), "w", encoding="utf-8") as f:
            json.dump({"format": refmaps.FORMAT, "version": refmaps.VERSION + 1, "entries": []}, f)
        with self.assertRaises(refmaps.RefmapsError):
            refmaps.entries(self.root)


class ResampleTest(RefmapsTestBase):
    def test_same_grid_is_exact(self):
        data = _smooth_field(self.rng, SHAPE)
        entry = refmaps.add_file(self.writeMap("same.nii.gz", data), root=self.root)
        values = refmaps.resample_to(entry, self.bundle, root=self.root)
        expected = data.astype(np.float32).astype(np.float64).reshape(-1)[self.bundle.mask_indices]
        np.testing.assert_allclose(values, expected, atol=1e-6)
        # Cached: the second call reads the cache (float32)
        again = refmaps.resample_to(entry, self.bundle, root=self.root)
        np.testing.assert_allclose(again, values, atol=1e-6)

    def test_finer_grid_fraction_mode(self):
        # A 1 mm map covering the 2 mm grid: each 2 mm voxel is the mean of its 8 sub-voxels (nonzero everywhere)
        fine_shape = tuple(2 * n for n in SHAPE)
        data = 5.0 + _smooth_field(self.rng, fine_shape, 4.0)
        affine = np.diag([1.0, 1.0, 1.0, 1.0])
        affine[:3, 3] = self.bundle.affine[:3, 3] - 0.5
        entry = refmaps.add_file(self.writeMap("fine.nii.gz", data, affine), root=self.root)
        values = refmaps.resample_to(entry, self.bundle, root=self.root)
        coarse = data.astype(np.float32).reshape(SHAPE[0], 2, SHAPE[1], 2, SHAPE[2], 2).mean(axis=(1, 3, 5))
        np.testing.assert_allclose(values, coarse.reshape(-1)[self.bundle.mask_indices], rtol=1e-5)

    def test_partial_coverage_is_nan(self):
        data = np.zeros(SHAPE)
        data[: SHAPE[0] // 2] = 1.0 + self.rng.random((SHAPE[0] // 2, *SHAPE[1:]))
        values = refmaps.map_on_grid(self.writeMap("half.nii.gz", data), self.bundle)
        # Same grid: zeros are kept as values, not as "outside"
        self.assertFalse(np.isnan(values).any())
        shifted = self.bundle.affine.copy()
        shifted[:3, 3] += 0.5  # off-grid: resampled, zero voxels count as not covered
        values = refmaps.map_on_grid(self.writeMap("half_shift.nii.gz", data, shifted), self.bundle)
        i = np.unravel_index(self.bundle.mask_indices, SHAPE)[0]
        self.assertTrue(np.isnan(values[i >= SHAPE[0] // 2 + 1]).all())
        self.assertTrue(np.isfinite(values[i < SHAPE[0] // 2 - 1]).all())


class CompareTest(RefmapsTestBase):
    def setUp(self):
        super().setUp()
        v = self.bundle.n_voxels
        self.mu = self.rng.standard_normal(v)
        self.pc1 = self.rng.standard_normal(v)
        shared = self.rng.standard_normal(v)
        self.maps = {
            "A": 2.0 * self.mu + shared + 0.5 * self.rng.standard_normal(v),
            "B": 2.0 * self.mu + 0.3 * self.pc1 + shared + 0.5 * self.rng.standard_normal(v),
            "C": 2.0 * self.mu + self.rng.standard_normal(v),
        }
        external = self.rng.standard_normal(v) + self.mu
        external[: v // 5] = np.nan  # an external map that does not cover every voxel
        self.maps["ext"] = external
        self.reference = {"mu_r": self.mu, "pc1": self.pc1}

    def test_matrices_match_similarity(self):
        result = refmaps.compare(self.maps, self.reference)
        self.assertEqual(result.names, ["A", "B", "C", "ext"])
        self.assertEqual(result.covariates, ("mu_r", "pc1"))
        covariates = np.vstack([self.mu, self.pc1])
        for i, a in enumerate(result.names):
            for j, b in enumerate(result.names):
                if i == j:
                    continue
                r, rho = similarity.pearson_spearman(self.maps[a], self.maps[b])
                self.assertAlmostEqual(result.pearson[i, j], r, places=10)
                self.assertAlmostEqual(result.spearman[i, j], rho, places=10)
                self.assertAlmostEqual(result.partial[i, j], similarity.partial_spatial_correlation(self.maps[a], self.maps[b], covariates), places=10)
                self.assertEqual(result.n_voxels[i, j], int((np.isfinite(self.maps[a]) & np.isfinite(self.maps[b])).sum()))
        np.testing.assert_allclose(result.pearson, result.pearson.T)
        # μC drives r; the partial r keeps only the shared component of A and B
        a, b, c = 0, 1, 2
        self.assertGreater(result.pearson[a, c], 0.6)
        self.assertLess(abs(result.partial[a, c]), 0.15)
        self.assertGreater(result.partial[a, b], 0.5)

    def test_reference_in_matrix_gives_nan_partial(self):
        maps = dict(self.maps, mu=self.mu)
        result = refmaps.compare(maps, self.reference)
        index = result.names.index("mu")
        self.assertTrue(np.isnan(result.partial[index, 0]))
        self.assertGreater(result.pearson[index, 0], 0.8)

    def test_no_covariates(self):
        result = refmaps.compare(self.maps)
        self.assertIsNone(result.partial)
        self.assertEqual(result.covariates, ())

    def test_mask(self):
        mask = np.zeros(self.bundle.n_voxels, dtype=bool)
        mask[::2] = True
        result = refmaps.compare(self.maps, self.reference, mask=mask)
        r = similarity.pearson(self.maps["A"][mask], self.maps["C"][mask])
        self.assertAlmostEqual(result.pearson[0, 2], r, places=10)


class PermutationTest(RefmapsTestBase):
    """p only for pairs built from in-study raw inputs (scores or labels reshuffled)."""

    def setUp(self):
        super().setUp()
        self.n, v = 40, self.bundle.n_voxels
        self.network = self.rng.standard_normal(v)
        self.raw = self.rng.standard_normal((self.n, v))
        self.load = self.rng.standard_normal(self.n)
        self.raw += np.outer(self.load, self.network)  # each lesion's map carries the network by its load

    def test_planted_association_gives_small_p(self):
        scores_a = self.load + 0.3 * self.rng.standard_normal(self.n)
        scores_b = self.load + 0.3 * self.rng.standard_normal(self.n)
        a = refmaps.in_study_map("symptom A", "score", np.arange(self.n), scores_a)
        b = refmaps.in_study_map("symptom B", "score", np.arange(self.n), scores_b)
        result = refmaps.permutation_p(self.raw, a, b, n_perm=199, seed=1)
        self.assertGreater(result.r, 0.5)
        self.assertAlmostEqual(result.p, 1 / 200)
        self.assertEqual(result.n_perm, 199)
        self.assertEqual(result.statistic, "pearson")
        # The observed r is the r of the maps built from the raw inputs
        r = similarity.pearson(refmaps.build_in_study(self.raw, a), refmaps.build_in_study(self.raw, b))
        self.assertAlmostEqual(result.r, r, places=8)

    def test_null_p_is_calibrated(self):
        ps = []
        for seed in range(40):
            rng = np.random.default_rng(100 + seed)
            a = refmaps.in_study_map("a", "score", np.arange(self.n), rng.standard_normal(self.n))
            b = refmaps.in_study_map("b", "contrast", np.arange(self.n), rng.permutation(np.r_[np.ones(20), np.zeros(20)]))
            ps.append(refmaps.permutation_p(self.raw, a, b, n_perm=99, seed=seed).p)
        ps = np.asarray(ps)
        # Under the null p is (discrete) uniform: about 5% below 0.05 and a mean near 0.5
        self.assertLessEqual(np.mean(ps <= 0.05), 0.15)
        self.assertGreater(ps.mean(), 0.35)
        self.assertLess(ps.mean(), 0.65)

    def test_correlated_scores_null_is_calibrated(self):
        # Two scores of the same participants that correlate with each other (as NIHSS and mRS do) but not with the
        # lesion maps: the participants are permuted jointly, so the null keeps the scores' correlation
        ps = []
        n, v = 60, self.bundle.n_voxels
        for seed in range(30):
            rng = np.random.default_rng(200 + seed)
            raw = rng.standard_normal((n, v)) + np.outer(rng.standard_normal(n), rng.standard_normal(v))
            latent = rng.standard_normal(n)
            a = refmaps.in_study_map("a", "score", np.arange(n), latent + 0.5 * rng.standard_normal(n))
            b = refmaps.in_study_map("b", "score", np.arange(n), latent + 0.5 * rng.standard_normal(n))
            result = refmaps.permutation_p(raw, a, b, n_perm=99, seed=seed)
            self.assertEqual(result.scheme, "joint")
            ps.append(result.p)
        ps = np.asarray(ps)
        self.assertLessEqual(np.mean(ps <= 0.05), 0.10)
        self.assertGreater(ps.mean(), 0.3)

    def test_score_and_its_dichotomy_null_is_calibrated(self):
        # A score map against the contrast of its own dichotomy (score > 0), with no lesion effect
        ps = []
        n, v = 40, self.bundle.n_voxels
        for seed in range(25):
            rng = np.random.default_rng(300 + seed)
            raw = rng.standard_normal((n, v)) + np.outer(rng.standard_normal(n), rng.standard_normal(v))
            score = rng.standard_normal(n)
            a = refmaps.in_study_map("score", "score", np.arange(n), score)
            b = refmaps.in_study_map("dichotomy", "contrast", np.arange(n), (score > 0).astype(float))
            ps.append(refmaps.permutation_p(raw, a, b, n_perm=99, seed=seed).p)
        ps = np.asarray(ps)
        self.assertLessEqual(np.mean(ps <= 0.05), 0.12)
        self.assertGreater(ps.mean(), 0.3)

    def test_permutation_schemes(self):
        scores = self.load + 0.3 * self.rng.standard_normal(self.n)
        rows = np.arange(self.n)
        a = refmaps.in_study_map("a", "score", rows, scores)
        b = refmaps.in_study_map("b", "score", rows, self.load + 0.3 * self.rng.standard_normal(self.n))
        joint = refmaps.permutation_p(self.raw, a, b, n_perm=49, seed=4)
        self.assertEqual(joint.scheme, "joint")
        # The participants are matched by row, not by position: the same map with its rows listed in reverse gives the same test
        reversed_b = refmaps.in_study_map("b", "score", rows[::-1], b.values[::-1])
        again = refmaps.permutation_p(self.raw, a, reversed_b, n_perm=49, seed=4)
        self.assertAlmostEqual(again.r, joint.r, places=10)
        self.assertEqual(again.p, joint.p)
        # Disjoint participants: independent shuffles; partly shared: mixed
        first, second = refmaps.in_study_map("first", "score", rows[:20], scores[:20]), refmaps.in_study_map("second", "score", rows[20:], scores[20:])
        self.assertEqual(refmaps.permutation_p(self.raw, first, second, n_perm=19, seed=1).scheme, "independent")
        overlap = refmaps.in_study_map("overlap", "score", rows[10:30], scores[10:30])
        self.assertEqual(refmaps.permutation_p(self.raw, first, overlap, n_perm=19, seed=1).scheme, "mixed")
        # Each permuted value vector is a permutation of the map's own values, and the shared rows move together
        permuter = refmaps._Permuter(first, overlap)
        values_a, values_b = permuter.draw(np.random.default_rng(0), 5)
        for row in range(5):
            np.testing.assert_array_equal(np.sort(values_a[row]), np.sort(first.values))
            np.testing.assert_array_equal(np.sort(values_b[row]), np.sort(overlap.values))
            np.testing.assert_array_equal(values_a[row, 10:20], values_b[row, :10])  # identical scores on the shared rows
        # The scheme is recorded in the outputs
        comparison = refmaps.compare({"a": refmaps.build_in_study(self.raw, a), "b": refmaps.build_in_study(self.raw, b)})
        pairs = refmaps.pairs_rows(comparison, {(0, 1): joint}, {"a": a, "b": b})
        self.assertEqual((pairs[0]["p_scheme"], pairs[0]["p_statistic"]), ("joint", "Pearson r"))
        out = refmaps.write_outputs(comparison, os.path.join(self.tmp, "out"), tests={(0, 1): joint}, formats=())
        with open(out["json"], encoding="utf-8") as f:
            self.assertEqual(json.load(f)["tests"][0]["scheme"], "joint")

    def test_cancel(self):
        from LesionNetworkMappingLib import jobs

        a = refmaps.in_study_map("a", "score", np.arange(self.n), self.load)
        b = refmaps.in_study_map("b", "score", np.arange(self.n), self.load + self.rng.standard_normal(self.n))
        token = jobs.CancelToken(signals=False)
        token.cancel("test")
        with self.assertRaises(jobs.Cancelled):
            refmaps.permutation_p(self.raw, a, b, n_perm=49, cancel=token)

    def test_partial_statistic_and_sampling(self):
        mu = self.network + self.rng.standard_normal(self.bundle.n_voxels)
        a = refmaps.in_study_map("a", "score", np.arange(self.n), self.load)
        b = refmaps.in_study_map("b", "score", np.arange(self.n), self.load + self.rng.standard_normal(self.n))
        result = refmaps.permutation_p(self.raw, a, b, n_perm=49, seed=2, covariates=mu[np.newaxis], max_voxels=200)
        self.assertEqual(result.statistic, "partial")
        self.assertEqual(result.n_voxels, 200)
        self.assertEqual(result.null.shape, (49,))
        self.assertAlmostEqual(result.min_p, 1 / 50)

    def test_no_p_for_external_or_mean_maps(self):
        a = refmaps.in_study_map("a", "score", np.arange(self.n), self.load)
        mean = refmaps.in_study_map("mean", "mean", np.arange(self.n))
        self.assertEqual(refmaps.p_reason(a, a), "")
        self.assertEqual(refmaps.p_reason(a, None), refmaps.NO_P_EXTERNAL)
        self.assertEqual(refmaps.p_reason({"kind": "reference"}, a), refmaps.NO_P_EXTERNAL)
        self.assertEqual(refmaps.p_reason(a, mean), refmaps.NO_P_MEAN)
        with self.assertRaises(refmaps.NotInStudyError):
            refmaps.permutation_p(self.raw, a, None)
        with self.assertRaises(refmaps.NotInStudyError):
            refmaps.permutation_p(self.raw, mean, a)
        self.assertIn("spin", refmaps.NO_P_EXTERNAL)
        # In the pairs table, external pairs carry the reason and no p
        comparison = refmaps.compare({"a": refmaps.build_in_study(self.raw, a), "ref": self.network})
        rows = refmaps.pairs_rows(comparison, {}, {"a": a})
        self.assertEqual(rows[0]["p_perm"], "")
        self.assertEqual(rows[0]["p_note"], refmaps.NO_P_EXTERNAL)

    def test_in_study_checks(self):
        with self.assertRaises(refmaps.RefmapsError):
            refmaps.in_study_map("c", "contrast", np.arange(5), [1, 0, 0, 0, 0])
        with self.assertRaises(refmaps.RefmapsError):
            refmaps.in_study_map("s", "score", np.arange(5), [1, 1, 1, 1, 1])
        spec = refmaps.in_study_map("s", "score", np.arange(6), [1, 2, np.nan, 4, 5, 6])
        self.assertEqual(spec.rows.tolist(), [0, 1, 3, 4, 5])


class NoteTest(RefmapsTestBase):
    def test_threshold(self):
        v = self.bundle.n_voxels
        current = self.rng.standard_normal(v)
        current -= current.mean()
        noise = self.rng.standard_normal(v)
        noise -= noise.mean()
        noise -= current * (noise @ current) / (current @ current)
        noise *= np.linalg.norm(current) / np.linalg.norm(noise)

        def at(r):
            return r * current + np.sqrt(1 - r * r) * noise

        candidates = [
            {"name": "Tinnitus", "values": at(0.65), "condition": "tinnitus"},
            {"name": "Depression (other study)", "values": at(0.9), "condition": "Depression"},
            {"name": "Migraine", "values": at(0.59), "condition": "migraine"},
            {"name": "Unknown", "values": at(0.95)},
            {"name": "Mania", "values": at(-0.7), "condition": "mania"},
        ]
        rows = refmaps.top_similar(current, candidates, k=10, condition="depression", related=("mania",))
        byName = {row["name"]: row for row in rows}
        self.assertAlmostEqual(byName["Tinnitus"]["r"], 0.65, places=6)
        self.assertTrue(byName["Tinnitus"]["unrelated"])
        self.assertIn("clinically unrelated", byName["Tinnitus"]["note"])
        self.assertEqual(byName["Migraine"]["note"], "")  # below 0.6
        self.assertEqual(byName["Depression (other study)"]["note"], "")  # same condition
        self.assertEqual(byName["Unknown"]["note"], "")  # condition unknown
        self.assertEqual(byName["Mania"]["note"], "")  # listed as related
        self.assertEqual([row["name"] for row in rows][:2], ["Unknown", "Depression (other study)"])
        self.assertEqual(len(refmaps.top_similar(current, candidates, k=2, condition="depression")), 2)
        # Negative r counts by its magnitude for an unrelated condition
        rows = refmaps.top_similar(current, candidates[4:], condition="depression")
        self.assertIn("clinically unrelated", rows[0]["note"])

    def test_connectome_reference_note(self):
        v = self.bundle.n_voxels
        current = self.rng.standard_normal(v)
        rows = refmaps.top_similar(current, [{"name": "μC_r", "values": current + 0.1 * self.rng.standard_normal(v), "kind": "connectome"}])
        self.assertIn("connectome", rows[0]["note"])


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class _FakeSession:
    """Answers the NeuroVault API calls; the image file itself is already in the cache (adopted), so nothing is downloaded."""

    def __init__(self, image, collection, images):
        self.image, self.collection, self.images = image, collection, images
        self.calls = []

    def get(self, url, params=None, timeout=None, **kwargs):
        self.calls.append(url)
        if "/images/" in url and "/collections/" not in url:
            return _FakeResponse(self.image)
        if url.endswith("/images/"):
            return _FakeResponse({"results": self.images})
        return _FakeResponse(self.collection)


class SourcesTest(RefmapsTestBase):
    def test_remove_url_entry_deletes_download(self):
        import functools
        import http.server
        import threading
        from unittest import mock

        served = os.path.join(self.tmp, "served")
        os.makedirs(served)
        first = _smooth_field(self.rng, SHAPE)
        nifti.write_nifti(os.path.join(served, "m.nii.gz"), first.astype(np.float32), self.bundle.affine)
        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        handler = functools.partial(QuietHandler, directory=served)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/m.nii.gz"
        with mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}):
            entry = refmaps.add_url(url, root=self.root, offline=False)
            twin = refmaps.add_url(url, root=self.root, offline=False)  # a second entry sharing the downloaded file
            path = refmaps.entry_path(entry, self.root)
            old = refmaps.resample_to(entry, self.bundle, root=self.root)
            refmaps.remove_entry(twin["id"], root=self.root)
            self.assertTrue(os.path.isfile(path))  # still used by the first entry
            refmaps.remove_entry(entry["id"], root=self.root)
            self.assertFalse(os.path.exists(os.path.dirname(path)))
            relpath = entry["file"]
            self.assertNotIn(relpath, fetch.read_manifest(self.root)["files"])
            # The server's file changed: adding the URL again downloads the new file (no stale cache, no checksum clash)
            nifti.write_nifti(os.path.join(served, "m.nii.gz"), (-first).astype(np.float32), self.bundle.affine)
            again = refmaps.add_url(url, root=self.root, offline=False)
            new = refmaps.resample_to(again, self.bundle, root=self.root)
        np.testing.assert_allclose(new, -old, atol=1e-5)
        self.assertNotEqual(again["sha256"], entry["sha256"])

    def test_neurovault_offline(self):
        path = self.writeMap("pain.nii.gz", _smooth_field(self.rng, SHAPE))
        url = "https://neurovault.org/media/images/3324/pain.nii.gz"
        image = {"id": 12345, "name": "Pain signature", "file": url.replace("https", "http"), "collection_id": 3324, "map_type": "T map",
                 "target_template_image": "GenericMNI", "file_size": os.path.getsize(path)}
        collection = {"id": 3324, "name": "Pain maps", "authors": "Doe J", "DOI": "10.1/xyz", "url": "http://neurovault.org/collections/3324/"}
        resource = fetch.Resource({"id": "neurovault/12345", "urls": [url], "filename": "pain.nii.gz", "checksum": fetch.RECORD_ON_FIRST_DOWNLOAD})
        fetch.adopt(resource, path, cache=self.root)
        session = _FakeSession(image, collection, [image])
        added = refmaps.add_neurovault(image_id=12345, condition="pain", root=self.root, session=session, offline=True)
        self.assertEqual(len(added), 1)
        entry = added[0]
        self.assertEqual(entry["id"], "nv-12345")
        self.assertEqual(entry["licence"], refmaps.NEUROVAULT_LICENCE)
        self.assertIn("CC0", entry["licence_note"])
        self.assertIn("doi:10.1/xyz", entry["citation"])
        self.assertEqual(entry["source"]["type"], "neurovault")
        self.assertEqual(entry["source"]["collection_id"], "3324")
        self.assertTrue(any(call.startswith(refmaps.NEUROVAULT_API) for call in session.calls))
        self.assertEqual(refmaps.resample_to(entry, self.bundle, root=self.root).shape, (self.bundle.n_voxels,))

    def test_vdh_needs_terms_and_pinned_commit(self):
        with self.assertRaises(refmaps.RefmapsError) as raised:
            refmaps.add_vdh(root=self.root, offline=True)
        self.assertIn("terms", str(raised.exception))
        with self.assertRaises(refmaps.RefmapsError) as raised:
            refmaps.add_vdh(commit="0000000", accepted_terms_sha256="a" * 64, root=self.root, offline=True)
        self.assertIn("pinned", str(raised.exception))
        self.assertEqual(refmaps.entries(self.root), [])  # nothing was downloaded or registered

    def test_vdh_entry_from_cache(self):
        catalog = refmaps.vdh_catalog()
        resource = catalog.get("aphasia-M2001/mask")
        data = np.zeros(SHAPE, dtype=np.float32)
        data[4:7, 5:8, 3:6] = 1
        path = self.writeMap("sub_M2001_lesion.nii.gz", data)
        fetch.adopt(resource, path, cache=self.root)
        # The pinned git blob does not match this synthetic file: refused and not registered
        with self.assertRaises(refmaps.RefmapsError) as raised:
            refmaps.add_vdh(entry="aphasia-M2001/mask", root=self.root, offline=True)
        self.assertIn("git blob", str(raised.exception))
        self.assertEqual(refmaps.entries(self.root), [])
        # Same file with the blob check satisfied (a patched catalog entry): attribution, licence and no redistribution
        patched = dict(resource.data, git_blob=leaddbs.git_blob_sha1(fetch.cached_path(resource, self.root)))
        catalog_data = dict(catalog.data, entries=[patched if item["id"] == resource.id else item for item in catalog.data["entries"]])
        catalog_path = os.path.join(self.tmp, "parity.json")
        with open(catalog_path, "w", encoding="utf-8") as f:
            json.dump(catalog_data, f)
        entry = refmaps.add_vdh(entry="aphasia-M2001/mask", root=self.root, offline=True, catalog_path=catalog_path, condition="aphasia")
        self.assertFalse(entry["redistribute"])
        self.assertEqual(entry["badge"], "grey")
        self.assertIn("never redistributed", entry["licence_note"])
        self.assertIn("ca410e6", entry["citation"])
        self.assertEqual(entry["source"]["commit"], "ca410e6")


class OutputsTest(RefmapsTestBase):
    def test_write_outputs(self):
        v = self.bundle.n_voxels
        mu = self.rng.standard_normal(v)
        maps = {"Group T": mu + self.rng.standard_normal(v), "Reference, pain": mu + self.rng.standard_normal(v), "μC_r": mu}
        comparison = refmaps.compare(maps, {"mu_r": mu, "pc1": self.rng.standard_normal(v)})
        infos = {"Group T": {"kind": "group", "file": os.path.join(self.tmp, "x.nii.gz")},
                 "Reference, pain": {"kind": "reference", "id": "nv-1", "licence": "CC0-1.0", "citation": "Doe", "source": {"type": "neurovault", "path": "/home/me/x.nii.gz"}},
                 "μC_r": {"kind": "connectome"}}
        out = os.path.join(self.tmp, "compare")
        written = refmaps.write_outputs(comparison, out, infos=infos, condition="pain", top=[{"name": "Reference, pain", "r": 0.7, "note": "x"}])
        for name in ("compare_r.csv", "compare_spearman.csv", "compare_partial.csv", "compare_pairs.csv", "compare.json"):
            self.assertTrue(os.path.isfile(os.path.join(out, name)), name)
        self.assertTrue(os.path.isfile(os.path.join(written["figure"], "compare-matrix.png")))
        self.assertTrue(os.path.isfile(os.path.join(written["figure"], "compare-matrix.svg")))
        for root, _dirs, files in os.walk(out):
            for name in files:
                if name.endswith((".json", ".csv", ".txt", ".svg")):
                    with open(os.path.join(root, name), encoding="utf-8") as f:
                        text = f.read()
                    self.assertNotIn(self.tmp, text, name)
                    self.assertNotIn("/home/me", text, name)
                self.assertFalse(name.endswith((".nii", ".nii.gz")), "no map file is copied")
        with open(os.path.join(out, "compare.json"), encoding="utf-8") as f:
            record = json.load(f)
        self.assertEqual(record["format"], refmaps.COMPARE_FORMAT)
        self.assertEqual(record["covariates"], ["mu_r", "pc1"])
        self.assertIn("no spin", record["statistics"]["p"])
        with open(os.path.join(out, "compare_r.csv"), encoding="utf-8") as f:
            header = f.readline()
        self.assertIn('"Reference, pain"', header)

    def test_heatmap_many_maps(self):
        names = [f"map {index}" for index in range(20)]
        matrix = np.corrcoef(self.rng.standard_normal((20, 50)))
        figure = refmaps.heatmap_figure(names, matrix, None)
        self.assertGreater(figure.size[0], 200)
        self.assertIn("map 19", figure.caption)


if __name__ == "__main__":
    unittest.main()

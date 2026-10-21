"""Unit tests of the cluster and peak table (clusters.py): sign split, extents, sub-peaks, labels and writers.

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingClustersTest.py -v
"""

import csv
import itertools
import os
import sys
import tempfile
import unittest

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np
from scipy import ndimage

from LesionNetworkMappingLib import atlases, clusters, thresholds
from LesionNetworkMappingLib.geometry import Grid, mask_indices, space_family

SHAPE = (20, 22, 18)
# 2 mm voxels; x = 2 i - 19 is never 0, so every voxel is left (i <= 9) or right (i >= 10)
AFFINE = np.array([[2.0, 0, 0, -19.0], [0, 2.0, 0, -21.0], [0, 0, 2.0, -17.0], [0, 0, 0, 1.0]])


def grid():
    return Grid(SHAPE, AFFINE)


def naive_table(volume, threshold, connectivity, min_mm, max_subpeaks, min_extent_mm3=0.0, voxel_mm3=8.0):
    """Reference implementation with plain Python loops: breadth-first clusters, brute-force local maxima.

    Assumes no ties between neighbouring values (continuous random data).
    :return: list of (sign, voxels, [(ijk, value), ...] peaks strongest first, value-weighted centre in ijk), in table order.
    """
    offsets = [d for d in itertools.product((-1, 0, 1), repeat=3) if d != (0, 0, 0) and sum(map(abs, d)) <= {6: 1, 18: 2, 26: 3}[connectivity]]
    peak_offsets = [d for d in itertools.product((-1, 0, 1), repeat=3) if d != (0, 0, 0)]
    found = {"pos": [], "neg": []}
    for sign in ("pos", "neg"):
        score = volume if sign == "pos" else -volume
        supra = {tuple(int(v) for v in ijk) for ijk in np.argwhere(np.isfinite(score) & (score >= threshold))}
        seen = set()
        for start in sorted(supra):
            if start in seen:
                continue
            cluster, queue = {start}, [start]
            seen.add(start)
            while queue:
                voxel = queue.pop()
                for d in offsets:
                    other = (voxel[0] + d[0], voxel[1] + d[1], voxel[2] + d[2])
                    if other in supra and other not in seen:
                        seen.add(other)
                        cluster.add(other)
                        queue.append(other)
            if len(cluster) * voxel_mm3 < min_extent_mm3:
                continue
            maxima = []
            for voxel in cluster:
                neighbours = [(voxel[0] + d[0], voxel[1] + d[1], voxel[2] + d[2]) for d in peak_offsets]
                if all(score[voxel] >= score[n] for n in neighbours if n in cluster):
                    maxima.append(voxel)
            maxima.sort(key=lambda v: -score[v])
            kept = []
            for voxel in maxima:
                world = AFFINE[:3, :3] @ voxel + AFFINE[:3, 3]
                if all(np.linalg.norm(world - (AFFINE[:3, :3] @ k + AFFINE[:3, 3])) > min_mm for k in kept):
                    kept.append(voxel)
                if len(kept) > max_subpeaks:
                    break
            weights = np.array([abs(volume[v]) for v in cluster])
            centre = (np.array(sorted(cluster), dtype=float) * np.array([abs(volume[v]) for v in sorted(cluster)])[:, None]).sum(axis=0) / weights.sum()
            found[sign].append((sign, len(cluster), [(k, float(volume[k])) for k in kept], centre))
        found[sign].sort(key=lambda item: -abs(item[2][0][1]))
    return found["pos"] + found["neg"]


class TwoBlobTest(unittest.TestCase):
    def setUp(self):
        self.volume = np.zeros(SHAPE)
        self.volume[3:6, 8:11, 7:10] = 4.0  # 27 voxels, left
        self.volume[4, 9, 8] = 6.0  # its peak
        self.volume[13:15, 5:7, 4:8] = -3.0  # 16 voxels, right
        self.volume[14, 6, 5] = -5.0

    def test_two_sided_split_by_sign(self):
        table = clusters.cluster_table(self.volume, grid(), None, 2.0)
        self.assertEqual([row["id"] for row in table], ["1", "2"])
        positive, negative = table
        self.assertEqual((positive["sign"], positive["voxels"], positive["mm3"], positive["peak"]), ("pos", 27, 216.0, 6.0))
        self.assertEqual((negative["sign"], negative["voxels"], negative["mm3"], negative["peak"]), ("neg", 16, 128.0, -5.0))
        self.assertEqual(positive["ijk"], (4, 9, 8))
        self.assertEqual(positive["xyz"], (-11.0, -3.0, -1.0))
        self.assertEqual((positive["hemisphere"], negative["hemisphere"]), ("L", "R"))
        self.assertEqual(negative["ijk"], (14, 6, 5))
        # centre of gravity weighted by |value|
        local = np.argwhere(self.volume < 0)
        weights = -self.volume[tuple(local.T)]
        np.testing.assert_allclose(negative["cog"], AFFINE[:3, :3] @ ((local * weights[:, None]).sum(0) / weights.sum()) + AFFINE[:3, 3])
        self.assertEqual(len(table.clusters), 2)
        self.assertEqual(sorted(np.unique(table.cluster_vector).tolist()), [0, 1, 2])
        self.assertEqual(int((table.cluster_vector == 2).sum()), 16)

    def test_one_sided(self):
        self.assertEqual([row["sign"] for row in clusters.cluster_table(self.volume, grid(), None, 2.0, sign="pos")], ["pos"])
        negative = clusters.cluster_table(self.volume, grid(), None, 2.0, sign="neg")
        self.assertEqual([(row["id"], row["sign"], row["voxels"]) for row in negative], [("1", "neg", 16)])

    def test_signs_never_merge(self):
        volume = np.zeros(SHAPE)
        volume[5, 5, 5:8] = 3.0
        volume[5, 5, 8:11] = -3.0  # touches the positive run
        table = clusters.cluster_table(volume, grid(), None, 1.0)
        self.assertEqual([(row["sign"], row["voxels"]) for row in table], [("pos", 3), ("neg", 3)])

    def test_threshold_is_inclusive(self):
        table = clusters.cluster_table(self.volume, grid(), None, 4.0)  # |x| >= 4: the positive block and the -5 voxel
        self.assertEqual([row["voxels"] for row in table], [27, 1])
        self.assertEqual([row["voxels"] for row in clusters.cluster_table(self.volume, grid(), None, 3.0)], [27, 16])

    def test_connectivity(self):
        volume = np.zeros(SHAPE)
        volume[2, 2, 2] = volume[3, 3, 3] = 5.0  # corner neighbours
        volume[8, 8, 8] = volume[9, 9, 8] = 4.0  # edge neighbours
        self.assertEqual(len(clusters.cluster_table(volume, grid(), None, 1.0)), 2)
        self.assertEqual(len(clusters.cluster_table(volume, grid(), None, 1.0, connectivity=18)), 3)
        self.assertEqual(len(clusters.cluster_table(volume, grid(), None, 1.0, connectivity=6)), 4)

    def test_masked_vector_and_nan(self):
        mask = np.zeros(SHAPE, dtype=bool)
        mask[2:18, 2:20, 2:16] = True
        mask[4, 9, 8] = False  # the positive peak is outside the mask
        indices = mask_indices(mask)
        values = self.volume.reshape(-1)[indices].copy()
        table = clusters.cluster_table(values, grid(), indices, 2.0)
        self.assertEqual([(row["voxels"], row["peak"]) for row in table], [(26, 4.0), (16, -5.0)])
        self.assertEqual(table.cluster_vector.shape, values.shape)
        volume = self.volume.copy()
        volume[4, 9, 8] = np.nan
        self.assertEqual([row["voxels"] for row in clusters.cluster_table(volume, grid(), None, 2.0)], [26, 16])

    def test_extra_maps_at_peak(self):
        fz = self.volume * 0.1
        table = clusters.cluster_table(self.volume, grid(), None, 2.0, extra_maps={"Fz": fz, "PFwe": np.full(SHAPE, 0.01)})
        self.assertAlmostEqual(table[0]["extra"]["Fz"], 0.6)
        self.assertAlmostEqual(table[1]["extra"]["Fz"], -0.5)
        self.assertEqual(table[1]["extra"]["PFwe"], 0.01)

    def test_resolved_threshold(self):
        resolved = thresholds.resolve({"kind": "custom_t", "value": 2.5}, 40, 1000)
        table = clusters.cluster_table(self.volume, grid(), None, resolved)
        self.assertEqual((table.threshold, table.statistic, table.resolved), (2.5, "T", resolved))
        self.assertEqual(len(clusters.cluster_table(self.volume, grid(), None, resolved.to_dict())), 2)

    def test_errors(self):
        with self.assertRaises(ValueError):
            clusters.cluster_table(self.volume, grid(), None, 2.0, sign="up")
        with self.assertRaises(ValueError):
            clusters.cluster_table(self.volume, grid(), None, 2.0, connectivity=8)
        with self.assertRaises(ValueError):
            clusters.cluster_table(self.volume, grid(), None, 2.0, max_subpeaks=27)
        for bad in (0.0, -1.0, float("nan")):
            with self.assertRaises(ValueError):
                clusters.cluster_table(self.volume, grid(), None, bad)
        with self.assertRaises(ValueError):
            clusters.cluster_table(np.zeros(10), grid(), np.arange(11), 1.0)
        with self.assertRaises(ValueError):
            clusters.cluster_table(np.zeros(10), grid(), None, 1.0)


class SubpeakTest(unittest.TestCase):
    def bar(self):
        volume = np.zeros(SHAPE)
        volume[2:18, 10, 9] = 3.0
        for i, value in ((4, 10.0), (9, 8.0), (11, 7.0), (15, 6.0)):
            volume[i, 10, 9] = value
        return volume

    def test_subpeaks(self):
        table = clusters.cluster_table(self.bar(), grid(), None, 2.0)
        # i=11 is 4 mm from i=9 (dropped); i=15 is 12 mm from i=9 (kept)
        self.assertEqual([(row["id"], row["ijk"][0], row["peak"], row["subpeak"]) for row in table], [("1", 4, 10.0, False), ("1a", 9, 8.0, True), ("1b", 15, 6.0, True)])
        self.assertEqual(table[0]["voxels"], 16)
        self.assertIsNone(table[1]["voxels"])
        self.assertIsNone(table[1]["mm3"])
        self.assertIsNone(table[1]["cog"])
        self.assertEqual(len(table.clusters), 1)

    def test_max_subpeaks_and_distance(self):
        self.assertEqual([row["id"] for row in clusters.cluster_table(self.bar(), grid(), None, 2.0, max_subpeaks=1)], ["1", "1a"])
        self.assertEqual([row["id"] for row in clusters.cluster_table(self.bar(), grid(), None, 2.0, max_subpeaks=0)], ["1"])
        # 12 mm: i=9 is 10 mm from i=4 (dropped), i=11 is 14 mm away (kept), i=15 is 8 mm from i=11 (dropped)
        table = clusters.cluster_table(self.bar(), grid(), None, 2.0, subpeak_min_mm=12)
        self.assertEqual([(row["id"], row["ijk"][0]) for row in table], [("1", 4), ("1a", 11)])
        # a peak exactly at the minimum distance is dropped (nilearn keeps peaks farther than min_distance):
        # i=9 (10 mm from i=4) and i=15 (8 mm from i=11) go, the end of the bar (i=17, a local maximum) stays
        table = clusters.cluster_table(self.bar(), grid(), None, 2.0, subpeak_min_mm=10)
        self.assertEqual([row["ijk"][0] for row in table], [4, 11, 17])

    def test_plateau_gives_one_peak(self):
        volume = np.zeros(SHAPE)
        volume[3:10, 10, 9] = 5.0  # a flat bar of 7 voxels: one peak, at its centre
        table = clusters.cluster_table(volume, grid(), None, 1.0)
        self.assertEqual([(row["id"], row["ijk"]) for row in table], [("1", (6, 10, 9))])
        volume[3:9, 10, 9] = 5.0
        volume[9, 10, 9] = 0.0  # 6 voxels: the two central voxels tie, the first in index order wins
        self.assertEqual(clusters.cluster_table(volume, grid(), None, 1.0)[0]["ijk"], (5, 10, 9))


class MinimumExtentTest(unittest.TestCase):
    def test_min_extent(self):
        volume = np.zeros(SHAPE)
        volume[3:6, 3:6, 3:6] = 2.0  # 27 voxels = 216 mm3
        volume[15, 15, 15] = 9.0  # 1 voxel = 8 mm3 (strongest)
        self.assertEqual([row["voxels"] for row in clusters.cluster_table(volume, grid(), None, 1.0)], [1, 27])
        table = clusters.cluster_table(volume, grid(), None, 1.0, min_extent_mm3=8.0)
        self.assertEqual(([row["voxels"] for row in table], table.dropped), ([1, 27], 0))
        table = clusters.cluster_table(volume, grid(), None, 1.0, min_extent_mm3=9.0)
        self.assertEqual(([row["voxels"] for row in table], table.dropped), ([27], 1))
        self.assertEqual(table[0]["id"], "1")
        self.assertEqual(int((table.cluster_vector != 0).sum()), 27)
        self.assertEqual(len(clusters.cluster_table(volume, grid(), None, 1.0, min_extent_mm3=217.0)), 0)


class NaiveReferenceTest(unittest.TestCase):
    def test_matches_naive_reference(self):
        rng = np.random.default_rng(19)
        centre = np.array(SHAPE) / 2.0 - 0.5
        mask = (((np.indices(SHAPE).reshape(3, -1).T - centre) ** 2 / (np.array(SHAPE) / 2.2) ** 2).sum(1) <= 1).reshape(SHAPE)  # an ellipsoid
        indices = mask_indices(mask)
        compared = 0
        for seed_shift, connectivity, min_extent, max_subpeaks in ((0, 26, 0.0, 3), (1, 6, 0.0, 3), (2, 26, 40.0, 1), (3, 18, 0.0, 2)):
            field = ndimage.gaussian_filter(rng.standard_normal(SHAPE), 1.2 + 0.2 * seed_shift)
            field /= field[mask].std()
            volume = np.where(mask, field, np.nan)
            table = clusters.cluster_table(field.reshape(-1)[indices], grid(), indices, 1.3, connectivity=connectivity, min_extent_mm3=min_extent, max_subpeaks=max_subpeaks)
            reference = naive_table(volume, 1.3, connectivity, 8.0, max_subpeaks, min_extent)
            self.assertGreater(len(reference), 3)
            self.assertEqual(len(table.clusters), len(reference))
            groups = [[row for row in table if row["cluster"] == number] for number in range(1, len(reference) + 1)]
            for rows, (sign, voxels, peaks, centre_ijk) in zip(groups, reference, strict=True):
                self.assertEqual((rows[0]["sign"], rows[0]["voxels"]), (sign, voxels))
                self.assertEqual([row["ijk"] for row in rows], [peak[0] for peak in peaks])
                np.testing.assert_allclose([row["peak"] for row in rows], [peak[1] for peak in peaks])
                np.testing.assert_allclose(rows[0]["cog"], AFFINE[:3, :3] @ centre_ijk + AFFINE[:3, 3], atol=1e-9)
                self.assertEqual(int((table.cluster_vector == rows[0]["cluster"]).sum()), voxels)
                compared += 1
        self.assertGreater(compared, 20)


class LabelTest(unittest.TestCase):
    def setUp(self):
        self.volume = np.zeros(SHAPE)
        self.volume[3:6, 8:11, 7:10] = 4.0
        self.volume[13:15, 5:7, 4:8] = -3.0

    def test_labels_are_optional(self):
        table = clusters.cluster_table(self.volume, grid(), None, 2.0)
        self.assertEqual([(row["labels"], row["network"]) for row in table], [([], None), ([], None)])
        self.assertEqual(table.atlases, [])

    def test_injected_labeler(self):
        calls = []

        def labeler(mask, grid_):
            calls.append((mask.shape, int(mask.sum()), grid_.shape))
            return [("toy", "left blob" if mask[4, 9, 8] else "right blob", 100.0, "Default")]

        table = clusters.cluster_table(self.volume, grid(), None, 2.0, labeler=labeler)
        self.assertEqual(calls, [(SHAPE, 27, SHAPE), (SHAPE, 16, SHAPE)])
        self.assertEqual([row["labels"] for row in table], [[("toy", "left blob", 100.0)], [("toy", "right blob", 100.0)]])
        self.assertEqual(table[0]["network"], "Default")
        self.assertEqual(table.atlases, ["toy"])

    def test_atlas_labeler(self):
        data = np.zeros(SHAPE, dtype=np.int32)
        data[:10] = 1
        data[10:] = 2
        family = space_family("MNI152NLin6Asym")
        atlas = atlases.Atlas("halves", data, AFFINE, {1: "7Networks_LH_Vis_1", 2: "7Networks_RH_Default_PCC_1"}, family=family, space="MNI152NLin6Asym", networks="schaefer")
        table = clusters.cluster_table(self.volume, grid(), None, 2.0, labeler=clusters.atlas_labeler([atlas], space="MNI152NLin6Asym"))
        self.assertEqual([[(atlas, label, round(pct, 6)) for atlas, label, pct in row["labels"]] for row in table], [[("halves", "7Networks_LH_Vis_1", 100.0)], [("halves", "7Networks_RH_Default_PCC_1", 100.0)]])
        self.assertEqual([row["network"] for row in table], ["Vis", "Default"])
        self.assertEqual(table.atlases, ["halves"])
        self.assertIn("halves", clusters.cluster_caption(table))
        # an atlas of another family is refused with a notice, never silently
        other = atlases.Atlas("other", data, AFFINE, {1: "a", 2: "b"}, family="2009" if family != "2009" else "fsl6")
        table = clusters.cluster_table(self.volume, grid(), None, 2.0, labeler=clusters.atlas_labeler([other], space="MNI152NLin6Asym"))
        self.assertEqual(table[0]["labels"], [])
        self.assertGreater(len(table.clusters), 1)
        self.assertEqual([notice["code"] for notice in table.notices], ["family_mismatch"])  # once, not once per cluster


class HomologueTest(unittest.TestCase):
    def test_pairs(self):
        volume = np.zeros(SHAPE)
        volume[4, 10, 9] = 6.0  # x = -11
        volume[14, 11, 9] = 5.0  # x = +9, y + 2: mirror distance 2.83 mm
        volume[2, 2, 2] = 4.0  # x = -15, far from everything
        volume[15, 10, 9] = -7.0  # x = +11, negative: never pairs with a positive peak
        table = clusters.cluster_table(volume, grid(), None, 1.0)
        entries = clusters.pair_homologues(table)
        summary = [(e["left"] and e["left"]["peak"], e["right"] and e["right"]["peak"], e["distance_mm"] and round(e["distance_mm"], 2)) for e in entries]
        self.assertEqual(summary, [(None, -7.0, None), (6.0, 5.0, 2.83), (4.0, None, None)])
        self.assertEqual(len(clusters.pair_homologues(table, max_mm=2.0)), 4)


class WriterTest(unittest.TestCase):
    def test_caption_and_files(self):
        volume = np.zeros(SHAPE)
        volume[3:6, 8:11, 7:10] = 9.0
        volume[4, 9, 8] = 12.0
        volume[13:15, 5:7, 4:8] = -8.0
        resolved = thresholds.resolve({"kind": "fwe_bonferroni", "alpha": 0.05}, 1000, 228483)
        table = clusters.cluster_table(volume, grid(), None, resolved, extra_maps={"Fz": volume / 40}, labeler=lambda mask, g: [("HO<cort>", "Frontal & Pole", 62.5)])
        caption = clusters.cluster_caption(table)
        for expected in ("FWE (Bonferroni)", "α = 0.05", "N = 1000", "df = 999", "|T| ≥ 5.", "26-connected", "two-sided", "HO<cort>", "descriptive", "no cluster-extent inference"):  # noqa: RUF001
            self.assertIn(expected, caption)
        plain = clusters.cluster_caption(clusters.cluster_table(volume, grid(), None, 3.0, sign="pos", statistic="Fz"), n=50)
        self.assertIn("Fz ≥ 3", plain)
        self.assertIn("N = 50", plain)
        self.assertIn("no atlas labels", plain)
        self.assertIn("descriptive", plain)
        with tempfile.TemporaryDirectory() as directory:
            tsv, page = os.path.join(directory, "clusters.tsv"), os.path.join(directory, "clusters.html")
            self.assertEqual(clusters.write_clusters(table, tsv, page), caption)
            with open(tsv, encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream, delimiter="\t"))
            self.assertEqual(list(rows[0]), ["cluster_id", "sign", "voxels", "volume_mm3", "peak", "Fz", "x", "y", "z", "hemisphere", "cog_x", "cog_y", "cog_z", "labels", "network"])
            self.assertEqual([(r["cluster_id"], r["sign"], r["voxels"], r["volume_mm3"], r["peak"], r["x"], r["hemisphere"]) for r in rows], [("1", "+", "27", "216", "12", "-11.0", "L"), ("2", "-", "16", "128", "-8", "7.0", "R")])
            self.assertEqual(rows[0]["labels"], "HO<cort>: Frontal & Pole (62.5%)")
            self.assertEqual(rows[0]["Fz"], "0.3")
            with open(page, encoding="utf-8") as stream:
                text = stream.read()
            self.assertIn("HO&lt;cort&gt;: Frontal &amp; Pole", text)
            self.assertIn("no cluster-extent inference", text)
            self.assertIn("<caption>", text)
            self.assertEqual(sorted(os.listdir(directory)), ["clusters.html", "clusters.tsv"])
            umask = os.umask(0)
            os.umask(umask)
            self.assertEqual(os.stat(tsv).st_mode & 0o777, 0o666 & ~umask)
            self.assertEqual(os.stat(page).st_mode & 0o777, 0o666 & ~umask)
            clusters.write_clusters(list(table), tsv, None, caption="custom")
            large = [dict(table[0], voxels=1225043, mm3=1225043.0), dict(table[0], voxels=3658, mm3=12345.75)]
            clusters.write_clusters(large, tsv, None, caption="volumes")
            with open(tsv, encoding="utf-8") as stream:
                self.assertEqual([r["volume_mm3"] for r in csv.DictReader(stream, delimiter="\t")], ["1225043", "12345.75"])
            with self.assertRaises(ValueError):
                clusters.write_clusters(list(table), tsv)


if __name__ == "__main__":
    unittest.main()

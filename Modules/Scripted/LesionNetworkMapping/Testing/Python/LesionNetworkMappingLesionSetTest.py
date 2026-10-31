"""Unit tests of the lesion-set diagnostics (lesionset.py): exact overlap, Dice, SMD, matching, tissue, CLI.

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingLesionSetTest.py -v
"""

import json
import math
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

from LesionNetworkMappingLib import lesionops, lesionset
from LesionNetworkMappingLib.geometry import GRID_BY_ID, Grid
from LesionNetworkMappingLib.nifti import read_nifti, write_nifti
from LesionNetworkMappingLib.study import Study

SPACE = "MNI152NLin2009cAsym"


def small_grid(shape=(12, 10, 8), spacing=2.0, flipped=False):
    affine = np.diag([spacing, spacing, spacing, 1.0])
    affine[:3, 3] = (-spacing * (shape[0] - 1) / 2.0, -20.0, -10.0)  # x = 0 lies between two voxel centres for even ni
    if flipped:
        affine[0, 0] = -spacing
        affine[0, 3] = spacing * (shape[0] - 1) / 2.0
    return Grid(shape, affine)


def boxes(grid, specs):
    """Binary volumes from (i0, i1, j0, j1, k0, k1) boxes."""
    out = []
    for i0, i1, j0, j1, k0, k1 in specs:
        volume = np.zeros(grid.shape)
        volume[i0:i1, j0:j1, k0:k1] = 1.0
        out.append(volume)
    return out


class OverlapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-lesionset-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.grid = small_grid()

    def test_exact_counts_at_three(self):
        # Five nested boxes along i: voxel column i is covered by exactly min(i, 5) lesions for i < 6
        volumes = boxes(self.grid, [(i, 6, 2, 4, 2, 4) for i in range(1, 6)])
        occ = lesionset.LesionSet.from_volumes(volumes, self.grid, SPACE)
        maps = lesionset.overlap_maps(occ)
        self.assertEqual(maps.count.dtype, np.int16)
        expected = np.zeros(self.grid.shape, dtype=np.int16)
        for volume in volumes:
            expected += volume.astype(np.int16)
        np.testing.assert_array_equal(maps.count, expected)
        at_least_3 = lesionset.overlap_at_least(maps.count, 3)
        self.assertEqual(int(at_least_3.sum()), 3 * 2 * 2)  # i = 3, 4, 5
        np.testing.assert_array_equal(at_least_3, expected >= 3)
        np.testing.assert_array_equal(maps.percent, (expected.astype(np.float64) * 100.0 / 5).astype(np.float32))
        self.assertEqual(maps.max_count, 5)
        self.assertEqual(maps.legend, "max 5/5 (100%)")

        # Written and read back: still exact integers, so a viewer threshold ">= 3" selects the same voxels
        count_path, percent_path = lesionset.write_overlap(self.tmp, maps, occ, out_dir=self.tmp)
        self.assertTrue(count_path.endswith("group_space-MNI152NLin2009cAsym_desc-overlapCount_map.nii.gz"))
        data, affine, _ = read_nifti(count_path)
        self.assertEqual(data.dtype, np.int16)
        np.testing.assert_array_equal(data, expected)
        np.testing.assert_allclose(affine, self.grid.affine, atol=1e-5)
        np.testing.assert_array_equal(read_nifti(percent_path)[0], maps.percent)
        with open(count_path[: -len(".nii.gz")] + ".json", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["SlicerStudio"]["legend"], "max 5/5 (100%)")

    def test_legend_and_binarization(self):
        self.assertEqual(lesionset.overlap_legend(6, 23), "max 6/23 (26%)")
        volume = np.zeros(self.grid.shape)
        volume[1, 1, 1], volume[2, 2, 2] = 0.5, 0.49
        occ = lesionset.LesionSet.from_volumes([volume], self.grid, SPACE)
        maps = lesionset.overlap_maps(occ, binarize_at=0.5)
        self.assertEqual(int(maps.count[1, 1, 1]), 1)
        self.assertEqual(int(maps.count[2, 2, 2]), 0)

    def test_write_in_the_template_header(self):
        spec = GRID_BY_ID["TF2009-2mm"]
        reference = os.path.join(self.tmp, "ref.nii.gz")
        write_nifti(reference, np.zeros(spec.shape, dtype=np.uint8), spec.affine, sform_code=4, qform_code=4)
        volume = np.zeros(spec.shape)
        volume[40:44, 50:52, 40:41] = 1
        occ = lesionset.LesionSet.from_volumes([volume, volume], spec.grid, SPACE)
        maps = lesionset.overlap_maps(occ)
        count_path, _ = lesionset.write_overlap(self.tmp, maps, occ, out_dir=self.tmp, reference=reference)
        data, affine, _ = read_nifti(count_path)
        np.testing.assert_array_equal(data, maps.count)
        np.testing.assert_allclose(affine, spec.affine, atol=1e-5)


class DiceTest(unittest.TestCase):
    def test_equals_brute_force(self):
        grid = small_grid((16, 14, 12))
        rng = np.random.default_rng(3)
        volumes = []
        for _ in range(14):
            volume = np.zeros(grid.shape)
            i, j, k = rng.integers(0, 10, 3)
            a, b, c = rng.integers(1, 6, 3)
            volume[i:i + a, j:j + b, k:k + c] = rng.uniform(0.3, 1.0, (min(a, 16 - i), min(b, 14 - j), min(c, 12 - k)))
            volumes.append(volume)
        volumes.append(np.zeros(grid.shape))  # an empty lesion (after binarization) has Dice 0 with everything
        volumes[-1][0, 0, 0] = 0.2
        occ = lesionset.LesionSet.from_volumes(volumes, grid, SPACE)
        result = lesionset.pairwise_dice(occ)
        binary = [v.reshape(-1) >= 0.5 for v in volumes]
        brute = []
        for a in range(len(binary)):
            for b in range(a + 1, len(binary)):
                total = binary[a].sum() + binary[b].sum()
                brute.append(2.0 * np.logical_and(binary[a], binary[b]).sum() / total if total else 0.0)
        np.testing.assert_allclose(result.condensed, brute, atol=1e-12)
        self.assertEqual(int(result.histogram.sum()), len(brute))
        self.assertEqual(result.max_overlap_count, int(np.sum(binary, axis=0).max()))
        self.assertLessEqual(result.n_candidates, len(brute))
        self.assertAlmostEqual(result.mean, float(np.mean(brute)))
        square = lesionset.dice_matrix(result.condensed, len(volumes))
        self.assertAlmostEqual(square[2, 5], square[5, 2])
        self.assertEqual(square[3, 3], 1.0)

    def test_disjoint_boxes_are_not_computed(self):
        grid = small_grid()
        occ = lesionset.LesionSet.from_volumes(boxes(grid, [(0, 2, 0, 2, 0, 2), (6, 8, 6, 8, 5, 7), (0, 2, 0, 2, 0, 1)]), grid, SPACE)
        result = lesionset.pairwise_dice(occ)
        self.assertEqual(result.n_candidates, 1)
        np.testing.assert_allclose(result.condensed, [0.0, 2 * 4 / 12, 0.0])


class VolumeAndTallyTest(unittest.TestCase):
    def test_volume_summary_and_log_suggestion(self):
        summary = lesionset.volume_summary([1.0, 2.0, 3.0, 4.0, 100.0])
        self.assertEqual(summary["median"], 3.0)
        self.assertEqual(summary["iqr"], 2.0)
        self.assertGreater(summary["skew"], 2.0)
        self.assertTrue(summary["suggest_log"])
        self.assertFalse(lesionset.volume_summary([1.0, 2.0, 3.0])["suggest_log"])
        self.assertIsNone(lesionset.volume_summary([])["median"])

    def test_hemisphere_split_with_flipped_storage(self):
        for flipped in (False, True):
            grid = small_grid(flipped=flipped)
            ni = grid.shape[0]
            low, high = boxes(grid, [(0, 3, 0, 2, 0, 2)])[0], boxes(grid, [(ni - 3, ni, 0, 2, 0, 2)])[0]
            both = boxes(grid, [(0, ni, 0, 2, 0, 2)])[0]
            occ = lesionset.LesionSet.from_volumes([low, high, both], grid, SPACE)
            labels, counts, share = lesionset.hemisphere_split(occ)
            left, right = ("R", "L") if flipped else ("L", "R")  # voxel i = 0 is left (x < 0) unless stored right to left
            self.assertEqual(labels, [left, right, "bilateral"])
            self.assertEqual(counts, {"L": 1, "R": 1, "bilateral": 1})
            self.assertAlmostEqual(share[2], 0.5)

    def test_hemisphere_matches_the_intake_side_check(self):
        # Lesions crossing the midline by different amounts: the posted side is the traced side shown at intake
        grid = small_grid((20, 4, 4))  # x = 0 lies between i = 9 and i = 10
        volumes = boxes(grid, [(0, 10, 0, 2, 0, 2), (1, 11, 0, 2, 0, 2), (3, 11, 0, 2, 0, 2), (6, 14, 0, 2, 0, 2), (9, 19, 0, 2, 0, 2)])
        occ = lesionset.LesionSet.from_volumes(volumes, grid, SPACE)
        labels, _, share = lesionset.hemisphere_split(occ)
        self.assertEqual(labels, ["L", "L", "L", "bilateral", "R"])  # 90%, 7/8 = 87.5% left are "L" (>= 80%)
        for k, volume in enumerate(volumes):
            traced = lesionops.side_check(volume, grid.affine, None)
            self.assertAlmostEqual(share[k], traced["left_frac"])
            self.assertEqual(labels[k], traced["traced_side"] if traced["traced_side"] != "midline" else "bilateral")

    def test_dimensionality_from_mask(self):
        grid = small_grid()
        occ = lesionset.LesionSet.from_volumes(boxes(grid, [(1, 4, 1, 4, 2, 3), (1, 4, 1, 4, 2, 5)]), grid, SPACE)
        self.assertEqual(lesionset.dimensionality_tally(occ), {"2D": 1, "3D": 1})

    def test_multi2d_from_mask(self):
        # Two single-slice axial drawings on different levels (a case-report tracing): multi2D, as at intake
        grid = small_grid()
        drawings = np.zeros(grid.shape)
        drawings[1:4, 1:4, 2] = 1.0
        drawings[6:9, 5:8, 6] = 1.0
        occ = lesionset.LesionSet.from_volumes([drawings, *boxes(grid, [(1, 4, 1, 4, 2, 5)])], grid, SPACE)
        self.assertEqual(occ.dimensionality, [("multi2D", "mask"), ("3D", "mask")])
        self.assertEqual(lesionops.detect_dimensionality(drawings, grid.affine)["dimensionality"], "multi2D")
        self.assertEqual(lesionset.dimensionality_tally(occ), {"3D": 1, "multi2D": 1})
        summary = lesionset.summarize(occ)
        self.assertEqual(lesionset.posted_values(occ, summary)["sub-001"]["dimensionality"], "multi2D")
        self.assertEqual(lesionset.lesion_covariates(occ, None, None)["sub-001"]["dim_2d"], 1.0)


class BalanceTest(unittest.TestCase):
    def test_smd_formula(self):
        a, b = [1.0, 2.0, 3.0, 4.0], [2.0, 4.0, 6.0]
        expected = (np.mean(a) - np.mean(b)) / math.sqrt((np.var(a, ddof=1) + np.var(b, ddof=1)) / 2.0)
        self.assertAlmostEqual(lesionset.smd(a, b), expected)
        self.assertAlmostEqual(lesionset.smd(a, b), -1.5 / math.sqrt((5.0 / 3.0 + 4.0) / 2.0))
        # Binary: (p1 - p2) / sqrt((p1 (1 - p1) + p2 (1 - p2)) / 2)
        p1, p2 = 0.75, 0.25
        self.assertAlmostEqual(lesionset.smd([1, 1, 1, 0], [0, 0, 1, 0]), (p1 - p2) / math.sqrt((p1 * (1 - p1) + p2 * (1 - p2)) / 2))
        self.assertEqual(lesionset.smd([1.0, 1.0], [1.0]), 0.0)
        self.assertIsNone(lesionset.smd([], [1.0]))
        self.assertAlmostEqual(lesionset.smd([1.0, None, 3.0], [2.0, float("nan"), 2.0, 2.0]), 0.0)
        table = lesionset.balance({"a": {"volume_ml": 1.0}, "b": {"volume_ml": 9.0}, "c": {"volume_ml": 1.0}, "d": {"volume_ml": 2.0}}, ["a", "b"], ["c", "d"], names=("volume_ml",))
        self.assertTrue(table["volume_ml"]["flagged"])

    def test_matching_is_seeded_and_without_replacement(self):
        rng = np.random.default_rng(11)
        cases = {f"sub-c{k:02d}": {"volume_ml": float(v), "wm_frac": float(w)} for k, (v, w) in enumerate(zip(rng.lognormal(2.5, 0.6, 12), rng.uniform(0.2, 0.8, 12), strict=True))}
        controls = {f"sub-k{k:02d}": {"volume_ml": float(v), "wm_frac": float(w)} for k, (v, w) in enumerate(zip(rng.lognormal(2.0, 0.8, 40), rng.uniform(0.1, 0.9, 40), strict=True))}
        controls["sub-k99"] = {"volume_ml": None, "wm_frac": 0.5}
        first = lesionset.matched_subsample(cases, controls, ["volume_ml", "wm_frac"], seed=5)
        again = lesionset.matched_subsample(dict(reversed(list(cases.items()))), controls, ["volume_ml", "wm_frac"], seed=5)
        self.assertEqual(first, again)
        matched = [pair[1] for pair in first["pairs"]]
        self.assertEqual(len(matched), len(set(matched)))
        self.assertEqual(len(first["pairs"]), 12)
        self.assertEqual(first["missing"], ["sub-k99"])
        self.assertEqual(len(first["sids"]), 24)
        after = abs(first["smd_after"]["volume_ml"])
        self.assertLess(after, abs(first["smd_before"]["volume_ml"]))
        two = lesionset.matched_subsample(cases, controls, ["volume_ml", "wm_frac"], seed=5, ratio=2)
        self.assertEqual(len(two["pairs"]), 24)
        # One case, three controls: the nearest one is taken
        simple = lesionset.matched_subsample({"sub-a": {"x": 5.0}}, {"sub-b": {"x": 1.0}, "sub-c": {"x": 4.0}, "sub-d": {"x": 9.0}}, ["x"], seed=0)
        self.assertEqual(simple["pairs"][0][:2], ["sub-a", "sub-c"])
        self.assertEqual(simple["sids"], ["sub-a", "sub-c"])

    def test_subset_never_edits_include(self):
        tmp = tempfile.mkdtemp(prefix="lnm-lesionset-subset-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        study = Study.create(os.path.join(tmp, "s"))
        for sid in ("01", "02", "03"):
            study.add_subject(sid)
        before = [study.subject(sid)["include"] for sid in study.subject_ids]
        lesionset.add_subset(study, "matched-1", ["sub-01", "sub-03"])
        self.assertEqual(study.subsets, {"matched-1": ["sub-01", "sub-03"]})
        self.assertEqual([study.subject(sid)["include"] for sid in study.subject_ids], before)
        with self.assertRaises(ValueError):
            lesionset.add_subset(study, "x", ["sub-09"])


class TissueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-lesionset-tissue-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.grid = small_grid()
        shape = self.grid.shape
        self.gm = np.zeros(shape)
        self.wm = np.zeros(shape)
        self.csf = np.zeros(shape)
        self.gm[:6] = 0.8
        self.wm[:6] = 0.2
        self.wm[6:] = 0.6
        self.csf[6:] = 0.3

    def test_fractions_on_synthetic_priors(self):
        volumes = boxes(self.grid, [(0, 2, 0, 2, 0, 2), (4, 8, 0, 2, 0, 2), (10, 12, 0, 1, 0, 1)])
        volumes[2] *= 0.5  # fractional occupancy weights the priors
        occ = lesionset.LesionSet.from_volumes(volumes, self.grid, SPACE)
        priors = {"gm": self.gm, "wm": self.wm, "csf": self.csf, "space": "MNI152NLin2009cAsym"}
        result = lesionset.tissue_fractions(occ, priors)
        np.testing.assert_allclose(result.fractions["gm"], [0.8, 0.4, 0.0])
        np.testing.assert_allclose(result.fractions["wm"], [0.2, 0.4, 0.6])
        np.testing.assert_allclose(result.fractions["csf"], [0.0, 0.15, 0.3])
        np.testing.assert_allclose(result.outside, [0.0, 0.05, 0.1], atol=1e-12)

        # Prior files of the same family on a finer grid are resampled (mean probability per grid voxel)
        fine = Grid(tuple(2 * n for n in self.grid.shape), self.grid.affine @ np.diag([0.5, 0.5, 0.5, 1.0]) @ np.array([[1, 0, 0, -0.5], [0, 1, 0, -0.5], [0, 0, 1, -0.5], [0, 0, 0, 1.0]]))
        files = {}
        for tissue, data in (("gm", self.gm), ("wm", self.wm), ("csf", self.csf)):
            path = os.path.join(self.tmp, f"tpl-MNI152NLin2009cAsym_res-01_label-{tissue.upper()}_probseg.nii.gz")
            write_nifti(path, np.repeat(np.repeat(np.repeat(data, 2, 0), 2, 1), 2, 2).astype(np.float32), fine.affine)
            files[tissue] = path
        found = lesionset.find_priors(self.tmp, self.grid)
        self.assertEqual(found["space"], "MNI152NLin2009cAsym")
        self.assertEqual(found["gm"], files["gm"])
        from_files = lesionset.tissue_fractions(occ, found)
        np.testing.assert_allclose(from_files.fractions["wm"], result.fractions["wm"], atol=1e-6)

    def test_find_priors_prefers_the_lesion_family(self):
        # Two complete sets: NLin6 at the grid's 2 mm and 2009c at 1 mm; 2009c lesions use the 2009c set
        for space, res in (("MNI152NLin6Asym", "02"), ("MNI152NLin2009cAsym", "01")):
            for label in ("GM", "WM", "CSF"):
                open(os.path.join(self.tmp, f"tpl-{space}_res-{res}_label-{label}_probseg.nii.gz"), "wb").close()
        self.assertEqual(lesionset.find_priors(self.tmp, self.grid, SPACE)["space"], "MNI152NLin2009cAsym")
        self.assertEqual(lesionset.find_priors(self.tmp, self.grid, "MNI152NLin6Asym")["space"], "MNI152NLin6Asym")
        self.assertEqual(lesionset.find_priors(self.tmp, self.grid)["space"], "MNI152NLin6Asym")  # no space: the grid's resolution

    def test_family_mismatch_is_refused(self):
        occ = lesionset.LesionSet.from_volumes(boxes(self.grid, [(0, 2, 0, 2, 0, 2)]), self.grid, SPACE)
        with self.assertRaises(lesionset.PriorFamilyError) as raised:
            lesionset.tissue_fractions(occ, {"gm": self.gm, "wm": self.wm, "csf": self.csf, "space": "MNI152NLin6Asym"})
        self.assertIn("fsl6", raised.exception.reason)
        self.assertIn("2009", raised.exception.reason)
        self.assertIn("warp", raised.exception.reason)
        with self.assertRaises(lesionset.PriorFamilyError):
            lesionset.tissue_fractions(occ, {"gm": self.gm, "wm": self.wm, "csf": self.csf, "space": None})
        summary = lesionset.summarize(occ, {"gm": self.gm, "wm": self.wm, "csf": self.csf, "space": "MNI152NLin6Asym"})
        self.assertIsNone(summary["tissue"])
        self.assertIn("fsl6", summary["tissue_error"])

    def test_warp_priors_records_the_transform(self):
        try:
            import SimpleITK as sitk
        except ImportError:
            self.skipTest("SimpleITK is not available")
        src = GRID_BY_ID["TF2009-2mm"]
        dst = GRID_BY_ID["NLin6-2mm"]
        priors = {"space": "MNI152NLin2009cAsym"}
        for tissue, value in (("gm", 0.7), ("wm", 0.2), ("csf", 0.1)):
            path = os.path.join(self.tmp, f"tpl-MNI152NLin2009cAsym_res-02_label-{tissue.upper()}_probseg.nii.gz")
            write_nifti(path, np.full(src.shape, value, dtype=np.float32), src.affine)
            priors[tissue] = path
        reference = os.path.join(self.tmp, "tpl-MNI152NLin6Asym_res-02_desc-brain_mask.nii.gz")
        write_nifti(reference, np.ones(dst.shape, dtype=np.uint8), dst.affine)
        xfm = os.path.join(self.tmp, "tpl-MNI152NLin6Asym_from-MNI152NLin2009cAsym_mode-image_xfm.h5")
        sitk.WriteTransform(sitk.Transform(3, sitk.sitkIdentity), xfm)
        warped = lesionset.warp_priors(priors, xfm, reference, os.path.join(self.tmp, "warped"))
        self.assertEqual(warped["space"], "MNI152NLin6Asym")
        self.assertEqual(len(warped["warped"]), 3)
        self.assertEqual(warped["warped"][0]["tissue"], "gm")
        self.assertIn("transform_sha256", warped["warped"][0])
        grid = dst.grid
        volume = np.zeros(grid.shape)
        volume[40:45, 50:55, 40:45] = 1
        occ = lesionset.LesionSet.from_volumes([volume], grid, "MNI152NLin6Asym")
        result = lesionset.tissue_fractions(occ, warped)
        self.assertAlmostEqual(float(result.fractions["gm"][0]), 0.7, places=5)
        self.assertEqual(result.priors["warped"][0]["source"], os.path.basename(priors["gm"]))


class StudyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-lesionset-study-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.folder = os.path.join(self.tmp, "study")
        self.study = Study.create(self.folder, space=SPACE)
        self.spec = GRID_BY_ID["TF2009-2mm"]
        fine = GRID_BY_ID["TF2009-1mm"]
        os.makedirs(os.path.join(self.folder, "lesions"), exist_ok=True)

        def lesion(name, grid, box, dtype=np.uint8, value=1):
            data = np.zeros(grid.shape, dtype=dtype)
            data[box] = value
            path = os.path.join(self.folder, "lesions", name)
            write_nifti(path, data, grid.affine)
            return f"lesions/{name}"

        left = (slice(20, 30), slice(50, 60), slice(40, 46))  # x < 0 on TF2009-2mm (RAS)
        right = (slice(70, 80), slice(50, 60), slice(40, 46))
        self.study.add_subject("01", group="case", lesion=lesion("a.nii.gz", self.spec, left), source_type="case_report")
        self.study.add_subject("02", group="case", lesion=lesion("b.nii.gz", self.spec, right, value=2))
        # A 1 mm lesion (2 x 2 x 2 voxels = 8 uL) on the TemplateFlow 1 mm grid: resampled exactly onto 2 mm
        self.study.add_subject("03", group="control", lesion=lesion("c.nii.gz", fine, (slice(40, 42), slice(50, 52), slice(60, 61))))
        self.study.add_subject("04", group="control", lesion=lesion("d.nii.gz", self.spec, (slice(20, 28), slice(50, 58), slice(40, 46))), dimensionality="3D")
        self.study.add_subject("05", group="case", include=0, lesion=lesion("e.nii.gz", self.spec, left))
        self.study.add_subject("06", group="case", lesion="lesions/missing.nii.gz")
        nlin6 = GRID_BY_ID["NLin6-2mm"]
        self.study.add_subject("07", group="case", lesion=lesion("f.nii.gz", nlin6, left))
        self.study.review_required = False  # a case_report row would otherwise require approved reviews
        self.study.save()

    def test_load_and_summarize(self):
        occ = lesionset.load_occupancies(self.study)
        self.assertEqual(occ.sids, ["sub-01", "sub-02", "sub-03", "sub-04"])
        self.assertEqual(occ.excluded["include=0"], ["sub-05"])
        self.assertEqual(occ.excluded[lesionset.EXCLUDED_MISSING_FILE], ["sub-06"])
        self.assertEqual([reason for reason, sids in occ.excluded.items() if "sub-07" in sids], ["space MNI152NLin6Asym is not in the 2009 family of MNI152NLin2009cAsym"])
        np.testing.assert_allclose(occ.volumes_ml, [10 * 10 * 6 * 8 / 1000, 10 * 10 * 6 * 8 / 1000, 4 / 1000, 8 * 8 * 6 * 8 / 1000])
        self.assertEqual(occ.dimensionality, [("3D", "mask"), ("3D", "mask"), ("2D", "mask"), ("3D", "column")])
        reviewed = lesionset.load_occupancies(self.study, require_review=True)
        self.assertEqual(len(reviewed), 0)
        self.assertEqual(reviewed.excluded["review_status != approved"], ["sub-01", "sub-02", "sub-03", "sub-04", "sub-06", "sub-07"])
        cases = lesionset.load_occupancies(self.study, groups="case")
        self.assertEqual(cases.sids, ["sub-01", "sub-02"])
        summary = lesionset.summarize(occ)
        self.assertEqual(summary["hemisphere"], {"L": 3, "R": 1, "bilateral": 0})  # sub-03 lies at x = -56 mm
        self.assertEqual(summary["n_cases"], 2)
        self.assertEqual(summary["source_type"], {"case_report": 1, "unknown": 3})
        self.assertEqual(summary["dimensionality"], {"2D": 1, "3D": 3})
        self.assertIn("volume_ml", summary["balance"])
        self.assertEqual(summary["overlap"]["legend"], "max 2/4 (50%)")
        keys = [item["key"] for item in summary["literature"]]
        self.assertEqual(keys[:2], ["set_size", "dice"])
        self.assertTrue(summary["literature"][0]["text"].startswith("This set: 2 lesions. Sets of \u2265 10 heterogeneous lesions approach the degree map"))
        json.dumps(summary, allow_nan=False)  # strict JSON

    def test_corrupt_deflate_stream_is_an_unreadable_lesion(self):
        """A .nii.gz with a valid gzip header but a damaged deflate stream (gzip raises ``zlib.error``, which is no
        OSError/ValueError/EOFError) is left out as unreadable; the other lesions are still loaded.
        """
        import zlib

        from LesionNetworkMappingLib import nifti

        path = os.path.join(self.folder, "lesions", "b.nii.gz")  # sub-02
        with open(path, "wb") as f:
            f.write(b"\x1f\x8b\x08\x00" + b"garbage-not-a-nifti" * 10)
        for reader in (nifti.read_nifti_header, nifti.read_nifti, nifti.read_raw_header):
            with self.assertRaises(ValueError, msg=reader.__name__) as raised:
                reader(path)
            self.assertIsInstance(raised.exception, zlib.error)  # callers that catch zlib.error keep working
            self.assertIn("b.nii.gz", str(raised.exception))
        occ = lesionset.load_occupancies(self.study)
        self.assertEqual(occ.sids, ["sub-01", "sub-03", "sub-04"])
        self.assertEqual(occ.excluded[lesionset.EXCLUDED_UNREADABLE], ["sub-02"])

    def test_run_posts_through_the_inbox(self):
        participants = os.path.join(self.folder, "participants.tsv")
        with open(participants, "rb") as f:
            before = f.read()
        priors = {"gm": np.full(self.spec.shape, 0.5), "wm": np.full(self.spec.shape, 0.25), "csf": np.full(self.spec.shape, 0.25), "space": SPACE}
        result = lesionset.run(self.study, priors=priors, match={"name": "matched", "covariates": ["volume_ml"], "seed": 1})
        with open(participants, "rb") as f:
            self.assertEqual(f.read(), before)  # participants.tsv is never written directly
        self.assertTrue(os.path.isfile(os.path.join(self.folder, "derivatives", "lesions", "group_space-MNI152NLin2009cAsym_desc-overlapCount_map.nii.gz")))
        with open(os.path.join(self.folder, "derivatives", "lnm", "lesionset.json"), encoding="utf-8") as f:
            content = json.load(f)
        self.assertEqual(content["format"], "slicer-lnm-lesionset")
        self.assertEqual(content["matched"]["name"], "matched")
        self.assertEqual(len(content["matched"]["pairs"]), 2)
        self.assertEqual(content["summary"]["tissue"]["mean_pct"]["gm"], 50.0)
        self.assertIsNotNone(result["inbox"])
        study = Study.open(self.folder)
        study.apply_inbox()
        self.assertEqual(study.inbox_errors, [])
        self.assertEqual(study.value("sub-01", "hemisphere"), "L")
        self.assertEqual(study.value("sub-02", "hemisphere"), "R")
        self.assertEqual(study.value("sub-01", "gm_frac", float), 0.5)
        self.assertEqual(study.value("sub-03", "dimensionality"), "2D")
        self.assertEqual(study.value("sub-04", "dimensionality"), "3D")  # the column value is kept, not posted
        self.assertIsNone(study.value("sub-05", "hemisphere"))
        self.assertEqual(study.value("sub-05", "include"), "0")

    def test_run_posts_multi2d_and_uses_same_family_priors(self):
        folder = os.path.join(self.tmp, "multi")
        study = Study.create(folder, space=SPACE)
        os.makedirs(os.path.join(folder, "lesions"), exist_ok=True)
        data = np.zeros(self.spec.shape, dtype=np.uint8)
        data[20:30, 50:60, 40] = 1  # two axial drawings at z = 40 and z = 50
        data[22:28, 52:58, 50] = 1
        write_nifti(os.path.join(folder, "lesions", "a.nii.gz"), data, self.spec.affine)
        study.add_subject("01", group="case", lesion="lesions/a.nii.gz")
        study.review_required = False
        study.save()
        priors = os.path.join(self.tmp, "priors")
        os.makedirs(priors)
        for space, res, gm in (("MNI152NLin6Asym", "02", 0.1), ("MNI152NLin2009cAsym", "01", 0.6)):
            grid = self.spec if res == "02" else GRID_BY_ID["TF2009-1mm"]
            for label, value in (("GM", gm), ("WM", 0.9 - gm), ("CSF", 0.1)):
                write_nifti(os.path.join(priors, f"tpl-{space}_res-{res}_label-{label}_probseg.nii.gz"), np.full(grid.shape, value, dtype=np.float32), grid.affine)
        result = lesionset.run(study, priors=priors)
        summary = result["summary"]
        self.assertIsNone(summary["tissue_error"])
        self.assertEqual(summary["tissue"]["priors"]["space"], SPACE)
        self.assertEqual(summary["dimensionality"], {"multi2D": 1})
        reopened = Study.open(folder)
        reopened.apply_inbox()
        self.assertEqual(reopened.value("sub-01", "dimensionality"), "multi2D")
        self.assertAlmostEqual(reopened.value("sub-01", "gm_frac", float), 0.6, places=5)

    def test_command_line(self):
        out = os.path.join(self.tmp, "out")
        process = subprocess.run(
            [sys.executable, "-m", "LesionNetworkMappingLib.lesionset", "--study", self.folder, "--out", out, "--no-post", "--groups", "case,control"],
            cwd=MODULE_DIR,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        events = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual(events[0]["event"], "start")
        self.assertEqual(events[-1]["event"], "done")
        self.assertTrue(any(event["event"] == "warning" and event.get("code") == "excluded" for event in events))
        result = next(event for event in events if event["event"] == "result" and event["kind"] == "lesionset")
        self.assertEqual(result["legend"], "max 2/4 (50%)")
        self.assertIsNone(result["inbox"])
        self.assertEqual(sorted(os.listdir(out)), sorted(["lesionset.json", *[os.path.basename(p) for p in lesionset.overlap_paths(out, SPACE)], *[os.path.basename(p)[: -len(".nii.gz")] + ".json" for p in lesionset.overlap_paths(out, SPACE)]]))
        self.assertFalse(os.path.isdir(os.path.join(self.folder, ".studio", "inbox")) and os.listdir(os.path.join(self.folder, ".studio", "inbox")))
        bad = subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.lesionset", "--study", self.tmp], cwd=MODULE_DIR, capture_output=True, text=True, timeout=120, check=False)
        self.assertEqual(bad.returncode, 2)


class LiteratureTest(unittest.TestCase):
    def test_strings(self):
        texts = lesionset.literature_context({"n": 23, "n_cases": 23, "dice": {"mean": 0.16}, "volumes": {"skew": 2.4, "suggest_log": True}, "dimensionality": {"2D": 4, "3D": 19}})
        by_key = {item["key"]: item for item in texts}
        self.assertEqual(by_key["set_size"]["text"], "This set: 23 lesions. Sets of \u2265 10 heterogeneous lesions approach the degree map (r > 0.44; 20\u201325 lesions r > 0.62; van den Heuvel 2026)")
        self.assertEqual(by_key["dice"]["text"], "This set: mean pairwise Dice 0.16. At mean pairwise Dice 0.16, conjunction was significant for 64% of random sets (van den Heuvel 2026)")
        self.assertIn("log(volume)", by_key["volume_skew"]["text"])
        self.assertIn("Cotovio 2022", by_key["dimensionality"]["text"])
        low = lesionset.literature_context({"n": 5, "dice": {"mean": 0.02}})
        self.assertIn("0.08", low[1]["text"])
        # The nearest reference: 0.22 is nearer 0.25 than 0.16
        self.assertIn("At mean pairwise Dice 0.25, conjunction was significant for 97%", lesionset.literature_context({"n": 5, "dice": {"mean": 0.22}})[1]["text"])
        self.assertIn("At mean pairwise Dice 0.16", lesionset.literature_context({"n": 5, "dice": {"mean": 0.2}})[1]["text"])


if __name__ == "__main__":
    unittest.main()

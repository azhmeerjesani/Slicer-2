"""Tests of coords.py: the coordinate parsers (CSV, Sleuth, NiMARE JSON), the Talairach to MNI conversion on a
published reference point and its round trip, explicit sphere radius vs diameter, study-level union seeds, the
random-coordinate null (foci per study kept, gray matter only, the same-family rule for the GM mask), the atrophy
rules, the stimulation seeds and R-map helpers, and seeds mapped by the same engine (cohortmaps) as lesions.

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingCoordsTest.py -v
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

from LesionNetworkMappingLib import cohortmaps, connectome, coords, nifti
from LesionNetworkMappingLib.geometry import Grid
from LesionNetworkMappingLib.study import read_tsv

GRID_2MM = Grid((20, 24, 20), np.array([[2.0, 0, 0, -19.0], [0, 2.0, 0, -23.0], [0, 0, 2.0, -19.0], [0, 0, 0, 1.0]]))


def _absolute_strings(value):
    """The strings of a JSON value that are absolute local paths (shared records hold none, SPEC 9.7 rule 10)."""
    if isinstance(value, dict):
        return [item for child in value.values() for item in _absolute_strings(child)]
    if isinstance(value, list):
        return [item for child in value for item in _absolute_strings(child)]
    return [value] if isinstance(value, str) and os.path.isabs(value) else []


def _env():
    return dict(os.environ, PYTHONPATH=MODULE_DIR + os.pathsep + os.environ.get("PYTHONPATH", ""))


def _events(stdout):
    return [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]


class CoordsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-coords-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path


class ParserTest(CoordsTestCase):
    def test_csv_header_and_columns(self):
        path = self.write("foci.csv", "study,x,y,z,space,radius\nA,-44,31,27,Talairach,\nA,10,-20,30,MNI,6\nB,0,0,0,,\n")
        foci = coords.read_csv(path)
        self.assertEqual(len(foci), 3)
        self.assertEqual([f.study for f in foci], ["A", "A", "B"])
        self.assertEqual([f.space for f in foci], ["TAL", "MNI", "MNI"])
        self.assertIsNone(foci[0].radius_mm)
        self.assertEqual(foci[1].radius_mm, 6.0)
        self.assertEqual(list(coords.group_by_study(foci)), ["A", "B"])

    def test_csv_headerless_and_tsv(self):
        foci = coords.read_csv(self.write("plain.txt", "1 2 3\n4 5 6\n"))
        self.assertEqual([f.xyz for f in foci], [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)])
        self.assertEqual({f.study for f in foci}, {"plain"})
        foci = coords.read_csv(self.write("t.tsv", "x\ty\tz\tdiameter\n1\t2\t3\t6\n"))
        self.assertEqual(foci[0].radius_mm, 3.0)  # a diameter column is read as a diameter

    def test_csv_errors(self):
        with self.assertRaisesRegex(ValueError, "radius and a diameter"):
            coords.read_csv(self.write("both.csv", "x,y,z,radius,diameter\n1,2,3,4,8\n"))
        with self.assertRaisesRegex(ValueError, "no z column"):
            coords.read_csv(self.write("noz.csv", "x,y\n1,2\n"))
        with self.assertRaisesRegex(ValueError, "Unknown coordinate space"):
            coords.read_csv(self.write("bad.csv", "x,y,z,space\n1,2,3,Colin\n"))

    def test_csv_row_with_more_fields_than_header(self):
        # Decimal commas split each number in two: the row must not be cut to the header and read as other numbers
        # (it was read as (-33, 5, -57), and with a radius column the 25 became the sphere radius).
        with self.assertRaisesRegex(ValueError, r"row 2 has 7 values for 4 columns.*Decimal commas"):
            coords.read_foci(self.write("de.csv", "study,x,y,z\nMüller 2020,-33,5,-57,25,11,75\n"))
        with self.assertRaisesRegex(ValueError, "row 3 has 7 values for 4 columns"):
            coords.read_csv(self.write("de_r.csv", "x,y,z,radius\n1,2,3,4\n-33,5,-57,25,11,75,4\n"))
        with self.assertRaisesRegex(ValueError, "row 2 has 5 values for 4 columns"):
            coords.read_csv(self.write("extra.csv", "study,x,y,z\nA,10,20,30,99\n"))
        with self.assertRaisesRegex(ValueError, "row 2 has 4 values for 3 columns"):
            coords.read_csv(self.write("plain_extra.txt", "1 2 3\n4 5 6 7\n"))  # header-less: row 1 sets the columns
        # Trailing empty fields (a spreadsheet's "1,2,3,") and short rows stay accepted.
        foci = coords.read_csv(self.write("trailing.csv", "study,x,y,z\nA,-33.5,-57.25,11.75,,\nB,1,2,3\n"))
        self.assertEqual([f.xyz for f in foci], [(-33.5, -57.25, 11.75), (1.0, 2.0, 3.0)])
        self.assertEqual(coords.read_csv(self.write("short.csv", "x,y,z,radius\n1,2,3\n"))[0].radius_mm, None)

    def test_sleuth(self):
        text = ("// Reference=Talairach\n// Smith et al., 2001: Delusions\n// Subjects=12\n-44 31 27\n20\t-32\t14\n\n"
                "// Jones, 2005\n// Subjects=10\n1 2 3\n\n// Reference=MNI\n// Smith et al., 2001: Delusions\n// Subjects=9\n4,5,6\n")
        foci = coords.read_sleuth(self.write("sleuth.txt", text))
        groups = coords.group_by_study(foci)
        self.assertEqual(list(groups), ["Smith et al., 2001: Delusions", "Jones, 2005", "Smith et al., 2001: Delusions #2"])
        self.assertEqual([len(v) for v in groups.values()], [2, 1, 1])
        self.assertEqual([f.space for f in foci], ["TAL", "TAL", "TAL", "MNI"])
        self.assertEqual(foci[0].subjects, 12)
        self.assertEqual(foci[-1].subjects, 9)
        self.assertEqual(coords.read_foci(os.path.join(self.tmp, "sleuth.txt")), foci)  # detected by content
        with self.assertRaisesRegex(ValueError, "Reference"):
            coords.read_sleuth(self.write("noref.txt", "// a study\n1 2 3\n"))
        # "// Subjects=N" before the experiment's name line belongs to that experiment
        first = coords.read_sleuth(self.write("first.txt", "// Reference=MNI\n// Subjects=12\n// A\n1 2 3\n\n// Subjects=20\n// B\n4 5 6\n\n// C\n7 8 9\n"))
        self.assertEqual([(f.study, f.subjects) for f in first], [("A", 12), ("B", 20), ("C", None)])

    def test_nimare_json(self):
        dataset = {"pain": {"contrasts": {"1": {"coords": {"space": "MNI", "x": [1, 2], "y": [3, 4], "z": [5, 6]}, "metadata": {"sample_sizes": [20]}}}},
                   "itch": {"contrasts": {"a": {"coords": {"space": "talairach", "x": [7], "y": [8], "z": [9]}}, "b": {"coords": {"x": [], "y": [], "z": []}}}}}
        path = self.write("ds.json", json.dumps(dataset))
        foci = coords.read_foci(path)
        self.assertEqual([f.study for f in foci], ["pain-1", "pain-1", "itch-a"])
        self.assertEqual([f.space for f in foci], ["MNI", "MNI", "TAL"])
        self.assertEqual(foci[0].subjects, 20)


class TalairachTest(unittest.TestCase):
    def test_published_reference_point(self):
        # NiMARE's tal2mni test value (inverse Lancaster 2007 "pooled" icbm2tal): (-44, 31, 27) TAL -> MNI
        np.testing.assert_allclose(coords.tal2mni([-44, 31, 27]), [-45.83997568, 35.97904559, 23.55194326], atol=1e-6)
        # The icbm2tal origin offset: MNI (0, 0, 0) -> TAL = the matrices' translation column
        for name, matrix in coords.LANCASTER_MATRICES.items():
            np.testing.assert_allclose(coords.mni2tal([0, 0, 0], name), [row[3] for row in matrix[:3]], atol=1e-12)

    def test_round_trip(self):
        rng = np.random.default_rng(0)
        points = rng.uniform(-70, 70, size=(50, 3))
        for name in coords.LANCASTER_MATRICES:
            np.testing.assert_allclose(coords.mni2tal(coords.tal2mni(points, name), name), points, atol=1e-9)
            np.testing.assert_allclose(coords.tal2mni(coords.mni2tal(points, name), name), points, atol=1e-9)
        with self.assertRaises(ValueError):
            coords.tal2mni([0, 0, 0], "afni")

    def test_conversion_recorded(self):
        foci = [coords.Focus(-44, 31, 27, "A", "TAL"), coords.Focus(1, 2, 3, "B", "MNI")]
        mni, record = coords.to_mni(foci)
        self.assertEqual([f.space for f in mni], ["MNI", "MNI"])
        self.assertEqual(mni[1].xyz, (1.0, 2.0, 3.0))
        self.assertEqual(record["converted"], 1)
        self.assertEqual(record["studies"], ["A"])
        self.assertIn("doi:10.1002/hbm.20345", record["citation"])
        self.assertIn("Lancaster", coords.conversion_text(record))
        self.assertEqual(coords.to_mni(mni)[1]["converted"], 0)


class SphereTest(unittest.TestCase):
    def test_radius_or_diameter(self):
        self.assertEqual(coords.sphere_radius(diameter_mm=6)[0], 3.0)
        self.assertEqual(coords.sphere_radius()[0], coords.DEFAULT_RADIUS_MM)
        self.assertEqual(coords.sphere_radius(diameter_mm=6)[1], {"radius_mm": 3.0, "diameter_mm": 6.0, "given": "diameter"})
        self.assertIn("6 mm diameter", coords.sphere_text(coords.sphere_radius(3)[1]))
        with self.assertRaisesRegex(ValueError, "not both"):
            coords.sphere_radius(3, 6)
        with self.assertRaises(ValueError):
            coords.sphere_radius(-1)
        focus = [coords.Focus(0.5, -0.5, 1.0)]
        a, _ = coords.spheres(focus, GRID_2MM, radius_mm=3)
        b, _ = coords.spheres(focus, GRID_2MM, diameter_mm=6)
        np.testing.assert_array_equal(a, b)
        with self.assertRaises(ValueError):
            coords.spheres(focus, GRID_2MM, radius_mm=3, diameter_mm=6)

    def test_fractional_volume(self):
        grid = Grid((30, 30, 30), np.array([[1.0, 0, 0, -15], [0, 1.0, 0, -15], [0, 0, 1.0, -15], [0, 0, 0, 1]]))
        for radius in (2.0, 4.0, 6.0):
            volume, _ = coords.spheres([coords.Focus(0.3, 0.1, -0.2)], grid, radius_mm=radius)
            self.assertLessEqual(volume.max(), 1.0)
            self.assertAlmostEqual(volume.sum() / (4.0 / 3.0 * math.pi * radius**3), 1.0, delta=0.04)
        # An oblique (rotated) grid: the same volume through the affine
        angle = math.radians(30)
        rotation = np.array([[math.cos(angle), -math.sin(angle), 0], [math.sin(angle), math.cos(angle), 0], [0, 0, 1]]) * 1.5
        affine = np.eye(4)
        affine[:3, :3] = rotation
        affine[:3, 3] = -rotation @ np.array([15, 15, 15])
        volume, _ = coords.spheres([coords.Focus(0, 0, 0)], Grid((30, 30, 30), affine), radius_mm=5)
        self.assertAlmostEqual(volume.sum() * 1.5**3 / (4.0 / 3.0 * math.pi * 125), 1.0, delta=0.04)

    def test_talairach_refused_and_focus_radius(self):
        with self.assertRaisesRegex(ValueError, "converted"):
            coords.spheres([coords.Focus(0, 0, 0, space="TAL")], GRID_2MM)
        small, record = coords.spheres([coords.Focus(0, 0, 0, radius_mm=2)], GRID_2MM, radius_mm=6)
        large, _ = coords.spheres([coords.Focus(0, 0, 0)], GRID_2MM, radius_mm=6)
        self.assertLess(small.sum(), large.sum())
        self.assertEqual(record["per_focus_radii_mm"], [2.0])

    def test_study_union_seeds(self):
        groups = {"A": [coords.Focus(-10, 0, 0, "A"), coords.Focus(-10, 1, 0, "A")], "B": [coords.Focus(10, 0, 0, "B")]}
        names, weights, record = coords.study_union_seeds(groups, GRID_2MM, radius_mm=4)
        self.assertEqual(names, ["A", "B"])
        self.assertEqual(weights.shape, (2, GRID_2MM.n_voxels))
        self.assertLessEqual(weights.max(), 1.0)  # overlapping spheres of one study are a union, not a sum
        single, _ = coords.spheres(groups["A"][:1], GRID_2MM, 4)
        self.assertGreater(weights[0].sum(), single.sum())
        self.assertLess(weights[0].sum(), 2 * single.sum())
        indices = np.flatnonzero(np.ones(GRID_2MM.shape, bool).reshape(-1))[::3]
        _names, masked, _ = coords.study_union_seeds(groups, GRID_2MM, radius_mm=4, mask_indices=indices)
        np.testing.assert_array_equal(masked, weights[:, indices])
        self.assertEqual(record["radius_mm"], 4.0)


class NullTest(unittest.TestCase):
    def setUp(self):
        self.gm = np.zeros(GRID_2MM.shape, dtype=bool)
        self.gm[3:17, 4:20, 5:15] = True
        self.groups = {"A": [coords.Focus(-6, 0, 0, "A"), coords.Focus(-4, 2, 0, "A")], "B": [coords.Focus(-6, 1, 0, "B")], "C": [coords.Focus(8, 8, 4, "C")] * 3}

    def test_sets_keep_foci_per_study(self):
        sets, study_index, names = coords.random_coordinate_sets(self.groups, self.gm, GRID_2MM, k=20, seed=4)
        self.assertEqual(names, ["A", "B", "C"])
        self.assertEqual(sets.shape, (20, 6, 3))
        self.assertEqual(np.bincount(study_index).tolist(), [2, 1, 3])
        voxels = np.rint(GRID_2MM.world_to_voxel(sets.reshape(-1, 3))).astype(int)
        self.assertTrue(self.gm[tuple(voxels.T)].all())  # every null focus is in gray matter
        again, _, _ = coords.random_coordinate_sets(self.groups, self.gm, GRID_2MM, k=20, seed=4)
        np.testing.assert_array_equal(sets, again)
        other, _, _ = coords.random_coordinate_sets(self.groups, self.gm, GRID_2MM, k=20, seed=5)
        self.assertFalse(np.array_equal(sets, other))

    def test_null_result(self):
        result = coords.random_coordinate_null(self.groups, self.gm, k=30, seed=1, grid=GRID_2MM, radius_mm=4, keep_maps=True, columns_per_call=7)
        self.assertEqual(result["counts"], {"A": 2, "B": 1, "C": 3})
        self.assertEqual(result["null_peaks"].shape, (30,))
        self.assertEqual(result["null_maps"].shape, (30, GRID_2MM.n_voxels))
        # A and B overlap near (-6, 0, 0): 2 of 3 studies; random foci rarely do better
        self.assertAlmostEqual(result["observed_peak"], 200.0 / 3.0, places=4)
        self.assertGreater(result["percentile"], 50.0)
        self.assertTrue(1.0 / 31 <= result["p_peak"] <= 1.0)
        self.assertTrue(np.all((result["voxel_p"] >= 1.0 / 31) & (result["voxel_p"] <= 1.0)))
        np.testing.assert_allclose(result["null_mean"], result["null_maps"].mean(axis=0), atol=1e-4)

    def test_network_fn_sees_one_row_per_study(self):
        calls = []

        def network_fn(weights):
            calls.append(weights.shape[0])
            return weights > 0.5

        coords.random_coordinate_null(self.groups, self.gm, k=5, seed=0, grid=GRID_2MM, network_fn=network_fn, columns_per_call=6)
        self.assertEqual(calls[0], 3)  # the observed studies
        self.assertTrue(all(n % 3 == 0 for n in calls[1:]))
        self.assertEqual(sum(calls[1:]), 15)

    def test_null_spheres_keep_focus_radius(self):
        # A focus radius from the file (8 mm) applies to the null spheres too, not only to the observed ones
        gm = np.zeros(GRID_2MM.shape, dtype=bool)
        gm[9:11, 11:13, 9:11] = True  # central voxels: every null sphere lies inside the grid
        groups = {"A": [coords.Focus(1, 1, 1, "A", radius_mm=8)], "B": [coords.Focus(-1, -1, -1, "B")]}
        rows = []

        def network_fn(weights):
            rows.append(np.asarray(weights).copy())
            return coords.seed_overlap_fn(weights)

        result = coords.random_coordinate_null(groups, gm, k=6, seed=2, grid=GRID_2MM, network_fn=network_fn, radius_mm=4, columns_per_call=4)
        observed, null = rows[0], np.concatenate(rows[1:]).reshape(6, 2, -1)
        for study in range(2):
            np.testing.assert_allclose(null[:, study].sum(axis=1), observed[study].sum(), rtol=0.05)
        self.assertGreater(observed[0].sum(), 4 * observed[1].sum())  # 8 mm against 4 mm
        self.assertEqual(result["sphere"]["radius_mm"], 4.0)
        self.assertEqual(result["sphere"]["per_focus_radii_mm"], [8.0])
        np.testing.assert_array_equal(coords.focus_radii(groups["A"] + groups["B"], radius_mm=4), [8.0, 4.0])

    def test_same_family_enforced(self):
        probseg = np.where(self.gm, 0.9, 0.2).astype(np.float32)
        path = os.path.join(tempfile.mkdtemp(prefix="lnm-coords-gm-"), "tpl-MNI152NLin2009cAsym_res-02_label-GM_probseg.nii.gz")
        try:
            nifti.write_nifti(path, probseg, GRID_2MM.affine, dtype=np.float32)
            mask = coords.gm_mask_for(path, GRID_2MM, target_space="MNI152NLin2009cAsym")
            np.testing.assert_array_equal(mask, self.gm)
            self.assertTrue(coords.gm_mask_for(path, GRID_2MM, target_space="MNI152NLin2009aSym").any())  # same family
            with self.assertRaisesRegex(coords.FamilyError, "family"):
                coords.gm_mask_for(path, GRID_2MM, target_space="MNI152NLin6Asym")
            with self.assertRaises(coords.FamilyError):
                coords.gm_mask_for(probseg, GRID_2MM, gm_space=None, target_space="MNI152NLin6Asym")  # unknown GM space
            with self.assertRaises(coords.FamilyError):
                coords.gm_mask_for(path, GRID_2MM, target_space=None)  # unknown grid space
        finally:
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)


class AtrophyTest(unittest.TestCase):
    def test_rules(self):
        w = np.array([-3.0, -2.6, -2.2, -1.8, -1.2, 0.0, 1.0, 2.5, np.nan]).reshape(3, 3, 1)
        counts = {}
        for rule in ("w<-2", "w<-1.5", "w<-2.5"):
            seed, record = coords.atrophy_seed(w, rule)
            counts[rule] = int(seed.sum())
            self.assertEqual(record["rule"], rule)
            self.assertTrue(set(np.unique(seed)) <= {0.0, 1.0})
        self.assertEqual(counts, {"w<-2": 3, "w<-1.5": 4, "w<-2.5": 2})
        self.assertEqual(coords.atrophy_rule(-2), "w<-2")
        self.assertEqual(coords.atrophy_rule("w < −2"), "w<-2")  # noqa: RUF001 (a minus sign, as typed in papers)
        with self.assertRaises(ValueError):
            coords.atrophy_rule("w<-3")

    def test_top5_and_weighted(self):
        rng = np.random.default_rng(1)
        w = rng.normal(size=(20, 20, 10))
        seed, record = coords.atrophy_seed(w, "top5%")
        self.assertAlmostEqual(seed.sum() / w.size, 0.05, delta=0.002)
        self.assertTrue(np.all(w[seed > 0] <= record["cut"]))
        weighted, _ = coords.atrophy_seed(w, "w<-2", weighted=True)
        self.assertAlmostEqual(float(weighted.max()), 1.0)
        self.assertEqual(int((weighted > 0).sum()), int((w < -2).sum()))
        self.assertEqual(float(weighted.reshape(-1)[np.argmin(w)]), 1.0)


class StimulationTest(CoordsTestCase):
    def test_points(self):
        binary, record = coords.stimulation_seed("points", GRID_2MM, points=[(0, 0, 0)], radius_mm=6)
        graded, _ = coords.stimulation_seed("points", GRID_2MM, points=[(0, 0, 0)], diameter_mm=12, weighting="graded")
        self.assertEqual(record["sphere"]["diameter_mm"], 12.0)
        self.assertGreater(binary.sum(), graded.sum())
        self.assertEqual(int((binary > 0).sum()), int((graded > 0).sum()))
        with self.assertRaises(ValueError):
            coords.stimulation_seed("points", GRID_2MM, points=[(0, 0, 0)], weighting="threshold")

    def test_images(self):
        fine = Grid((40, 48, 40), np.array([[1.0, 0, 0, -19.5], [0, 1.0, 0, -23.5], [0, 0, 1.0, -19.5], [0, 0, 0, 1.0]]))
        world = np.indices(fine.shape).reshape(3, -1).T @ fine.affine[:3, :3].T + fine.affine[:3, 3]
        field = np.exp(-np.linalg.norm(world, axis=1) ** 2 / 50.0).reshape(fine.shape) * 120.0
        path = os.path.join(self.tmp, "sub-01_efield.nii.gz")
        nifti.write_nifti(path, field.astype(np.float32), fine.affine, dtype=np.float32)
        normalized, record = coords.stimulation_seed("efield", GRID_2MM, image=path)
        self.assertAlmostEqual(float(normalized.max()), 1.0, places=5)
        self.assertEqual(record["source"]["file"], "sub-01_efield.nii.gz")
        # A vector E-field (SimNIBS *_E.nii.gz, X x Y x Z x 3) uses its magnitude
        vector = np.stack([field * 0.6, field * 0.8, np.zeros_like(field)], axis=-1)
        magnitude, _ = coords.stimulation_seed("efield", GRID_2MM, image=(vector, fine.affine))
        np.testing.assert_allclose(magnitude, normalized, atol=1e-5)
        with self.assertRaisesRegex(ValueError, "one 3D volume"):
            coords.stimulation_seed("efield", GRID_2MM, image=(np.stack([field, field], axis=-1), fine.affine))
        thresholded, record = coords.stimulation_seed("efield", GRID_2MM, image=path, weighting="threshold", threshold=60.0)
        self.assertTrue(set(np.unique(thresholded)) <= {0.0, 1.0})
        self.assertLess(thresholded.sum(), (normalized > 0.01).sum())
        vta = (np.linalg.norm(world, axis=1) <= 3.0).reshape(fine.shape).astype(np.float32)
        probabilistic, _ = coords.stimulation_seed("vta", GRID_2MM, image=(vta, fine.affine), weighting="probabilistic")
        binary, _ = coords.stimulation_seed("vta", GRID_2MM, image=(vta, fine.affine))
        self.assertAlmostEqual(probabilistic.sum() * 8.0, vta.sum(), delta=1e-3)  # the volume is conserved
        self.assertTrue(set(np.unique(binary)) <= {0.0, 1.0})

    def test_image_space_is_checked_recorded_and_announced(self):
        """Stimulation images are used as MNI world mm. One not known to be in MNI space (a SimNIBS subject-space
        magnE on an oblique scanner grid) is told apart from an MNI one (a known template grid, or a NIfTI header
        marked MNI-152 on its own small grid, as Lead-DBS VTAs are), recorded per seed in seeds.json and named
        in the notice shown before mapping (it used to be mapped silently, with no space anywhere).
        """
        def blob(shape, affine, width):
            world = np.indices(shape).reshape(3, -1).T @ affine[:3, :3].T + affine[:3, 3]
            return np.exp(-np.linalg.norm(world, axis=1) ** 2 / width).reshape(shape).astype(np.float32)

        angle = np.deg2rad(15.0)
        oblique = np.eye(4)
        oblique[:3, :3] = [[1, 0, 0], [0, np.cos(angle), -np.sin(angle)], [0, np.sin(angle), np.cos(angle)]]
        oblique[:3, 3] = (oblique[:3, :3] @ -np.array([20.0, 24.0, 20.0]))  # centred on (0, 0, 0)
        native = os.path.join(self.tmp, "sub-01_magnE.nii.gz")
        nifti.write_nifti(native, blob((40, 48, 40), oblique, 50.0) * 120, oblique, dtype=np.float32, sform_code=1, qform_code=1)
        small = np.array([[0.5, 0, 0, -10.0], [0, 0.5, 0, -10.0], [0, 0, 0.5, -10.0], [0, 0, 0, 1.0]])
        header_mni = os.path.join(self.tmp, "sub-02_vta.nii.gz")
        nifti.write_nifti(header_mni, blob((41, 41, 41), small, 50.0), small, dtype=np.float32, sform_code=4, qform_code=4)
        nlin6 = np.array([[-2.0, 0, 0, 90.0], [0, 2.0, 0, -126.0], [0, 0, 2.0, -72.0], [0, 0, 0, 1.0]])
        template = os.path.join(self.tmp, "sub-03_magnE_MNI.nii.gz")
        nifti.write_nifti(template, blob((91, 109, 91), nlin6, 50.0) * 120, nlin6, dtype=np.float32, sform_code=2, qform_code=2)

        space = coords.stimulation_image_space(native)
        self.assertEqual(space, {"mni": False, "space": None, "basis": None, "oblique": True})
        self.assertEqual(coords.stimulation_image_space(header_mni), {"mni": True, "space": "MNI152 (NIfTI header)", "basis": "header", "oblique": False})
        space = coords.stimulation_image_space(template)
        self.assertTrue(space["mni"])
        self.assertEqual((space["basis"], space["space"]), ("grid", "MNI152NLin6Asym"))
        # An (array, affine) image has no header: only its grid can tell
        self.assertEqual(coords.stimulation_image_space((np.zeros((41, 41, 41)), small))["mni"], False)

        # The record of each seed keeps it; the seed set names the unconfirmed ones (file names only)
        _seed, record = coords.stimulation_seed("efield", GRID_2MM, image=native)
        self.assertFalse(record["space"]["mni"])
        table = self.write("efields.csv", "participant_id,file\nsub-01,sub-01_magnE.nii.gz\nsub-02,sub-02_vta.nii.gz\nsub-03,sub-03_magnE_MNI.nii.gz\n")
        out = os.path.join(self.tmp, "stim spaces")
        coords.prepare_stimulation(out, coords.read_stimulation_table(table, "efield"), GRID_2MM, "efield")
        seeds = coords.read_seed_record(out)
        self.assertEqual([seed["stimulation"]["space"]["mni"] for seed in seeds["seeds"]], [False, True, True])
        self.assertEqual(seeds["stimulation"]["space_unconfirmed"], ["sub-01"])
        self.assertIn("Not confirmed as MNI space: sub-01", seeds["stimulation"]["placement"])
        self.assertEqual(_absolute_strings(seeds), [])

        # The notice shown before mapping names the file and says it is on an oblique (scanner-like) grid
        notice = coords.stimulation_space_notice([("sub-01_magnE.nii.gz", coords.stimulation_image_space(native)),
                                                  ("sub-03_magnE_MNI.nii.gz", coords.stimulation_image_space(template))])
        self.assertIn("Not confirmed as MNI space: sub-01_magnE.nii.gz", notice)
        self.assertNotIn("sub-03", notice)
        self.assertIn("*_MNI", notice)
        self.assertIn("It is on an oblique grid", notice)
        several = coords.stimulation_space_notice([("a.nii.gz", coords.stimulation_image_space(native)), ("b.nii.gz", {"mni": False, "oblique": False})])
        self.assertIn("Not confirmed as MNI space: a.nii.gz, b.nii.gz", several)
        self.assertIn("1 of 2 are on an oblique grid", several)
        self.assertEqual(coords.stimulation_space_notice([("sub-02_vta.nii.gz", coords.stimulation_image_space(header_mni))]), "")
        points = coords.read_stimulation_table(self.write("points.csv", "participant_id,x,y,z\np1,0,0,0\n"), "points")
        coords.prepare_stimulation(os.path.join(self.tmp, "stim points"), points, GRID_2MM, "points", radius_mm=6)
        self.assertIn("MNI coordinates", coords.read_seed_record(os.path.join(self.tmp, "stim points"))["stimulation"]["placement"])

    def test_rmap_helpers(self):
        rng = np.random.default_rng(3)
        outcome = rng.normal(size=12)
        maps = rng.normal(size=(12, 40))
        maps[:, 0] = outcome * 2 + 1
        r = coords.outcome_rmap(maps, outcome)
        expected = [np.corrcoef(outcome, maps[:, v])[0, 1] for v in range(40)]
        np.testing.assert_allclose(r, expected, atol=1e-6)
        np.testing.assert_allclose(coords.outcome_rmap(maps, outcome, flip=True), -np.asarray(expected), atol=1e-6)
        weighted = coords.outcome_weighted_map(maps, outcome)
        np.testing.assert_allclose(weighted, outcome @ maps / np.abs(outcome).sum(), rtol=1e-5)
        args = coords.slnm_arguments("maps.tsv", "outcomes.tsv", "improvement", "mask.nii.gz", "out", flip=True)
        self.assertIn("--flip", args)
        self.assertEqual(args[args.index("--input") + 1], "Fz")

    def test_stimulation_table_and_folder(self):
        table = self.write("sites.csv", "participant_id,x,y,z,improvement\np1,-12,14,10,55\np2,-8,10,4,20\np2,-6,10,4,20\np3,-10,16,8,n/a\n")
        self.assertEqual(coords.numeric_columns(table), ["improvement"])
        items = coords.read_stimulation_table(table, "points", "improvement")
        out = os.path.join(self.tmp, "run")
        tsv = coords.prepare_stimulation(out, items, GRID_2MM, "points", radius_mm=6, outcome_column="improvement")
        _columns, rows = read_tsv(tsv)
        self.assertEqual([row["participant_id"] for row in rows], ["p1", "p2", "p3"])
        _columns, outcomes = read_tsv(os.path.join(out, coords.OUTCOMES_TSV))
        self.assertEqual([row["improvement"] for row in outcomes], ["55.0", "20.0", "n/a"])  # a missing outcome stays missing
        record = coords.read_seed_record(out)
        self.assertEqual(record["kind"], "stimulation")
        self.assertEqual(record["seeds"][1]["stimulation"]["points"], 2)  # two rows of p2 make one seed
        self.assertEqual(_absolute_strings(record), [])


class SeedSetTest(CoordsTestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="lnm-coords-bundle-")
        cls.meta = connectome.make_synthetic(os.path.join(cls.root, "syn.lnmc"), shape=(16, 18, 16), n_subjects=4, runs=(30,), seed=2, name="Syn")
        cls.bundle = connectome.ConnectomeBundle.open(cls.meta["path"])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def foci_file(self):
        a = self.meta["blobs"]["A"]["center_world"]
        a2 = self.meta["blobs"]["A2"]["center_world"]
        tal = coords.mni2tal(a)
        return self.write("foci.csv", "study,x,y,z,space\n" + f"S1,{a[0]},{a[1]},{a[2]},MNI\nS2,{tal[0]:.6f},{tal[1]:.6f},{tal[2]:.6f},TAL\nS3,{a2[0]},{a2[1]},{a2[2]},MNI\n")

    def test_prepare_and_map_with_cohortmaps(self):
        source = self.foci_file()
        out = os.path.join(self.tmp, "run with space")
        tsv = coords.prepare_coordinates(out, coords.read_foci(source), self.bundle.grid, diameter_mm=8, source=source, grid_space="MNI152NLin2009cAsym")
        record = coords.read_seed_record(out)
        self.assertEqual(record["kind"], "coordinates")
        self.assertEqual(record["sphere"], {"radius_mm": 4.0, "diameter_mm": 8.0, "given": "diameter"})
        self.assertEqual(record["conversion"]["converted"], 1)
        self.assertEqual(record["source"]["file"], "foci.csv")
        self.assertEqual([seed["participant_id"] for seed in record["seeds"]], ["S1", "S2", "S3"])
        self.assertEqual(_absolute_strings(record), [])
        _columns, rows = read_tsv(tsv)
        self.assertTrue(all(not os.path.isabs(row["lesion"]) for row in rows))
        # S1 and S2 (the same point given in MNI and in Talairach) give nearly the same seed
        s1, _a, _h = nifti.read_nifti(os.path.join(out, rows[0]["lesion"]))
        s2, _a, _h = nifti.read_nifti(os.path.join(out, rows[1]["lesion"]))
        self.assertGreater(float((np.minimum(s1, s2)).sum() / np.maximum(s1, s2).sum()), 0.95)
        # The same engine as lesions: cohortmaps maps the seeds (weights as they are)
        args = coords.cohortmaps_arguments(tsv, self.bundle.path, out)
        process = subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.cohortmaps", *args], check=False, cwd=MODULE_DIR, env=_env(), capture_output=True, text=True, timeout=600)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        maps = cohortmaps.read_maps_tsv(os.path.join(out, "maps.tsv"))
        self.assertEqual([row["participant_id"] for row in maps], ["S1", "S2", "S3"])
        t1, _a, _h = nifti.read_nifti(maps[0]["T"])
        t3, _a, _h = nifti.read_nifti(maps[2]["T"])
        mask = self.bundle.mask
        self.assertGreater(np.corrcoef(np.asarray(t1)[mask], np.asarray(t3)[mask])[0, 1], 0.5)  # A and A2 share a network

    def test_seed_outside_the_mask_is_refused_by_name(self):
        """A seed with no weight in the connectome's brain mask is refused before anything is written, named by its
        participant id or study (its all-zero map would otherwise fail the whole seed-set run under a cache name).
        """
        a = self.meta["blobs"]["A"]["center_world"]
        corner = (np.asarray(self.bundle.affine) @ np.array([1.0, 1.0, 1.0, 1.0]))[:3]  # on the grid, outside the ellipsoid mask
        items = [{"participant_id": "sub-01", "points": [tuple(a)]}, {"participant_id": "sub-03", "points": [tuple(corner)]},
                 {"participant_id": "sub-02", "points": [tuple(a)]}]
        out = os.path.join(self.tmp, "stim outside")
        with self.assertRaises(coords.OutsideMaskError) as caught:
            coords.prepare_stimulation(out, items, self.bundle.grid, "points", radius_mm=3, mask_indices=self.bundle.mask_indices)
        self.assertIsInstance(caught.exception, ValueError)  # the UI's "The seeds cannot be made" path
        self.assertEqual(caught.exception.labels, ["sub-03"])
        self.assertIn("sub-03 lies outside the connectome's brain mask", str(caught.exception))
        self.assertFalse(os.path.exists(os.path.join(out, coords.LESIONS_TSV)))
        self.assertFalse(os.path.exists(os.path.join(out, coords.SEED_FOLDER)))
        # The seeds inside the mask are made as before; without a mask nothing is checked (the library's earlier contract)
        tsv = coords.prepare_stimulation(out, [items[0], items[2]], self.bundle.grid, "points", radius_mm=3, mask_indices=self.bundle.mask_indices)
        self.assertEqual([row["participant_id"] for row in read_tsv(tsv)[1]], ["sub-01", "sub-02"])
        coords.prepare_stimulation(os.path.join(self.tmp, "stim unchecked"), items, self.bundle.grid, "points", radius_mm=3)
        # Coordinates: the study is named; several are all named
        foci = [coords.Focus(*a, study="Smith 2020"), coords.Focus(*corner, study="Out 2021"), coords.Focus(*corner, study="Far 2022")]
        with self.assertRaises(coords.OutsideMaskError) as caught:
            coords.prepare_coordinates(os.path.join(self.tmp, "coords outside"), foci, self.bundle.grid, radius_mm=3, mask_indices=self.bundle.mask_indices)
        self.assertEqual(caught.exception.labels, ["Out 2021", "Far 2022"])
        self.assertIn("Out 2021, Far 2022 lie outside", str(caught.exception))
        # The command line refuses the same way, naming the study
        source = self.write("outside.csv", "study,x,y,z,space\n" + f"Smith 2020,{a[0]},{a[1]},{a[2]},MNI\nOut 2021,{corner[0]},{corner[1]},{corner[2]},MNI\n")
        process = subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.coords", "seeds", "--foci", source, "--bundle", self.bundle.path,
                                  "--out", os.path.join(self.tmp, "cli outside"), "--radius", "3"],
                                 check=False, cwd=MODULE_DIR, env=_env(), capture_output=True, text=True, timeout=300)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("Out 2021 lies outside the connectome's brain mask", process.stdout + process.stderr)

    def test_atrophy_prepare(self):
        w = np.zeros(self.bundle.shape, dtype=np.float32)
        w[self.bundle.mask] = 0.5
        center = self.meta["blobs"]["A"]["center_ijk"]
        w[center[0] - 1:center[0] + 2, center[1] - 1:center[1] + 2, center[2] - 1:center[2] + 2] = -3.0
        path = os.path.join(self.tmp, "sub-01_wmap.nii.gz")
        nifti.write_nifti(path, w, self.bundle.affine, dtype=np.float32)
        tsv = coords.prepare_atrophy(os.path.join(self.tmp, "atrophy"), {"sub-01": path}, self.bundle.grid, "w<-2")
        _columns, rows = read_tsv(tsv)
        seed, _a, _h = nifti.read_nifti(os.path.join(self.tmp, "atrophy", rows[0]["lesion"]))
        self.assertEqual(int(np.asarray(seed).sum()), 27)
        self.assertEqual(coords.read_seed_record(os.path.join(self.tmp, "atrophy"))["sources"]["sub-01"]["file"], "sub-01_wmap.nii.gz")

    def test_cli_seeds_and_null(self):
        source = self.foci_file()
        out = os.path.join(self.tmp, "cli")
        process = subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.coords", "seeds", "--foci", source, "--bundle", self.bundle.path, "--out", out, "--radius", "4"],
                                 check=False, cwd=MODULE_DIR, env=_env(), capture_output=True, text=True, timeout=300)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual(_events(process.stdout)[-1]["event"], "done")
        self.assertTrue(os.path.isfile(os.path.join(out, coords.LESIONS_TSV)))
        gm = np.where(self.bundle.mask, 0.8, 0.0).astype(np.float32)
        gm_path = os.path.join(self.tmp, "tpl-MNI152NLin2009cAsym_res-02_label-GM_probseg.nii.gz")
        nifti.write_nifti(gm_path, gm, self.bundle.affine, dtype=np.float32)
        null_dir = os.path.join(self.tmp, "null")
        base = [sys.executable, "-m", "LesionNetworkMappingLib.coords", "null", "--foci", source, "--bundle", self.bundle.path, "--gm-mask", gm_path, "--out", null_dir,
                "--k", "4", "--seed", "1", "--rule", json.dumps({"kind": "custom_t", "value": 2.0})]
        process = subprocess.run([*base, "--grid-space", "MNI152NLin6Asym"], check=False, cwd=MODULE_DIR, env=_env(), capture_output=True, text=True, timeout=300)
        self.assertNotEqual(process.returncode, 0)  # a 2009c GM mask for an NLin6 connectome is refused
        self.assertIn("family", _events(process.stdout)[-1]["message"])
        process = subprocess.run([*base, "--grid-space", "MNI152NLin2009cAsym", "--keep-maps"], check=False, cwd=MODULE_DIR, env=_env(), capture_output=True, text=True, timeout=600)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        with open(os.path.join(null_dir, coords.NULL_JSON), encoding="utf-8") as f:
            record = json.load(f)
        self.assertEqual(record["k"], 4)
        self.assertEqual(record["counts"], {"S1": 1, "S2": 1, "S3": 1})
        self.assertEqual(record["conversion"]["converted"], 1)
        self.assertTrue(0.0 <= record["percentile"] <= 100.0)
        self.assertEqual(_absolute_strings(record), [])
        for name in record["outputs"].values():
            self.assertTrue(os.path.isfile(os.path.join(null_dir, name)), name)
        _stack, _a, header = nifti.read_nifti(os.path.join(null_dir, record["outputs"]["coordNullMaps"]))
        self.assertEqual(tuple(header["shape"])[3], 4)


class AtrophyDocsTest(unittest.TestCase):
    """Atrophy seeds exist only as ``coords atrophy`` on the command line: the README and the module page say so."""

    @staticmethod
    def _read(*parts):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(MODULE_DIR))), *parts)
        if not os.path.isfile(path):
            return None  # not run from a source tree
        with open(path, encoding="utf-8") as f:
            return " ".join(f.read().split())

    def test_readme_does_not_claim_an_atrophy_seed_mode(self):
        readme = self._read("README.md")
        if readme is None:
            self.skipTest("no source tree")
        self.assertIn("Coordinate, atrophy and stimulation seeds.", readme)
        self.assertNotIn("foci (Talairach is converted to MNI; radius or diameter is explicit), atrophy maps", readme)
        self.assertIn("atrophy seeds on the command line", readme)
        self.assertIn("command-line feature, not a seed mode of the Map workspace", readme)

    def test_module_page_documents_the_atrophy_command(self):
        page = self._read("Docs", "user_guide", "modules", "lesionnetworkmapping.md")
        if page is None:
            self.skipTest("no source tree")
        self.assertIn("### Atrophy seeds (command line)", page)
        self.assertIn("-m LesionNetworkMappingLib.coords atrophy", page)
        for rule in coords.ATROPHY_RULES:
            self.assertIn(rule, page)
        self.assertIn("does not check the space of a w-map", page)


if __name__ == "__main__":
    unittest.main()

"""Tests of the control-lesion sets (controlsets.py) and their study links (Study.link_control_set, controlset: paths).

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingControlSetsTest.py -v
"""

import contextlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np

from LesionNetworkMappingLib import cohortmaps, connectome, controlsets, engine, jobs, nifti
from LesionNetworkMappingLib.geometry import GRID_BY_ID
from LesionNetworkMappingLib.study import Study

NLIN6 = GRID_BY_ID["NLin6-2mm"]
TF2009_1MM = GRID_BY_ID["TF2009-1mm"]
# (file name, world center, radius in mm); x < 0 is left. "ctl_006" is one slice thick (a 2D tracing).
CONTROLS = (
    ("ctl_001_lesion.nii.gz", (-8.0, -6.0, 0.0), 5.0),
    ("ctl_002_lesion.nii.gz", (8.0, 6.0, 0.0), 5.0),
    ("ctl_003_lesion.nii.gz", (-6.0, 8.0, 4.0), 4.0),
    ("ctl_004_lesion.nii.gz", (6.0, -8.0, -4.0), 4.0),
    ("ctl_005_lesion.nii.gz", (-10.0, 0.0, -2.0), 6.0),
)


def _quiet(*_args, **_kwargs):
    pass


def _sphere(path, grid, center, radius, value=1, flat=False):
    """A binary sphere (or its middle axial slice when ``flat``) on a registry grid."""
    affine = grid.affine
    ijk = np.indices(grid.shape).reshape(3, -1).T
    world = ijk @ affine[:3, :3].T + affine[:3, 3]
    mask = np.linalg.norm(world - np.asarray(center, dtype=float), axis=1) <= radius
    if flat:
        mask &= np.abs(world[:, 2] - center[2]) < grid.spacing / 2
    data = (mask.reshape(grid.shape) * value).astype(np.uint8)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    nifti.write_nifti(path, data, affine)
    return path


@contextlib.contextmanager
def counting_engine():
    passes = []
    original = engine.compute_network_maps

    def counter(bundle, weights, *args, **kwargs):
        passes.append(np.atleast_2d(weights).shape[0])
        return original(bundle, weights, *args, **kwargs)

    engine.compute_network_maps = counter
    try:
        yield passes
    finally:
        engine.compute_network_maps = original


class ControlSetsTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = tempfile.mkdtemp(prefix="lnm-controlsets-data-")
        cls.folder = os.path.join(cls.data, "controls")
        for name, center, radius in CONTROLS:
            _sphere(os.path.join(cls.folder, "masks", name), NLIN6, center, radius)
        _sphere(os.path.join(cls.folder, "masks", "ctl_006_lesion.nii.gz"), NLIN6, (4.0, 4.0, 2.0), 6.0, flat=True)
        _sphere(os.path.join(cls.folder, "masks", "ctl_007_lesion.nii.gz"), NLIN6, (0.0, 0.0, 0.0), 0.0, value=0)  # empty
        _sphere(os.path.join(cls.folder, "anat", "ctl_001_T1w.nii.gz"), NLIN6, (0.0, 0.0, 0.0), 30.0)  # a scan, not a lesion
        _sphere(os.path.join(cls.folder, "anat", "brain_mask.nii.gz"), NLIN6, (0.0, 0.0, 0.0), 30.0)  # anatomical
        cls.n_usable = len(CONTROLS) + 1

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.data, ignore_errors=True)

    def setUp(self):
        jobs.reset_cancel()
        self.tmp = tempfile.mkdtemp(prefix="lnm-controlsets-test-")
        self.root = os.path.join(self.tmp, "SlicerStudio", controlsets.REGISTRY_DIR)
        self.addCleanup(setattr, Study, "controlset_root", Study.controlset_root)
        Study.controlset_root = self.root

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def register(self, name="Ferguson490", **kwargs):
        kwargs.setdefault("source", "Ferguson et al. 2019 (synthetic stand-in)")
        kwargs.setdefault("licence_note", "Data use agreement: research only, no redistribution")
        return controlsets.register_folder(self.folder, name, root=self.root, **kwargs)

    def study(self, name, space="MNI152NLin6Asym", cases=()):
        study = Study.create(self.path(name), name=name, space=space)
        for index, (center, radius) in enumerate(cases):
            sid = f"sub-case{index + 1:02d}"
            path = _sphere(os.path.join(study.path, "lesions", f"{sid}_space-MNI152NLin6Asym_desc-lesion_mask.nii.gz"), NLIN6, center, radius)
            study.add_subject(sid, {"group": "case", "lesion": study.relpath(path), "status": "traced", "space": "MNI152NLin6Asym"})
        study.save()
        return study


class RegistryTest(ControlSetsTestBase):
    def test_register_folder_records_the_set(self):
        entry = self.register()
        folder = os.path.join(self.root, "Ferguson490")
        self.assertEqual(entry["dir"], folder)
        self.assertEqual((entry["name"], entry["n"], entry["space"], entry["grid_id"], entry["family"]), ("Ferguson490", self.n_usable, "MNI152NLin6Asym", "NLin6-2mm", "fsl6"))
        self.assertEqual(entry["licence_note"], "Data use agreement: research only, no redistribution")
        self.assertIn("Ferguson", entry["source"])
        self.assertEqual(entry["dimensionality"], {"2D": 1, "3D": len(CONTROLS)})
        self.assertEqual(sum(entry["hemisphere"].values()), self.n_usable)
        self.assertEqual(entry["hemisphere"]["L"], 3)
        ids = [lesion["id"] for lesion in entry["lesions"]]
        self.assertEqual(ids, [f"sub-ctl{k:03d}" for k in range(1, 7)])
        for lesion in entry["lesions"]:
            self.assertEqual(len(lesion["sha256"]), 64)
            self.assertGreater(lesion["volume_ml"], 0)
            self.assertTrue(lesion["rel"].startswith("masks/"), lesion["rel"])
        reasons = {item["rel"]: item["reason"] for item in entry["skipped"]}
        self.assertEqual(reasons["masks/ctl_007_lesion.nii.gz"], controlsets.SKIPPED_EMPTY)
        self.assertEqual(reasons["anat/ctl_001_T1w.nii.gz"], controlsets.SKIPPED_NOT_LESION)
        self.assertEqual(reasons["anat/brain_mask.nii.gz"], controlsets.SKIPPED_ANATOMICAL)
        # The registry holds the record and the lesion table, never a copy of a lesion, and no absolute path
        with open(os.path.join(folder, controlsets.CONTROLSET_JSON), encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(json.loads(text)["format"], controlsets.FORMAT)
        self.assertNotIn(self.data, text)
        self.assertFalse(os.path.isabs(entry["folder"]))
        self.assertEqual(entry["folder_path"], self.folder)
        files = [name for _dir, _subdirs, names in os.walk(folder) for name in names]
        self.assertFalse([name for name in files if name.endswith((".nii", ".nii.gz"))])
        rows = cohortmaps.read_lesions_tsv(os.path.join(folder, controlsets.LESIONS_TSV))
        self.assertEqual([(sid, sha) for sid, _path, sha in rows], [(lesion["id"], lesion["sha256"]) for lesion in entry["lesions"]])
        self.assertTrue(all(os.path.isfile(path) for _sid, path, _sha in rows))
        # Listed, loaded again, and registered only once
        self.assertEqual([item["name"] for item in controlsets.list_sets(self.root)], ["Ferguson490"])
        self.assertEqual(controlsets.load("Ferguson490", self.root)["content_sha256"], entry["content_sha256"])
        with self.assertRaises(FileExistsError):
            self.register()
        self.assertEqual(self.register(replace=True)["content_sha256"], entry["content_sha256"])

    def test_names_are_validated(self):
        for name in ("", "../x", "a/b", ".hidden", "x" * 65, "with space"):
            with self.assertRaises(ValueError, msg=name):
                controlsets.validate_name(name)
        self.assertEqual(controlsets.validate_name("Siddiqi_1090.v2"), "Siddiqi_1090.v2")

    def test_atlas_style_grid_asks_for_the_space(self):
        # ATLAS R2.0-style file names on the TemplateFlow 2009 1 mm grid, which holds 2009c and 2009a images alike
        folder = self.path("atlas")
        names = ("sub-r001s001_ses-1_space-MNI152NLin2009aSym_label-L_desc-T1lesion_mask.nii.gz",
                 "sub-r001s002_ses-1_space-MNI152NLin2009aSym_label-L_desc-T1lesion_mask.nii.gz")
        for name, x in zip(names, (-20.0, 20.0), strict=True):
            _sphere(os.path.join(folder, name.split("_ses-")[0], "ses-1", "anat", name), TF2009_1MM, (x, 0.0, 0.0), 5.0)
        scan = controlsets.scan_folder(folder)
        self.assertEqual([item["id"] for item in scan["lesions"]], ["sub-r001s001", "sub-r001s002"])
        with self.assertRaises(controlsets.AmbiguousSpaceError) as raised:
            controlsets.register_folder(folder, "ATLAS", root=self.root, scan=scan)
        question = raised.exception.questions[0]
        self.assertEqual((question["grid_id"], question["n"]), ("TF2009-1mm", 2))
        self.assertEqual(question["candidates"][0], "MNI152NLin2009aSym")  # the file names' space comes first
        self.assertEqual(set(question["candidates"]), set(TF2009_1MM.candidates))
        self.assertFalse(os.path.exists(os.path.join(self.root, "ATLAS")))  # nothing is written before the answer
        asked = []
        entry = controlsets.register_folder(folder, "ATLAS", root=self.root, scan=scan, ask=lambda q: asked.append(q) or q["candidates"][0])
        self.assertEqual(len(asked), 1)
        self.assertEqual((entry["space"], entry["family"], entry["n"]), ("MNI152NLin2009aSym", "2009", 2))
        self.assertEqual(entry["hemisphere"], {"L": 1, "R": 1})
        # An answer given up front is not asked again; a space of another family is refused
        entry = controlsets.register_folder(folder, "ATLAS", root=self.root, space={"TF2009-1mm": "MNI152NLin2009cAsym"}, ask=self.fail, replace=True)
        self.assertEqual(entry["space"], "MNI152NLin2009cAsym")
        with self.assertRaises(ValueError):
            controlsets.register_folder(folder, "ATLAS", root=self.root, space="MNI152NLin6Asym", replace=True)
        with self.assertRaises(ValueError):
            controlsets.register_folder(folder, "ATLAS", root=self.root, ask=lambda q: "MNI152NLin6Asym", replace=True)

    def test_mixed_families_are_refused(self):
        folder = self.path("mixed")
        _sphere(os.path.join(folder, "a_lesion.nii.gz"), NLIN6, (0.0, 0.0, 0.0), 5.0)
        _sphere(os.path.join(folder, "b_lesion.nii.gz"), GRID_BY_ID["McGill2009a-1mm"], (0.0, 0.0, 0.0), 5.0)
        with self.assertRaises(ValueError) as raised:
            controlsets.register_folder(folder, "Mixed", root=self.root)
        self.assertIn("families", str(raised.exception))

    def test_unrecognised_names_take_every_image(self):
        folder = self.path("plain")
        for k, x in enumerate((-12.0, 12.0)):
            _sphere(os.path.join(folder, f"patient{k + 1}.nii.gz"), NLIN6, (x, 0.0, 0.0), 4.0)
        entry = controlsets.register_folder(folder, "Plain", root=self.root)
        self.assertTrue(entry["all_lesions"])
        self.assertEqual([lesion["id"] for lesion in entry["lesions"]], ["sub-patient1", "sub-patient2"])


class StudyLinkTest(ControlSetsTestBase):
    def test_controlset_paths_resolve_through_the_registry(self):
        entry = self.register()
        rel = entry["lesions"][0]["rel"]
        value = controlsets.controlset_path("Ferguson490", rel)
        self.assertEqual(value, "controlset:Ferguson490/masks/ctl_001_lesion.nii.gz")
        self.assertEqual(controlsets.parse_path(value), ("Ferguson490", rel))
        self.assertIsNone(controlsets.parse_path("lesions/sub-01.nii.gz"))
        study = self.study("A")
        expected = os.path.join(self.folder, "masks", "ctl_001_lesion.nii.gz")
        self.assertEqual(study.abspath(value), expected)
        # Without Study.controlset_root, the environment variable names the registry
        study.controlset_root = None
        Study.controlset_root = None
        with mock.patch.dict(os.environ, {controlsets.ENV_ROOT: self.root}):
            self.assertEqual(study.abspath(value), expected)
        with mock.patch.dict(os.environ, {controlsets.ENV_ROOT: self.path("elsewhere")}):
            self.assertFalse(os.path.exists(study.abspath(value)))  # not registered there: reported as a missing file
        # Ordinary paths are unchanged
        self.assertEqual(study.abspath("lesions/x.nii.gz"), os.path.join(study.path, "lesions", "x.nii.gz"))
        # An invalid set name, or a path out of an unregistered set's folder, gives a missing file inside the registry
        for value in ("controlset:../../etc/passwd", "controlset:..%2F/x.nii.gz", "controlset:Unknown/../../../x.nii.gz"):
            path = controlsets.resolve_path(value, self.root)
            self.assertTrue(path.startswith(os.path.join(self.root, "")), (value, path))
            self.assertFalse(os.path.exists(path), value)

    def test_link_adds_rows_without_copying(self):
        entry = self.register()
        study = self.study("A", cases=[((-8.0, 0.0, 0.0), 6.0)])
        before = sorted(os.listdir(os.path.join(study.path, "lesions")))
        report = study.link_control_set("Ferguson490", entry["content_sha256"])
        self.assertEqual(len(report["added"]), self.n_usable)
        self.assertEqual(report["added"][0], "sub-Ferguson490ctl001")
        study.save()
        self.assertEqual(sorted(os.listdir(os.path.join(study.path, "lesions"))), before)  # no copies
        reopened = Study.open(study.path)
        self.assertEqual(reopened.control_sets, [{"name": "Ferguson490", "sha256": entry["content_sha256"]}])
        row = reopened.subject("sub-Ferguson490ctl001")
        self.assertEqual((row["group"], row["source_type"], row["source_dataset"], row["source_id"], row["status"], row["space"]),
                         ("control", "control_db", "Ferguson490", "sub-ctl001", "traced", "MNI152NLin6Asym"))
        self.assertEqual(row["lesion"], "controlset:Ferguson490/masks/ctl_001_lesion.nii.gz")
        self.assertEqual(row["hemisphere"], "L")
        self.assertEqual(float(row["volume_ml"]), entry["lesions"][0]["volume_ml"])
        self.assertEqual(reopened.subject("sub-Ferguson490ctl006")["dimensionality"], "2D")
        with open(os.path.join(study.path, "participants.tsv"), encoding="utf-8") as f:
            self.assertNotIn(self.data, f.read())  # no absolute local path in the shared table
        # The control lesions are found by the analyses (cohortmaps' study rows)
        lesions, excluded = cohortmaps.study_lesions(reopened)
        self.assertEqual(len(lesions), self.n_usable + 1)
        self.assertFalse(excluded)
        self.assertEqual(len(reopened.lesion_paths(group="control", existing_only=True)), self.n_usable)
        # Linking again updates the rows instead of adding them; a changed set is detected
        again = reopened.link_control_set("Ferguson490")
        self.assertEqual((len(again["added"]), len(again["updated"])), (0, self.n_usable))
        self.assertEqual(len(reopened), self.n_usable + 1)
        with self.assertRaises(ValueError):
            reopened.link_control_set("Ferguson490", "0" * 64)
        # Unlink removes the rows and the record, never the files
        removed = reopened.unlink_control_set("Ferguson490")
        self.assertEqual(len(removed), self.n_usable)
        self.assertEqual((len(reopened), reopened.control_sets), (1, []))
        self.assertTrue(all(os.path.isfile(os.path.join(self.folder, "masks", name)) for name, _c, _r in CONTROLS))

    def test_old_study_files_are_unchanged(self):
        study = self.study("Plain")
        with open(os.path.join(study.path, "study.json"), encoding="utf-8") as f:
            self.assertNotIn("control_sets", json.load(f))
        self.assertEqual(study.control_sets, [])

    def test_matched_subsample_and_partial_link(self):
        self.register()
        study = self.study("A", cases=[((-8.0, 0.0, 0.0), 5.0), ((8.0, 0.0, 0.0), 4.0)])
        matched = controlsets.matched_subsample("Ferguson490", study, ("volume_ml",), seed=3, root=self.root)
        self.assertEqual(len(matched["controls"]), 2)
        self.assertEqual(sorted(pair[0] for pair in matched["pairs"]), ["sub-case01", "sub-case02"])
        self.assertEqual(matched, controlsets.matched_subsample("Ferguson490", study, ("volume_ml",), seed=3, root=self.root))  # seeded
        report = study.link_control_set("Ferguson490", ids=matched["controls"])
        self.assertEqual(len(report["added"]), 2)
        self.assertEqual(sorted(row["source_id"] for row in controlsets.linked_rows(study)), sorted(matched["controls"]))
        # The link is the selection: a full link, then the subsample again, shrinks to the subsample
        self.assertEqual(len(study.link_control_set("Ferguson490")["added"]), self.n_usable - 2)
        report = study.link_control_set("Ferguson490", ids=matched["controls"][:1])
        self.assertEqual((len(report["updated"]), len(report["removed"])), (1, self.n_usable - 1))
        self.assertEqual([row["source_id"] for row in controlsets.linked_rows(study)], matched["controls"][:1])
        self.assertEqual(len(study), 3)  # the two cases are untouched

    def test_relink_after_the_set_is_registered_again(self):
        folder = self.path("shrinking")
        shutil.copytree(os.path.join(self.folder, "masks"), folder)
        entry = controlsets.register_folder(folder, "Shrinking", root=self.root)
        study = self.study("A", cases=[((-8.0, 0.0, 0.0), 5.0)])
        self.assertEqual(len(study.link_control_set("Shrinking")["added"]), self.n_usable)
        os.remove(os.path.join(folder, "ctl_005_lesion.nii.gz"))
        changed = controlsets.register_folder(folder, "Shrinking", root=self.root, replace=True)
        self.assertNotEqual(changed["content_sha256"], entry["content_sha256"])
        report = study.link_control_set("Shrinking")
        self.assertEqual(report["removed"], ["sub-Shrinkingctl005"])
        self.assertEqual(len(report["updated"]), self.n_usable - 1)
        rows = controlsets.linked_rows(study, "Shrinking")
        self.assertEqual(sorted(row["source_id"] for row in rows), sorted(lesion["id"] for lesion in changed["lesions"]))
        self.assertTrue(all(os.path.isfile(study.abspath(row["lesion"])) for row in rows))
        self.assertEqual(study.control_sets, [{"name": "Shrinking", "sha256": changed["content_sha256"]}])
        self.assertTrue(os.path.isfile(os.path.join(self.folder, "masks", "ctl_005_lesion.nii.gz")))  # files are never removed

    def test_a_link_without_rows_is_not_recorded(self):
        self.register("Set1")
        self.register("Set-1")  # its participant ids collide with Set1's
        study = self.study("A")
        self.assertEqual(len(study.link_control_set("Set1")["added"]), self.n_usable)
        report = study.link_control_set("Set-1")
        self.assertEqual((report["added"], len(report["skipped"])), ([], self.n_usable))
        self.assertEqual([item["name"] for item in study.control_sets], ["Set1"])
        study.unlink_control_set("Set1")
        report = study.link_control_set("Set1", ids=["sub-nothing"])
        self.assertEqual((report["added"], study.control_sets), ([], []))
        self.assertNotIn("control_sets", study.metadata)

    def test_mismatch_report(self):
        entry = self.register()
        # Big right-sided cases against small mostly left-sided controls, in another template family
        study = self.study("A", space="MNI152NLin2009cAsym", cases=[((12.0, 0.0, 0.0), 12.0), ((14.0, 4.0, 0.0), 11.0), ((10.0, -4.0, 2.0), 13.0)])
        report = controlsets.mismatch_report(entry, study)
        self.assertEqual((report["n_cases"], report["n_controls"]), (3, self.n_usable))
        volume = report["covariates"]["volume_ml"]
        self.assertGreater(volume["smd"], controlsets.SMD_FLAG)
        self.assertTrue(volume["flagged"])
        self.assertLess(report["covariates"]["hemisphere_left"]["smd"], -controlsets.SMD_FLAG)
        self.assertIsNotNone(report["covariates"]["dim_2d"]["smd"])
        self.assertEqual(report["family"], {"set": "fsl6", "study": "2009", "set_space": "MNI152NLin6Asym", "study_space": "MNI152NLin2009cAsym", "differ": True})
        self.assertTrue(report["flagged"])
        self.assertTrue(any("Template family" in text for text in report["warnings"]))
        # Matching cases in the same family: no warning
        similar = self.study("B", cases=[(center, radius) for _name, center, radius in CONTROLS])
        similar.link_control_set("Ferguson490")
        report = controlsets.mismatch_report("Ferguson490", similar)
        self.assertFalse(report["family"]["differ"])
        self.assertFalse(report["covariates"]["volume_ml"]["flagged"])

    def test_case_values_are_read_ahead_for_a_worker(self):
        entry = self.register()
        study = self.study("A", cases=[((12.0, 0.0, 0.0), 12.0), ((-14.0, 4.0, 0.0), 11.0)])
        study.set("sub-case02", {"volume_ml": 9.5, "dimensionality": "3D", "hemisphere": "L"})  # a complete row needs no file
        cache = {}
        paths = controlsets.missing_case_files(study, cache)
        self.assertEqual(paths, [study.abspath(study.subject("sub-case01")["lesion"])])
        progress = []
        cache.update(controlsets.read_case_stats(paths, progress=lambda done, total: progress.append((done, total))))
        self.assertEqual(progress, [(1, 1)])
        self.assertEqual(controlsets.missing_case_files(study, cache), [])
        with mock.patch("LesionNetworkMappingLib.nifti.read_nifti", side_effect=AssertionError("a file was read")):
            warm = controlsets.mismatch_report(entry, study, cache=cache)
        self.assertEqual(warm, controlsets.mismatch_report(entry, study, cache={}))
        self.assertEqual(controlsets.study_case_covariates(study, cache=cache)["sub-case02"]["volume_ml"], 9.5)
        with self.assertRaises(jobs.Cancelled):
            controlsets.read_case_stats(paths, cancel=lambda: True)

    def test_lesion_values_do_not_depend_on_the_crop(self):
        from LesionNetworkMappingLib import lesionops

        affine = NLIN6.affine
        for center, radius, flat in (((-8.0, -6.0, 0.0), 5.0, False), ((4.0, 4.0, 2.0), 6.0, True), ((0.5, 0.0, 0.0), 6.0, False)):
            path = _sphere(self.path("crop.nii.gz"), NLIN6, center, radius, flat=flat)
            image, _affine, _header = nifti.read_nifti(path)
            stats = controlsets._lesion_stats(image, affine)
            fraction = (np.asarray(image) > 0).astype(np.float32)
            side = lesionops.side_check(fraction, affine, None)
            self.assertEqual(stats["dimensionality"], lesionops.detect_dimensionality(fraction, affine)["dimensionality"])
            self.assertEqual(stats["left_share"], round(float(side["left_frac"]), 4))
            self.assertAlmostEqual(stats["volume_ml"], float(fraction.sum()) * abs(np.linalg.det(affine[:3, :3])) / 1000.0, places=3)
        probabilistic = np.zeros(NLIN6.shape, dtype=np.float32)
        probabilistic[40:43, 50:52, 30:31] = 0.5
        probabilistic[0, 0, 0] = np.nan
        self.assertAlmostEqual(controlsets._lesion_stats(probabilistic, affine)["volume_ml"], 6 * 0.5 * 8 / 1000.0, places=6)
        self.assertEqual(controlsets._lesion_stats(np.zeros(NLIN6.shape, dtype=np.uint8), affine)["volume_ml"], 0.0)


class CacheTest(ControlSetsTestBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.meta = connectome.make_synthetic(os.path.join(cls.data, "synthetic.lnmc"), n_subjects=4, runs=(20, 20), seed=2)
        cls.bundle = connectome.ConnectomeBundle.open(cls.meta["path"])

    @classmethod
    def tearDownClass(cls):
        cls.bundle.close()
        super().tearDownClass()

    def test_cache_is_reused_across_two_studies(self):
        entry = self.register()
        cache_root = controlsets.maps_cache_root(entry)
        self.assertEqual(cache_root, os.path.join(entry["dir"], controlsets.CACHE_DIR))
        cache_dir = controlsets.maps_cache_dir(entry, self.bundle)
        self.assertEqual(os.path.dirname(cache_dir), cache_root)
        self.assertTrue(os.path.basename(cache_dir).startswith(f"conn-{engine.connectome_label(self.bundle.name)}_subj-"))
        records = []
        with counting_engine() as passes:
            for name in ("A", "B"):
                study = self.study(name, cases=[((-8.0, 0.0, 0.0), 5.0)])
                study.link_control_set("Ferguson490")
                study.save()
                lesions = controlsets.study_control_lesions(study, "Ferguson490")
                self.assertEqual(len(lesions), self.n_usable)
                before = sum(passes)
                record = cohortmaps.run(lesions, self.bundle, os.path.join(study.path, "derivatives", "lnm", "controls"), cache_root=cache_root, study=study, emit=_quiet)
                records.append((record, sum(passes) - before))
        (first, computed_a), (second, computed_b) = records
        self.assertEqual(computed_a, self.n_usable)  # study A maps the controls once ...
        self.assertEqual(computed_b, 0)  # ... study B reuses every cached map
        self.assertEqual(first["cache_dir"], second["cache_dir"])
        status = controlsets.cache_status(entry, self.bundle)
        self.assertEqual((status["n"], status["hits"], status["new"]), (self.n_usable, self.n_usable, 0))
        self.assertEqual(status["cache_dir"], cache_dir)
        # Mapping the set itself finds the same entries; another subject list is another cache folder
        with counting_engine() as passes:
            record = controlsets.map_controls(entry, self.bundle, emit=_quiet)
        self.assertEqual(sum(passes), 0)
        self.assertEqual(os.path.basename(record["cache_dir"]), os.path.basename(cache_dir))
        self.assertTrue(os.path.isfile(os.path.join(controlsets.maps_out_dir(entry, self.bundle), cohortmaps.MAPS_TSV)))
        self.assertNotEqual(controlsets.maps_cache_dir(entry, self.bundle, subjects=2), cache_dir)
        args = controlsets.map_job_args(entry, self.bundle.path)
        self.assertEqual(args[args.index("--cache") + 1], cache_root)
        self.assertEqual(args[args.index("--lesions-tsv") + 1], os.path.join(entry["dir"], controlsets.LESIONS_TSV))


if __name__ == "__main__":
    unittest.main()

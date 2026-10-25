"""The user's own MNI templates in the lesion network mapping library (Slicer Studio A-10, decision D-T2).

- geometry: the runtime grid extension point (``register_grid`` / ``unregister_grid`` / ``grid_spec_from_affine``):
  ``detect_space`` and ``detect_grid_candidates`` recognize a registered grid in RAS and LAS storage, built-in grids
  win and cannot be replaced, and ``resample_to_grid`` moves a lesion onto it with its volume conserved;
- the template families of the spaces a user may choose ("MNI" for "MNI (other)" is an unknown family);
- normalize: ``user:<id>`` resolved from a Templates folder (``find_template``, ``template_grid``, output names without a
  colon, the ``check`` command line, missing templates and masks), and a real affine registration to a user template
  whose normalization.json records the template's id, name, space and checksums (needs BRAINSFit and BRAINSResample:
  run with PythonSlicer).

Run without Slicer's GUI::

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingTemplatesTest.py -v
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
MODULE_DIR = os.path.dirname(os.path.dirname(TEST_DIR))
for folder in (MODULE_DIR, TEST_DIR):
    if folder not in sys.path:
        sys.path.insert(0, folder)

import numpy as np  # noqa: E402

from LesionNetworkMappingLib import geometry, jobs, nifti, normalize  # noqa: E402

TEMPLATE = "MNI152NLin2009cAsym"


def _sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _bundled():
    found = normalize.find_template(TEMPLATE)
    return found["files"]["fixed"], found["files"]["mask"]


def _tools_available():
    try:
        normalize.find_executable("BRAINSFit")
        normalize.find_executable("BRAINSResample")
    except jobs.JobError:
        return False
    image, mask = _bundled()
    return bool(image and mask)


def _env():
    env = dict(os.environ)
    env["PYTHONPATH"] = MODULE_DIR + os.pathsep + env.get("PYTHONPATH", "")
    for name in jobs.THREAD_ENV_VARS:
        env.pop(name, None)
    return env


def _cli(*args, timeout=600):
    process = subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.normalize", *args, "--events", "json"], check=False, capture_output=True,
                             text=True, encoding="utf-8", errors="replace", env=_env(), cwd=MODULE_DIR, timeout=timeout)
    events = [json.loads(line) for line in process.stdout.splitlines() if line.strip().startswith("{")]
    return process.returncode, events, process.stderr


def make_user_template(templates_dir, name, slug, space, image, mask=None, head=None):
    """A template folder as Slicer Studio writes it (StudioLib.templates): copies of the files and a template.json."""
    folder = os.path.join(templates_dir, name)
    os.makedirs(folder, exist_ok=True)
    files, sha = {"surfaces": []}, {}
    for role, path in (("image", image), ("mask", mask), ("head", head)):
        if path:
            target = os.path.join(folder, os.path.basename(path))
            shutil.copyfile(path, target)
            files[role] = os.path.basename(path)
            sha[role] = _sha256(target)
    header = nifti.read_nifti_header(image)
    record = {"schema": normalize.USER_TEMPLATE_FORMAT, "version": 1, "id": slug, "name": name, "space": space, "resolution_mm": 2,
              "shape": list(header["shape"][:3]), "contrast": "T1w", "files": files, "sha256": sha,
              "grid": {"shape": list(header["shape"][:3]), "affine": header["affine"].tolist()}, "notes": "", "added": "2026-10-25", "default": False}
    with open(os.path.join(folder, normalize.USER_TEMPLATE_JSON), "w", encoding="utf-8") as f:
        json.dump(record, f)
    return folder, record


class TemplatesGeometryTest(unittest.TestCase):
    """The runtime grid extension point of geometry.py and the families of user template spaces."""

    def setUp(self):
        self.spacing = 1.5
        self.shape = (129, 153, 129)
        self.affine = np.diag([self.spacing, self.spacing, self.spacing, 1.0])
        self.affine[:3, 3] = (-96.5, -132.5, -78.5)

    def tearDown(self):
        for spec in geometry.registered_grids():
            geometry.unregister_grid(spec.grid_id)

    def test_register_detect_unregister(self):
        self.assertIsNone(geometry.detect_space((self.shape, self.affine)))
        spec = geometry.grid_spec_from_affine("user:test-15", self.shape, self.affine, "MNI152NLin2009bAsym")
        self.assertEqual(spec.family, "2009b")
        self.assertFalse(spec.x_flipped)
        geometry.register_grid(spec)
        self.assertIn("user:test-15", geometry.GRID_BY_ID)
        self.assertEqual(geometry.detect_space((self.shape, self.affine)), ("MNI152NLin2009bAsym", 1.5))
        # LAS storage of the same grid
        las = self.affine.copy()
        las[0, 0] = -self.spacing
        las[0, 3] = self.affine[0, 3] + self.spacing * (self.shape[0] - 1)
        self.assertEqual(geometry.detect_space((self.shape, las)), ("MNI152NLin2009bAsym", 1.5))
        match = geometry.detect_grid_candidates(self.shape, las, origin_tolerance=0.01)
        self.assertEqual(match.grid_id, "user:test-15")
        # The connectome-grid form is unchanged (only the connectome grids)
        self.assertEqual(geometry.detect_space(self.shape, self.affine), "native")
        # A 1 mm shift is another grid (exact origins, as for Studio's space checks)
        shifted = self.affine.copy()
        shifted[:3, 3] += 1.0
        self.assertIsNone(geometry.detect_grid_candidates(self.shape, shifted, origin_tolerance=0.01))
        # Registering again replaces; built-in grids win and cannot be replaced
        geometry.register_grid(geometry.grid_spec_from_affine("user:test-15", self.shape, self.affine, "MNI"))
        self.assertEqual(geometry.detect_space((self.shape, self.affine)), ("MNI", 1.5))
        self.assertEqual(len(geometry.registered_grids()), 1)
        builtin = geometry.GRID_BY_ID["TF2009-2mm"]
        with self.assertRaises(ValueError):
            geometry.register_grid(builtin)
        geometry.register_grid(geometry.GridSpec("user:on-2009", builtin.shape, builtin.spacing, builtin.lowest_center, ("MNI152NLin2009bAsym",), "2009b"))
        self.assertEqual(geometry.detect_space((builtin.shape, builtin.affine)), ("MNI152NLin2009cAsym", 2), "the built-in grid wins")
        self.assertTrue(geometry.unregister_grid("user:test-15"))
        self.assertFalse(geometry.unregister_grid("user:test-15"))
        self.assertNotIn("user:test-15", geometry.GRID_BY_ID)
        self.assertIn("TF2009-2mm", geometry.GRID_BY_ID)
        self.assertIsNone(geometry.detect_space((self.shape, self.affine)))

    def test_unregistrable_grids(self):
        anisotropic = self.affine.copy()
        anisotropic[2, 2] = 3.0
        self.assertIsNone(geometry.grid_spec_from_affine("user:aniso", self.shape, anisotropic, "MNI"))
        oblique = self.affine.copy()
        angle = np.radians(10)
        oblique[:3, :3] = oblique[:3, :3] @ np.array([[np.cos(angle), -np.sin(angle), 0], [np.sin(angle), np.cos(angle), 0], [0, 0, 1]])
        self.assertIsNone(geometry.grid_spec_from_affine("user:oblique", self.shape, oblique, "MNI"))

    def test_resample_onto_user_grid(self):
        spec = geometry.register_grid(geometry.grid_spec_from_affine("user:test-15", self.shape, self.affine, "MNI152NLin2009bAsym"))
        lesion = np.zeros((97, 115, 97), dtype=np.uint8)
        lesion[30:40, 50:60, 40:50] = 1
        source = geometry.GRID_BY_ID["TF2009-2mm"]
        moved = geometry.resample_to_grid(lesion, source.affine, spec.grid, mode="fraction")
        self.assertEqual(moved.shape, self.shape)
        source_ml = lesion.sum() * 8.0 / 1000.0
        self.assertAlmostEqual(float(moved.sum()) * self.spacing ** 3 / 1000.0, source_ml, places=6)

    def test_families(self):
        self.assertEqual(geometry.space_family("MNI152NLin2009bAsym"), "2009b")
        self.assertEqual(geometry.space_family("MNI152NLin2009aSym"), "2009")
        self.assertEqual(geometry.space_family("MNI152NLin6Asym"), "fsl6")
        self.assertEqual(geometry.space_family("MNIColin27"), "colin")
        self.assertEqual(geometry.space_family("MNI"), "unknown")
        self.assertFalse(geometry.same_family("MNI", "MNI"), "MNI (other) is an unknown family: always the family note")
        self.assertFalse(geometry.same_family("MNI152NLin2009bAsym", "MNI152NLin6Asym"))


class TemplatesNormalizeTest(unittest.TestCase):
    """normalize.py with "user:<id>" templates."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lnm-templates-test-")
        cls.templates_dir = os.path.join(cls.tmp, "Slicer Studio é", "Templates")
        image, mask = _bundled()
        cls.image, cls.mask = image, mask
        cls.folder, cls.record = make_user_template(cls.templates_dir, "My 2009b brain", "my-2009b-brain", "MNI152NLin2009bAsym", image, mask)
        cls.nomask_folder, _ = make_user_template(cls.templates_dir, "No mask", "no-mask", "MNI", image)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for spec in geometry.registered_grids():
            geometry.unregister_grid(spec.grid_id)

    def test_resolve(self):
        self.assertTrue(normalize.is_user_template("user:my-2009b-brain"))
        self.assertFalse(normalize.is_user_template(TEMPLATE))
        self.assertEqual(normalize.user_template_folders([self.templates_dir]), sorted([self.folder, self.nomask_folder]))
        self.assertEqual(normalize.user_template_folders([self.folder]), [self.folder])
        self.assertIsNone(normalize.resolve_user_template("user:other", [self.templates_dir]))
        found = normalize.find_template("user:my-2009b-brain", dirs=[self.templates_dir])
        self.assertEqual(found["missing"], ["head"])
        self.assertEqual(found["files"]["fixed"], os.path.join(self.folder, os.path.basename(self.image)))
        self.assertEqual(found["files"]["reference"], found["files"]["fixed"])
        self.assertEqual(found["sha256"]["fixed"], self.record["sha256"]["image"])
        self.assertEqual(found["user"], {"id": "user:my-2009b-brain", "name": "My 2009b brain", "space": "MNI152NLin2009bAsym",
                                         "sha256": self.record["sha256"]["image"]})
        self.assertEqual(found["warnings"], [])
        grid = normalize.template_grid("user:my-2009b-brain")
        self.assertEqual(tuple(grid.shape), (97, 115, 97))
        self.assertEqual(normalize.template_space("user:my-2009b-brain"), "MNI152NLin2009bAsym")
        self.assertEqual(normalize.template_label("user:my-2009b-brain"), "userMy2009bBrain")
        self.assertEqual(normalize.template_record("user:my-2009b-brain")["name"], "My 2009b brain")
        names = normalize.output_names("sub-01", "T1w", "user:my-2009b-brain")
        self.assertEqual(names["lesion"], "sub-01_space-userMy2009bBrain_desc-lesion_mask.nii.gz")
        self.assertTrue(all(":" not in name for name in names.values()))
        # Built-in names are unchanged
        self.assertEqual(normalize.output_names("sub-01", "T1w", TEMPLATE)["lesion"], f"sub-01_space-{TEMPLATE}_res-1_desc-lesion_mask.nii.gz")
        # Not found, no mask
        with self.assertRaises(jobs.JobError) as context:
            normalize.find_template("user:missing", dirs=[self.templates_dir])
        self.assertEqual(context.exception.code, "template_missing")
        with self.assertRaises(jobs.JobError) as context:
            normalize._require_template(normalize.find_template("user:no-mask", dirs=[self.templates_dir]))
        self.assertIn("brain mask", context.exception.message)
        # A file changed after it was added is reported
        changed_dir = os.path.join(self.tmp, "changed")
        folder, _record = make_user_template(changed_dir, "Changed", "changed", "MNI", self.image, self.mask)
        with open(os.path.join(folder, normalize.USER_TEMPLATE_JSON), encoding="utf-8") as f:
            record = json.load(f)
        record["sha256"]["image"] = "0" * 64
        with open(os.path.join(folder, normalize.USER_TEMPLATE_JSON), "w", encoding="utf-8") as f:
            json.dump(record, f)
        self.assertTrue(normalize.find_template("user:changed", dirs=[changed_dir])["warnings"])

    def test_study_auto_template(self):
        """A study made on a user template normalizes to it ("auto"), found by checksum; else the built-in fallback."""
        from LesionNetworkMappingLib.study import Study

        sha = self.record["sha256"]["image"]

        def study(name, source, checksum):
            record = {"id": "MNI152NLin2009bAsym", "res": 2, "source": source, "name": "My 2009b brain", "space": "MNI152NLin2009bAsym", "sha256": checksum}
            return Study.create(os.path.join(self.tmp, "studies", name), name=name, template=record)

        own = study("own", "user:my-2009b-brain", sha)
        self.assertEqual(normalize._study_template(own, "auto", [self.templates_dir]), ("user:my-2009b-brain", None))
        # Another computer chose another slug for the same file: found by checksum
        renamed = study("renamed", "user:icbm-2009b", sha)
        self.assertEqual(normalize._study_template(renamed, "auto", [self.templates_dir])[0], "user:my-2009b-brain")
        # An explicit template still wins
        self.assertEqual(normalize._study_template(own, TEMPLATE, [self.templates_dir]), (TEMPLATE, None))
        # Not on this computer (no Templates folder, or another file with the same slug): the built-in fallback and its note
        for case, dirs in ((own, []), (study("other-file", "user:my-2009b-brain", "f" * 64), [self.templates_dir])):
            template_id, note = normalize._study_template(case, "auto", dirs)
            self.assertEqual(template_id, normalize.DEFAULT_TEMPLATE)
            self.assertIn("MNI152NLin2009bAsym", note or "")
        # A study template without a brain mask cannot be the target: the same image with a mask in another template folder,
        # else the built-in fallback
        nomask = study("nomask", "user:no-mask", sha)
        self.assertEqual(normalize._study_template(nomask, "auto", [self.templates_dir])[0], "user:my-2009b-brain")
        self.assertEqual(normalize._study_template(nomask, "auto", [self.nomask_folder])[0], normalize.DEFAULT_TEMPLATE)

    def test_cli_check_and_arguments(self):
        code, events, stderr = _cli("check", "--template", "user:my-2009b-brain", "--template-dir", self.templates_dir)
        done = [event for event in events if event["event"] in ("done", "result")]
        report = next((event for event in events if "templates" in event), None) or (done[-1] if done else None)
        self.assertIsNotNone(report, stderr[-2000:])
        templates = report.get("templates") or (report.get("report") or {}).get("templates")
        self.assertIsNotNone(templates, report)
        entry = templates["user:my-2009b-brain"]
        self.assertTrue(entry["usable"])
        self.assertEqual(entry["name"], "My 2009b brain")
        self.assertEqual(entry["space"], "MNI152NLin2009bAsym")
        self.assertEqual(entry["grid1"]["id"], "user:my-2009b-brain")
        code, _events, stderr = _cli("check", "--template", "bogus")
        self.assertEqual(code, jobs.EXIT_USAGE, stderr)

    @unittest.skipUnless(_tools_available(), "BRAINSFit/BRAINSResample or the bundled template are not available (run with PythonSlicer)")
    def test_normalize_to_user_template(self):
        import lnm_normalize_fixture as fixture

        subject = fixture.make_native(os.path.join(self.tmp, "native"), "sub-01", ("large",))
        out = os.path.join(self.tmp, "out")
        code, events, stderr = _cli("run", "--scan", subject["scan"], "--lesion", subject["lesion"], "--out", out, "--quality", "affine",
                                    "--template", "user:my-2009b-brain", "--template-dir", self.templates_dir)
        self.assertEqual(code, 0, f"{stderr[-2000:]} {[event for event in events if event['event'] == 'error']}")
        with open(os.path.join(out, normalize.PROVENANCE_JSON), encoding="utf-8") as f:
            record = json.load(f)
        template = record["template"]
        self.assertEqual(template["id"], "user:my-2009b-brain")
        self.assertEqual(template["name"], "My 2009b brain")
        self.assertEqual(template["space"], "MNI152NLin2009bAsym")
        self.assertEqual(template["image_sha256"], self.record["sha256"]["image"])
        self.assertEqual(template["sha256"]["fixed"], self.record["sha256"]["image"])
        self.assertEqual(template["files"]["fixed"], os.path.basename(self.image))
        self.assertNotIn(self.templates_dir, json.dumps(template), "no path of the template in the provenance")
        lesion_path = os.path.join(out, record["outputs"]["lesion"]["path"])
        self.assertTrue(os.path.basename(lesion_path).startswith("sub-01_space-userMy2009bBrain_desc-lesion_mask"))
        header = nifti.read_nifti_header(lesion_path)
        self.assertEqual(tuple(header["shape"][:3]), (97, 115, 97))
        self.assertTrue(np.allclose(header["affine"], nifti.read_nifti_header(self.image)["affine"], atol=1e-4), "the outputs are on the template's grid")
        with open(os.path.join(out, record["outputs"]["lesion_sidecar"]["path"]), encoding="utf-8") as f:
            sidecar = json.load(f)
        self.assertEqual(sidecar["SlicerStudio"]["space"], "MNI152NLin2009bAsym")
        self.assertEqual(sidecar["SpatialReference"], "MNI152NLin2009bAsym")
        # The warped lesion lies where the truth is (the 2 mm grid of the template)
        lesion, affine, _header = nifti.read_nifti(lesion_path)
        truth = fixture._fractions(lesion.shape, affine, np.eye(4), [fixture.LESIONS["large"]]) >= 0.5
        dice = 2.0 * np.logical_and(lesion > 0, truth).sum() / max(1, (lesion > 0).sum() + truth.sum())
        self.assertGreaterEqual(dice, 0.75)
        # warp reuses the run's template
        native_copy = os.path.join(self.tmp, "native-copy.nii.gz")
        shutil.copyfile(subject["lesion"], native_copy)
        code, events, stderr = subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.normalize", "warp", "--run", out, "--image", native_copy,
                                               "--template-dir", self.templates_dir, "--events", "json"], check=False, capture_output=True, text=True,
                                              env=_env(), cwd=MODULE_DIR, timeout=600).returncode, None, None
        self.assertEqual(code, 0)
        warps = os.listdir(os.path.join(out, "warps"))
        self.assertTrue(any(name.startswith("native-copy_space-userMy2009bBrain_desc-lesion_mask") for name in warps), warps)


if __name__ == "__main__":
    unittest.main()

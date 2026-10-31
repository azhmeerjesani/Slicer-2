"""Tests that the template space of a lesion and its template-family notice are recorded with the run:
run.json, qa.json, the exported sidecar, provenance.json, methods.md and the study methods (SPEC 7.7: the
space-mismatch notice is a QA value, and all QA values go to the sidecar and qa.json).

A lesion on the FSL MNI152NLin6Asym 2 mm grid mapped with a connectome on MNI152NLin2009cAsym (the demo's
declared space) gives an approximate map; before the fix the notice was only shown in the Map module.

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingLesionSpaceTest.py -v
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

from LesionNetworkMappingLib import connectome, export, geometry, methods, nifti

CONNECTOME_SPACE = "MNI152NLin2009cAsym"
#: The FSL MNI152NLin6Asym 2 mm grid (91 x 109 x 91)
FSL2_SHAPE = (91, 109, 91)
FSL2_AFFINE = np.array([[-2.0, 0, 0, 90.0], [0, 2.0, 0, -126.0], [0, 0, 2.0, -72.0], [0, 0, 0, 1.0]])


def _env():
    return dict(os.environ, PYTHONPATH=MODULE_DIR + os.pathsep + os.environ.get("PYTHONPATH", ""))


def _run_engine(*args):
    result = subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.engine", *args], check=False, capture_output=True, text=True, env=_env(), timeout=300)
    return result


class LesionSpaceRecordTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lnm-lesionspace-test-")
        result = connectome.make_synthetic(os.path.join(cls.tmp, "demo.lnmc"), shape=(20, 24, 20), n_subjects=3, runs=(20, 20), seed=1, name="Synthetic demo")
        connectome.set_metadata(result["path"], space=CONNECTOME_SPACE)
        cls.bundle = result["path"]
        # A sphere at the origin on the FSL 2 mm grid (inside the synthetic grid, which is centred on the origin)
        ijk = np.indices(FSL2_SHAPE).reshape(3, -1).T
        world = ijk @ FSL2_AFFINE[:3, :3].T + FSL2_AFFINE[:3, 3]
        lesion = (np.linalg.norm(world - np.array([0.0, 2.0, 0.0]), axis=1) <= 6.0).reshape(FSL2_SHAPE).astype(np.uint8)
        cls.lesion = os.path.join(cls.tmp, "sub-07_space-MNI152NLin6Asym_res-2_label-lesion_mask.nii.gz")
        nifti.write_nifti(cls.lesion, lesion, FSL2_AFFINE)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _map(self, out, *extra):
        result = _run_engine("--bundle", self.bundle, "--lesion", self.lesion, "--out", out, "--run-json", os.path.join(out, "run.json"), "--threads", "1", *extra)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with open(os.path.join(out, "run.json"), encoding="utf-8") as f:
            run = json.load(f)
        with open(os.path.join(out, "qa.json"), encoding="utf-8") as f:
            qa = json.load(f)
        return run, qa

    def test_family_notice(self):
        notice = geometry.family_notice("MNI152NLin6Asym", CONNECTOME_SPACE)
        self.assertIn("MNI152NLin6Asym", notice)
        self.assertIn("approximate", notice)
        self.assertEqual(geometry.family_notice("MNI152NLin2009cAsym", CONNECTOME_SPACE), "")
        self.assertEqual(geometry.family_notice(None, CONNECTOME_SPACE), "")
        self.assertEqual(geometry.family_notice("MNI152NLin6Asym", None), "")
        self.assertIn("MNI (other)", geometry.family_notice("MNI", CONNECTOME_SPACE))

    def test_mismatch_recorded_in_run_qa_sidecar_and_methods(self):
        out = os.path.join(self.tmp, "run-detected")
        run, qa = self._map(out)  # no --lesion-space: the space of the lesion file's grid
        lesion = run["lesions"][0]
        self.assertEqual(lesion["name"], "sub-07")
        self.assertEqual(lesion["space"], "MNI152NLin6Asym")
        self.assertIn("approximate", lesion["space_notice"])
        self.assertIn(CONNECTOME_SPACE, lesion["space_notice"])
        values = qa["lesions"]["sub-07"]
        self.assertEqual(values["lesion_space"], "MNI152NLin6Asym")
        self.assertEqual(values["space_notice"], lesion["space_notice"])

        dest = os.path.join(self.tmp, "export-detected")
        files = export.export_run(out, dest)
        with open(files["sidecars"][0], encoding="utf-8") as f:
            sidecar = json.load(f)
        self.assertEqual(sidecar["lesion"]["space"], "MNI152NLin6Asym")
        self.assertIn("approximate", sidecar["lesion"]["space_notice"])
        self.assertIn("approximate", sidecar["qa"]["space_notice"])
        with open(files["provenance"], encoding="utf-8") as f:
            provenance = json.load(f)
        self.assertEqual(provenance["lesions"][0]["space"], "MNI152NLin6Asym")
        with open(files["methods"], encoding="utf-8") as f:
            text = f.read()
        self.assertIn("Lesion template space: MNI152NLin6Asym (sub-07)", text)
        self.assertIn("approximate near subcortical structures", text)
        # The study methods (the Report card) read the exported provenance.json
        rendered = methods.render(methods.methods_sections(dest), "md")
        self.assertIn("MNI152NLin6Asym", rendered)
        self.assertIn("approximate near subcortical structures", rendered)

    def test_given_space_of_the_same_family_has_no_notice(self):
        out = os.path.join(self.tmp, "run-given")
        run, qa = self._map(out, "--lesion-space", CONNECTOME_SPACE)
        self.assertEqual(run["lesions"][0]["space"], CONNECTOME_SPACE)
        self.assertEqual(run["lesions"][0]["space_notice"], "")
        self.assertIn("--lesion-space", run["argv"])
        self.assertEqual(qa["lesions"]["sub-07"]["space_notice"], "")
        files = export.export_run(out, os.path.join(self.tmp, "export-given"))
        with open(files["methods"], encoding="utf-8") as f:
            text = f.read()
        self.assertIn(f"Lesion template space: {CONNECTOME_SPACE} (sub-07)", text)
        self.assertNotIn("approximate", text)
        # reproduce.sh repeats the recorded space
        with open(files["reproduce"], encoding="utf-8") as f:
            self.assertIn("--lesion-space", f.read())

    def test_space_count_must_match_the_lesions(self):
        result = _run_engine("--bundle", self.bundle, "--lesion", self.lesion, "--lesion", self.lesion, "--lesion-space", "A", "--lesion-space", "B", "--lesion-space", "C",
                             "--out", os.path.join(self.tmp, "run-bad"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--lesion-space", result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()

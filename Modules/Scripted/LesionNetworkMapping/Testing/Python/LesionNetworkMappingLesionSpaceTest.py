"""Tests that the template space of a lesion and its template-family notice are recorded with the run:
run.json, qa.json, the exported sidecar, provenance.json, methods.md and the study methods (SPEC 7.7: the
space-mismatch notice is a QA value, and all QA values go to the sidecar and qa.json).

A lesion on the FSL MNI152NLin6Asym 2 mm grid mapped with a connectome on MNI152NLin2009cAsym (the demo's
declared space) gives an approximate map; before the fix the notice was only shown in the Map module.

A lesion in native (scanner) space mapped with an MNI connectome by the engine or cohortmaps command line gets a
"native_space" warning event and a recorded note (BB-1 bug engine-cohortmaps-cli-native-lesion-mapped-silently-as-mni);
so does a lesion declared in Talairach space or with a label that is not a known MNI template (BB-1 bug
non-mni-declared-space-talairach-mapped-as-mni).

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

    def _native_lesion(self, name, code=1):
        """A FLAIR-like lesion in scanner space (80 x 80 x 20 voxels of 1.2 x 1.2 x 3 mm, sform and qform code ``code``),
        on no known template grid, with a blob near the origin (inside the synthetic connectome's grid).
        """
        shape = (80, 80, 20)
        affine = np.array([[1.2, 0, 0, -47.4], [0, 1.2, 0, -47.4], [0, 0, 3.0, -28.5], [0, 0, 0, 1.0]])
        ijk = np.indices(shape).reshape(3, -1).T
        world = ijk @ affine[:3, :3].T + affine[:3, 3]
        data = (np.linalg.norm(world - np.array([2.0, 0.0, 0.0]), axis=1) <= 7.0).reshape(shape).astype(np.uint8)
        path = os.path.join(self.tmp, name)
        nifti.write_nifti(path, data, affine, sform_code=code, qform_code=code)
        self.assertIsNone(geometry.detect_space(path))
        return path

    @staticmethod
    def _events(result):
        events = []
        for line in result.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        return events

    def _map_native(self, lesion, out, *extra):
        result = _run_engine("--bundle", self.bundle, "--lesion", lesion, "--out", out, "--run-json", os.path.join(out, "run.json"), "--threads", "1", *extra)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with open(os.path.join(out, "run.json"), encoding="utf-8") as f:
            run = json.load(f)
        with open(os.path.join(out, "qa.json"), encoding="utf-8") as f:
            qa = json.load(f)
        warnings = [event for event in self._events(result) if event.get("event") == "warning" and event.get("code") == "native_space"]
        return run, qa, warnings

    def test_native_lesion_with_mni_connectome_is_never_silent(self):
        """A scanner-space lesion (no known grid, header code 1) mapped with an MNI connectome gets a native_space
        warning event and a recorded note (run.json, qa.json); before the fix it was mapped silently as MNI.
        """
        from LesionNetworkMappingLib import engine

        lesion = self._native_lesion("sub-01_FLAIR_lesion.nii.gz")
        run, qa, warnings = self._map_native(lesion, os.path.join(self.tmp, "run-native"))
        self.assertEqual(len(warnings), 1, warnings)
        self.assertEqual(warnings[0]["lesion"], run["lesions"][0]["name"])
        self.assertIsNone(run["lesions"][0]["space"])
        self.assertEqual(run["lesions"][0]["space_notice"], engine.UNKNOWN_SPACE_NOTICE)
        self.assertEqual(qa["lesions"][run["lesions"][0]["name"]]["space_notice"], engine.UNKNOWN_SPACE_NOTICE)
        # Declared native: the native-space note, not the template-family ("a few millimetres apart") note
        run, qa, warnings = self._map_native(lesion, os.path.join(self.tmp, "run-native-given"), "--lesion-space", "native")
        self.assertEqual(len(warnings), 1, warnings)
        self.assertEqual(run["lesions"][0]["space"], "native")
        self.assertEqual(run["lesions"][0]["space_notice"], engine.NATIVE_SPACE_NOTICE)
        self.assertNotIn("approximate", run["lesions"][0]["space_notice"])
        self.assertEqual(geometry.family_notice("native", CONNECTOME_SPACE), "")
        # A lesion already in MNI space: its header says MNI (code 4), or its template is given
        mni = self._native_lesion("sub-02_space-MNI_lesion.nii.gz", code=nifti.XFORM_MNI)
        run, qa, warnings = self._map_native(mni, os.path.join(self.tmp, "run-header-mni"))
        self.assertEqual(warnings, [])
        self.assertEqual(run["lesions"][0]["space_notice"], "")
        run, qa, warnings = self._map_native(lesion, os.path.join(self.tmp, "run-given-mni"), "--lesion-space", CONNECTOME_SPACE)
        self.assertEqual(warnings, [])
        self.assertEqual(run["lesions"][0]["space_notice"], "")
        # A lesion on a known template grid: no native-space warning
        run, qa, warnings = self._map_native(self.lesion, os.path.join(self.tmp, "run-fsl-grid"))
        self.assertEqual(warnings, [])

    def test_native_lesion_in_cohortmaps_is_never_silent(self):
        lesion = self._native_lesion("sub-01_FLAIR_lesion.nii.gz")
        tsv = os.path.join(self.tmp, "lesions.tsv")
        with open(tsv, "w", encoding="utf-8") as f:
            f.write(f"participant_id\tlesion\nsub-01\t{os.path.basename(lesion)}\nsub-07\t{os.path.basename(self.lesion)}\n")
        out = os.path.join(self.tmp, "cohort-native")
        result = subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.cohortmaps", "--lesions-tsv", tsv, "--bundle", self.bundle, "--out", out, "--threads", "1"],
                                check=False, capture_output=True, text=True, env=_env(), timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        warnings = [event for event in self._events(result) if event.get("event") == "warning" and event.get("code") == "native_space"]
        self.assertEqual(len(warnings), 1, result.stdout)
        self.assertEqual(warnings[0]["participants"], ["sub-01"])
        with open(os.path.join(out, "cohortmaps.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["native_space"], ["sub-01"])

    def test_non_mni_declared_space_is_never_silent(self):
        """A lesion declared in Talairach space or with a label that is not a known MNI template (a misspelt one) gets a
        native_space warning event and the not-MNI note; before the fix it got no warning and the template-family
        ("a few millimetres apart, approximate") note (BB-1 bug non-mni-declared-space-talairach-mapped-as-mni).
        """
        from LesionNetworkMappingLib import engine

        lesion = self._native_lesion("sub-03_lesion.nii.gz", code=3)  # header code 3: Talairach
        for index, label in enumerate(("Talairach", "TT_N27", "MNI512NLin6Asym")):
            run, qa, warnings = self._map_native(lesion, os.path.join(self.tmp, f"run-not-mni-{index}"), "--lesion-space", label)
            notice = engine.NOT_MNI_SPACE_NOTICE.format(space=label)
            self.assertEqual(len(warnings), 1, (label, warnings))
            self.assertEqual(run["lesions"][0]["space_notice"], notice)
            self.assertEqual(qa["lesions"][run["lesions"][0]["name"]]["space_notice"], notice)
            self.assertNotIn("approximate", notice)
            self.assertEqual(geometry.family_notice(label, CONNECTOME_SPACE), "")
            self.assertTrue(engine.is_space_warning(notice))
        # MNI template labels of no known family keep the template-family note and no warning
        for label in ("MNI", "MNI305", "userAbc1", "user:abc"):
            self.assertTrue(geometry.is_mni_space(label), label)
            self.assertEqual(engine.native_space_notice(label, lesion, True), "")
            self.assertFalse(engine.is_space_warning(geometry.family_notice(label, CONNECTOME_SPACE)))
        run, qa, warnings = self._map_native(lesion, os.path.join(self.tmp, "run-mni-other"), "--lesion-space", "MNI")
        self.assertEqual(warnings, [])
        self.assertIn("MNI (other)", run["lesions"][0]["space_notice"])

    def test_non_mni_study_space_in_cohortmaps_is_never_silent(self):
        """cohortmaps --study: a subject whose recorded space is Talairach is listed in native_space with a warning."""
        from LesionNetworkMappingLib import cohortmaps
        from LesionNetworkMappingLib.study import Study

        study = Study.create(os.path.join(self.tmp, "study-tal"), name="Talairach", space=CONNECTOME_SPACE)
        target = os.path.join(study.path, "lesions", os.path.basename(self.lesion))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(self.lesion, target)
        study.add_subject("sub-07", {"lesion": study.relpath(target), "space": "Talairach"})
        bundle = connectome.ConnectomeBundle.open(self.bundle)
        items = [{"participant_id": "sub-07", "lesion": target}]
        self.assertEqual(cohortmaps._native_space_lesions(items, bundle, study), ["sub-07"])
        study.set("sub-07", space="MNI152NLin6Asym")
        self.assertEqual(cohortmaps._native_space_lesions(items, bundle, study), [])

    def test_space_count_must_match_the_lesions(self):
        result = _run_engine("--bundle", self.bundle, "--lesion", self.lesion, "--lesion", self.lesion, "--lesion-space", "A", "--lesion-space", "B", "--lesion-space", "C",
                             "--out", os.path.join(self.tmp, "run-bad"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--lesion-space", result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()

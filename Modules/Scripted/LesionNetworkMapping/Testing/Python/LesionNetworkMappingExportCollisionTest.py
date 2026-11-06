"""Export never silently replaces the maps of another lesion that has the same name (BB-1).

Two lesions with the same run name (two segmentations both called "Lesion", files with the same name, or before the
fix any two names written in another script, which all became "-Lesion") exported into one folder: the second export
copied its maps over the first lesion's ``<name>_conn-<label>_desc-*_funcmap.nii.gz`` and rewrote its sidecar, and the
only feedback was the toast "Exported · conn-<label>". ``export.foreign_lesions`` finds such files and the Export card
asks before replacing them (or exports into another folder).

The library tests run without Slicer's GUI::

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingExportCollisionTest.py -v

The Export card tests need the Slicer application and are skipped under a plain PythonSlicer.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

from LesionNetworkMappingLib import export

try:
    import slicer

    _HAS_APP = getattr(slicer, "app", None) is not None
except ImportError:
    _HAS_APP = False

BUNDLE = {"name": "Synthetic demo", "label": "SYNTHETIC", "n_voxels": 10, "n_subjects": 120}


class _Helpers:
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="lnm-export-collision-")
        self.dest = os.path.join(self.work, "Results", export.run_dir_name(BUNDLE["name"]))

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def runMeta(self, patient, name="Lesion"):
        """A finished run of one lesion named ``name`` whose lesion file is ``patient``'s (its maps hold ``patient``)."""
        runDir = os.path.join(self.work, "runs", patient)
        os.makedirs(runDir, exist_ok=True)
        maps = {}
        for key in ("AvgR", "AvgFz", "T"):
            path = os.path.join(runDir, f"{name}_{key}.nii.gz")
            with open(path, "wb") as f:
                f.write(f"{patient}-{key}".encode())
            maps[key] = path
        meta = {"status": "done", "n": 120, "bundle": BUNDLE, "maps": {name: maps},
                "lesions": [{"name": name, "file": f"lesion/{name}.nii.gz", "sha256": export.subjects_sha256([patient])}]}
        with open(os.path.join(runDir, "run.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f)
        return runDir, meta

    def write(self, meta, dest=None):
        return export.write_run(dest or self.dest, meta["maps"], [entry["name"] for entry in meta["lesions"]], BUNDLE, run_meta=meta)

    def mapContents(self, dest=None):
        dest = dest or self.dest
        return {name: open(os.path.join(dest, name), "rb").read().decode() for name in sorted(os.listdir(dest)) if name.endswith(".nii.gz")}


class LesionNetworkMappingExportCollisionLibTest(_Helpers, unittest.TestCase):
    def test_other_lesion_of_the_same_name_is_foreign(self):
        _runDir, first = self.runMeta("patient1")
        self.assertEqual(export.foreign_lesions(self.dest, first), [], "an empty or missing folder holds no other lesion")
        self.write(first)
        self.assertEqual(export.foreign_lesions(self.dest, first), [], "exporting the same lesion again updates its own files")
        _runDir, second = self.runMeta("patient2")
        self.assertEqual(export.foreign_lesions(self.dest, second), ["Lesion"])
        _runDir, renamed = self.runMeta("patient2", name="Lesion2")
        self.assertEqual(export.foreign_lesions(self.dest, renamed), [], "a lesion of another name replaces nothing")

    def test_lesion_of_another_name_keeps_the_run_level_files_of_the_first(self):
        """A second lesion exported into the folder of the first: provenance.json, methods.md and reproduce.sh cover both."""
        _runDir, first = self.runMeta("patient1")
        self.write(first)
        _runDir, second = self.runMeta("patient2", name="Lesion2")
        files = self.write(second)
        with open(files["provenance"], encoding="utf-8") as f:
            provenance = json.load(f)
        firstFiles = sorted(name for name in os.listdir(self.dest) if name.startswith("Lesion_"))
        self.assertEqual(len(firstFiles), 4, "the first lesion's three maps and sidecar stay")
        self.assertEqual([lesion["name"] for lesion in provenance["lesions"]], ["Lesion2"], "the top level describes this export's run")
        self.assertEqual([[lesion["name"] for lesion in record["lesions"]] for record in provenance["earlier_runs"]], [["Lesion"]])
        self.assertEqual(provenance["earlier_runs"][0]["N"], 120)
        listed = sorted(name for name in os.listdir(self.dest) if name != "provenance.json")
        self.assertEqual(sorted(provenance["files"]), listed, "provenance.json lists every file of the folder")
        with open(files["reproduce"], encoding="utf-8") as f:
            script = f.read()
        self.assertIn("lesion/Lesion2.nii.gz", script)
        self.assertIn("lesion/Lesion.nii.gz", script, "reproduce.sh also recomputes the first lesion")
        self.assertIn('--out "$OUT/earlier-1"', script)
        if shutil.which("sh"):
            import subprocess

            self.assertEqual(subprocess.run(["sh", "-n", files["reproduce"]], check=False).returncode, 0, "reproduce.sh is valid shell")
        with open(files["methods"], encoding="utf-8") as f:
            methods = f.read()
        self.assertIn("earlier export 1 in this folder (Lesion)", methods)
        self.assertEqual(methods.count("For each of N = 120 subjects"), 2, "methods.md describes both runs")

        # The first lesion exported again: its own record is replaced, the second lesion's becomes the earlier one
        provenance = json.load(open(self.write(first)["provenance"], encoding="utf-8"))
        self.assertEqual([lesion["name"] for lesion in provenance["lesions"]], ["Lesion"])
        self.assertEqual([[lesion["name"] for lesion in record["lesions"]] for record in provenance["earlier_runs"]], [["Lesion2"]])
        self.assertEqual(sorted(provenance["files"]), listed)

        # A lesion of the same name replaced (as asked): the replaced lesion is not described any more
        os.remove(export.sidecar_path(self.dest, "Lesion2", BUNDLE["name"]))
        _runDir, third = self.runMeta("patient3")
        provenance = json.load(open(self.write(third)["provenance"], encoding="utf-8"))
        self.assertNotIn("earlier_runs", provenance, "Lesion was replaced and Lesion2's sidecar is gone")

    def test_maps_without_a_readable_sidecar_count_as_foreign(self):
        _runDir, first = self.runMeta("patient1")
        self.write(first)
        sidecar = export.sidecar_path(self.dest, "Lesion", BUNDLE["name"])
        with open(sidecar, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(export.foreign_lesions(self.dest, first), ["Lesion"])
        os.remove(sidecar)
        self.assertEqual(export.foreign_lesions(self.dest, first), ["Lesion"])


@unittest.skipUnless(_HAS_APP, "needs the Slicer application")
class LesionNetworkMappingExportCollisionCardTest(_Helpers, unittest.TestCase):
    """The Export card asks before an export replaces another lesion's maps, and can export into another folder."""

    def setUp(self):
        super().setUp()
        import qt

        from LesionNetworkMappingUI import card_export

        self.qt, self.card_export = qt, card_export
        self.settings = qt.QSettings()
        self.savedFolder = self.settings.value(card_export.LAST_FOLDER_SETTING)
        self.realDialog = qt.QFileDialog.getExistingDirectory
        qt.QFileDialog.getExistingDirectory = lambda *args: os.path.join(self.work, "Results")
        self.questions = []

    def tearDown(self):
        self.qt.QFileDialog.getExistingDirectory = self.realDialog
        if self.savedFolder is None:
            self.settings.remove(self.card_export.LAST_FOLDER_SETTING)
        else:
            self.settings.setValue(self.card_export.LAST_FOLDER_SETTING, self.savedFolder)
        super().tearDown()

    def card(self, runDir, answer, study=None):
        """An Export card whose run is ``runDir`` and whose question is answered with ``answer`` (exportTo: export.export_run)."""
        card = self.card_export.ExportCard.__new__(self.card_export.ExportCard)

        class Run:
            pass

        run = Run()
        run.runDir, run.n, run.partial = runDir, 120, False
        bundle = Run()
        bundle.name, bundle.n_subjects = BUNDLE["name"], 120
        ctx = Run()
        ctx.bundle = bundle
        card.ctx = ctx
        card._run = lambda: run
        card.studyDestination = lambda: study

        def askReplace(destination, names, otherDestination):
            self.questions.append((destination, list(names), otherDestination))
            return answer

        card.askReplace = askReplace
        card.exportTo = lambda destination: (export.export_run(runDir, destination), destination)[1]
        return card

    def test_second_lesion_of_the_same_name_does_not_replace_the_first(self):
        firstDir, _meta = self.runMeta("patient1")
        self.assertEqual(self.card(firstDir, "replace").export(), self.dest)
        self.assertEqual(self.questions, [], "nothing to replace: no question")
        first = self.mapContents()
        secondDir, _meta = self.runMeta("patient2")

        # Cancel: nothing is written
        self.assertIsNone(self.card(secondDir, None).export())
        self.assertEqual(self.questions, [(self.dest, ["Lesion"], self.dest + "-2")])
        self.assertEqual(self.mapContents(), first)

        # Export into another folder: the first lesion's maps stay
        self.assertEqual(self.card(secondDir, "new").export(), self.dest + "-2")
        self.assertEqual(self.mapContents(), first)
        self.assertTrue(all(text.startswith("patient2") for text in self.mapContents(self.dest + "-2").values()))

        # The same lesion exported again updates its own files without a question
        del self.questions[:]
        self.assertEqual(self.card(firstDir, None).export(), self.dest)
        self.assertEqual(self.questions, [])

        # A third lesion of the same name: the next free folder is offered; Replace replaces, as asked
        thirdDir, _meta = self.runMeta("patient3")
        self.assertEqual(self.card(thirdDir, "replace").export(), self.dest)
        self.assertEqual(self.questions[-1], (self.dest, ["Lesion"], self.dest + "-3"))
        self.assertTrue(all(text.startswith("patient3") for text in self.mapContents().values()))

    def test_study_export_asks_without_another_folder(self):
        firstDir, _meta = self.runMeta("patient1")
        self.card(firstDir, None, study=self.dest).export()
        first = self.mapContents()
        secondDir, _meta = self.runMeta("patient2")
        self.assertIsNone(self.card(secondDir, None, study=self.dest).export())
        self.assertEqual(self.questions, [(self.dest, ["Lesion"], None)])
        self.assertEqual(self.mapContents(), first)


if __name__ == "__main__":
    unittest.main()

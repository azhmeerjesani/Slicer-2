"""Tests that the fast index dialog of another connectome leaves the Cohort card's lesion maps alone (BB-1).

Manage… → select a connectome that is not the Connectome card's → Build fast index… opened the dialog for that connectome,
but the dialog took the Cohort card's lesion maps (computed with the Connectome card's connectome) as its own: it offered
and chose "The N subjects of the lesion maps" whenever the subject ids had the same names, wrote their subject list into the
cohort's fastparity folder, and once that index existed ran the parity check of the other connectome's index against them,
overwriting the cohort's parity.json, so fast mode of the Nulls card switched off (bug
lnm-reference-fastindex-paths:fastindex-dialog-other-connectome-uses-cohort-subjects-and-overwrites-parity). The dialog now
uses the lesion maps only when they were computed with its connectome (cohortmaps.json).

They need the Slicer application and build two tiny synthetic connectomes (no network, no index build)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingFastIndexOtherBundleUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import json
import os
import shutil
import tempfile
import types
import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class LesionNetworkMappingFastIndexOtherBundleUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from LesionNetworkMappingLib import connectome, export

        cls.tmp = tempfile.mkdtemp(prefix="lnm fastindex other ")
        cls.bundles = {}
        for name, n in (("Synthetic", 6), ("Medium", 8)):  # both name their subjects sub-001, sub-002, …
            info = connectome.make_synthetic(os.path.join(cls.tmp, f"{name}.lnmc"), shape=(12, 14, 12), n_subjects=n, runs=(12,), seed=4, name=name)
            cls.bundles[name] = connectome.ConnectomeBundle.open(info["path"])
        own = cls.bundles["Synthetic"]
        cls.cohortIds = list(own.subject_ids)[:4]
        cls.folder = os.path.join(cls.tmp, "Cohort", "derivatives", "lnm", "conn-Synthetic", "cohort")
        os.makedirs(cls.folder)
        cls.mapsTsv = os.path.join(cls.folder, "maps.tsv")
        with open(cls.mapsTsv, "w", encoding="utf-8") as f:
            f.write("participant_id\tlesion\tavg_fz\n")
        record = {"format": "slicer-lnm-cohortmaps", "status": "done", "bundle": export.bundle_info(own), "subject_ids": cls.cohortIds}
        cls.recordPath = os.path.join(cls.folder, "cohortmaps.json")
        with open(cls.recordPath, "w", encoding="utf-8") as f:
            json.dump(record, f)
        cls.parityFolder = os.path.join(cls.folder, "fastparity")

    @classmethod
    def tearDownClass(cls):
        for bundle in cls.bundles.values():
            bundle.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _dialog(self, name):
        from LesionNetworkMappingUI import manager_fastindex

        ctx = types.SimpleNamespace(cohortMaps=self.mapsTsv, bundle=self.bundles["Synthetic"])  # the Connectome card's connectome
        dialog = manager_fastindex.FastIndexDialog(ctx, self.bundles[name].path)
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        dialog.cache = os.path.join(self.tmp, "cache")
        dialog.refresh()
        return dialog

    def test_1_own_connectome_uses_the_lesion_maps(self):
        dialog = self._dialog("Synthetic")
        self.assertEqual(dialog.mapsTsv(), self.mapsTsv)
        self.assertIsNone(dialog.mapsConnectome())
        keys = [key for _text, key, _ids in dialog.subjectOptions()]
        self.assertIn("cohort", keys)
        self.assertEqual(dialog.selectedSubjects(), ("cohort", self.cohortIds), "the dialog of the maps' connectome no longer chooses their subjects")

    def test_2_other_connectome_leaves_the_lesion_maps_alone(self):
        dialog = self._dialog("Medium")
        self.assertIsNone(dialog.mapsTsv(), "the Medium dialog takes the lesion maps computed with Synthetic as its own")
        self.assertEqual(dialog.mapsConnectome(), "Synthetic")
        options = dialog.subjectOptions()
        self.assertNotIn("cohort", [key for _text, key, _ids in options], "the Medium dialog offers the subjects of the Synthetic lesion maps")
        key, ids = dialog.selectedSubjects()
        self.assertEqual(key, "all")
        self.assertEqual(ids, list(self.bundles["Medium"].subject_ids))
        args = dialog.buildArguments()
        self.assertNotIn("--subject-list", args, "the Medium build reads the subject list of the Synthetic cohort")
        self.assertFalse(os.path.exists(self.parityFolder), "the Medium dialog wrote into the Synthetic cohort's fastparity folder")
        self.assertIsNone(dialog.cohortIndexFolder())
        self.assertIsNone(dialog.parityArguments(), "the Medium dialog would write its parity report into the Synthetic cohort")
        enabled, reason = dialog.parityState()
        self.assertFalse(enabled)
        self.assertIn("Synthetic", reason)
        self.assertFalse(dialog.parityButton.enabled)
        self.assertIn("Synthetic", dialog.subjectsHint.text)

    def test_3_connectome_of_older_records(self):
        """The record's manifest sha256 or its path; without either, the Connectome card's connectome."""
        from LesionNetworkMappingUI import manager_fastindex

        own, other = self.bundles["Synthetic"], self.bundles["Medium"]
        with open(self.recordPath, encoding="utf-8") as f:
            original = f.read()
        try:
            record = json.loads(original)
            manifest = record["bundle"]["manifest_sha256"]
            for info in ({"name": "Synthetic", "path": own.path}, {"name": "Synthetic", "manifest_sha256": manifest, "path": os.path.join(self.tmp, "moved.lnmc")},
                         {"name": "Synthetic", "manifest_sha256": "0" * 64, "path": own.path}):
                record["bundle"] = info
                with open(self.recordPath, "w", encoding="utf-8") as f:
                    json.dump(record, f)
                self.assertIsNone(manager_fastindex.otherConnectome(self.mapsTsv, own), info)
                self.assertEqual(manager_fastindex.otherConnectome(self.mapsTsv, other), "Synthetic", info)
            record.pop("bundle")
            with open(self.recordPath, "w", encoding="utf-8") as f:
                json.dump(record, f)
            self.assertIsNone(manager_fastindex.otherConnectome(self.mapsTsv, own, own))
            self.assertEqual(manager_fastindex.otherConnectome(self.mapsTsv, other, own), "Synthetic")
            self.assertIsNone(manager_fastindex.otherConnectome(self.mapsTsv, other))
        finally:
            with open(self.recordPath, "w", encoding="utf-8") as f:
                f.write(original)

    def test_4_unreachable_connectome_opens_with_a_sentence(self):
        """A connectome whose drive was unplugged: the dialog opens (no raw AttributeError) and says it cannot be opened."""
        from LesionNetworkMappingUI import manager_fastindex

        for path in (os.path.join(self.tmp, "unplugged.lnmc"), None):
            ctx = types.SimpleNamespace(cohortMaps=self.mapsTsv, bundle=self.bundles["Synthetic"])
            dialog = manager_fastindex.FastIndexDialog(ctx, path)
            self.addCleanup(dialog.deleteLater)
            self.addCleanup(dialog.close)
            self.assertIsNone(dialog.bundle)
            self.assertFalse(dialog.buildButton.enabled)
            self.assertIsNone(dialog._existingIndex(["sub-001"]))


if __name__ == "__main__":
    unittest.main()

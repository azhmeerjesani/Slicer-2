"""Tests that a connectome never takes the lesion maps of another connectome with the same name as its own (BB-1).

The cohort folder ``derivatives/lnm/conn-<label>/cohort`` is named after the connectome name only, so two registered
connectomes with the same name (the same grid and subject ids but other time series, or a subset) share it. Choosing the
second one showed the first one's lesion maps as its own (Cohort card "Lesion maps: …", Compute "Done"), and the Nulls card
ran its null models on them and wrote into the first one's results (bug
lnm-reference-fastindex-paths-r12-rerun:same-name-connectome-uses-other-connectomes-cohort-maps-and-nulls). The Map module now
takes the maps only when their cohortmaps.json names the chosen connectome (its connectome.json sha256 or its folder), and the
cards show none of the other connectome's results.

They need the Slicer application and build two tiny synthetic connectomes (no network, no mapping)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingSameNameCohortMapsUITest

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


class LesionNetworkMappingSameNameCohortMapsUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from LesionNetworkMappingLib import connectome, export

        cls.tmp = tempfile.mkdtemp(prefix="lnm same name ")
        cls.bundles = {}
        for key, seed in (("A", 4), ("B", 7)):  # same name, grid and subject ids; other time series
            info = connectome.make_synthetic(os.path.join(cls.tmp, key, "Synthetic.lnmc"), shape=(12, 14, 12), n_subjects=6, runs=(12,), seed=seed, name="Synthetic")
            cls.bundles[key] = connectome.ConnectomeBundle.open(info["path"])
        cls.study = types.SimpleNamespace(path=os.path.join(cls.tmp, "Study"))
        cls.folder = os.path.join(cls.study.path, "derivatives", "lnm", "conn-Synthetic", "cohort")
        os.makedirs(cls.folder)
        cls.mapsTsv = os.path.join(cls.folder, "maps.tsv")
        with open(cls.mapsTsv, "w", encoding="utf-8") as f:
            f.write("participant_id\tlesion\tavg_fz\n")
        record = {"format": "slicer-lnm-cohortmaps", "status": "done", "bundle": export.bundle_info(cls.bundles["A"]), "subject_ids": list(cls.bundles["A"].subject_ids)}
        with open(os.path.join(cls.folder, "cohortmaps.json"), "w", encoding="utf-8") as f:
            json.dump(record, f)

    @classmethod
    def tearDownClass(cls):
        for bundle in cls.bundles.values():
            bundle.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _widget(self, key):
        """The cohort-maps lookup of the Map widget for the connectome ``key``, without the module's GUI."""
        from LesionNetworkMapping import LesionNetworkMappingLogic, LesionNetworkMappingWidget

        widget = types.SimpleNamespace(cohortStudy=self.study, bundle=self.bundles[key], cohortMapsPath=None, _parameterNode=None, logic=LesionNetworkMappingLogic,
                                       _publishCohortMaps=lambda: None)
        for name in ("cohortOutputDir", "_cohortFolderOfOtherConnectome", "_findCohortMaps"):
            if hasattr(LesionNetworkMappingWidget, name):
                setattr(widget, name, types.MethodType(getattr(LesionNetworkMappingWidget, name), widget))
        return widget

    def test_1_own_connectome_finds_its_maps(self):
        widget = self._widget("A")
        self.assertEqual(widget._findCohortMaps(), self.mapsTsv)
        self.assertEqual(widget.cohortOutputDir(), self.folder)
        self.assertEqual(widget.cohortOutputDir(ownOnly=True), self.folder)

    def test_2_same_name_connectome_does_not_take_them(self):
        widget = self._widget("B")
        self.assertEqual(self.bundles["A"].name, self.bundles["B"].name)
        self.assertIsNone(widget._findCohortMaps(), "connectome B takes the lesion maps of connectome A (same name) as its own")
        self.assertIsNone(widget.cohortMapsPath)
        self.assertEqual(widget.cohortOutputDir(), self.folder, "B maps its lesions into the cohort folder of its name")
        self.assertIsNone(widget.cohortOutputDir(ownOnly=True), "the cards of B show the results in A's cohort folder")


if __name__ == "__main__":
    unittest.main()

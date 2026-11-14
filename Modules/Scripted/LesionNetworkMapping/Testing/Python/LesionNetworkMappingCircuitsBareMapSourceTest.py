"""Test the Circuits card's line for a bare circuit map (bug circuits-card-says-provenance-not-recorded-for-slnm-circuit).

A result map with slnm.json (or glm.json) beside it and no circuit.json used to read "its provenance is not recorded" although
the run record names the lesions that made it (and Score lesions said "made from N of the scored lesions"). The line now names
the run and its lesion count; without a run record the generic text stays.

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingCircuitsBareMapSourceTest
"""

import json
import os
import tempfile
import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class CircuitsBareMapSourceTest(unittest.TestCase):
    def setUp(self):
        slicer.util.selectModule("LesionNetworkMapping")
        self.folder = tempfile.mkdtemp(prefix="bare-map-")
        self.addCleanup(__import__("shutil").rmtree, self.folder, True)

    def _text(self):
        from LesionNetworkMappingUI import card_circuits

        return card_circuits.CircuitsCard._bareMapText(os.path.join(self.folder, "r.nii.gz"))

    def _maps(self, n):
        with open(os.path.join(self.folder, "maps.tsv"), "w") as handle:
            handle.write("participant_id\tlesion_sha256\n")
            for i in range(n):
                handle.write(f"sub-{i}\t{i:064x}\n")

    def test_run_record_names_the_source(self):
        self._maps(3)
        with open(os.path.join(self.folder, "slnm.json"), "w") as handle:
            json.dump({"run_id": "abc", "maps_tsv": "maps.tsv"}, handle)
        text = self._text()
        self.assertNotIn("not recorded", text)
        self.assertIn("slnm", text)
        self.assertIn("3 lesion(s)", text)

    def test_no_record_keeps_the_generic_text(self):
        self.assertIn("not recorded", self._text())

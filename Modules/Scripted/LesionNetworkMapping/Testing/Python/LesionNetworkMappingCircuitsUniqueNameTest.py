"""Test that a repeated circuit name gets a readable suffix (bug circuits-second-seed-of-cluster1-named-cluster12).

A second circuit named Cluster1 was called Cluster12, which reads as cluster 12. It is now Cluster1Run2, like the VLSM tab.

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingCircuitsUniqueNameTest
"""

import os
import shutil
import tempfile
import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class CircuitsUniqueNameTest(unittest.TestCase):
    def setUp(self):
        slicer.util.selectModule("LesionNetworkMapping")
        from LesionNetworkMappingUI import card_circuits

        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, True)
        card = object.__new__(card_circuits.CircuitsCard)
        card.circuitsFolder = lambda: self.folder
        self.card = card

    def test_repeated_name_gets_run_suffix(self):
        names = []
        for _ in range(3):
            name, path = self.card._uniqueFolder("Cluster1")
            os.makedirs(path)
            names.append(name)
        self.assertEqual(names, ["Cluster1", "Cluster1Run2", "Cluster1Run3"])
        self.assertTrue(os.path.isdir(os.path.join(self.folder, "circuit-Cluster1Run2")))


if __name__ == "__main__":
    unittest.main()

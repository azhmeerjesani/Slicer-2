"""Test that an all-zero conjunction map cannot be seeded (bug circuits-empty-conjunction-seed-enabled-raw-valueerror-empty-folder).

The Circuits card offered Seed this for a conjunction map without a voxel, the job failed with a raw "ValueError: ..." notice
and left an empty circuit folder. The card now says the map has no voxel (Seed this is disabled), and a failed seed removes
its folder and shows the message without the exception class name.

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingCircuitsEmptyConjunctionTest
"""

import os
import tempfile
import types
import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class CircuitsEmptyConjunctionTest(unittest.TestCase):
    def setUp(self):
        slicer.util.selectModule("LesionNetworkMapping")
        from LesionNetworkMappingUI import card_circuits

        self.module = card_circuits
        card = object.__new__(card_circuits.CircuitsCard)
        card.sourceKind = lambda: "conjunction"
        card._signCache = {}
        card.seedSign = lambda: "pos"
        self.card = card

    def test_empty_conjunction_has_no_seed(self):
        card = self.card
        card._groupMapPath = lambda: ("/nonexistent/group_desc-conj_map.nii.gz", "conj")
        card.mapSigns = lambda path: (False, False)
        source = card.seedSource()
        self.assertTrue(source.error)
        self.assertIn("no voxel", source.error)
        card.mapSigns = lambda path: (True, False)
        self.assertFalse(card.seedSource().error)

    def test_failed_seed_leaves_no_folder_and_no_class_name(self):
        card = self.card
        notices = []
        card._setNotice = notices.append
        card._cleanedUp = False
        card.seedProgress = types.SimpleNamespace(update=lambda: None)
        card.nameEdit = types.SimpleNamespace(setProperty=lambda *a: None)
        card.refresh = lambda: None
        with tempfile.TemporaryDirectory() as root:
            out = os.path.join(root, "circuit-Conj")
            os.makedirs(out)
            job = types.SimpleNamespace(state="failed", failureReason="ValueError: The seed is empty", succeeded=lambda: False)
            card._onSeedDone(job, out)
            self.assertFalse(os.path.exists(out))
        self.assertEqual(notices, ["The circuit could not be made: The seed is empty"])


if __name__ == "__main__":
    unittest.main()

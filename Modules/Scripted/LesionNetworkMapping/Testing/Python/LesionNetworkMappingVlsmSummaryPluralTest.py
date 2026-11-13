"""Test of the VLSM result summary wording (bug vlsm-summary-1-clusters-plural): "1 clusters" now reads "1 cluster".

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingVlsmSummaryPluralTest
"""

import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class VlsmSummaryPluralTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        slicer.util.selectModule("LesionNetworkMapping")
        from LesionNetworkMappingUI import cohort_vlsm

        cls.vlsm = cohort_vlsm

    def test_one_cluster_is_singular(self):
        text = self.vlsm._fweSummaryText(28, 1, "0.006")
        self.assertEqual(text, "FWE p ≤ 0.05: 28 voxels in 1 cluster (smallest FWE p 0.006)")

    def test_plural_and_single_voxel(self):
        self.assertIn("5 voxels in 2 clusters", self.vlsm._fweSummaryText(5, 2, "0.01"))
        self.assertIn("1 voxel in 1 cluster", self.vlsm._fweSummaryText(1, 1, "0.01"))
        self.assertIn("0 voxels in 0 clusters", self.vlsm._fweSummaryText(0, 0, "n/a"))

    def test_cluster_size_text(self):
        self.assertEqual(self.vlsm._clusterSizeText(1, 0.008), "1 voxel (0.008 mL)")
        self.assertEqual(self.vlsm._clusterSizeText(250, 2.0), "250 voxels (2.0 mL)")
        self.assertEqual(self.vlsm._clusterSizeText(3, 0.024), "3 voxels (0.024 mL)")

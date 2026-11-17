"""Test of the peak position in the VLSM cluster choices (bug vlsm-cluster-peak-half-even-rounding-disagrees-with-clusters-card).

``_whereText`` rounded half to even ("L 48" for -48.5, "I 0" for -0.5) while the Clusters card rounds half away from
zero (-49, -1), so the same peak read differently (the 2009c 2 mm grid has every voxel centre at .5 mm).

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingVlsmWhereTextTest
"""

import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class VlsmWhereTextTest(unittest.TestCase):
    def test_peak_reads_like_the_clusters_card(self):
        from LesionNetworkMappingUI import card_clusters, cohort_vlsm

        self.assertEqual(cohort_vlsm._whereText(-48.5, 9.5, -0.5), "L 49 A 10 I 1")
        for value in (-48.5, 48.5, 49.5, -0.5, 0.5, 7.0, -58.5):
            text = cohort_vlsm._whereText(value, 0, 0).split()[1]
            self.assertEqual(text, card_clusters.formatCoordinate(abs(value)))


if __name__ == "__main__":
    unittest.main()

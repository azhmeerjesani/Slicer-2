"""Test of the coordinates in the Circuits card (bug circuits-card-coordinates-half-even-disagree-with-clusters-table).

``card_circuits.whereText`` rounded half to even ("L 40 . A 10 . I 0" for -40.5, 9.5, -0.5) while the Clusters card
rounds half away from zero (-41, 10, -1).

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingCircuitsWhereTextTest
"""

import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class CircuitsWhereTextTest(unittest.TestCase):
    def test_circuits_line_reads_like_the_clusters_card(self):
        from LesionNetworkMappingUI import card_circuits, card_clusters

        self.assertEqual(card_circuits.whereText(-40.5, 9.5, -0.5), "L 41 · A 10 · I 1")
        self.assertEqual(card_circuits.whereText(-34.5, -30.5, 9.5), "L 35 · P 31 · S 10")
        for value in (-40.5, 40.5, 49.5, -0.5, 0.5, 7.0, -58.5):
            text = card_circuits.whereText(value, 0, 0).split()[1]
            self.assertEqual(text, card_clusters.formatCoordinate(abs(value)))


if __name__ == "__main__":
    unittest.main()

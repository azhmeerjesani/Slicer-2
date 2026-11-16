"""Test of the singular in the Circuits card (bug circuits-one-voxels-plural).

A one-voxel cluster read "(1 voxels, ...)" in the source line and "1 seed voxels" in the circuit line.

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingCircuitsOneVoxelTest
"""

import types
import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class _Table:
    def __init__(self, vector):
        self.cluster_vector = vector


class CircuitsOneVoxelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        slicer.util.selectModule("LesionNetworkMapping")
        from LesionNetworkMappingUI import card_circuits

        cls.module = card_circuits

    def _sourceText(self, vector):
        import numpy as np

        card = object.__new__(self.module.CircuitsCard)
        card.ctx = types.SimpleNamespace(currentRun=None, parameterNode=types.SimpleNamespace(statistic="T"))
        grid = types.SimpleNamespace(shape=(2, 2, 1), affine=np.eye(4))
        source = types.SimpleNamespace(key="conj", name="Cohort conj", statistic="T", grid=grid, thresholdText=lambda: "|conj| ≥ 0.5")
        row = {"cluster": 1, "id": 1, "xyz": (1.0, 2.0, 3.0)}
        card.sourceKind = lambda: "cluster"
        card.selectedCluster = lambda: (row, _Table(vector), source)
        card.radiusSpin = types.SimpleNamespace(value=3.0)
        seed = card.seedSource()
        self.assertIsNone(seed.error)
        return seed.text

    def test_source_line_singular_and_plural(self):
        self.assertIn("(1 voxel,", self._sourceText([1, 0, 0, 0]))
        self.assertNotIn("1 voxels", self._sourceText([1, 0, 0, 0]))
        self.assertIn("(2 voxels,", self._sourceText([1, 1, 0, 0]))

    def test_circuit_line_singular_and_plural(self):
        for count, expected in ((1, "1 seed voxel"), (2, "2 seed voxels")):
            card = object.__new__(self.module.CircuitsCard)
            card.circuitLabel = types.SimpleNamespace(setText=lambda text, box=[]: box.append(text) or None)
            record = {"parent": {}, "seed": {"kind": "mask", "voxels_in_mask": count}}
            card.currentCircuitRecord = lambda record=record: record
            card.currentCircuitPath = lambda: "/nonexistent/circuit.nii.gz"
            card.showButton = types.SimpleNamespace(setEnabled=lambda flag: None)
            texts = []
            card.circuitLabel = types.SimpleNamespace(setText=texts.append)
            card._showCircuitInfo()
            self.assertTrue(texts[-1].startswith(expected + " ") or texts[-1].startswith(expected + "·") or expected in texts[-1].split(" · ")[0], texts[-1])
            if count == 1:
                self.assertNotIn("1 seed voxels", texts[-1])


if __name__ == "__main__":
    unittest.main()

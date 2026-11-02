"""Test of the parent that a circuit seeded from the Result map records (bug map-single:circuit-from-fz-cluster-records-t-map-as-parent).

A circuit seeded from a cluster (or the peak of a cluster) of the Fz-thresholded Result recorded the run's T map as its parent
(circuit.json ``parent`` and the Circuits card's "from ..."), although the cluster was cut from the Fz map. The parent is now the
run output of the statistic the cluster source was made from: T, Fz (``_desc-AvgRFz``) or r (``_desc-AvgR``).

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingCircuitsParentTest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

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


class _Table:
    cluster_vector = [1, 1, 0, 0]


class CircuitsParentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        slicer.util.selectModule("LesionNetworkMapping")
        from LesionNetworkMappingUI import card_circuits

        cls.module = card_circuits
        cls.folder = tempfile.mkdtemp(prefix="lnm-circuits-parent-")
        cls.outputs = []
        for desc in ("AvgR", "AvgRFz", "T"):  # AvgR first: "_desc-AvgR" must not match the AvgRFz file
            path = os.path.join(cls.folder, f"Lesion_conn-Demo_desc-{desc}_funcmap.nii.gz")
            with open(path, "wb") as handle:
                handle.write(b"x")
            cls.outputs.append(path)
        cls.paths = dict(zip(("r", "Fz", "T"), cls.outputs, strict=True))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.folder, ignore_errors=True)

    def _card(self, statistic, kind, shownStatistic="T"):
        """A Circuits card controller (without its widgets) whose Clusters card holds a Result source of ``statistic``."""
        import numpy as np

        card = object.__new__(self.module.CircuitsCard)
        run = types.SimpleNamespace(outputs=list(self.outputs))
        card.ctx = types.SimpleNamespace(currentRun=run, parameterNode=types.SimpleNamespace(statistic=shownStatistic))
        grid = types.SimpleNamespace(shape=(2, 2, 1), affine=np.eye(4))
        source = types.SimpleNamespace(key="result", name="Lesion LNM", statistic=statistic, grid=grid, thresholdText=lambda: f"|{statistic}| ≥ 0.125")
        row = {"cluster": 1, "id": 1, "xyz": (1.0, 2.0, 3.0)}
        card.sourceKind = lambda: kind
        card.selectedCluster = lambda: (row, _Table(), source)
        card.radiusSpin = types.SimpleNamespace(value=3.0)
        return card

    def test_cluster_of_each_statistic_records_its_own_map(self):
        for statistic in ("T", "Fz", "r"):
            for kind in ("cluster", "peak"):
                with self.subTest(statistic=statistic, kind=kind):
                    seed = self._card(statistic, kind).seedSource()
                    self.assertIsNone(seed.error)
                    self.assertEqual(seed.parent, self.paths[statistic])

    def test_result_map_path_follows_the_shown_statistic(self):
        for statistic in ("T", "Fz", "r"):
            card = self._card("T", "cluster", shownStatistic=statistic)
            self.assertEqual(card._resultMapPath(), self.paths[statistic])

    def test_missing_statistic_file_gives_no_parent_not_the_t_map(self):
        card = self._card("Fz", "cluster")
        card.ctx.currentRun.outputs = [self.paths["T"], self.paths["r"]]
        self.assertIsNone(card.seedSource().parent)

    def test_preview_outputs_are_recognised(self):
        card = self._card("Fz", "cluster")
        preview = os.path.join(self.folder, "Lesion_conn-Demo_desc-AvgRFzPreviewN5_funcmap.nii.gz")
        with open(preview, "wb") as handle:
            handle.write(b"x")
        card.ctx.currentRun.outputs = [self.paths["T"], preview]
        self.assertEqual(card.seedSource().parent, preview)


if __name__ == "__main__":
    unittest.main()

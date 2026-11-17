"""Test that the Circuits card's seed line follows a remade Clusters table (bug map-single:circuit-peak-seed-uses-crosshair-after-clusters-table-update).

With a sub-peak row selected in the Clusters card and "Peak sphere" chosen, remaking the Clusters table (a new threshold or a
column sort) cleared the model and dropped the selected row without ``currentRowChanged``. The Circuits line kept naming the
cluster peak while Seed this seeded a sphere at the crosshair. The card now also follows the cluster model's reset, so the line
always names the seed that Seed this uses, and a sort keeps the selected row (only the order of the same table changes).

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingCircuitsSourceFollowsTableTest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import types
import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")

import qt


class CircuitsSourceFollowsTableTest(unittest.TestCase):
    ROWS = [
        {"cluster": 1, "id": "1", "xyz": (30.0, 10.0, 4.0)},
        {"cluster": 1, "id": "1a", "xyz": (33.0, 9.0, 5.0)},
    ]

    def setUp(self):
        slicer.util.selectModule("LesionNetworkMapping")
        from LesionNetworkMappingUI import card_circuits

        crosshair = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLCrosshairNode")
        self.assertIsNotNone(crosshair)
        crosshair.SetCrosshairRAS(0.0, 0.0, 0.0)
        # The Clusters card's table: a real view and model, as ClustersCard builds them
        self.view = qt.QTableView()
        self.model = qt.QStandardItemModel(self.view)
        self.view.setModel(self.model)
        self.view.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
        self.view.setSelectionMode(qt.QAbstractItemView.SingleSelection)
        grid = types.SimpleNamespace(shape=(1, 1, 1), affine=None)
        self.clusters = types.SimpleNamespace(view=self.view, table=object(), source=types.SimpleNamespace(key="map", path=None, grid=grid), displayRows=[])
        self._fill()

        card = object.__new__(card_circuits.CircuitsCard)
        card.ctx = types.SimpleNamespace(card=lambda key: self.clusters if key == "clusters" else None, currentRun=None, parameterNode=None)
        card._clustersConnected = False
        card.sourceKind = lambda: "peak"
        card.radiusSpin = types.SimpleNamespace(value=6.0)
        card._resultMapPath = lambda statistic=None: None
        self.shown = []  # the line as refreshSource sets it
        card.refreshSource = lambda *args: self.shown.append(card.seedSource().text)
        card._connectClusters()
        self.assertTrue(card._clustersConnected)
        self.card = card

    def _fill(self):
        """Remake the table the way ClustersCard._fillView does: clear the model, then add the rows."""
        self.clusters.displayRows = list(self.ROWS)
        self.model.clear()
        self.model.setColumnCount(1)
        for index, row in enumerate(self.ROWS):
            self.model.setItem(index, 0, qt.QStandardItem(row["id"]))

    def test_line_follows_a_remade_table(self):
        self.view.setCurrentIndex(self.model.index(1, 0))
        self.assertIn("1a", self.shown[-1])
        self.assertIn("33", self.shown[-1])
        self._fill()  # a new threshold or a sort: the selection is dropped
        self.assertEqual(self.view.currentIndex().row(), -1)
        seeded = self.card.seedSource()
        self.assertEqual(seeded.peak, (0.0, 0.0, 0.0))  # Seed this uses the crosshair now...
        self.assertEqual(self.shown[-1], seeded.text)  # ...and the line says so
        self.assertIn("crosshair", self.shown[-1])

    def test_sort_keeps_the_selected_row(self):
        """A real ClustersCard (without its card frame): a header sort keeps the row, so the line and the seed stay on its peak."""
        from LesionNetworkMappingUI import card_clusters

        def row(cluster, rowId, xyz, mm3, subpeak):
            return {"cluster": cluster, "id": rowId, "xyz": xyz, "mm3": mm3, "voxels": int(mm3 / 8), "peak": 7.0, "sign": "pos", "subpeak": subpeak,
                    "hemisphere": "R", "labels": [], "network": "", "extra": {}}

        clusters = object.__new__(card_clusters.ClustersCard)
        clusters.view, clusters.itemModel = self.view, self.model
        clusters.detailLabel = qt.QLabel()
        clusters._columns = card_clusters._columnSpecs(())
        clusters.knownSpace = False
        clusters.sortColumn, clusters.sortDescending, clusters._loading = None, False, False
        clusters.table = [row(1, "1", (30.0, 10.0, 4.0), 800.0, False), row(1, "1a", (33.0, 9.0, 5.0), 800.0, True), row(2, "2", (-33.0, -57.0, 11.0), 1600.0, False)]
        clusters.source = self.clusters.source
        clusters._fillView()
        self.clusters = clusters
        self.view.setCurrentIndex(self.model.index(1, 0))
        self.assertIn("peak of cluster 1a", self.shown[-1])
        self.assertTrue(clusters.sortBy("mm3", True))  # cluster 2 first: 1a moves to the last row
        self.assertEqual([r["id"] for r in clusters.displayRows], ["2", "1", "1a"])
        self.assertEqual(self.view.currentIndex().row(), 2)
        seeded = self.card.seedSource()
        self.assertEqual(seeded.peak, (33.0, 9.0, 5.0))
        self.assertEqual(self.shown[-1], seeded.text)


if __name__ == "__main__":
    unittest.main()

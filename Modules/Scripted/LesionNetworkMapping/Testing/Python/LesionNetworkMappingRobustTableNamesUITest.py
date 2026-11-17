"""Test that the Robust card's seed-variant, split-half and replicate tables show their row names.

``widgets.tableView`` gives every column the same width (Stretch). On the narrow inspector the name column of the
seed-variant table was 98 px wide: "Extrusion 2 mm" and "Extrusion 4 mm" both read "Extrusion ...", the three sphere
variants "Sphere r ...", and the split-half metrics "r of the ha..." (bug
map-robust-card:robust-tables-first-column-elided-rows-indistinguishable). Like the sweep table, the name column now
stretches and the number columns fit their contents; a name that still does not fit keeps its end (the middle is
elided) and the split-half rows carry the full metric name as the tooltip.

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingRobustTableNamesUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import os
import shutil
import tempfile
import time
import unittest

try:
    import qt
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


def _pump(ms):
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        slicer.app.processEvents()
        time.sleep(0.005)


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class RobustTableNamesUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        slicer.util.selectModule("LesionNetworkMapping")
        _pump(50)
        cls.robust = slicer.modules.LesionNetworkMappingWidget.ctx.card("robust")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-robust-names-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.robust.refresh()
        _pump(50)

    def tables(self):
        return {"seeds": self.robust.seedsTable, "split": self.robust.splitTable, "replicate": self.robust.replicateTable, "sweep": self.robust.sweepTable}

    def test_the_name_column_stretches_and_the_numbers_fit(self):
        for name, table in self.tables().items():
            header = table.horizontalHeader()
            self.assertEqual(header.sectionResizeMode(0), qt.QHeaderView.Stretch, f"{name}: the name column does not take the free room")
            for column in range(1, table.columnCount):
                self.assertEqual(header.sectionResizeMode(column), qt.QHeaderView.ResizeToContents, f"{name}: column {column} keeps an equal share of the width")
            self.assertEqual(table.textElideMode, qt.Qt.ElideMiddle, f"{name}: a long name loses its end")

    def test_split_half_rows_have_the_full_metric_name_as_tooltip(self):
        from LesionNetworkMappingLib import robustness

        columns = ("analysis", "metric", "n_splits", "half_size", "median", "p2_5", "p97_5", "mean", "min", "max", "undefined", "seed", "label")
        rows = [("lesion_split_half", robustness.METRIC_LABELS[key], 1000, 7, 0.9, 0.8, 0.95, 0.9, 0.7, 0.97, 0, 0, "label") for key in ("r_overlap", "dice_sensitivity", "r_glm_t")]
        _write(os.path.join(self.tmp, "splithalf.tsv"), "\t".join(columns) + "\n" + "".join("\t".join(str(v) for v in row) + "\n" for row in rows))
        self.robust.showSplitHalf(self.tmp)
        table = self.robust.splitTable
        self.assertEqual(table.rowCount, 3)
        for row, key in enumerate(("r_overlap", "dice_sensitivity", "r_glm_t")):
            self.assertEqual(table.item(row, 0).toolTip(), robustness.METRIC_LABELS[key])
        names = [table.item(row, 0).text() for row in range(3)]
        self.assertEqual(len(set(names)), 3)
        metrics = table.fontMetrics()
        for name in names:
            width = metrics.horizontalAdvance(name) if hasattr(metrics, "horizontalAdvance") else metrics.width(name)
            self.assertLess(width, 110, f"'{name}' does not fit the name column of the inspector ({name})")


if __name__ == "__main__":
    unittest.main()

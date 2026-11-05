"""Test that the Robust card's seed-variant row summarises the lesions the variant changed.

Lesions a variant leaves unchanged keep their primary seed, so their r and Dice against the primary maps are 1 by
construction. The row took the median over every lesion: a sphere variant whose coordinates put three of fifteen seeds
on another network (r ~ -1 for those three) read "Median r 1.00 · Median Dice 1.00", exactly like a variant that changes
nothing (bug map-robust-card:seeds-median-hides-changed-lesions). The medians are now over the changed lesions.

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingRobustSeedsUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import os
import shutil
import tempfile
import time
import unittest

try:
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


class RobustSeedsUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        slicer.util.selectModule("LesionNetworkMapping")
        _pump(50)
        cls.robust = slicer.modules.LesionNetworkMappingWidget.ctx.card("robust")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-robust-seeds-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.robust.refresh()
        _pump(50)

    def writeSeeds(self, rows):
        columns = ("variant", "map", "kind", "changed", "r", "dice", "peaks_persist", "note")
        with open(os.path.join(self.tmp, "seeds.tsv"), "w", encoding="utf-8") as f:
            f.write("\t".join(columns) + "\n")
            f.writelines("\t".join(str(value) for value in row) + "\n" for row in rows)

    def table(self):
        table = self.robust.seedsTable
        return {table.item(row, 0).text(): [table.item(row, column).text() for column in range(1, table.columnCount)] for row in range(table.rowCount)}

    def test_medians_are_over_the_changed_lesions(self):
        moved = {"sub-01": (-0.997, 0.20), "sub-02": (-0.998, 0.30), "sub-04": (-0.996, 0.10)}
        rows = []
        for index in range(1, 16):
            sid = f"sub-{index:02d}"
            r, dice = moved.get(sid, (1.0, 1.0))
            rows.append(("sphere4", sid, "lesion", "true" if sid in moved else "false", r, dice, "3/3", "n/a"))
            # probabilistic changes one lesion slightly: its median is that lesion's, not the 1.0 of the others
            rows.append(("probabilistic", sid, "lesion", "true" if index == 6 else "false", 0.95 if index == 6 else 1.0, 0.9 if index == 6 else 1.0, "3/3", "n/a"))
        rows += [("sphere4", "group mean", "group_mean", 3, -0.89, "n/a", "3/3", "mean of the lesion maps (unthresholded)"),
                 ("binarized", "all", "variant", 0, "n/a", "n/a", "n/a", "not applicable: no lesion changed")]
        self.writeSeeds(rows)
        self.robust.showSeeds(self.tmp)
        shown = self.table()
        self.assertEqual(shown["Sphere r = 4 mm"], ["3/15", "\u22121.00", "0.20"], "the inverted maps of the changed lesions are hidden by the unchanged ones")
        self.assertEqual(shown["Probabilistic"], ["1/15", "0.95", "0.90"])
        self.assertEqual(shown["Binarized"], ["0", "—", "—"])
        self.assertIn("over the changed lesions", self.robust.seedsNote.text)
        self.assertIn("changed", self.robust.seedsTable.horizontalHeaderItem(2).toolTip())


if __name__ == "__main__":
    unittest.main()

"""Tests that the Overlap tab shows the sign and sensitivity of the group maps it lists after a project is reopened.

The two controls were saved nowhere: after quit and reopen they showed the defaults (±, 90 %) next to a listed
"Sensitivity ≥ 75 % (pos)" map, and the Robust card's primary overlap criterion, the Nulls card and the next Compute
group maps silently used 90 % / ± (bug analysis-persistence:overlap-sensitivity-and-sign-reset-on-reopen). The tab now
shows the settings of the listed group maps (their cohort.json) whenever another record appears.

They need the Slicer application (no network, no connectome; the group maps records are written by hand)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingOverlapRestoreUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import json
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


def _writeJson(path, data, bump=0):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    if bump:  # another mtime than the previous record (a rewritten cohort.json)
        stamp = time.time() + bump
        os.utime(path, (stamp, stamp))


class _DoneJob:
    state = "done"
    failureReason = ""

    @staticmethod
    def succeeded():
        return True

    @staticmethod
    def isFinished():
        return True


class LesionNetworkMappingOverlapRestoreUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lnm overlap restore ")
        slicer.util.selectModule("LesionNetworkMapping")
        _pump(50)
        cls.widget = slicer.modules.LesionNetworkMappingWidget
        cls.ctx = cls.widget.ctx
        cls.card = cls.ctx.card("cohort")
        cls.cohortMapsBefore = getattr(cls.ctx, "cohortMaps", None)
        cls.folder = os.path.join(cls.tmp, "derivatives", "lnm", "conn-Synthetic", "cohort")
        os.makedirs(cls.folder)
        cls.mapsTsv = os.path.join(cls.folder, "maps.tsv")
        with open(cls.mapsTsv, "w", encoding="utf-8") as f:
            f.write("participant_id\tT\n")
        _writeJson(os.path.join(cls.folder, "cohortmaps.json"), {"status": "done", "n": 120, "lesions": {"mapped": 3}})

    @classmethod
    def tearDownClass(cls):
        try:
            cls.ctx.cohortMaps = cls.cohortMapsBefore
            cls.card.refresh()
        except Exception:
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.assertIsNotNone(self.card, "the Map module has no Cohort card")
        self.tab = self.card.overlap
        self._record(75.0, "pos")

    def _record(self, pct, sign, bump=0):
        _writeJson(os.path.join(self.folder, "cohort.json"),
                   {"sign": sign, "sensitivity_pct": pct, "outputs": {f"sens{pct:g}": f"group_conn-Synthetic_desc-sens{pct:g}_map.nii.gz"}}, bump)

    def _reopen(self):
        """What a reopened project does: a new session starts with the defaults (±, 90 %), then finds the cohort maps."""
        self.ctx.cohortMaps = None
        self.card.refresh()
        self.tab.sensitivitySlider.value = 90
        self.tab.setSign("both")
        self.ctx.cohortMaps = self.mapsTsv
        self.card.refresh()
        _pump(50)

    def _assertSettings(self, pct, sign):
        tab = self.tab
        self.assertEqual(float(tab.sensitivitySlider.value), pct, "the Overlap tab does not show the listed group maps' sensitivity")
        self.assertEqual(tab.sign(), sign, "the Overlap tab does not show the listed group maps' sign")
        args = tab.arguments(self.mapsTsv)
        self.assertEqual(args[args.index("--sensitivity") + 1], str(round(pct)), "the next Compute group maps would use another sensitivity")
        self.assertEqual(args[args.index("--sign") + 1], sign, "the next Compute group maps would use another sign")
        robust = self.ctx.card("robust")
        if robust is not None:
            self.assertEqual(robust.overlapPct(), pct, "the Robust card's primary overlap criterion is not the listed one")
        nulls = self.ctx.card("nulls")
        if nulls is not None and hasattr(nulls, "overlapSettings"):
            self.assertEqual(nulls.overlapSettings(), (pct, sign))

    def test_1_reopen_shows_the_listed_settings(self):
        self._reopen()
        self.assertEqual(self.card.currentMapsTsv(), self.mapsTsv)
        self._assertSettings(75.0, "pos")

    def test_2_user_change_stays_until_another_record(self):
        self._reopen()
        self.tab.sensitivitySlider.value = 80
        self.tab.setSign("neg")
        self.card.refresh()
        self.tab.refresh()
        _pump(50)
        self._assertSettings(80.0, "neg")
        # a rewritten cohort.json (for example by "Run everything") is shown
        self._record(60.0, "both", bump=5)
        self.card.refresh()
        self._assertSettings(60.0, "both")

    def test_3_no_record_keeps_the_controls(self):
        self._reopen()
        os.remove(os.path.join(self.folder, "cohort.json"))
        self.tab.sensitivitySlider.value = 85
        self.card.refresh()
        self._assertSettings(85.0, "pos")

    def test_4_change_during_the_job_is_kept(self):
        """A sign or sensitivity changed while Compute group maps runs stays when the job ends (the result is listed)."""
        self._reopen()
        self.tab._submittedSettings = ("pos", 75)
        self.tab.sensitivitySlider.value = 95
        self._record(75.0, "pos", bump=10)  # the job's cohort.json
        self.tab._onDone(_DoneJob())
        self._assertSettings(95.0, "pos")

    def test_4_spare_height_goes_below_the_rows(self):
        """The tab pages share the tallest tab's height; the Overlap layout must end in a stretch (bug cohort-overlap-tab-blank-gaps)."""
        layout = self.tab.widget.layout()
        last = layout.itemAt(layout.count() - 1)
        self.assertIsNotNone(last.spacerItem(), "the Overlap tab layout has no closing stretch: spare height is spread between its rows")


if __name__ == "__main__":
    unittest.main()

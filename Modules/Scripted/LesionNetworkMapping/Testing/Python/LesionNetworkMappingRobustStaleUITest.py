"""Test that the Robust card marks the results of earlier cohort maps or lesions as stale.

After the lesions were mapped again with other lesions (a lesion set to QC fail, a new lesion), the card still showed the
split-half, influence and replicate results of the old maps.tsv as the current result, with no mark; the same after a
lesion file was edited (bug map-robust-card:robust-results-not-stale-after-cohort-remap). A result now records the
identity of the maps it used (``cohort_maps.maps_sha256``); one of other maps, an older result written before the maps,
or a result whose lesion file changed after it was mapped is shown with a "run again" line.

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingRobustStaleUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import hashlib
import json
import os
import shutil
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")

IDS = ("sub-01", "sub-02", "sub-03", "sub-04")


def _pump(ms):
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        slicer.app.processEvents()
        time.sleep(0.005)


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _touch(path, seconds):
    """Set the modification time of ``path`` to ``seconds`` from now (a clear order, whatever the file system's resolution)."""
    stamp = time.time() + seconds
    os.utime(path, (stamp, stamp))


class RobustStaleUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        slicer.util.selectModule("LesionNetworkMapping")
        _pump(50)
        cls.ctx = slicer.modules.LesionNetworkMappingWidget.ctx
        cls.robust = cls.ctx.card("robust")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-robust-stale-")
        self.conn = os.path.join(self.tmp, "derivatives", "lnm", "conn-Synthetic")
        self.maps = os.path.join(self.conn, "cohort", "maps.tsv")
        self.lesions = {}
        for sid in IDS:
            path = os.path.join(self.conn, "cohort", "lesions", f"{sid}_desc-lesion_mask.nii.gz")
            _write(path, f"lesion of {sid}")
            self.lesions[sid] = path
        self.writeMaps(IDS, -100)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.robust.refresh()  # back to the module's own study and connectome
        _pump(50)

    def sha(self, sid):
        with open(self.lesions[sid], "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def writeMaps(self, ids, seconds):
        """A maps.tsv of ``ids`` (cache key = lesion hash + "k"), modified ``seconds`` from now."""
        lines = ["participant_id\tlesion\tlesion_sha256\tkey"]
        lines += [f"{sid}\tlesions/{os.path.basename(self.lesions[sid])}\t{self.sha(sid)}\t{self.sha(sid)}k" for sid in ids]
        _write(self.maps, "\n".join(lines) + "\n")
        _touch(self.maps, seconds)

    def writeSplitHalf(self, identity, seconds):
        folder = os.path.join(self.conn, "robustness", "splithalf")
        _write(os.path.join(folder, "splithalf.tsv"), "analysis\tmetric\tmedian\tp2_5\tp97_5\tmin\tmax\tn_splits\thalf_size\tseed\tlabel\n"
               "lesion_split_half\tr of the halves' overlap maps\t0.9\t0.8\t0.95\t0.7\t0.99\t1000\t2\t0\tlesion split-half\n")
        _write(os.path.join(folder, "splithalf.json"), json.dumps({"analysis": "splithalf", "n_lesions": 4, "cohort_maps": {"run_id": "cohortmaps-1", "maps_sha256": identity}}))
        _touch(os.path.join(folder, "splithalf.json"), seconds)

    def writeInfluence(self, seconds):
        """An influence result written before the records named their maps (no cohort_maps)."""
        folder = os.path.join(self.conn, "robustness", "influence")
        _write(os.path.join(folder, "influence.tsv"), "rank\tparticipant_id\tinfluence\tdice_vs_full\n" + "".join(f"{rank}\t{sid}\t0.{rank}\t0.9\n" for rank, sid in enumerate(IDS, 1)))
        _write(os.path.join(folder, "influence.json"), json.dumps({"analysis": "influence", "influence": "1 - Dice"}))
        _touch(os.path.join(folder, "influence.json"), seconds)

    def card(self):
        """Refresh the card on the files of this test; return {kind: shown stale text or None when hidden}, the status and the methods header."""
        with mock.patch.object(self.ctx, "study", SimpleNamespace(path=self.tmp)), mock.patch.object(self.robust, "currentMapsTsv", return_value=self.maps):
            self.robust.refresh()
            stale = {kind: (None if self.robust.sections[kind]["stale"].isHidden() else self.robust.sections[kind]["stale"].text) for kind in ("splithalf", "influence")}
            return stale, self.robust.statusLabel.text, self.robust.methodsHeader.text

    def test_results_of_earlier_maps_or_lesions_are_marked_stale(self):
        from LesionNetworkMappingLib import robustness

        first = robustness.maps_tsv_sha256(self.maps)
        self.writeSplitHalf(first, -50)
        self.writeInfluence(-50)
        stale, status, header = self.card()
        self.assertEqual(set(self.robust.results), {"splithalf", "influence"}, "the results of this connectome are shown")
        self.assertEqual(stale, {"splithalf": None, "influence": None}, "results of the current maps are not marked")
        self.assertNotIn("run them again", status)
        self.assertEqual(header, "Methods sentences")

        # the lesions mapped again without sub-04 (QC fail): both results belong to the previous maps
        self.writeMaps(IDS[:3], -10)
        self.assertNotEqual(robustness.maps_tsv_sha256(self.maps), first)
        stale, status, header = self.card()
        self.assertIn("previous cohort maps", stale["splithalf"] or "", "a result of other maps is shown as current")
        self.assertIn("3 lesion maps now", stale["splithalf"])
        self.assertIn("previous cohort maps", stale["influence"] or "", "an older result (no recorded maps) of earlier maps is shown as current")
        self.assertIn("2 of them belong to earlier cohort maps or lesions: run them again.", status)
        self.assertNotEqual(header, "Methods sentences")

        # split-half run again on the current maps: current again; the old influence stays marked
        self.writeSplitHalf(robustness.maps_tsv_sha256(self.maps), 0)
        stale, status, _header = self.card()
        self.assertEqual(stale["splithalf"], None)
        self.assertIn("previous cohort maps", stale["influence"] or "")
        self.assertIn("1 of them belong", status)

        # the same lesions mapped again (same identity, newer maps.tsv): the recorded identity decides, not the time
        self.writeMaps(IDS[:3], 20)
        self.assertEqual(self.card()[0]["splithalf"], None)

        # sub-02's lesion file edited after the mapping: the maps (and the result) are out of date
        with open(self.lesions["sub-02"], "a", encoding="utf-8") as f:
            f.write(" edited")
        _touch(self.lesions["sub-02"], 40)
        stale, status, _header = self.card()
        self.assertIn("A lesion changed after the lesions were mapped (sub-02)", stale["splithalf"] or "", "a result of a changed lesion is shown as current")
        self.assertIn("run them again", status)

        # the lesion saved again with the bytes it was mapped with: current again
        _write(self.lesions["sub-02"], "lesion of sub-02")
        _touch(self.lesions["sub-02"], 60)
        self.assertEqual(self.card()[0]["splithalf"], None)

    def test_seed_variants_and_consolidation_are_marked_stale_after_a_lesion_edit(self):
        """Bug map-robust-card:robust-seeds-consolidation-not-stale-after-lesion-edit: their manifests record each source_sha256."""
        folders = {}
        for kind, name in (("seeds", "variants.json"), ("consolidate", "consolidation.json")):
            folders[kind] = os.path.join(self.conn, "robustness", kind)
            entries = [{"participant_id": sid, "source": os.path.relpath(self.lesions[sid], folders[kind]), "source_sha256": self.sha(sid)} for sid in IDS]
            record = {"variants": {"flipped": {"lesions": entries}}} if kind == "seeds" else {"lesions": entries}
            _write(os.path.join(folders[kind], name), json.dumps(record))
        with mock.patch.object(self.robust, "currentMapsTsv", return_value=self.maps):
            for kind, folder in folders.items():
                self.assertEqual(self.robust.staleReason(kind, folder), "", kind)
            with open(self.lesions["sub-02"], "a", encoding="utf-8") as f:
                f.write(" edited")
            _touch(self.lesions["sub-02"], 40)
            for kind, folder in folders.items():
                self.assertIn("A lesion changed after this was made (sub-02)", self.robust.staleReason(kind, folder), kind)
            _write(self.lesions["sub-02"], "lesion of sub-02")
            _touch(self.lesions["sub-02"], 60)
            for kind, folder in folders.items():
                self.assertEqual(self.robust.staleReason(kind, folder), "", kind)


if __name__ == "__main__":
    unittest.main()

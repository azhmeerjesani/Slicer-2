"""Test that each connectome of the Robust card's "Replicate on…" list has its own result folder.

The run folder was ``robustness/replicate_<name>``, built from the connectome's name with every character other than an
ASCII letter or digit dropped. Two registered connectomes with the same name (a full connectome and a subset of it), or
with names that differ only in punctuation ("GSP-1000" and "GSP 1000"), shared one folder: the card showed one's result
for the other, which had never been replicated on, and replicating on one overwrote the other's result (bug
map-robust-card:replicate-same-name-connectomes-share-result). The folder is now ``replicate_<name>_<tag>``, the tag
coming from the connectome's identifier (name and data fingerprint); a folder of an earlier version is shown only for
the connectome its result names.

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingRobustReplicateUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

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


def _pump(ms):
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        slicer.app.processEvents()
        time.sleep(0.005)


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class RobustReplicateUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        slicer.util.selectModule("LesionNetworkMapping")
        _pump(50)
        cls.ctx = slicer.modules.LesionNetworkMappingWidget.ctx
        cls.robust = cls.ctx.card("robust")

    def setUp(self):
        from LesionNetworkMappingLib import connectome

        self.tmp = tempfile.mkdtemp(prefix="lnm-robust-replicate-")
        self.conn = os.path.join(self.tmp, "study", "derivatives", "lnm", "conn-Synthetic")
        self.maps = os.path.join(self.conn, "cohort", "maps.tsv")
        _write(self.maps, "participant_id\tlesion\tlesion_sha256\tkey\n")
        stamp = time.time() - 1000
        os.utime(self.maps, (stamp, stamp))
        self.bundles = {}
        for key, name, n, seed in (("full", "Second", 4, 1), ("subset", "Second", 3, 2), ("dash", "GSP-1000", 3, 3), ("space", "GSP 1000", 3, 4)):
            path = os.path.join(self.tmp, "connectomes", f"{key}.lnmc")
            connectome.make_synthetic(path, shape=(8, 10, 8), n_subjects=n, runs=(6, 6), seed=seed, name=name)
            self.bundles[key] = (path, f"{name} · N={n}")

    def tearDown(self):
        for path, _text in self.bundles.values():
            self.robust._bundleInfo.pop(path, None)
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.robust.refresh()  # back to the module's own study and connectome
        _pump(50)

    def patches(self):
        items = [(text, path) for path, text in self.bundles.values()]
        return (mock.patch.object(self.ctx, "study", SimpleNamespace(path=os.path.join(self.tmp, "study"))),
                mock.patch.object(self.robust, "currentMapsTsv", return_value=self.maps),
                mock.patch.object(self.robust, "otherConnectomes", return_value=items))

    def select(self, key):
        """Select a connectome in "Replicate on…"; return (its run folder, the shown summary or None when no result is shown)."""
        study, maps, others = self.patches()
        with study, maps, others:
            self.robust.refresh()
            combo = self.robust.replicateCombo
            index = [combo.itemData(i) for i in range(combo.count)].index(self.bundles[key][0])
            combo.setCurrentIndex(index)
            self.robust.refresh()
            folder = self.robust.folderFor("replicate")
            shown = None if self.robust.sections["replicate"]["holder"].isHidden() else self.robust.replicateSummary.text
            return folder, shown

    def writeResult(self, folder, name, n, manifest=None):
        record = {"analysis": "replicate", "connectome_a": {"label": "Synthetic", "n": 20}, "connectome_b": {"label": name, "n": n, "manifest_sha256": manifest},
                  "summary": {"cross_connectome_r": 0.9, "flagged": 0, "thresholds": {}}}
        _write(os.path.join(folder, "connectomes.tsv"), "map\tkind\tparticipant_id\tr\tdice\tpeaks_persist\tflag\tnote\noverlap\tgroup\tn/a\t0.9\t0.8\t3\tn/a\tn/a\n")
        _write(os.path.join(folder, "replicate.json"), json.dumps(record))

    def test_connectomes_of_the_same_name_have_their_own_results(self):
        full, shown = self.select("full")
        self.assertIsNone(shown, "no result before any replicate run")
        self.writeResult(full, "Second", 4)
        self.assertIn("against Second (N=4)", self.select("full")[1] or "")

        subset, shown = self.select("subset")
        self.assertNotEqual(subset, full, "two connectomes named Second share one replicate folder")
        self.assertIsNone(shown, "the never-replicated Second (N=3) shows the result of Second (N=4)")

        self.writeResult(subset, "Second", 3)
        self.assertIn("against Second (N=3)", self.select("subset")[1] or "")
        with open(os.path.join(full, "replicate.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["connectome_b"]["n"], 4, "replicating on N=3 overwrote the N=4 result")
        self.assertIn("against Second (N=4)", self.select("full")[1] or "", "Second (N=4) shows another connectome's result")

        dash, space = self.select("dash")[0], self.select("space")[0]
        self.assertNotEqual(dash, space, "names that differ only in punctuation share one replicate folder")

        # the same connectome in another folder (or on another computer) keeps its folder
        copy = os.path.join(self.tmp, "connectomes", "copy.lnmc")
        shutil.copytree(self.bundles["full"][0], copy)
        try:
            self.assertEqual(self.robust.replicateFolderName(copy), os.path.basename(full))
        finally:
            self.robust._bundleInfo.pop(copy, None)

    def test_same_subjects_different_data_have_their_own_results(self):
        """Two connectomes of one name, grid and subjects but different time series (with and without GSR) do not share."""
        from LesionNetworkMappingLib import connectome
        from LesionNetworkMappingLib.export import data_sha256, file_sha256

        path = os.path.join(self.tmp, "connectomes", "gsr.lnmc")
        connectome.make_synthetic(path, shape=(8, 10, 8), n_subjects=4, runs=(6, 6), seed=9, name="Second")
        self.bundles["gsr"] = (path, "Second · N=4 (GSR)")
        full, _shown = self.select("full")
        self.writeResult(full, "Second", 4)
        gsr, shown = self.select("gsr")
        self.assertNotEqual(gsr, full, "two connectomes with different data share one replicate folder")
        self.assertIsNone(shown, "the never-replicated GSR connectome shows the result of the other one")

        # A folder under the tag of an earlier version (no time series fingerprint) is still shown for its connectome
        shutil.rmtree(full)
        earlier = os.path.join(os.path.dirname(full), self.robust._replicateFolderName(self.bundles["full"][0], data_sha256))
        self.assertNotEqual(earlier, full)
        self.writeResult(earlier, "Second", 4, manifest=file_sha256(os.path.join(self.bundles["full"][0], "connectome.json")))
        folder, shown = self.select("full")
        self.assertEqual(folder, earlier, "the earlier result of this connectome is no longer shown")
        self.assertIn("against Second (N=4)", shown or "")
        folder, shown = self.select("gsr")
        self.assertNotEqual(folder, earlier, "the GSR connectome uses the earlier result of the other one")
        self.assertIsNone(shown)

    def test_folder_of_an_earlier_version_is_shown_only_for_its_connectome(self):
        from LesionNetworkMappingLib.export import file_sha256

        legacy = os.path.join(self.conn, "robustness", "replicate_GSP1000")
        self.writeResult(legacy, "GSP1000", 3, manifest=file_sha256(os.path.join(self.bundles["dash"][0], "connectome.json")))
        folder, shown = self.select("dash")
        self.assertEqual(folder, legacy, "the earlier result of GSP-1000 is no longer shown")
        self.assertIn("against GSP1000 (N=3)", shown or "")
        folder, shown = self.select("space")
        self.assertNotEqual(folder, legacy, "GSP 1000 uses the earlier result of GSP-1000")
        self.assertIsNone(shown)


if __name__ == "__main__":
    unittest.main()

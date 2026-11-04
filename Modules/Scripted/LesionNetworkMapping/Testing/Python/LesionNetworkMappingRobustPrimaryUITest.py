"""Test that the Robust card's "Primary (fixed)" line follows the Overlap tab's sensitivity.

The line is built from the sensitivity that the analyses use (``primaryKey()``, read live from the Overlap tab's slider), but
the card refreshed only on a change of the study, bundle, rule, run or compute state: after the sensitivity was changed in
Map > Cohort > Overlap the line kept the old percent while the analyses used the new one (bug
analysis-persistence:robust-primary-text-stale-after-overlap-change).

The primary's sign is the Overlap tab's too: the card ran every analysis over |T| ("both") and over the cases and
controls together whatever the Overlap tab showed (bug map-robust-card:robust-card-ignores-overlap-sign).

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingRobustPrimaryUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

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


class RobustPrimaryUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        slicer.util.selectModule("LesionNetworkMapping")
        _pump(50)
        cls.widget = slicer.modules.LesionNetworkMappingWidget
        cls.ctx = cls.widget.ctx
        cls.robust = cls.ctx.card("robust")
        cls.slider = cls.ctx.card("cohort").tab("overlap").sensitivitySlider
        cls.before = cls.slider.value

    @classmethod
    def tearDownClass(cls):
        cls.slider.value = cls.before
        _pump(50)

    def _percent(self):
        return f"sensitivity ≥ {self.robust.overlapPct():g}%"

    def test_1_label_follows_the_overlap_slider(self):
        self.assertIsNotNone(self.robust)
        for value in (75, 100, 60, 90):
            self.slider.value = value
            _pump(400)  # longer than the card's refresh debounce
            self.assertEqual(self.robust.primaryKey()[1], float(value))
            self.assertIn(self._percent(), self.robust.primaryLabel.text, f"the Primary line is stale after the slider moved to {value}")

    def test_2_the_context_notifies_an_overlap_change(self):
        calls = []
        self.ctx.overlapChanged.append(lambda: calls.append(1))
        try:
            self.slider.value = 55 if self.slider.value != 55 else 65
            self.assertTrue(calls, "ctx.overlapChanged was not notified")
        finally:
            self.ctx.overlapChanged.pop()
            self.slider.value = self.before
            _pump(50)

    def test_3_sign_follows_the_overlap_tab(self):
        overlap = self.ctx.card("cohort").tab("overlap")
        before = overlap.sign()
        folder = tempfile.mkdtemp(prefix="lnm-robust-sign-")
        participants = os.path.join(folder, "participants.tsv")
        with open(participants, "w", encoding="utf-8") as f:
            f.write("participant_id\tgroup\nsub-01\tcase\n")
        try:
            for sign, text in (("pos", "positive sign"), ("neg", "negative sign"), ("both", "both signs")):
                overlap.setSign(sign)
                _pump(400)  # longer than the card's refresh debounce
                self.assertEqual(self.robust.sign(), sign)
                self.assertEqual(self.robust.primaryKey()[2], sign)
                self.assertIn(text, self.robust.primaryLabel.text, f"the Primary line does not state the sign {sign}")
                with mock.patch.object(self.ctx, "study", SimpleNamespace(path=folder)), mock.patch.object(self.robust, "currentMapsTsv", return_value=os.path.join(folder, "maps.tsv")):
                    for kind in ("sweep", "splithalf", "influence"):
                        args = self.robust.arguments(kind, os.path.join(folder, kind))
                        self.assertIn("--sign", args, f"{kind} does not get the Overlap tab's sign")
                        self.assertEqual(args[args.index("--sign") + 1], sign, kind)
                        self.assertEqual(args[args.index("--participants") + 1], participants, f"{kind}: the primary overlap is over the cases")
        finally:
            overlap.setSign(before)
            shutil.rmtree(folder, ignore_errors=True)
            _pump(400)


if __name__ == "__main__":
    unittest.main()

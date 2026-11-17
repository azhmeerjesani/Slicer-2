"""Test that the Validate card says before the run that the connectome has no reference maps (BB-1).

The μC (degree) and PC1 baselines need the connectome's reference maps. The card said "No reference maps (volume, μC,
PC1 and lesion location are always included)" with Run enabled, and the rows came out "not computed" with a reason that
named a terminal command (bug lnm-reference-fastindex-paths:validate-no-reference-maps-reason-names-python-command).
The card now says it before the run and points to Manage… > Build reference maps, and looks again after a build.

It needs the Slicer application and builds a tiny synthetic connectome (no network)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingValidateRefNoteUITest

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


def _quiet(*args, **kwargs):
    pass


class LesionNetworkMappingValidateRefNoteUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from LesionNetworkMappingLib import connectome, reference

        cls.reference = reference
        cls.tmp = tempfile.mkdtemp(prefix="lnm validate refnote ")
        info = connectome.make_synthetic(os.path.join(cls.tmp, "Synthetic.lnmc"), n_subjects=5, seed=3, name="Synthetic")
        cls.bundle = connectome.ConnectomeBundle.open(info["path"])
        slicer.util.selectModule("LesionNetworkMapping")
        _pump(50)
        cls.widget = slicer.modules.LesionNetworkMappingWidget
        cls.ctx = cls.widget.ctx
        cls.bundleBefore = cls.ctx.bundle
        cls.card = cls.ctx.card("validate")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.ctx.setBundle(cls.bundleBefore)
        except Exception:
            pass
        cls.bundle.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_note_before_the_run_and_after_a_build(self):
        card = self.card
        self.assertIsNotNone(card, "the Map module has no Validate card")
        self.ctx.setBundle(self.bundle)
        _pump(50)
        card.showRefmapState()
        self.assertFalse(card.connectomeHasReference())
        note = card.connectomeRefNote
        self.assertFalse(note.isHidden(), "no note about the missing reference maps")
        for text in ("μC", "PC1", "Manage… > Build reference maps"):
            self.assertIn(text, note.text)
        self.assertNotIn("python -m", note.text)
        self.assertNotIn("μC", card.refmapLabel.text)  # the label is about extra maps; μC and PC1 are not "always included"
        self.reference.build(self.bundle, n_seeds=40, k=3, power_iters=2, register=False, emit=_quiet)
        card.showRefmapState()
        self.assertTrue(card.connectomeHasReference())
        self.assertTrue(note.isHidden(), "the note stays after the reference maps were built")
        self.assertEqual(note.text, "")


if __name__ == "__main__":
    unittest.main()

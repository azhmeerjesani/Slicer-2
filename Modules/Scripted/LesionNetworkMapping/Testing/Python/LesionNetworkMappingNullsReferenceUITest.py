"""Tests that the Nulls card notices reference maps built in the same session (BB-1).

The card keeps whether the connectome has reference maps (rl_strength needs them) per bundle. That answer was forgotten
only when the cohort maps changed, so after Manage… → Build reference maps (or the QA card's button) the card kept
refusing rl_strength with "build the reference maps" until the app restarted (bug
lnm-reference-fastindex-paths:nulls-rl-strength-stays-disabled-after-reference-build). A finished reference job and a
connectome change now make the card look again.

They need the Slicer application and build a tiny synthetic connectome (no network)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingNullsReferenceUITest

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


class LesionNetworkMappingNullsReferenceUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from LesionNetworkMappingLib import connectome, reference

        cls.reference = reference
        cls.tmp = tempfile.mkdtemp(prefix="lnm nulls reference ")
        cls.bundles = []
        for name in ("A", "B"):
            info = connectome.make_synthetic(os.path.join(cls.tmp, f"Synthetic {name}.lnmc"), n_subjects=5, seed=3, name=f"Synthetic {name}")
            cls.bundles.append(connectome.ConnectomeBundle.open(info["path"]))
        slicer.util.selectModule("LesionNetworkMapping")
        _pump(50)
        cls.widget = slicer.modules.LesionNetworkMappingWidget
        cls.ctx = cls.widget.ctx
        cls.bundleBefore = cls.ctx.bundle
        cls.card = cls.ctx.card("nulls")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.ctx.setBundle(cls.bundleBefore)
        except Exception:
            pass
        for bundle in cls.bundles:
            bundle.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.assertIsNotNone(self.card, "the Map module has no Nulls card")

    def test_1_reference_job_finishing_enables_rl_strength(self):
        """The reference build of the manager and the QA card is a runner job of kind "reference"."""
        from LesionNetworkMappingUI import jobrunner

        bundle = self.bundles[0]
        self.ctx.setBundle(bundle)
        _pump(50)
        self.assertIsNone(self.reference.load_reference(bundle))
        self.assertFalse(self.card.hasReference(), "the card sees reference maps that do not exist")
        spec = jobrunner.JobSpec("LesionNetworkMappingLib.reference", ["build", "--bundle", bundle.path, "--seeds", "40", "--pcs", "3", "--power-iters", "2"],
                                 title="Reference maps", heavy=True, kind="reference", name="reference", runRoot=bundle.path)
        job = self.ctx.submit(spec)
        deadline = time.monotonic() + 240
        while not job.isFinished() and time.monotonic() < deadline:
            _pump(50)
        self.assertTrue(job.isFinished(), "the reference build did not finish")
        self.assertTrue(job.succeeded(), f"the reference build failed: {job.failureReason}")
        _pump(50)
        self.assertIsNotNone(self.reference.load_reference(bundle), "the reference maps are not on disk")
        self.assertTrue(self.card.hasReference(), "the Nulls card still refuses rl_strength after the reference maps were built")

    def test_2_connectome_change_looks_again(self):
        """Reference maps built outside the app (the command line) count once the connectome is chosen again."""
        bundle = self.bundles[1]
        self.ctx.setBundle(bundle)
        _pump(50)
        self.assertFalse(self.card.hasReference())
        self.reference.build(bundle, n_seeds=40, k=3, power_iters=2, register=False, emit=_quiet)
        self.assertIsNotNone(self.reference.load_reference(bundle))
        self.ctx.setBundle(None)
        self.ctx.setBundle(bundle)
        _pump(50)
        self.assertTrue(self.card.hasReference(), "choosing the connectome again keeps the stale answer")


if __name__ == "__main__":
    unittest.main()

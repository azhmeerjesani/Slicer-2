"""Tests that the Compare and QA cards use the current reference maps after a build or rebuild in the same session (BB-1).

The Compare card kept the reference maps it loaded (or the None of "no reference maps yet") for as long as the same
connectome was in use, and cached the μC_r / PC1 vectors under a key without the reference build. After "Build reference
maps" the card offered no μC_r / PC1 / Hubness rows and no partial r, and after a rebuild it compared with the old arrays
(bug refmaps-compare-qa-v2:compare-card-keeps-session-cached-reference-maps). The cards now reload when the reference
files on disk change.

They need the Slicer application and build a tiny synthetic connectome (no network)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingCompareReferenceUITest

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


class LesionNetworkMappingCompareReferenceUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from LesionNetworkMappingLib import connectome, reference

        cls.referenceLib = reference
        cls.tmp = tempfile.mkdtemp(prefix="lnm compare reference ")
        info = connectome.make_synthetic(os.path.join(cls.tmp, "Synthetic C.lnmc"), n_subjects=5, seed=3, name="Synthetic C")
        cls.bundle = connectome.ConnectomeBundle.open(info["path"])
        slicer.util.selectModule("LesionNetworkMapping")
        _pump(50)
        cls.widget = slicer.modules.LesionNetworkMappingWidget
        cls.ctx = cls.widget.ctx
        cls.bundleBefore = cls.ctx.bundle
        cls.compare = cls.ctx.card("compare")
        cls.qa = cls.ctx.card("qa")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.ctx.setBundle(cls.bundleBefore)
        except Exception:
            pass
        cls.bundle.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _connectomeSources(self):
        return {source.key: source for source in self.compare.discoverSources() if source.group == "connectome"}

    def test_1_build_and_rebuild_are_picked_up(self):
        import numpy as np

        self.assertIsNotNone(self.compare, "the Map module has no Compare card")
        self.assertIsNotNone(self.qa, "the Map module has no QA card")
        bundle = self.bundle
        self.ctx.setBundle(bundle)
        _pump(50)

        # No reference maps yet: both cards see none (and remember that answer).
        self.assertIsNone(self.compare.reference())
        self.assertIsNone(self.qa._loadedReference())
        self.assertEqual(self._connectomeSources(), {})

        # Build: the cards see the new reference without a restart or a connectome change.
        self.referenceLib.build(bundle, n_seeds=40, k=3, power_iters=2, seed=0, register=False, emit=_quiet)
        first = self.referenceLib.load_reference(bundle, verify=False)
        self.assertIsNotNone(first)
        self.assertIsNotNone(self.compare.reference(), "the Compare card keeps 'no reference maps' after the build")
        self.assertEqual(self.compare.reference().sha256, first.sha256)
        self.assertIsNotNone(self.qa._loadedReference(), "the QA card keeps 'no reference maps' after the build")
        sources = self._connectomeSources()
        self.assertEqual(set(sources), {"connectome:mu_r", "connectome:pc1", "connectome:hub"})
        np.testing.assert_array_equal(self.compare.vector(sources["connectome:hub"]), np.asarray(first.maps["hub"], dtype=np.float64))

        # Rebuild with other seeds (μC_r is exact, Hubness depends on the seeds): the cards and the cached vectors follow it.
        time.sleep(0.01)
        self.referenceLib.build(bundle, n_seeds=25, k=3, power_iters=2, seed=7, register=False, emit=_quiet)
        second = self.referenceLib.load_reference(bundle, verify=False)
        self.assertIsNotNone(second)
        self.assertNotEqual(second.sha256, first.sha256)
        self.assertFalse(np.array_equal(first.maps["hub"], second.maps["hub"]), "the rebuild did not change Hubness (test setup)")
        self.assertEqual(self.compare.reference().sha256, second.sha256, "the Compare card compares with the old reference maps")
        self.assertEqual(self.qa._loadedReference().sha256, second.sha256, "the QA card keeps the old reference maps")
        sources = self._connectomeSources()
        self.assertEqual(sources["connectome:hub"].info["reference_sha256"], second.sha256)
        np.testing.assert_array_equal(self.compare.vector(sources["connectome:hub"]), np.asarray(second.maps["hub"], dtype=np.float64),
                                      err_msg="the Compare card's cached Hubness vector is the one of the old build")


if __name__ == "__main__":
    unittest.main()

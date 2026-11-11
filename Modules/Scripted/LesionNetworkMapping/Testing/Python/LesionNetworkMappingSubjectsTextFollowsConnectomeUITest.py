"""Test that the Map module's "All N subjects" text follows the selected connectome (BB-1).

Bug map-compute-subjects-count-stale-after-connectome-switch: choosing another connectome left the text next to
All / Preview at the old connectome's count until the module was left and entered again.

Needs the Slicer application and builds two tiny synthetic connectomes (no network)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingSubjectsTextFollowsConnectomeUITest
"""

import os
import shutil
import tempfile
import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class SubjectsTextFollowsConnectomeUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from LesionNetworkMappingLib import connectome

        cls.tmp = tempfile.mkdtemp(prefix="lnm-subjects-text ")
        cls.paths = {}
        for name, n in (("Five", 5), ("Seven", 7)):
            cls.paths[n] = connectome.make_synthetic(os.path.join(cls.tmp, f"{name}.lnmc"), shape=(12, 14, 12), n_subjects=n, runs=(12,), seed=4, name=name)["path"]
        slicer.util.selectModule("LesionNetworkMapping")
        slicer.app.processEvents()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_text_follows_the_selected_connectome(self):
        w = slicer.modules.LesionNetworkMappingWidget
        node = w._parameterNode
        old = node.subjectsMode if node is not None else None
        try:
            if node is not None:
                node.subjectsMode = "all"
            w._selectConnectome(self.paths[7])
            self.assertEqual(w.subjectsLabel.text, "All 7 subjects")
            w._selectConnectome(self.paths[5])
            self.assertEqual(w.subjectsLabel.text, "All 5 subjects")
        finally:
            if node is not None and old is not None:
                node.subjectsMode = old


if __name__ == "__main__":
    unittest.main()

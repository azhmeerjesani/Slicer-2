"""Tests that a scene opened while Map is shown makes Map use the scene's connectome (BB-1).

The scene's parameter node names its connectome by identifier (SPEC 9.7 rule 10). Opening a scene while Map was shown only
read the parameter node again: the Connectome card, the status and Compute kept the connectome chosen before, so the next
map was computed with that one while the scene recorded the other (bug
lnm-reference-fastindex-paths-r12-rerun:scene-load-while-map-shown-keeps-other-connectome). Entering Map after the load
re-resolved it; now opening (or closing) a scene while Map is shown keeps the parameter node and the connectome in use in step.

It needs the Slicer application and makes two small synthetic connectomes (no network, no engine run)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingSceneLoadConnectomeUITest

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


def _pump(seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        slicer.app.processEvents()
        time.sleep(0.005)


class LesionNetworkMappingSceneLoadConnectomeUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import qt
        from LesionNetworkMapping import CONNECTOMES_SETTING
        from LesionNetworkMappingLib import connectome

        cls.tmp = tempfile.mkdtemp(prefix="lnm scene load connectome ")
        cls.savedSetting = qt.QSettings().value(CONNECTOMES_SETTING)
        cls.syn = connectome.make_synthetic(os.path.join(cls.tmp, "syn.lnmc"), shape=(12, 14, 12), n_subjects=12, runs=(10,), seed=0, name="SceneSyn")
        cls.other = connectome.make_synthetic(os.path.join(cls.tmp, "other.lnmc"), shape=(12, 14, 12), n_subjects=5, runs=(10,), seed=1, name="SceneOther")
        slicer.mrmlScene.Clear(0)
        slicer.util.selectModule("LesionNetworkMapping")
        _pump(0.05)
        cls.widget = slicer.modules.LesionNetworkMappingWidget
        cls.widget.logic.registerConnectome(cls.syn["path"])
        cls.widget.logic.registerConnectome(cls.other["path"])

    @classmethod
    def tearDownClass(cls):
        import qt
        from LesionNetworkMapping import CONNECTOMES_SETTING

        slicer.mrmlScene.Clear(0)
        settings = qt.QSettings()
        if cls.savedSetting is None:
            settings.remove(CONNECTOMES_SETTING)
        else:
            settings.setValue(CONNECTOMES_SETTING, cls.savedSetting)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _select(self, meta, name):
        widget = self.widget
        index = widget.connectomeCombo.findData(os.path.abspath(meta["path"]))
        self.assertGreaterEqual(index, 0)
        widget.connectomeCombo.setCurrentIndex(index)
        widget.onConnectomeActivated(index)
        self.assertEqual(widget.bundle.name, name)

    def _assertInStep(self, name):
        widget = self.widget
        node = widget._parameterNode
        self.assertIsNotNone(widget.bundle)
        self.assertEqual(widget.bundle.name, name, "Map computes with another connectome than the scene's")
        self.assertTrue(node.connectome.startswith(name + "#"), node.connectome)
        self.assertEqual(os.path.normpath(widget.connectomePath()), os.path.normpath(widget.logic.resolveConnectome(node.connectome)))
        self.assertIn(name, str(widget.connectomeCombo.currentText))

    def _saveSceneWithSyn(self, name):
        widget = self.widget
        self.assertTrue(widget.parent.isEntered)
        widget.initializeParameterNode()
        widget.refreshConnectomes()
        self._select(self.syn, "SceneSyn")
        path = os.path.join(self.tmp, name)
        self.assertTrue(slicer.util.saveScene(path))
        self._select(self.other, "SceneOther")
        self.assertTrue(widget._parameterNode.connectome.startswith("SceneOther#"))
        return path

    def test_scene_opened_while_map_is_shown_uses_its_connectome(self):
        path = self._saveSceneWithSyn("clear.mrb")
        slicer.util.loadScene(path, {"clear": True})  # File > Load scene
        _pump(0.2)
        self._assertInStep("SceneSyn")

    def test_scene_imported_while_map_is_shown_uses_its_connectome(self):
        path = self._saveSceneWithSyn("import.mrb")
        slicer.util.loadScene(path)
        _pump(0.2)
        self._assertInStep("SceneSyn")

    def test_closed_scene_keeps_the_connectome_in_use_in_the_new_parameter_node(self):
        self.widget.initializeParameterNode()
        self.widget.refreshConnectomes()
        self._select(self.other, "SceneOther")
        slicer.mrmlScene.Clear(0)
        _pump(0.2)
        self._assertInStep("SceneOther")


if __name__ == "__main__":
    unittest.main()

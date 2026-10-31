"""Manage > Remove from the list on the connectome the open project uses.

The project keeps naming its connectome (shown as not registered, like a scene opened on a computer without it) and
no other registered bundle is chosen silently; the manager says that the open project uses it. Removing a bundle the
project does not use changes nothing in the project. A bundle that a background job registers when it ends (Get a
connectome…, Convert…) is listed, and chosen only when the project has no connectome yet. It needs the Slicer
application (no network)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingConnectomeManagerTest

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


class LesionNetworkMappingConnectomeManagerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from LesionNetworkMappingLib import connectome

        cls.tmp = tempfile.mkdtemp(prefix="lnm manager ")
        cls.paths = [
            os.path.abspath(connectome.make_synthetic(os.path.join(cls.tmp, f"{name}.lnmc"), shape=(8, 9, 8), n_subjects=6, runs=(12, 12), name=name)["path"])
            for name in ("Manager one", "Manager two")
        ]
        # Bundles that a background job (Get a connectome…, Convert…) registers while a project is open
        cls.later = [
            os.path.abspath(connectome.make_synthetic(os.path.join(cls.tmp, f"{name}.lnmc"), shape=(8, 9, 8), n_subjects=6, runs=(12, 12), name=name)["path"])
            for name in ("Manager fetched", "Manager converted")
        ]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        slicer.mrmlScene.Clear(0)
        slicer.util.selectModule("LesionNetworkMapping")
        self.widget = slicer.modules.LesionNetworkMappingWidget
        self.logic = self.widget.logic
        for path in self.paths:
            self.logic.registerConnectome(path)
        self.widget.refreshConnectomes(select=self.paths[0])
        _pump(100)

    def tearDown(self):
        import qt

        from LesionNetworkMapping import CONNECTOMES_SETTING

        if self.widget.managerDialog is not None:
            self.widget.managerDialog.hide()
        paths = [p for p in self.logic.registeredConnectomes() if os.path.abspath(p) not in self.paths + self.later]
        qt.QSettings().setValue(CONNECTOMES_SETTING, paths)
        slicer.mrmlScene.Clear(0)

    def _select(self, manager, path):
        for index, row in enumerate(manager.rows):
            if os.path.normpath(row["path"]) == os.path.normpath(path):
                manager.table.selectRow(index)
                return
        self.fail(f"{path} is not in the manager")

    def test_removing_the_connectome_in_use_keeps_the_project_naming_it(self):
        widget = self.widget
        node = widget._parameterNode
        self.assertIsNotNone(node)
        identifier = node.connectome
        self.assertTrue(identifier.startswith("Manager one#"), identifier)
        manager = widget.openManager()
        _pump(100)
        self._select(manager, self.paths[0])
        manager.removeButton.click()
        _pump(200)
        self.assertNotIn(self.paths[0], [os.path.abspath(p) for p in self.logic.registeredConnectomes()])
        self.assertEqual(node.connectome, identifier)
        self.assertIsNone(widget.bundle)
        self.assertEqual(widget._connectomePath, "")
        self.assertIn("not registered", widget.bundleError)
        self.assertEqual(widget.connectomeCombo.currentIndex, -1)
        self.assertIn("open project", str(manager.statusLabel.text))
        # Adding the folder again gives the project its connectome back
        widget.refreshConnectomes(select=self.paths[0])
        _pump(100)
        self.assertIsNotNone(widget.bundle)
        self.assertEqual(node.connectome, identifier)

    def test_context_menu_remove_keeps_the_project_naming_it(self):
        widget = self.widget
        identifier = widget._parameterNode.connectome
        self.assertTrue(widget.onRemoveBundle())
        _pump(100)
        self.assertEqual(widget._parameterNode.connectome, identifier)
        self.assertIsNone(widget.bundle)

    def test_removing_another_connectome_leaves_the_project_alone(self):
        widget = self.widget
        node = widget._parameterNode
        identifier = node.connectome
        manager = widget.openManager()
        _pump(100)
        self._select(manager, self.paths[1])
        manager.removeButton.click()
        _pump(200)
        self.assertEqual(node.connectome, identifier)
        self.assertIsNotNone(widget.bundle)
        self.assertEqual(os.path.abspath(widget.bundle.path), self.paths[0])
        self.assertNotIn("open project", str(manager.statusLabel.text))

    class _FinishedJob:
        state = "done"
        failureReason = None
        errorEvent = None

        @staticmethod
        def succeeded():
            return True

        @staticmethod
        def isFinished():
            return True

    def _assertProjectKeeps(self, identifier, newPath):
        widget = self.widget
        self.assertIn(newPath, [os.path.abspath(p) for p in self.logic.registeredConnectomes()])
        self.assertGreaterEqual(widget.connectomeCombo.findData(newPath), 0, "the new bundle is listed in the combo")
        self.assertEqual(widget._parameterNode.connectome, identifier)
        self.assertIsNotNone(widget.bundle)
        self.assertEqual(os.path.abspath(widget.bundle.path), self.paths[0])
        self.assertEqual(os.path.abspath(widget.connectomeCombo.currentData), self.paths[0])

    def test_background_fetch_keeps_the_project_connectome(self):
        from LesionNetworkMappingUI import manager_catalog

        identifier = self.widget._parameterNode.connectome
        self.assertTrue(identifier.startswith("Manager one#"), identifier)
        controller = manager_catalog.FetchController()
        controller.state = "running"
        controller.resultPath = self.later[0]
        controller._onDone(self._FinishedJob())
        _pump(100)
        self.assertEqual(controller.state, "done")
        self._assertProjectKeeps(identifier, self.later[0])

    def test_background_conversion_keeps_the_project_connectome(self):
        widget = self.widget
        identifier = widget._parameterNode.connectome
        manager = widget.openManager()
        _pump(100)
        captured = {}
        manager._submit = lambda spec, done: captured.setdefault("done", done)
        manager.startConversion(["convert"], self.later[1])
        captured["done"](self._FinishedJob())
        _pump(100)
        self._assertProjectKeeps(identifier, self.later[1])
        self.assertIn("Choose it in the Connectome card", str(manager.statusLabel.text))
        self.assertEqual(os.path.abspath(manager.selectedPath()), self.later[1])

    def test_background_job_chooses_the_new_bundle_for_a_project_without_one(self):
        widget = self.widget
        node = widget._parameterNode
        node.connectome = ""
        node.connectomePath = ""
        self.logic.registerConnectome(self.later[0])
        widget.refreshConnectomesAfterJob(self.later[0])
        _pump(100)
        self.assertTrue(node.connectome.startswith("Manager fetched#"), node.connectome)
        self.assertEqual(os.path.abspath(widget.bundle.path), self.later[0])

    def test_import_dialog_label_count_uses_the_singular_for_one_lesion(self):
        from LesionNetworkMappingUI import manager_parity

        folders = {}
        for name, lesions in (("one", ["sub-a"]), ("two", ["sub-a", "sub-b"])):
            folders[name] = os.path.join(self.tmp, f"maps {name}")
            os.makedirs(folders[name])
            for lesion in lesions:
                open(os.path.join(folders[name], f"{lesion}_conn-GSP1000v2xFullSet_desc-T_funcmap.nii.gz"), "wb").close()
        dialog = manager_parity.openImport(self.widget.ctx)
        try:
            labels = {}
            for name, folder in folders.items():
                dialog.folderEdit.setText(folder)
                dialog.scan()
                labels[name] = [dialog.labelCombo.itemText(i) for i in range(dialog.labelCombo.count)]
            self.assertEqual(labels["one"], ["GSP1000v2xFullSet (1 lesion)"])
            self.assertEqual(labels["two"], ["GSP1000v2xFullSet (2 lesions)"])
        finally:
            dialog.hide()


if __name__ == "__main__":
    unittest.main()

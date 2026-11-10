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

    def _useConnectome(self):
        """The scene holds a map computed with the selected connectome (what makes the scene "use" it)."""
        volume = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "Map of the connectome")
        self.widget._parameterNode.resultVolume = volume
        return volume

    def test_removing_the_connectome_in_use_keeps_the_project_naming_it(self):
        widget = self.widget
        node = widget._parameterNode
        self.assertIsNotNone(node)
        self._useConnectome()
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
        self._useConnectome()
        self.assertTrue(widget.onRemoveBundle())
        _pump(100)
        self.assertEqual(widget._parameterNode.connectome, identifier)
        self.assertIsNone(widget.bundle)

    def test_removing_the_auto_selected_connectome_without_a_project_just_unregisters_it(self):
        # Nothing uses the connectome: choosing it in the combo only stored its identifier in the parameter node
        widget = self.widget
        node = widget._parameterNode
        self.assertTrue(node.connectome.startswith("Manager one#"), node.connectome)
        self.assertFalse(widget._projectUsesConnectome())
        manager = widget.openManager()
        _pump(100)
        self._select(manager, self.paths[0])
        manager.removeButton.click()
        _pump(200)
        self.assertNotIn(self.paths[0], [os.path.abspath(p) for p in self.logic.registeredConnectomes()])
        self.assertNotIn("open project", str(manager.statusLabel.text))
        self.assertIn("Removed from the list", str(manager.statusLabel.text))
        # Map falls back to the remaining bundle: no "not registered" error
        self.assertIsNone(widget.bundleError)
        self.assertIsNotNone(widget.bundle)
        self.assertEqual(os.path.abspath(widget.bundle.path), self.paths[1])
        self.assertEqual(os.path.abspath(widget.connectomeCombo.currentData), self.paths[1])
        self.assertTrue(node.connectome.startswith("Manager two#"), node.connectome)

    def test_removing_the_only_connectome_without_a_project_shows_the_empty_state(self):
        widget = self.widget
        self.logic.unregisterConnectome(self.paths[1])
        widget.refreshConnectomes(select=self.paths[0])
        _pump(100)
        self.assertFalse(widget.onRemoveBundle())
        _pump(100)
        self.assertEqual(widget._parameterNode.connectome, "")
        self.assertIsNone(widget.bundle)
        self.assertIsNone(widget.bundleError)
        self.assertEqual(widget.connectomeCombo.count, 0)

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

    def test_manager_demo_button_shows_the_running_demo_job(self):
        # While the demo job runs the manager's button is disabled and shows the progress, like the Map card's button
        widget = self.widget

        class _RunningJob:
            def isFinished(self):
                return False

        started = []
        callbacks = {}

        def fakeStart(onEvent=None, onDone=None):
            started.append(1)
            callbacks["event"] = onEvent
            return _RunningJob()

        widget._demoJob = None
        original = self.logic.startDemoJob
        self.logic.startDemoJob = fakeStart
        try:
            manager = widget.openManager()
            _pump(100)
            button = manager.demoButton
            self.assertTrue(button.enabled)
            self.assertEqual(str(button.text), "Create synthetic demo")
            button.click()
            _pump(50)
            self.assertFalse(button.enabled)
            self.assertIn("Creating the demo", str(button.text))
            callbacks["event"]({"event": "progress", "done": 1, "total": 4})
            self.assertFalse(button.enabled)
            self.assertIn("25%", str(button.text))
            self.assertEqual(started, [1])
            # A manager opened while the job runs shows it as well
            manager.hide()
            manager.deleteLater()
            widget.managerDialog = None
            reopened = widget.openManager()
            _pump(100)
            self.assertFalse(reopened.demoButton.enabled)
            self.assertIn("25%", str(reopened.demoButton.text))
            # When it ends the button is back (the card button keeps its own label)
            widget._demoJob = None
            widget._updateDemoButton()
            self.assertTrue(reopened.demoButton.enabled)
            self.assertEqual(str(reopened.demoButton.text), "Create synthetic demo")
            self.assertEqual(str(widget.demoButton.text), "Use the demo")
        finally:
            self.logic.startDemoJob = original
            widget._demoJob = None
            widget._updateDemoButton()

    def test_reference_button_is_disabled_while_its_estimate_runs(self):
        # A double click on "Build reference maps…" asks once (and would build once): the estimate job marks the dialog busy
        widget = self.widget
        manager = widget.openManager()
        _pump(100)
        self._select(manager, self.paths[0])
        asked = []
        original = slicer.util.confirmYesNoDisplay
        slicer.util.confirmYesNoDisplay = lambda text, **kwargs: asked.append(text) and False
        try:
            manager.referenceButton.click()
            _pump(50)
            self.assertFalse(manager.referenceButton.enabled)
            self.assertIsNone(manager.buildReference(), "a second request while the estimate runs starts nothing")
            deadline = time.monotonic() + 120
            while (manager.job is not None or not asked) and time.monotonic() < deadline:
                _pump(50)
            _pump(200)
            self.assertEqual(len(asked), 1, asked)
            self.assertTrue(manager.referenceButton.enabled)
        finally:
            slicer.util.confirmYesNoDisplay = original

    def test_hubness_and_reference_on_a_read_only_connectome_say_so_before_any_job_or_question(self):
        # A lab share: no raw errno text, no confirmation that ends in a failure, no job (same wording as Build fast index…)
        widget = self.widget
        manager = widget.openManager()
        _pump(100)
        self._select(manager, self.paths[0])
        asked = []
        original, originalAccess = slicer.util.confirmYesNoDisplay, os.access
        slicer.util.confirmYesNoDisplay = lambda text, **kwargs: asked.append(text) and False
        target = os.path.normpath(self.paths[0])
        os.access = lambda p, mode, *a, **k: False if (mode & os.W_OK and os.path.normpath(str(p)) == target) else originalAccess(p, mode, *a, **k)
        try:
            self.assertIsNone(manager.buildHubness())
            self.assertIn("read-only", manager.statusLabel.text)
            self.assertNotIn("Errno", manager.statusLabel.text)
            self.assertIsNone(manager.job)
            manager.statusLabel.setText("")
            self.assertIsNone(manager.buildReference())
            self.assertIn("read-only", manager.statusLabel.text)
            self.assertIsNone(manager.job)
            _pump(200)
            self.assertEqual(asked, [])
        finally:
            os.access = originalAccess
            slicer.util.confirmYesNoDisplay = original

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

    def _defaultButtons(self, dialog):
        """The text of every push button of the dialog (not of the dialogs it opened) that Return would click."""
        return [str(button.text) for button in dialog.findChildren("QPushButton") if button.isDefault() and button.window().objectName == dialog.objectName]

    def test_return_in_the_manager_does_not_open_a_file_chooser(self):
        # No button is the default (QDialog made "Add bundle…" the default: Return on the table opened its file chooser)
        manager = self.widget.openManager()
        manager.show()
        _pump(200)
        self.assertEqual(self._defaultButtons(manager), [])
        for button in manager.findChildren("QPushButton"):
            if button.window().objectName == manager.objectName:
                self.assertFalse(button.autoDefault, str(button.text))
        manager.table.setFocus()
        _pump(100)
        self.assertEqual(self._defaultButtons(manager), [])

    def test_return_in_the_import_dialog_runs_import_not_browse(self):
        # "Browse…" was the default: Return in the Folder or Destination field opened its folder chooser
        from LesionNetworkMappingUI import manager_parity

        dialog = manager_parity.openImport(self.widget.ctx)
        try:
            dialog.show()
            _pump(200)
            self.assertEqual(self._defaultButtons(dialog), ["Import"])
            for edit in (dialog.folderEdit, dialog.outEdit):
                edit.setFocus()
                _pump(50)
                self.assertEqual(self._defaultButtons(dialog), ["Import"])
            self.assertFalse(dialog.findChild("QPushButton", "LNMLeadDbsImportBrowse").autoDefault)
        finally:
            dialog.hide()

    def test_return_in_the_convert_dialog_runs_convert(self):
        # "Estimate size" was the default; Return in a path field now runs Convert, never a Browse… button
        manager = self.widget.openManager()
        manager.openConvert()
        _pump(100)
        dialog = manager.convertDialog
        try:
            dialog.show()
            _pump(200)
            self.assertEqual(self._defaultButtons(dialog), ["Convert"])
            dialog.outEdit.setFocus()
            _pump(50)
            self.assertEqual(self._defaultButtons(dialog), ["Convert"])
            self.assertFalse(dialog.estimateButton.autoDefault)
        finally:
            dialog.hide()

    def test_convert_name_and_bundle_follow_the_folder_chosen_last(self):
        # Name and Bundle folder filled in automatically follow a second folder; typed or browsed values are kept
        manager = self.widget.openManager()
        dialog = manager.openConvert() or manager.convertDialog
        _pump(100)
        dialog = manager.convertDialog
        try:
            dialog.inputEdit.setText("/data/Cohort A")
            dialog._inputsChanged()
            self.assertEqual(dialog.nameEdit.text, "Cohort A")
            self.assertEqual(os.path.basename(dialog.outEdit.text), "CohortA.lnmc")
            dialog.inputEdit.setText("/data/Cohort B")
            dialog._inputsChanged()
            self.assertEqual(dialog.nameEdit.text, "Cohort B")
            self.assertEqual(os.path.basename(dialog.outEdit.text), "CohortB.lnmc")
            dialog.nameEdit.setText("My name")
            dialog.outEdit.setText("/elsewhere/mine.lnmc")
            dialog.inputEdit.setText("/data/Cohort C")
            dialog._inputsChanged()
            self.assertEqual(dialog.nameEdit.text, "My name")
            self.assertEqual(dialog.outEdit.text, "/elsewhere/mine.lnmc")
        finally:
            dialog.hide()

    def test_convert_without_resume_into_a_stopped_conversion_asks_first(self):
        """Bug BB-1 convert-without-resume-silently-deletes-stopped-conversion: Convert… with Resume unticked (the default) into
        the bundle folder of a stopped conversion deleted the subjects it had written. Convert asks now: Resume (ticks it),
        Start over (--overwrite) or Cancel (no job). A folder without a stopped conversion is not asked about.
        """
        from unittest import mock

        import numpy as np

        from LesionNetworkMappingLib import connectome

        out = os.path.join(self.tmp, "Stopped convert.lnmc")
        mask = np.zeros((4, 4, 4), bool)
        mask[1:3, 1:3, 1:3] = True
        writer = connectome.ConnectomeWriter(out, mask, np.eye(4), name="Stopped convert")
        writer.add_subject("sub-01", [np.random.default_rng(0).standard_normal((int(mask.sum()), 6))])
        manager = self.widget.openManager()
        dialog = manager.openConvert() or manager.convertDialog
        try:
            dialog.sourceCombo.setCurrentIndex(dialog.sourceCombo.findData("nifti"))
            dialog.termsEdit.setPlainText("")
            dialog.inputEdit.setText(self.tmp)
            dialog.maskEdit.setText(os.path.join(self.tmp, "mask.nii.gz"))
            dialog.outEdit.setText(out)
            for answer, expected in ((None, None), ("resume", "--resume"), ("overwrite", "--overwrite")):
                dialog.resumeCheckBox.setChecked(False)
                dialog.estimate = {"enough": True}
                with mock.patch.object(dialog, "askInterrupted", return_value=answer) as ask, mock.patch.object(manager, "startConversion") as start:
                    dialog.onConvert()
                ask.assert_called_once_with(out)
                if expected is None:
                    start.assert_not_called()
                    continue
                args = start.call_args[0][0]
                self.assertIn(expected, args)
                self.assertFalse({"--resume", "--overwrite"} - {expected} & set(args))
            self.assertEqual(sorted(os.listdir(os.path.join(out, "subjects"))), ["sub-01.f16.npy", "sub-01.norms.npy"])  # the dialog deletes nothing itself
            # Resume ticked, or a folder without a stopped conversion: nothing to ask
            for ticked, folder in ((True, out), (False, os.path.join(self.tmp, "New convert.lnmc"))):
                dialog.resumeCheckBox.setChecked(ticked)
                dialog.outEdit.setText(folder)
                dialog.estimate = {"enough": True}
                with mock.patch.object(dialog, "askInterrupted") as ask, mock.patch.object(manager, "startConversion") as start:
                    dialog.onConvert()
                ask.assert_not_called()
                self.assertEqual("--resume" in start.call_args[0][0], ticked)
                self.assertNotIn("--overwrite", start.call_args[0][0])
        finally:
            dialog.hide()
            shutil.rmtree(out, ignore_errors=True)

    def test_buttons_show_their_full_text_at_the_smallest_size(self):
        # The seven actions of the selected connectome were one row: at 1366 x 768 their labels were cut. They wrap now:
        # as opened (the dialog fits the screen) and at the dialog's minimum size, every visible button is as wide as its
        # text needs, inside the dialog, and overlaps no other button. Long connectome names may still be cut.
        import qt

        manager = self.widget.openManager()
        _pump(200)
        actions = ("Build reference maps…", "Build hubness map", "Remove from the list", "Get a connectome…", "Import Lead-DBS maps…",
                   "Verify against Lead-DBS…", "Build fast index…")
        texts = [str(button.text) for button in manager.findChildren("QPushButton") if button.isVisible()]
        for text in actions:
            self.assertIn(text, texts)

        def check(when):
            dialog = qt.QRect(0, 0, manager.width, manager.height)
            rects = {}
            for button in manager.findChildren("QPushButton"):
                if not button.isVisible():
                    continue
                text = str(button.text)
                need = button.sizeHint.width()
                self.assertGreaterEqual(button.width, need, f"{when}: '{text}' is {button.width} px wide, its text needs {need} px")
                rect = qt.QRect(button.mapTo(manager, qt.QPoint(0, 0)), button.size)
                self.assertTrue(dialog.contains(rect), f"{when}: '{text}' ({rect}) leaves the dialog ({dialog})")
                for other, otherRect in rects.items():
                    self.assertFalse(rect.intersects(otherRect), f"{when}: '{text}' overlaps '{other}'")
                rects[text] = rect

        check("as opened")
        self.assertLessEqual(manager.width, 1366, "the dialog is wider than a 1366 x 768 screen")
        self.assertLessEqual(manager.height, 768, "the dialog is higher than a 1366 x 768 screen")
        manager.resize(manager.minimumSize)
        _pump(200)
        self.assertEqual((manager.width, manager.height), (manager.minimumWidth, manager.minimumHeight))
        check("at the minimum size")

    def test_name_column_stays_readable_at_a_larger_app_font(self):
        # At a 125% app font (17 px) the seven content-sized columns took the whole 720 px dialog and left the Name column
        # 65 px ("Sh…"). The dialog asks for the width of the other columns plus a readable Name column, up to the screen.
        import qt

        original = qt.QFont(qt.QApplication.font())
        font = qt.QFont(original)
        manager = None
        try:
            if self.widget.managerDialog is not None:
                self.widget.managerDialog.hide()
                self.widget.managerDialog = None  # a manager made at the new font
            font.setPixelSize(17)
            qt.QApplication.setFont(font)
            _pump(300)
            manager = self.widget.openManager()
            _pump(300)
            table = manager.table
            metrics = qt.QFontMetrics(table.font)
            shortest = min(metrics.horizontalAdvance(str(table.item(row, 0).text()).split(" (")[0]) for row in range(table.rowCount))
            self.assertGreaterEqual(table.columnWidth(0), shortest + 12, f"the Name column is {table.columnWidth(0)} px, a short name needs {shortest} px")
            self.assertGreaterEqual(table.columnWidth(0), manager.NAME_COLUMN_WIDTH[0] - 20)
            self.assertFalse(table.horizontalScrollBar().isVisible(), "the table scrolls sideways")
        finally:
            if manager is not None:
                manager.hide()
            self.widget.managerDialog = None
            qt.QApplication.setFont(original)
            _pump(200)

    def test_table_width_hint_reads_the_layout_margins_on_qt5_and_qt6(self):
        # Qt 6 declares the contentsMargins property on QLayout, so PythonQt gives the QMargins value instead of the
        # getter: "layout.contentsMargins()" raised TypeError in the manager's constructor and no entry point opened it
        manager = self.widget.openManager()
        _pump(100)
        layout = manager.layout()

        class _Qt6Layout:
            """The layout as Qt 6 PythonQt shows it: contentsMargins is the property value."""

            def __init__(self, layout):
                self._layout = layout

            def __getattr__(self, name):
                if name == "contentsMargins":
                    return self._layout.contentsMargins()
                return getattr(self._layout, name)

        qt5 = manager._tableWidthHint(layout)
        qt6 = manager._tableWidthHint(_Qt6Layout(layout))
        self.assertGreater(qt5, 0)
        self.assertEqual(qt6, qt5)


    def test_typed_paths_of_convert_and_import_are_expanded_or_refused(self):
        """'~/…' typed as Convert…'s bundle folder was given to the job unchanged: the job runs in the module folder, so the bundle
        was written into <modules>/~/… inside the application, and registering the same text (resolved in the application's own
        folder) failed. '~' is expanded now in Convert… and Import Lead-DBS maps…; a relative path is refused with a hint.
        """
        from unittest import mock

        from LesionNetworkMappingUI import jobrunner, manager_parity

        home = os.path.expanduser("~")
        manager = self.widget.openManager()
        dialog = manager.openConvert()
        try:
            dialog.sourceCombo.setCurrentIndex(dialog.sourceCombo.findData("nifti"))
            dialog.termsEdit.setPlainText("")
            dialog.resumeCheckBox.setChecked(False)
            dialog.inputEdit.setText("~/LNM convert runs")
            dialog.maskEdit.setText("~/LNM convert runs/mask.nii.gz")
            dialog.outEdit.setText(" ~/LNM tilde.lnmc ")
            with mock.patch.object(jobrunner, "instance") as runner:
                dialog.runEstimate()
                args = runner.return_value.submit.call_args[0][0].args
            self.assertEqual(args[args.index("--out") + 1], os.path.join(home, "LNM tilde.lnmc"))  # the free space of the folder written to
            dialog.estimate = {"enough": True}
            with mock.patch.object(manager, "startConversion") as start:
                dialog.onConvert()
            args, outPath = start.call_args[0]
            self.assertEqual(outPath, os.path.join(home, "LNM tilde.lnmc"))  # what is registered is what the job writes
            self.assertEqual(args[args.index("--out") + 1], outPath)
            self.assertEqual(args[args.index("--input") + 1], os.path.join(home, "LNM convert runs"))
            self.assertEqual(args[args.index("--mask") + 1], os.path.join(home, "LNM convert runs", "mask.nii.gz"))
            for edit in (dialog.outEdit, dialog.inputEdit, dialog.maskEdit):
                saved = edit.text
                edit.setText("relative folder")
                dialog.estimate = {"enough": True}
                with mock.patch.object(manager, "startConversion") as start, mock.patch.object(jobrunner, "instance") as runner:
                    dialog.onConvert()
                    self.assertIsNone(dialog.runEstimate())
                    start.assert_not_called()
                    runner.assert_not_called()
                self.assertIn("is not a full path", str(dialog.estimateLabel.text), edit.objectName)
                edit.setText(saved)
        finally:
            dialog.hide()
        importDialog = manager_parity.openImport(self.widget.ctx)
        try:
            importDialog.folderEdit.setText("~/LNM lead-dbs maps")
            importDialog.outEdit.setText("~/LNM imported")
            args = importDialog.arguments()
            self.assertEqual(args[args.index("--folder") + 1], os.path.join(home, "LNM lead-dbs maps"))
            if "--out" in args:  # no study open: the typed destination folder
                self.assertEqual(args[args.index("--out") + 1], os.path.join(home, "LNM imported"))
            importDialog.scan()
            self.assertIn(os.path.join(home, "LNM lead-dbs maps"), str(importDialog.statusLabel.text))  # not found, but looked for in the home folder
            importDialog.folderEdit.setText("lead-dbs maps")
            importDialog.scan()
            self.assertIn("is not a full path", str(importDialog.statusLabel.text))
            self.assertIsNone(importDialog.arguments())
            with mock.patch.object(jobrunner, "instance") as runner:
                self.assertIsNone(importDialog.startImport())
                runner.assert_not_called()
            self.assertIn("is not a full path", str(importDialog.statusLabel.text))
        finally:
            importDialog.hide()

    def test_same_name_connectomes_are_told_apart(self):
        from LesionNetworkMapping import CONNECTOMES_SETTING
        from LesionNetworkMappingLib import connectome

        folders = [os.path.join(self.tmp, "same full"), os.path.join(self.tmp, "same subset")]
        paths = []
        for folder, n in zip(folders, (6, 3), strict=True):
            os.makedirs(folder, exist_ok=True)
            paths.append(os.path.abspath(connectome.make_synthetic(os.path.join(folder, "c.lnmc"), shape=(8, 9, 8), n_subjects=n, runs=(12, 12), name="Same Name")["path"]))
        import qt

        saved = list(qt.QSettings().value(CONNECTOMES_SETTING) or [])
        try:
            for path in paths:
                self.logic.registerConnectome(path)
            self.widget.refreshConnectomes(select=paths[0])
            combo = self.widget.connectomeCombo
            texts = [combo.itemText(i) for i in range(combo.count)]
            same = [combo.itemText(combo.findData(p)) for p in paths]
            self.assertEqual(len(set(same)), 2, same)
            self.assertIn("6", same[0])
            self.assertIn("3", same[1])
            # Names that are unique stay as they are
            self.assertEqual([t for t in texts if t.startswith("Manager one")], ["Manager one (SYNTHETIC — not real data)"])
            manager = self.widget.openManager()
            _pump(100)
            names = [manager.table.item(r, 0).text() for r in range(manager.table.rowCount) if "Same Name" in manager.table.item(r, 0).text()]
            self.assertEqual(len(set(names)), 2, names)
        finally:
            qt.QSettings().setValue(CONNECTOMES_SETTING, saved)
            self.widget.refreshConnectomes(select=self.paths[0])


if __name__ == "__main__":
    unittest.main()

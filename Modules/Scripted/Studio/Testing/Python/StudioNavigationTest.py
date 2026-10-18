r"""Navigation tests of the Slicer Studio shell: "where am I, how do I get back".

Run with the main window and testing enabled:

  Slicer --no-splash --testing --python-code "import slicer.testing; slicer.testing.runUnitTest(['<this dir>'], 'StudioNavigationTest'); slicer.app.exit(0)"

Studio never runs by itself under --testing; the tests apply it with ``Shell(force=True)`` and
always restore the Classic interface. They cover:

- the inspector key (``Ctrl+\``, not Tab) and the "Panel hidden" hint, and the tab of the current
  workspace showing the inspector again and leaving the focus mode;
- the focus mode toast and its "Exit focus mode" pill;
- the Esc stack (place / window-level mode, maximized view, focus mode), with synthetic key events;
- Classic -> Studio returning to the workspace of the current module;
- View > Toolbars toggles honoured in Studio (``Studio/AllowedToolbars``) and not in Classic;
- a stable inspector width while Segment Editor tools are selected, also at 1366x768;
- the first-run window size.
"""

import numpy as np
import qt

import slicer
from slicer import ScriptedLoadableModule

# Widgets created by the tests that must live until the application exits
_keepAlive = []


def portableKey(keySequence):
    if isinstance(keySequence, str):
        keySequence = qt.QKeySequence(keySequence)
    return keySequence.toString(qt.QKeySequence.PortableText)


class StudioNavigationTest(ScriptedLoadableModule.ScriptedLoadableModuleTest):
    """Navigation tests of the Studio shell. They need the main window."""

    def setUp(self):
        slicer.mrmlScene.Clear(0)
        self.mainWindow = slicer.util.mainWindow()
        self.layoutManager = slicer.app.layoutManager()
        self.initialLayout = self.layoutManager.layout if self.layoutManager is not None else None
        self.initialGeometry = self.mainWindow.saveGeometry() if self.mainWindow is not None else None

    def tearDown(self):
        from StudioLib import shell

        live = shell._shell
        if live is not None:
            live.dispose()
        interactionNode = slicer.app.applicationLogic().GetInteractionNode()
        if interactionNode is not None:
            interactionNode.SwitchToViewTransformMode()
        if self.layoutManager is not None:
            self.layoutManager.removeAllMaximizedViewNodes()
            if self.initialLayout is not None and self.layoutManager.layout != self.initialLayout:
                self.layoutManager.setLayout(self.initialLayout)
        if self.initialGeometry is not None:
            self.mainWindow.restoreGeometry(self.initialGeometry)
        slicer.app.processEvents()

    def runTest(self):
        for name in sorted(name for name in dir(self) if name.startswith("test_")):
            self.setUp()
            try:
                getattr(self, name)()
            finally:
                self.tearDown()

    def _pump(self, count=4):
        for index in range(count):
            slicer.app.processEvents()

    def _applyStudio(self):
        from StudioLib import shell

        self.assertIsNotNone(self.mainWindow, "Studio tests need the main window")
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        self._pump()
        return studioShell

    def _dock(self):
        return self.mainWindow.findChild("QDockWidget", "PanelDockWidget")

    def _sendEscape(self, target, eventType=qt.QEvent.KeyPress):
        """Send Esc to ``target`` (it propagates to the main window like a real key press). Return the event."""
        event = qt.QKeyEvent(eventType, qt.Qt.Key_Escape, qt.Qt.NoModifier)
        if eventType == qt.QEvent.ShortcutOverride:
            # Qt asks whether a shortcut may take the key: accepted means "not a shortcut"
            event.ignore()
        qt.QApplication.sendEvent(target, event)
        return event

    def _redSliceView(self):
        return self.layoutManager.sliceWidget("Red").sliceView()

    def _loadScanAndSegmentation(self):
        volume = slicer.util.addVolumeFromArray(np.zeros((30, 64, 64), dtype=np.int16), name="patient_T1")
        segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode")
        segmentation.CreateDefaultDisplayNodes()
        segmentation.SetReferenceImageGeometryParameterFromVolumeNode(volume)
        segmentation.GetSegmentation().AddEmptySegment("Lesion")
        return volume, segmentation

    def test_1_inspector_key_and_current_tab(self):
        r"""Ctrl+\ (not Tab) toggles the inspector; the current workspace tab shows it again (NAV-04, CM-01)."""
        from StudioLib import shell

        studioShell = self._applyStudio()
        studioShell.switchTo("draw")
        self._pump()
        dock = self._dock()
        keys = [portableKey(shortcut.key) for shortcut in studioShell.shortcuts()]
        self.assertNotIn("Tab", keys)
        self.assertIn(portableKey(shell.INSPECTOR_SHORTCUT), keys)
        try:
            import StudioTest

            StudioTest.assertNoKeyCollisions()
            self.assertEqual(StudioTest.ambiguousShortcuts(self.mainWindow), {})
        except ImportError:
            pass
        # The app menu shows the key
        appMenu = studioShell.topBar.appMenu
        self.assertIn(studioShell.inspectorShortcutText(), appMenu.inspectorAction.text)

        shell._panelHiddenHintShown = False
        self.assertFalse(dock.isHidden())
        studioShell.toggleInspector()
        self._pump()
        self.assertTrue(dock.isHidden())
        self.assertTrue(studioShell._toast.isVisible())
        self.assertIn(studioShell.inspectorShortcutText(), studioShell._toast.text())
        # Clicking the tab of the current workspace shows it again
        studioShell.topBar.workspaceButtons["draw"].click()
        self._pump()
        self.assertFalse(dock.isHidden())
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        # The hint is shown once per session
        studioShell.toast("other", 5000)
        studioShell.toggleInspector()
        self._pump()
        self.assertEqual(studioShell._toast.text(), "other")
        # Ctrl+Alt+2 (switchTo the current workspace) shows it again too
        studioShell.switchTo("draw")
        self._pump()
        self.assertFalse(dock.isHidden())
        # Ctrl+5 (View > Module Panel) counts as hidden by the user
        dock.toggleViewAction().trigger()
        self._pump()
        self.assertTrue(dock.isHidden())
        studioShell.switchTo("draw")
        self._pump()
        self.assertFalse(dock.isHidden())

        # The current tab leaves the focus mode
        studioShell.setFocusMode(True)
        self._pump()
        self.assertTrue(studioShell.focusMode())
        studioShell.switchTo("draw")
        self._pump()
        self.assertFalse(studioShell.focusMode())
        self.assertFalse(dock.isHidden())
        self.assertTrue(studioShell.topBar.toolBar.isVisible())

        # The Library empty state hides the inspector itself: its tab does not show it
        studioShell.switchTo("library")
        self._pump()
        if dock.isHidden():
            studioShell.switchTo("library")
            self._pump()
            self.assertTrue(dock.isHidden())

    def test_2_focus_mode_exit(self):
        """The focus mode says how to leave it and shows an "Exit focus mode" pill (NAV-05)."""
        studioShell = self._applyStudio()
        studioShell.switchTo("draw")
        self._pump()
        pill = studioShell._focusModePill
        self.assertFalse(pill.isVisible())
        studioShell.toggleFocusMode()
        self._pump()
        self.assertTrue(studioShell.focusMode())
        self.assertTrue(pill.isVisible())
        self.assertIn("Esc", studioShell._toast.text())
        centralWidget = self.mainWindow.centralWidget()
        # Top-right corner of the views
        self.assertGreater(pill.button.x, centralWidget.width // 2)
        self.assertLess(pill.button.y, 60)
        pill.button.click()
        self._pump()
        self.assertFalse(studioShell.focusMode())
        self.assertFalse(pill.isVisible())
        self.assertFalse(self._dock().isHidden())
        # Ctrl+\ in focus mode leaves it and shows the inspector
        studioShell.setFocusMode(True)
        studioShell.toggleInspector()
        self._pump()
        self.assertFalse(studioShell.focusMode())
        self.assertFalse(self._dock().isHidden())
        # Classic removes the pill
        studioShell.setFocusMode(True)
        studioShell.classic(persist=False)
        self._pump()
        self.assertIsNone(self.mainWindow.findChild("QToolButton", "StudioExitFocusModeButton"))
        self.assertTrue(self.mainWindow.menuBar().isVisible())

    def test_3_escape_stack(self):
        """Esc leaves place / window-level mode, then a maximized view, then the focus mode (NAV-08)."""
        studioShell = self._applyStudio()
        studioShell.switchTo("draw")
        self._pump()
        sliceView = self._redSliceView()
        interactionNode = slicer.app.applicationLogic().GetInteractionNode()
        redNode = self.layoutManager.sliceWidget("Red").mrmlSliceNode()

        # A synthetic key press goes through the shortcut map like a real one: Qt first asks the focus widget
        # and its parents whether a shortcut may take the key (ShortcutOverride), then sends the key press.

        # Nothing to back out of: the key goes on to its other handlers
        override = self._sendEscape(sliceView, qt.QEvent.ShortcutOverride)
        self.assertFalse(override.isAccepted())
        self.assertFalse(studioShell.handleEscape())

        # 1. Place and window/level modes, each with one Esc
        for mode in (slicer.vtkMRMLInteractionNode.Place, slicer.vtkMRMLInteractionNode.AdjustWindowLevel):
            interactionNode.SetCurrentInteractionMode(mode)
            press = self._sendEscape(sliceView)
            self.assertTrue(press.isAccepted())
            self.assertEqual(interactionNode.GetCurrentInteractionMode(), slicer.vtkMRMLInteractionNode.ViewTransform)

        # Esc with a modifier is left alone
        studioShell.setFocusMode(True)
        shiftEscape = qt.QKeyEvent(qt.QEvent.KeyPress, qt.Qt.Key_Escape, qt.Qt.ShiftModifier)
        qt.QApplication.sendEvent(sliceView, shiftEscape)
        self.assertTrue(studioShell.focusMode())
        studioShell.setFocusMode(False)

        # A maximized view, the focus mode and an active Segment Editor tool: one state per Esc, innermost
        # first, and the tool is deselected only when nothing else is left. (The Segment Editor itself
        # leaves the place mode when a tool is active.)
        volume, segmentation = self._loadScanAndSegmentation()
        editor = slicer.modules.SegmentEditorWidget.editor
        editor.setSegmentationNode(segmentation)
        editor.setSourceVolumeNode(volume)
        editor.setActiveEffectByName("Paint")
        studioShell.setFocusMode(True)
        self.layoutManager.addMaximizedViewNode(redNode)
        self._pump()
        layoutNode = self.layoutManager.layoutLogic().GetLayoutNode()
        self.assertEqual(layoutNode.GetNumberOfMaximizedViewNodes(), 1)
        self.assertIsNotNone(editor.activeEffect())

        press = self._sendEscape(sliceView)
        self._pump()
        self.assertTrue(press.isAccepted())
        self.assertEqual(layoutNode.GetNumberOfMaximizedViewNodes(), 0)
        self.assertTrue(studioShell.focusMode())
        self.assertIsNotNone(editor.activeEffect())

        # The handled shortcut override keeps the Segment Editor's Esc shortcut from firing
        sliceView = self._redSliceView()
        override = self._sendEscape(sliceView, qt.QEvent.ShortcutOverride)
        self.assertTrue(override.isAccepted())
        self._pump()
        self.assertFalse(studioShell.focusMode())
        self.assertFalse(self._dock().isHidden())
        self.assertFalse(studioShell._focusModePill.isVisible())
        self.assertIsNotNone(editor.activeEffect())

        # Nothing left: Esc is not consumed, so it reaches the Segment Editor's Esc shortcut, which deselects
        # the tool (window shortcuts only fire in the active window, which a headless test may not have)
        override = self._sendEscape(sliceView, qt.QEvent.ShortcutOverride)
        self.assertFalse(override.isAccepted())
        press = self._sendEscape(sliceView)
        self._pump()
        if slicer.app.activeWindow() == self.mainWindow:
            self.assertIsNone(editor.activeEffect())
        else:
            self.assertFalse(press.isAccepted())

        # Text fields keep their Esc (a field over the views stays shown in the focus mode)
        studioShell.setFocusMode(True)
        lineEdit = qt.QLineEdit(self.mainWindow.centralWidget())
        lineEdit.show()
        self.mainWindow.activateWindow()
        lineEdit.setFocus()
        self._pump()
        if slicer.app.focusWidget() == lineEdit:
            self._sendEscape(lineEdit)
            self.assertTrue(studioShell.focusMode())
        studioShell.setFocusMode(False)
        lineEdit.hide()
        lineEdit.setParent(None)
        _keepAlive.append(lineEdit)

        # A transient module still returns with its close button
        slicer.util.selectModule("Data")
        self._pump()
        self.assertIsNone(studioShell.currentWorkspace())
        self._sendEscape(sliceView)
        self.assertIsNone(studioShell.currentWorkspace())
        studioShell.panelHeader.closeButton.click()
        self._pump()
        self.assertEqual(studioShell.currentWorkspace(), "draw")

        # Classic: Studio does not handle Esc
        studioShell.classic(persist=False)
        self._pump()
        self.layoutManager.addMaximizedViewNode(redNode)
        self._pump()
        self._sendEscape(sliceView)
        self.assertEqual(layoutNode.GetNumberOfMaximizedViewNodes(), 1)
        self.layoutManager.removeAllMaximizedViewNodes()

    def test_4_classic_round_trip_returns_to_workspace(self):
        """Classic -> Studio returns to the workspace of the current module (NAV-06); startup is unchanged."""
        studioShell = self._applyStudio()
        settings = qt.QSettings()
        savedMode = settings.value("Studio/Mode")
        try:
            studioShell.switchTo("draw")
            self._pump()
            studioShell.classic(persist=False)
            self._pump()
            self.assertEqual(studioShell.currentModuleName(), "SegmentEditor")
            studioShell.studio()
            self._pump()
            self.assertEqual(studioShell.currentWorkspace(), "draw")
            self.assertEqual(studioShell.currentModuleName(), "SegmentEditor")

            # A module without workspace: back to the last workspace
            studioShell.classic(persist=False)
            slicer.util.selectModule("Data")
            self._pump()
            studioShell.studio()
            self._pump()
            self.assertEqual(studioShell.currentWorkspace(), "draw")

            # The Library module: Library
            studioShell.classic(persist=False)
            slicer.util.selectModule("Studio")
            self._pump()
            studioShell.studio()
            self._pump()
            self.assertEqual(studioShell.currentWorkspace(), "library")

            # At startup (apply()) the start workspace is shown whatever the current module
            studioShell.classic(persist=False)
            slicer.util.selectModule("SegmentEditor")
            self._pump()
            studioShell.apply()
            self._pump()
            self.assertEqual(studioShell.currentWorkspace(), studioShell._startWorkspace())
        finally:
            if savedMode is None:
                settings.remove("Studio/Mode")
            else:
                settings.setValue("Studio/Mode", savedMode)

    def test_5_toolbar_toggle_honoured(self):
        """View > Toolbars shows a toolbar in Studio and remembers it; Classic is unchanged (NAV-03, CM-08)."""
        mainWindow = self.mainWindow
        settings = qt.QSettings()
        savedAllowed = settings.value("Studio/AllowedToolbars")
        settings.remove("Studio/AllowedToolbars")
        stateBefore = mainWindow.saveState()
        toolBar = mainWindow.findChild("QToolBar", "MouseModeToolBar") or mainWindow.findChild("QToolBar", "ViewToolBar")
        self.assertIsNotNone(toolBar)
        classicVisible = toolBar.isVisible()
        try:
            studioShell = self._applyStudio()
            self.assertTrue(toolBar.isHidden())
            toggle = toolBar.toggleViewAction()
            toggle.trigger()
            self._pump(8)
            self.assertTrue(toolBar.isVisible())
            self.assertIn(toolBar.objectName, studioShell._allowedToolBars())
            # Unchecking it hides it and forgets it
            toggle.trigger()
            self._pump(8)
            self.assertTrue(toolBar.isHidden())
            self.assertNotIn(toolBar.objectName, studioShell._allowedToolBars())
            # A toolbar that shows itself (not from View > Toolbars) is still hidden again
            toolBar.show()
            self._pump(8)
            self.assertTrue(toolBar.isHidden())
            # Shown again, then Classic restores the classic state exactly
            toggle.trigger()
            self._pump(8)
            self.assertTrue(toolBar.isVisible())
            studioShell.classic(persist=False)
            self._pump()
            self.assertEqual(toolBar.isVisible(), classicVisible)
            self.assertEqual(studioShell._toolBarToggleConnections, [])
            # In Classic the toggle does not change the Studio setting
            allowed = studioShell._allowedToolBars()
            toggle.trigger()
            self._pump()
            self.assertEqual(studioShell._allowedToolBars(), allowed)
            toggle.trigger()
            self._pump()
            self.assertEqual(mainWindow.saveState(), stateBefore)
            # Applied again, the remembered toolbar is shown
            studioShell.apply()
            self._pump(8)
            self.assertTrue(toolBar.isVisible())
        finally:
            if savedAllowed is None:
                settings.remove("Studio/AllowedToolbars")
            else:
                settings.setValue("Studio/AllowedToolbars", savedAllowed)

    def test_6_stable_panel_width(self):
        """Selecting Segment Editor tools does not change the inspector width (NFT-06)."""
        mainWindow = self.mainWindow
        mainWindow.resize(1600, 1000)
        self._pump()
        modulePanel = mainWindow.findChild("QWidget", "ModulePanel")
        scrollArea = modulePanel.findChild("QScrollArea", "ScrollArea")
        classicPolicy = scrollArea.horizontalScrollBarPolicy
        classicMinimumWidth = modulePanel.minimumWidth
        studioShell = self._applyStudio()
        volume, segmentation = self._loadScanAndSegmentation()
        studioShell.switchTo("draw")
        self._pump(10)
        editor = slicer.modules.SegmentEditorWidget.editor
        editor.setSegmentationNode(segmentation)
        editor.setSourceVolumeNode(volume)
        self._pump(10)
        dock = self._dock()
        # Draw lands on the stored width (Studio/PanelWidth, 340 px by default)
        self.assertLessEqual(abs(dock.width - studioShell.panelWidth), 2)
        reference = dock.width
        for size in ((1600, 1000), (1366, 768)):
            mainWindow.resize(*size)
            self._pump(10)
            for effectName in ("Paint", "Draw", "Erase", "Threshold", "Islands", ""):
                editor.setActiveEffectByName(effectName)
                self._pump(10)
                self.assertLessEqual(abs(dock.width - reference), 2, f"{effectName} at {size}")
                # Wide options scroll rather than being clipped
                contents = scrollArea.widget()
                self.assertGreaterEqual(contents.width, contents.layout().minimumSize().width() - 1, effectName)
                if contents.width > scrollArea.viewport().width:
                    self.assertTrue(scrollArea.horizontalScrollBar().isVisible(), effectName)
        mainWindow.resize(1600, 1000)
        self._pump()
        # A transient module may widen the inspector; the workspace gets the stored width back
        slicer.util.selectModule("Data")
        self._pump(10)
        studioShell.closeTransientModule()
        self._pump(10)
        self.assertLessEqual(abs(dock.width - reference), 2)

        studioShell.classic(persist=False)
        self._pump()
        self.assertEqual(scrollArea.horizontalScrollBarPolicy, classicPolicy)
        self.assertEqual(modulePanel.minimumWidth, classicMinimumWidth)
        for browser in modulePanel.findChildren("ctkFittedTextBrowser"):
            self.assertNotEqual(browser.minimumWidth, 1, browser.objectName)

    def test_7_first_run_window_size(self):
        """Without a restored geometry the window gets about 90% of the screen (novice step 2)."""
        from StudioLib import shell

        mainWindow = self.mainWindow
        settings = slicer.app.userSettings()
        restored = slicer.util.toBool(settings.value("MainWindow/RestoreGeometry", False)) and settings.contains("MainWindow/geometry")
        keysBefore = sorted(settings.allKeys())
        mainWindow.showNormal()
        mainWindow.resize(835, 570)
        self._pump()
        resized = shell.fitWindowToScreen(mainWindow)
        self._pump()
        self.assertEqual(sorted(settings.allKeys()), keysBefore)
        if restored:
            self.assertFalse(resized)
            return
        self.assertTrue(resized)
        screen = mainWindow.screen() if callable(getattr(mainWindow, "screen", None)) else slicer.app.primaryScreen()
        available = screen.availableGeometry() if callable(screen.availableGeometry) else screen.availableGeometry
        self.assertGreaterEqual(mainWindow.width, int(available.width() * 0.85))
        self.assertGreaterEqual(mainWindow.height, int(available.height() * 0.85))
        # Already large enough: unchanged
        self.assertFalse(shell.fitWindowToScreen(mainWindow))

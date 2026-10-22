r"""Navigation tests of the Slicer Studio shell: "where am I, how do I get back".

Run with the main window and testing enabled:

  Slicer --no-splash --testing --python-code "import slicer.testing; slicer.testing.runUnitTest(['<this dir>'], 'StudioNavigationTest'); slicer.app.exit(0)"

Studio never runs by itself under --testing; the tests apply it with ``Shell(force=True)`` and
always restore the Classic interface. They cover:

- the inspector keys (``Ctrl+\`` and ``Ctrl+5``, not Tab) and the "Inspector hidden" toast, and the tab of the
  current workspace showing the inspector again and the hidden panels;
- the "Hide panels" toast and its "Show panels" pill;
- the Esc ladder (overlays, place / window-level mode, maximized view, hidden panels, transient module), with
  synthetic key events, also with a spin box, a line edit or an item editor focused;
- Classic -> Studio returning to the workspace of the current module;
- View > Toolbars toggles honoured in Studio (``Studio/AllowedToolbars``) and not in Classic;
- a stable inspector width while Segment Editor tools are selected, also at 1366x768;
- the first-run window size;
- the labelled back button of transient modules, Ctrl+F and Ctrl+H, the inspector notice slot, the help and
  menu hooks, the scene root directory (Ctrl+S folder), the contrast tokens, the Studio-only navigation
  behaviours, the error log docking, the names and platform keys, F10 / Alt menus, F6 regions, Space and
  Enter on focused buttons, no invisible Tab stop over the Library canvas, the inspector edge handle and the
  consequence toasts;
- PageUp and PageDown stepping the slice view under the pointer when the focus is on the inspector.
"""

import os
import sys
import time

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
        return self._sendKey(target, qt.Qt.Key_Escape, eventType=eventType)

    def _sendKey(self, target, key, modifiers=qt.Qt.NoModifier, eventType=qt.QEvent.KeyPress, text=""):
        event = qt.QKeyEvent(eventType, key, modifiers, text)
        if eventType == qt.QEvent.ShortcutOverride:
            # Qt asks whether a shortcut may take the key: accepted means "not a shortcut"
            event.ignore()
        qt.QApplication.sendEvent(target, event)
        return event

    def _pressEscape(self, target=None):
        """A real Esc press as Qt delivers it: the shortcut override, then the key press (unless the override was taken)."""
        target = target if target is not None else self.mainWindow
        override = self._sendEscape(target, qt.QEvent.ShortcutOverride)
        if not override.isAccepted():
            self._sendEscape(target)
        self._pump()
        return override.isAccepted()

    def _studioShortcut(self, studioShell, keySequence):
        """The Studio QShortcut on ``keySequence``, or None."""
        portable = portableKey(keySequence)
        for shortcut in studioShell.shortcuts():
            if portableKey(shortcut.key) == portable:
                return shortcut
        return None

    def _activateShortcut(self, studioShell, keySequence):
        shortcut = self._studioShortcut(studioShell, keySequence)
        self.assertIsNotNone(shortcut, f"no Studio shortcut on {keySequence}")
        # Emitting the signal runs the shortcut (synthetic key events do not go through the shortcut map)
        shortcut.activated()
        self._pump()

    def _focus(self, widget):
        """Give ``widget`` the keyboard focus. Return True if it has it (a headless window may not be active)."""
        self.mainWindow.activateWindow()
        widget.setFocus(qt.Qt.OtherFocusReason)
        self._pump()
        return slicer.app.focusWidget() == widget

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
        # A second key for keyboard layouts that cannot type Ctrl+\: Ctrl+5, the classic View > Module Panel key
        # (the dock's toggle view action, so not a second Studio shortcut on the same key)
        self.assertEqual(portableKey(shell.INSPECTOR_ALTERNATE_SHORTCUT), "Ctrl+5")
        self.assertIn("Ctrl+5", [portableKey(key) for key in dock.toggleViewAction().shortcuts()])
        self.assertNotIn("Ctrl+5", keys)
        # F6 moves between the regions of the window (D-U1)
        self.assertIn("F6", keys)
        self.assertIn("Shift+F6", keys)
        try:
            import StudioTest

            StudioTest.assertNoKeyCollisions()
            self.assertEqual(StudioTest.ambiguousShortcuts(self.mainWindow), {})
        except ImportError:
            pass
        # The app menu shows the key
        appMenu = studioShell.topBar.appMenu
        self.assertIn(studioShell.inspectorShortcutText(), appMenu.inspectorAction.text)

        self.assertFalse(dock.isHidden())
        studioShell.toggleInspector()
        self._pump()
        self.assertTrue(dock.isHidden())
        # A consequence toast names the tab and the edge handle; the keys are in its tooltip
        self.assertTrue(studioShell._stickyToast.isVisible())
        self.assertIn("Draw", studioShell.toastText())
        self.assertIn("handle", studioShell.toastText())
        self.assertIn(studioShell.inspectorShortcutText(alternate=True), studioShell._stickyToast.frame.toolTip)
        self.assertIn(qt.QKeySequence("Ctrl+5").toString(qt.QKeySequence.NativeText), studioShell.inspectorShortcutText(alternate=True))
        # Clicking the tab of the current workspace shows it again
        studioShell.topBar.workspaceButtons["draw"].click()
        self._pump()
        self.assertFalse(dock.isHidden())
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        # The toast is shown each time the user hides the inspector
        studioShell.toast("other", 5000)
        studioShell.toggleInspector()
        self._pump()
        self.assertIn("handle", studioShell.toastText())
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
        """Hide panels (the former focus mode) says how to show them and shows a "Show panels" pill (NAV-05)."""
        studioShell = self._applyStudio()
        studioShell.switchTo("draw")
        self._pump()
        pill = studioShell._focusModePill
        self.assertFalse(pill.isVisible())
        studioShell.toggleFocusMode()
        self._pump()
        self.assertTrue(studioShell.focusMode())
        self.assertTrue(pill.isVisible())
        self.assertEqual(pill.button.text, "Show panels")
        self.assertIn("Esc", studioShell.toastText())
        self.assertIn("Panels hidden", studioShell.toastText())
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
        # The keypad modifier is not a modifier (an Esc key sent through the keypad)
        keypadEscape = qt.QKeyEvent(qt.QEvent.KeyPress, qt.Qt.Key_Escape, qt.Qt.KeypadModifier)
        qt.QApplication.sendEvent(sliceView, keypadEscape)
        self.assertFalse(studioShell.focusMode())

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

        # Text fields with unsent edits keep their Esc (a field over the views stays shown with hidden panels)
        studioShell.setFocusMode(True)
        lineEdit = qt.QLineEdit(self.mainWindow.centralWidget())
        lineEdit.show()
        lineEdit.setText("typed")
        lineEdit.setModified(True)
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

        # A transient module: Esc returns to the workspace it was opened from (rung 5); so does its back button
        slicer.util.selectModule("Data")
        self._pump()
        self.assertIsNone(studioShell.currentWorkspace())
        self._sendEscape(sliceView)
        self._pump()
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        slicer.util.selectModule("Data")
        self._pump()
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
        # A first round trip settles what the saved state holds besides the toolbars and docks: the inspector
        # layout item after a restoreState() (the value differs between a fresh window and a restored one), and
        # the current module (Studio leaves its last module selected), whose width the inspector depends on
        warmUp = self._applyStudio()
        warmUp.classic(persist=False)
        warmUp.dispose()
        self._pump()
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
        """Selecting Segment Editor tools neither moves the views nor clips the options (NFT-06)."""
        from StudioLib import shell

        mainWindow = self.mainWindow
        mainWindow.resize(1600, 1000)
        self._pump()
        settings = qt.QSettings()
        widthsKey = f"{shell._WORKSPACE_PANEL_WIDTHS_GROUP}/SegmentEditor"
        savedWidths = settings.value(widthsKey)
        settings.remove(widthsKey)
        modulePanel = mainWindow.findChild("QWidget", "ModulePanel")
        scrollArea = modulePanel.findChild("QScrollArea", "ScrollArea")
        dock = self._dock()
        classicPolicy = scrollArea.horizontalScrollBarPolicy
        classicMinimumWidth = modulePanel.minimumWidth
        classicContentsMinimumWidth = dock.widget().minimumWidth
        effectNames = ("Paint", "Draw", "Erase", "Threshold", "Islands", "")

        def assertNotClipped(message):
            contents = scrollArea.widget()
            self.assertGreaterEqual(contents.width, contents.layout().minimumSize().width() - 1, message)
            self.assertFalse(scrollArea.horizontalScrollBar().isVisible(), message)

        try:
            studioShell = self._applyStudio()
            volume, segmentation = self._loadScanAndSegmentation()
            studioShell.switchTo("draw")
            self._pump(10)
            editor = slicer.modules.SegmentEditorWidget.editor
            editor.setSegmentationNode(segmentation)
            editor.setSourceVolumeNode(volume)
            self._pump(10)
            # Draw lands on the stored width (Studio/PanelWidth), or wider if the Segment Editor needs it
            self.assertGreaterEqual(dock.width, studioShell.panelWidth - 2)
            assertNotClipped("landing")

            # First use of each tool: the inspector may widen (once) so that nothing is clipped, never shrinks
            previous = dock.width
            for effectName in effectNames:
                editor.setActiveEffectByName(effectName)
                self._pump(10)
                self.assertGreaterEqual(dock.width, previous - 2, effectName)
                assertNotClipped(f"first use of {effectName!r}")
                previous = dock.width
            reference = dock.width
            # The width that the Segment Editor needs is remembered (Studio/*)
            self.assertTrue(str(settings.value(widthsKey, "")).startswith(f"{reference}@"), settings.value(widthsKey))

            # Then choosing a tool does not change the width, also at 1366x768
            for size in ((1600, 1000), (1366, 768)):
                mainWindow.resize(*size)
                self._pump(10)
                for effectName in effectNames:
                    editor.setActiveEffectByName(effectName)
                    self._pump(10)
                    self.assertLessEqual(abs(dock.width - reference), 2, f"{effectName} at {size}")
                    assertNotClipped(f"{effectName!r} at {size}")
            mainWindow.resize(1600, 1000)
            self._pump()
            # The workspaces share the width: switching does not resize the views
            studioShell.switchTo("library")
            self._pump(10)
            if not dock.isHidden():
                self.assertLessEqual(abs(dock.width - reference), 2)
            studioShell.switchTo("draw")
            self._pump(10)
            self.assertLessEqual(abs(dock.width - reference), 2)
            # The user cannot drag the inspector narrower than the tools need
            self.assertGreaterEqual(dock.minimumSizeHint.width(), reference - 2)
            # A transient module may widen the inspector; the workspace gets its width back
            slicer.util.selectModule("Data")
            self._pump(10)
            studioShell.closeTransientModule()
            self._pump(10)
            self.assertLessEqual(abs(dock.width - reference), 2)

            studioShell.classic(persist=False)
            self._pump()
            self.assertEqual(scrollArea.horizontalScrollBarPolicy, classicPolicy)
            self.assertEqual(modulePanel.minimumWidth, classicMinimumWidth)
            self.assertEqual(dock.widget().minimumWidth, classicContentsMinimumWidth)
            for browser in modulePanel.findChildren("ctkFittedTextBrowser"):
                self.assertNotEqual(browser.minimumWidth, 1, browser.objectName)

            # The next session lands on the remembered width at once, whatever tool is armed
            editor.setActiveEffectByName("")
            studioShell.studio()
            self._pump(10)
            self.assertEqual(studioShell.currentWorkspace(), "draw")
            self.assertLessEqual(abs(dock.width - reference), 2)
            studioShell.classic(persist=False)
            self._pump()

            # A width measured with another build or font is measured again
            settings.setValue(widthsKey, f"{shell.MAX_WORKSPACE_PANEL_WIDTH}@another build")
            otherShell = shell.Shell(force=True)
            self.assertEqual(otherShell._workspacePanelWidth("SegmentEditor", 350), 350)
        finally:
            if savedWidths is None:
                settings.remove(widthsKey)
            else:
                settings.setValue(widthsKey, savedWidths)

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
        from StudioLib import ui

        screen = ui.screenOf(mainWindow)
        available = screen.availableGeometry() if callable(screen.availableGeometry) else screen.availableGeometry
        self.assertGreaterEqual(mainWindow.width, int(available.width() * 0.85))
        self.assertGreaterEqual(mainWindow.height, int(available.height() * 0.85))
        # Already large enough: unchanged
        self.assertFalse(shell.fitWindowToScreen(mainWindow))

    #
    # Shell navigation II and keyboard essentials (U-01)
    #

    def _enterTransient(self, studioShell, moduleName="Volumes", fromWorkspace="draw"):
        studioShell.switchTo(fromWorkspace)
        self._pump()
        slicer.util.selectModule(moduleName)
        self._pump()
        self.assertIsNone(studioShell.currentWorkspace())
        self.assertEqual(studioShell.currentModuleName(), moduleName)

    def test_8_escape_leaves_transient_module(self):
        """Esc in a transient module returns to the workspace it was opened from (ladder rung 5)."""
        studioShell = self._applyStudio()
        self._enterTransient(studioShell, "Volumes")
        self.assertTrue(self._pressEscape())
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        self.assertEqual(studioShell.currentModuleName(), "SegmentEditor")
        # Nothing left to back out of in Draw: Esc is not consumed (it goes on to the Segment Editor)
        self.assertFalse(self._pressEscape())
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        # Library is outside the ladder
        studioShell.switchTo("library")
        self._pump()
        self._pressEscape()
        self.assertEqual(studioShell.currentWorkspace(), "library")
        # The ladder is published for the shortcut list
        from StudioLib import shell

        self.assertEqual(len(shell.ESCAPE_LADDER), 6)
        self.assertTrue(all(len(rung) == 2 for rung in shell.ESCAPE_LADDER))

    def test_8b_escape_with_spinbox(self):
        """A spin box does not keep Esc; a line edit with unsent text and an open item editor do (USABILITY 3)."""
        from StudioLib import shell

        studioShell = self._applyStudio()
        self._enterTransient(studioShell, "Volumes")
        modulePanel = self.mainWindow.findChild("QWidget", "ModulePanel")
        # Child widgets of the module panel, as a module would have them
        spinBox = qt.QSpinBox(modulePanel)
        spinBox.show()
        _keepAlive.append(spinBox)
        if not self._focus(spinBox):
            self.skipTest("the main window cannot take the keyboard focus here")
        self.assertTrue(self._pressEscape())
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        self.assertNotEqual(slicer.app.focusWidget(), spinBox)
        # The line edit of a spin box counts as the spin box
        self.assertFalse(shell._keepsEscape(spinBox.findChild("QLineEdit")))
        self.assertTrue(shell._leavesFieldOnEscape(spinBox.findChild("QLineEdit")))
        spinBox.hide()

        # A line edit with unsent edits keeps Esc; an empty or unmodified one does not
        self._enterTransient(studioShell, "Volumes")
        lineEdit = qt.QLineEdit(modulePanel)
        lineEdit.show()
        _keepAlive.append(lineEdit)
        lineEdit.setText("typed")
        lineEdit.setModified(True)
        self.assertTrue(self._focus(lineEdit))
        self.assertFalse(self._pressEscape())
        self.assertIsNone(studioShell.currentWorkspace())
        lineEdit.setModified(False)
        self.assertFalse(shell._keepsEscape(lineEdit))
        lineEdit.hide()

        # An item view with an open editor keeps Esc
        table = qt.QTableWidget(1, 1, modulePanel)
        table.setItem(0, 0, qt.QTableWidgetItem("cell"))
        table.resize(200, 80)
        table.show()
        _keepAlive.append(table)
        self._pump()
        table.setFocus()
        table.editItem(table.item(0, 0))
        self._pump()
        self.assertTrue(shell._keepsEscape(slicer.app.focusWidget()) or shell._keepsEscape(table))
        self.assertFalse(self._pressEscape())
        self.assertIsNone(studioShell.currentWorkspace())
        table.hide()
        # A read-only text browser does not keep Esc (help text), an editable text field does
        browser = qt.QTextBrowser()
        self.assertFalse(shell._keepsEscape(browser))
        self.assertTrue(shell._keepsEscape(qt.QPlainTextEdit()))
        # Esc from a neutral focus (the module panel, if the window can take the focus) leaves the transient module
        self._focus(modulePanel)
        self.assertTrue(self._pressEscape())
        self.assertEqual(studioShell.currentWorkspace(), "draw")

    def test_8c_overlay_rung(self):
        """A registered Studio overlay is closed by the first Esc; the second Esc runs the next rung (rung 0.5)."""
        studioShell = self._applyStudio()
        studioShell.switchTo("draw")
        self._pump()
        centralWidget = self.mainWindow.centralWidget()
        overlays = []
        for index in range(2):
            overlay = qt.QFrame(centralWidget)
            overlay.setObjectName(f"StudioTestOverlay{index}")
            overlay.resize(100, 100)
            overlay.show()
            overlays.append(overlay)
            _keepAlive.append(overlay)
            studioShell.registerEscapeOverlay(overlay, overlay.hide)
        studioShell.setFocusMode(True)
        self._pump()
        # The top-most (last registered) first, one per press
        self.assertTrue(self._pressEscape())
        self.assertFalse(overlays[1].isVisible())
        self.assertTrue(overlays[0].isVisible())
        self.assertTrue(studioShell.focusMode())
        self.assertTrue(self._pressEscape())
        self.assertFalse(overlays[0].isVisible())
        self.assertTrue(studioShell.focusMode())
        # Hidden overlays stay registered but do not take Esc: the next rung runs
        self.assertTrue(self._pressEscape())
        self.assertFalse(studioShell.focusMode())
        for overlay in overlays:
            studioShell.unregisterEscapeOverlay(overlay)
            overlay.setParent(None)
        self.assertEqual(studioShell._escapeOverlays, [])

    def test_9_back_button_labelled(self):
        """A transient module shows a focusable "← Draw" button whose label does not change in a chain."""
        studioShell = self._applyStudio()
        header = studioShell.panelHeader
        studioShell.switchTo("draw")
        self._pump()
        self.assertFalse(header.closeButton.isVisible())
        self.assertTrue(header.captionLabel.isVisible())
        self.assertIn("Segment Editor", header.captionLabel.text)
        self.assertEqual(header.titleLabel.text, "Draw")
        self._enterTransient(studioShell, "Volumes")
        button = header.closeButton
        self.assertEqual(button.objectName, "StudioPanelCloseButton")
        self.assertTrue(button.isVisible())
        self.assertEqual(button.text, "Draw")
        self.assertEqual(button.focusPolicy, qt.Qt.TabFocus)
        self.assertEqual(button.toolButtonStyle, qt.Qt.ToolButtonTextBesideIcon)
        self.assertIn("Back to Draw", button.toolTip)
        self.assertIn("Esc", button.toolTip)
        self.assertIn("Close Volumes and return", button.toolTip)
        self.assertFalse(header.prefixLabel.isVisible())
        self.assertFalse(header.captionLabel.isVisible())
        # Left of the title
        self.assertLess(button.x, header.titleLabel.x)
        # Draw -> Volumes -> Data (Search): still "← Draw"
        slicer.util.selectModule("Data")
        self._pump()
        self.assertEqual(button.text, "Draw")
        self.assertEqual(studioShell.transientOrigin(), "draw")
        button.click()
        self._pump()
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        self.assertIsNone(studioShell.transientOrigin())
        # From Library: "← Library"
        self._enterTransient(studioShell, "Volumes", fromWorkspace="library")
        self.assertEqual(button.text, "Library")
        self._pressEscape()
        self.assertEqual(studioShell.currentWorkspace(), "library")
        # The focus starts on the module panel container of a transient module
        self._enterTransient(studioShell, "Volumes")
        self._pump()
        if slicer.app.activeWindow() == self.mainWindow:
            self.assertEqual(slicer.app.focusWidget(), self.mainWindow.findChild("QWidget", "ModulePanel"))

    def test_10_ctrl_h_ctrl_f(self):
        """Ctrl+F opens Search for modules and Ctrl+H shows Library (not on macOS); Classic gets its keys back."""
        import StudioTest

        mainWindow = self.mainWindow
        names = ("FileAddDataAction", "ViewFindModuleAction", "ModuleHomeAction")

        def classicKeys():
            return {name: [portableKey(key) for key in mainWindow.findChild("QAction", name).shortcuts()] for name in names}

        before = classicKeys()
        self.assertEqual(before["ViewFindModuleAction"], ["Ctrl+F"])
        self.assertEqual(before["ModuleHomeAction"], ["Ctrl+H"])
        studioShell = self._applyStudio()
        studioShell.switchTo("draw")
        self._pump()
        self.assertEqual(mainWindow.findChild("QAction", "ViewFindModuleAction").shortcuts(), ())
        self.assertEqual(len(mainWindow.findChild("QAction", "ModuleHomeAction").shortcuts()), 0)
        self.assertIsNotNone(self._studioShortcut(studioShell, "Ctrl+F"))
        self.assertIsNotNone(self._studioShortcut(studioShell, "Ctrl+H"))
        self.assertEqual(StudioTest.ambiguousShortcuts(mainWindow), {})
        StudioTest.assertNoKeyCollisions()
        calls = []
        studioShell.openPalette = lambda category=None: calls.append(category)
        self._activateShortcut(studioShell, "Ctrl+F")
        self.assertEqual(calls, ["modules"])
        del studioShell.openPalette
        self._activateShortcut(studioShell, "Ctrl+H")
        self.assertEqual(studioShell.currentWorkspace(), "library")
        studioShell.classic(persist=False)
        self._pump()
        self.assertEqual(classicKeys(), before)

        # macOS: Ctrl+H (Cmd+H hides the application) is neither bound nor taken from the classic action
        platform = sys.platform
        try:
            sys.platform = "darwin"
            studioShell.apply()
            self._pump()
            self.assertIsNone(self._studioShortcut(studioShell, "Ctrl+H"))
            self.assertIsNotNone(self._studioShortcut(studioShell, "Ctrl+F"))
            self.assertIsNone(self._studioShortcut(studioShell, "Alt+F"))
            self.assertEqual([portableKey(key) for key in mainWindow.findChild("QAction", "ModuleHomeAction").shortcuts()], ["Ctrl+H"])
            studioShell.classic(persist=False)
            self._pump()
        finally:
            sys.platform = platform
        self.assertEqual(classicKeys(), before)

    def test_11_inspector_notice(self):
        """The notice slot sits above the module, shows its notice in its workspace only and never widens the inspector."""
        from StudioLib import shell, ui

        settings = qt.QSettings()
        savedWidth = settings.value("Studio/PanelWidth")
        try:
            studioShell = self._applyStudio()
            studioShell.switchTo("draw")
            self._pump()
            dock = self._dock()
            slot = self.mainWindow.findChild("QWidget", "StudioInspectorNotice")
            self.assertIsNotNone(slot)
            layout = dock.widget().layout()
            self.assertEqual(layout.indexOf(slot) + 1, layout.indexOf(self.mainWindow.findChild("QWidget", "ModulePanel")))
            self.assertFalse(slot.isVisible())

            label = qt.QLabel("Download the 1 mm template?")
            label.setWordWrap(True)
            _keepAlive.append(label)
            studioShell.setInspectorNotice(label, key="test", workspace="draw")
            self._pump()
            self.assertTrue(slot.isVisible())
            self.assertTrue(label.isVisible())
            self.assertEqual(studioShell.inspectorNotice(), label)
            # Shown only while Draw is current, and kept registered
            studioShell.switchTo("library")
            self._pump()
            self.assertFalse(label.isVisible())
            self.assertEqual(studioShell.inspectorNotice(), label)
            studioShell.switchTo("draw")
            self._pump()
            self.assertTrue(label.isVisible())
            slicer.util.selectModule("Volumes")
            self._pump()
            self.assertFalse(label.isVisible())
            studioShell.closeTransientModule()
            self._pump()
            self.assertTrue(label.isVisible())
            # Another key's clear does nothing; a new notice replaces the old one, which goes back to its owner
            self.assertFalse(studioShell.clearInspectorNotice("other"))
            studioShell.panelWidth = shell.MIN_PANEL_WIDTH
            self._pump(10)
            dockWidth = dock.width
            wide = qt.QLabel("W" * 60)
            _keepAlive.append(wide)
            self.assertGreater(wide.sizeHint.width(), 500)
            studioShell.setInspectorNotice(wide, key="wide")
            self._pump(10)
            self.assertFalse(label.isVisible())
            self.assertIsNone(label.parentWidget())
            self.assertLessEqual(abs(dock.width - dockWidth), 1)
            for child in [wide, *slot.findChildren("QWidget")]:
                geometry = child.geometry
                self.assertGreaterEqual(geometry.left(), 0)
                self.assertLessEqual(geometry.right(), slot.width, child.objectName)
            # A notice line: buttons under the text below 300 px, inside the slot
            line = ui.noticeLine("A long offer text that has to wrap in a narrow inspector", ui.noticeActions(notNow=lambda: None, details=lambda: None))
            _keepAlive.append(line)
            studioShell.setInspectorNotice(line, key="line", workspace="draw")
            self._pump(10)
            self.assertLessEqual(abs(dock.width - dockWidth), 1)
            buttons = line.findChildren("QToolButton", "StudioNoticeButton")
            self.assertEqual(len(buttons), 2)
            for button in buttons:
                topRight = button.mapTo(slot, qt.QPoint(button.width, 0))
                self.assertLessEqual(topRight.x(), slot.width + 1)
            # Narrower than 300 px, the buttons go under the text
            narrow = ui.noticeLine("Another offer", ui.noticeActions(notNow=lambda: None))
            _keepAlive.append(narrow)
            narrow.show()
            narrow.resize(240, 60)
            self._pump()
            self.assertEqual(narrow.layout().direction(), qt.QBoxLayout.TopToBottom)
            narrow.resize(400, 40)
            self._pump()
            self.assertEqual(narrow.layout().direction(), qt.QBoxLayout.LeftToRight)
            narrow.hide()
            self.assertTrue(studioShell.clearInspectorNotice("line"))
            self.assertIsNone(studioShell.inspectorNotice())
            self.assertFalse(slot.isVisible())
            # Classic removes the slot
            studioShell.setInspectorNotice(label, key="test")
            studioShell.classic(persist=False)
            self._pump()
            self.assertIsNone(self.mainWindow.findChild("QWidget", "StudioInspectorNotice"))
        finally:
            if savedWidth is None:
                settings.remove("Studio/PanelWidth")
            else:
                settings.setValue("Studio/PanelWidth", savedWidth)

    def test_12_help_and_menu_hooks(self):
        """'?' shows the help of the current tool from the hook and links the module help; '⋯' shows hook items."""
        from StudioLib import shell, workspaces

        studioShell = self._applyStudio()
        studioShell.switchTo("draw")
        self._pump()
        header = studioShell.panelHeader
        # Draw's own hook (W2-05): the help of the current tool, then a link to the module documentation
        drawHelp = header.helpHtml()
        self.assertIn(shell._MODULE_HELP_LINK, drawHelp)
        self.assertIn("Module documentation", drawHelp)
        triggered = []

        class NoHelpHooks:
            pass

        # Without a help hook the popover shows the module help
        originalHooks = workspaces.hooks
        workspaces.hooks = lambda ws: NoHelpHooks if ws is not None and ws.key == "draw" else originalHooks(ws)
        try:
            moduleHelp = header.helpHtml()
        finally:
            workspaces.hooks = originalHooks
        self.assertIn("Segment Editor", moduleHelp)
        self.assertNotIn(shell._MODULE_HELP_LINK, moduleHelp)

        class FakeHooks:
            @staticmethod
            def helpHtml(shell):
                return ("Brush", "<p>Paint with a round brush (B)</p>")

            @staticmethod
            def moreMenuActions(shell):
                return [("Show all controls", lambda: triggered.append(True), True), ("Plain item", lambda: None, None)]

        originalHooks = workspaces.hooks
        workspaces.hooks = lambda ws: FakeHooks if ws is not None and ws.key == "draw" else originalHooks(ws)
        try:
            html = header.helpHtml()
            self.assertIn("Brush", html)
            self.assertIn("Module documentation", html)
            menu = header.helpPopover()
            browser = menu.findChild("QTextBrowser", "StudioHelpBrowser")
            self.assertFalse(browser.openLinks)
            self.assertIn("Paint with a round brush", browser.toPlainText())
            browser.anchorClicked(qt.QUrl(shell._MODULE_HELP_LINK))
            self.assertIn("Segment Editor", browser.toPlainText())
            menu.deleteLater()
            header.moreMenu.aboutToShow()
            texts = [action.text for action in header.moreMenu.actions()]
            self.assertEqual(texts[0], "Show all controls")
            self.assertEqual(texts[1], "Plain item")
            self.assertTrue(header.moreMenu.actions()[0].checked)
            self.assertFalse(header.moreMenu.actions()[1].checkable)
            self.assertIn("Show Data Probe", texts)
            header.moreMenu.actions()[0].trigger()
            self.assertEqual(triggered, [True])
            # Rebuilt each time (not duplicated)
            header.moreMenu.aboutToShow()
            self.assertEqual([action.text for action in header.moreMenu.actions()].count("Show all controls"), 1)
        finally:
            workspaces.hooks = originalHooks
        header.moreMenu.aboutToShow()
        texts = [action.text for action in header.moreMenu.actions()]
        self.assertNotIn("Plain item", texts)
        self.assertEqual(texts.count("Show all controls"), 1)  # Draw's own item (W2-05)
        self.assertEqual(header.helpHtml(), drawHelp)
        # The header buttons are reachable with Tab
        for button in (header.helpButton, header.moreButton, header.closeButton):
            self.assertEqual(button.focusPolicy, qt.Qt.TabFocus)

    def test_13_scene_root_directory(self):
        """The Save dialog (Ctrl+S) never opens in the application folder: Studio sets the scene root, Classic restores it."""
        import tempfile

        from StudioLib import shell

        settings = qt.QSettings()
        savedFolder = settings.value("Studio/LastSaveFolder")
        originalRoot = slicer.mrmlScene.GetRootDirectory()
        # In the home folder: the temporary folder counts as an application folder
        folder = tempfile.mkdtemp(prefix="StudioNavigationTest-", dir=qt.QDir.homePath())
        if shell.Shell._isApplicationDirectory(folder):
            os.rmdir(folder)
            self.skipTest("the home folder is in the temporary folder, which Studio never proposes for saving")
        try:
            settings.setValue("Studio/LastSaveFolder", folder)
            # Applied once, so that the keys that applying Studio always writes exist
            studioShell = self._applyStudio()
            studioShell.classic(persist=False)
            self._pump()
            slicer.mrmlScene.SetRootDirectory(slicer.app.slicerHome)
            keysBefore = sorted(settings.allKeys())
            studioShell.apply()
            self._pump()
            self.assertEqual(os.path.realpath(slicer.mrmlScene.GetRootDirectory()), os.path.realpath(folder))
            # No settings key is written for it
            self.assertEqual(sorted(settings.allKeys()), keysBefore)
            studioShell.classic(persist=False)
            self._pump()
            self.assertEqual(slicer.mrmlScene.GetRootDirectory(), slicer.app.slicerHome)
            # A missing root and no last folder: the home folder
            settings.remove("Studio/LastSaveFolder")
            missing = os.path.join(folder, "missing")
            slicer.mrmlScene.SetRootDirectory(missing)
            studioShell.apply()
            self._pump()
            self.assertEqual(os.path.realpath(slicer.mrmlScene.GetRootDirectory()), os.path.realpath(qt.QDir.homePath()))
            # A user folder is kept, and checked again when the scene is closed
            slicer.mrmlScene.SetRootDirectory(folder)
            slicer.mrmlScene.Clear(0)
            self.assertEqual(slicer.mrmlScene.GetRootDirectory(), folder)
            slicer.mrmlScene.SetRootDirectory(slicer.app.slicerHome)
            slicer.mrmlScene.Clear(0)
            self.assertEqual(os.path.realpath(slicer.mrmlScene.GetRootDirectory()), os.path.realpath(qt.QDir.homePath()))
            studioShell.classic(persist=False)
            self._pump()
            # The root that the user (here the test) set is not the one Studio set: it is kept
            slicer.mrmlScene.SetRootDirectory(missing)
            studioShell.apply()
            self._pump()
            slicer.mrmlScene.SetRootDirectory(folder)
            studioShell.classic(persist=False)
            self._pump()
            self.assertEqual(slicer.mrmlScene.GetRootDirectory(), folder)
        finally:
            slicer.mrmlScene.SetRootDirectory(originalRoot)
            if savedFolder is None:
                settings.remove("Studio/LastSaveFolder")
            else:
                settings.setValue("Studio/LastSaveFolder", savedFolder)
            os.rmdir(folder)

    def test_14_contrast_tokens(self):
        """WCAG ratios of the tokens in both themes, and ui.token() equals the style's token (USABILITY 9.3)."""
        from StudioLib import ui

        style = slicer.app.style()
        originalPalette = qt.QPalette(slicer.app.palette())
        names = ("text3", "text-disabled", "control-border", "accent-fg", "card", "canvas", "text2", "border", "hairline")
        try:
            for theme, window in (("dark", "#1c1c1f"), ("light", "#f4f4f6")):
                slicer.app.setPalette(qt.QPalette(qt.QColor(window)))
                self.assertEqual(ui.isDark(), theme == "dark")
                windowColor = ui.token("window")
                for background in (windowColor, ui.token("card")):
                    self.assertGreaterEqual(ui.contrastRatio(ui.token("text3"), background), 4.5, f"text3 on {background.name()} ({theme})")
                self.assertGreaterEqual(ui.contrastRatio(ui.token("control-border"), windowColor), 3.0, f"control-border ({theme})")
                if theme == "light":
                    self.assertGreaterEqual(ui.contrastRatio(ui.token("accent-fg"), windowColor), 4.5)
                if hasattr(style, "token"):
                    for name in names:
                        styleColor = style.token(name)
                        if styleColor.isValid():
                            self.assertEqual(ui.token(name).name(), styleColor.name(), f"{name} ({theme})")
                # The fallback table matches the style
                table = ui._DARK_TOKENS if theme == "dark" else ui._LIGHT_TOKENS
                if hasattr(style, "token"):
                    for name, value in table.items():
                        if name.startswith("accent") and style.token(name).name() != value and slicer.util.settingsValue("Appearance/AccentColor", ""):
                            continue
                        self.assertEqual(style.token(name).name(), value, f"fallback {name} ({theme})")
        finally:
            slicer.app.setPalette(originalPalette)
            self._pump()
        self.assertAlmostEqual(ui.contrastRatio("#000000", "#ffffff"), 21.0, places=3)
        self.assertAlmostEqual(ui.contrastRatio(qt.QColor("#777777"), "#777777"), 1.0, places=6)
        self.assertEqual(ui.tooltip("Back to Draw", "Esc", "Close Volumes and return"), "Back to Draw · Esc · Close Volumes and return")
        self.assertEqual(ui.tooltip("Name"), "Name")
        self.assertIn("&lt;", ui.tooltip("Smaller", "Ctrl+<"))
        screen = ui.primaryScreen()
        self.assertIsNotNone(screen)
        self.assertGreater(screen.geometry.width(), 0)
        self.assertGreaterEqual(ui.fontPx(13), 1)

    def test_15_navigation_behaviours(self):
        """Studio turns on W3-02a's navigation behaviours (statics and focus follows mouse); Classic turns them off."""
        sliceWidget = slicer.vtkMRMLSliceIntersectionWidget
        windowLevelWidget = slicer.vtkMRMLWindowLevelWidget
        if not hasattr(sliceWidget, "SetPageStepSlices") or not hasattr(windowLevelWidget, "SetLeftDragWindowLevel"):
            self.skipTest("this build has no Studio navigation behaviours (W3-02a)")
        self.assertEqual(sliceWidget.GetPageStepSlices(), 0)
        self.assertFalse(slicer.app.property("SlicerFocusFollowsMouse"))
        studioShell = self._applyStudio()
        studioShell.switchTo("draw")
        self._pump()
        self.assertEqual(sliceWidget.GetPageStepSlices(), 10)
        self.assertTrue(sliceWidget.GetClampSliceStepping())
        self.assertTrue(sliceWidget.GetSymmetricZoom())
        self.assertTrue(windowLevelWidget.GetLeftDragWindowLevel())
        self.assertTrue(slicer.app.property("SlicerFocusFollowsMouse"))
        sliceView = self._redSliceView()
        vtkWidget = sliceView.VTKWidget()

        def hoverGivesFocus():
            self.mainWindow.activateWindow()
            self.mainWindow.findChild("QWidget", "ModulePanel").setFocus()
            self._pump()
            if slicer.app.activeWindow() != self.mainWindow:
                return None
            # A QEnterEvent: Qt 6 widgets read the position of an Enter event
            point = qt.QPointF(10, 10)
            enter = qt.QEnterEvent(point, point, qt.QPointF(sliceView.mapToGlobal(qt.QPoint(10, 10)))) if hasattr(qt, "QEnterEvent") else qt.QEvent(qt.QEvent.Enter)
            qt.QApplication.sendEvent(sliceView, enter)
            self._pump()
            return slicer.app.focusWidget() == vtkWidget

        focused = hoverGivesFocus()
        if focused is not None:
            self.assertTrue(focused, "hovering the view does not give it the keyboard focus in Studio")
        studioShell.classic(persist=False)
        self._pump()
        self.assertEqual(sliceWidget.GetPageStepSlices(), 0)
        self.assertFalse(sliceWidget.GetClampSliceStepping())
        self.assertFalse(sliceWidget.GetSymmetricZoom())
        self.assertFalse(windowLevelWidget.GetLeftDragWindowLevel())
        self.assertFalse(slicer.app.property("SlicerFocusFollowsMouse"))
        focused = hoverGivesFocus()
        if focused is not None:
            self.assertFalse(focused, "hovering a view gives it the keyboard focus in Classic")

    def test_16_error_log_bottom(self):
        """In Studio the error log opens at the bottom; Classic restores where it was."""
        mainWindow = self.mainWindow
        dock = mainWindow.errorLogDockWidget()
        classic = (dock.isHidden(), dock.floating, mainWindow.dockWidgetArea(dock))
        studioShell = self._applyStudio()
        studioShell.switchTo("draw")
        self._pump()
        dock.show()
        self._pump(8)
        self.assertFalse(dock.isHidden())
        self.assertFalse(dock.floating)
        self.assertEqual(mainWindow.dockWidgetArea(dock), qt.Qt.BottomDockWidgetArea)
        studioShell.classic(persist=False)
        self._pump()
        self.assertEqual((dock.isHidden(), dock.floating, mainWindow.dockWidgetArea(dock)), classic)

    def test_17_names_and_platform_keys(self):
        """'Hide panels' replaces 'focus mode' (⌘6 on macOS); Search is named Search; Classic texts come back."""
        from StudioLib import shell

        mainWindow = self.mainWindow
        maximizeView = mainWindow.findChild("QAction", "WindowMaximizeViewAction")
        classicTexts = (maximizeView.text, maximizeView.toolTip)
        statusBarAction = mainWindow.findChild("QAction", "ShowStatusBarAction")
        statusBarEnabled = statusBarAction.enabled
        dicomAction = slicer.modules.dicom.action() if hasattr(slicer.modules, "dicom") else None
        dicomText = dicomAction.text if dicomAction is not None else None
        studioShell = self._applyStudio()
        if dicomAction is not None and "&" not in dicomText:
            # ≡ > File > Add DICOM Data is reachable with F10, F, D
            self.assertEqual(portableKey(qt.QKeySequence.mnemonic(dicomAction.text)), "Alt+D")
        studioShell.installClassicSwitch()
        switch = mainWindow.findChild("QAction", "StudioInterfaceAction")
        self.assertEqual(switch.text, "Studio interface")
        self.assertTrue(switch.checked)
        self.assertEqual(maximizeView.text, "Hide panels")
        appMenu = studioShell.topBar.appMenu
        self.assertTrue(appMenu.focusModeAction.text.startswith("Hide panels"))
        search = mainWindow.findChild("QAction", "StudioAllToolsAction")
        self.assertTrue(search.text.startswith("Search…"))
        self.assertIn(qt.QKeySequence("Ctrl+K").toString(qt.QKeySequence.NativeText), search.text)
        self.assertEqual(studioShell._focusModePill.button.text, "Show panels")
        self.assertEqual(shell.focusModeKeyText(), "F11")
        platform = sys.platform
        try:
            sys.platform = "darwin"
            self.assertEqual(shell.focusModeKeyText(), qt.QKeySequence("Ctrl+6").toString(qt.QKeySequence.NativeText))
        finally:
            sys.platform = platform
        # View menu in Studio: the status bar cannot be shown, Module Panel follows the inspector
        viewMenu = mainWindow.findChild("QMenu", "ViewMenu")
        dock = self._dock()
        studioShell.switchTo("draw")
        studioShell.toggleInspector()
        self._pump()
        viewMenu.aboutToShow()
        self.assertFalse(statusBarAction.enabled)
        self.assertFalse(dock.toggleViewAction().checked)
        studioShell.setInspectorVisible(True)
        self.assertIn(mainWindow.findChild("QAction", "StudioRecentMessagesAction"), viewMenu.actions())
        studioShell.classic(persist=False)
        self._pump()
        self.assertEqual((maximizeView.text, maximizeView.toolTip), classicTexts)
        if dicomAction is not None:
            self.assertEqual(dicomAction.text, dicomText)
        self.assertEqual(statusBarAction.enabled, statusBarEnabled)
        self.assertEqual(switch.text, "Switch to Studio interface")
        self.assertFalse(switch.checked)
        self.assertNotIn("Recent messages…", [action.text for action in viewMenu.actions()])

    def test_18_f10_and_alt_menus(self):
        """F10 opens ≡; Alt+F opens ≡ > File and a bare Alt opens ≡ (Windows and Linux)."""
        studioShell = self._applyStudio()
        studioShell.switchTo("draw")
        self._pump()
        menu = studioShell.topBar.appMenu.menu
        fileMenu = self.mainWindow.findChild("QMenu", "FileMenu")

        def closeMenus():
            for popup in (fileMenu, menu):
                popup.close()
            self._pump()

        self._activateShortcut(studioShell, "F10")
        self.assertTrue(menu.isVisible())
        closeMenus()
        self.assertFalse(menu.isVisible())
        if sys.platform != "darwin":
            self._activateShortcut(studioShell, "Alt+F")
            self._pump(6)
            self.assertTrue(menu.isVisible())
            self.assertTrue(fileMenu.isVisible())
            self.assertEqual(menu.activeAction(), fileMenu.menuAction())
            closeMenus()
            for keySequence in ("Alt+E", "Alt+H"):
                self.assertIsNotNone(self._studioShortcut(studioShell, keySequence))
            # Not Alt+V (the slice views use V with any modifier): Alt, then V (the View mnemonic of ≡)
            self.assertIsNone(self._studioShortcut(studioShell, "Alt+V"))
            # A bare Alt press and release (not Alt+key)
            self.mainWindow.activateWindow()
            self._pump()
            if slicer.app.activeWindow() == self.mainWindow:
                self._sendKey(self.mainWindow, qt.Qt.Key_Alt, qt.Qt.AltModifier, qt.QEvent.ShortcutOverride)
                self._sendKey(self.mainWindow, qt.Qt.Key_Alt, qt.Qt.NoModifier, qt.QEvent.KeyRelease)
                self._pump(6)
                self.assertTrue(menu.isVisible(), f"focus: {slicer.app.focusWidget()}")
                closeMenus()
                # Alt then another key: no menu
                self._sendKey(self.mainWindow, qt.Qt.Key_Alt, qt.Qt.AltModifier, qt.QEvent.ShortcutOverride)
                self._sendKey(self.mainWindow, qt.Qt.Key_X, qt.Qt.AltModifier, qt.QEvent.ShortcutOverride)
                self._sendKey(self.mainWindow, qt.Qt.Key_Alt, qt.Qt.NoModifier, qt.QEvent.KeyRelease)
                self._pump(6)
                self.assertFalse(menu.isVisible())
                # Ctrl held, then Alt (also AltGr on Windows, which arrives as Ctrl+Alt): no menu
                self._sendKey(self.mainWindow, qt.Qt.Key_Alt, qt.Qt.AltModifier | qt.Qt.ControlModifier, qt.QEvent.ShortcutOverride)
                self._sendKey(self.mainWindow, qt.Qt.Key_Alt, qt.Qt.ControlModifier, qt.QEvent.KeyRelease)
                self._pump(6)
                self.assertFalse(menu.isVisible())
        # Every shortcut has a description (for the shortcut list)
        for shortcut in studioShell.shortcuts():
            self.assertTrue(shortcut.property("studioDescription"), portableKey(shortcut.key))

    def test_19_f6_regions(self):
        """F6 moves the keyboard focus from the top bar to the inspector to the views, Shift+F6 back."""
        studioShell = self._applyStudio()
        studioShell.switchTo("draw")
        self._pump()
        if not self._focus(studioShell.topBar.menuButton):
            self.skipTest("the main window cannot take the keyboard focus here")
        self.assertEqual(studioShell.focusRegion(), "topBar")
        self._activateShortcut(studioShell, "F6")
        self.assertEqual(studioShell.focusRegion(), "inspector")
        self._activateShortcut(studioShell, "F6")
        self.assertEqual(studioShell.focusRegion(), "view")
        sliceViewNames = list(self.layoutManager.sliceViewNames())
        self.assertTrue(any(self.layoutManager.sliceWidget(name).sliceView().VTKWidget() == slicer.app.focusWidget() for name in sliceViewNames))
        self._activateShortcut(studioShell, "F6")
        self.assertEqual(studioShell.focusRegion(), "topBar")
        self._activateShortcut(studioShell, "Shift+F6")
        self.assertEqual(studioShell.focusRegion(), "view")
        # In a transient module the inspector region starts on the back button
        self._enterTransient(studioShell, "Volumes")
        self._focus(studioShell.topBar.menuButton)
        self._activateShortcut(studioShell, "F6")
        self.assertEqual(slicer.app.focusWidget(), studioShell.panelHeader.closeButton)
        # Library: the canvas replaces the views
        studioShell.switchTo("library")
        self._pump()
        self._focus(studioShell.topBar.menuButton)
        regions = [name for name, container, focus in studioShell._focusRegions()]
        self.assertIn("canvas", regions)
        self.assertNotIn("view", regions)

    def test_20_button_keys_in_draw(self):
        """Space and Enter press a focused top bar button also in Draw (before the Segment Editor's Space shortcut)."""
        studioShell = self._applyStudio()
        studioShell.switchTo("draw")
        self._pump()
        libraryButton = studioShell.topBar.workspaceButtons["library"]
        self.assertTrue(int(libraryButton.focusPolicy) & int(qt.Qt.TabFocus))
        if not self._focus(studioShell.topBar.menuButton):
            self.skipTest("the main window cannot take the keyboard focus here")
        # Tab reaches the Library tab
        for _step in range(40):
            if slicer.app.focusWidget() == libraryButton:
                break
            self._sendKey(slicer.app.focusWidget(), qt.Qt.Key_Tab)
            self._pump(1)
        self.assertEqual(slicer.app.focusWidget(), libraryButton)
        for key in (qt.Qt.Key_Space, qt.Qt.Key_Return, qt.Qt.Key_Enter):
            studioShell.switchTo("draw")
            self._pump()
            self.assertTrue(self._focus(libraryButton))
            override = self._sendKey(libraryButton, key, eventType=qt.QEvent.ShortcutOverride)
            self.assertTrue(override.isAccepted(), f"the shortcut override of {key} is not taken by the button")
            self._sendKey(libraryButton, key, text=" " if key == qt.Qt.Key_Space else "\r")
            self._sendKey(libraryButton, key, eventType=qt.QEvent.KeyRelease, text=" " if key == qt.Qt.Key_Space else "\r")
            self._pump()
            self.assertEqual(studioShell.currentWorkspace(), "library", f"key {key}")
        # Header buttons too
        studioShell.switchTo("draw")
        self._enterTransient(studioShell, "Volumes")
        backButton = studioShell.panelHeader.closeButton
        self.assertTrue(self._focus(backButton))
        self._sendKey(backButton, qt.Qt.Key_Return, text="\r")
        self._pump()
        self.assertEqual(studioShell.currentWorkspace(), "draw")

    def test_21_no_invisible_tab_stop(self):
        """Over the Library canvas, Tab never lands on a covered view widget; the policies come back exactly."""
        studioShell = self._applyStudio()
        viewport = self.layoutManager.viewport()
        studioShell.switchTo("draw")
        self._pump()
        policies = {id(widget): (widget, widget.focusPolicy) for widget in viewport.findChildren("QWidget")}
        studioShell.switchTo("library")
        self._pump()
        canvas = studioShell.canvas()
        self.assertIsNotNone(canvas)
        self.assertTrue(canvas.isVisible())
        # Hidden widgets (view bar popups, views of other layouts) are no Tab stops: the visible ones are covered
        tabStops = [widget for widget in viewport.findChildren("QWidget") if int(widget.focusPolicy) & int(qt.Qt.TabFocus) and widget.isVisible()]
        self.assertEqual(tabStops, [])
        # A layout chosen while the canvas is shown (Alt+1..5 in Library): its views are covered too
        self.layoutManager.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
        self._pump(6)
        tabStops = [widget for widget in viewport.findChildren("QWidget") if int(widget.focusPolicy) & int(qt.Qt.TabFocus) and widget.isVisible()]
        self.assertEqual(tabStops, [])
        start = studioShell._firstTabStop(canvas) or studioShell.topBar.menuButton
        if not self._focus(start):
            self.skipTest("the main window cannot take the keyboard focus here")
        for _step in range(30):
            widget = slicer.app.focusWidget()
            self._sendKey(widget, qt.Qt.Key_Tab)
            self._pump(1)
            widget = slicer.app.focusWidget()
            if widget is None:
                continue
            self.assertTrue(widget.isVisible(), widget.className())
            self.assertFalse(viewport.isAncestorOf(widget), f"Tab reached the covered {widget.className()} {widget.objectName}")
        def assertRestored():
            for widget, policy in policies.values():
                try:
                    current = widget.focusPolicy
                except ValueError:
                    # A view of the layout that was replaced
                    continue
                self.assertEqual(current, policy, widget.className())

        # Back in Draw (and in Classic) every policy is restored
        studioShell.switchTo("draw")
        self._pump()
        assertRestored()
        policies.update({id(widget): (widget, widget.focusPolicy) for widget in viewport.findChildren("QWidget")})
        studioShell.switchTo("library")
        self._pump()
        studioShell.classic(persist=False)
        self._pump()
        assertRestored()

    def test_21b_tab_stops_after_classic_and_hidden_window(self):
        """The views under the canvas are covered after a layout change in Classic and when Studio was applied while
        the main window was hidden (the Tab stops found per layout must not go stale).
        """
        from StudioLib import shell

        viewport = self.layoutManager.viewport()

        def visibleTabStops():
            return [f"{widget.className()}:{widget.objectName}" for widget in viewport.findChildren("QWidget")
                    if int(widget.focusPolicy) & int(qt.Qt.TabFocus) and widget.isVisible()]

        # A layout chosen in Classic (where Studio does not follow layout changes), then Library again
        studioShell = self._applyStudio()
        studioShell.switchTo("library")
        self._pump()
        self.assertEqual(visibleTabStops(), [])
        studioShell.switchTo("draw")
        self._pump()
        studioShell.classic(persist=False)
        self._pump()
        newLayout = (slicer.vtkMRMLLayoutNode.SlicerLayoutThreeOverThreeView
                     if self.layoutManager.layout != slicer.vtkMRMLLayoutNode.SlicerLayoutThreeOverThreeView
                     else slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
        self.layoutManager.setLayout(newLayout)
        self._pump(6)
        studioShell.apply()
        self._pump()
        studioShell.switchTo("library")
        self._pump(6)
        self.assertEqual(visibleTabStops(), [])
        studioShell.dispose()
        self._pump()

        # Studio applied and Library shown while the main window is hidden (before the first show)
        self.mainWindow.hide()
        self._pump()
        try:
            studioShell = shell.Shell(force=True)
            studioShell.apply()
            studioShell.switchTo("library")
            self._pump()
        finally:
            self.mainWindow.show()
            self._pump(6)
        self.assertIsNotNone(studioShell.canvas())
        self.assertTrue(studioShell.canvas().isVisible())
        self.assertEqual(visibleTabStops(), [])
        # Back in Draw every view widget accepts Tab again
        studioShell.switchTo("draw")
        self._pump()
        self.assertNotEqual(visibleTabStops(), [])

    def test_22_inspector_handle(self):
        r"""A hidden inspector has an edge handle; one click shows it; Ctrl+5 also toggles it (USABILITY 6.3)."""
        studioShell = self._applyStudio()
        studioShell.switchTo("draw")
        self._pump()
        dock = self._dock()
        handle = studioShell.inspectorHandle()
        self.assertIsNotNone(handle)
        self.assertEqual(handle.objectName, "StudioInspectorHandle")
        self.assertFalse(handle.isVisible())
        self._activateShortcut(studioShell, "Ctrl+\\")
        self.assertTrue(dock.isHidden())
        self.assertTrue(handle.isVisible())
        centralWidget = self.mainWindow.centralWidget()
        self.assertEqual(handle.geometry.right(), centralWidget.width - 1)
        self.assertEqual((handle.width, handle.height), (20, 56))

        def assertPlaced():
            """Vertically centred, unless that covers the view bar of a view on the right (its offset text)."""
            geometry = handle.geometry
            self.assertGreaterEqual(geometry.top(), 0)
            self.assertLessEqual(geometry.bottom(), centralWidget.height - 1)
            bands = []
            for bar in self.layoutManager.viewport().findChildren("qMRMLViewControllerBar"):
                if bar.isVisible():
                    topLeft = bar.mapTo(centralWidget, qt.QPoint(0, 0))
                    if topLeft.x() + bar.width > centralWidget.width - handle.width:
                        bands.append((topLeft.y(), topLeft.y() + bar.height))
            for top, bottom in bands:
                self.assertFalse(geometry.top() < bottom and geometry.top() + geometry.height() > top,
                                 f"the handle {geometry.top()}..{geometry.bottom()} covers a view bar {top}..{bottom}")
            centreTop = (centralWidget.height - handle.height) // 2
            if not any(centreTop < bottom and centreTop + handle.height > top for top, bottom in bands):
                self.assertLessEqual(abs(geometry.center().y() - centralWidget.height // 2), 2)

        assertPlaced()
        for layout in (slicer.vtkMRMLLayoutNode.SlicerLayoutConventionalView, slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView,
                       slicer.vtkMRMLLayoutNode.SlicerLayoutOneUpRedSliceView):
            self.layoutManager.setLayout(layout)
            self._pump(6)
            self.assertTrue(handle.isVisible())
            assertPlaced()
        self.assertEqual(handle.focusPolicy, qt.Qt.TabFocus)
        self.assertIn("Ctrl+5", handle.toolTip.replace(qt.QKeySequence("Ctrl+5").toString(qt.QKeySequence.NativeText), "Ctrl+5"))
        handle.click()
        self._pump()
        self.assertFalse(dock.isHidden())
        self.assertFalse(handle.isVisible())
        # Ctrl+5 (View > Module Panel)
        dock.toggleViewAction().trigger()
        self._pump()
        self.assertTrue(dock.isHidden())
        self.assertTrue(handle.isVisible())
        dock.toggleViewAction().trigger()
        self._pump()
        self.assertFalse(dock.isHidden())
        self.assertFalse(handle.isVisible())
        # Not with hidden panels (their pill), not over the Library canvas, not in Classic
        studioShell.setFocusMode(True)
        self._pump()
        self.assertFalse(handle.isVisible())
        studioShell.setFocusMode(False)
        studioShell.switchTo("library")
        studioShell.setInspectorVisible(False)
        self._pump()
        self.assertFalse(handle.isVisible())
        studioShell.switchTo("draw")
        studioShell.toggleInspector()
        self._pump()
        self.assertTrue(handle.isVisible())
        studioShell.classic(persist=False)
        self._pump()
        self.assertIsNone(self.mainWindow.findChild("QToolButton", "StudioInspectorHandle"))

    def test_23_sticky_toast(self):
        """A consequence toast stays until the next input; a confirmation times out; both are recent messages."""
        studioShell = self._applyStudio()
        studioShell.switchTo("draw")
        self._pump()
        undone = []
        studioShell.toast("Saved sub-01_lesion.nii.gz", 300)
        self.assertEqual(studioShell.toastText(), "Saved sub-01_lesion.nii.gz")
        text = "Lesion 2 created · Lesion 1 hidden"
        studioShell.toast(text, level="consequence", action=lambda: undone.append(True), actionText="Undo")
        self.assertEqual(studioShell.toastText(), text)
        self.assertFalse(studioShell._toast.isVisible())
        toast = studioShell._stickyToast
        self.assertTrue(toast.actionButton.isVisible())
        self.assertEqual(toast.actionButton.text, "Undo")
        self.assertEqual(toast.label.accessibleName, text)
        self.assertEqual(toast.label.accessibleDescription, "Undo")
        # 6 s without input
        start = time.time()
        while time.time() - start < 6.0:
            slicer.app.processEvents()
            time.sleep(0.05)
        self.assertTrue(toast.isVisible())
        # The next key press closes it
        self._sendKey(self.mainWindow, qt.Qt.Key_A, text="a")
        self._pump()
        self.assertFalse(toast.isVisible())
        self.assertFalse(toast._filterInstalled)
        # The action button runs its action
        studioShell.toast(text, level="consequence", action=lambda: undone.append(True), actionText="Undo")
        toast.actionButton.click()
        self._pump()
        self.assertEqual(undone, [True])
        self.assertFalse(toast.isVisible())
        # A confirmation times out
        studioShell.toast("Layout: Focus", 200)
        start = time.time()
        while time.time() - start < 1.2:
            slicer.app.processEvents()
            time.sleep(0.05)
        self.assertEqual(studioShell.toastText(), "")
        messages = [(text, level) for timestamp, text, level in studioShell.recentMessages()]
        self.assertIn(("Saved sub-01_lesion.nii.gz", "info"), messages)
        self.assertIn((text, "consequence"), messages)
        self.assertEqual(messages[-1], ("Layout: Focus", "info"))
        self.assertLessEqual(len(studioShell.recentMessages()), 20)
        for index in range(30):
            studioShell.toast(f"message {index}", 10)
        self.assertEqual(len(studioShell.recentMessages()), 20)
        self.assertEqual(studioShell.recentMessages()[-1][1], "message 29")

    def test_24_module_selection_leaves_startup_layout(self):
        """Draw or Map entered by a module selection replace the startup layout by theirs; a chosen layout is kept (D5)."""
        from StudioLib import workspaces

        studioShell = self._applyStudio()
        studioShell.switchTo("library")
        self._pump()
        initial = slicer.vtkMRMLLayoutNode.SlicerLayoutInitialView
        self.layoutManager.setLayout(initial)
        self._pump()
        key = "map" if workspaces.isAvailable(workspaces.workspace("map")) else "draw"
        ws = workspaces.workspace(key)
        slicer.util.selectModule(ws.moduleName)
        self._pump()
        self.assertEqual(studioShell.currentWorkspace(), key)
        self.assertEqual(self.layoutManager.layout, studioShell.workspaces.rememberedLayout(ws))
        self.assertNotEqual(self.layoutManager.layout, initial)

        # A layout chosen before the module selection (here four-up by its own id) is kept
        studioShell.switchTo("library")
        self._pump()
        fourUp = slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView
        self.layoutManager.setLayout(fourUp)
        self._pump()
        slicer.util.selectModule("SegmentEditor")
        self._pump()
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        self.assertEqual(self.layoutManager.layout, fourUp)

        # The layout restored from the previous session (the close guard saves a conventional one) is not a choice
        conventional = slicer.vtkMRMLLayoutNode.SlicerLayoutConventionalView
        studioShell.switchTo("library")
        self._pump()
        self.layoutManager.setLayout(conventional)
        self._pump()
        studioShell.workspaces.resetLayoutChoice()
        slicer.util.selectModule(ws.moduleName)
        self._pump()
        self.assertEqual(self.layoutManager.layout, studioShell.workspaces.rememberedLayout(ws))

        # The layout of a scene that was opened is kept
        studioShell.switchTo("library")
        self._pump()
        self.layoutManager.setLayout(conventional)
        self._pump()
        studioShell.workspaces.resetLayoutChoice()
        slicer.mrmlScene.StartState(slicer.vtkMRMLScene.ImportState)
        slicer.mrmlScene.EndState(slicer.vtkMRMLScene.ImportState)
        slicer.util.selectModule(ws.moduleName)
        self._pump()
        self.assertEqual(self.layoutManager.layout, conventional)

    def test_25_page_keys_step_the_hovered_view(self):
        """PageUp and PageDown step the slice view under the pointer also when the focus is on a tool tile of Draw (they
        used to scroll the inspector); over the inspector they scroll it as before; a text input keeps them. A view
        that has the focus steps once (not again through the main window), and only itself.
        """
        from StudioLib import shell

        volume, segmentation = self._loadScanAndSegmentation()
        studioShell = self._applyStudio()
        studioShell.switchTo("draw")
        slicer.util.setSliceViewerLayers(background=volume, fit=True)
        self._pump()
        sliceView = self._redSliceView()
        logic = self.layoutManager.sliceWidget("Red").sliceLogic()
        scrollArea = self._dock().findChild("QScrollArea", "ScrollArea")
        self.assertIsNotNone(scrollArea)
        self.mainWindow.activateWindow()
        qt.QCursor.setPos(sliceView.mapToGlobal(qt.QPoint(sliceView.width // 2, sliceView.height // 2)))
        self._pump()
        hit = qt.QApplication.widgetAt(qt.QCursor.pos())
        if hit is None or not (hit == sliceView or sliceView.isAncestorOf(hit)):
            self.skipTest("the pointer cannot be put over the view on this display")
        bounds = [0.0] * 6
        logic.GetLowestVolumeSliceBounds(bounds)
        logic.SetSliceOffset((bounds[4] + bounds[5]) / 2.0)
        self._pump()
        # The key goes to a button of the inspector (the focus after a scan is opened), then up to the scroll area. The
        # button has the focus (the pointer over the view may have given it to the view, which then takes the key itself)
        buttons = [widget for widget in self._dock().findChildren("QAbstractButton") if widget.isVisible() and widget.focusPolicy != qt.Qt.NoFocus]
        self.assertTrue(buttons)
        button = buttons[0]
        if not self._focus(button):
            self.skipTest("the focus cannot be given to the inspector on this display")
        pageStep = max(1, studioShell.navigationBehaviours()["pageStepSlices"]) * logic.GetLowestVolumeSliceSpacing()[2]
        before = logic.GetSliceOffset()
        self._sendKey(button, qt.Qt.Key_PageUp)
        self._pump()
        self.assertAlmostEqual(logic.GetSliceOffset() - before, pageStep, places=3, msg="PageUp over the view did not step its slice once")
        # The view took the focus: the next key is its own, and it steps once
        renderWidget = sliceView.VTKWidget()
        self.assertEqual(slicer.app.focusWidget(), renderWidget)
        self._sendKey(renderWidget, qt.Qt.Key_PageDown)
        self._pump()
        self.assertAlmostEqual(logic.GetSliceOffset(), before, places=3, msg="PageDown in the view did not step back once")
        # A chord keeps its target
        self.assertTrue(self._focus(button))
        self._sendKey(button, qt.Qt.Key_PageUp, qt.Qt.ControlModifier)
        self._pump()
        self.assertAlmostEqual(logic.GetSliceOffset(), before, places=3, msg="Ctrl+PageUp was given to the view")
        # The view has the focus: it steps its slice itself, once (the key event also comes up to the main window)
        if self._focus(renderWidget):
            self._sendKey(renderWidget, qt.Qt.Key_PageUp)
            self._pump()
            self.assertAlmostEqual(logic.GetSliceOffset() - before, pageStep, places=3, msg="PageUp in the focused view stepped twice")
            self._sendKey(renderWidget, qt.Qt.Key_PageDown)
            self._pump()
            self.assertAlmostEqual(logic.GetSliceOffset(), before, places=3, msg="PageDown in the focused view did not step back once")
            # The pointer over another view: only the focused view steps
            yellowView = self.layoutManager.sliceWidget("Yellow").sliceView()
            yellowLogic = self.layoutManager.sliceWidget("Yellow").sliceLogic()
            if yellowView.isVisible():
                qt.QCursor.setPos(yellowView.mapToGlobal(qt.QPoint(yellowView.width // 2, yellowView.height // 2)))
                self._pump()
                yellowBefore = yellowLogic.GetSliceOffset()
                self._sendKey(renderWidget, qt.Qt.Key_PageUp)
                self._pump()
                self.assertAlmostEqual(logic.GetSliceOffset() - before, pageStep, places=3, msg="The focused view did not step once")
                self.assertAlmostEqual(yellowLogic.GetSliceOffset(), yellowBefore, places=3, msg="The hovered view also stepped")
                self._sendKey(renderWidget, qt.Qt.Key_PageDown)
                self._pump()
                qt.QCursor.setPos(sliceView.mapToGlobal(qt.QPoint(sliceView.width // 2, sliceView.height // 2)))
                self._pump()
            self.assertTrue(shell._isInView(renderWidget))
        self.assertAlmostEqual(logic.GetSliceOffset(), before, places=3)
        # Over the inspector the key is the scroll area's, as before
        qt.QCursor.setPos(scrollArea.mapToGlobal(qt.QPoint(scrollArea.width // 2, scrollArea.height // 2)))
        self._pump()
        self.assertTrue(self._focus(button))
        self._sendKey(button, qt.Qt.Key_PageUp)
        self._pump()
        self.assertAlmostEqual(logic.GetSliceOffset(), before, places=3, msg="PageUp over the inspector stepped the slice")
        # A line edit keeps its keys (over the view too)
        qt.QCursor.setPos(sliceView.mapToGlobal(qt.QPoint(sliceView.width // 2, sliceView.height // 2)))
        self._pump()
        edit = qt.QLineEdit(self._dock())
        edit.show()
        try:
            if self._focus(edit):
                self._sendKey(edit, qt.Qt.Key_PageUp)
                self._pump()
                self.assertAlmostEqual(logic.GetSliceOffset(), before, places=3, msg="PageUp in a line edit stepped the slice")
        finally:
            edit.hide()
            edit.deleteLater()
        # Classic: the keys are not redirected
        studioShell.classic(persist=False)
        self._pump()
        self._sendKey(self.mainWindow, qt.Qt.Key_PageUp)
        self._pump()
        self.assertAlmostEqual(logic.GetSliceOffset(), before, places=3, msg="Classic redirected PageUp")
        slicer.mrmlScene.RemoveNode(segmentation)
        slicer.mrmlScene.RemoveNode(volume)

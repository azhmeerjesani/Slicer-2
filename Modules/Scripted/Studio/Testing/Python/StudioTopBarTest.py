r"""Top bar and view command tests of Slicer Studio (U-06).

Run with the main window and testing enabled:

  Slicer --no-splash --testing --python-code "import slicer.testing; slicer.testing.runUnitTest(['<this dir>'], 'StudioTopBarTest'); slicer.app.exit(0)"

Studio never runs by itself under --testing; the tests apply it with ``Shell(force=True)`` and restore the
Classic interface. They cover:

- the "Name · key · description" tooltips, Tab focus and Space / Enter on every control;
- the switcher centred by the 3-column grid (breadcrumb, status chip, resizing, compact mode);
- the parent-tab cue, the Map tab when Map is not available, the breadcrumb;
- the layout popover: Focus + 3D (Alt+5), Compare (only with a figure), the View section, "More layouts…";
- "Single" as a maximize (hovered or last-used view, Esc and a second press restore);
- the layout hint in Library and the 3/4 view of the first 3D view.
"""

import importlib.util
import os
import re
import tempfile
import time

import numpy as np
import qt
import vtk

import slicer
from slicer import ScriptedLoadableModule


def portableKey(keySequence):
    if isinstance(keySequence, str):
        keySequence = qt.QKeySequence(keySequence)
    return keySequence.toString(qt.QKeySequence.PortableText)


class StudioTopBarTest(ScriptedLoadableModule.ScriptedLoadableModuleTest):
    """Tests of the Studio top bar and layout picker. They need the main window."""

    def setUp(self):
        slicer.mrmlScene.Clear(0)
        self.mainWindow = slicer.util.mainWindow()
        self.layoutManager = slicer.app.layoutManager()
        self.initialLayout = self.layoutManager.layout if self.layoutManager is not None else None
        self.initialGeometry = self.mainWindow.saveGeometry() if self.mainWindow is not None else None
        self._restores = []

    def tearDown(self):
        from StudioLib import shell

        for restore in reversed(self._restores):
            restore()
        self._restores = []
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
        crosshair = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLCrosshairNode")
        if crosshair is not None:
            crosshair.SetCrosshairMode(slicer.vtkMRMLCrosshairNode.NoCrosshair)
        if self.initialGeometry is not None:
            self.mainWindow.restoreGeometry(self.initialGeometry)
        slicer.app.processEvents()

    def runTest(self):
        only = os.environ.get("STUDIO_TEST_ONLY", "")
        for name in sorted(name for name in dir(self) if name.startswith("test_") and only in name):
            self.setUp()
            try:
                getattr(self, name)()
            finally:
                self.tearDown()

    #
    # Helpers
    #

    def _pump(self, count=4):
        for _index in range(count):
            slicer.app.processEvents()

    def _wait(self, condition, milliseconds=2500):
        """Process events until ``condition()`` holds (True) or the time is over (False)."""
        end = time.time() + milliseconds / 1000.0
        while time.time() < end:
            slicer.app.processEvents()
            if condition():
                return True
            time.sleep(0.01)
        return bool(condition())

    def _applyStudio(self, patch=None):
        from StudioLib import shell

        self.assertIsNotNone(self.mainWindow, "Studio tests need the main window")
        # The layout that Draw remembers from earlier runs and tests
        qt.QSettings().setValue("Studio/Draw/Layout", 7001)
        studioShell = shell.Shell(force=True)
        if patch is not None:
            patch(studioShell)
        studioShell.apply()
        self._pump()
        return studioShell

    def _patch(self, owner, name, value):
        """Replace an attribute until the end of the test."""
        original = getattr(owner, name)
        setattr(owner, name, value)
        self._restores.append(lambda: setattr(owner, name, original))

    def _sendKey(self, target, key, modifiers=qt.Qt.NoModifier, eventType=qt.QEvent.KeyPress, text=""):
        event = qt.QKeyEvent(eventType, key, modifiers, text)
        if eventType == qt.QEvent.ShortcutOverride:
            event.ignore()
        qt.QApplication.sendEvent(target, event)
        return event

    def _pressKey(self, target, key, text=""):
        """A real key press: the shortcut override, then the press and the release (as Qt delivers them)."""
        override = self._sendKey(target, key, eventType=qt.QEvent.ShortcutOverride, text=text)
        self._sendKey(target, key, text=text)
        self._sendKey(target, key, eventType=qt.QEvent.KeyRelease, text=text)
        self._pump()
        return override.isAccepted()

    def _focus(self, widget):
        self.mainWindow.activateWindow()
        widget.setFocus(qt.Qt.OtherFocusReason)
        self._pump()
        return slicer.app.focusWidget() == widget

    def _studioShortcut(self, studioShell, keySequence):
        portable = portableKey(keySequence)
        for shortcut in studioShell.shortcuts():
            if portableKey(shortcut.key) == portable:
                return shortcut
        return None

    def _activateShortcut(self, studioShell, keySequence):
        shortcut = self._studioShortcut(studioShell, keySequence)
        self.assertIsNotNone(shortcut, f"no Studio shortcut on {keySequence}")
        shortcut.activated()
        self._pump()

    def _enterTransient(self, studioShell, moduleName="Volumes", fromWorkspace="draw"):
        studioShell.switchTo(fromWorkspace)
        self._pump()
        slicer.util.selectModule(moduleName)
        self._pump()
        self.assertIsNone(studioShell.currentWorkspace())

    @staticmethod
    def _addVolume(name, shape=(8, 24, 24), role=None):
        node = slicer.util.addVolumeFromArray(np.ones(shape, dtype=np.int16) * 100, name=name)
        if role:
            node.SetAttribute("Studio.Role", role)
        return node

    @staticmethod
    def _tip(widget):
        """The tooltip as written (the application wraps plain tooltips in <p>)."""
        return re.sub(r"</?(?:p|qt)>", "", str(widget.toolTip))

    @staticmethod
    def _editor():
        return slicer.modules.SegmentEditorWidget.editor

    @staticmethod
    def _center(widget, reference):
        return widget.mapTo(reference, qt.QPoint(widget.width // 2, 0)).x()

    def _assertCentred(self, topBar, message=""):
        self._pump()
        barCenter = topBar.toolBar.width / 2.0
        self.assertLessEqual(abs(self._center(topBar.switcherFrame, topBar.toolBar) - barCenter), 1.0, message)

    def _studioLayoutActions(self):
        layoutMenu = self.mainWindow.findChild("QMenu", "LayoutMenu")
        return [(str(action.text), action.isSeparator(), str(action.data()), action.isCheckable()) for action in layoutMenu.actions()]

    def _interactor(self, studioShell, viewName):
        self.assertIn(viewName, studioShell.hoverTracker._observed)
        return studioShell.hoverTracker._observed[viewName][0]

    #
    # Tooltips, focus and keys
    #

    def test_tooltips_format(self):
        """Every top bar control explains itself: "Name · key · description"."""
        studioShell = self._applyStudio()
        topBar = studioShell.topBar
        studioShell.switchTo("draw")
        topBar._errorRows = 3
        topBar._updateErrorBadge()
        topBar._updateLayoutPicker()
        self._pump()
        buttons = [button for button in topBar.toolBar.findChildren("QAbstractButton") if not str(button.objectName).startswith("qt_")]
        names = {str(button.objectName) for button in buttons}
        for expected in ("StudioAppMenuButton", "StudioBreadcrumb", "StudioLibraryWorkspaceButton", "StudioDrawWorkspaceButton", "StudioMapWorkspaceButton",
                         "StudioUndoButton", "StudioRedoButton", "StudioLayoutButton", "StudioSearchPill", "StudioErrorBadge", "StudioLayoutTile_single"):
            self.assertIn(expected, names)
        for button in buttons:
            self.assertIn(" · ", self._tip(button), f"{button.objectName}: {button.toolTip!r}")
        for action in topBar.viewActions.values():
            self.assertIn(" · ", str(action.toolTip), str(action.objectName))
        # The texts of the work package
        self.assertEqual(self._tip(topBar.menuButton), "Menu · F10 · File, edit, view and help")
        self.assertEqual(self._tip(topBar.workspaceButtons["library"]), "Library · Ctrl+Alt+1 · Open scans, lesion masks and sample data; recent files")
        self.assertEqual(self._tip(topBar.workspaceButtons["draw"]), "Draw · Ctrl+Alt+2 · Segment Editor: brush, outline and erase lesions")
        self.assertEqual(self._tip(topBar.undoButton), "Undo · Ctrl+Z · Undo the last lesion edit")
        self.assertEqual(self._tip(topBar.redoButton), "Redo · Ctrl+Y or Ctrl+Shift+Z · Redo the last undone edit")
        self.assertEqual(self._tip(topBar.searchPill), "Search · Ctrl+K · modules, tools, actions, views, files")
        self.assertEqual(self._tip(topBar.errorBadge), "Errors · Ctrl+0 · open the error log")
        self.assertEqual(self._tip(topBar.viewActions["crosshair"]), "Crosshair · Shift+move · show where the other views cut")

    def test_focusable_and_keys(self):
        """Tab reaches every top bar control; Space and Enter press them also in Draw; the arrows move in the switcher."""
        calls = []

        def patch(studioShell):
            studioShell.openPalette = lambda: calls.append("search")

        studioShell = self._applyStudio(patch)
        topBar = studioShell.topBar
        studioShell.switchTo("draw")
        topBar._errorRows = 2
        topBar._updateErrorBadge()
        self._pump()
        tab = int(qt.Qt.TabFocus)
        controls = [topBar.menuButton, *topBar.workspaceButtons.values(), topBar.undoButton, topBar.redoButton, topBar.layoutButton, topBar.searchPill, topBar.errorBadge]
        for control in controls:
            self.assertTrue(int(control.focusPolicy) & tab, f"{control.objectName} takes no Tab focus")
        # The error badge is no focus stop while it is hidden
        topBar._errorRows = topBar._seenErrorRows
        topBar._updateErrorBadge()
        self.assertFalse(topBar.errorBadge.isVisibleTo(topBar.toolBar))
        self.assertEqual(int(topBar.errorBadge.focusPolicy), int(qt.Qt.NoFocus))
        # The search pill has a :focus rule
        self.assertIn(":focus", str(topBar.searchPill.styleSheet))
        self.assertIn("2px solid", str(topBar.searchPill.styleSheet))
        if not self._focus(topBar.menuButton):
            self.skipTest("the main window cannot take the keyboard focus here")
        # Tab visits every control (the others between them are the inspector and the views)
        visited = set()
        for _step in range(80):
            visited.add(str(slicer.app.focusWidget().objectName) if slicer.app.focusWidget() is not None else "")
            self._sendKey(slicer.app.focusWidget(), qt.Qt.Key_Tab)
            self._pump(1)
        for control in (topBar.menuButton, topBar.layoutButton, topBar.searchPill):
            self.assertIn(str(control.objectName), visited)
        # Qt keeps one Tab stop in the group of tabs that has had the focus: at least one is reached, the arrows reach the others
        self.assertTrue(visited & {str(button.objectName) for button in topBar.workspaceButtons.values()})
        # Space and Enter press a focused button in Draw (the Segment Editor's Space shortcut must not take them)
        libraryButton = topBar.workspaceButtons["library"]
        for key, text in ((qt.Qt.Key_Space, " "), (qt.Qt.Key_Return, "\r"), (qt.Qt.Key_Enter, "\r")):
            studioShell.switchTo("draw")
            self._pump()
            self.assertTrue(self._focus(libraryButton))
            self.assertTrue(self._pressKey(libraryButton, key, text), f"the shortcut override of {key} is not taken")
            self.assertEqual(studioShell.currentWorkspace(), "library", f"key {key}")
        studioShell.switchTo("draw")
        self.assertTrue(self._focus(topBar.searchPill))
        self._pressKey(topBar.searchPill, qt.Qt.Key_Space, " ")
        self._pressKey(topBar.searchPill, qt.Qt.Key_Return, "\r")
        self.assertEqual(calls, ["search", "search"])
        # The arrow keys move between the tabs
        self.assertTrue(self._focus(libraryButton))
        self._sendKey(libraryButton, qt.Qt.Key_Right)
        self.assertEqual(slicer.app.focusWidget(), topBar.workspaceButtons["draw"])
        self._sendKey(topBar.workspaceButtons["draw"], qt.Qt.Key_Left)
        self.assertEqual(slicer.app.focusWidget(), libraryButton)
        self._sendKey(libraryButton, qt.Qt.Key_Left)
        self.assertEqual(slicer.app.focusWidget(), libraryButton)

    #
    # Geometry
    #

    def test_switcher_centred(self):
        """The switcher stays centred when the breadcrumb or the status chip changes and after resizing; no fixed widths."""
        studioShell = self._applyStudio()
        topBar = studioShell.topBar
        self._assertCentred(topBar, "at the start")
        studioShell.setBreadcrumb("a very long name " * 8 + "the end")
        self._assertCentred(topBar, "with a long breadcrumb")
        self.assertTrue(str(topBar.breadcrumbButton.text).startswith("a very long name"))
        self.assertIn("…", str(topBar.breadcrumbButton.text))
        self.assertLessEqual(topBar.breadcrumbButton.width, 360)
        self.assertEqual(topBar.breadcrumbText(), "a very long name " * 8 + "the end")
        studioShell.setStatusChip("Mapping 34%", "busy")
        self.assertTrue(topBar.statusChip.isVisibleTo(topBar.toolBar))
        self._assertCentred(topBar, "with the status chip")
        # The chip is in the right column: it did not move the switcher and stays right of it
        self.assertGreater(topBar.statusChip.mapTo(topBar.toolBar, qt.QPoint(0, 0)).x(), topBar.switcherFrame.mapTo(topBar.toolBar, qt.QPoint(0, 0)).x())
        for width in (1000, 1920, 1366, 1920):
            self.mainWindow.resize(width, 760)
            self._pump(6)
            self._assertCentred(topBar, f"at {width} px")
            self.assertLessEqual(self.mainWindow.minimumSizeHint.width(), 1000,
                                 f"the window can shrink to 1000 px (top bar {topBar.toolBar.minimumSizeHint.width()}, inspector {self.mainWindow.findChild('QDockWidget', 'PanelDockWidget').minimumSizeHint.width()})")
            self.assertEqual(self.mainWindow.width, width)
        studioShell.setStatusChip(None)
        studioShell.setBreadcrumb(None)
        self._assertCentred(topBar, "after clearing")
        # No fixed widths on the top bar contents: the side containers and the switcher are not fixed
        for widget in (topBar.leftContainer, topBar.rightContainer, topBar.searchPill, topBar.breadcrumbButton):
            self.assertNotEqual(widget.minimumWidth, widget.maximumWidth, widget.objectName)
        self.assertEqual(topBar.grid.columnStretch(0), 1)
        self.assertEqual(topBar.grid.columnStretch(1), 0)
        self.assertEqual(topBar.grid.columnStretch(2), 1)
        self.assertEqual(topBar.grid.columnMinimumWidth(0), topBar.grid.columnMinimumWidth(2))

    def test_compact_mode(self):
        """setCompact(True): the search pill is a 30 px icon button, the switcher shows icons, the breadcrumb is at most 160 px."""
        studioShell = self._applyStudio()
        topBar = studioShell.topBar
        studioShell.setBreadcrumb("a very long name " * 6)
        wide = topBar.updateColumnMinimums()
        tips = {key: self._tip(button) for key, button in topBar.workspaceButtons.items()}
        topBar.setCompact(True)
        self._pump(6)
        self.assertTrue(topBar.isCompact())
        self.assertEqual(topBar.searchPill.width, 30)
        self.assertFalse(topBar.searchPlaceholder.isVisibleTo(topBar.searchPill))
        self.assertFalse(topBar.searchKey.isVisibleTo(topBar.searchPill))
        for key, button in topBar.workspaceButtons.items():
            self.assertEqual(int(button.toolButtonStyle), int(qt.Qt.ToolButtonIconOnly))
            self.assertEqual(self._tip(button), tips[key])
        self.assertLessEqual(topBar.breadcrumbButton.width, 160)
        self.assertLess(topBar.grid.columnMinimumWidth(0), wide)
        self._assertCentred(topBar, "compact")
        topBar.setCompact(False)
        self._pump(6)
        self.assertGreater(topBar.searchPill.width, 100)
        self.assertTrue(topBar.searchPlaceholder.isVisibleTo(topBar.searchPill))
        for button in topBar.workspaceButtons.values():
            self.assertEqual(int(button.toolButtonStyle), int(qt.Qt.ToolButtonTextBesideIcon))
        self._assertCentred(topBar, "after compact")

    #
    # Parent tab, Map unavailable and breadcrumb
    #

    def test_parent_cue(self):
        """A transient module shows the tab it was opened from in the parent style; a click on it returns."""
        studioShell = self._applyStudio()
        topBar = studioShell.topBar
        self._enterTransient(studioShell, "Volumes")
        self.assertFalse(any(button.checked for button in topBar.workspaceButtons.values()))
        cues = {key: bool(button.property("studioParent")) for key, button in topBar.workspaceButtons.items()}
        self.assertEqual(cues, {"library": False, "draw": True, "map": False})
        self.assertIn("studioParent", str(topBar.contents.styleSheet))
        # A chain Draw -> Volumes -> Data keeps Draw as the parent
        slicer.util.selectModule("Data")
        self._pump()
        self.assertTrue(topBar.workspaceButtons["draw"].property("studioParent"))
        topBar.workspaceButtons["draw"].click()
        self._pump()
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        self.assertFalse(any(button.property("studioParent") for button in topBar.workspaceButtons.values()))
        self.assertTrue(topBar.workspaceButtons["draw"].checked)
        # From Library
        self._enterTransient(studioShell, "Volumes", fromWorkspace="library")
        self.assertTrue(topBar.workspaceButtons["library"].property("studioParent"))
        self.assertFalse(topBar.workspaceButtons["draw"].property("studioParent"))

    def test_map_unavailable(self):
        """A Map tab without Map is enabled but not checkable; a click explains it; the current tab stays checked."""
        from StudioLib import workspaces

        studioShell = self._applyStudio()
        topBar = studioShell.topBar
        studioShell.switchTo("draw")
        original = workspaces.isAvailable
        self._patch(workspaces, "isAvailable", lambda ws: False if ws.key == "map" else original(ws))
        self._patch(importlib.util, "find_spec", lambda name, *args: None)
        topBar.refreshWorkspaceAvailability()
        mapButton = topBar.workspaceButtons["map"]
        self.assertTrue(mapButton.enabled)
        self.assertFalse(mapButton.checkable)
        self.assertTrue(mapButton.property("studioUnavailable"))
        self.assertIn("not part of this build", str(mapButton.toolTip))
        self.assertIn(" · ", str(mapButton.toolTip))
        mapButton.click()
        self._pump()
        popover = topBar.toolBar.findChild("QMenu", "StudioMapUnavailablePopover")
        self.assertIsNotNone(popover)
        self.assertTrue(popover.isVisible())
        self.assertTrue(topBar.workspaceButtons["draw"].checked)
        self.assertFalse(mapButton.checked)
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        text = popover.findChild("QLabel", "StudioMapUnavailableText")
        self.assertIn("Map computes the brain network connected to a lesion", str(text.text))
        self.assertIn("appears here as the Map tab", str(text.text))
        links = str(popover.findChild("QLabel", "StudioMapUnavailableLinks").text)
        self.assertIn("README", links)
        self.assertNotIn("Copy command", links, "no command without the library")
        # Esc closes it (the Esc ladder, rung 0.5)
        self.assertTrue(studioShell.handleEscape())
        self.assertTrue(self._wait(lambda: topBar._mapPopover is None))
        self.assertIsNone(topBar.toolBar.findChild("QMenu", "StudioMapUnavailablePopover"))
        self.assertTrue(topBar.workspaceButtons["draw"].checked)
        # With the library installed the command can be copied
        self._patch(importlib.util, "find_spec", lambda name, *args: object())
        popover = topBar.showUnavailablePopover("map")
        self._pump()
        self.assertIn("Copy command", str(popover.findChild("QLabel", "StudioMapUnavailableLinks").text))
        topBar._onUnavailableLink("copy")
        self._pump()
        clipboard = str(qt.QApplication.clipboard().text())
        self.assertIn("--launch PythonSlicer -m LesionNetworkMappingLib.engine --help", clipboard)
        self.assertTrue(clipboard.startswith('"'))
        self.assertTrue(self._wait(lambda: topBar._mapPopover is None))
        # Ctrl+Alt+3 keeps the toast
        self._activateShortcut(studioShell, "Ctrl+Alt+3")
        self.assertIn("not available", studioShell.recentMessages()[-1][1])
        # Map becomes available: checkable again
        self._restores.pop()()
        self._restores.pop()()
        topBar.refreshWorkspaceAvailability()
        if original(workspaces.workspace("map")):
            self.assertTrue(mapButton.checkable)
            self.assertFalse(mapButton.property("studioUnavailable"))

    def test_breadcrumb_follows_source(self):
        """The breadcrumb names the Draw source and lesion, a loaded set wins, and a click opens Library."""
        studioShell = self._applyStudio()
        topBar = studioShell.topBar
        scanA = self._addVolume("patient_A")
        scanB = self._addVolume("patient_B")
        figure = self._addVolume("fig", shape=(1, 24, 24), role="figure")
        studioShell.switchTo("draw")
        editor = self._editor()
        segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Lesions")
        segmentation.CreateDefaultDisplayNodes()
        editor.setSegmentationNode(segmentation)
        editor.setSourceVolumeNode(scanB)
        self.assertTrue(self._wait(lambda: topBar.breadcrumbText() == "patient_B \u203a Lesions"), topBar.breadcrumbText())
        editor.setSourceVolumeNode(scanA)
        self.assertTrue(self._wait(lambda: topBar.breadcrumbText() == "patient_A \u203a Lesions"), topBar.breadcrumbText())
        # Only Draw names the lesion
        studioShell.switchTo("library")
        self.assertTrue(self._wait(lambda: topBar.breadcrumbText() == "patient_A"), topBar.breadcrumbText())
        studioShell.switchTo("draw")
        # Without a source: the first scan that is not a figure or a map
        editor.setSourceVolumeNode(None)
        slicer.mrmlScene.RemoveNode(scanA)
        slicer.mrmlScene.RemoveNode(scanB)
        self.assertTrue(self._wait(lambda: topBar.breadcrumbText() == ""), topBar.breadcrumbText())
        self.assertEqual(str(topBar.breadcrumbButton.text), "", "no hidden text of an earlier breadcrumb")
        self.assertEqual(topBar._sourceVolume(), None)
        self.assertEqual(figure.GetAttribute("Studio.Role"), "figure")
        # An explicit breadcrumb wins until it is cleared
        scanC = self._addVolume("patient_C")
        editor.setSourceVolumeNode(scanC)
        clicked = []
        studioShell.setBreadcrumb("sub-05", "5 of 20", onClick=lambda: clicked.append(1))
        self.assertEqual(topBar.breadcrumbText(), "sub-05 · 5 of 20")
        editor.setSourceVolumeNode(None)
        self._wait(lambda: False, 400)
        self.assertEqual(topBar.breadcrumbText(), "sub-05 · 5 of 20")
        self.assertTrue(topBar.breadcrumbButton.property("studioClickable"))
        topBar.breadcrumbButton.click()
        self.assertEqual(clicked, [1])
        studioShell.setBreadcrumb("Study", "sub-001")
        self.assertEqual(topBar.breadcrumbText(), "Study  /  sub-001")
        self.assertFalse(topBar.breadcrumbButton.property("studioClickable"), "no click action: no hover style")
        self.assertEqual(int(topBar.breadcrumbButton.focusPolicy), int(qt.Qt.NoFocus))
        studioShell.setBreadcrumb(None)
        editor.setSourceVolumeNode(scanC)
        self.assertTrue(self._wait(lambda: topBar.breadcrumbText().startswith("patient_C")), topBar.breadcrumbText())
        # The default click opens Library
        self.assertTrue(topBar.breadcrumbButton.property("studioClickable"))
        self.assertIn("Show in Library", str(topBar.breadcrumbButton.toolTip))
        self.assertIn(" · ", str(topBar.breadcrumbButton.toolTip))
        topBar.breadcrumbButton.click()
        self._pump()
        self.assertEqual(studioShell.currentWorkspace(), "library")

    def test_breadcrumb_ampersand(self):
        """A '&' in a name is shown as typed: not a mnemonic, so no underline and no shortcut on the breadcrumb."""
        studioShell = self._applyStudio()
        topBar = studioShell.topBar
        button = topBar.breadcrumbButton
        studioShell.setBreadcrumb("T1 & T2_scan", "Lesions", onClick=lambda: None)
        self.assertEqual(topBar.breadcrumbText(), "T1 & T2_scan  /  Lesions")
        self.assertEqual(str(button.text), "T1 && T2_scan  /  Lesions", "the displayed text escapes the mnemonic")
        self.assertEqual(str(button.shortcut.toString()), "", "no mnemonic shortcut")
        # The default breadcrumb (the Draw source scan) is escaped as well
        studioShell.setBreadcrumb(None)
        studioShell.switchTo("draw")
        scan = self._addVolume("T1 & T2_scan")
        self._editor().setSourceVolumeNode(scan)
        self.assertTrue(self._wait(lambda: topBar.breadcrumbText().startswith("T1 & T2_scan")), topBar.breadcrumbText())
        self.assertTrue(str(button.text).startswith("T1 && T2_scan"), str(button.text))
        self.assertEqual(str(button.shortcut.toString()), "")

    #
    # Layout popover
    #

    def test_view_section(self):
        """Crosshair, slice intersections, Link views and Reset views in the layout popover."""
        from StudioLib import layouts

        studioShell = self._applyStudio()
        topBar = studioShell.topBar
        studioShell.switchTo("draw")
        crosshair = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLCrosshairNode")
        none, basic, hashmarks = (slicer.vtkMRMLCrosshairNode.NoCrosshair, slicer.vtkMRMLCrosshairNode.ShowBasic, slicer.vtkMRMLCrosshairNode.ShowHashmarks)
        crosshair.SetCrosshairMode(none)
        actions = topBar.viewActions
        self.assertEqual([str(action.text) for action in actions.values()], ["Crosshair", "Slice intersections", "Link views", "Reset views"])
        self.assertTrue(actions["crosshair"].checkable)
        self.assertFalse(actions["reset"].checkable)
        topBar._updateLayoutPicker()
        self.assertFalse(actions["crosshair"].checked)
        actions["crosshair"].trigger()
        self.assertEqual(crosshair.GetCrosshairMode(), basic)
        topBar._updateLayoutPicker()
        self.assertTrue(actions["crosshair"].checked, "the state is visible in the popover")
        actions["crosshair"].trigger()
        self.assertEqual(crosshair.GetCrosshairMode(), none)
        # The last mode that showed something comes back
        crosshair.SetCrosshairMode(hashmarks)
        topBar._updateLayoutPicker()
        actions["crosshair"].trigger()
        self.assertEqual(crosshair.GetCrosshairMode(), none)
        actions["crosshair"].trigger()
        self.assertEqual(crosshair.GetCrosshairMode(), hashmarks)
        # Slice intersections
        logic = slicer.app.applicationLogic()
        mode = slicer.vtkMRMLApplicationLogic.IntersectingSlicesVisibility
        before = bool(logic.GetIntersectingSlicesEnabled(mode))
        topBar._updateLayoutPicker()
        self.assertEqual(actions["intersections"].checked, before)
        actions["intersections"].trigger()
        self.assertEqual(bool(logic.GetIntersectingSlicesEnabled(mode)), not before)
        topBar._updateLayoutPicker()
        self.assertEqual(actions["intersections"].checked, not before)
        actions["intersections"].trigger()
        self.assertEqual(bool(logic.GetIntersectingSlicesEnabled(mode)), before)
        # Link views: every composite node but a figure view
        figure = self._addVolume("figure", shape=(1, 30, 20), role="figure")
        yellow = self.layoutManager.sliceWidget("Yellow").mrmlSliceCompositeNode()
        yellow.SetBackgroundVolumeID(figure.GetID())
        yellow.SetLinkedControl(False)
        self._pump()
        composites = slicer.util.getNodesByClass("vtkMRMLSliceCompositeNode")
        for node in composites:
            node.SetLinkedControl(False)
        topBar._updateLayoutPicker()
        self.assertFalse(actions["link"].checked)
        actions["link"].trigger()
        for node in composites:
            self.assertEqual(bool(node.GetLinkedControl()), node.GetLayoutName() != "Yellow", node.GetLayoutName())
        topBar._updateLayoutPicker()
        self.assertTrue(actions["link"].checked)
        actions["link"].trigger()
        self.assertFalse(any(node.GetLinkedControl() for node in composites))
        # Reset views: the figure view keeps its slice (offset 0) and its mirroring
        sliceNode = self.layoutManager.sliceWidget("Yellow").mrmlSliceNode()
        sliceNode.SetOrientationToAxial()
        sliceNode.GetSliceToRAS().SetElement(0, 0, -1)
        sliceNode.UpdateMatrices()
        offset = sliceNode.GetSliceOffset()
        red = self.layoutManager.sliceWidget("Red").mrmlSliceNode()
        red.SetSliceOffset(red.GetSliceOffset() + 7.0)
        actions["reset"].trigger()
        self._pump()
        self.assertAlmostEqual(sliceNode.GetSliceOffset(), offset)
        self.assertEqual(sliceNode.GetSliceToRAS().GetElement(0, 0), -1)
        self.assertTrue(layouts.viewsLinked() is False)

    def test_focus3d_tile_and_alt5(self):
        """Focus + 3D is a tile and Alt+5; the first 3D view shows the slice in a 3/4 view."""
        from StudioLib import layouts

        # A 3D view was created earlier (the classic layout has one): it does not count as the first 3D view of Studio
        self.layoutManager.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutConventionalView)
        self._pump()
        self.assertGreater(self.layoutManager.threeDViewCount, 0)
        redSlice = self.layoutManager.sliceWidget("Red").mrmlSliceNode()
        redSlice.SetSliceVisible(False)
        studioShell = self._applyStudio()
        topBar = studioShell.topBar
        studioShell.switchTo("draw")
        self._pump(10)
        self.assertEqual(layouts.threeDViewNamesInLayout(), [])
        self.assertFalse(topBar._threeDPrepared)
        self.assertIn(("focus3d", "Focus + 3D", "Alt+5", "box", layouts.FOCUS_3D_LAYOUT_ID), layouts.PICKER_ENTRIES)
        tile = topBar.layoutTiles["focus3d"]
        self.assertEqual(str(tile.text), "Focus + 3D\nAlt+5")
        self.assertIn(" · ", str(tile.toolTip))
        self.assertIsNotNone(self._studioShortcut(studioShell, "Alt+5"))
        self.assertFalse(topBar._threeDPrepared)
        self._activateShortcut(studioShell, "Alt+5")
        self.assertEqual(self.layoutManager.layout, layouts.FOCUS_3D_LAYOUT_ID)
        self.assertTrue(self._wait(lambda: topBar._threeDPrepared))
        self._pump(4)
        camera = self.layoutManager.threeDWidget(0).threeDView().cameraNode().GetCamera()
        direction = np.array(camera.GetPosition()) - np.array(camera.GetFocalPoint())
        direction /= np.linalg.norm(direction)
        self.assertGreater(abs(direction[0]), 0.2, f"the camera is turned (direction {direction})")
        self.assertGreater(direction[2], 0.1, f"the camera is raised (direction {direction})")
        self.assertTrue(redSlice.GetSliceVisible(), "the Red slice is shown in 3D")
        # Once per session: the camera is not reset again
        camera.Azimuth(60)
        position = camera.GetPosition()
        studioShell.setLayoutFromPicker("focus")
        studioShell.setLayoutFromPicker("focus3d")
        self._pump(6)
        self.assertEqual(camera.GetPosition(), position)
        # The tile
        studioShell.setLayoutFromPicker("fourup")
        topBar._updateLayoutPicker()
        self.assertFalse(tile.checked)
        tile.click()
        self._pump()
        self.assertEqual(self.layoutManager.layout, layouts.FOCUS_3D_LAYOUT_ID)
        topBar._updateLayoutPicker()
        self.assertTrue(tile.checked)
        # The user's choice of the Red slice is kept
        studioShell.dispose()
        studioShell = self._applyStudio()
        topBar = studioShell.topBar
        studioShell.switchTo("draw")
        redSlice.SetSliceVisible(True)
        redSlice.SetSliceVisible(False)
        studioShell.setLayoutFromPicker("focus3d")
        self.assertTrue(self._wait(lambda: topBar._threeDPrepared))
        self.assertFalse(redSlice.GetSliceVisible())

    def test_single_is_maximize(self):
        """Single maximizes the view the user worked in, inside the current layout; Esc or a second press restores."""
        from StudioLib import layouts

        studioShell = self._applyStudio()
        topBar = studioShell.topBar
        studioShell.switchTo("draw")
        self._pump()
        layoutId = self.layoutManager.layout
        node = layouts.layoutNode()
        self.assertEqual(layoutId, layouts.FOCUS_LAYOUT_ID)
        red, green = self._interactor(studioShell, "Red"), self._interactor(studioShell, "Green")
        tracker = studioShell.hoverTracker
        # Work in Red, then cross Green with the pointer: the picker click (outside the views) enlarges Red
        red.InvokeEvent(vtk.vtkCommand.MouseWheelForwardEvent)
        green.InvokeEvent(vtk.vtkCommand.EnterEvent)
        green.InvokeEvent(vtk.vtkCommand.LeaveEvent)
        self.assertIsNone(tracker.hoveredNow)
        self.assertEqual(tracker.lastInteracted, "Red")
        self.assertEqual(tracker.singleTarget(), "Red")
        topBar._updateLayoutPicker()
        self.assertFalse(topBar.layoutTiles["single"].checked)
        topBar.layoutTiles["single"].click()
        self._pump()
        self.assertEqual(self.layoutManager.layout, layoutId, "no layout change")
        self.assertEqual(node.GetNumberOfMaximizedViewNodes(), 1)
        self.assertEqual(layouts.maximizedViewNames(), ["Red"])
        topBar._updateLayoutPicker()
        self.assertTrue(topBar.layoutTiles["single"].checked, "the tile shows the maximized state")
        self.assertFalse(topBar.layoutTiles["focus"].checked, "one state")
        # Esc (rung 2) restores: the layout is unchanged
        self.assertTrue(studioShell.handleEscape())
        self.assertEqual(node.GetNumberOfMaximizedViewNodes(), 0)
        self.assertEqual(self.layoutManager.layout, layoutId)
        topBar._updateLayoutPicker()
        self.assertFalse(topBar.layoutTiles["single"].checked)
        self.assertTrue(topBar.layoutTiles["focus"].checked)
        # Alt+2 over a view enlarges the hovered one; a second press restores
        green.InvokeEvent(vtk.vtkCommand.EnterEvent)
        self.assertEqual(tracker.hoveredNow, "Green")
        self._activateShortcut(studioShell, "Alt+2")
        self.assertEqual(layouts.maximizedViewNames(), ["Green"])
        self.assertEqual(self.layoutManager.layout, layoutId)
        self._activateShortcut(studioShell, "Alt+2")
        self.assertEqual(node.GetNumberOfMaximizedViewNodes(), 0)
        # (The real pointer of the display may enter a view when the layout changes under it)
        for name in ("Red", "Green", "Yellow"):
            self._interactor(studioShell, name).InvokeEvent(vtk.vtkCommand.LeaveEvent)
        self.assertIsNone(tracker.hoveredNow)
        # The palette and every other caller of the shell go through the same function
        studioShell.setLayoutFromPicker("single")
        self.assertEqual(layouts.maximizedViewNames(), ["Red"])
        # A layout chosen while a view is maximized restores it first (one enlarged state)
        studioShell.setLayoutFromPicker("fourup")
        self.assertEqual(node.GetNumberOfMaximizedViewNodes(), 0)
        self.assertEqual(self.layoutManager.layout, slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
        # Interactions with other views change the target; the keys count as work, too
        self.layoutManager.sliceWidget("Yellow")
        self._interactor(studioShell, "Yellow").InvokeEvent(vtk.vtkCommand.KeyPressEvent)
        self.assertEqual(tracker.singleTarget(), "Yellow")
        topBar.layoutTiles["single"].click()
        self.assertEqual(layouts.maximizedViewNames(), ["Yellow"])
        self.assertTrue(studioShell.handleEscape())
        self.assertEqual(layouts.maximizedViewNames(), [])
        # The legacy helpers stay
        self.assertEqual(layouts.pickerLayoutId("single"), slicer.vtkMRMLLayoutNode.SlicerLayoutOneUpRedSliceView)
        self.assertEqual(layouts.singleLayoutId("Green"), slicer.vtkMRMLLayoutNode.SlicerLayoutOneUpGreenSliceView)

    def test_tracker_without_mouse_move_work(self):
        """The tracker uses VTK observers (no Qt event filter) and follows the views of the current layout."""
        from StudioLib import layouts

        studioShell = self._applyStudio()
        tracker = studioShell.hoverTracker
        self.assertEqual(sorted(tracker._observed), sorted(self.layoutManager.sliceViewNames()))
        self._interactor(studioShell, "Green").InvokeEvent(vtk.vtkCommand.LeftButtonPressEvent)
        self.assertEqual(tracker.lastInteracted, "Green")
        self.layoutManager.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutOneUpRedSliceView)
        self._pump()
        self.assertEqual(layouts.sliceViewNamesInLayout(), ["Red"])
        self.assertEqual(tracker.singleTarget(), "Red", "a view that the layout does not show is no target")
        self.layoutManager.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
        self._pump()
        self.assertEqual(sorted(layouts.sliceViewNamesInLayout()), ["Green", "Red", "Yellow"])
        self.assertEqual(sorted(tracker._observed), sorted(self.layoutManager.sliceViewNames()))
        self.assertEqual(tracker.singleTarget(), "Green")
        self._interactor(studioShell, "Green").InvokeEvent(vtk.vtkCommand.EnterEvent)
        self.assertEqual(tracker.lastSliceViewName, "Green")
        # Disposing removes the observers
        interactor = self._interactor(studioShell, "Red")
        tracker.dispose()
        self.assertEqual(tracker._observed, {})
        interactor.InvokeEvent(vtk.vtkCommand.EnterEvent)
        self.assertNotEqual(tracker.hoveredNow, "Red")

    def test_compare_layout_listed_only_with_figure(self):
        """Compare (7005) is a tile only while a figure is shown."""
        from StudioLib import importer, layouts

        studioShell = self._applyStudio()
        topBar = studioShell.topBar
        studioShell.switchTo("draw")
        self.assertEqual([entry[0] for entry in layouts.pickerEntries()], ["focus", "single", "fourup", "row", "focus3d"])
        topBar._updateLayoutPicker()
        self.assertTrue(topBar.layoutTiles["compare"].isHidden())
        # A real figure: a picture dropped on a scan
        self._addVolume("patient_T1", shape=(16, 24, 24))
        directory = tempfile.mkdtemp(prefix="studio-topbar-")
        image = qt.QImage(200, 100, qt.QImage.Format_RGB32)
        image.fill(qt.QColor(255, 255, 255))
        path = os.path.join(directory, "case1_fig2.png")
        self.assertTrue(image.save(path))
        importer.loadFiles([path], shell=studioShell)
        self._pump(8)
        self.assertIsNotNone(importer.figureController())
        self.assertTrue(layouts.figureShown())
        self.assertEqual([entry[0] for entry in layouts.pickerEntries()], ["focus", "single", "fourup", "row", "focus3d", "compare"])
        self.assertEqual(self.layoutManager.layout, importer.COMPARE_LAYOUT_ID)
        studioShell.setLayoutFromPicker("fourup")
        # The Segment Editor puts its source volume into every view when the layout changes; the figure guard undoes that
        self._pump(10)
        topBar._updateLayoutPicker()
        compare = topBar.layoutTiles["compare"]
        self.assertFalse(compare.isHidden())
        self.assertEqual(str(compare.text).split("\n")[0], "Compare")
        self.assertIn(" · ", str(compare.toolTip))
        compare.click()
        self._pump(6)
        self.assertEqual(self.layoutManager.layout, importer.COMPARE_LAYOUT_ID)
        topBar._updateLayoutPicker()
        self.assertTrue(compare.checked)
        # Removing the figure takes the tile away again
        importer.figureController().restore()
        self._pump(6)
        topBar._updateLayoutPicker()
        self.assertTrue(compare.isHidden())

    def test_more_layouts_does_not_modify_layoutmenu(self):
        """More layouts… shows the Studio layouts and the classic actions by reference; the Layout menu is not changed."""
        before = self._studioLayoutActions()
        self.assertTrue(before)
        studioShell = self._applyStudio()
        topBar = studioShell.topBar
        menu = topBar.moreLayoutsMenu()
        titles = [str(action.text) for action in menu.actions() if not action.isSeparator()]
        self.assertEqual(titles[:3], ["Studio Focus", "Studio Focus + 3D", "Studio Row"])
        layoutMenu = self.mainWindow.findChild("QMenu", "LayoutMenu")
        classicTexts = [str(action.text) for action in layoutMenu.actions() if not action.isSeparator()]
        self.assertEqual(titles[3:], classicTexts)
        self.assertEqual(self._studioLayoutActions(), before, "the Layout menu is the same while Studio is active")
        # A Studio entry sets the layout; the button of the popover pops the menu up
        studioShell.switchTo("draw")
        action = next(action for action in menu.actions() if str(action.text) == "Studio Row")
        action.trigger()
        self._pump()
        self.assertEqual(self.layoutManager.layout, 7003)
        topBar._onMoreLayouts()
        self._pump()
        self.assertTrue(menu.isVisible())
        menu.close()
        studioShell.dispose()
        self._pump()
        self.assertEqual(self._studioLayoutActions(), before, "no Studio entry leaks into Classic")

    #
    # Library layout hint
    #

    def test_library_layout_toast(self):
        """A layout chosen while Library is shown says that it is shown in Draw; Draw shows no hint."""
        from StudioLib import layouts

        studioShell = self._applyStudio()
        studioShell.switchTo("draw")
        self._pump()
        before = len([message for message in studioShell.recentMessages() if message[1].startswith("Layout:")])
        studioShell.setLayoutFromPicker("fourup")
        self._pump()
        self.assertEqual(len([message for message in studioShell.recentMessages() if message[1].startswith("Layout:")]), before)
        studioShell.switchTo("library")
        self._pump()
        self.assertIsNotNone(studioShell.canvas())
        self.assertEqual(len([message for message in studioShell.recentMessages() if message[1].startswith("Layout:")]), before, "no hint for entering Library")
        studioShell.setLayoutFromPicker("row")
        self._pump()
        self.assertEqual(studioShell.recentMessages()[-1][1], "Layout: Row (shown in Draw)")
        self._activateShortcut(studioShell, "Alt+3")
        self.assertEqual(studioShell.recentMessages()[-1][1], "Layout: Four-up (shown in Draw)")
        self._activateShortcut(studioShell, "Alt+5")
        self.assertEqual(studioShell.recentMessages()[-1][1], "Layout: Focus + 3D (shown in Draw)")
        # Single: a maximize behind the canvas
        self._activateShortcut(studioShell, "Alt+2")
        self.assertEqual(studioShell.recentMessages()[-1][1], "Layout: Single (shown in Draw)")
        self.assertTrue(layouts.isMaximized())
        self._activateShortcut(studioShell, "Alt+2")
        self.assertFalse(layouts.isMaximized())

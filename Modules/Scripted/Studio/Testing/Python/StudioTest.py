"""Tests of the Slicer Studio shell (Modules/Scripted/Studio).

Run with the main window and testing enabled:

  Slicer --no-splash --testing --python-code "import slicer.testing; slicer.testing.runUnitTest(['<this dir>'], 'StudioTest'); slicer.app.exit(0)"

Studio never runs by itself under --testing; the tests apply it with ``Shell(force=True)`` and
always restore the Classic interface.

``assertNoKeyCollisions()`` is a module-level function so that later work packages can extend the
reserved key table (for example with the Draw keys).
"""

import qt

import slicer
from slicer import ScriptedLoadableModule

#
# Keys used by the views and the Segment Editor. Studio shortcuts must not take them (SPEC 9.6).
# Each entry is (Qt portable key sequence, any modifier, source). With "any modifier", the key
# collides whatever other modifiers the Studio shortcut adds to the entry's own: "[" with any
# modifier collides with Ctrl+[, and "Shift+V" (the keysym "V", which VTK only gets with Shift:
# vtkMRMLSliceIntersectionWidget.cxx:223) collides with Ctrl+Shift+V but not with Ctrl+V (keysym "v").
#

VTK_KEY_BINDINGS = [
    # Slice views: vtkMRMLSliceIntersectionWidget.cxx:172-202
    ("G", False, "slice view: toggle label opacity"),
    ("T", False, "slice view: toggle foreground opacity"),
    ("Right", False, "slice view: next slice"),
    ("Left", False, "slice view: previous slice"),
    ("Up", False, "slice view: next slice"),
    ("Down", False, "slice view: previous slice"),
    ("F", False, "slice view: next slice"),
    ("B", False, "slice view: previous slice"),
    ("V", False, "slice view: toggle slice visibility"),
    ("Shift+V", True, "slice view: toggle all slices visibility (any modifier)"),
    ("[", True, "slice view: previous background volume (any modifier)"),
    ("]", True, "slice view: next background volume (any modifier)"),
    ("{", True, "slice view: previous foreground volume (any modifier)"),
    ("}", True, "slice view: next foreground volume (any modifier)"),
    ("R", False, "slice view: reset field of view"),
    # Slice views: page and first/last slice navigation (SPEC 6.2, Appendix A)
    ("PgUp", False, "slice view: page up"),
    ("PgDown", False, "slice view: page down"),
    ("Home", False, "slice view: first slice"),
    ("End", False, "slice view: last slice"),
    # vtkMRMLInteractionWidget.cxx:87, vtkMRMLWindowLevelWidget.cxx:81
    ("Esc", False, "views: cancel interaction"),
    # 3D views: vtkMRMLCameraWidget.cxx:58-98
    ("Num+1", False, "3D view: anterior"), ("Shift+Num+1", False, "3D view: posterior"),
    ("End", False, "3D view: anterior"), ("Shift+End", False, "3D view: posterior"),
    ("Num+3", False, "3D view: left"), ("Shift+Num+3", False, "3D view: right"),
    ("PgDown", False, "3D view: left"), ("Shift+PgDown", False, "3D view: right"),
    ("Num+7", False, "3D view: superior"), ("Shift+Num+7", False, "3D view: inferior"),
    ("Home", False, "3D view: superior"), ("Shift+Home", False, "3D view: inferior"),
    ("Num+2", False, "3D view: rotate"), ("Num+8", False, "3D view: rotate"),
    ("Num+4", False, "3D view: rotate"), ("Num+6", False, "3D view: rotate"),
    ("Down", False, "3D view: rotate"), ("Up", False, "3D view: rotate"),
    ("Left", False, "3D view: rotate"), ("Right", False, "3D view: rotate"),
    ("Shift+Num+2", False, "3D view: translate"), ("Shift+Num+8", False, "3D view: translate"),
    ("Shift+Num+4", False, "3D view: translate"), ("Shift+Num+6", False, "3D view: translate"),
    ("Shift+Down", False, "3D view: translate"), ("Shift+Up", False, "3D view: translate"),
    ("Shift+Left", False, "3D view: translate"), ("Shift+Right", False, "3D view: translate"),
    ("+", False, "3D view: zoom in"), ("-", False, "3D view: zoom out"),
    ("Ctrl+B", False, "3D view: toggle tilt lock"),
    ("Num+0", False, "3D view: reset camera"), ("Ins", False, "3D view: reset camera"),
    ("Num+5", False, "3D view: reset rotation"), ("Clear", False, "3D view: reset rotation"),
    ("Shift+Num+5", False, "3D view: reset translation"), ("Shift+Clear", False, "3D view: reset translation"),
    # Markups on a widget: vtkSlicerMarkupsWidget.cxx:69-72
    ("S", False, "markups: snap to slice"),
    ("Shift+Del", False, "markups: reset"),
    ("Del", False, "markups: delete point"),
    ("Backspace", False, "markups: delete point"),
    # Draw effect (SegmentEditorDrawEffect.py): a / Return apply, x deletes the last point
    ("A", False, "Draw effect: apply"),
    ("Return", False, "Draw effect: apply"),
    ("X", False, "Draw effect: delete last point"),
    # Paint effect (qSlicerSegmentEditorPaintEffect.cxx): brush size
    ("+", False, "Paint effect: larger brush"),
    ("=", False, "Paint effect: larger brush"),
    ("-", False, "Paint effect: smaller brush"),
    ("_", False, "Paint effect: smaller brush"),
    ("[", False, "Paint effect: smaller brush"),
    ("]", False, "Paint effect: larger brush"),
]

# Segment Editor QShortcuts: qMRMLSegmentEditorWidget.cxx:2315-2394
SEGMENT_EDITOR_SHORTCUTS = (
    [str(index % 10) for index in range(1, 11)]
    + [f"Shift+{index % 10}" for index in range(1, 11)]
    + ["Esc", "Space", "Z", "Ctrl+Z", "Y", "Ctrl+Y", "Q", "W", "/", "*", ",", ".", ">", "<", "I"])

# Widgets created by the tests that must live until the application exits
_keepAlive = []

#: Intentional overrides of reserved keys by Studio shortcuts. Must stay empty (SPEC 9.6).
KEY_COLLISION_EXEMPTIONS = []

_MODIFIERS = ("Meta", "Ctrl", "Alt", "Shift", "Num")


def portableKey(keySequence):
    """Qt portable text of a key sequence (for example "Ctrl+Alt+1")."""
    if isinstance(keySequence, str):
        keySequence = qt.QKeySequence(keySequence)
    return keySequence.toString(qt.QKeySequence.PortableText)


def _splitKey(key):
    """Return (modifiers, base key) of a portable key string, e.g. ("Ctrl", "Alt"), "1"."""
    parts = key.split("+")
    modifiers = []
    while len(parts) > 1 and parts[0] in _MODIFIERS:
        modifiers.append(parts.pop(0))
    base = "+".join(parts) if parts != ["", ""] else "+"
    return frozenset(modifiers), base


def reservedKeyBindings():
    """All reserved keys: [(portable key, any modifier, source)]."""
    reserved = [(portableKey(key), anyModifier, source) for key, anyModifier, source in VTK_KEY_BINDINGS]
    reserved += [(portableKey(key), False, "Segment Editor shortcut") for key in SEGMENT_EDITOR_SHORTCUTS]
    return reserved


def keyCollisions(shortcutKeys, extraReserved=()):
    """Return the collisions of ``shortcutKeys`` ([(key, description)]) with the reserved keys.

    Collisions are [(studio key, studio description, reserved key, reserved source)].
    """
    reserved = reservedKeyBindings() + [(portableKey(key), anyModifier, source) for key, anyModifier, source in extraReserved]
    exempted = {portableKey(key) for key in KEY_COLLISION_EXEMPTIONS}
    collisions = []
    for key, description in shortcutKeys:
        key = portableKey(key)
        if not key or key in exempted:
            continue
        modifiers, base = _splitKey(key)
        for reservedKey, anyModifier, source in reserved:
            reservedModifiers, reservedBase = _splitKey(reservedKey)
            if base == reservedBase and ((anyModifier and reservedModifiers <= modifiers) or modifiers == reservedModifiers):
                collisions.append((key, description, reservedKey, source))
    return collisions


#: Keys of the Draw workspace (SPEC 5.4, Appendix A; D-A10 for M; SPEC_LNM_ADDENDUM 7.4 for Ctrl+V, "paste a figure",
#: enabled while the clipboard holds an image), checked while Draw is shown. Data: later work packages add their Draw keys
#: here, and the collision test runs over every installed Studio key.
DRAW_KEYS = ["P", "D", "M", "E", "L", "Shift+F", "O", "N", "Shift+N", "C", "Alt+Left", "Alt+Right", "Ctrl+Shift+S", "Ctrl+V"]


def studioShortcutKeys(studioShell):
    """[(key, description)] of every enabled Studio QShortcut of the main window: the shell's (global and of the current
    workspace) and the ones that other parts of Studio install (the Draw presentation of the Segment Editor), which carry
    the ``studioShortcut`` property.
    """
    shortcuts = list(studioShell.shortcuts())
    for shortcut in studioShell.mainWindow.findChildren("QShortcut"):
        if shortcut.enabled and shortcut.property("studioShortcut") and shortcut not in shortcuts:
            shortcuts.append(shortcut)
    return [(shortcut.key, shortcut.property("studioDescription")) for shortcut in shortcuts if shortcut.enabled]


def assertNoKeyCollisions(shortcutKeys=None, extraReserved=()):
    """Fail if a Studio shortcut uses a key of the views or of the Segment Editor (SPEC 9.6).

    :param shortcutKeys: [(key sequence, description)]; by default every Studio shortcut installed now
      (``studioShortcutKeys()``: global, of the current workspace and of the Draw presentation).
    :param extraReserved: more reserved keys [(key, any modifier, source)].
    """
    if shortcutKeys is None:
        from StudioLib import shell

        studioShell = shell.instance()
        if studioShell is None:
            raise AssertionError("assertNoKeyCollisions: Studio is not active")
        shortcutKeys = studioShortcutKeys(studioShell)
    collisions = keyCollisions(shortcutKeys, extraReserved)
    if collisions:
        raise AssertionError("Studio shortcuts collide with reserved keys:\n" + "\n".join(
            f"  {key} ({description}) vs {reservedKey}: {source}" for key, description, reservedKey, source in collisions))
    if KEY_COLLISION_EXEMPTIONS:
        raise AssertionError(f"The key collision exemption list must be empty: {KEY_COLLISION_EXEMPTIONS}")


def ambiguousShortcuts(mainWindow):
    """Window-wide keys that more than one enabled QShortcut or QAction of the main window uses,
    when at least one of them is a Studio shortcut. Qt triggers neither of ambiguous shortcuts.
    """
    owners = {}
    windowContexts = (qt.Qt.WindowShortcut, qt.Qt.ApplicationShortcut)
    for shortcut in mainWindow.findChildren("QShortcut"):
        if not shortcut.enabled or shortcut.context not in windowContexts:
            continue
        parent = shortcut.parent()
        if parent is not None and parent.isWidgetType() and not parent.isVisible():
            continue
        key = portableKey(shortcut.key)
        if key:
            owners.setdefault(key, []).append(("Studio " if shortcut.property("studioShortcut") else "") + "QShortcut " + str(shortcut.property("studioDescription") or shortcut.objectName))
    for action in mainWindow.actions():
        if not action.enabled or action.shortcutContext not in windowContexts:
            continue
        for keySequence in action.shortcuts():
            key = portableKey(keySequence)
            if key:
                owners.setdefault(key, []).append(("Studio " if action.objectName.startswith("Studio") else "") + "QAction " + (action.objectName or action.text))
    return {key: names for key, names in owners.items() if len(names) > 1 and any(name.startswith("Studio") for name in names)}


#
# StudioTest
#


class StudioTest(ScriptedLoadableModule.ScriptedLoadableModuleTest):
    """Tests of the Studio shell. They need the main window."""

    def setUp(self):
        slicer.mrmlScene.Clear(0)
        self.mainWindow = slicer.util.mainWindow()
        self.layoutManager = slicer.app.layoutManager()
        self.initialLayout = self.layoutManager.layout if self.layoutManager is not None else None

    def tearDown(self):
        from StudioLib import shell

        live = shell._shell
        if live is not None:
            live.dispose()
        if self.layoutManager is not None and self.initialLayout is not None and self.layoutManager.layout != self.initialLayout:
            self.layoutManager.setLayout(self.initialLayout)
        slicer.app.processEvents()
        # Delete what the shell disposed with deleteLater() (processEvents() does not, below the main event loop):
        # a top bar left alive keeps the File, Edit, View and Help menus in its app menu, and the next shell would
        # then not attach their shortcuts to the main window
        slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)

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

    def _wait(self, ms):
        """Process events for ``ms`` milliseconds (timers fire)."""
        import time

        end = time.perf_counter() + ms / 1000.0
        while time.perf_counter() < end:
            slicer.app.processEvents()
            time.sleep(0.005)

    def _applyStudio(self):
        from StudioLib import shell

        self.assertIsNotNone(self.mainWindow, "Studio tests need the main window")
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        self._pump()
        return studioShell

    def test_1_testing_guard(self):
        """Under --testing no Studio code runs and the Classic interface is complete (SPEC 9.1)."""
        self.assertTrue(slicer.app.testingEnabled())
        from StudioLib import shell

        self.assertIsNone(shell.instance())
        mainWindow = self.mainWindow
        self.assertIsNotNone(mainWindow)
        self.assertIsNone(mainWindow.findChild("QToolBar", "StudioTopBar"))
        self.assertIsNone(mainWindow.findChild("QAction", "StudioInterfaceAction"))
        self.assertFalse(mainWindow.menuBar().isHidden())
        mainToolBar = mainWindow.findChild("QToolBar", "MainToolBar")
        self.assertIsNotNone(mainToolBar)
        self.assertTrue(mainToolBar.isVisible())
        self.assertIsNotNone(mainWindow.findChild("QWidget", "DataProbeCollapsibleWidget"))
        self.assertIsNotNone(mainWindow.findChild("QLabel", "LogoLabel"))
        dock = mainWindow.findChild("QDockWidget", "PanelDockWidget")
        self.assertEqual(mainWindow.dockWidgetArea(dock), qt.Qt.LeftDockWidgetArea)

    def test_2_apply_classic_round_trip(self):
        """Classic restores the exact window state, the logo title bar and the menubar (SPEC 2.7)."""
        from StudioLib import shell

        mainWindow = self.mainWindow
        dock = mainWindow.findChild("QDockWidget", "PanelDockWidget")
        titleBar = dock.titleBarWidget()
        stateBefore = mainWindow.saveState()
        closeSceneAction = mainWindow.findChild("QAction", "FileCloseSceneAction")
        addDataAction = mainWindow.findChild("QAction", "FileAddDataAction")
        addDataShortcuts = [portableKey(key) for key in addDataAction.shortcuts()]
        # Menu actions with a window shortcut are also actions of the main window in every mode (W3-03), so that their
        # keys work while the menubar is hidden
        self.assertIn(mainWindow, shell.associatedWidgets(closeSceneAction))

        studioShell = self._applyStudio()
        self.assertIs(shell.instance(), studioShell)
        self.assertIsNotNone(studioShell.applyDurationMs)
        topBar = mainWindow.findChild("QToolBar", "StudioTopBar")
        self.assertIsNotNone(topBar)
        self.assertEqual(topBar.height, 44)
        self.assertTrue(topBar.property("SlicerKeepInFocusMode"))
        self.assertTrue(mainWindow.menuBar().isHidden())
        self.assertTrue(mainWindow.statusBar().isHidden())
        self.assertTrue(mainWindow.findChild("QWidget", "DataProbeCollapsibleWidget").isHidden())
        for toolBar in mainWindow.findChildren("QToolBar"):
            if toolBar != topBar:
                self.assertTrue(toolBar.isHidden(), toolBar.objectName)
        self.assertEqual(mainWindow.dockWidgetArea(dock), qt.Qt.RightDockWidgetArea)
        self.assertEqual(dock.titleBarWidget().objectName, "StudioPanelHeader")
        # The module panel starts below the 40 px panel header (QDockWidget uses its size hint)
        self.assertGreaterEqual(dock.widget().mapTo(dock, qt.QPoint(0, 0)).y(), 40)
        self.assertIn(mainWindow, shell.associatedWidgets(closeSceneAction))
        self.assertEqual(list(addDataAction.shortcuts()), [])
        self.assertEqual(studioShell.currentWorkspace(), "library")
        # Library empty state (nothing loaded): the inspector is hidden (SPEC 2.5, StudioLib/library.py)
        self.assertTrue(dock.isHidden())
        studioShell.setInspectorVisible(True)
        self._pump()
        # Ctrl+5 (View > Module Panel) keeps toggling the inspector
        panelToggle = dock.toggleViewAction()
        self.assertEqual(portableKey(panelToggle.shortcut), "Ctrl+5")
        self.assertTrue(panelToggle.enabled)
        self.assertIn(mainWindow, shell.associatedWidgets(panelToggle))
        panelToggle.trigger()
        self._pump()
        self.assertTrue(dock.isHidden())
        panelToggle.trigger()
        self._pump()
        self.assertFalse(dock.isHidden())

        studioShell.classic(persist=False)
        self._pump()
        self.assertIsNone(shell.instance())
        self.assertEqual(mainWindow.saveState(), stateBefore)
        self.assertTrue(dock.titleBarWidget() == titleBar)
        self.assertFalse(titleBar.isHidden())
        self.assertFalse(mainWindow.menuBar().isHidden())
        self.assertIsNone(mainWindow.findChild("QToolBar", "StudioTopBar"))
        self.assertIn(mainWindow, shell.associatedWidgets(closeSceneAction))
        self.assertEqual([portableKey(key) for key in addDataAction.shortcuts()], addDataShortcuts)
        self.assertEqual(mainWindow.dockWidgetArea(dock), qt.Qt.LeftDockWidgetArea)

        # Back to Studio and Classic again
        studioShell.studio()
        self._pump()
        # The saved state holds a cached minimum size of the module panel, which restoreState() ignores: it is only
        # byte-identical if the inspector was laid out while shown (it is hidden in the Library empty state)
        studioShell.setInspectorVisible(True)
        self._pump()
        self.assertIsNotNone(mainWindow.findChild("QToolBar", "StudioTopBar"))
        studioShell.classic(persist=False)
        self._pump()
        self.assertEqual(mainWindow.saveState(), stateBefore)

    def test_3_workspace_switching(self):
        """switchTo() selects the workspace module and changes the layout only when needed (SPEC 2.6)."""
        from StudioLib import layouts

        studioShell = self._applyStudio()
        layoutChanges = []
        onLayoutChanged = layoutChanges.append
        self.layoutManager.connect("layoutChanged(int)", onLayoutChanged)
        try:
            self.assertTrue(studioShell.isRenderPausedByCanvas())
            studioShell.switchTo("draw")
            self._pump()
            self.assertEqual(studioShell.currentModuleName(), "SegmentEditor")
            self.assertEqual(studioShell.currentWorkspace(), "draw")
            self.assertEqual(self.layoutManager.layout, layouts.FOCUS_LAYOUT_ID)
            self.assertFalse(studioShell.isRenderPausedByCanvas())
            self.assertTrue(studioShell.topBar.undoButton.isVisibleTo(studioShell.topBar.toolBar))
            dock = self.mainWindow.findChild("QDockWidget", "PanelDockWidget")
            drawPanelWidth = dock.width
            studioShell.switchTo("library")
            self._pump()
            self.assertEqual(studioShell.currentModuleName(), "Studio")
            self.assertEqual(studioShell.currentWorkspace(), "library")
            self.assertTrue(studioShell.isRenderPausedByCanvas())
            # The inspector keeps its width between workspaces (the views are not resized)
            self.assertEqual(dock.width, drawPanelWidth)
            self.assertLessEqual(len(layoutChanges), 1)
            # Already on the remembered layout: no layout change
            studioShell.switchTo("draw")
            self._pump()
            self.assertLessEqual(len(layoutChanges), 1)

            # A module selected by other means is transient, the close button returns to the workspace
            slicer.util.selectModule("Data")
            self._pump()
            self.assertIsNone(studioShell.currentWorkspace())
            self.assertTrue(studioShell.panelHeader.closeButton.isVisibleTo(studioShell.panelHeader.widget))
            self.assertFalse(any(button.checked for button in studioShell.topBar.workspaceButtons.values()))
            studioShell.closeTransientModule()
            self._pump()
            self.assertEqual(studioShell.currentWorkspace(), "draw")
            self.assertEqual(studioShell.currentModuleName(), "SegmentEditor")
            # Leaving a transient module shrinks the inspector back to the stored width (or what Draw needs)
            self.assertEqual(dock.width, max(studioShell.panelWidth, dock.minimumSizeHint.width()))

            # Selecting a workspace module by other means checks its tab
            slicer.util.selectModule("Studio")
            self._pump()
            self.assertEqual(studioShell.currentWorkspace(), "library")
            self.assertTrue(studioShell.topBar.workspaceButtons["library"].checked)

            # The layout chosen in a workspace is remembered
            studioShell.switchTo("draw")
            self._pump()
            self.layoutManager.setLayout(layouts.ROW_LAYOUT_ID)
            self._pump()
            self.assertEqual(slicer.util.settingsValue("Studio/Draw/Layout", 0, converter=int), layouts.ROW_LAYOUT_ID)
            qt.QSettings().remove("Studio/Draw/Layout")
        finally:
            self.layoutManager.disconnect("layoutChanged(int)", onLayoutChanged)

    def test_4_no_key_collisions(self):
        """Studio shortcuts are disjoint from the view and Segment Editor keys, and not ambiguous (SPEC 9.6)."""
        from StudioLib import workspaces

        studioShell = self._applyStudio()
        # Ctrl+V (paste a figure, L-29) is enabled in Draw only while the clipboard holds an image: give it one
        clipboard = qt.QApplication.clipboard()
        image = qt.QImage(8, 8, qt.QImage.Format_RGB32)
        image.fill(qt.QColor(255, 255, 255))
        clipboard.setImage(image)
        self.addCleanup(clipboard.clear)
        self.assertEqual(keyCollisions([("Ctrl+Z", "self check")]), [("Ctrl+Z", "self check", "Ctrl+Z", "Segment Editor shortcut")])
        self.assertTrue(keyCollisions([("Ctrl+]", "any modifier check")]))
        # Keysym "V" needs Shift (whatever else is held); Ctrl+V gives "v", which the views bind without modifier only
        self.assertTrue(keyCollisions([("Ctrl+Shift+V", "any modifier check")]))
        self.assertTrue(keyCollisions([("V", "plain key check")]))
        self.assertEqual(keyCollisions([("Ctrl+V", "paste check")]), [])
        for ws in workspaces.WORKSPACES:
            if not workspaces.isAvailable(ws):
                continue
            studioShell.switchTo(ws.key)
            self._pump()
            assertNoKeyCollisions()
            classicSwitch = self.mainWindow.findChild("QAction", "StudioInterfaceAction")
            keys = studioShortcutKeys(studioShell)
            if ws.key == "draw":
                # Every Draw key is installed (SPEC 5.4) and checked
                installed = {portableKey(key) for key, _description in keys}
                expected = [key for key in DRAW_KEYS if (key != "M" or slicer.modules.SegmentEditorWidget.editor.effectByName("Smart fill") is not None)
                            and (key != "Ctrl+V" or _hasFigureTracing())]
                self.assertEqual([key for key in expected if portableKey(key) not in installed], [])
            if classicSwitch is not None:
                keys.append((classicSwitch.shortcut, "Classic switch"))
            assertNoKeyCollisions(keys)
            self.assertEqual(ambiguousShortcuts(self.mainWindow), {}, f"ambiguous shortcuts in {ws.title}")

    def test_5_layouts(self):
        """The Studio layouts are registered with the canonical XML (SPEC 9.7 rule 3)."""
        from StudioLib import layouts

        layoutNode = self.layoutManager.layoutLogic().GetLayoutNode()
        self.assertEqual(layouts.register(layoutNode), [])
        for layoutId, (title, description) in layouts.LAYOUTS.items():
            self.assertTrue(layoutNode.IsLayoutDescription(layoutId))
            self.assertEqual(layoutNode.GetLayoutDescription(layoutId), description, title)
        self.assertEqual(layouts.pickerLayoutId("single"), slicer.vtkMRMLLayoutNode.SlicerLayoutOneUpRedSliceView)
        self.assertEqual(layouts.singleLayoutId("Yellow"), slicer.vtkMRMLLayoutNode.SlicerLayoutOneUpYellowSliceView)

    def test_6_shell_api(self):
        """Inspector footer, canvas, toast, breadcrumb, status chip, primary action and workspace shortcuts."""
        studioShell = self._applyStudio()
        studioShell.switchTo("draw")
        self._pump()

        footer = qt.QLabel("footer")
        studioShell.setInspectorFooter(footer)
        self._pump()
        self.assertTrue(footer.isVisible())
        studioShell.setInspectorFooter(None)
        self.assertFalse(footer.isVisible())

        canvas = qt.QFrame()
        studioShell.setCanvas(canvas)
        self._pump()
        self.assertTrue(canvas.isVisible())
        self.assertEqual(canvas.geometry, self.mainWindow.centralWidget().rect)
        self.assertTrue(studioShell.isRenderPausedByCanvas())
        studioShell.setCanvas(None)
        self.assertFalse(canvas.isVisible())
        self.assertFalse(studioShell.isRenderPausedByCanvas())

        studioShell.toast("Undo · Brush", 5000)
        self._pump()
        self.assertTrue(studioShell._toast.isVisible())
        self.assertEqual(studioShell._toast.text(), "Undo · Brush")

        studioShell.setBreadcrumb("Study", "sub-001")
        self.assertIn("sub-001", studioShell.topBar.breadcrumbButton.text)
        studioShell.setBreadcrumb(None)
        studioShell.setStatusChip("Ready", "ok")
        self.assertTrue(studioShell.topBar.statusChip.isVisibleTo(studioShell.topBar.toolBar))
        studioShell.setStatusChip(None)
        self.assertFalse(studioShell.topBar.statusChip.isVisibleTo(studioShell.topBar.toolBar))

        calls = []
        studioShell.setPrimaryAction(lambda: calls.append("primary"), "Map network →")
        studioShell.triggerPrimaryAction()
        self.assertEqual(calls, ["primary"])

        studioShell.addWorkspaceShortcut("draw", "N", lambda: calls.append("N"), "next gap")
        self.assertIn("N", [portableKey(shortcut.key) for shortcut in studioShell.shortcuts()])
        studioShell.switchTo("library")
        self._pump()
        self.assertNotIn("N", [portableKey(shortcut.key) for shortcut in studioShell.shortcuts()])
        # The primary action belongs to the workspace that set it
        studioShell.triggerPrimaryAction()
        self.assertEqual(calls, ["primary"])
        studioShell.switchTo("draw")
        self._pump()
        self.assertIn("N", [portableKey(shortcut.key) for shortcut in studioShell.shortcuts()])

        studioShell.setInspectorVisible(False)
        self.assertTrue(self.mainWindow.findChild("QDockWidget", "PanelDockWidget").isHidden())
        studioShell.setInspectorVisible(True)
        self.assertFalse(self.mainWindow.findChild("QDockWidget", "PanelDockWidget").isHidden())

    def test_7_widget_without_main_window(self):
        """The Library module widget can be created without main window (generic test path)."""
        import Studio

        originalMainWindow = slicer.util.mainWindow
        slicer.util.mainWindow = lambda *args, **kwargs: None
        try:
            widget = Studio.StudioWidget()
            self._pump()
            self.assertTrue(widget.parent.isVisible())
            if widget.libraryPanel is None:
                self.assertIsNotNone(widget.openButton)
                self.assertIsNotNone(widget.templateButton)
            widget.parent.close()
            # Not deleted from C++ (deleteLater): the parent is owned by Python, and PythonQt would
            # delete it a second time when the last reference to the module widget is released
            _keepAlive.append(widget)
        finally:
            slicer.util.mainWindow = originalMainWindow

    def test_8_ui_helpers(self):
        """Tokens, tinted icons and roles."""
        from StudioLib import ui

        self.assertTrue(ui.token("canvas").isValid())
        self.assertTrue(ui.token("text").isValid())
        with self.assertRaises(ValueError):
            ui.token("no-such-token")
        icon = ui.icon("brush", 18)
        self.assertFalse(icon.isNull())
        self.assertIs(ui.icon("brush", 18), icon)
        for name in ("library", "brush", "waypoints", "menu", "undo-2", "redo-2", "layout-panel-left", "search", "circle-alert", "x", "circle-help", "ellipsis"):
            self.assertFalse(ui.icon(name, 16).isNull(), name)
        # Checked icons are tinted with accent-fg, normal icons with text2
        normal = ui.icon("brush", 18).pixmap(18, 18, qt.QIcon.Normal, qt.QIcon.Off).toImage()
        checked = ui.icon("brush", 18).pixmap(18, 18, qt.QIcon.Normal, qt.QIcon.On).toImage()
        opaque = [(x, y) for x in range(18) for y in range(18) if qt.QColor.fromRgba(normal.pixel(x, y)).alpha() == 255]
        self.assertTrue(opaque)
        x, y = opaque[0]
        self.assertEqual(qt.QColor.fromRgba(normal.pixel(x, y)).name(), ui.token("text2").name())
        self.assertEqual(qt.QColor.fromRgba(checked.pixel(x, y)).name(), ui.token("accent-fg").name())

        frame, _layout = ui.card("Lesion")
        self.assertEqual(frame.property("slicerRole"), "card")
        button = ui.primaryButton("Done")
        self.assertEqual(button.property("slicerRole"), "primary")
        if not ui.supportsRoles():
            self.assertIn('slicerRole="primary"', button.styleSheet)
        chip = ui.chip("Ready", "ok")
        ui.setChipLevel(chip, "warn")
        self.assertEqual(chip.property("chipLevel"), "warn")
        frame, group = ui.segmented(["A", "B"])
        self.assertEqual(len(group.buttons()), 2)
        with self.assertRaises(ValueError):
            ui.setRole(qt.QWidget(), "no-such-role")

    def test_9_late_toolbars(self):
        """Toolbars that extensions add when Studio is shown are hidden, and shown again in Classic (SPEC 2.3)."""
        mainWindow = self.mainWindow
        studioShell = self._applyStudio()
        added = mainWindow.addToolBar("Studio test toolbar")
        added.setObjectName("StudioTestAddedToolBar")
        constructed = qt.QToolBar("Studio test toolbar 2", mainWindow)
        constructed.setObjectName("StudioTestConstructedToolBar")
        mainWindow.addToolBar(constructed)
        hiddenByOwner = qt.QToolBar("Studio test toolbar 3", mainWindow)
        hiddenByOwner.setObjectName("StudioTestHiddenToolBar")
        hiddenByOwner.hide()
        mainWindow.addToolBar(hiddenByOwner)
        removedByOwner = mainWindow.addToolBar("Studio test toolbar 4")
        removedByOwner.setObjectName("StudioTestRemovedToolBar")
        reparentedByOwner = mainWindow.addToolBar("Studio test toolbar 5")
        reparentedByOwner.setObjectName("StudioTestReparentedToolBar")
        container = qt.QWidget()
        toolBars = (added, constructed, hiddenByOwner, removedByOwner, reparentedByOwner)
        try:
            self._pump()
            for toolBar in toolBars:
                self.assertTrue(toolBar.isHidden(), toolBar.objectName)
            # Owners take their toolbars out of the main window while Studio is shown
            mainWindow.removeToolBar(removedByOwner)
            reparentedByOwner.setParent(container)
            studioShell.classic(persist=False)
            self._pump()
            self.assertTrue(added.isVisible())
            self.assertTrue(constructed.isVisible())
            self.assertTrue(hiddenByOwner.isHidden())
            # Not shown again outside the toolbar areas
            self.assertTrue(removedByOwner.isHidden())
            self.assertTrue(reparentedByOwner.isHidden())
        finally:
            for toolBar in toolBars:
                mainWindow.removeToolBar(toolBar)
                toolBar.deleteLater()
            container.deleteLater()
            self._pump()

    #
    # Draw (StudioLib/draw.py) and the importer
    #

    def _scanFile(self, name="sub-07_T1w.nii.gz"):
        """Write a small synthetic scan to a temporary NIfTI file and return its path."""
        import tempfile

        import numpy as np

        directory = tempfile.mkdtemp(prefix="StudioTest", dir=slicer.app.temporaryPath)
        path = f"{directory}/{name}"
        node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "StudioTestScan")
        z, y, x = np.mgrid[0:24, 0:28, 0:20]
        slicer.util.updateVolumeFromArray(node, (100 * np.exp(-((x - 10) ** 2 + (y - 14) ** 2 + (z - 12) ** 2) / 50.0)).astype(np.int16))
        node.SetSpacing(2.0, 2.0, 2.5)
        node.SetOrigin(-20.0, -28.0, -30.0)
        self.assertTrue(slicer.util.saveNode(node, path))
        slicer.mrmlScene.RemoveNode(node)
        return path

    def _openScanInDraw(self, studioShell):
        """Open a scan with the importer (it switches to Draw) and return (path, editor)."""
        from StudioLib import importer

        path = self._scanFile()
        result = importer.loadFiles([path], studioShell)
        self._pump(8)
        self.assertEqual(len(result.volumes), 1)
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        return path, slicer.modules.SegmentEditorWidget.editor

    @staticmethod
    def _effect(editor):
        return editor.activeEffect().name if editor.activeEffect() is not None else None

    @staticmethod
    def _paintCube(editor, size=4):
        segmentationNode, segmentId, sourceVolumeNode = editor.segmentationNode(), editor.currentSegmentID(), editor.sourceVolumeNode()
        array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, sourceVolumeNode)
        array[8:8 + size, 10:10 + size, 6:6 + size] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, sourceVolumeNode)
        return int(slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, sourceVolumeNode).sum())

    def _pressSpace(self, editor):
        """Trigger the Segment Editor's own Space shortcut, as the key does."""
        shortcuts = [shortcut for shortcut in self.mainWindow.findChildren("QShortcut")
                     if shortcut.property("effectIndex") == -1 and shortcut.key.toString(qt.QKeySequence.PortableText) == "Space"]
        self.assertEqual(len(shortcuts), 1, "the Segment Editor installs one Space shortcut while Draw is shown")
        shortcuts[0].activated()
        self._pump()

    def test_10a_space_pauses_after_tool_switch(self):
        """Space pauses the active tool and resumes it, also after a switch of tools (it must not activate the
        previous tool: a drag meant to adjust contrast would then erase the lesion).
        """
        studioShell = self._applyStudio()
        _path, editor = self._openScanInDraw(studioShell)
        editor.setActiveEffectByName("Paint")
        self._pump()
        self._pressSpace(editor)
        self.assertIsNone(self._effect(editor))
        self._pressSpace(editor)
        self.assertEqual(self._effect(editor), "Paint")

        # After any switch (E, E: Erase and back to Brush; or the tiles) Space still pauses the current tool
        for previous, current in (("Erase", "Paint"), ("Draw", "Paint"), ("Paint", "Erase")):
            editor.setActiveEffectByName(previous)
            self._pump()
            editor.setActiveEffectByName(current)
            self._pump()
            self._pressSpace(editor)
            self.assertIsNone(self._effect(editor), f"Space after {previous} -> {current} must pause")
            self._pressSpace(editor)
            self.assertEqual(self._effect(editor), current, f"Space resumes {current}")

        # Esc, then Space resumes the deselected tool
        editor.setActiveEffect(None)
        self._pump()
        self._pressSpace(editor)
        self.assertEqual(self._effect(editor), "Erase")
        editor.setActiveEffect(None)
        self._pump()

    def test_10_draw_round_trips(self):
        """A dropped scan lands unarmed with Brush suggested; the chosen tool and segment survive round trips; Esc is
        respected; a lesion opened to be traced (template, New lesion) arms Brush (NAV-02, NFT-03, D-U2).
        """
        from StudioLib import draw, importer

        studioShell = self._applyStudio()
        _path, editor = self._openScanInDraw(studioShell)
        widget = slicer.modules.SegmentEditorWidget
        # A plain scan lands with no tool armed: the Brush tile is suggested (outlined, not checked) and the card says how to start
        self.assertIsNone(self._effect(editor))
        self.assertIsNotNone(widget.tileOutline("Paint"))
        self.assertIn("Pick Brush (P)", draw.extension().card.hintLabel.text)
        segmentationId, segmentId = editor.segmentationNodeID(), editor.currentSegmentID()
        self.assertTrue(segmentId)

        editor.setActiveEffectByName("Erase")
        self._pump()
        self.assertIsNone(widget.tileOutline("Paint"))
        studioShell.switchTo("library")
        self._pump()
        self.assertIsNone(editor.activeEffect())
        self.assertEqual(draw.drawState(studioShell).effect, "Erase")
        studioShell.switchTo("draw")
        self._pump()
        self.assertEqual(self._effect(editor), "Erase")
        self.assertEqual((editor.segmentationNodeID(), editor.currentSegmentID()), (segmentationId, segmentId))

        # A transient module: the Segment Editor is left (its exit() drops the effect) before the Draw hook runs
        slicer.util.selectModule("Volumes")
        self._pump()
        self.assertIsNone(studioShell.currentWorkspace())
        studioShell.closeTransientModule()
        self._pump()
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        self.assertEqual(self._effect(editor), "Erase")
        self.assertEqual((editor.segmentationNodeID(), editor.currentSegmentID()), (segmentationId, segmentId))

        # Classic round trip
        studioShell.classic(persist=False)
        self._pump()
        self.assertIsNone(self.mainWindow.findChild("QPushButton", "StudioSaveLesionButton"))
        studioShell.studio()
        self._pump()
        studioShell.switchTo("draw")
        self._pump()
        self.assertEqual(self._effect(editor), "Erase")

        # Esc (the Segment Editor's deactivate shortcut) inside Draw: the tool stays deselected, Space resumes it (dashed outline)
        editor.setActiveEffect(None)
        self._pump()
        studioShell.switchTo("library")
        self._pump()
        studioShell.switchTo("draw")
        self._pump()
        self.assertIsNone(editor.activeEffect())
        self.assertIsNotNone(widget.tileOutline("Erase"))

        # A tool that the editor dropped by itself is armed again on a lesion opened to be traced ("Lesion 2", New lesion)
        editor.setActiveEffectByName("Paint")
        self._pump()
        otherSegmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Lesion 2")
        otherSegmentation.SetReferenceImageGeometryParameterFromVolumeNode(editor.sourceVolumeNode())
        editor.setSegmentationNode(otherSegmentation)
        editor.setCurrentSegmentID(otherSegmentation.GetSegmentation().AddEmptySegment("", "Lesion 2"))
        self._pump()
        self.assertEqual(self._effect(editor), "Paint")

        # A new patient scan dropped while drawing lands unarmed (its first drag navigates, D-U2)
        importer.loadFiles([self._scanFile("sub-08_T1w.nii.gz")], studioShell)
        self._pump(8)
        self.assertEqual(editor.sourceVolumeNode().GetName(), "sub-08_T1w")
        self.assertIsNone(self._effect(editor))

        # ... also after the scene is closed (node IDs are reused) and when opened while Draw is not shown
        slicer.mrmlScene.Clear(0)
        importer.loadFiles([self._scanFile("sub-09_T1w.nii.gz")], studioShell)
        self._pump(8)
        self.assertIsNone(self._effect(editor))
        studioShell.switchTo("library")
        self._pump()
        importer.loadFiles([self._scanFile("sub-10_T1w.nii.gz")], studioShell)
        self._pump(8)
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        self.assertIsNone(self._effect(editor))

        # New lesion (the card's ⋯, Search) arms Brush
        draw.newLesion(studioShell)
        self._pump(8)
        self.assertEqual(self._effect(editor), "Paint")
        self.assertTrue(editor.segmentationNode().GetName().startswith("Lesion "))

    def test_11_save_lesion(self):
        """Save lesion writes a NIfTI mask on the lesion grid and a sidecar, no node left behind; empty lesions are not
        written; the second save of a session is silent; the card follows Saved / Unsaved changes (NFT-01, CRT-05, U3, U4).
        """
        import json
        import os

        import numpy as np

        from StudioLib import draw

        studioShell = self._applyStudio()
        path, editor = self._openScanInDraw(studioShell)
        # The footer and the primary action belong to Draw
        saveButton = self.mainWindow.findChild("QPushButton", "StudioSaveLesionButton")
        self.assertIsNotNone(saveButton)
        self.assertTrue(saveButton.isVisible())
        self.assertTrue(saveButton.enabled)
        self.assertEqual(saveButton.text, studioShell.primaryActionLabel())
        self.assertTrue(studioShell.primaryActionLabel().startswith("Save lesion · "))
        self.assertIn("Ctrl+Shift+S", [portableKey(shortcut.key) for shortcut in studioShell.shortcuts()])
        segmentationNode, sourceVolumeNode = editor.segmentationNode(), editor.sourceVolumeNode()
        self.assertEqual(draw.lesionSaveState(segmentationNode)[0], "never")

        settings = qt.QSettings()
        previousFolder = settings.value(draw.LAST_SAVE_FOLDER_KEY)
        settings.remove(draw.LAST_SAVE_FOLDER_KEY)
        try:
            # An empty lesion: nothing is written
            emptyPath = os.path.join(os.path.dirname(path), "empty", "sub-07_lesion.nii.gz")
            self.assertIsNone(draw.saveLesionDialog(studioShell, path=emptyPath))
            self.assertFalse(os.path.exists(emptyPath))
            self.assertEqual(studioShell.toastText(), "The lesion is empty: nothing was saved")

            voxels = self._paintCube(editor)
            self.assertEqual(voxels, 64)
            self.assertEqual(draw.defaultLesionFileName(segmentationNode, sourceVolumeNode), "sub-07_lesion.nii.gz")
            sourceVolumeNode.SetName("patient_T1")
            segmentationNode.SetName("Lesions (patient_T1)")
            sourceVolumeNode.GetStorageNode().SetFileName(path.replace("sub-07_T1w", "patient_T1"))
            self.assertEqual(draw.defaultLesionFileName(segmentationNode, sourceVolumeNode), "patient_T1_lesion.nii.gz")
            segmentationNode.SetName("Lesion 2")
            self.assertEqual(draw.defaultLesionFileName(segmentationNode, sourceVolumeNode), "patient_T1_lesion.nii.gz")
            sourceVolumeNode.GetStorageNode().SetFileName(path)
            sourceVolumeNode.SetName("sub-07_T1w")

            # The scan is in a temporary folder: the dialog proposes the home folder (or Documents), never the application folder
            directory = draw.defaultSaveDirectory(sourceVolumeNode)
            self.assertFalse(draw._isInside(directory, slicer.app.slicerHome))
            self.assertIn(directory, (qt.QDir.homePath(), os.path.join(qt.QDir.homePath(), "Documents")))

            segmentationNode.SetAttribute(draw.NOTES_ATTRIBUTE, "Left MCA stroke")
            segmentationNode.SetAttribute(draw.SOURCE_REFERENCE_ATTRIBUTE, "doi:10.1000/xyz, Fig. 2")
            nodeCount = slicer.mrmlScene.GetNumberOfNodes()
            outputPath = os.path.join(os.path.dirname(path), "lesions", "sub-07_lesion")
            written = draw.saveLesionDialog(studioShell, path=outputPath)
            self.assertEqual(written, outputPath + ".nii.gz")
            self.assertTrue(os.path.isfile(written))
            self.assertEqual(sorted(os.listdir(os.path.dirname(written))), ["sub-07_lesion.json", "sub-07_lesion.nii.gz"])
            self.assertEqual(slicer.mrmlScene.GetNumberOfNodes(), nodeCount)
            self.assertEqual(slicer.util.settingsValue(draw.LAST_SAVE_FOLDER_KEY, ""), os.path.dirname(written))
            self.assertEqual(draw.defaultSaveDirectory(sourceVolumeNode), os.path.dirname(written))
            self.assertEqual(studioShell.toastText(), "Saved sub-07_lesion.nii.gz")
            # Sidecar: the scan relative to the sidecar, the space, the notes; no absolute path
            with open(os.path.join(os.path.dirname(written), "sub-07_lesion.json"), encoding="utf-8") as f:
                sidecar = json.load(f)
            self.assertEqual(sidecar["Source"], "../sub-07_T1w.nii.gz")
            self.assertEqual(sidecar["Space"], "scanner")
            self.assertEqual(sidecar["Notes"], "Left MCA stroke")
            self.assertEqual(sidecar["SourceReference"], "doi:10.1000/xyz, Fig. 2")
            self.assertTrue(sidecar["GeneratedBy"].startswith("Slicer Studio"))
            # Saved state: attributes hold the base name and the time only
            self.assertEqual(segmentationNode.GetAttribute(draw.SAVED_FILE_ATTRIBUTE), "sub-07_lesion.nii.gz")
            self.assertEqual(draw.lesionSaveState(segmentationNode)[:2], ("saved", written))
            self._pump(4)
            self.assertTrue(draw.extension().card.saveStateLabel.text.startswith("Saved · sub-07_lesion.nii.gz"))

            labelmapNode = slicer.util.loadLabelVolume(written, {"show": False})
            try:
                array = slicer.util.arrayFromVolume(labelmapNode)
                self.assertEqual(int(np.count_nonzero(array)), voxels)
                self.assertEqual(sorted(np.unique(array).tolist()), [0, 1])
                self.assertEqual(labelmapNode.GetImageData().GetDimensions(), sourceVolumeNode.GetImageData().GetDimensions())
                for actual, expected in ((labelmapNode.GetSpacing(), sourceVolumeNode.GetSpacing()), (labelmapNode.GetOrigin(), sourceVolumeNode.GetOrigin())):
                    self.assertTrue(np.allclose(actual, expected), (actual, expected))
            finally:
                slicer.mrmlScene.RemoveNode(labelmapNode)
            with open(written, "rb") as f:
                import gzip

                header = gzip.decompress(f.read())[:352]
            # NIfTI datatype 2: unsigned char
            self.assertEqual(int.from_bytes(header[70:72], "little"), 2)

            # One more stroke: "Unsaved changes"; the CTA (Ctrl+Enter) re-saves silently to the same file
            editor.saveStateForUndo()
            array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, editor.currentSegmentID(), sourceVolumeNode)
            array[2:4, 2:4, 2:4] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, editor.currentSegmentID(), sourceVolumeNode)
            self.assertEqual(draw.lesionSaveState(segmentationNode)[0], "unsaved")
            self._pump(4)
            slicer.app.processEvents()
            self._wait(draw.METRICS_DEBOUNCE_MS + 100)
            self.assertTrue(draw.extension().card.saveStateLabel.text.startswith("Unsaved changes"))
            # Notes and source just typed in the card (the notes debounce pending, the source field still being
            # edited) reach the sidecar of a save by shortcut
            card = draw.extension().card
            card.notesEdit.setPlainText("Typed just before Ctrl+Enter")
            card.sourceReferenceEdit.selectAll()
            card.sourceReferenceEdit.insert("10.1000/typed")
            self.assertTrue(card.sourceReferenceEdit.isModified())
            studioShell.triggerPrimaryAction()
            self._pump()
            self.assertEqual(draw.lesionSaveState(segmentationNode)[:2], ("saved", written))
            self.assertEqual(sorted(os.listdir(os.path.dirname(written))), ["sub-07_lesion.json", "sub-07_lesion.nii.gz"])
            with open(os.path.join(os.path.dirname(written), "sub-07_lesion.json"), encoding="utf-8") as f:
                sidecar = json.load(f)
            self.assertEqual(sidecar["Notes"], "Typed just before Ctrl+Enter")
            self.assertEqual(sidecar["SourceReference"], "10.1000/typed")
            self.assertFalse(card.sourceReferenceEdit.isModified())
            # Notes typed just before another lesion is shown stay with the lesion they were typed for
            otherNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Other lesion")
            try:
                card.notesEdit.setPlainText("For sub-07")
                card.refresh({"lesionId": otherNode.GetID(), "notes": "", "sourceReference": ""})
                self.assertEqual(segmentationNode.GetAttribute(draw.NOTES_ATTRIBUTE), "For sub-07")
                self.assertIsNone(otherNode.GetAttribute(draw.NOTES_ATTRIBUTE))
                self.assertEqual(card.notesEdit.toPlainText(), "")
                card.refresh({"lesionId": segmentationNode.GetID(), "notes": "For sub-07", "sourceReference": "10.1000/typed"})
            finally:
                slicer.mrmlScene.RemoveNode(otherNode)
            # After the scene is saved and reopened: "Last saved as …" from the attributes
            draw._savedRecords.clear()
            self.assertEqual(draw.lesionSaveState(segmentationNode)[:2], ("previous", "sub-07_lesion.nii.gz"))
            self.assertTrue(draw.saveStateText(segmentationNode)[0].startswith("Last saved as sub-07_lesion.nii.gz"))
        finally:
            if previousFolder is None:
                settings.remove(draw.LAST_SAVE_FOLDER_KEY)
            else:
                settings.setValue(draw.LAST_SAVE_FOLDER_KEY, previousFolder)

        # Several segments: values 1, 2... (the later segment wins where they overlap), and the toast says so
        segmentationNode.GetSegmentation().AddEmptySegment("", "Second lesion")
        written = draw.saveLesionDialog(studioShell, path=os.path.join(os.path.dirname(path), "lesions", "two.nii.gz"))
        self.assertIsNotNone(written)
        self.assertIn("2 segments", studioShell.toastText())

        # The footer is deleted when Draw is left, and created again on the next entry
        from StudioLib import ui

        footerWidget = draw.session(studioShell).footer.widget
        studioShell.switchTo("library")
        self._pump()
        slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
        self.assertIsNone(self.mainWindow.findChild("QPushButton", "StudioSaveLesionButton"))
        self.assertIsNone(draw.session(studioShell).footer)
        self.assertFalse([entry for entry in ui._boundStyleSheets if entry[0] is footerWidget])
        self.assertNotIn("Ctrl+Shift+S", [portableKey(shortcut.key) for shortcut in studioShell.shortcuts()])
        studioShell.switchTo("draw")
        self._pump()
        self.assertIsNotNone(self.mainWindow.findChild("QPushButton", "StudioSaveLesionButton"))

    def test_12_recent_already_open(self):
        """A Recent row or an Open… of a file that is already loaded does not load a duplicate in Studio (NAV-05)."""
        from StudioLib import importer

        studioShell = self._applyStudio()
        path, editor = self._openScanInDraw(studioShell)
        voxels = self._paintCube(editor)
        segmentationNode = editor.segmentationNode()
        counts = lambda: (len(slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")), len(slicer.util.getNodesByClass("vtkMRMLSegmentationNode")))
        before = counts()
        studioShell.switchTo("library")
        self._pump()

        result = importer.loadRecentFile({"fileName": path, "fileType": "VolumeFile"}, None, studioShell)
        self._pump()
        self.assertEqual(counts(), before)
        self.assertEqual([node.GetName() for node in result.alreadyOpen], ["sub-07_T1w"])
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        self.assertEqual(editor.segmentationNode().GetID(), segmentationNode.GetID())
        self.assertEqual(int(slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, editor.currentSegmentID(), editor.sourceVolumeNode()).sum()), voxels)
        self.assertEqual(studioShell._toast.text(), "sub-07_T1w is already open")

        result = importer.loadFiles([path.replace("/", "//")], studioShell)
        self._pump()
        self.assertEqual(counts(), before)
        self.assertEqual(len(result.alreadyOpen), 1)

        # Classic keeps the official behavior: the file is loaded again
        studioShell.classic(persist=False)
        self._pump()
        result = importer.loadFiles([path])
        self._pump()
        self.assertEqual(len(result.volumes), 1)
        self.assertEqual(counts()[0], before[0] + 1)

    def test_13_draw_redo_shortcut(self):
        """Ctrl+Y redoes in Draw without an ambiguous key, and is removed outside Draw (NFT-07)."""
        from StudioLib import draw

        studioShell = self._applyStudio()
        _path, editor = self._openScanInDraw(studioShell)
        shortcut = self.mainWindow.findChild("QShortcut", "StudioDrawRedoShortcut")
        if qt.QKeySequence(qt.QKeySequence.Redo).toString(qt.QKeySequence.PortableText) == "Ctrl+Y":
            # The Segment Editor binds Ctrl+Y itself (Windows): no second shortcut
            self.assertIsNone(shortcut)
            return
        self.assertIsNotNone(shortcut)
        self.assertEqual(portableKey(shortcut.key), "Ctrl+Y")
        owners = [other for other in self.mainWindow.findChildren("QShortcut") if other.enabled and portableKey(other.key) == "Ctrl+Y"]
        owners += [action for action in self.mainWindow.actions() if action.enabled and "Ctrl+Y" in [portableKey(key) for key in action.shortcuts()]]
        self.assertEqual(len(owners), 1)

        editor.saveStateForUndo()
        voxels = self._paintCube(editor)
        segmentationNode, segmentId, sourceVolumeNode = editor.segmentationNode(), editor.currentSegmentID(), editor.sourceVolumeNode()
        count = lambda: int(slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, sourceVolumeNode).sum())
        editor.undo()
        self.assertEqual(count(), 0)
        # Not while the user types in a text field (Ctrl+Y must not change the lesion then)
        lineEdit = qt.QLineEdit(self.mainWindow)
        lineEdit.show()
        self.mainWindow.activateWindow()
        lineEdit.setFocus()
        self._pump()
        if slicer.app.focusWidget() == lineEdit:
            draw._redo()
            self.assertEqual(count(), 0)
        lineEdit.hide()
        lineEdit.setParent(None)
        lineEdit.deleteLater()
        self._pump()
        draw._redo()
        self.assertEqual(count(), voxels)

        studioShell.switchTo("library")
        self._pump()
        self.assertIsNone(self.mainWindow.findChild("QShortcut", "StudioDrawRedoShortcut"))
        studioShell.classic(persist=False)
        self._pump()
        self.assertIsNone(self.mainWindow.findChild("QShortcut", "StudioDrawRedoShortcut"))

    #
    # Files of other installations (SPEC 9.7) and first entries into Draw
    #

    #: Terminology of a "Neoplasm" segment, as official Slicer writes it
    NEOPLASM_TERMINOLOGY = (
        "Segmentation category and type - 3D Slicer General Anatomy list~SCT^49755003^Morphologically Altered Structure"
        "~SCT^108369006^Neoplasm~^^~Anatomic codes - DICOM master list~^^~^^")

    @staticmethod
    def _segmentStates(segmentationNode):
        """[(name, color, terminology)] of the segments of a segmentation."""
        import vtk

        segmentation = segmentationNode.GetSegmentation()
        states = []
        for index in range(segmentation.GetNumberOfSegments()):
            segment = segmentation.GetSegment(segmentation.GetNthSegmentID(index))
            terminology = vtk.mutable("")
            segment.GetTag(slicer.vtkSegment.GetTerminologyEntryTagName(), terminology)
            states.append((segment.GetName(), [round(component, 3) for component in segment.GetColor()], str(terminology)))
        return states

    @staticmethod
    def _errorLogErrors():
        """Descriptions of the Error, Critical and Fatal entries of the error log."""
        model = slicer.app.errorLogModel()
        levelColumn, descriptionColumn = 2, 4  # ctkErrorLogAbstractModel::LogLevelColumn, DescriptionColumn
        return [str(model.index(row, descriptionColumn).data()) for row in range(model.rowCount())
                if model.index(row, levelColumn).data() in ("Error", "Critical", "Fatal")]

    def _coveredByLibrary(self, widget):
        """True if a widget of the Library page is shown over the center of ``widget``."""
        covering = slicer.app.widgetAt(widget.mapToGlobal(qt.QPoint(widget.width // 2, widget.height // 2)))
        while covering is not None:
            if covering.objectName.startswith("StudioLibrary"):
                return True
            covering = covering.parentWidget()
        return False

    def test_14_segmentation_files_kept(self):
        """Segments read from files that store them keep their name, color and terminology; a label map mask gets "Lesion" (F1)."""
        import os

        import numpy as np

        from StudioLib import importer

        scanPath = self._scanFile()
        directory = os.path.dirname(scanPath)
        scan = slicer.util.loadVolume(scanPath, {"show": False})
        mask = np.zeros(slicer.util.arrayFromVolume(scan).shape, np.uint8)
        mask[10:14, 12:16, 8:12] = 1

        # One "Tumor" segment with terminology (what official Slicer writes), as .seg.nrrd and as .nrrd
        segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "OneTumor")
        segmentationNode.CreateDefaultDisplayNodes()
        segmentationNode.SetReferenceImageGeometryParameterFromVolumeNode(scan)
        segmentId = segmentationNode.GetSegmentation().AddEmptySegment("Tumor", "Tumor", [0.1, 0.6, 0.2])
        segmentationNode.GetSegmentation().GetSegment(segmentId).SetTag(slicer.vtkSegment.GetTerminologyEntryTagName(), self.NEOPLASM_TERMINOLOGY)
        slicer.util.updateSegmentBinaryLabelmapFromArray(mask, segmentationNode, segmentId, scan)
        expected = [("Tumor", [0.1, 0.6, 0.2], self.NEOPLASM_TERMINOLOGY)]
        self.assertEqual(self._segmentStates(segmentationNode), expected)
        segmentationFiles = [os.path.join(directory, "OneTumor.seg.nrrd"), os.path.join(directory, "sub-07_lesion.nrrd")]
        for path in segmentationFiles:
            self.assertTrue(slicer.util.saveNode(segmentationNode, path), path)
        # A plain label map mask
        labelmapNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode", "mask")
        slicer.util.updateVolumeFromArray(labelmapNode, mask)
        labelmapNode.CopyOrientation(scan)
        maskPath = os.path.join(directory, "sub-07_mask.nii.gz")
        self.assertTrue(slicer.util.saveNode(labelmapNode, maskPath))
        slicer.mrmlScene.Clear(0)

        for path in segmentationFiles:
            self.assertTrue(importer.storesSegments(path), path)
        self.assertFalse(importer.storesSegments(maskPath))
        self.assertFalse(importer.storesSegments(scanPath))

        studioShell = self._applyStudio()
        for path in segmentationFiles:
            slicer.mrmlScene.Clear(0)
            result = importer.loadFiles([path], studioShell)
            self._pump()
            self.assertEqual(len(result.segmentations), 1, path)
            self.assertEqual(self._segmentStates(result.segmentations[0]), expected, path)
            # The recent files path (Add Data, Recent) keeps them too
            slicer.mrmlScene.Clear(0)
            result = importer.loadRecentFile({"fileName": path, "fileType": "SegmentationFile"}, None, studioShell)
            self._pump()
            self.assertEqual(len(result.segmentations), 1, path)
            self.assertEqual(self._segmentStates(result.segmentations[0]), expected, path)

        slicer.mrmlScene.Clear(0)
        result = importer.loadFiles([maskPath], studioShell)
        self._pump()
        self.assertEqual(len(result.segmentations), 1)
        name, color, _terminology = self._segmentStates(result.segmentations[0])[0]
        self.assertEqual((name, color), (importer.LESION_SEGMENT_NAME, [round(component, 3) for component in importer.LESION_COLOR]))

    def test_15_scene_opened_in_library(self):
        """A scene or a segmentation opened in Library is shown in Draw, a scene in its own layout (F2)."""
        import os

        from StudioLib import importer

        scanPath = self._scanFile()
        directory = os.path.dirname(scanPath)
        scan = slicer.util.loadVolume(scanPath)
        segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Tumor")
        segmentationNode.CreateDefaultDisplayNodes()
        segmentationNode.SetReferenceImageGeometryParameterFromVolumeNode(scan)
        segmentationNode.GetSegmentation().AddEmptySegment("Tumor", "Tumor", [0.1, 0.6, 0.2])
        conventional = slicer.vtkMRMLLayoutNode.SlicerLayoutConventionalView
        self.layoutManager.setLayout(conventional)
        self._pump()
        scenePath = os.path.join(directory, "conventional.mrb")
        self.assertTrue(slicer.util.saveScene(scenePath))
        segmentationPath = os.path.join(directory, "tumor.seg.nrrd")
        self.assertTrue(slicer.util.saveNode(segmentationNode, segmentationPath))
        slicer.mrmlScene.Clear(0)

        studioShell = self._applyStudio()
        # Closing a scene in Draw records the layout of Draw (Studio/Draw/Layout): restored at the end
        settings = qt.QSettings()
        previousDrawLayout = settings.value("Studio/Draw/Layout")
        try:
            for path, layout in ((scenePath, conventional), (segmentationPath, None)):
                slicer.mrmlScene.Clear(0)
                studioShell.switchTo("library")
                self._pump()
                # Library keeps the current layout: another one than the layout of the scene
                self.layoutManager.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
                self._pump()
                self.assertIsNotNone(studioShell.canvas())
                importer.loadFiles([path], studioShell)
                self._pump(8)
                self.assertEqual(studioShell.currentWorkspace(), "draw", path)
                self.assertIsNone(studioShell.canvas(), path)
                self.assertFalse(studioShell.isRenderPausedByCanvas(), path)
                if layout is not None:
                    # The scene keeps its layout (not the remembered Draw layout)
                    self.assertEqual(self.layoutManager.layout, layout)
                for viewName in self.layoutManager.sliceViewNames():
                    sliceWidget = self.layoutManager.sliceWidget(viewName)
                    if sliceWidget.isVisible():
                        self.assertFalse(self._coveredByLibrary(sliceWidget), f"{path}: {viewName}")
                self.assertEqual(len(slicer.util.getNodesByClass("vtkMRMLSegmentationNode")), 1, path)
        finally:
            if previousDrawLayout is None:
                settings.remove("Studio/Draw/Layout")
            else:
                settings.setValue("Studio/Draw/Layout", previousDrawLayout)

    def test_16_draw_entry_logs_no_error(self):
        """Start on MNI template and Library -> Draw, with the mouse pointer where a slice view appears, log no error (UX-B1)."""
        from StudioLib import importer

        studioShell = self._applyStudio()
        studioShell.switchTo("library")
        self._pump()
        # The pointer over the view area: a slice view appears under it when the Library page is hidden
        centralWidget = self.mainWindow.centralWidget()
        qt.QCursor.setPos(centralWidget.mapToGlobal(qt.QPoint(centralWidget.width // 3, centralWidget.height // 2)))
        self._pump()
        errorCount = len(self._errorLogErrors())
        try:
            importer.startOnTemplate(shell=studioShell)
            self._pump(10)
            editor = slicer.modules.SegmentEditorWidget.editor
            self.assertEqual(studioShell.currentWorkspace(), "draw")
            self.assertEqual(self._effect(editor), "Paint")
            self.assertEqual(self._errorLogErrors()[errorCount:], [])

            studioShell.switchTo("library")
            self._pump()
            studioShell.switchTo("draw")
            self._pump(10)
            self.assertEqual(self._effect(editor), "Paint")
            self.assertEqual(self._errorLogErrors()[errorCount:], [])
        finally:
            importer._closeTemplateBanner()

    def test_17_help_popover(self):
        """The help popover is a card with a border and padding under the panel header, as wide as the inspector (UX-B3)."""
        from StudioLib import shell, ui

        studioShell = self._applyStudio()
        studioShell.switchTo("draw")
        self._pump()
        header = studioShell.panelHeader
        menu = header.helpPopover()
        self.assertIsNotNone(menu)
        try:
            browser = menu.findChild("QTextBrowser", "StudioHelpBrowser")
            self.assertIsNotNone(browser)
            self.assertEqual(browser.document.documentMargin, shell.HELP_POPOVER_PADDING)
            self.assertIn(f"border: 1px solid {ui.token('border').name()}", menu.styleSheet)
            self.assertIn(f"background-color: {ui.token('card').name()}", menu.styleSheet)
            menu.popup(header.helpPopoverPosition())
            self._pump()
            self.assertTrue(menu.isVisible())
            self.assertEqual(menu.width, max(header.widget.width, shell.HELP_POPOVER_MIN_WIDTH + 2))
            # The card is drawn: the browser shows the card color
            image = browser.viewport().grab().toImage()
            self.assertEqual(qt.QColor.fromRgba(image.pixel(2, image.height() - 3)).name(), ui.token("card").name())
        finally:
            menu.close()
            menu.deleteLater()
            self._pump()

    #
    # Draw workspace (W2-05): presentation, keys, Lesion card, save model, views
    #

    def _startOnTemplate(self, studioShell):
        """Start on MNI template (Draw, Brush armed) and return the editor."""
        from StudioLib import importer

        importer.startOnTemplate(shell=studioShell)
        self._pump(10)
        self.addCleanup(importer._closeTemplateBanner)
        return slicer.modules.SegmentEditorWidget.editor

    def _drag(self, viewName, offsets, modifiers=None):
        """A left-button drag in a slice view through ``offsets`` (pixels from the view centre)."""
        import vtk

        view = self.layoutManager.sliceWidget(viewName).sliceView()
        interactor = view.interactor()
        centerX, centerY = view.width // 2, view.height // 2
        points = [(centerX + dx, centerY + dy) for dx, dy in offsets]
        interactor.SetEventPosition(*points[0])
        interactor.InvokeEvent(vtk.vtkCommand.MouseMoveEvent)
        interactor.InvokeEvent(vtk.vtkCommand.LeftButtonPressEvent)
        for point in points[1:]:
            interactor.SetEventPosition(*point)
            interactor.InvokeEvent(vtk.vtkCommand.MouseMoveEvent)
        interactor.InvokeEvent(vtk.vtkCommand.LeftButtonReleaseEvent)
        self._pump()
        return view

    @staticmethod
    def _voxels(editor):
        segmentationNode = editor.segmentationNode()
        return int(slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, editor.currentSegmentID(), editor.sourceVolumeNode()).sum())

    def _studioKeys(self, studioShell):
        return {portableKey(key): description for key, description in studioShortcutKeys(studioShell)}

    def test_18_draw_presentation_round_trip(self):
        """Draw is the studio presentation of the Segment Editor; Classic restores every property of the classic table (SPEC 5.1)."""
        import SegmentEditor

        studioShell = self._applyStudio()
        studioShell.switchTo("draw")
        self._pump()
        widget = slicer.modules.SegmentEditorWidget
        editor = widget.editor
        self.assertEqual(widget.presentation(), "studio")
        for name, value in SegmentEditor.STUDIO_PROPERTIES.items():
            if hasattr(editor, name) and name != "maximumNumberOfUndoStates":
                self.assertEqual(getattr(editor, name), value, name)
        self.assertGreaterEqual(editor.maximumNumberOfUndoStates, 20)
        self.assertFalse(editor.findChild("QWidget", "UndoRedoGroupBox").isVisibleTo(editor))
        self.assertTrue(editor.findChild("QWidget", "EffectHelpBrowser").isHidden())
        self.assertFalse(editor.findChild("QWidget", "SegmentsTableResizableFrame").isVisibleTo(editor))
        tiles = [(button.objectName, button.text) for button in widget._effectButtons() if button.isVisibleTo(editor)]
        expected = [(name, text) for name, text in (("Paint", "Brush"), ("Draw", "Outline"), ("Smart fill", "Smart fill"), ("Erase", "Erase"),
                                                    ("Fill between slices", "Fill gaps")) if editor.effectByName(name) is not None]
        self.assertEqual(tiles, expected)
        self.assertIn("Brush · P · 1", editor.findChild("QToolButton", "Paint").toolTip)
        self.assertIsNotNone(self.mainWindow.findChild("QFrame", "StudioLesionCard"))
        self.assertIsNotNone(self.mainWindow.findChild("QWidget", "StudioDisplayCard"))
        # More tools: every effect, in the studio order, and the masking section (its overwrite options, V-03)
        editor.setActiveEffectByName("Paint")
        self._pump()
        masking = editor.findChild("QWidget", "MaskingGroupBox")
        hasEffect = editor.activeEffect() is not None
        widget.setMoreToolsShown(True)
        self._pump()
        self.assertTrue(editor.findChild("QToolButton", "Level tracing").isVisibleTo(editor))
        self.assertTrue(editor.maskingSectionVisible)
        if hasEffect:
            # Shown at once, without choosing another tool
            self.assertTrue(masking.isVisibleTo(editor))
        widget.setMoreToolsShown(False)
        self._pump()
        self.assertFalse(editor.findChild("QToolButton", "Level tracing").isVisibleTo(editor))
        self.assertFalse(editor.maskingSectionVisible)
        self.assertFalse(masking.isVisibleTo(editor))
        # A masking that restricts editing stays in sight
        editorNode = editor.mrmlSegmentEditorNode()
        editorNode.SetOverwriteMode(slicer.vtkMRMLSegmentEditorNode.OverwriteNone)
        self._pump()
        self.assertTrue(editor.maskingSectionVisible)
        editorNode.SetOverwriteMode(slicer.vtkMRMLSegmentEditorNode.OverwriteAllSegments)
        self._pump()
        self.assertFalse(editor.maskingSectionVisible)
        editor.setActiveEffectByName("")
        self._pump()
        # The inline rename never writes the classic settings key that official Slicer reads
        tableView = editor.findChild("qMRMLSegmentsTableView", "SegmentsTableView")
        self.assertEqual(tableView.useTerminologySelectorSettingsKey, SegmentEditor.STUDIO_TERMINOLOGY_SETTINGS_KEY)
        self.assertFalse(tableView.useTerminologySelector)

        studioShell.classic(persist=False)
        self._pump()
        self.assertEqual(widget.presentation(), "classic")
        table = {name: value for name, value in SegmentEditor.classicProperties().items() if hasattr(editor, name)}
        self.assertEqual({name: getattr(editor, name) for name in table}, table)
        self.assertTrue(editor.findChild("QWidget", "UndoRedoGroupBox").isVisibleTo(editor))
        self.assertFalse(editor.findChild("QWidget", "EffectHelpBrowser").isHidden())
        self.assertFalse(editor.findChild("QWidget", "SegmentsTableResizableFrame").isHidden())
        # The official tool order (and so the positional keys 1..0) comes back
        self.assertEqual(list(editor.effectNameOrder())[:4], ["Threshold", "Paint", "Draw", "Erase"])
        self.assertEqual(list(editor.effectNameOrder()), widget._classicEffectNameOrder)
        self.assertEqual(tableView.useTerminologySelectorSettingsKey, SegmentEditor.CLASSIC_TERMINOLOGY_SETTINGS_KEY)
        for button in widget._effectButtons():
            effect = widget._buttonEffect(button)
            self.assertEqual(button.text, effect.title if effect is not None else "None")
            self.assertIsNone(button.property("slicerRole"))
        self.assertFalse(editor.findChild("QToolButton", "NULL").isHidden())
        for objectName in ("StudioLesionCard", "SegmentEditorMoreToolsButton", "StudioDisplayCard"):
            child = self.mainWindow.findChild("QWidget", objectName)
            self.assertTrue(child is None or not child.isVisible(), objectName)
        # The module navigation keys taken by Draw (Alt+Left/Right) are back
        previousModule = self.mainWindow.findChild("QAction", "PreviousModuleAction")
        self.assertIn(qt.QKeySequence(qt.QKeySequence.Back).toString(qt.QKeySequence.PortableText),
                      [portableKey(key) for key in previousModule.shortcuts()])
        self.assertEqual(widget.parent.sizePolicy.horizontalPolicy(), qt.QSizePolicy.Minimum)

    def test_18b_draw_keeps_studio_controls_after_classic_round_trip(self):
        """Classic entered from another page than Draw and left again: Draw hides the classic selectors and buttons.

        The panel of the Segment Editor is hidden there, and its visibility getters read False whatever was set; the
        studio table must still be written (bug: the selectors and the Add/Remove/Show 3D row stayed shown).
        """
        studioShell = self._applyStudio()
        editor = self._startOnTemplate(studioShell)
        studioShell.switchTo("draw")
        self._pump()
        widget = slicer.modules.SegmentEditorWidget
        hiddenNames = ("SegmentationNodeComboBox", "SourceVolumeNodeComboBox", "AddSegmentButton", "RemoveSegmentButton", "Show3DButton")
        for round_ in range(2):
            studioShell.switchTo("library")
            self._pump()
            studioShell.classic(persist=False)
            self._pump()
            self.assertEqual(widget.presentation(), "classic")
            # Leaving the Segment Editor for another module hides its panel (its visibility getters then read False)
            slicer.util.selectModule("Data" if round_ == 0 else "Volumes")
            self._pump()
            studioShell.studio()
            self._pump()
            studioShell.switchTo("draw")
            self._pump()
            self.assertEqual(widget.presentation(), "studio")
            for objectName in hiddenNames:
                child = editor.findChild("QWidget", objectName)
                self.assertFalse(child.isVisible(), f"{objectName} shown after classic round trip {round_}")

    def test_19_draw_first_click_paints(self):
        """Start on MNI template: Brush armed at 3 mm, and a first drag in the axial view paints into "Lesion" (SPEC 5.3)."""
        from StudioLib import draw

        studioShell = self._applyStudio()
        editor = self._startOnTemplate(studioShell)
        self.assertEqual(self._effect(editor), "Paint")
        paint = editor.effectByName("Paint")
        self.assertEqual(paint.integerParameter("BrushDiameterIsRelative"), 0)
        self.assertAlmostEqual(paint.doubleParameter("BrushAbsoluteDiameter"), slicer.util.settingsValue(draw.BRUSH_DIAMETER_KEY, 3.0, converter=float), 3)
        segment = editor.segmentationNode().GetSegmentation().GetSegment(editor.currentSegmentID())
        self.assertEqual(segment.GetName(), "Lesion")
        self._drag("Red", [(dx, 0) for dx in range(0, 40, 4)])
        self.assertGreater(self._voxels(editor), 0)
        # Voxel view is on by default on the MNI template (SPEC 5.3)
        self.assertEqual(editor.sourceVolumeNode().GetDisplayNode().GetInterpolate(), 0)
        # The brush size chips set the diameter of Brush and Erase
        draw.extension().brushChips[6.0].click()
        self._pump()
        self.assertAlmostEqual(paint.doubleParameter("BrushAbsoluteDiameter"), 6.0, 3)
        draw.extension().brushChips[3.0].click()
        self._pump()

    def test_20_draw_smartfill_tile(self):
        """Smart fill has a labelled tile (third), the key M and a "Smart fill · M · 3" tooltip (D-A10)."""
        studioShell = self._applyStudio()
        editor = self._startOnTemplate(studioShell)
        if editor.effectByName("Smart fill") is None:
            self.skipTest("Smart fill is not registered in this build")
        widget = slicer.modules.SegmentEditorWidget
        tile = editor.findChild("QToolButton", "Smart fill")
        self.assertEqual(tile.text, "Smart fill")
        self.assertFalse(tile.icon.isNull())
        self.assertIn("Smart fill · M · 3", tile.toolTip)
        self.assertEqual([button.objectName for button in widget._effectButtons()].index("Smart fill"), 3)
        keys = {portableKey(shortcut.key): shortcut for shortcut in self.mainWindow.findChildren("QShortcut") if shortcut.enabled and shortcut.property("studioShortcut")}
        keys["M"].activated()
        self._pump()
        self.assertEqual(self._effect(editor), "Smart fill")

    def test_21_draw_keys(self):
        """P D M E L Shift+F O and N C work in Draw, are removed outside Draw and collide with no view key (SPEC 5.4, 9.6)."""
        studioShell = self._applyStudio()
        editor = self._startOnTemplate(studioShell)
        keys = {portableKey(shortcut.key): shortcut for shortcut in self.mainWindow.findChildren("QShortcut") if shortcut.enabled and shortcut.property("studioShortcut")}
        for key, effectName in (("P", "Paint"), ("D", "Draw"), ("L", "Level tracing"), ("Shift+F", "Fill between slices"), ("P", "Paint")):
            keys[key].activated()
            self._pump()
            self.assertEqual(self._effect(editor), effectName, key)
        keys["E"].activated()
        self._pump()
        self.assertEqual(self._effect(editor), "Erase")
        keys["E"].activated()
        self._pump()
        self.assertEqual(self._effect(editor), "Paint")
        displayNode = editor.segmentationNode().GetDisplayNode()
        fill = displayNode.GetVisibility2DFill()
        keys["O"].activated()
        self.assertNotEqual(displayNode.GetVisibility2DFill(), fill)
        keys["O"].activated()
        assertNoKeyCollisions()
        studioShell.switchTo("library")
        self._pump()
        remaining = self._studioKeys(studioShell)
        for key in ("P", "D", "E", "L", "Shift+F", "O", "N", "C", "Alt+Left"):
            self.assertNotIn(key, remaining)
        studioShell.switchTo("draw")
        self._pump()
        self.assertIn("P", self._studioKeys(studioShell))

    def test_22_lesion_metrics(self):
        """Lesion metrics: parts (26-connectivity), gaps, centroid; mL equals Segment Statistics within 0.1%, in at most 15 ms (SPEC 5.2)."""
        import time

        import numpy as np

        from StudioLib import draw, lesioncard

        mask = np.zeros((10, 12, 14), bool)
        mask[2:4, 3:6, 3:6] = True
        mask[6:8, 3:6, 3:6] = True  # slices 4 and 5 are gaps along k
        mask[7, 9, 11] = True  # a separate speck (no 26-neighbour)
        matrix = np.diag([2.0, 2.0, 2.0, 1.0])
        matrix[:3, 3] = (-10.0, 0.0, 0.0)
        metrics = lesioncard.computeLesionMetrics(mask, (2, 2, 2), matrix, axis=0)
        self.assertEqual(metrics["voxels"], int(mask.sum()))
        self.assertAlmostEqual(metrics["ml"], mask.sum() * 8 / 1000.0)
        self.assertEqual(metrics["parts"], 3)
        self.assertEqual(metrics["gaps"], [4, 5])
        _kk, _jj, ii = np.nonzero(mask)
        self.assertAlmostEqual(metrics["centroid_ras"][0], -10.0 + 2.0 * ii.mean())
        self.assertIn("2 parts", lesioncard.readoutHtml(dict(metrics, parts=2)))
        self.assertEqual(lesioncard.computeLesionMetrics(np.zeros((3, 3, 3)), (1, 1, 1), np.eye(4))["voxels"], 0)

        # A lesion on a 1 mm MNI-size grid against Segment Statistics
        volumeNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "MetricsGrid")
        slicer.util.updateVolumeFromArray(volumeNode, np.zeros((182, 218, 182), np.int16))
        # The MNI152NLin6Asym 1 mm grid (FSL): LAS, origin (90, -126, -72)
        volumeNode.SetIJKToRASMatrix(vtk_matrix([[-1, 0, 0, 90], [0, 1, 0, -126], [0, 0, 1, -72], [0, 0, 0, 1]]))
        segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "MetricsLesion")
        segmentationNode.CreateDefaultDisplayNodes()
        segmentationNode.SetReferenceImageGeometryParameterFromVolumeNode(volumeNode)
        segmentId = segmentationNode.GetSegmentation().AddEmptySegment("", "Lesion")
        z, y, x = np.ogrid[0:182, 0:218, 0:182]
        lesion = ((x - 120) ** 2 + (y - 100) ** 2 + (z - 90) ** 2 <= 14 ** 2).astype(np.uint8)
        slicer.util.updateSegmentBinaryLabelmapFromArray(lesion, segmentationNode, segmentId, volumeNode)
        times = []
        for _index in range(5):
            draw._metricsCache.clear()
            start = time.perf_counter()
            metrics = draw.lesionMetrics(segmentationNode, compute=True, segmentId=segmentId)
            times.append((time.perf_counter() - start) * 1000.0)
        import SegmentStatistics

        logic = SegmentStatistics.SegmentStatisticsLogic()
        logic.getParameterNode().SetParameter("Segmentation", segmentationNode.GetID())
        logic.computeStatistics()
        statistics = logic.getStatistics()
        reference = statistics[segmentId, "LabelmapSegmentStatisticsPlugin.volume_cm3"]
        self.assertLess(abs(metrics["mL"] - reference) / reference, 0.001)
        self.assertEqual(metrics["parts"], 1)
        self.assertLessEqual(sorted(times)[len(times) // 2], 15.0, times)
        # MNI grid, x < 0: the left hemisphere
        self.assertEqual(metrics["sides"], "Left")

    def test_23_draw_save_keeps_lesion_grid(self):
        """A 91x109x91 NLin6Asym mask shown on the 2009c template is saved on its own grid with identical voxels (U3)."""
        import os
        import tempfile

        import numpy as np

        from StudioLib import draw, importer

        directory = tempfile.mkdtemp(prefix="StudioTestGrid", dir=slicer.app.temporaryPath)
        maskNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode", "nlin6")
        mask = np.zeros((91, 109, 91), np.uint8)
        mask[40:44, 50:55, 20:24] = 1
        slicer.util.updateVolumeFromArray(maskNode, mask)
        ijkToRas = vtk_matrix([[-2, 0, 0, 90], [0, 2, 0, -126], [0, 0, 2, -72], [0, 0, 0, 1]])
        maskNode.SetIJKToRASMatrix(ijkToRas)
        maskPath = os.path.join(directory, "sub-01_lesion.nii.gz")
        self.assertTrue(slicer.util.saveNode(maskNode, maskPath))
        slicer.mrmlScene.Clear(0)

        studioShell = self._applyStudio()
        editor = self._startOnTemplate(studioShell)
        importer.loadFiles([maskPath], studioShell)
        self._pump(10)
        segmentationNode = editor.segmentationNode()
        self.assertEqual(segmentationNode.GetName(), "sub-01_lesion")
        outputPath = os.path.join(directory, "out", "saved_lesion.nii.gz")
        written = draw.saveLesionDialog(studioShell, path=outputPath)
        self.assertEqual(written, outputPath)
        # Both files read the same way: same grid (dims and IJK to RAS, the sform) and identical voxels
        original = slicer.util.loadLabelVolume(maskPath, {"show": False})
        saved = slicer.util.loadLabelVolume(written, {"show": False})
        try:
            self.assertEqual(saved.GetImageData().GetDimensions(), (91, 109, 91))
            savedMatrix, originalMatrix = vtk_matrix(), vtk_matrix()
            saved.GetIJKToRASMatrix(savedMatrix)
            original.GetIJKToRASMatrix(originalMatrix)
            for row in range(4):
                for column in range(4):
                    self.assertAlmostEqual(savedMatrix.GetElement(row, column), originalMatrix.GetElement(row, column), 4)
            self.assertTrue(np.array_equal(slicer.util.arrayFromVolume(saved) > 0, slicer.util.arrayFromVolume(original) > 0))
            self.assertEqual(int((slicer.util.arrayFromVolume(saved) > 0).sum()), int(mask.sum()))
        finally:
            slicer.mrmlScene.RemoveNode(saved)
            slicer.mrmlScene.RemoveNode(original)

    def test_24_draw_place_keeps_tool(self):
        """Place mode suspends the Draw tool and arms it again when the mode ends (U14)."""
        from StudioLib import draw

        studioShell = self._applyStudio()
        editor = self._startOnTemplate(studioShell)
        editor.setActiveEffectByName("Erase")
        self._pump()
        interactionNode = slicer.app.applicationLogic().GetInteractionNode()
        interactionNode.SetCurrentInteractionMode(slicer.vtkMRMLInteractionNode.Place)
        self._pump()
        self.assertEqual(draw.suspendedEffect(studioShell), "Erase")
        self.assertIn("Erase resumes after", draw.extension().card.hintLabel.text)
        interactionNode.SwitchToViewTransformMode()
        self._pump(6)
        self.assertEqual(self._effect(editor), "Erase")
        self.assertIsNone(draw.suspendedEffect(studioShell))
        self.assertEqual(draw.drawState(studioShell).effect, "Erase")

    def test_25_draw_double_click_guard(self):
        """A double-click with Paint active does not maximize the view; without a tool it still does (U12)."""
        import vtk

        studioShell = self._applyStudio()
        editor = self._startOnTemplate(studioShell)
        layoutNode = self.layoutManager.layoutLogic().GetLayoutNode()
        interactor = self.layoutManager.sliceWidget("Red").sliceView().interactor()
        view = self.layoutManager.sliceWidget("Red").sliceView()
        interactor.SetEventPosition(view.width // 2, view.height // 2)
        self.assertEqual(self._effect(editor), "Paint")
        interactor.InvokeEvent(vtk.vtkCommand.LeftButtonDoubleClickEvent)
        self._pump()
        self.assertEqual(layoutNode.GetNumberOfMaximizedViewNodes(), 0)
        # Also after the view interactions are reconfigured while Paint is active (W3-02a setters)
        crosshair = view.displayableManagerByClassName("vtkMRMLCrosshairDisplayableManager")
        sliceIntersectionWidget = crosshair.GetSliceIntersectionWidget() if crosshair is not None else None
        widgetClass = getattr(slicer, "vtkMRMLSliceIntersectionWidget", None)
        if sliceIntersectionWidget is not None and hasattr(widgetClass, "SetPageStepSlices"):
            actions = sliceIntersectionWidget.GetActionsEnabled()
            pageStep = widgetClass.GetPageStepSlices()
            try:
                sliceIntersectionWidget.SetActionsEnabled(0)
                sliceIntersectionWidget.SetActionsEnabled(actions)
                widgetClass.SetPageStepSlices(pageStep + 3)
                self._pump()
                self.assertEqual(self._effect(editor), "Paint")
                interactor.InvokeEvent(vtk.vtkCommand.LeftButtonDoubleClickEvent)
                self._pump()
                self.assertEqual(layoutNode.GetNumberOfMaximizedViewNodes(), 0)
            finally:
                widgetClass.SetPageStepSlices(pageStep)
                sliceIntersectionWidget.SetActionsEnabled(actions)
        editor.setActiveEffect(None)
        self._pump()
        interactor.InvokeEvent(vtk.vtkCommand.LeftButtonDoubleClickEvent)
        self._pump()
        self.assertEqual(layoutNode.GetNumberOfMaximizedViewNodes(), 1)
        layoutNode.RemoveAllMaximizedViewNodes()
        self._pump()

    def test_26_draw_empty_state(self):
        """Draw without a scan creates no segmentation and shows what to do next; a figure is never the source (U11)."""
        import numpy as np

        studioShell = self._applyStudio()
        nodeCount = slicer.mrmlScene.GetNumberOfNodes()
        studioShell.switchTo("draw")
        self._pump(6)
        self.assertEqual(len(slicer.util.getNodesByClass("vtkMRMLSegmentationNode")), 0)
        self.assertEqual(slicer.mrmlScene.GetNumberOfNodes(), nodeCount)
        emptyState = self.mainWindow.findChild("QFrame", "StudioDrawEmptyState")
        self.assertTrue(emptyState.isVisible())
        for name in ("StudioDrawOpenButton", "StudioDrawStartOnTemplateButton", "StudioDrawLibraryButton"):
            self.assertTrue(self.mainWindow.findChild("QPushButton", name).isVisible(), name)
        self.assertFalse(slicer.modules.SegmentEditorWidget.editor.isVisible())
        # No orphan "Tools" header or "More tools" button without the tool tiles (bug bash: draw-empty-state-orphan-tools-header)
        toolsRow = self.mainWindow.findChild("QWidget", "StudioDrawToolsRow")
        self.assertIsNotNone(toolsRow)
        self.assertFalse(toolsRow.isVisible())
        # A figure (Studio.Role) shown in the views is never the source volume
        studioShell.switchTo("library")
        self._pump()
        figure = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLVectorVolumeNode", "figure")
        slicer.util.updateVolumeFromArray(figure, np.zeros((1, 20, 30, 3), np.uint8))
        figure.SetAttribute("Studio.Role", "figure")
        slicer.util.setSliceViewerLayers(background=figure)
        studioShell.switchTo("draw")
        self._pump(6)
        editor = slicer.modules.SegmentEditorWidget.editor
        self.assertIsNone(editor.sourceVolumeNode())
        self.assertEqual(len(slicer.util.getNodesByClass("vtkMRMLSegmentationNode")), 0)
        self.assertTrue(emptyState.isVisible())
        self.assertFalse(toolsRow.isVisible())

    def test_27_draw_follow_and_center(self):
        """After a stroke in Red the other views move to it (Follow); Center on lesion moves every view to the lesion (U9, U10)."""
        from StudioLib import draw

        studioShell = self._applyStudio()
        editor = self._startOnTemplate(studioShell)
        settings = qt.QSettings()
        settings.remove(draw.FOLLOW_STROKES_KEY)
        red, green = (self.layoutManager.sliceWidget(name).mrmlSliceNode() for name in ("Red", "Green"))
        redOffset = red.GetSliceOffset()
        view = self._drag("Red", [(40 + dx, 30) for dx in range(0, 16, 4)])
        self._wait(100)
        self.assertGreater(self._voxels(editor), 0)
        # The release point: 30 px above the centre of Red (screen y up = anterior)
        ras = red.GetXYToRAS().MultiplyPoint((view.width // 2 + 52, view.height // 2 + 30, 0, 1))
        self.assertLessEqual(abs(green.GetSliceOffset() - ras[1]), 2.0)
        self.assertAlmostEqual(red.GetSliceOffset(), redOffset, 3)
        # Follow off: the other views stay
        settings.setValue(draw.FOLLOW_STROKES_KEY, False)
        greenOffset = green.GetSliceOffset()
        self._drag("Red", [(-60 + dx, -40) for dx in range(0, 16, 4)])
        self._wait(100)
        self.assertAlmostEqual(green.GetSliceOffset(), greenOffset, 3)
        settings.remove(draw.FOLLOW_STROKES_KEY)
        # Center on lesion: the visible button moves every view to the lesion centroid
        centerButton = self.mainWindow.findChild("QToolButton", "StudioCenterOnLesionButton")
        self.assertTrue(centerButton.isVisible())
        centerButton.click()
        self._pump()
        centroid = draw.lesionMetrics(editor.segmentationNode(), compute=True)["centroid_ras"]
        self.assertLessEqual(abs(green.GetSliceOffset() - centroid[1]), 2.0)
        self.assertLessEqual(abs(red.GetSliceOffset() - centroid[2]), 2.0)

    def test_28_draw_show_all_controls_and_help(self):
        """⋯ Show all controls shows the classic controls inside Draw and back; "?" shows the active tool (U17, U18)."""
        from StudioLib import draw

        studioShell = self._applyStudio()
        editor = self._startOnTemplate(studioShell)
        title, body = draw.helpHtml(studioShell)
        self.assertEqual(title, "Draw · Brush")
        self.assertIn("Draw keys", body)
        items = [item for item in draw.moreMenuActions(studioShell) if item is not None]
        # The display items of the Draw ⋯ menu (V-03: Show lesion in 3D, Voxel view, Window/level), then Show all controls
        self.assertEqual([item[0] for item in items], ["Show lesion in 3D", "Voxel view", "Window/level", "Show all controls"])
        self.assertEqual([title for title, _callback, _checked in items[2][1]], ["Auto", "Brain", "Narrow"])
        _text, toggle, checked = items[-1]
        self.assertFalse(checked)
        try:
            toggle()
            self._pump()
            self.assertEqual(studioShell.currentWorkspace(), "draw")
            self.assertTrue(editor.findChild("QWidget", "UndoRedoGroupBox").isVisible())
            self.assertTrue(editor.findChild("QWidget", "SegmentsTableResizableFrame").isVisible())
            self.assertTrue(editor.segmentationNodeSelectorVisible)
            draw.moreMenuActions(studioShell)[-1][1]()
            self._pump()
            self.assertFalse(editor.findChild("QWidget", "UndoRedoGroupBox").isVisible())
            self.assertFalse(editor.findChild("QWidget", "SegmentsTableResizableFrame").isVisible())
        finally:
            qt.QSettings().remove(draw.SHOW_ALL_CONTROLS_KEY)

    def test_29_draw_cleanup_and_smooth_safety(self):
        """Fill gaps and specks is one undo step; Smooth keeps a one-slice lesion and warns when it removes most of it (SPEC 5.2, U15)."""
        from StudioLib import draw

        studioShell = self._applyStudio()
        editor = self._startOnTemplate(studioShell)
        segmentationNode, segmentId, source = editor.segmentationNode(), editor.currentSegmentID(), editor.sourceVolumeNode()
        array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, source)
        array[:] = 0
        array[40:46, 50:56, 30:36] = 1
        array[42, 52, 33] = 0  # a hole
        array[60, 70, 60] = 1  # a speck
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, source)
        before = self._voxels(editor)
        result = draw.fillGapsAndSpecks(studioShell)
        self.assertIsNotNone(result)
        after = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, source)
        self.assertEqual(int(after[60, 70, 60]), 0)
        editor.undo()
        self.assertEqual(self._voxels(editor), before)
        # A one-slice lesion: Smooth switches to Closing, which keeps it
        array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, source)
        array[:] = 0
        array[45, 40:60, 40:60] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, source)
        self._wait(draw.METRICS_DEBOUNCE_MS + 50)
        editor.setActiveEffectByName("Smoothing")
        self._pump(4)
        effect = editor.activeEffect()
        self.assertEqual(effect.parameter("SmoothingMethod"), "MORPHOLOGICAL_CLOSING")
        effect.self().onApply()
        self._pump()
        self.assertGreater(self._voxels(editor), 0)
        # A destructive Smooth: a consequence toast with Undo
        self._wait(draw.METRICS_DEBOUNCE_MS + 50)
        effect.setParameter("SmoothingMethod", "MEDIAN")
        effect.setParameter("KernelSizeMm", 9.0)
        effect.self().onApply()
        self._wait(draw.METRICS_DEBOUNCE_MS + 100)
        self.assertTrue(studioShell.toastText().startswith("Smooth removed"), studioShell.toastText())
        editor.setActiveEffect(None)

    def test_29b_draw_fill_gaps_link_says_when_nothing_was_filled(self):
        """The card's "Fill" link must not do nothing silently: thin strokes that interpolation cannot join give a message."""
        from StudioLib import draw

        studioShell = self._applyStudio()
        editor = self._startOnTemplate(studioShell)
        segmentationNode, segmentId, source = editor.segmentationNode(), editor.currentSegmentID(), editor.sourceVolumeNode()

        def setLesion(planes):
            array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, source)
            array[:] = 0
            for k, j0, j1, i0, i1 in planes:
                array[k, j0:j1, i0:i1] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, source)

        # Two one-voxel-thin lines three slices apart: the Fill between slices effect adds nothing
        setLesion([(40, 50, 51, 30, 36), (44, 50, 51, 30, 36)])
        before = self._voxels(editor)
        studioShell.toast("")
        draw.fillGaps(studioShell)
        self._pump()
        self.assertEqual(self._voxels(editor), before)
        self.assertTrue(studioShell.toastText().startswith("Nothing to fill"), studioShell.toastText())
        # Two wide plates: the slices between are filled and no such message is shown
        setLesion([(40, 50, 56, 30, 36), (44, 50, 56, 30, 36)])
        before = self._voxels(editor)
        studioShell.toast("")
        draw.fillGaps(studioShell)
        self._pump()
        self.assertGreater(self._voxels(editor), before)
        self.assertFalse(studioShell.toastText().startswith("Nothing to fill"), studioShell.toastText())
        editor.setActiveEffect(None)

    def test_29c_draw_fill_gaps_closes_gaps_between_thick_parts(self):
        """The card's "Fill" link and the Fill gaps tile close gaps between parts that are several slices thick."""
        from StudioLib import draw

        studioShell = self._applyStudio()
        editor = self._startOnTemplate(studioShell)
        segmentationNode, segmentId, source = editor.segmentationNode(), editor.currentSegmentID(), editor.sourceVolumeNode()

        def setLesion(slabs):
            array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, source)
            array[:] = 0
            for k0, k1 in slabs:
                array[k0:k1, 50:58, 30:38] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, source)
            self._pump()

        def gapCount():
            return len(draw.lesionMetrics(segmentationNode, compute=True)["gaps"])

        for slabs, gaps in (([(34, 40), (42, 48)], 2), ([(34, 40), (41, 47)], 1), ([(34, 38), (40, 44), (46, 50)], 4)):
            for viaTile in (False, True):
                editor.setActiveEffect(None)
                setLesion(slabs)
                before = self._voxels(editor)
                self.assertEqual(gapCount(), gaps, slabs)
                studioShell.toast("")
                if viaTile:
                    editor.setActiveEffectByName("Fill between slices")
                    self._pump()
                    editor.activeEffect().self().fillButton.click()
                else:
                    draw.fillGaps(studioShell)
                self._pump()
                self.assertGreater(self._voxels(editor), before, (slabs, viaTile))
                self.assertEqual(gapCount(), 0, (slabs, viaTile))
                self.assertFalse(studioShell.toastText().startswith("Nothing to fill"), studioShell.toastText())
        # The voxels of the parts themselves are kept
        setLesion([(34, 40), (42, 48)])
        before = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, source).copy()
        draw.fillGaps(studioShell)
        self._pump()
        after = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, source)
        self.assertTrue(((after > 0) | (before == 0)).all())
        editor.setActiveEffect(None)

    def test_29e_fill_gaps_never_joins_parts_side_by_side_and_classic_is_official(self):
        """Fill gaps fills only slices that are empty for the lesion along the slicing axis: two parts side by side
        within the slices are never joined. Classic Fill between slices gives the result of official 3D Slicer (the
        plain contour interpolation), also for parts that are several slices thick.
        """
        import numpy as np
        from StudioLib import draw

        studioShell = self._applyStudio()
        editor = self._startOnTemplate(studioShell)
        segmentationNode, segmentId, source = editor.segmentationNode(), editor.currentSegmentID(), editor.sourceVolumeNode()

        def setLesion(boxes):
            array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, source)
            array[:] = 0
            for k0, k1, j0, j1, i0, i1 in boxes:
                array[k0:k1, j0:j1, i0:i1] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, source)
            self._pump()
            return array.copy()

        def lesion():
            return slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, source).copy()

        def fillWithTile():
            editor.setActiveEffectByName("Fill between slices")
            self._pump()
            editor.activeEffect().self().fillButton.click()
            self._pump()
            editor.setActiveEffect(None)

        sideBySide = [(34, 46, 50, 58, 20, 28), (34, 46, 50, 58, 34, 42)]
        aboveBelow = [(34, 46, 44, 50, 26, 34), (34, 46, 56, 62, 26, 34)]
        # Left part with a gap between thick parts, right part on every slice: the lesion has no empty slice
        gapOnOneSide = [(34, 38, 50, 58, 20, 28), (42, 46, 50, 58, 20, 28), (34, 46, 50, 58, 34, 42)]
        for boxes in (sideBySide, aboveBelow, gapOnOneSide):
            for viaTile in (False, True):
                editor.setActiveEffect(None)
                before = setLesion(boxes)
                if viaTile:
                    fillWithTile()
                else:
                    draw.fillGaps(studioShell)
                    self._pump()
                    editor.setActiveEffect(None)
                after = lesion()
                self.assertEqual(int(np.count_nonzero(after)), int(np.count_nonzero(before)), (boxes, viaTile))
                self.assertTrue(((after > 0) == (before > 0)).all(), (boxes, viaTile))

        # Classic: the result of the plain interpolation, as in official 3D Slicer
        import vtk
        import vtkITK
        from vtk.util import numpy_support

        studioShell.classic(persist=False)
        self._pump()
        self.assertEqual(slicer.modules.SegmentEditorWidget.presentation(), "classic")
        for boxes in (sideBySide, [(34, 40, 50, 58, 30, 38), (42, 48, 50, 58, 30, 38)]):
            before = setLesion(boxes)
            image = vtk.vtkImageData()
            image.SetDimensions(before.shape[2], before.shape[1], before.shape[0])
            image.GetPointData().SetScalars(numpy_support.numpy_to_vtk(before.astype(np.uint8).ravel(), deep=True))
            interpolator = vtkITK.vtkITKMorphologicalContourInterpolator()
            interpolator.SetInputData(image)
            interpolator.Update()
            official = numpy_support.vtk_to_numpy(interpolator.GetOutput().GetPointData().GetScalars()).reshape(before.shape)
            fillWithTile()
            after = lesion()
            self.assertTrue(((after > 0) == ((official > 0) | (before > 0))).all(), boxes)
        editor.setActiveEffect(None)

    def test_29f_fill_between_slices_parameters_follow_the_presentation_after_scene_load(self):
        """A scene saved in one presentation carries the Fill between slices parameters of the Segment Editor node.
        Loaded in the other presentation, the parameters are those of the presentation: Classic gives the result of
        official 3D Slicer (no gap filling between thick parts), and Studio Fill gaps keeps the selected segment only.
        """
        widget = slicer.modules.segmenteditor.widgetRepresentation().self()
        initialPresentation = widget.presentation()
        try:
            self._checkFillParametersAfterSceneLoad(widget)
        finally:
            widget.editor.setActiveEffect(None)
            widget.setPresentation(initialPresentation)
            self._pump()

    def _checkFillParametersAfterSceneLoad(self, widget):
        import os
        import shutil
        import tempfile

        import numpy as np

        slicer.util.selectModule("SegmentEditor")
        self._pump()
        editor = widget.editor

        def parameters():
            effect = editor.effectByName("Fill between slices")
            return {name: effect.integerParameter(name) if effect.parameterDefined(name) else None
                    for name in ("SelectedSegmentOnly", "FillGapsBetweenThickParts")}

        def saveBundle():
            directory = tempfile.mkdtemp(prefix="StudioTestFillScene", dir=slicer.app.temporaryPath)
            self.addCleanup(shutil.rmtree, directory, True)
            self.assertTrue(slicer.app.applicationLogic().SaveSceneToSlicerDataBundleDirectory(directory))
            return f"{directory}/{os.path.basename(directory)}.mrml"

        def loadBundle(path, entered):
            editor.setActiveEffect(None)
            slicer.util.selectModule("SegmentEditor" if entered else "Data")
            self._pump()
            slicer.mrmlScene.Clear(0)
            self._pump()
            self.assertTrue(slicer.util.loadScene(path))
            slicer.util.selectModule("SegmentEditor")
            self._pump()

        # Two parts several slices thick, separated by empty slices along k (what Studio Fill gaps fills)
        widget.setPresentation("studio")
        self._pump()
        volume = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "FillSceneVolume")
        slicer.util.updateVolumeFromArray(volume, np.full((30, 40, 40), 100, np.int16))
        volume.CreateDefaultDisplayNodes()
        segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "FillSceneSegmentation")
        segmentation.CreateDefaultDisplayNodes()
        segmentation.SetReferenceImageGeometryParameterFromVolumeNode(volume)
        segmentId = segmentation.GetSegmentation().AddEmptySegment("lesion", "Lesion")
        lesion = np.zeros((30, 40, 40), np.uint8)
        lesion[5:11, 10:30, 10:30] = 1
        lesion[18:25, 10:30, 10:30] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(lesion, segmentation, segmentId, volume)
        editor.setSegmentationNode(segmentation)
        editor.setSourceVolumeNode(volume)
        editor.mrmlSegmentEditorNode().SetSelectedSegmentID(segmentId)
        self._pump()
        self.assertEqual(parameters(), {"SelectedSegmentOnly": 1, "FillGapsBetweenThickParts": 1})
        studioScene = saveBundle()

        # Studio scene in Classic, loaded while the Segment Editor is not shown
        widget.setPresentation("classic")
        self._pump()
        loadBundle(studioScene, entered=False)
        self.assertEqual(parameters(), {"SelectedSegmentOnly": 0, "FillGapsBetweenThickParts": 0})
        segmentation = slicer.util.getNode("FillSceneSegmentation")
        volume = slicer.util.getNode("FillSceneVolume")
        editor.setSegmentationNode(segmentation)
        editor.setSourceVolumeNode(volume)
        segmentId = segmentation.GetSegmentation().GetSegmentIdBySegmentName("Lesion")
        before = slicer.util.arrayFromSegmentBinaryLabelmap(segmentation, segmentId, volume).copy()
        self.assertEqual(int(np.count_nonzero(before)), int(np.count_nonzero(lesion)))
        editor.setActiveEffectByName("Fill between slices")
        effect = editor.activeEffect().self()
        effect.onPreview()
        self._pump()
        effect.onApply()
        self._pump()
        editor.setActiveEffect(None)
        after = slicer.util.arrayFromSegmentBinaryLabelmap(segmentation, segmentId, volume)
        # Official 3D Slicer leaves the gap between parts that span several slices open
        self.assertTrue(((after > 0) == (before > 0)).all())

        # Classic scene in Studio, loaded while the Segment Editor is shown
        classicScene = saveBundle()
        widget.setPresentation("studio")
        self._pump()
        loadBundle(classicScene, entered=True)
        self.assertEqual(parameters(), {"SelectedSegmentOnly": 1, "FillGapsBetweenThickParts": 1})

    def test_29d_draw_cleanup_that_changes_nothing_offers_no_undo(self):
        """A Keep largest part or Fill gaps and specks that changes nothing adds no undo step, so its toast has no
        Undo: that Undo would undo the user's previous, unrelated stroke.
        """
        from StudioLib import draw

        studioShell = self._applyStudio()
        editor = self._startOnTemplate(studioShell)
        segmentationNode, segmentId, source = editor.segmentationNode(), editor.currentSegmentID(), editor.sourceVolumeNode()
        array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, source)
        array[:] = 0
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, source)
        # "Stroke 1" then "stroke 2" touching it, each its own undo step: one solid part without gaps or specks
        editor.saveStateForUndo()
        array[40:46, 50:56, 30:36] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, source)
        editor.saveStateForUndo()
        array[40:46, 50:56, 36:40] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, source)
        voxels = self._voxels(editor)
        actionButton = studioShell._stickyToast.actionButton
        for cleanup in (draw.keepLargestPart, draw.fillGapsAndSpecks):
            studioShell._stickyToast.hide()
            result = cleanup(studioShell)
            self._pump()
            self.assertEqual(result, (voxels, voxels), cleanup.__name__)
            self.assertEqual(self._voxels(editor), voxels, cleanup.__name__)
            self.assertTrue(studioShell.toastText(), cleanup.__name__)
            if actionButton.isVisible():
                actionButton.click()
                self._pump()
            self.assertEqual(self._voxels(editor), voxels, f"{cleanup.__name__}: the toast's Undo undid an earlier stroke")
        # A cleanup that changes something is still one undo step with an Undo in its toast
        array[60, 70, 60] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, source)
        studioShell._stickyToast.hide()
        self.assertEqual(draw.keepLargestPart(studioShell), (voxels + 1, voxels))
        self._pump()
        self.assertTrue(actionButton.isVisible())
        actionButton.click()
        self._pump()
        self.assertEqual(self._voxels(editor), voxels + 1)

    def test_30_draw_extension_points(self):
        """Inspector cards, card rows and ⋯ items of other work packages; network maps leave Draw; assisted counts (LNM addendum)."""
        import numpy as np

        from StudioLib import draw

        studioShell = self._applyStudio()
        editor = self._startOnTemplate(studioShell)
        before, after = qt.QLabel("before tools"), qt.QLabel("after display")
        draw.addInspectorCard("test.before", before, 10)
        draw.addInspectorCard("test.after", after, 150)
        self._pump()
        self.assertTrue(before.isVisible() and after.isVisible())
        header = self.mainWindow.findChild("QWidget", "StudioDrawHeader")
        trailing = self.mainWindow.findChild("QWidget", "StudioDrawTrailing")
        self.assertTrue(header.isAncestorOf(before))
        self.assertTrue(trailing.isAncestorOf(after))
        self.assertTrue(draw.removeInspectorCard("test.before"))
        self.assertTrue(draw.removeInspectorCard("test.after"))
        calls = []
        card = draw.lesionCard()
        card.addMenuAction("Test item", lambda: calls.append("item"))
        card._fillMenu()
        self.assertIn("Test item", [str(action.text) for action in card.menu.actions()])
        card.removeMenuAction("Test item")
        draw.notifyAssist("smartfill", 12, {"tolerance": 0.1})
        self.assertEqual(draw.assistCounts()["smartfill"]["voxels"], 12)
        # A network map in the foreground leaves the slice views when Draw is entered
        networkMap = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "network map")
        slicer.util.updateVolumeFromArray(networkMap, np.zeros((4, 4, 4), np.float32))
        networkMap.SetAttribute("LNM.Result", "1")
        slicer.util.setSliceViewerLayers(foreground=networkMap)
        studioShell.switchTo("library")
        self._pump()
        studioShell.switchTo("draw")
        self._pump()
        compositeNode = self.layoutManager.sliceWidget("Red").mrmlSliceCompositeNode()
        self.assertFalse(compositeNode.GetForegroundVolumeID())

    def test_31_draw_side_word_preset_and_set(self):
        """'Left' only on an MNI grid, the Brain preset in one click, Alt+→ through a loaded set without new nodes (U6, U8, U16)."""
        import os
        import tempfile

        import numpy as np

        from StudioLib import draw, importer

        def readout():
            draw.session(studioShell).refreshPanel(computeMetrics=True)
            return str(self.mainWindow.findChild("QLabel", "StudioLesionReadout").text)

        def paintAt(editor, ras):
            segmentationNode, segmentId, sourceVolumeNode = editor.segmentationNode(), editor.currentSegmentID(), editor.sourceVolumeNode()
            rasToIjk = vtk_matrix()
            sourceVolumeNode.GetRASToIJKMatrix(rasToIjk)
            i, j, k = (round(value) for value in rasToIjk.MultiplyPoint((*ras, 1.0))[:3])
            array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, sourceVolumeNode)
            array[k - 1:k + 2, j - 1:j + 2, i - 1:i + 2] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, sourceVolumeNode)

        studioShell = self._applyStudio()
        # On the MNI template a lesion at x < 0 is "Left"
        editor = self._startOnTemplate(studioShell)
        paintAt(editor, (-30.0, -10.0, 10.0))
        self.assertTrue(readout().startswith("Left · "), readout())
        # Display: the Brain preset changes window/level in one click
        displayNode = editor.sourceVolumeNode().GetDisplayNode()
        before = (displayNode.GetWindow(), displayNode.GetLevel())
        # (the presets are items of the Draw ⋯ menu, V-03)
        draw.extension().displayCard.applyPreset("brain")
        self._pump()
        self.assertNotEqual((displayNode.GetWindow(), displayNode.GetLevel()), before)
        # On a scan in scanner space: no side word
        slicer.mrmlScene.Clear(0)
        _path, editor = self._openScanInDraw(studioShell)
        source = editor.sourceVolumeNode()
        bounds = [0.0] * 6
        source.GetRASBounds(bounds)
        paintAt(editor, (bounds[0] + 0.25 * (bounds[1] - bounds[0]), 0.5 * (bounds[2] + bounds[3]), 0.5 * (bounds[4] + bounds[5])))
        text = readout()
        self.assertTrue(text.split(" · ")[0].endswith("mL"), text)
        # A loaded set of lesions on the NLin6Asym 2 mm grid: Alt+→ steps through it and adds no node
        slicer.mrmlScene.Clear(0)
        directory = tempfile.mkdtemp(prefix="StudioTestSet", dir=slicer.app.temporaryPath)
        paths = []
        for index in range(3):
            maskNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode", "mask")
            mask = np.zeros((91, 109, 91), np.uint8)
            mask[40:43, 50:53, 20 + 5 * index:23 + 5 * index] = 1
            slicer.util.updateVolumeFromArray(maskNode, mask)
            maskNode.SetIJKToRASMatrix(vtk_matrix([[-2, 0, 0, 90], [0, 2, 0, -126], [0, 0, 2, -72], [0, 0, 0, 1]]))
            paths.append(os.path.join(directory, f"sub-0{index + 1}_lesion.nii.gz"))
            self.assertTrue(slicer.util.saveNode(maskNode, paths[-1]))
            slicer.mrmlScene.RemoveNode(maskNode)
        importer.loadFiles(paths, studioShell)
        self._pump(10)
        loadedSet = importer.loadedSet()
        self.assertIsNotNone(loadedSet)
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        # A lesion just loaded from its file (named and styled by the importer, voxels unchanged) is saved (U4)
        state, savedPath, _savedTime = draw.lesionSaveState(loadedSet.current())
        self.assertEqual(state, "saved")
        self.assertIn(os.path.basename(savedPath), [os.path.basename(path) for path in paths])
        nodeCount = slicer.mrmlScene.GetNumberOfNodes()
        index = loadedSet.index
        keys = {portableKey(shortcut.key): shortcut for shortcut in self.mainWindow.findChildren("QShortcut")
                if shortcut.enabled and shortcut.property("studioShortcut")}
        nextKey = keys.get(portableKey(draw.NEXT_SUBJECT_SHORTCUT))
        if nextKey is not None:
            nextKey.activated()
        else:
            draw.stepSubject(1, studioShell)
        self._pump(6)
        self.assertEqual(loadedSet.index, index + 1)
        self.assertEqual(slicer.mrmlScene.GetNumberOfNodes(), nodeCount)
        self.assertEqual(editor.segmentationNode().GetID(), loadedSet.current().GetID())
        loadedSet.close()
        self._pump()

    def test_32_draw_lesion_segment_and_new_lesion(self):
        """Looking at a scan in Draw adds no segment, the first tool adds "Lesion"; a saved lesion offers New lesion (UX-M9, U7)."""
        import os
        import tempfile

        from StudioLib import draw

        studioShell = self._applyStudio()
        studioShell.switchTo("library")
        self._pump()
        volume = slicer.util.loadVolume(self._scanFile())
        self._pump(4)
        for node in slicer.util.getNodesByClass("vtkMRMLSegmentationNode"):
            slicer.mrmlScene.RemoveNode(node)
        studioShell.switchTo("library")
        self._pump()
        studioShell.switchTo("draw")
        self._pump(6)
        editor = slicer.modules.SegmentEditorWidget.editor
        segmentationNode = editor.segmentationNode()
        self.assertIsNotNone(segmentationNode)
        self.assertEqual(editor.sourceVolumeNode().GetID(), volume.GetID())
        self.assertEqual(segmentationNode.GetSegmentation().GetNumberOfSegments(), 0)
        self.assertEqual([node.GetName() for node in slicer.util.getNodesByClass("vtkMRMLSegmentationNode")], [segmentationNode.GetName()])
        # The Brush tile is suggested, and picking it adds the "Lesion" segment
        self.assertIsNotNone(slicer.modules.SegmentEditorWidget.tileOutline("Paint"))
        editor.findChild("QToolButton", "Paint").click()
        self._pump(6)
        self.assertEqual(self._effect(editor), "Paint")
        segmentation = segmentationNode.GetSegmentation()
        self.assertEqual(segmentation.GetNumberOfSegments(), 1)
        self.assertEqual(segmentation.GetNthSegment(0).GetName(), "Lesion")
        self.assertGreater(self._paintCube(editor), 0)
        card = draw.lesionCard()
        self.assertFalse(card.newLesionButton.isVisible())
        directory = tempfile.mkdtemp(prefix="StudioTestNew", dir=slicer.app.temporaryPath)
        self.assertTrue(draw.saveLesionDialog(studioShell, path=os.path.join(directory, "case-001_lesion.nii.gz")))
        self._pump(4)
        self.assertTrue(card.newLesionButton.isVisible())
        saved = editor.segmentationNode()
        card.newLesionButton.click()
        self._pump(8)
        self.assertIsNot(editor.segmentationNode(), saved)
        self.assertEqual(self._effect(editor), "Paint")
        self.assertFalse(card.newLesionButton.isVisible())

    #
    # Trace from a figure (L-29)
    #

    def test_32b_draw_overlay_zero_outline_off_never_nothing(self):
        """Overlay at 0% plus Outline off never leaves the lesion undrawn, in either order (the card's "never nothing" rule)."""
        from StudioLib import draw

        studioShell = self._applyStudio()
        editor = self._startOnTemplate(studioShell)
        self._drag("Red", [(dx, 0) for dx in range(0, 40, 4)])
        self.assertGreater(self._voxels(editor), 0)
        card = draw.extension().displayCard
        displayNode = editor.segmentationNode().GetDisplayNode()
        segmentId = editor.currentSegmentID()

        def drawn():
            return bool((displayNode.GetVisibility2DFill() and displayNode.GetSegmentOpacity2DFill(segmentId) > 0)
                        or displayNode.GetVisibility2DOutline())

        # Slider to 0% first, then the Outline off: the fill comes back
        card.opacitySlider.setValue(0)
        self._pump()
        self.assertTrue(card.outlineSwitch.checked)
        card.outlineSwitch.setChecked(False)
        self._pump()
        self.assertTrue(drawn())
        self.assertGreater(displayNode.GetSegmentOpacity2DFill(segmentId), 0)
        self.assertEqual(card.opacitySlider.value, round(displayNode.GetSegmentOpacity2DFill(segmentId) * 100))
        # The Outline off first, then the slider to 0%: the outline comes back
        card.opacitySlider.setValue(60)
        card.outlineSwitch.setChecked(False)
        self._pump()
        self.assertFalse(displayNode.GetVisibility2DOutline())
        card.opacitySlider.setValue(0)
        self._pump()
        self.assertTrue(drawn())
        self.assertTrue(displayNode.GetVisibility2DOutline())
        self.assertTrue(card.outlineSwitch.checked)

    def test_32c_draw_tool_options_radio_columns_fit_the_inspector(self):
        """The radio buttons of Islands and Scissors (columns in Classic) are one column in Draw, so that the narrow inspector
        does not cut their labels mid-letter; Classic puts them back in their cells.
        """
        studioShell = self._applyStudio()
        editor = self._startOnTemplate(studioShell)
        widget = slicer.modules.SegmentEditorWidget
        widget.setMoreToolsShown(True)
        self._pump()

        def radioCells(frame):
            """{text: (row, column)} of the radio buttons in the grid layouts of the options frame."""
            found = {}
            for layout in frame.findChildren("QGridLayout"):
                for row in range(layout.rowCount()):
                    for column in range(layout.columnCount()):
                        item = layout.itemAtPosition(row, column)
                        if item is not None and item.widget() is not None and item.widget().inherits("QRadioButton"):
                            found[item.widget().text] = (row, column)
            return found

        def cutButtons(effectName):
            editor.setActiveEffectByName(effectName)
            self._wait(300)
            frame = editor.activeEffect().optionsFrame()
            self.assertTrue(frame.isVisible(), effectName)
            buttons = [button for button in frame.findChildren("QAbstractButton") if button.isVisible() and button.text]
            self.assertGreaterEqual(len(buttons), 6, effectName)
            return frame, [(button.text, button.width, button.sizeHint.width()) for button in buttons if button.width < button.sizeHint.width()]

        for effectName in ("Islands", "Scissors"):
            frame, cut = cutButtons(effectName)
            self.assertEqual(cut, [], effectName)
            self.assertEqual({cell[1] for cell in radioCells(frame).values()}, {0}, effectName)
        # Scissors: the operation, the shape and the slice cut one under the other; "Apply to visible segments" with its check box last
        editor.setActiveEffectByName("Scissors")
        self._pump()
        frame = editor.activeEffect().optionsFrame()
        tops = {button.text: button.mapTo(frame, qt.QPoint(0, 0)).y() for button in frame.findChildren("QRadioButton") if button.isVisible()}
        self.assertLess(tops["Erase inside"], tops["Fill outside"])
        self.assertLess(tops["Fill outside"], tops["Free-form"])
        self.assertLess(tops["Rectangle"], tops["Unlimited"])
        editor.setActiveEffectByName("")

        studioShell.classic(persist=False)
        self._pump()
        for effectName, cells in (("Islands", {"Keep largest island": (0, 0), "Keep selected island": (0, 1), "Split islands to segments": (2, 0)}),
                                  ("Scissors", {"Erase inside": (1, 0), "Free-form": (1, 1), "Unlimited": (1, 2)})):
            editor.setActiveEffectByName(effectName)
            self._pump()
            frame = editor.activeEffect().optionsFrame()
            self.assertEqual([layout for layout in frame.findChildren("QGridLayout") if layout.property("studioStackedGridCells")], [], effectName)
            found = radioCells(frame)
            for text, cell in cells.items():
                self.assertEqual(found.get(text), cell, (effectName, text))
        editor.setActiveEffectByName("")

    def test_33_figure_round_trip(self):
        """A synthetic figure and 5 landmark pairs: RMS < 0.5 mm; the crop and fit file are saved; Show source rebuilds the
        overlay in <= 300 ms with the same fit; Ctrl+V follows the clipboard; the trace views are restored on close.
        """
        import os
        import tempfile
        import time

        import numpy as np
        import vtk

        if not _hasFigureTracing():
            self.skipTest("figure tracing is not in this build")
        from LesionNetworkMappingLib import figurefit
        from StudioLib import figure, importer, layouts

        studioShell = self._applyStudio()
        editor = self._startOnTemplate(studioShell)
        self.addCleanup(lambda: figure.closeFigure(removeNodes=True))
        directory = tempfile.mkdtemp(prefix="StudioTestFigure", dir=slicer.app.temporaryPath)
        # A 160 x 120 figure with an asymmetric pattern
        image = qt.QImage(160, 120, qt.QImage.Format_RGB32)
        image.fill(qt.QColor(20, 20, 20))
        painter = qt.QPainter(image)
        painter.fillRect(20, 15, 50, 30, qt.QColor(230, 230, 230))
        painter.fillRect(100, 70, 30, 40, qt.QColor(150, 150, 150))
        painter.end()
        path = os.path.join(directory, "case-7_fig1.png")
        self.assertTrue(image.save(path))
        importer.handleDrop([path], shell=studioShell)
        self._pump(10)
        session = figure.session()
        self.assertIsNotNone(session)
        self.assertEqual(len(session.panels), 1)
        card = self.mainWindow.findChild("QFrame", "StudioFigureCard")
        self.assertIsNotNone(card)
        self.assertTrue(card.isVisible())
        panel = session.panel()
        self.assertEqual(panel.convention, "unknown")
        self.assertEqual(panel.fitConvention, "radiological")
        for node in (panel.figure(), panel.transform(), panel.figureLandmarks(), panel.templateLandmarks()):
            self.assertEqual(node.GetAttribute("StudioFigure"), "1")
        # Truth: an axial cut at z = 10 mm, pitch 6, similarity 0.9 mm per pixel turned 5 degrees
        session.setCut(z=10.0, pitch=6.0, roll=0.0)
        cut = figurefit.cut_plane("axial", 10.0, 6.0, 0.0)
        angle = np.radians(5.0)
        truth = np.array([[0.9 * np.cos(angle), -0.9 * np.sin(angle), 70.0], [0.9 * np.sin(angle), 0.9 * np.cos(angle), -50.0], [0.0, 0.0, 1.0]])
        planePoints = np.array([[-40.0, -30.0], [40.0, -35.0], [0.0, 45.0], [-25.0, 20.0], [30.0, 10.0]])
        pixels = figurefit.apply_2d(figurefit.convention_matrix("radiological"), figurefit.apply_2d(np.linalg.inv(truth), planePoints))
        pixels += np.random.default_rng(3).normal(0.0, 0.2, pixels.shape)
        for index, (pixel, ras) in enumerate(zip(pixels, cut.to_ras(planePoints), strict=True)):
            panel.figureLandmarks().AddControlPoint(vtk.vtkVector3d(*panel.localOf(pixel)))
            panel.templateLandmarks().AddControlPointWorld(vtk.vtkVector3d(*ras))
            panel.templateLandmarks().SetNthControlPointLabel(index, f"L{index + 1}")
        session.updateFit()
        self.assertEqual(panel.pairCount(), 5)
        self.assertIsNotNone(panel.fit, panel.fitError)
        self.assertLess(panel.fit.rms_mm, 0.5)
        self.assertIn("(5 pairs)", card.findChild("QLabel", "StudioFigureResidualChip").text)
        # The affine model needs at least 4 pairs
        self.assertTrue(card.findChild("QFrame", "StudioFigureModel").findChildren("QToolButton")[1].enabled)
        # Trace views: layout 7001, template + figure in Red, the figure alone in Green; restored on close
        layoutBefore = layouts.FOCUS_LAYOUT_ID
        session.enterTrace()
        self._pump(6)
        self.assertEqual(self.layoutManager.layout, layouts.FOCUS_LAYOUT_ID)
        red = self.layoutManager.sliceWidget("Red").mrmlSliceCompositeNode()
        green = self.layoutManager.sliceWidget("Green").mrmlSliceCompositeNode()
        self.assertEqual(red.GetBackgroundVolumeID(), editor.sourceVolumeNode().GetID())
        self.assertEqual(red.GetForegroundVolumeID(), panel.figureId)
        self.assertAlmostEqual(red.GetForegroundOpacity(), figure.DEFAULT_OPACITY)
        self.assertEqual(green.GetBackgroundVolumeID(), panel.figureId)
        # The card's Side control confirms the convention (idClicked(int) on Qt 6, where buttonClicked(int) is gone)
        figure.card().conventionGroup.button(1).click()
        self._pump(2)
        self.assertEqual(panel.convention, "neurological")
        self.assertTrue(panel.confirmed)
        # The convention is confirmed once (every panel of the paper), then saved
        session.setConvention("radiological")
        self.assertTrue(panel.confirmed)
        # Only what the user stated goes to the study row: no record_id, and no figure / panel without labels
        self.assertFalse({"record_id", "figure", "panel"} & set(session.columns()))
        written = session.saveFigures(os.path.join(directory, "sources", "case-7"))
        self.assertEqual(sorted(os.path.basename(name) for name in written), ["fig-1.png", "fig-1_fit.json"])
        jsonPath = next(name for name in written if name.endswith(".json"))
        record = figurefit.read_fit_json(jsonPath)
        self.assertEqual(record["plane"], "axial")
        self.assertEqual(len(record["landmarks"]), 5)
        self.assertTrue(record["convention_confirmed"])
        self.assertLess(record["rms_mm"], 0.5)
        rms = panel.fit.rms_mm
        figure.closeFigure(removeNodes=True)
        self._pump(4)
        self.assertEqual(figure.figureNodes(), [])
        self.assertEqual(self.layoutManager.layout, layoutBefore)
        self.assertNotEqual(red.GetForegroundVolumeID(), panel.figureId)
        # Show source: the overlay from the fit file in <= 300 ms (best of 3: the first build warms caches)
        times = []
        for attempt in range(3):
            start = time.perf_counter()
            rebuilt, _ms = figure.showSource(jsonPath, studioShell)
            times.append((time.perf_counter() - start) * 1000.0)
            self.assertAlmostEqual(rebuilt.fit.rms_mm, rms, places=6)
            self.assertTrue(rebuilt.confirmed)
            if attempt < 2:
                figure.closeFigure(removeNodes=True)
                self._pump(2)
        self.assertLessEqual(min(times), figure.REBUILD_BUDGET_MS, times)
        self.assertEqual(red.GetForegroundVolumeID(), figure.session().panel().figureId)
        # Ctrl+V: a Draw shortcut, enabled only while the clipboard holds an image
        clipboard = qt.QApplication.clipboard()
        self.addCleanup(clipboard.clear)
        clipboard.setText("text")
        self._pump(2)
        paste = [shortcut for shortcut in studioShell.shortcuts() if portableKey(shortcut.key) == "Ctrl+V"]
        self.assertEqual(len(paste), 1)
        self.assertFalse(paste[0].enabled)
        clipboard.setImage(image)
        self._pump(2)
        self.assertTrue(paste[0].enabled)


def _hasFigureTracing():
    """True when this build has trace-from-figure (StudioLib.figure, L-29)."""
    try:
        from StudioLib import figure  # noqa: F401
    except ImportError:
        return False
    return True


def vtk_matrix(rows=None):
    """A vtkMatrix4x4 (identity, or the given rows)."""
    import vtk

    matrix = vtk.vtkMatrix4x4()
    if rows is not None:
        for row in range(4):
            for column in range(4):
                matrix.SetElement(row, column, rows[row][column])
    return matrix

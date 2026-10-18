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
# collides whatever the modifiers of the Studio shortcut are.
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
            if base == reservedBase and (anyModifier or modifiers == reservedModifiers):
                collisions.append((key, description, reservedKey, source))
    return collisions


def assertNoKeyCollisions(shortcutKeys=None, extraReserved=()):
    """Fail if a Studio shortcut uses a key of the views or of the Segment Editor (SPEC 9.6).

    :param shortcutKeys: [(key sequence, description)]; by default the shortcuts that the live Studio
      shell has installed (global shortcuts and those of the current workspace).
    :param extraReserved: more reserved keys [(key, any modifier, source)].
    """
    if shortcutKeys is None:
        from StudioLib import shell

        studioShell = shell.instance()
        if studioShell is None:
            raise AssertionError("assertNoKeyCollisions: Studio is not active")
        shortcutKeys = [(shortcut.key, shortcut.property("studioDescription")) for shortcut in studioShell.shortcuts()]
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
        self.assertNotIn(mainWindow, shell.associatedWidgets(closeSceneAction))

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
        self.assertNotIn(mainWindow, shell.associatedWidgets(closeSceneAction))
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
        self.assertEqual(keyCollisions([("Ctrl+Z", "self check")]), [("Ctrl+Z", "self check", "Ctrl+Z", "Segment Editor shortcut")])
        self.assertTrue(keyCollisions([("Ctrl+]", "any modifier check")]))
        for ws in workspaces.WORKSPACES:
            if not workspaces.isAvailable(ws):
                continue
            studioShell.switchTo(ws.key)
            self._pump()
            assertNoKeyCollisions()
            classicSwitch = self.mainWindow.findChild("QAction", "StudioInterfaceAction")
            keys = [(shortcut.key, shortcut.property("studioDescription")) for shortcut in studioShell.shortcuts()]
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

    def test_10_draw_round_trips(self):
        """Draw arms Paint on arrival and keeps the tool and segment across round trips; Esc is respected (NAV-02, NFT-03)."""
        from StudioLib import draw

        studioShell = self._applyStudio()
        _path, editor = self._openScanInDraw(studioShell)
        # First entry with a lesion segment and a source volume: Paint is armed
        self.assertEqual(self._effect(editor), "Paint")
        segmentationId, segmentId = editor.segmentationNodeID(), editor.currentSegmentID()
        self.assertTrue(segmentId)

        editor.setActiveEffectByName("Erase")
        self._pump()
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

        # Esc (the Segment Editor's deactivate shortcut) inside Draw: the tool stays deselected
        editor.setActiveEffect(None)
        self._pump()
        studioShell.switchTo("library")
        self._pump()
        studioShell.switchTo("draw")
        self._pump()
        self.assertIsNone(editor.activeEffect())

        # A tool that the editor dropped by itself (another segmentation) is armed again on the new one
        editor.setActiveEffectByName("Paint")
        self._pump()
        otherSegmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Other")
        otherSegmentation.SetReferenceImageGeometryParameterFromVolumeNode(editor.sourceVolumeNode())
        editor.setSegmentationNode(otherSegmentation)
        editor.setCurrentSegmentID(otherSegmentation.GetSegmentation().AddEmptySegment("", "Other lesion"))
        self._pump()
        self.assertEqual(self._effect(editor), "Paint")

    def test_11_save_lesion(self):
        """Save lesion writes a NIfTI mask on the scan grid, readable by Slicer, with no node left behind (NFT-01, CRT-05)."""
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
        self.assertEqual(studioShell.primaryActionLabel(), "Save lesion…")
        self.assertIn("Ctrl+Shift+S", [portableKey(shortcut.key) for shortcut in studioShell.shortcuts()])

        voxels = self._paintCube(editor)
        self.assertEqual(voxels, 64)
        segmentationNode, sourceVolumeNode = editor.segmentationNode(), editor.sourceVolumeNode()
        self.assertEqual(draw.defaultLesionFileName(segmentationNode, sourceVolumeNode), "sub-07_lesion.nii.gz")
        sourceVolumeNode.SetName("patient_T1")
        segmentationNode.SetName("Lesions (patient_T1)")
        sourceVolumeNode.GetStorageNode().SetFileName(path.replace("sub-07_T1w", "patient_T1"))
        self.assertEqual(draw.defaultLesionFileName(segmentationNode, sourceVolumeNode), "patient_T1_lesion.nii.gz")
        sourceVolumeNode.GetStorageNode().SetFileName(path)

        settings = qt.QSettings()
        previousFolder = settings.value(draw.LAST_SAVE_FOLDER_KEY)
        settings.remove(draw.LAST_SAVE_FOLDER_KEY)
        try:
            # The scan is in a temporary folder: the dialog proposes the home folder (or Documents), never the application folder
            directory = draw.defaultSaveDirectory(sourceVolumeNode)
            self.assertFalse(draw._isInside(directory, slicer.app.slicerHome))
            self.assertIn(directory, (qt.QDir.homePath(), os.path.join(qt.QDir.homePath(), "Documents")))

            nodeCount = slicer.mrmlScene.GetNumberOfNodes()
            outputPath = os.path.join(os.path.dirname(path), "lesions", "sub-07_lesion")
            written = draw.saveLesionDialog(studioShell, path=outputPath)
            self.assertEqual(written, outputPath + ".nii.gz")
            self.assertTrue(os.path.isfile(written))
            self.assertEqual(os.listdir(os.path.dirname(written)), ["sub-07_lesion.nii.gz"])
            self.assertEqual(slicer.mrmlScene.GetNumberOfNodes(), nodeCount)
            self.assertEqual(slicer.util.settingsValue(draw.LAST_SAVE_FOLDER_KEY, ""), os.path.dirname(written))
            self.assertEqual(draw.defaultSaveDirectory(sourceVolumeNode), os.path.dirname(written))
            self.assertEqual(studioShell._toast.text(), "Saved lesion to sub-07_lesion.nii.gz")

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
        finally:
            if previousFolder is None:
                settings.remove(draw.LAST_SAVE_FOLDER_KEY)
            else:
                settings.setValue(draw.LAST_SAVE_FOLDER_KEY, previousFolder)

        studioShell.switchTo("library")
        self._pump()
        self.assertFalse(saveButton.isVisible())
        self.assertNotIn("Ctrl+Shift+S", [portableKey(shortcut.key) for shortcut in studioShell.shortcuts()])

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
        draw._redo()
        self.assertEqual(count(), voxels)

        studioShell.switchTo("library")
        self._pump()
        self.assertIsNone(self.mainWindow.findChild("QShortcut", "StudioDrawRedoShortcut"))
        studioShell.classic(persist=False)
        self._pump()
        self.assertIsNone(self.mainWindow.findChild("QShortcut", "StudioDrawRedoShortcut"))

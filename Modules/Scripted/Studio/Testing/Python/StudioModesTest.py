"""Mode pill of the Slicer Studio shell (StudioLib/modes.py).

Run with the main window and testing enabled:

  Slicer --no-splash --testing --python-code "import slicer.testing; slicer.testing.runUnitTest(['<this dir>'], 'StudioModesTest'); slicer.app.exit(0)"

Studio never runs by itself under --testing; the tests apply it with ``Shell(force=True)`` and always
restore the Classic interface. They cover:

- the pill of the Place mode (single and persistent) naming the point list, with its Esc exit (the shell's Esc
  ladder) and its ✕;
- the name of the Draw tool that resumes after the mode ("Brush resumes after");
- the pill of the window/level mode, on one view (four-up);
- the pill being hidden while a canvas covers the views (Library), and shown again afterwards;
- Classic and uninstall(): no pill, no observer, no timer.
"""

import qt

import slicer
from slicer import ScriptedLoadableModule


class StudioModesTest(ScriptedLoadableModule.ScriptedLoadableModuleTest):
    """Mode pill of the Studio shell. It needs the main window."""

    def setUp(self):
        slicer.mrmlScene.Clear(0)
        self.mainWindow = slicer.util.mainWindow()
        self.interactionNode = slicer.app.applicationLogic().GetInteractionNode()
        self.selectionNode = slicer.app.applicationLogic().GetSelectionNode()
        self.interactionNode.SwitchToViewTransformMode()
        self.interactionNode.SetPlaceModePersistence(0)

    def tearDown(self):
        from StudioLib import draw, shell

        self.interactionNode.SwitchToViewTransformMode()
        self.interactionNode.SetPlaceModePersistence(0)
        stubbed = self.__dict__.pop("_realSuspendedEffect", None)
        if stubbed is not None:
            draw.suspendedEffect = stubbed
        live = shell._shell
        if live is not None:
            live.dispose()
        slicer.app.processEvents()
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

    def _applyStudio(self, workspace="draw"):
        """Apply Studio and show a workspace: the views are under a canvas in Library, where the pill is hidden."""
        from StudioLib import modes, shell

        self.assertIsNotNone(self.mainWindow, "Studio tests need the main window")
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        self._pump()
        if workspace is not None:
            studioShell.switchTo(workspace)
            self._pump()
        if modes.instance() is None:
            # The plug-in list of this build does not install it yet
            modes.install(studioShell)
        self.assertIsNotNone(modes.instance())
        return studioShell

    def _addPointList(self, name="Lesion centre"):
        node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", name)
        self.selectionNode.SetActivePlaceNodeID(node.GetID())
        return node

    def _wait(self, milliseconds):
        loop = qt.QEventLoop()
        qt.QTimer.singleShot(milliseconds, loop.quit)
        (getattr(loop, "exec", None) or loop.exec_)()

    def _pillByName(self):
        return self.mainWindow.findChild("QToolButton", "StudioModePill")

    #
    # Texts
    #

    def test_mode_text(self):
        """The text of the pill for each mode (a pure function)."""
        from StudioLib import modes

        place = slicer.vtkMRMLInteractionNode.Place
        self.assertEqual(modes.modeText(place, False, "Lesion centre"), "Placing Lesion centre point · click in a view · Esc to cancel")
        self.assertEqual(modes.modeText(place, True, "Lesion centre"), "Placing Lesion centre points · Esc to stop")
        self.assertEqual(modes.modeText(place, False, ""), "Placing a point · click in a view · Esc to cancel")
        self.assertEqual(modes.modeText(slicer.vtkMRMLInteractionNode.AdjustWindowLevel), "Window/level · drag in a view · Esc to exit")
        self.assertEqual(modes.modeText(place, False, "L", "Brush"), "Placing L point · click in a view · Esc to cancel · Brush resumes after")
        self.assertIsNone(modes.modeText(slicer.vtkMRMLInteractionNode.ViewTransform))
        # Long list names are shortened
        self.assertLess(len(modes.modeText(place, False, "x" * 100)), 100)

    #
    # Place
    #

    def test_place_pill(self):
        """Place mode shows the pill with the name of the list; Esc (the shell's ladder) and the pill's ✕ end the mode."""
        from StudioLib import modes

        studioShell = self._applyStudio()
        centralWidget = self.mainWindow.centralWidget()
        self.assertFalse(modes.pill().isVisible())
        self._addPointList("Lesion centre")
        self.interactionNode.SetCurrentInteractionMode(self.interactionNode.Place)
        self._pump()
        pill = modes.pill()
        self.assertEqual(pill, self._pillByName())
        self.assertTrue(pill.isVisible())
        self.assertEqual(pill.objectName, "StudioModePill")
        self.assertEqual(modes.instance().pill.fullText, "Placing Lesion centre point · click in a view · Esc to cancel")
        self.assertEqual(pill.parentWidget(), centralWidget)
        # Top centre of one view (the largest one when the mouse pointer is not over a view)
        target = modes.instance()._targetViewRect()
        self.assertIsNotNone(target)
        self.assertAlmostEqual(pill.x + pill.width / 2, target.x() + target.width() / 2, delta=1.5)
        self.assertEqual(pill.y, max(0, target.y()) + modes.PILL_TOP)
        self.assertGreaterEqual(pill.height, 24)

        # Persistent placement says "points"
        self.interactionNode.SetPlaceModePersistence(1)
        self._pump()
        self.assertEqual(modes.instance().pill.fullText, "Placing Lesion centre points · Esc to stop")
        self.interactionNode.SetPlaceModePersistence(0)
        self._pump()
        self.assertEqual(modes.instance().pill.fullText, "Placing Lesion centre point · click in a view · Esc to cancel")

        # Esc: one rung, then the pill is gone and the mouse mode is back to navigation
        self.assertTrue(studioShell.handleEscape())
        self._pump()
        self.assertEqual(self.interactionNode.GetCurrentInteractionMode(), self.interactionNode.ViewTransform)
        self.assertFalse(pill.isVisible())
        self.assertFalse(studioShell.handleEscape())

        # The pill's ✕ does the same
        self.interactionNode.SetCurrentInteractionMode(self.interactionNode.Place)
        self._pump()
        self.assertTrue(pill.isVisible())
        pill.click()
        self._pump()
        self.assertEqual(self.interactionNode.GetCurrentInteractionMode(), self.interactionNode.ViewTransform)
        self.assertFalse(pill.isVisible())

    def test_place_pill_follows_the_active_list(self):
        """Another active list renames the pill while Place is active."""
        from StudioLib import modes

        self._applyStudio()
        self._addPointList("First")
        self.interactionNode.SetCurrentInteractionMode(self.interactionNode.Place)
        self._pump()
        self.assertIn("First", modes.instance().pill.fullText)
        self._addPointList("Second")
        self._pump()
        self.assertIn("Second", modes.instance().pill.fullText)

    def test_place_pill_names_resuming_tool(self):
        """When Draw suspended a tool for the mode, the pill says that it resumes."""
        from StudioLib import draw, modes

        self._applyStudio()
        self._addPointList("Lesion centre")
        self._realSuspendedEffect = draw.suspendedEffect
        draw.suspendedEffect = lambda shell=None: "Paint"
        self.interactionNode.SetCurrentInteractionMode(self.interactionNode.Place)
        self._pump()
        self.assertEqual(modes.instance().pill.fullText, "Placing Lesion centre point · click in a view · Esc to cancel · Brush resumes after")
        # A tool name without a Studio name is shown as it is
        draw.suspendedEffect = lambda shell=None: "Scissors"
        self.interactionNode.SetPlaceModePersistence(1)
        self._pump()
        self.assertEqual(modes.instance().pill.fullText, "Placing Lesion centre points · Esc to stop · Scissors resumes after")
        draw.suspendedEffect = lambda shell=None: None
        self.interactionNode.SetPlaceModePersistence(0)
        self._pump()
        self.assertNotIn("resumes", modes.instance().pill.fullText)

    def test_place_pill_with_draw_tool(self):
        """With the real Draw session: Place suspends the brush, the pill says it resumes, and Esc arms it again."""
        from StudioLib import draw, importer, modes

        studioShell = self._applyStudio()
        importer.startOnTemplate(2, studioShell)
        self._pump(30)
        draw.requestArm(studioShell)
        self._pump(30)
        editor = draw.segmentEditor()
        self.assertEqual(editor.activeEffect().name if editor.activeEffect() else None, "Paint")
        self._addPointList("Lesion centre")
        self.interactionNode.SetCurrentInteractionMode(self.interactionNode.Place)
        self._pump(30)
        self.assertEqual(draw.suspendedEffect(studioShell), "Paint")
        self.assertTrue(modes.pill().isVisible())
        self.assertEqual(modes.instance().pill.fullText,
                         "Placing Lesion centre point · click in a view · Esc to cancel · Brush resumes after")
        self.assertTrue(studioShell.handleEscape())
        self._pump(30)
        self.assertFalse(modes.pill().isVisible())
        self.assertEqual(editor.activeEffect().name if editor.activeEffect() else None, "Paint")
        self.assertIsNone(draw.suspendedEffect(studioShell))

    #
    # Window/level
    #

    def test_wl_pill(self):
        """The window/level mode shows its pill with the exit; Esc and the pill's ✕ end it."""
        from StudioLib import modes

        studioShell = self._applyStudio()
        self.interactionNode.SetCurrentInteractionMode(self.interactionNode.AdjustWindowLevel)
        self._pump()
        pill = modes.pill()
        self.assertTrue(pill.isVisible())
        self.assertEqual(modes.instance().pill.fullText, "Window/level · drag in a view · Esc to exit")
        self.assertTrue(studioShell.handleEscape())
        self._pump()
        self.assertFalse(pill.isVisible())
        self.assertEqual(self.interactionNode.GetCurrentInteractionMode(), self.interactionNode.ViewTransform)
        self.interactionNode.SetCurrentInteractionMode(self.interactionNode.AdjustWindowLevel)
        self._pump()
        self.assertTrue(pill.isVisible())
        pill.click()
        self._pump()
        self.assertFalse(pill.isVisible())

    def test_pill_follows_resize(self):
        """The pill stays centred when the view area changes size, and is elided when it is narrow."""
        from StudioLib import modes

        self._applyStudio()
        self._addPointList("Lesion centre")
        self.interactionNode.SetCurrentInteractionMode(self.interactionNode.Place)
        self._pump()
        pill = modes.pill()
        centralWidget = self.mainWindow.centralWidget()
        modes.instance().pill.updatePosition()
        self.assertLessEqual(pill.width, centralWidget.width)
        controller = modes.instance()
        # A narrow view area: the text is elided to the available width, the full text stays as the accessible name
        controller.pill.parentWidget = _Narrow(centralWidget, 340)
        controller.pill.updatePosition()
        self.assertLessEqual(pill.width, 340 - 2 * modes.PILL_SIDE_RESERVE)
        self.assertTrue(pill.text.endswith("…"), pill.text)
        self.assertIn("Esc to cancel", pill.accessibleName)

    def test_pill_on_one_view_in_four_up(self):
        """Four-up: the pill is on one view (the hovered one, else the largest), never across the views."""
        from StudioLib import hud, modes

        self._applyStudio()
        layoutManager = slicer.app.layoutManager()
        layoutManager.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
        self._pump(10)
        centralWidget = self.mainWindow.centralWidget()

        def pillRect():
            pill = modes.pill()
            return qt.QRect(pill.mapTo(centralWidget, qt.QPoint(0, 0)), pill.size)

        def viewsUnder(rect):
            return [name for name, viewRect in hud.viewRects(centralWidget) if viewRect.intersects(rect)]

        # No view under the mouse pointer: the largest view
        qt.QCursor.setPos(self.mainWindow.mapToGlobal(qt.QPoint(2, 2)))
        self.interactionNode.SetCurrentInteractionMode(self.interactionNode.AdjustWindowLevel)
        self._pump()
        self.assertTrue(modes.pill().isVisible())
        self.assertEqual(len(viewsUnder(pillRect())), 1, viewsUnder(pillRect()))
        self.interactionNode.SwitchToViewTransformMode()
        self._pump()

        # The mode starts with the mouse pointer over Green: the pill is on Green, and stays there when it moves
        green = dict(hud.viewRects(centralWidget))["Green"]
        qt.QCursor.setPos(centralWidget.mapToGlobal(green.center()))
        self._pump()
        self.interactionNode.SetCurrentInteractionMode(self.interactionNode.AdjustWindowLevel)
        self._pump()
        if modes.instance().pill._availableWidth(green) >= modes.instance().pill.fullWidth():
            self.assertEqual(viewsUnder(pillRect()), ["Green"])
        else:
            self.assertEqual(len(viewsUnder(pillRect())), 1)
        target = modes.instance().targetView
        qt.QCursor.setPos(self.mainWindow.mapToGlobal(qt.QPoint(2, 2)))
        modes.instance().refresh()
        self._pump()
        self.assertEqual(modes.instance().targetView, target)

    #
    # Library canvas, Classic
    #

    def test_pill_hidden_over_library_canvas(self):
        """A canvas over the views (Library) hides the pill; it is back when the canvas is closed."""
        from StudioLib import modes

        studioShell = self._applyStudio()
        self._addPointList("Lesion centre")
        self.interactionNode.SetCurrentInteractionMode(self.interactionNode.Place)
        self._pump()
        pill = modes.pill()
        self.assertTrue(pill.isVisible())
        canvas = qt.QWidget()
        canvas.setObjectName("StudioTestCanvas")
        try:
            studioShell.setCanvas(canvas)
            self._pump()
            # The shell does not signal the canvas: the pill notices it within its check interval
            self._wait(modes.CANVAS_CHECK_MS * 3)
            self.assertFalse(pill.isVisible())
            # A mode started over the canvas does not show the pill either
            self.interactionNode.SwitchToViewTransformMode()
            self._pump()
            self.interactionNode.SetCurrentInteractionMode(self.interactionNode.AdjustWindowLevel)
            self._pump()
            self.assertFalse(pill.isVisible())
            studioShell.setCanvas(None)
            self._wait(modes.CANVAS_CHECK_MS * 3)
            self.assertTrue(pill.isVisible())
        finally:
            studioShell.setCanvas(None)
            canvas.hide()
            canvas.deleteLater()

    def test_library_workspace_hides_the_pill(self):
        """The real Library canvas hides the pill too."""
        from StudioLib import modes

        studioShell = self._applyStudio("library")
        if studioShell.canvas() is None:
            return
        self.interactionNode.SetCurrentInteractionMode(self.interactionNode.AdjustWindowLevel)
        self._pump()
        self.assertFalse(modes.pill().isVisible())
        studioShell.switchTo("draw")
        self._wait(modes.CANVAS_CHECK_MS * 3)
        self.assertTrue(modes.pill().isVisible())

    def test_uninstall_cleans(self):
        """Classic has no pill: the widget, the observers and the timers are gone."""
        from StudioLib import modes

        studioShell = self._applyStudio()
        controller = modes.instance()
        self._addPointList("Lesion centre")
        self.interactionNode.SetCurrentInteractionMode(self.interactionNode.Place)
        self._pump()
        self.assertIsNotNone(self._pillByName())
        self.assertTrue(controller.canvasTimer.isActive())

        studioShell.classic(persist=False)
        self._pump()
        if modes.instance() is not None:
            modes.uninstall(None)
        self.assertIsNone(modes.instance())
        self.assertIsNone(modes.pill())
        slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
        self.assertIsNone(self._pillByName())
        self.assertEqual(len(self.mainWindow.centralWidget().findChildren("QToolButton", "StudioModePill")), 0)
        self.assertFalse(controller.updateTimer.isActive())
        self.assertFalse(controller.canvasTimer.isActive())
        self.assertEqual(controller._observed, [])
        self.assertEqual(controller.sceneTags, [])
        # Place mode in Classic shows nothing
        self.interactionNode.SetCurrentInteractionMode(self.interactionNode.AdjustWindowLevel)
        self._pump()
        self.assertIsNone(self._pillByName())
        # Installing without an active shell shows nothing either
        modes.install(studioShell)
        self.assertIsNone(modes.instance())
        modes.install(None)
        self.assertIsNone(modes.instance())

    def test_reinstall_does_not_leak(self):
        """Installing twice leaves one pill (apply Studio again without leaving it)."""
        from StudioLib import modes

        studioShell = self._applyStudio()
        modes.install(studioShell)
        modes.install(studioShell)
        self._pump()
        slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
        self.assertEqual(len(self.mainWindow.findChildren("QToolButton", "StudioModePill")), 1)


class _Narrow:
    """A stand-in for the central widget with another width (the pill only reads ``width``)."""

    def __init__(self, widget, width):
        self._widget = widget
        self.width = width

    def __getattr__(self, name):
        return getattr(self._widget, name)

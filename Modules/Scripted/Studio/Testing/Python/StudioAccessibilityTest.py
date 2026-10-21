r"""Accessibility, first-run and close tests of Slicer Studio (U-03, USABILITY 8 rule 10, 9 and 12).

Run with the main window and testing enabled:

  Slicer --no-splash --testing --python-code "import slicer.testing; slicer.testing.runUnitTest(['<this dir>'], 'StudioAccessibilityTest'); slicer.app.exit(0)"

Studio never runs by itself under --testing; the tests apply it with ``Shell(force=True)`` and always restore the
Classic interface. They cover:

- the AltGr guard: a focused text input keeps a printable Ctrl+Alt character ('@'), a view keeps the workspace keys;
- visible keyboard focus: the focus ring of the buttons (>= 3:1 against the unfocused state) and the focus frame of
  the slice views (Tab or F6 shows it, a mouse press hides it);
- Studio fonts that follow the application font live, with the panel title larger than the body text;
- the narrow window mode (no top bar control beyond the window edge at 960 px);
- the research-use line of the Library footer and its "Don't show again" (the official key, written once);
- the close prompt modes (saved lesion and template only, unsaved lesion edits, Save lesion then Cancel).
"""

import os

import qt

import slicer
from slicer import ScriptedLoadableModule


def _luminance(color):
    def channel(value):
        value = value / 255.0
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(color.red()) + 0.7152 * channel(color.green()) + 0.0722 * channel(color.blue())


def focusChangeStats(focusedImage, unfocusedImage):
    """(pixels whose contrast between the two images is >= 3:1, largest contrast) of two images of the same size."""
    focusedImage = focusedImage.convertToFormat(qt.QImage.Format_RGB32)
    unfocusedImage = unfocusedImage.convertToFormat(qt.QImage.Format_RGB32)
    count = 0
    largest = 1.0
    for y in range(min(focusedImage.height(), unfocusedImage.height())):
        for x in range(min(focusedImage.width(), unfocusedImage.width())):
            first = _luminance(qt.QColor(focusedImage.pixel(x, y)))
            second = _luminance(qt.QColor(unfocusedImage.pixel(x, y)))
            ratio = (max(first, second) + 0.05) / (min(first, second) + 0.05)
            largest = max(largest, ratio)
            if ratio >= 3.0:
                count += 1
    return count, largest


class StudioAccessibilityTest(ScriptedLoadableModule.ScriptedLoadableModuleTest):
    """Accessibility, first-run and close tests of the Studio shell. They need the main window."""

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
        if self.layoutManager is not None:
            self.layoutManager.removeAllMaximizedViewNodes()
            if self.initialLayout is not None and self.layoutManager.layout != self.initialLayout:
                self.layoutManager.setLayout(self.initialLayout)
        if self.initialGeometry is not None:
            self.mainWindow.restoreGeometry(self.initialGeometry)
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
        for _index in range(count):
            slicer.app.processEvents()

    def _wait(self, ms):
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

    def _focus(self, widget, reason=qt.Qt.OtherFocusReason):
        """Give ``widget`` the keyboard focus. Return True if it has it."""
        self.mainWindow.activateWindow()
        widget.setFocus(reason)
        self._pump()
        return slicer.app.focusWidget() == widget

    @staticmethod
    def _keyEvent(eventType, key, modifiers, text):
        event = qt.QKeyEvent(eventType, key, modifiers, text)
        if eventType == qt.QEvent.ShortcutOverride:
            # Qt asks whether a shortcut may take the key: accepted means "not a shortcut"
            event.ignore()
        return event

    def _redFocusWidget(self):
        sliceView = self.layoutManager.sliceWidget("Red").sliceView()
        return sliceView.VTKWidget() if hasattr(sliceView, "VTKWidget") else sliceView

    #
    # AltGr
    #

    def test_altgr_guard(self):
        """A focused line edit keeps AltGr+2 ('@', Ctrl+Alt on Windows); in a view Ctrl+Alt+2 still switches workspace."""
        from StudioLib import a11y

        studioShell = self._applyStudio()
        self.assertIsNotNone(a11y.instance())
        studioShell.switchTo("library")
        self._pump()
        lineEdit = qt.QLineEdit(self.mainWindow)
        lineEdit.setObjectName("StudioAccessibilityTestLineEdit")
        lineEdit.setGeometry(10, 60, 200, 24)
        lineEdit.show()
        try:
            if not self._focus(lineEdit):
                self.skipTest("the window cannot take the keyboard focus here")
            ctrlAlt = qt.Qt.ControlModifier | qt.Qt.AltModifier
            override = self._keyEvent(qt.QEvent.ShortcutOverride, qt.Qt.Key_2, ctrlAlt, "@")
            qt.QApplication.sendEvent(lineEdit, override)
            self.assertTrue(override.isAccepted(), "the AltGr character must not be taken by a shortcut")
            qt.QApplication.sendEvent(lineEdit, self._keyEvent(qt.QEvent.KeyPress, qt.Qt.Key_2, ctrlAlt, "@"))
            self._pump()
            self.assertEqual(lineEdit.text, "@")
            self.assertEqual(studioShell.currentWorkspace(), "library")
            # Without a character (a real Ctrl+Alt+2) and with other modifiers the shortcuts keep the key
            for modifiers, text in ((ctrlAlt, ""), (qt.Qt.ControlModifier, "2"), (ctrlAlt | qt.Qt.ShiftModifier, "@")):
                override = self._keyEvent(qt.QEvent.ShortcutOverride, qt.Qt.Key_2, modifiers, text)
                qt.QApplication.sendEvent(lineEdit, override)
                self.assertFalse(override.isAccepted(), (modifiers, text))

            # In a view the workspace key keeps working (documented: AltGr+digit acts as Ctrl+Alt+digit on Windows)
            redWidget = self._redFocusWidget()
            studioShell.switchTo("draw")
            self._pump()
            if not self._focus(redWidget):
                self.skipTest("the view cannot take the keyboard focus here")
            override = self._keyEvent(qt.QEvent.ShortcutOverride, qt.Qt.Key_1, ctrlAlt, "¡")
            qt.QApplication.sendEvent(redWidget, override)
            self.assertFalse(override.isAccepted())
            shortcut = next(shortcut for shortcut in studioShell.shortcuts()
                            if shortcut.key.toString(qt.QKeySequence.PortableText) == "Ctrl+Alt+1")
            shortcut.activated()
            self._pump()
            self.assertEqual(studioShell.currentWorkspace(), "library")
        finally:
            lineEdit.hide()
            lineEdit.setParent(None)
            lineEdit.deleteLater()

    #
    # Focus visibility
    #

    def _grabAround(self, widget, pad=4):
        self._pump()
        top = widget.window()
        topLeft = widget.mapTo(top, qt.QPoint(0, 0))
        return top.grab(qt.QRect(topLeft.x() - pad, topLeft.y() - pad, widget.width + 2 * pad, widget.height + 2 * pad)).toImage()

    def _ringStats(self, widget):
        """Contrast of the focus change of ``widget`` (focused as after Tab, then unfocused)."""
        window = widget.window()
        window.activateWindow()
        # As after a Tab key press (QApplication sets it on Tab and clears it on a mouse press)
        window.setAttribute(qt.Qt.WA_KeyboardFocusChange, True)
        widget.setFocus(qt.Qt.TabFocusReason)
        self._pump()
        if not widget.hasFocus():
            self.skipTest("the window cannot take the keyboard focus here")
        focusedSize = (widget.width, widget.height)
        focused = self._grabAround(widget)
        widget.clearFocus()
        self._pump()
        self.assertEqual((widget.width, widget.height), focusedSize, "the focus must not change the size")
        return focusChangeStats(focused, self._grabAround(widget))

    def test_focus_ring_visible(self):
        """A button focused with Tab differs from its unfocused state by >= 3:1 along a 2 px ring, with the application
        style (Library buttons, search pill, panel header) and with the fallback role stylesheets of ``ui``.
        """
        from StudioLib import ui

        studioShell = self._applyStudio()
        studioShell.switchTo("library")
        self._pump(8)
        canvas = studioShell.canvas()
        widgets = [self.mainWindow.findChild("QPushButton", "StudioSearchPill")]
        widgets += [canvas.findChild("QPushButton", name) for name in ("StudioLibraryOpenButton", "StudioLibraryStartOnTemplateButton")]
        studioShell.switchTo("draw")
        self._pump(8)
        for widget in widgets:
            self.assertIsNotNone(widget)
        for widget in widgets + [self.mainWindow.findChild("QToolButton", "StudioPanelHelpButton")]:
            if widget is None or not widget.isVisible():
                if widget is not None and widget.objectName != "StudioPanelHelpButton":
                    studioShell.switchTo("library")
                    self._pump(8)
                if widget is None or not widget.isVisible():
                    continue
            count, largest = self._ringStats(widget)
            perimeter = 2 * (widget.width + widget.height)
            # A 2 px ring: about twice the perimeter (the rounded corners are antialiased)
            self.assertGreaterEqual(count, int(1.6 * perimeter), (widget.objectName, count, perimeter, largest))

        # The fallback stylesheets of the roles (application styles without slicerRole support)
        host = qt.QWidget(self.mainWindow)
        host.setGeometry(10, 60, 400, 60)
        layout = qt.QHBoxLayout(host)
        buttons = []
        for role in ("secondary", "primary", "ghost"):
            button = qt.QPushButton(role.capitalize())
            button.setProperty("slicerRole", role)
            button.setStyleSheet(ui._roleStyleSheet(role))
            layout.addWidget(button)
            buttons.append(button)
        host.show()
        host.raise_()
        try:
            for button in buttons:
                count, largest = self._ringStats(button)
                perimeter = 2 * (button.width + button.height)
                self.assertGreaterEqual(count, int(1.6 * perimeter), (button.text, count, perimeter, largest))
        finally:
            host.hide()
            host.setParent(None)
            host.deleteLater()

    def test_view_focus_frame(self):
        """Tab (or F6) into a slice view shows a 2 px accent frame; a mouse press or leaving the view hides it; the focus
        that follows the mouse does not show it. Only the view that shows the frame is filtered (no Python code on the
        hover and render paths of the views).
        """
        from StudioLib import a11y

        studioShell = self._applyStudio()
        studioShell.switchTo("draw")
        self.layoutManager.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
        self._pump(8)
        plugin = a11y.instance()
        self.assertIsNotNone(plugin)
        plugin.updateViewFrames()
        frame = plugin.viewFrame("Red")
        self.assertIsNotNone(frame)
        self.assertFalse(any(viewFrame.filtering for viewFrame in plugin.viewFrames.values()))
        redWidget = self._redFocusWidget()
        yellowWidget = self.layoutManager.sliceWidget("Yellow").sliceView()
        yellowWidget = yellowWidget.VTKWidget() if hasattr(yellowWidget, "VTKWidget") else yellowWidget

        # The keyboard: the pointer is elsewhere (on the top bar)
        qt.QCursor.setPos(studioShell.topBar.toolBar.mapToGlobal(qt.QPoint(5, 5)))
        if not self._focus(redWidget, qt.Qt.TabFocusReason):
            self.skipTest("the view cannot take the keyboard focus here")
        self.assertTrue(frame.isVisible())
        self.assertEqual(frame.width, redWidget.width)
        self.assertEqual(frame.height, redWidget.height)
        self.assertTrue(frame.testAttribute(qt.Qt.WA_TransparentForMouseEvents))
        self.assertTrue(plugin.viewFrames["Red"].filtering)

        # A mouse press hides it, and the view is no longer filtered
        center = qt.QPointF(redWidget.width / 2, redWidget.height / 2)
        globalCenter = qt.QPointF(redWidget.mapToGlobal(center.toPoint()))
        press = qt.QMouseEvent(qt.QEvent.MouseButtonPress, center, globalCenter, qt.Qt.MiddleButton, qt.Qt.MiddleButton, qt.Qt.NoModifier)
        qt.QApplication.sendEvent(redWidget, press)
        release = qt.QMouseEvent(qt.QEvent.MouseButtonRelease, center, globalCenter, qt.Qt.MiddleButton, qt.Qt.NoButton, qt.Qt.NoModifier)
        qt.QApplication.sendEvent(redWidget, release)
        self._pump()
        self.assertFalse(frame.isVisible())
        self.assertFalse(plugin.viewFrames["Red"].filtering)

        # The focus follows the mouse into Yellow: no frame
        qt.QCursor.setPos(yellowWidget.mapToGlobal(qt.QPoint(yellowWidget.width // 2, yellowWidget.height // 2)))
        self._focus(yellowWidget, qt.Qt.MouseFocusReason)
        self.assertFalse(plugin.viewFrame("Yellow").isVisible())
        self.assertFalse(any(viewFrame.filtering for viewFrame in plugin.viewFrames.values()))
        # F6 (ShortcutFocusReason) into Red shows it; leaving the view with Tab hides it and shows the next one
        qt.QCursor.setPos(studioShell.topBar.toolBar.mapToGlobal(qt.QPoint(5, 5)))
        self._focus(redWidget, qt.Qt.ShortcutFocusReason)
        self.assertTrue(frame.isVisible())
        self._focus(yellowWidget, qt.Qt.TabFocusReason)
        self.assertFalse(frame.isVisible())
        self.assertTrue(plugin.viewFrame("Yellow").isVisible())
        self.assertEqual([name for name, viewFrame in plugin.viewFrames.items() if viewFrame.filtering], ["Yellow"])

        # Classic: no frame and no filter
        studioShell.classic(persist=False)
        self._pump()
        self.assertIsNone(a11y.instance())
        self._focus(redWidget, qt.Qt.TabFocusReason)
        self.assertIsNone(redWidget.findChild("QFrame", "StudioViewFocusFrame"))

    #
    # Fonts and window sizes
    #

    def test_font_scaling_live(self):
        """With a 14 pt application font the Studio labels grow without a restart; the panel title stays larger than the body."""
        from StudioLib import ui

        studioShell = self._applyStudio()
        studioShell.switchTo("draw")
        self._pump(8)
        title = self.mainWindow.findChild("QLabel", "StudioPanelHeaderTitle")
        self.assertIsNotNone(title)
        frame, _layout = ui.card("Card title")
        cardTitle = frame.findChild("QLabel", "StudioCardTitle")
        originalFont = qt.QFont(qt.QApplication.font())
        titleBefore = qt.QFontInfo(title.font).pixelSize()
        cardTitleBefore = qt.QFontInfo(cardTitle.font).pixelSize()
        try:
            font = qt.QFont(originalFont)
            font.setPointSizeF(14.0)
            qt.QApplication.setFont(font)
            self._pump(8)
            bodyPx = ui.applicationFontPx()
            titleAfter = qt.QFontInfo(title.font).pixelSize()
            self.assertGreater(titleAfter, titleBefore)
            self.assertGreater(titleAfter, bodyPx)
            self.assertGreater(qt.QFontInfo(cardTitle.font).pixelSize(), cardTitleBefore)
            # The role stylesheets follow too
            self.assertIn(f"font-size: {ui.fontPx(12)}px", ui._roleStyleSheet("chip"))
        finally:
            qt.QApplication.setFont(originalFont)
            self._pump(8)
            frame.deleteLater()
        self.assertEqual(qt.QFontInfo(title.font).pixelSize(), titleBefore)

    def test_narrow_mode(self):
        """At 960x540 (QT_SCALE_FACTOR=2 on a 1080p screen) the top bar is compact and no control is beyond the window
        edge; at 1920 it is normal again.
        """
        from StudioLib import a11y

        studioShell = self._applyStudio()
        studioShell.switchTo("draw")
        self._pump(8)
        topBar = studioShell.topBar
        # The Draw layout of a 960x540 window (QT_SCALE_FACTOR=2): views side by side need more room
        self.layoutManager.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutOneUpRedSliceView)
        self.mainWindow.showNormal()
        self.mainWindow.resize(960, 540)
        self._wait(200)
        self.assertLess(self.mainWindow.width, 1100, f"minimum width {self.mainWindow.minimumSizeHint.width()}")
        self.assertTrue(topBar.isCompact())
        self.assertTrue(a11y.instance().isNarrow())
        toolBar = topBar.toolBar
        right = self.mainWindow.width
        for child in toolBar.findChildren("QWidget"):
            if not child.isVisible() or not child.isWidgetType():
                continue
            childRight = child.mapTo(self.mainWindow, qt.QPoint(child.width, 0)).x()
            self.assertLessEqual(childRight, right + 1, child.objectName or child.className())
        dock = self.mainWindow.findChild("QDockWidget", "PanelDockWidget")
        self.assertLessEqual(dock.mapTo(self.mainWindow, qt.QPoint(dock.width, 0)).x(), right + 1)

        self.mainWindow.resize(1920, 1000)
        self._wait(200)
        self.assertFalse(topBar.isCompact())
        self.assertFalse(a11y.instance().isNarrow())

    #
    # First run
    #

    def test_disclaimer_line(self):
        """The disclaimer is a Library footer line in Studio; "Don't show again" writes the official key once (and only
        then); Classic gets the modal (the application property is cleared).
        """
        from StudioLib import firstrun, library

        settings = qt.QSettings()
        key = firstrun.DONT_SHOW_DISCLAIMER_KEY
        previous = settings.value(key) if settings.contains(key) else None
        settings.remove(key)
        previousProperty = slicer.app.property(firstrun.DISCLAIMER_HANDLED_PROPERTY)
        try:
            studioShell = self._applyStudio()
            self.assertTrue(slicer.app.property(firstrun.DISCLAIMER_HANDLED_PROPERTY))
            studioShell.switchTo("library")
            self._pump(8)
            if hasattr(self.mainWindow, "disclaimerRequested"):
                self.assertTrue(firstrun.connectDisclaimer())
                self.mainWindow.disclaimerRequested("Thank you for using 3D Slicer!<br><br>This software is not intended for clinical use.")
            else:
                # Without the main window hook (W3-03): as if it had been emitted
                firstrun.onDisclaimerRequested("This software is not intended for clinical use.")
            self._pump(4)
            self.assertIn("not intended for clinical use", firstrun.disclaimerMessage())
            canvas = studioShell.canvas()
            footerText = canvas.findChild("QLabel", "StudioLibraryFooterText")
            self.assertIsNotNone(footerText)
            self.assertTrue(footerText.isVisible())
            self.assertEqual(footerText.text, "Research use only, not for clinical use")
            self.assertFalse(settings.contains(key), "the key is written only on an explicit click")
            self.assertIsNone(slicer.app.activeModalWidget())
            action = canvas.findChild("QPushButton", "StudioLibraryFooterAction")
            self.assertIsNotNone(action)
            self.assertEqual(action.text, "Don't show again")
            action.click()
            self._pump(4)
            self.assertEqual(int(settings.value(key)), int(qt.QMessageBox.Ok))
            self.assertFalse(footerText.isVisible())
            # Classic keeps the official modal
            studioShell.classic(persist=False)
            self._pump()
            self.assertFalse(slicer.app.property(firstrun.DISCLAIMER_HANDLED_PROPERTY))
            self.assertNotIn(firstrun.DISCLAIMER_FOOTER_KEY, getattr(library, "_footerLines", {}))
        finally:
            if previous is None:
                settings.remove(key)
            else:
                settings.setValue(key, previous)
            slicer.app.setProperty(firstrun.DISCLAIMER_HANDLED_PROPERTY, previousProperty)

    #
    # Close prompt
    #

    def _scanFile(self, name="sub-01_T1w.nii.gz"):
        import tempfile

        import numpy as np

        directory = tempfile.mkdtemp(prefix="StudioAccessibilityTest", dir=slicer.app.temporaryPath)
        path = f"{directory}/{name}"
        node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "StudioTestScan")
        z, y, x = np.mgrid[0:24, 0:28, 0:20]
        slicer.util.updateVolumeFromArray(node, (100 * np.exp(-((x - 10) ** 2 + (y - 14) ** 2 + (z - 12) ** 2) / 50.0)).astype(np.int16))
        node.SetSpacing(2.0, 2.0, 2.5)
        self.assertTrue(slicer.util.saveNode(node, path))
        slicer.mrmlScene.RemoveNode(node)
        return path

    def _closeProperties(self):
        from StudioLib import firstrun

        # As qSlicerMainWindow::confirmCloseApplication: clear the properties, then ask
        self.mainWindow.setProperty(firstrun.CLOSE_MODE_PROPERTY, "")
        self.mainWindow.setProperty(firstrun.CLOSE_SUMMARY_PROPERTY, "")
        self.mainWindow.closeSummaryRequested()
        return (str(self.mainWindow.property(firstrun.CLOSE_MODE_PROPERTY) or ""),
                str(self.mainWindow.property(firstrun.CLOSE_SUMMARY_PROPERTY) or ""))

    def test_close_summary_modes(self):
        """Saved lesion + template only: "lesionSaved"; one more stroke: "unsavedLesion" naming the lesion; Save lesion
        with a cancelled file dialog: SlicerStudioSaveResult is "cancelled" (the application stays open).
        """
        import numpy as np

        from StudioLib import draw, firstrun, importer, space

        if not hasattr(self.mainWindow, "closeSummaryRequested"):
            self.skipTest("the main window has no close summary hook (W3-03)")
        studioShell = self._applyStudio()
        path = self._scanFile()
        importer.loadFiles([path], studioShell)
        self._pump(8)
        editor = slicer.modules.SegmentEditorWidget.editor
        segmentationNode, sourceVolumeNode = editor.segmentationNode(), editor.sourceVolumeNode()
        self.assertIsNotNone(segmentationNode)
        # The template (never written by Studio) does not count as unsaved
        template = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "MNI template")
        template.SetAttribute(space.ROLE_ATTRIBUTE, space.ROLE_TEMPLATE)

        # Nothing drawn: the classic prompt
        self.assertEqual(self._closeProperties(), ("", ""))

        segmentId = editor.currentSegmentID()
        array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, sourceVolumeNode)
        array[8:12, 10:14, 6:10] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, sourceVolumeNode)
        mode, summary = self._closeProperties()
        self.assertEqual(mode, firstrun.CLOSE_MODE_UNSAVED_LESION)
        self.assertEqual(summary, f"{segmentationNode.GetName()} has not been saved")

        written = draw.saveLesionDialog(studioShell, path=os.path.join(os.path.dirname(path), "sub-01_lesion.nii.gz"))
        self.assertTrue(written)
        mode, summary = self._closeProperties()
        self.assertEqual(mode, firstrun.CLOSE_MODE_LESION_SAVED)
        self.assertTrue(summary.startswith("Your lesion is saved as sub-01_lesion.nii.gz. The session (scene) is not saved. Quit?"), summary)

        # One more stroke
        array[2:4, 2:4, 2:4] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, sourceVolumeNode)
        mode, summary = self._closeProperties()
        self.assertEqual(mode, firstrun.CLOSE_MODE_UNSAVED_LESION)
        self.assertTrue(summary.startswith("sub-01_lesion has changes since it was saved"), summary)

        # Save lesion, then Cancel in the file dialog: the application stays open
        def cancelDialog():
            dialog = slicer.app.activeModalWidget()
            if dialog is not None:
                dialog.reject()
            else:
                qt.QTimer.singleShot(50, cancelDialog)

        qt.QTimer.singleShot(100, cancelDialog)
        self.mainWindow.setProperty(firstrun.SAVE_RESULT_PROPERTY, "")
        self.mainWindow.saveRequested()
        self._pump()
        self.assertEqual(self.mainWindow.property(firstrun.SAVE_RESULT_PROPERTY), "cancelled")
        self.assertEqual(draw.lesionSaveState(segmentationNode)[0], "unsaved")

        # Other unsaved data (a modified scan): the classic prompt, which saves everything
        other = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "Other")
        slicer.util.updateVolumeFromArray(other, np.zeros((2, 2, 2), dtype=np.int16))
        self.assertEqual(self._closeProperties(), ("", ""))
        slicer.mrmlScene.RemoveNode(other)

        # Classic: no close hooks
        studioShell.classic(persist=False)
        self._pump()
        self.assertEqual(self._closeProperties(), ("", ""))

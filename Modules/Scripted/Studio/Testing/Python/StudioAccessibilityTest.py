r"""Accessibility, first-run and close tests of Slicer Studio (U-03, USABILITY 8 rule 10, 9 and 12).

Run with the main window and testing enabled:

  Slicer --no-splash --testing --python-code "import slicer.testing; slicer.testing.runUnitTest(['<this dir>'], 'StudioAccessibilityTest'); slicer.app.exit(0)"

Studio never runs by itself under --testing; the tests apply it with ``Shell(force=True)`` and always restore the
Classic interface. They cover:

- the AltGr guard (Windows only): a focused text input keeps a printable Ctrl+Alt character ('@'), a view keeps the
  workspace keys; elsewhere a text input leaves Ctrl+Alt+digit to the workspace keys;
- visible keyboard focus: the focus ring of the buttons (>= 3:1 against the unfocused state) and the focus frame of
  the slice views (Tab or F6 shows it, a mouse press hides it);
- Studio fonts that follow the application font live, with the panel title larger than the body text;
- the narrow window mode (no top bar control beyond the window edge at 960 px);
- the research-use line of the Library footer and its "Don't show again" (the official key, written once);
- the close prompt modes (saved lesion and template only, unsaved lesion edits, Save lesion then Cancel).
"""

import itertools
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

    def _waitFor(self, condition, ms):
        import time

        end = time.perf_counter() + ms / 1000.0
        while time.perf_counter() < end:
            slicer.app.processEvents()
            if condition():
                return True
            time.sleep(0.005)
        return condition()

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
        """A focused line edit keeps AltGr+2 ('@', Ctrl+Alt on Windows); in a view Ctrl+Alt+2 still switches workspace.
        Where AltGr is not Ctrl+Alt (Linux, macOS) there is no guard: Ctrl+Alt+1 (text '1' on X11) stays a workspace key.
        """
        from StudioLib import a11y

        studioShell = self._applyStudio()
        self.assertIsNotNone(a11y.instance())
        studioShell.switchTo("library")
        self._pump()
        lineEdit = qt.QLineEdit(self.mainWindow)
        lineEdit.setObjectName("StudioAccessibilityTestLineEdit")
        lineEdit.setGeometry(10, 60, 200, 24)
        lineEdit.show()
        platformGuard = a11y.ALTGR_IS_CTRL_ALT
        # The guard as on Windows (read when the focus changes)
        a11y.ALTGR_IS_CTRL_ALT = True
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

            # Linux and macOS: no guard, a real Ctrl+Alt+1 in a text input (text '1' on X11) switches the workspace
            a11y.ALTGR_IS_CTRL_ALT = False
            studioShell.switchTo("draw")
            self._pump()
            lineEdit.show()
            lineEdit.raise_()
            redWidget.setFocus(qt.Qt.OtherFocusReason)
            self._pump()
            if not self._focus(lineEdit):
                self.skipTest("the window cannot take the keyboard focus here")
            self.assertIsNone(a11y.instance()._guarded)
            override = self._keyEvent(qt.QEvent.ShortcutOverride, qt.Qt.Key_1, ctrlAlt, "1")
            qt.QApplication.sendEvent(lineEdit, override)
            self.assertFalse(override.isAccepted(), "Ctrl+Alt+1 must stay a workspace key")
            shortcut.activated()
            self._pump()
            self.assertEqual(studioShell.currentWorkspace(), "library")
            self.assertEqual(lineEdit.text, "@")
        finally:
            a11y.ALTGR_IS_CTRL_ALT = platformGuard
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

        # F6 into the view under a resting pointer (Shell._focusView picks the last hovered view): the pointer has not
        # moved since the focus left for the inspector, so the focus did not follow the mouse
        qt.QCursor.setPos(redWidget.mapToGlobal(qt.QPoint(redWidget.width // 2, redWidget.height // 2)))
        self._pump()
        helpButton = self.mainWindow.findChild("QToolButton", "StudioPanelHelpButton")
        self.assertIsNotNone(helpButton)
        self._focus(helpButton, qt.Qt.ShortcutFocusReason)
        self.assertFalse(any(viewFrame.isShown() for viewFrame in plugin.viewFrames.values()))
        self.assertTrue(self._focus(redWidget, qt.Qt.ShortcutFocusReason))
        self.assertTrue(frame.isVisible())
        # From a text input with the pointer moved over the view since (focus follows the mouse never leaves a text
        # input): a keyboard focus change too
        lineEdit = qt.QLineEdit(self.mainWindow)
        lineEdit.setObjectName("StudioAccessibilityTestLineEdit")
        lineEdit.setGeometry(10, 60, 200, 24)
        lineEdit.show()
        try:
            qt.QCursor.setPos(studioShell.topBar.toolBar.mapToGlobal(qt.QPoint(5, 5)))
            self._focus(lineEdit)
            self.assertFalse(frame.isVisible())
            qt.QCursor.setPos(redWidget.mapToGlobal(qt.QPoint(redWidget.width // 3, redWidget.height // 3)))
            self._pump()
            self.assertTrue(lineEdit.hasFocus())
            self.assertTrue(self._focus(redWidget, qt.Qt.ShortcutFocusReason))
            self.assertTrue(frame.isVisible())
        finally:
            lineEdit.hide()
            lineEdit.setParent(None)
            lineEdit.deleteLater()

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
        """With a 14 pt application font the Studio labels grow without a restart; the panel title stays larger than the body.

        Library labels with a font set directly (drop zone title, links), buttons that inherit the font under a Studio
        stylesheet (Open…, the workspace switcher) and the panel title all grow, and get their size back afterwards.
        """
        from StudioLib import ui

        studioShell = self._applyStudio()
        studioShell.switchTo("library")
        self._pump(8)

        def pixelSizes(names):
            sizes = {}
            for name in names:
                widget = self.mainWindow.findChild("QWidget", name)
                self.assertIsNotNone(widget, name)
                sizes[name] = qt.QFontInfo(widget.font).pixelSize()
            return sizes

        libraryNames = ("StudioDropZoneTitle", "StudioLibraryClassicLink", "StudioLibraryOpenButton", "StudioLibraryWorkspaceButton")
        libraryBefore = pixelSizes(libraryNames)
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
            studioShell.switchTo("library")
            self._pump(8)
            libraryAfter = pixelSizes(libraryNames)
            for name in libraryNames:
                self.assertGreater(libraryAfter[name], libraryBefore[name], name)
            studioShell.switchTo("draw")
            self._pump(8)
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
        studioShell.switchTo("library")
        self._pump(8)
        self.assertEqual(pixelSizes(libraryNames), libraryBefore)

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

    def test_icon_only_buttons_have_accessible_names(self):
        """A button without text is announced by its accessible name (the tooltip is only the description): the
        top bar, panel header and every visible icon-only button of the view bars have one.
        """
        studioShell = self._applyStudio()
        studioShell.switchTo("draw")
        self.layoutManager.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
        self._wait(200)
        expected = {
            "StudioAppMenuButton": "Menu",
            "StudioLayoutButton": "Layout",
            "StudioSearchPill": "Search",
            "StudioUndoButton": "Undo",
            "StudioRedoButton": "Redo",
            "StudioPanelHelpButton": "Help and acknowledgment",
            "StudioPanelMoreButton": "More",
        }
        for objectName, name in expected.items():
            button = self.mainWindow.findChild("QAbstractButton", objectName)
            self.assertIsNotNone(button, objectName)
            self.assertEqual(str(button.accessibleName), name, objectName)
            # The name never carries the key or the description of the tooltip
            self.assertNotIn(" \u00b7 ", str(button.accessibleName), objectName)

        # Every visible button without text, whatever the view: a name that is not the whole tooltip
        unnamed = []
        count = 0
        for view in ("Red", "Green", "Yellow"):
            bar = self.layoutManager.sliceWidget(view).sliceController()
            for objectName in ("MaximizeViewButton", "OptionsButton", "BarLinkButton"):
                for button in bar.findChildren("QToolButton", objectName):
                    count += 1
                    if not str(button.accessibleName).strip():
                        unnamed.append(f"{view} {objectName}")
        self.assertGreater(count, 0)
        self.assertEqual(unnamed, [])
        threeDBar = self.layoutManager.threeDWidget(0).threeDController()
        for objectName in ("MaximizeViewButton", "OptionsButton"):
            for button in threeDBar.findChildren("QToolButton", objectName):
                self.assertTrue(str(button.accessibleName).strip(), f"3D {objectName}")
        # The maximize button names the action, without the key or the description of its tooltip
        maximize = self.layoutManager.sliceWidget("Red").sliceController().findChild("QToolButton", "MaximizeViewButton")
        self.assertIn(str(maximize.accessibleName), ("Maximize view", "Maximize", "Restore view layout", "Restore layout"))

    def test_settings_icon_only_buttons_have_accessible_names(self):
        """The Settings buttons without text are named too (BB-1): the accent color swatch of Appearance announces
        "Accent color" and its value, and the "..." of every template row names its template.
        """
        from StudioLib import templates, templates_ui

        self._applyStudio()
        dialog = slicer.app.settingsDialog()
        dialog.setCurrentPanel("Appearance")
        dialog.show()
        self._wait(200)
        try:
            swatch = dialog.findChild("QWidget", "AccentColorPickerButton")
            self.assertIsNotNone(swatch)
            self.assertEqual(str(swatch.accessibleName), "Accent color")
            self.assertTrue(str(swatch.accessibleDescription).strip(), "the current value of the swatch")
        finally:
            dialog.hide()
            self._wait(50)

        panel = templates_ui.TemplatesSettingsPanel()
        try:
            panel.refresh()
            self.assertGreater(len(panel.rows), 0)
            seenNames = set()
            for template in templates.listTemplates():
                row = panel.rowFor(template.id)
                button = row.findChild("QToolButton", "studioTemplateMoreButton")
                name = str(button.accessibleName)
                self.assertIn(template.name, name)
                self.assertNotIn(" \u00b7 ", name)
                self.assertTrue(str(button.toolTip).strip())
                seenNames.add(name)
            # Every row has its own name: a screen reader user can tell the buttons apart
            self.assertEqual(len(seenNames), len(panel.rows))
        finally:
            panel.deleteLater()

    def test_no_invisible_focus_stops(self):
        """No Tab or F6 stop draws nothing (USABILITY 9.1): the scroll areas of the inspector and of the Library canvas
        take no focus (their controls do), F6 to the inspector lands on a control, and the lesion readout line is a
        stop only while its text has a link. Classic gets the focus policy of the inspector scroll area back.
        """
        from StudioLib import lesioncard

        tab = int(qt.Qt.TabFocus)
        dock = self.mainWindow.findChild("QDockWidget", "PanelDockWidget")
        panelScrollArea = dock.findChild("QScrollArea", "ScrollArea")
        classicPolicy = panelScrollArea.focusPolicy
        self.assertTrue(int(classicPolicy) & tab, "the Classic inspector scroll area keeps its focus policy")

        studioShell = self._applyStudio()
        studioShell.switchTo("draw")
        self._pump(8)
        self.assertEqual(int(panelScrollArea.focusPolicy), int(qt.Qt.NoFocus))
        # F6 to the inspector: the focus lands on a widget that is no scroll area
        self.mainWindow.activateWindow()
        for _step in range(5):
            studioShell.cycleFocusRegion(1)
            if studioShell.focusRegion() == "inspector":
                break
        self._pump()
        if studioShell.focusRegion() != "inspector":
            self.skipTest("the main window cannot take the keyboard focus here")
        focused = slicer.app.focusWidget()
        self.assertIsNotNone(focused)
        self.assertFalse(focused.inherits("QAbstractScrollArea"), f"F6 stopped on {focused.className()} {focused.objectName}")
        # Tab never stops on it
        stops = [widget for widget in dock.findChildren("QWidget") if widget.isVisible() and int(widget.focusPolicy) & tab]
        self.assertNotIn(panelScrollArea, stops)

        studioShell.switchTo("library")
        self._pump(8)
        libraryScroll = studioShell.canvas().findChild("QScrollArea", "StudioLibraryScroll")
        self.assertIsNotNone(libraryScroll)
        self.assertEqual(int(libraryScroll.focusPolicy), int(qt.Qt.NoFocus))

        # The readout line of the Lesion card: a keyboard stop only with a link to follow
        card = lesioncard.LesionCard(lesioncard._NullController())
        try:
            readout = card.readoutLabel
            card._setReadout("Empty lesion", None)
            self.assertEqual(int(readout.textInteractionFlags), int(qt.Qt.NoTextInteraction))
            self.assertFalse(int(readout.focusPolicy) & tab, "a label without a link is no Tab stop")
            card._setReadout('2 parts <a href="keep">Keep largest</a>', None)
            self.assertTrue(int(readout.textInteractionFlags) & int(qt.Qt.LinksAccessibleByKeyboard))
            self.assertTrue(int(readout.focusPolicy) & tab, "a link is reachable with Tab")
            card._setReadout("Left \u00b7 1.00 mL", None)
            self.assertFalse(int(readout.focusPolicy) & tab)
        finally:
            card.widget.deleteLater()

        studioShell.dispose()
        self._pump(8)
        self.assertEqual(int(panelScrollArea.focusPolicy), int(classicPolicy), "Classic gets the focus policy back")

    def test_f6_to_a_scrolled_inspector_shows_the_focus(self):
        """F6 into an inspector that is scrolled down focuses its first stop and scrolls it into view, as Tab does
        (SPEC 2.11: the focus ring is visible), instead of leaving the focused control above the viewport.
        """
        import numpy as np

        slicer.util.addVolumeFromArray(np.arange(8 * 9 * 10, dtype=np.uint8).reshape(8, 9, 10), name="Scan")
        dock = self.mainWindow.findChild("QDockWidget", "PanelDockWidget")
        studioShell = self._applyStudio()
        studioShell.switchTo("draw")
        self.mainWindow.resize(1366, 520)
        self._pump(8)
        scrollArea = dock.findChild("QScrollArea", "ScrollArea")
        bar = scrollArea.verticalScrollBar()
        for height in (520, 440, 380, 320):
            self.mainWindow.resize(1366, height)
            self._pump(8)
            if bar.maximum > 0:
                break
        if bar.maximum <= 0:
            self.skipTest("the Draw inspector does not scroll in this window")
        # The keyboard user went down the inspector (it scrolled), then left it and comes back with F6
        bar.setValue(bar.maximum)
        self._pump()
        self.assertGreater(bar.value, 0)
        self.mainWindow.activateWindow()
        studioShell._focusInspector()
        self._pump()
        # The focus widget of the window (the application's one is empty while the window is not active)
        focused = self.mainWindow.focusWidget()
        self.assertIsNotNone(focused)
        if not scrollArea.isAncestorOf(focused):
            self.skipTest("F6 focuses the panel header, which is outside the scroll area")
        viewport = scrollArea.viewport()
        rect = qt.QRect(focused.mapTo(viewport, qt.QPoint(0, 0)), focused.size)
        self.assertTrue(viewport.rect.contains(rect),
                        f"F6 focused {focused.className()} {focused.objectName} out of view (scroll {bar.value})")

    def _tabStops(self, root):
        """The widgets under ``root`` that Tab stops on now, in the order of the keyboard focus chain (starting at the
        stop that is highest on the screen, then left).
        """
        tab = int(qt.Qt.TabFocus)
        first = None
        stops = []
        widget = root.nextInFocusChain()
        while widget is not None and widget != root:
            if widget.isVisible() and widget.isEnabled() and int(widget.focusPolicy) & tab and root.isAncestorOf(widget):
                stops.append(widget)
            widget = widget.nextInFocusChain()
        self.assertGreater(len(stops), 3, "the widget has Tab stops")

        def position(widget):
            point = widget.mapTo(root, qt.QPoint(0, 0))
            return point.y(), point.x()

        # The first stop: the leftmost of the stops on the top row (within the few pixels of the rows' rounding)
        highest = min(position(widget)[0] for widget in stops)
        top = min((index for index in range(len(stops)) if position(stops[index])[0] <= highest + 12), key=lambda index: position(stops[index])[1])
        return stops[top:] + stops[:top]

    def test_tab_order_with_node_selector(self):
        """A hidden node selector (qMRMLNodeComboBox: its combo box hands its focus to the selector, which takes no Tab
        focus) between two widgets does not break the Tab order that ui.arrangeTabOrder() makes (Qt ignores
        setTabOrder() for a widget without a focus policy), and the focus proxy is put back.
        """
        from StudioLib import ui

        window = qt.QWidget()
        window.setObjectName("StudioTabOrderTestWindow")
        layout = qt.QVBoxLayout(window)
        # Created in the opposite order of the layout
        last = qt.QPushButton("Last", window)
        middle = qt.QPushButton("Middle", window)
        selector = slicer.qMRMLNodeComboBox(window)
        selector.nodeTypes = ["vtkMRMLScalarVolumeNode"]
        first = qt.QPushButton("First", window)
        for widget in (first, selector, middle, last):
            layout.addWidget(widget)
        selector.hide()
        window.show()
        try:
            self._pump(5)
            combo = [child for child in selector.findChildren("QWidget") if child.focusProxy() is not None and child.focusProxy() == selector]
            self.assertTrue(combo, "the combo box of the selector has the selector as its focus proxy")
            combo = combo[0]
            ui.arrangeTabOrder(window)
            stops = []
            widget = first
            for _ in range(60):
                if widget.window() == window and int(widget.focusPolicy) & int(qt.Qt.TabFocus):
                    stops.append(widget)
                widget = widget.nextInFocusChain()
                if widget == first:
                    break
            order = [widget for widget in stops if widget in (first, combo, middle, last)]
            self.assertEqual([widget.className() if widget == combo else widget.text for widget in order],
                             ["First", combo.className(), "Middle", "Last"], "the selector sits between First and Middle")
            self.assertTrue(combo.focusProxy() == selector, "the focus proxy of the combo box is back")
        finally:
            window.deleteLater()

    def _assertReadingOrder(self, stops, direction, what):
        """Each stop is not above (direction "down") or not left of (direction "right") the one before it, where the two
        are not on the same row (or column), within the rounding of a few pixels.
        """
        for before, after in itertools.pairwise(stops):
            first = before.mapToGlobal(qt.QPoint(0, 0))
            second = after.mapToGlobal(qt.QPoint(0, 0))
            if direction == "down":
                ok = second.y() + 12 >= first.y() and (abs(second.y() - first.y()) > 12 or second.x() >= first.x())
            else:
                ok = second.x() + 2 >= first.x()
            self.assertTrue(ok, f"{what}: Tab goes from {before.objectName or before.className()} ({first.x()}, {first.y()}) "
                                f"to {after.objectName or after.className()} ({second.x()}, {second.y()})")

    def test_tab_order_follows_reading_order(self):
        """Tab follows the reading order: in the Draw inspector top to bottom (the card of "Trace from a figure" above
        Notes, "Normalize to MNI" above Notes), in each slice view bar left to right (orientation, slider, link, Fit,
        Options, Maximize), where the keyboard focus chain follows the creation order of the widgets.
        """
        import numpy as np

        slicer.util.addVolumeFromArray(np.arange(8 * 9 * 10, dtype=np.uint8).reshape(8, 9, 10), name="Scan")
        studioShell = self._applyStudio()
        studioShell.switchTo("draw")
        from StudioLib import draw

        # The cards are added over the first moments (the figure card, the case strip) and the order follows each one
        self._waitFor(lambda: not draw.extension()._tabOrderScheduled and self.mainWindow.findChild("QWidget", "StudioNormalizeDrawRow") is not None
                      and self.mainWindow.findChild("QWidget", "StudioNormalizeDrawRow").isVisible(), 5000)
        # The row of "Trace from a figure" is hidden until a figure is used: its place in the order does not depend on that
        figureSlot = self.mainWindow.findChild("QWidget", "StudioFigureSlot")
        self.assertIsNotNone(figureSlot)
        figureSlot.setVisible(True)
        self._wait(100)
        dock = self.mainWindow.findChild("QDockWidget", "PanelDockWidget")
        contents = dock.widget()
        stops = self._tabStops(contents)
        names = [widget.objectName for widget in stops]
        for name in ("StudioDisplayOpacitySlider", "StudioFigureRow", "StudioNormalizeDrawRow", "StudioDrawNotesRow"):
            self.assertIn(name, names, f"{name} is a Tab stop of the Draw inspector")
        self.assertLess(names.index("StudioDisplayOpacitySlider"), names.index("StudioNormalizeDrawRow"))
        self.assertLess(names.index("StudioNormalizeDrawRow"), names.index("StudioFigureRow"))
        self.assertLess(names.index("StudioFigureRow"), names.index("StudioDrawNotesRow"))
        if "StudioSaveLesionButton" in names:
            self.assertLess(names.index("StudioDrawNotesRow"), names.index("StudioSaveLesionButton"), "the footer comes last")
        self._assertReadingOrder(stops, "down", "Draw inspector")

        # Tab goes on from "More tools" to the next control below it in the inspector (the tool tiles, or the Display card
        # where no tile is shown), not up to the panel header and out to the top bar (the hidden node selectors of the
        # segment editor sit between the two in the layout)
        more = self.mainWindow.findChild("QWidget", "SegmentEditorMoreToolsButton")
        self.assertIsNotNone(more)
        following = more.nextInFocusChain()
        while following is not None and not (following.isVisible() and following.isEnabled() and int(following.focusPolicy) & int(qt.Qt.TabFocus)):
            following = following.nextInFocusChain()
        self.assertIsNotNone(following)
        self.assertTrue(dock.isAncestorOf(following) and following.mapToGlobal(qt.QPoint(0, 0)).y() > more.mapToGlobal(qt.QPoint(0, 0)).y(),
                        f"Tab goes from More tools to {following.objectName or following.className()}, not on down the inspector")

        # A card that a module adds later takes its place in the order (above the Display card here)
        late = qt.QPushButton("Late card")
        late.setObjectName("StudioLateCardButton")
        draw.addInspectorCard("late-card-test", late, 10)
        try:
            self._waitFor(lambda: not draw.extension()._tabOrderScheduled, 2000)
            names = [widget.objectName for widget in self._tabStops(contents)]
            self.assertIn("StudioLateCardButton", names)
            self.assertLess(names.index("StudioLateCardButton"), names.index("StudioNormalizeDrawRow"))
            self.assertLess(names.index("StudioLateCardButton"), names.index("StudioDisplayOpacitySlider"))
            self.assertLess(names.index("StudioLateCardButton"), names.index("SegmentEditorMoreToolsButton"), "before the tools")
            for name in ("StudioLesionMenuButton", "StudioFollowStrokesButton"):
                if name in names:
                    self.assertLess(names.index(name), names.index("StudioLateCardButton"), f"after the Lesion card ({name})")
        finally:
            draw.extension().removeInspectorCard("late-card-test")

        # The slice view bars (flat in Studio)
        layoutManager = slicer.app.layoutManager()
        controllers = [layoutManager.sliceWidget(name).sliceController() for name in layoutManager.sliceViewNames()]
        flatBefore = [controller.flatStyle for controller in controllers]
        try:
            for controller in controllers:
                controller.setProperty("flatStyle", False)
            self._pump(8)
            classicOrders = []
            for controller in controllers:
                bar = controller.findChild("QWidget", "BarWidget")
                classicOrders.append(self._chainStops(bar, shownOnly=False))
            for controller in controllers:
                controller.setProperty("flatStyle", True)
            self._wait(100)
            checked = 0
            for controller in controllers:
                bar = controller.findChild("QWidget", "BarWidget")
                if not bar.isVisible():
                    # The layout of the workspace shows some of the views
                    continue
                checked += 1
                barStops = self._chainStops(bar)
                barNames = [widget.objectName or widget.className() for widget in barStops]
                self.assertEqual(barNames[0], "OrientationButton", barNames)
                self.assertEqual(barNames[-1], "MaximizeViewButton", barNames)
                self.assertIn("BarLinkButton", barNames)
                self.assertIn("OptionsButton", barNames)
                # Left to right: the bar is a row
                self._assertReadingOrder(barStops, "right", "flat slice view bar")
            self.assertGreater(checked, 0, "a slice view bar is shown")
            # Back to the classic presentation: its own order again
            for controller in controllers:
                controller.setProperty("flatStyle", False)
            self._pump(8)
            for controller, classicOrder in zip(controllers, classicOrders, strict=True):
                bar = controller.findChild("QWidget", "BarWidget")
                self.assertEqual([widget.objectName or widget.className() for widget in self._chainStops(bar, shownOnly=False)],
                                 [widget.objectName or widget.className() for widget in classicOrder], "the classic bar keeps its Tab order")
        finally:
            for controller, flat in zip(controllers, flatBefore, strict=True):
                controller.setProperty("flatStyle", flat)

        studioShell.dispose()
        self._pump(8)

    def test_library_tab_order_follows_reading_order(self):
        """Tab goes through the Library page as it is shown: the Continue card (built when data is first loaded) comes after
        the header and before the project cards, the links and the recent files, and the inspector follows the page, its
        "Open now" rows (rebuilt with the scene) above the project's buttons. The keyboard focus chain follows the creation
        order of the widgets, which put the Continue card after the Recent rows and the inspector's rows before the cards
        that were built later.
        """
        import shutil
        import tempfile

        from StudioLib import importer, library, projects

        root = tempfile.mkdtemp(prefix="StudioLibraryTabOrder")
        previous = os.environ.get(projects.ROOT_ENVIRONMENT)
        os.environ[projects.ROOT_ENVIRONMENT] = root
        try:
            studioShell = self._applyStudio()
            studioShell.switchTo("library")
            importer.loadFiles([self._scanFile()], studioShell)
            self._pump(8)
            studioShell.switchTo("library")
            self._waitFor(lambda: not getattr(library, "_tabOrderScheduled", False), 3000)
            canvas = library.canvas()
            self.assertTrue(canvas.widget.isVisible())
            self.assertTrue(canvas.currentWork.widget.isVisible(), "the Continue card is shown")
            stops = self._tabStops(canvas.widget)
            names = [widget.objectName for widget in stops]
            for name in ("StudioCurrentWorkContinueButton", "StudioLibraryNewStudyLink", "StudioLibraryClassicLink"):
                self.assertIn(name, names)
            self.assertLess(names.index("StudioLibraryOpenButton"), names.index("StudioCurrentWorkContinueButton"), "Continue after the header")
            self.assertLess(names.index("StudioCurrentWorkDataLink"), names.index("StudioLibraryNewStudyLink"), "the Continue card before the links")
            if "studioProjectCard" in names:
                self.assertLess(names.index("StudioCurrentWorkContinueButton"), names.index("studioProjectCard"), "Continue before the projects")
            if "StudioRecentRow" in names:
                self.assertLess(names.index("StudioLibraryClassicLink"), names.index("StudioRecentRow"))
            self._assertReadingOrder(stops, "down", "Library page")

            # The inspector: after the page, "Open now" top to bottom, then the project's buttons
            dock = self.mainWindow.findChild("QDockWidget", "PanelDockWidget")
            self.assertTrue(dock.isVisible())

            def inspectorStops():
                # The Tab stops of the inspector in the order of the focus chain, from its first stop on the screen
                contents = dock.widget()
                found = []
                widget = contents.nextInFocusChain()
                while widget is not None and widget != contents and len(found) < 2000:
                    if widget.isVisible() and widget.isEnabled() and int(widget.focusPolicy) & int(qt.Qt.TabFocus) and contents.isAncestorOf(widget):
                        found.append(widget)
                    widget = widget.nextInFocusChain()
                top = min(range(len(found)), key=lambda index: (found[index].mapToGlobal(qt.QPoint(0, 0)).y(), found[index].mapToGlobal(qt.QPoint(0, 0)).x()))
                return found[top:] + found[:top]

            inspector = inspectorStops()
            inspectorNames = [widget.objectName for widget in inspector]
            self.assertIn("StudioOpenNowMenuButton", inspectorNames)
            self._assertReadingOrder(inspector, "down", "Library inspector")
            if "StudioProjectOverviewShowButton" in inspectorNames:
                self.assertLess(inspectorNames.index("StudioOpenNowMenuButton"), inspectorNames.index("StudioProjectOverviewShowButton"))
            chain = []
            widget = canvas.widget
            for _index in range(1500):
                widget = widget.nextInFocusChain()
                if widget == canvas.widget:
                    break
                if widget.isVisible() and widget.isEnabled() and int(widget.focusPolicy) & int(qt.Qt.TabFocus):
                    chain.append(widget)
            chainNames = [widget.objectName for widget in chain]
            self.assertLess(chainNames.index("StudioCurrentWorkContinueButton"), chainNames.index("StudioOpenNowMenuButton"),
                            "the page is reached before the inspector")

            # Rows that are built again (a node was renamed) keep their place
            node = slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")[0]
            node.SetName("A scan that was renamed")
            studioShell.switchTo("library")
            self._waitFor(lambda: not getattr(library, "_tabOrderScheduled", False), 3000)
            self._pump(4)
            self._assertReadingOrder(inspectorStops(), "down", "Library inspector after a rename")
            self._assertReadingOrder(self._tabStops(canvas.widget), "down", "Library page after a rename")
        finally:
            if previous is None:
                os.environ.pop(projects.ROOT_ENVIRONMENT, None)
            else:
                os.environ[projects.ROOT_ENVIRONMENT] = previous
            self._pump(4)
            shutil.rmtree(root, ignore_errors=True)

    def _chainStops(self, root, shownOnly=True):
        """The Tab stops inside ``root`` (the visible, enabled ones, or all), in the order of the focus chain from ``root``."""
        tab = int(qt.Qt.TabFocus)
        stops = []
        widget = root.nextInFocusChain()
        while widget is not None and widget != root:
            if ((widget.isVisible() and widget.isEnabled()) or not shownOnly) and int(widget.focusPolicy) & tab and root.isAncestorOf(widget):
                stops.append(widget)
            widget = widget.nextInFocusChain()
        self.assertGreater(len(stops), 2, "the widget has Tab stops")
        return stops

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

    def test_close_summary_study(self):
        """With a study open, closing writes the current subject first (a study has no Save) and asks only "All changes are
        saved in study <name>. Quit?": never the no-study "Save lesion…" / "Don't save" prompt, whose "Don't save" the
        study could not honour (it writes the subject when the application quits).
        """
        import shutil

        import numpy as np
        from LesionNetworkMappingLib import nifti
        from LesionNetworkMappingLib.study import Study

        from StudioLib import firstrun, study_ui

        if not hasattr(self.mainWindow, "closeSummaryRequested"):
            self.skipTest("the main window has no close summary hook (W3-03)")
        studioShell = self._applyStudio()
        scan = self._scanFile()
        folder = os.path.join(os.path.dirname(scan), "Close study")
        study = Study.create(folder, name="Close study")
        study.add_subject("sub-01", scan=study.relpath(scan), status="untraced", group="case")
        study.save()
        try:
            session = study_ui.openStudy(folder, studioShell, subject="sub-01")
            self._pump(8)
            segmentationNode, volumeNode = session.segmentationNode(), session.volumeNode()
            self.assertIsNotNone(segmentationNode)
            segmentId = segmentationNode.GetSegmentation().GetNthSegmentID(0)

            def paint(region):
                array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, volumeNode)
                array[region] = 1
                slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, volumeNode)
                self._pump()

            def voxels():
                path = session.study.abspath(session.study.subject("sub-01").get("lesion"))
                if not path or not os.path.isfile(path):
                    return 0
                return int((nifti.read_nifti(path)[0] > 0).sum())

            paint((slice(8, 10), slice(10, 12), slice(6, 8)))
            self.assertTrue(session.isDirty())
            self.assertEqual(voxels(), 0)
            # The subject is written when the prompt is described: the question cannot discard it
            mode, summary = self._closeProperties()
            self.assertEqual(mode, firstrun.CLOSE_MODE_LESION_SAVED)
            self.assertEqual(summary, "All changes are saved in study Close study. Quit?")
            self.assertEqual(voxels(), 8)
            self.assertFalse(session.isDirty())
            self.assertEqual(session.current(), "sub-01", "Cancel keeps the subject open")
            # More edits after Cancel are written the same way
            paint((slice(2, 4), slice(2, 4), slice(2, 4)))
            self.assertEqual(self._closeProperties()[0], firstrun.CLOSE_MODE_LESION_SAVED)
            self.assertEqual(voxels(), 16)
            # Other unsaved data (not part of the study): the classic prompt, which saves everything
            other = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "Other")
            slicer.util.updateVolumeFromArray(other, np.zeros((2, 2, 2), dtype=np.int16))
            self.assertEqual(self._closeProperties(), ("", ""))
            slicer.mrmlScene.RemoveNode(other)
        finally:
            study_ui.closeStudy()
            shutil.rmtree(os.path.dirname(scan), ignore_errors=True)

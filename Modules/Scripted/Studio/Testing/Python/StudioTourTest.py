"""Tests of the guided tours of Slicer Studio and the 'Where did it go?' map (T-01).

Run with the main window and testing enabled:

  Slicer --no-splash --testing --python-code "import slicer.testing; slicer.testing.runUnitTest(['<this dir>'], 'StudioTourTest'); slicer.app.exit(0)"

Studio never runs by itself under --testing; the tests apply it with ``Shell(force=True)`` and always restore the
Classic interface. They play every tour at 1920x1080 and 1366x768 in the dark and the light theme and check that
every step spotlights a visible, uncovered control and that the card is on the window without covering it; the
keys (Right, Left, Tab, Esc); that leaving a tour restores the workspace, the module, the layout and the inspector
and leaves the scene and the settings (except ``Studio/Tour/*``) as they were; the first-start offer; the menus and
the help link; the 'Where did it go?' panel and its "Show me"; that every map row points at a control of this build
or is marked as planned; and that the docs page shows the map. With ``STUDIO_TOUR_SHOTS=<folder>`` a screenshot of
every step is saved there.
"""

import os
import re

import numpy as np
import qt

import slicer
from slicer import ScriptedLoadableModule

SIZES = ((1920, 1080), (1366, 768))
THEMES = ("dark", "light")
STYLE_NAMES = {"dark": "Dark Slicer", "light": "Light Slicer"}
#: Places of official 3D Slicer that the map must cover (T-01 step 5), by row id
REQUIRED_ROWS = ("modules", "add-data", "save-data", "add-dicom", "layout", "settings", "python-console", "extensions", "data-module",
                 "volumes-module", "segment-editor", "segmentations-module", "markups", "mouse-mode", "view-controllers", "data-probe",
                 "toolbars", "shortcuts-reference", "about")
#: Docs page with the map, relative to the source tree
DOCS_PAGE = os.path.join("Docs", "user_guide", "studio_coming_from_slicer.md")
DOCS_TABLE = re.compile(r"<!-- BEGIN where-did-it-go[^>]*-->\n(.*?)<!-- END where-did-it-go -->", re.DOTALL)


def sourceDirectory():
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", ".."))


class StudioTourTest(ScriptedLoadableModule.ScriptedLoadableModuleTest):
    """Guided tours and the 'Where did it go?' map. They need the main window."""

    def setUp(self):
        slicer.mrmlScene.Clear(0)
        self.mainWindow = slicer.util.mainWindow()
        self.layoutManager = slicer.app.layoutManager()
        self.initialLayout = self.layoutManager.layout if self.layoutManager is not None else None
        self.initialGeometry = self.mainWindow.saveGeometry() if self.mainWindow is not None else None
        self.shotsFolder = os.environ.get("STUDIO_TOUR_SHOTS")

    def tearDown(self):
        from StudioLib import shell

        live = shell._shell
        if live is not None:
            live.dispose()
        self._applyTheme(None)
        if self.layoutManager is not None and self.initialLayout is not None and self.layoutManager.layout != self.initialLayout:
            self.layoutManager.setLayout(self.initialLayout)
        if self.initialGeometry is not None:
            self.mainWindow.restoreGeometry(self.initialGeometry)
        qt.QSettings().remove("Studio/Tour")
        slicer.app.processEvents()
        slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)

    def runTest(self):
        for name in sorted(name for name in dir(self) if name.startswith("test_")):
            self.setUp()
            try:
                getattr(self, name)()
            finally:
                self.tearDown()

    #
    # Helpers
    #

    def _pump(self, count=4):
        for index in range(count):
            slicer.app.processEvents()

    def _wait(self, ms):
        loop = qt.QEventLoop()
        qt.QTimer.singleShot(ms, loop.quit)
        loop.exec_()

    def _applyStudio(self):
        from StudioLib import shell

        self.assertIsNotNone(self.mainWindow, "Studio tests need the main window")
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        self._pump()
        # The shell builds the Draw page (and its parameter node) when idle, a moment after it is applied
        self._wait(1000)
        return studioShell

    def _applyTheme(self, theme):
        """Switch the style and palette at runtime (no setting is written); None restores the testing style."""
        name = STYLE_NAMES[theme] if theme else slicer.util.settingsValue("Styles/Style", "Slicer")
        style = qt.QStyleFactory.create(name)
        if style is None:
            return
        application = slicer.app
        application.removeEventFilter(application.style())
        application.setStyle(style)
        application.installEventFilter(application.style())
        application.setPalette(style.standardPalette())
        self._pump()

    def _resize(self, width, height):
        self.mainWindow.showNormal()
        self.mainWindow.resize(width, height)
        self._pump(8)

    def _sendKey(self, target, key, modifiers=qt.Qt.NoModifier):
        """A key press as Qt delivers it: the shortcut override, then the key press."""
        override = qt.QKeyEvent(qt.QEvent.ShortcutOverride, key, modifiers)
        override.ignore()
        qt.QApplication.sendEvent(target, override)
        qt.QApplication.sendEvent(target, qt.QKeyEvent(qt.QEvent.KeyPress, key, modifiers))
        self._pump()

    def _nodeIds(self):
        return sorted(slicer.mrmlScene.GetNthNode(index).GetID() for index in range(slicer.mrmlScene.GetNumberOfNodes()))

    def _settingsSnapshot(self):
        settings = qt.QSettings()
        return {key: str(settings.value(key)) for key in settings.allKeys()}

    def _viewState(self, studioShell):
        return {
            "workspace": studioShell.currentWorkspace(),
            "transient": studioShell.workspaces.transientModule,
            "origin": studioShell.transientOrigin(),
            "layout": self.layoutManager.layout,
            "inspector": studioShell.isInspectorVisible(),
            "hiddenByUser": studioShell._inspectorHiddenByUser,
        }

    def _loadVolume(self):
        volume = slicer.util.addVolumeFromArray(np.zeros((20, 48, 48), dtype=np.int16), name="patient_T1")
        slicer.util.setSliceViewerLayers(background=volume, fit=True)
        self._pump()
        return volume

    def _isInside(self, widget, ancestor):
        while widget is not None:
            if widget == ancestor:
                return True
            widget = widget.parentWidget()
        return False

    def _checkStep(self, player, label):
        """The control is visible, not covered and inside the spotlight; the card is on the window and does not cover it."""
        from StudioLib import tour

        mainWindow = self.mainWindow
        # The spotlight follows the control within one follow interval (the inspector may still be resizing)
        self._wait(2 * tour.FOLLOW_INTERVAL_MS)
        step = player.currentStep()
        target = player.target
        where = f"{label} step {step.id}"
        self.assertIsNotNone(target, where)
        self.assertTrue(target.isVisible(), where)
        targetRect = tour._rectIn(target, mainWindow).intersected(mainWindow.rect)
        self.assertFalse(targetRect.isEmpty(), where)
        spot = player.spotlightRect()
        self.assertIsNotNone(spot, where)
        tolerance = tour.RING_WIDTH + 1
        self.assertTrue(spot.adjusted(-tolerance, -tolerance, tolerance, tolerance).contains(targetRect), f"{where}: spotlight {spot} does not contain {targetRect}")
        card = player.cardRect()
        self.assertTrue(mainWindow.rect.contains(card), f"{where}: card {card} is not inside the window {mainWindow.rect}")
        self.assertFalse(card.intersects(spot), f"{where}: card {card} covers the spotlight {spot}")
        # Not covered by another widget: what is under the center of the control, without the tour
        overlay, cardFrame = player.overlay(), player.card().frame
        overlay.hide()
        cardFrame.hide()
        try:
            center = targetRect.center()
            hit = mainWindow.childAt(center)
            self.assertTrue(hit is not None and self._isInside(hit, target), f"{where}: the control is covered by {hit.objectName if hit else None}")
        finally:
            overlay.show()
            cardFrame.show()
            overlay.raise_()
            cardFrame.raise_()
        # The dimmed area takes the clicks (the window corner opposite to the spotlight is dimmed)
        corner = qt.QPoint(5, mainWindow.height - 5) if spot.center().x() > mainWindow.width // 2 else qt.QPoint(mainWindow.width - 5, mainWindow.height - 5)
        if not spot.contains(corner) and not card.contains(corner):
            self.assertEqual(mainWindow.childAt(corner), overlay, f"{where}: a click on the dimmed area reaches {mainWindow.childAt(corner)}")
        # The card says how it is done in official 3D Slicer
        self.assertTrue(player.card().slicerLabel.text, f"{where}: no 'In 3D Slicer' text")
        self.assertTrue(player.card().slicerBox.isVisible(), where)
        if self.shotsFolder:
            os.makedirs(self.shotsFolder, exist_ok=True)
            mainWindow.grab().save(os.path.join(self.shotsFolder, f"{label}-{player.index + 1:02d}-{step.id}.png"))

    def _playTour(self, tourId, label):
        """Play a tour to the end with Next. Return the ids of the steps shown."""
        from StudioLib import tour

        self.assertTrue(tour.startTour(tourId), f"{label}: {tourId} did not start")
        player = tour.activePlayer()
        self.assertIsNotNone(player)
        shown = []
        for index in range(40):
            if not player.active:
                break
            self._pump()
            self._checkStep(player, label)
            shown.append(player.currentStep().id)
            player.next()
        self.assertFalse(player.active, f"{label}: {tourId} did not end")
        self.assertTrue(player.completed)
        self._pump()
        return shown

    #
    # Tests
    #

    def test_01_nothing_under_testing(self):
        """Without the Studio shell (testing, Classic) there is no tour, offer, menu item or overlay."""
        from StudioLib import tour

        self.assertTrue(slicer.app.testingEnabled())
        self.assertIsNone(tour.plugin())
        self.assertFalse(tour.startTour("studio"))
        self.assertFalse(tour.showMigrationMap())
        self.assertFalse(tour.showMe("add-data"))
        self.assertFalse(tour.offerTour())
        self.assertEqual(tour.availableTours(), [])
        self.assertIsNone(self.mainWindow.findChild("QWidget", "StudioTourOverlay"))
        helpMenu = self.mainWindow.findChild("QMenu", "HelpMenu")
        self.assertNotIn("StudioTakeTourAction", [action.objectName for action in helpMenu.actions()])

        # Applied under testing (forced): the plug-in is installed but never offers a tour by itself
        studioShell = self._applyStudio()
        plugin = tour.plugin()
        self.assertIsNotNone(plugin)
        self.assertIsNone(plugin._offerTimer)
        self.assertLess(plugin.installDurationMs, 50.0)
        # Back to Classic: everything is removed
        studioShell.classic(persist=False)
        self._pump()
        self.assertIsNone(tour.plugin())
        self.assertFalse(tour.startTour("studio"))
        self.assertNotIn("StudioTakeTourAction", [action.objectName for action in helpMenu.actions()])

    def test_02_content(self):
        """Every step says how it is done in official 3D Slicer; the map covers the required places and is searchable."""
        from StudioLib import migration, tours

        tourIds = [tour.id for tour in tours.TOURS]
        self.assertEqual(len(tourIds), len(set(tourIds)))
        for tour in tours.TOURS:
            self.assertTrue(tour.title and tour.description, tour.id)
            stepIds = [step.id for step in tour.steps]
            self.assertEqual(len(stepIds), len(set(stepIds)), tour.id)
            for step in tour.steps:
                self.assertTrue(step.title, f"{tour.id}/{step.id}")
                self.assertTrue(step.inSlicerText(), f"{tour.id}/{step.id}: no 'In 3D Slicer' text")
                for rowId in step.rows:
                    self.assertIsNotNone(migration.row(rowId), f"{tour.id}/{step.id}: unknown row {rowId}")
                if step.tryIt:
                    self.assertIsNotNone(step.done, f"{tour.id}/{step.id}")
        self.assertGreaterEqual(len(tours.tour("studio").steps), 9)
        mapTargets = {step.target for step in tours.tour("map").steps}
        for objectName in ("LNMSeedCard", "LNMConnectomeCard", "LNMComputeButton", "LNMResultCard", "LNMDisclaimer"):
            self.assertIn(objectName, mapTargets)
        self.assertIn("not part of official 3D Slicer", migration.row("lesion-network-mapping").official)
        self.assertIn("not part of official 3D Slicer", migration.row("connectome").official)
        self.assertEqual(migration.lookup("lesion network mapping")[0].id, "lesion-network-mapping")
        self.assertEqual(migration.lookup("connectome")[0].id, "connectome")
        rowIds = [row.id for row in migration.ROWS]
        self.assertEqual(len(rowIds), len(set(rowIds)))
        for rowId in REQUIRED_ROWS:
            self.assertIn(rowId, rowIds)
        for row in migration.ROWS:
            self.assertTrue(row.official and row.studio, row.id)
            # Every marked key is a valid key sequence
            keys = migration.keysIn(row.studio) + migration.keysIn(row.note or "")
            for key in (row.key, row.officialKey):
                keys += key.split(" / ") if key else []
            for key in keys:
                self.assertEqual(qt.QKeySequence(key).count(), 1, f"{row.id}: {key}")
        # Search
        self.assertEqual(migration.lookup("add data")[0].id, "add-data")
        self.assertEqual(migration.lookup("python")[0].id, "python-console")
        self.assertIn("mouse-mode", [row.id for row in migration.lookup("window level")])
        self.assertIn("export-segmentation", [row.id for row in migration.lookup("export")])
        self.assertEqual(migration.lookup("zzzz"), [])
        self.assertEqual(len(migration.lookup("")), len(migration.ROWS))
        # Keys are shown as this platform shows them
        self.assertEqual(migration.formatKeys("{key:Ctrl+O}"), qt.QKeySequence("Ctrl+O").toString(qt.QKeySequence.NativeText))
        self.assertEqual(migration.formatKeys("{key:Ctrl+O}", native=False), "Ctrl+O")

    def test_03_docs_page(self):
        """The docs page 'Coming from 3D Slicer' shows the map table exactly as migration.markdownTable() makes it."""
        from StudioLib import migration

        path = os.path.join(sourceDirectory(), DOCS_PAGE)
        if not os.path.exists(path):
            # Not run from a source tree
            return
        with open(path, encoding="utf-8") as docsFile:
            text = docsFile.read()
        match = DOCS_TABLE.search(text)
        self.assertIsNotNone(match, "the docs page has no 'where-did-it-go' table markers")
        self.assertEqual(match.group(1).strip(), migration.markdownTable().strip(),
                         "the table of the docs page differs from migration.markdownTable(): regenerate it")
        index = os.path.join(sourceDirectory(), "Docs", "user_guide", "install_studio.md")
        if os.path.exists(index):
            with open(index, encoding="utf-8") as indexFile:
                self.assertIn("studio_coming_from_slicer.md", indexFile.read())

    def test_04_map_targets(self):
        """Every row of the map points at a control of this build ("Show me" spotlights it) or is marked as planned."""
        from StudioLib import migration, tour, tours

        studioShell = self._applyStudio()
        self._resize(1366, 768)
        before = self._nodeIds()
        mapAvailable = tours.tour("map").isAvailable(studioShell)
        for row in migration.ROWS:
            if row.planned or (row.workspace == "map" and not mapAvailable):
                continue
            self.assertTrue(row.target, row.id)
            self.assertTrue(tour.showMe(row.id), f"{row.id}: Show me found no control for {row.target}")
            player = tour.activePlayer()
            self.assertIsNotNone(player, row.id)
            self._checkStep(player, f"map-{row.id}")
            # The card shows the place in 3D Slicer
            self.assertIn(row.official, player.card().slicerLabel.text)
            player.stop()
            self._pump()
            self.assertIsNone(tour.activePlayer())
        self.assertEqual(self._nodeIds(), before)
        self.assertEqual(studioShell.currentWorkspace(), "library")

    def test_05_every_tour_every_size_and_theme(self):
        """Every tour at 1920x1080 and 1366x768, dark and light: aligned spotlight and card, nothing changed afterwards."""
        from StudioLib import tour, tours

        studioShell = self._applyStudio()
        settingsBefore = self._settingsSnapshot()
        expectedSkips = {"autosave"} if self.mainWindow.findChild("QWidget", "StudioSaveStatus") is None else set()
        for width, height in SIZES:
            self._resize(width, height)
            for theme in THEMES:
                self._applyTheme(theme)
                for tourInfo in tour.availableTours():
                    tourId = tourInfo["id"]
                    studioShell.switchTo("library")
                    self._pump()
                    nodesBefore = self._nodeIds()
                    modifiedBefore = slicer.mrmlScene.GetModifiedSinceRead()
                    stateBefore = self._viewState(studioShell)
                    label = f"{width}x{height}-{theme}-{tourId}"
                    shown = self._playTour(tourId, label)
                    self.assertGreater(len(shown), 0, label)
                    if tourId == "studio":
                        allSteps = [step.id for step in tours.tour("studio").steps]
                        self.assertEqual(set(allSteps) - set(shown), expectedSkips, f"{label}: steps skipped")
                    if tourId == "map":
                        # Every Map step points at a control that is shown and not covered (_checkStep): none is skipped
                        self.assertEqual(shown, [step.id for step in tours.tour("map").steps], f"{label}: steps skipped")
                    self.assertEqual(self._viewState(studioShell), stateBefore, label)
                    self.assertEqual(self._nodeIds(), nodesBefore, label)
                    self.assertEqual(slicer.mrmlScene.GetModifiedSinceRead(), modifiedBefore, label)
                    self.assertIn(f"{tourId}@{tours.CONTENT_VERSION}", tour.completedTours())
        # Only Studio/Tour/* keys were added or changed
        settingsAfter = self._settingsSnapshot()
        changed = {key for key in set(settingsBefore) | set(settingsAfter) if settingsBefore.get(key) != settingsAfter.get(key)}
        self.assertEqual({key for key in changed if not key.startswith("Studio/Tour/")}, set())

    def test_06_keys_and_escape_restore(self):
        """Right, Left, Enter, Tab and Esc in the card; Esc restores the workspace, the layout and the inspector."""
        from StudioLib import tour

        studioShell = self._applyStudio()
        self._resize(1366, 768)
        studioShell.switchTo("draw")
        self._pump()
        self.layoutManager.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
        self._pump()
        studioShell.toggleInspector()
        self._pump()
        before = self._viewState(studioShell)
        self.assertFalse(before["inspector"])
        self.assertTrue(before["hiddenByUser"])
        nodesBefore = self._nodeIds()

        self.assertTrue(tour.startTour("studio"))
        player = tour.activePlayer()
        card = player.card()
        self.assertEqual(player.index, 0)
        self.assertFalse(card.backButton.isEnabled())
        # Next has the focus: Right, Enter and Space go on, Left goes back
        self._sendKey(card.nextButton, qt.Qt.Key_Right)
        self.assertEqual(player.index, 1)
        self._sendKey(card.nextButton, qt.Qt.Key_Return)
        self.assertEqual(player.index, 2)
        self._sendKey(card.nextButton, qt.Qt.Key_Left)
        self.assertEqual(player.index, 1)
        self.assertTrue(card.backButton.isEnabled())
        # Tab stays inside the card (when the headless window can have the keyboard focus)
        focusable = [card.skipButton, card.backButton, card.nextButton]
        self.mainWindow.activateWindow()
        card.nextButton.setFocus()
        self._pump()
        if slicer.app.focusWidget() == card.nextButton:
            for index in range(4):
                self._sendKey(slicer.app.focusWidget(), qt.Qt.Key_Tab)
                self.assertIn(slicer.app.focusWidget(), focusable)
        # Esc leaves the tour from any step and puts everything back
        card.nextButton.setFocus()
        self._sendKey(card.nextButton, qt.Qt.Key_Escape)
        self.assertFalse(player.active)
        self.assertFalse(player.completed)
        self.assertIsNone(self.mainWindow.findChild("QWidget", "StudioTourOverlay"))
        self.assertEqual(self._viewState(studioShell), before)
        self.assertEqual(self._nodeIds(), nodesBefore)
        # The current tab still shows the inspector again (hidden by the user)
        studioShell.switchTo("draw")
        self._pump()
        self.assertTrue(studioShell.isInspectorVisible())

        # From a module opened from Draw (transient): back to the module, with its back button to Draw
        slicer.util.selectModule("Data")
        self._pump()
        before = self._viewState(studioShell)
        self.assertEqual(before["transient"], "Data")
        self.assertTrue(tour.startTour("studio"))
        player = tour.activePlayer()
        while player.active and player.currentStep().id != "save-lesion":
            player.next()
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        # Esc through the shell's Esc ladder (the focus is not in the card): the tour is the top rung
        self.mainWindow.setFocus()
        self.assertTrue(studioShell.handleEscape())
        self._pump()
        self.assertFalse(player.active)
        self.assertEqual(self._viewState(studioShell), before)
        self.assertEqual(studioShell.panelHeader.closeButton.text, "Draw")

    def test_07_try_it_and_skip(self):
        """A "Try it" step lets the control be clicked and goes on by itself; Skip tour leaves."""
        from StudioLib import tour

        studioShell = self._applyStudio()
        self._resize(1366, 768)
        self.assertTrue(tour.startTour("studio"))
        player = tour.activePlayer()
        while player.currentStep().id != "workspaces":
            player.next()
        drawButton = studioShell.topBar.workspaceButtons["draw"]
        center = drawButton.mapTo(self.mainWindow, qt.QPoint(drawButton.width // 2, drawButton.height // 2))
        # The spotlight lets the clicks through to the control
        self.assertTrue(self._isInside(self.mainWindow.childAt(center), drawButton))
        drawButton.click()
        self._wait(400)
        self.assertEqual(player.currentStep().id, "draw-tools")
        player.card().skipButton.click()
        self._pump()
        self.assertFalse(player.active)
        self.assertEqual(studioShell.currentWorkspace(), "library")
        # No tour while a modal dialog is open
        dialog = qt.QDialog(self.mainWindow)
        dialog.setModal(True)
        dialog.show()
        self._pump()
        try:
            if slicer.app.activeModalWidget() is not None:
                self.assertFalse(tour.startTour("studio"))
        finally:
            dialog.hide()
            dialog.deleteLater()
        self._pump()

    def test_08_with_data(self):
        """With a scan open, the tours visit Draw and leave the scene, the tool and the modified state as they were."""

        studioShell = self._applyStudio()
        self._resize(1366, 768)
        self._loadVolume()
        studioShell.switchTo("library")
        self._pump()
        nodesBefore = self._nodeIds()
        modifiedBefore = slicer.mrmlScene.GetModifiedSinceRead()
        stateBefore = self._viewState(studioShell)
        for tourId in ("studio", "draw"):
            self._playTour(tourId, f"data-{tourId}")
            self.assertEqual(self._nodeIds(), nodesBefore, tourId)
            self.assertEqual(slicer.mrmlScene.GetModifiedSinceRead(), modifiedBefore, tourId)
            self.assertEqual(self._viewState(studioShell), stateBefore, tourId)

    def test_09b_offer_keeps_clear_of_notices(self):
        """The offer avoids the buttons of the Library notices (an update, a required update), at both window sizes (BB-1)."""
        from StudioLib import library, tour

        studioShell = self._applyStudio()
        settings = qt.QSettings()
        # The last case: a short window and a wide card (a large font), where the bottom right corner covers the right end of
        # the drop zone and the top right corner, where the notice buttons are, is the only place left
        for width, height, cardWidth in SIZES[0] + (None,), SIZES[1] + (None,), (1366, 600, 450):
            self._resize(width, height)
            settings.remove("Studio/Tour")
            studioShell.switchTo("library")
            self._pump()
            self.assertTrue(tour.offerTour(), (width, height))
            self._pump()
            card = tour.plugin().offerWidget()
            self.assertIsNotNone(card)
            if cardWidth is not None:
                offerCard = tour.plugin().offerCard
                offerCard.frame.setFixedWidth(cardWidth)
                offerCard._fitHeight()
                offerCard.updatePosition()
                self._pump()
            # The notice appears after the offer, with its buttons at the right end of the row, like "Update now" and "Skip"
            notice = qt.QFrame()
            notice.setObjectName("StudioTestNotice")
            row = qt.QHBoxLayout(notice)
            row.addWidget(qt.QLabel("A new version is available"))
            row.addStretch(1)
            buttons = []
            for text in ("Update now", "Skip"):
                button = qt.QPushButton(text)
                button.setObjectName("StudioTestNotice" + text.split()[0])
                row.addWidget(button)
                buttons.append(button)
            try:
                library.showNotice("StudioTestNotice", notice)
                self._wait(200)
                self.assertTrue(card.isVisible())
                offerRect = tour._rectIn(card, self.mainWindow)
                for button in buttons:
                    self.assertTrue(button.isVisible(), button.text)
                    buttonRect = tour._rectIn(button, self.mainWindow)
                    self.assertFalse(offerRect.intersects(buttonRect), (width, height, button.text, offerRect, buttonRect))
                    at = qt.QApplication.widgetAt(button.mapToGlobal(qt.QPoint(button.width // 2, button.height // 2)))
                    self.assertTrue(at is not None and at.objectName == button.objectName, (width, height, button.text, at))
            finally:
                library.closeNotice("StudioTestNotice")
                notice.deleteLater()
                self._pump()
            tour.plugin().offerWidget().findChild("QPushButton", "StudioTourOfferNotNowButton").click()
            self._pump()

    def test_09c_offer_keeps_clear_of_toasts(self):
        """A long toast (centred at the bottom, up to the canvas width) does not cover the offer card or its link (BB-1)."""
        from StudioLib import tour

        studioShell = self._applyStudio()
        settings = qt.QSettings()
        text = "Study cohortC created in place: 6 subjects (participants.tsv kept, backup in .studio)"
        # 1229x691 is the window of a 1366x768 screen, where the toast reaches the card's left end
        for width, height in SIZES + ((1229, 691), (1000, 700)):
            self._resize(width, height)
            settings.remove("Studio/Tour")
            studioShell.switchTo("library")
            self._pump()
            self.assertTrue(tour.offerTour(), (width, height))
            self._pump()
            card = tour.plugin().offerWidget()
            self.assertIsNotNone(card)
            try:
                studioShell.toast(text, sticky=True, action=lambda: None, actionText="Show folder")
                self._wait(200)
                toast = self.mainWindow.findChild("QFrame", "StudioStickyToast")
                self.assertTrue(toast is not None and toast.isVisible(), (width, height))
                offerRect = tour._rectIn(card, self.mainWindow)
                toastRect = tour._rectIn(toast, self.mainWindow)
                self.assertFalse(offerRect.intersects(toastRect), (width, height, offerRect, toastRect))
                link = card.findChild("QPushButton", "StudioTourOfferMovedLink")
                self.assertTrue(link.isVisible())
                self.assertFalse(toastRect.intersects(tour._rectIn(link, self.mainWindow)), (width, height))
            finally:
                studioShell._stickyToast.hide()
            tour.plugin().offerWidget().findChild("QPushButton", "StudioTourOfferNotNowButton").click()
            self._pump()

    def test_09_first_start_offer(self):
        """The first-start offer is a non-modal card shown once; any answer (or opening a file) is remembered."""
        from StudioLib import tour

        studioShell = self._applyStudio()
        self._resize(1366, 768)
        settings = qt.QSettings()
        settings.remove("Studio/Tour")
        studioShell.switchTo("library")
        self._pump()
        focusBefore = slicer.app.focusWidget()
        self.assertTrue(tour.offerTour())
        self._pump()
        plugin = tour.plugin()
        card = plugin.offerWidget()
        self.assertIsNotNone(card)
        self.assertEqual(card.objectName, "StudioTourOffer")
        self.assertTrue(card.isVisible())
        self.assertIsNone(slicer.app.activeModalWidget())
        self.assertEqual(slicer.app.focusWidget(), focusBefore)
        # The Library page is full width (V-02): the offer covers none of its buttons (in the drop zone or the header) and
        # no project card
        offerRect = tour._rectIn(card, self.mainWindow)
        covered = []
        for objectName in ("StudioLibraryButtons", "StudioDropZoneTitle", "studioProjectCard", "studioStudyCard"):
            for widget in self.mainWindow.findChildren("QWidget", objectName):
                if widget.isVisible() and offerRect.intersects(tour._rectIn(widget, self.mainWindow)):
                    covered.append(objectName)
        self.assertEqual(covered, [])
        # Shown once
        self.assertFalse(tour.offerTour())
        notNow = card.findChild("QPushButton", "StudioTourOfferNotNowButton")
        notNow.click()
        self._pump()
        self.assertTrue(slicer.util.settingsValue("Studio/Tour/Offered", False, converter=slicer.util.toBool))
        self.assertIsNone(plugin.offerWidget())
        plugin._tryOffer()
        self.assertIsNone(plugin.offerWidget())

        # Elsewhere than Library: a notice line in the inspector; opening a file answers it
        settings.remove("Studio/Tour")
        studioShell.switchTo("draw")
        self._pump()
        self.assertTrue(tour.offerTour())
        self.assertEqual(studioShell.inspectorNoticeKey(), plugin.OFFER_NOTICE_KEY)
        self._loadVolume()
        self._wait(100)
        self.assertIsNone(plugin.offerWidget())
        self.assertIsNone(studioShell.inspectorNoticeKey())
        self.assertTrue(slicer.util.settingsValue("Studio/Tour/Offered", False, converter=slicer.util.toBool))

        # "Take the tour" starts it
        settings.remove("Studio/Tour")
        studioShell.switchTo("library")
        self._pump()
        self.assertTrue(tour.offerTour())
        plugin.offerWidget().findChild("QPushButton", "StudioTourOfferTakeButton").click()
        self._wait(100)
        self.assertEqual(tour.activeTour(), "studio")
        tour.stopTour()
        self._pump()

    def test_10_menus_and_help_link(self):
        """≡ > Help has the tour items in Studio only; the help popover has "Tour this workspace"."""
        from StudioLib import tour

        studioShell = self._applyStudio()
        self._resize(1366, 768)
        helpMenu = self.mainWindow.findChild("QMenu", "HelpMenu")
        names = [action.objectName for action in helpMenu.actions()]
        for name in ("StudioTakeTourAction", "StudioTourWorkspaceAction", "StudioComingFromSlicerAction", "StudioWhereDidItGoAction"):
            self.assertIn(name, names)
        # Above the items of 3D Slicer
        self.assertLess(names.index("StudioWhereDidItGoAction"), names.index("HelpAboutSlicerAppAction"))

        def action(name):
            return self.mainWindow.findChild("QAction", name)

        for workspace in ("library", "draw"):
            studioShell.switchTo(workspace)
            self._pump()
            helpMenu.aboutToShow()
            self.assertTrue(action("StudioTourWorkspaceAction").isEnabled())
            # Named after the workspace
            self.assertEqual(action("StudioTourWorkspaceAction").text, {"library": "Tour Library…", "draw": "Tour Draw…"}[workspace])
            action("StudioTourWorkspaceAction").trigger()
            self._pump()
            self.assertEqual(tour.activeTour(), workspace)
            tour.stopTour()
            self._pump()
            self.assertEqual(studioShell.currentWorkspace(), workspace)
            action("StudioTakeTourAction").trigger()
            self._pump()
            self.assertEqual(tour.activeTour(), "studio")
            tour.stopTour()
            action("StudioComingFromSlicerAction").trigger()
            self._pump()
            self.assertEqual(tour.activeTour(), "from-slicer")
            tour.stopTour()
            action("StudioWhereDidItGoAction").trigger()
            self._pump()
            self.assertTrue(tour.plugin().whereMap.isVisible())
            tour.plugin().whereMap.close()
            self._pump()
            self.assertEqual(studioShell.currentWorkspace(), workspace)

        # Help popover of the panel header
        studioShell.switchTo("draw")
        self._pump()
        header = studioShell.panelHeader
        self.assertIn("Tour this workspace", header.helpHtml())
        header._helpBrowser = qt.QTextBrowser()
        header._onHelpLinkClicked(qt.QUrl("studio-help:link:tour-workspace"))
        self._wait(50)
        self.assertEqual(tour.activeTour(), "draw")
        tour.stopTour()
        header._helpBrowser = None
        self._pump()

        # The "Coming from 3D Slicer" tour ends on the map
        self.assertTrue(tour.startTour("from-slicer"))
        player = tour.activePlayer()
        while player.active:
            player.next()
        self._wait(50)
        self.assertTrue(tour.plugin().whereMap.isVisible())
        tour.plugin().whereMap.close()

        # Classic: the items are gone from the shared Help menu
        studioShell.classic(persist=False)
        self._pump()
        names = [action.objectName for action in helpMenu.actions()]
        self.assertNotIn("StudioTakeTourAction", names)
        self.assertNotIn("StudioHelpSeparator", names)

    def test_11_where_map(self):
        """The 'Where did it go?' panel: non-modal, searchable, Esc clears then closes, Show me returns to it."""
        from StudioLib import tour

        studioShell = self._applyStudio()
        self._resize(1366, 768)
        self.assertTrue(tour.showMigrationMap())
        self._pump()
        whereMap = tour.plugin().whereMap
        self.assertTrue(whereMap.isVisible())
        self.assertIsNone(slicer.app.activeModalWidget())
        self.assertTrue(self.mainWindow.rect.contains(whereMap.frame.geometry))
        whereMap.searchField.setText("python")
        self._pump()
        self.assertEqual(whereMap.visibleRowIds()[0], "python-console")
        whereMap.searchField.setText("zzzz")
        self._pump()
        self.assertEqual(whereMap.visibleRowIds(), [])
        self.assertTrue(whereMap.emptyLabel.isVisible())
        # Esc: clears the search, then closes
        self._sendKey(whereMap.searchField, qt.Qt.Key_Escape)
        self.assertEqual(whereMap.searchField.text, "")
        self.assertTrue(whereMap.isVisible())
        self._sendKey(whereMap.searchField, qt.Qt.Key_Escape)
        self.assertFalse(whereMap.isVisible())
        # The shell's Esc ladder closes it too (rung 0.5)
        tour.showMigrationMap("layout")
        self._pump()
        self.mainWindow.setFocus()
        self.assertTrue(studioShell.handleEscape())
        self.assertFalse(whereMap.isVisible())
        # Show me, then back to the map with the same search
        tour.showMigrationMap("save")
        self._pump()
        button = None
        for candidate in whereMap.frame.findChildren("QPushButton", "StudioWhereShowMeButton"):
            if candidate.property("rowId") == "export-segmentation":
                button = candidate
        self.assertIsNotNone(button)
        button.click()
        self._pump()
        player = tour.activePlayer()
        self.assertIsNotNone(player)
        self.assertFalse(whereMap.isVisible())
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        self.assertEqual(player.target.objectName, "StudioSaveLesionButton")
        self._checkStep(player, "showme-save")
        player.card().nextButton.click()
        self._wait(50)
        self.assertTrue(whereMap.isVisible())
        self.assertEqual(whereMap.searchField.text, "save")
        self.assertEqual(studioShell.currentWorkspace(), "library")
        whereMap.close()

    def test_12_workspace_tour_from_a_module(self):
        """'Tour this workspace' from a module opened from Search: Esc and Skip tour bring the module back."""
        from StudioLib import tour

        studioShell = self._applyStudio()
        self._resize(1366, 768)
        studioShell.switchTo("library")
        self._pump()
        slicer.util.selectModule("Volumes")
        self._pump()
        before = self._viewState(studioShell)
        self.assertEqual(before["transient"], "Volumes")
        self.assertEqual(before["origin"], "library")
        self.assertEqual(studioShell.panelHeader.closeButton.text, "Library")

        # ≡ > Help > Tour Library…, then Esc through the shell's Esc ladder
        helpMenu = self.mainWindow.findChild("QMenu", "HelpMenu")
        helpMenu.aboutToShow()
        workspaceAction = self.mainWindow.findChild("QAction", "StudioTourWorkspaceAction")
        self.assertTrue(workspaceAction.isEnabled())
        self.assertEqual(workspaceAction.text, "Tour Library…")
        workspaceAction.trigger()
        self._pump()
        player = tour.activePlayer()
        self.assertEqual(tour.activeTour(), "library")
        self.assertEqual(studioShell.currentWorkspace(), "library")
        self.mainWindow.setFocus()
        self.assertTrue(studioShell.handleEscape())
        self._pump()
        self.assertFalse(player.active)
        self.assertEqual(self._viewState(studioShell), before)
        self.assertEqual(studioShell.panelHeader.closeButton.text, "Library")

        # The help popover link, then Skip tour
        header = studioShell.panelHeader
        self.assertIn("Tour this workspace", header.helpHtml())
        header._helpBrowser = qt.QTextBrowser()
        try:
            header._onHelpLinkClicked(qt.QUrl("studio-help:link:tour-workspace"))
            self._wait(50)
        finally:
            header._helpBrowser = None
        player = tour.activePlayer()
        self.assertEqual(tour.activeTour(), "library")
        player.next()
        player.card().skipButton.click()
        self._pump()
        self.assertFalse(player.active)
        self.assertEqual(self._viewState(studioShell), before)
        self.assertEqual(studioShell.panelHeader.closeButton.text, "Library")

    def test_13_settings_changed_during_a_tour_are_kept(self):
        """A setting the user changes during a tour (a shortcut still works) is kept; what the tour changed is put back."""
        from StudioLib import tour

        studioShell = self._applyStudio()
        self._resize(1366, 768)
        studioShell.switchTo("library")
        self._pump()
        settings = qt.QSettings()
        keys = ("Studio/LastOpenDirectory", "Studio/Autosave/Directory", "Studio/TourTestNewKey")
        saved = {key: settings.value(key) for key in keys if settings.contains(key)}
        try:
            settings.setValue("Studio/LastOpenDirectory", "/before")
            settings.remove("Studio/TourTestNewKey")
            viewKeys = ("Studio/LastWorkspace", "Studio/Draw/Layout", "Studio/Library/Layout")
            viewBefore = {key: str(settings.value(key)) for key in viewKeys}
            for leave in ("escape", "done"):
                settings.setValue("Studio/Autosave/Directory", "")
                self.assertTrue(tour.startTour("studio"))
                player = tour.activePlayer()
                while player.active and studioShell.currentWorkspace() != "draw":
                    player.next()
                self.assertTrue(player.active)
                # Written meanwhile by Studio itself, as Ctrl+O and the Autosave settings panel write them
                settings.setValue("Studio/LastOpenDirectory", f"/during-{leave}")
                settings.setValue("Studio/Autosave/Directory", f"/autosave-{leave}")
                settings.setValue("Studio/TourTestNewKey", leave)
                if leave == "escape":
                    self._sendKey(player.card().nextButton, qt.Qt.Key_Escape)
                else:
                    while player.active:
                        player.next()
                    self.assertTrue(player.completed)
                self._pump()
                self.assertFalse(player.active)
                self.assertEqual(settings.value("Studio/LastOpenDirectory"), f"/during-{leave}", leave)
                self.assertEqual(settings.value("Studio/Autosave/Directory"), f"/autosave-{leave}", leave)
                self.assertEqual(settings.value("Studio/TourTestNewKey"), leave, leave)
                # The workspace and the layouts that the tour showed are put back
                self.assertEqual(studioShell.currentWorkspace(), "library", leave)
                self.assertEqual({key: str(settings.value(key)) for key in viewKeys}, viewBefore, leave)
        finally:
            for key in keys:
                if key in saved:
                    settings.setValue(key, saved[key])
                else:
                    settings.remove(key)

    def test_14_drops_on_the_tour_are_ignored(self):
        """Files dragged over the dimmed area or the card are taken by the tour (the main window would load them)."""
        from StudioLib import tour

        self._applyStudio()
        self._resize(1366, 768)
        self.assertTrue(tour.startTour("studio"))
        player = tour.activePlayer()
        try:
            mimeData = qt.QMimeData()
            mimeData.setUrls([qt.QUrl.fromLocalFile("/tmp/does-not-exist.nrrd")])
            for widget in (player.overlay(), player.card().frame):
                self.assertTrue(widget.acceptDrops)
                try:
                    event = qt.QDragEnterEvent(qt.QPoint(5, 5), qt.Qt.CopyAction, mimeData, qt.Qt.LeftButton, qt.Qt.NoModifier)
                except (TypeError, ValueError):
                    # This PythonQt cannot make the event (QFlags arguments)
                    continue
                event.ignore()
                qt.QApplication.sendEvent(widget, event)
                self.assertTrue(event.isAccepted(), widget.objectName)
                self.assertEqual(event.dropAction(), qt.Qt.IgnoreAction, widget.objectName)
        finally:
            tour.stopTour()
            self._pump()

    def test_15_targets_below_the_fold_are_scrolled_into_view(self):
        """A control that is only scrolled out of its page (Library in a small window) is scrolled to, not skipped."""
        from StudioLib import tour

        studioShell = self._applyStudio()
        self._resize(1366, 768)
        studioShell.switchTo("library")
        self._pump()
        recentList = self.mainWindow.findChild("QWidget", "StudioRecentList")
        self.assertIsNotNone(recentList)
        scrollArea = recentList
        while scrollArea is not None and not scrollArea.inherits("QScrollArea"):
            scrollArea = scrollArea.parentWidget()
        self.assertIsNotNone(scrollArea, "the Library page is not in a scroll area")
        # A window so low that the list is below the fold, whatever is open
        for height in (700, 600, 520, 460):
            self._resize(1366, height)
            scrollArea.verticalScrollBar().setValue(0)
            self._pump()
            if recentList.visibleRegion().isEmpty():
                break
        self.assertTrue(recentList.isVisible())
        self.assertTrue(recentList.visibleRegion().isEmpty(), "the list is not below the fold in this window")
        self.assertGreater(scrollArea.verticalScrollBar().maximum, 0)
        self.assertTrue(tour.showMe("recent"), "Show me skipped a control that is only scrolled out")
        self._pump()
        player = tour.activePlayer()
        self.assertIsNotNone(player)
        self.assertGreater(scrollArea.verticalScrollBar().value, 0)
        self.assertFalse(recentList.visibleRegion().isEmpty())
        self._checkStep(player, "below-the-fold")
        tour.stopTour()
        self._pump()

    def test_16_card_labels_are_sentence_case(self):
        """The step counter and the "In 3D Slicer" caption of the card are sentence case, not capitals (VISUAL 2.4)."""
        from StudioLib import tour

        self._applyStudio()
        self._resize(1366, 768)
        self.assertTrue(tour.startTour("studio"))
        try:
            self._pump()
            card = tour.activePlayer().card()
            self.assertRegex(card.counterLabel.text, r"^Step 1 of \d+$")
            self.assertEqual(card.slicerCaption.text, "In 3D Slicer")
            for label in (card.counterLabel, card.slicerCaption):
                self.assertNotEqual(label.font.capitalization(), qt.QFont.AllUppercase, label.objectName)
                self.assertFalse(label.text.isupper(), label.objectName)
        finally:
            tour.stopTour()
            self._pump()

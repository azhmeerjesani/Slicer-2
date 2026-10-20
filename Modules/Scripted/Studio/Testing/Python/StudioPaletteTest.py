"""Tests of Search (the command palette, Ctrl+K), the keyboard shortcut overlay (Ctrl+/) and the in-view probe of
Slicer Studio (W3-06: StudioLib/palette.py, StudioLib/cheatsheet.py, StudioLib/probe.py, the Search tiers of
StudioLib/finder.py).

Run with the main window and testing enabled:

  Slicer --no-splash --testing --python-code "import slicer.testing; slicer.testing.runUnitTest(['<this dir>'], 'StudioPaletteTest'); slicer.app.exit(0)"

Studio never runs by itself under --testing; the tests apply it with ``Shell(force=True)`` and always restore the
Classic interface. They use the real index (the modules, layouts and actions of this build and the real synonym
tables of ``StudioLib.finder``): the gate queries of USABILITY 4 put the expected entry first with and without the
entries of W2-05 (simulated by a provider), and Enter runs it. All data is synthetic.
"""

import sys
import time
import types

import numpy as np
import qt
import vtk

import slicer
from slicer import ScriptedLoadableModule

#: USABILITY 4 gate queries: query -> expected first title
GATE_QUERIES = {
    "vol": "Volumes",
    "mark": "Markups",
    "trans": "Transforms",
    "fiducial": "Place point",
    "window level": "Window/level: Auto",
    "crosshair": "Show crosshair",
    "export": "Save lesion as NIfTI…",
    "four": "Four-Up",
    "seg ed": "Draw",
    "network": "Map",
}

#: The settings that the tests change
_SETTINGS = ("Studio/PaletteMRU", "Studio/AllowedToolbars", "Modules/FavoriteModules", "Studio/Draw/Layout", "Studio/Map/Layout",
             "Studio/LastWorkspace")


def _w205Provider(shell=None):
    """Entries like the ones W2-05 registers (U19), to check the gate queries with W2-05 present."""
    return [
        {"id": "draw.saveLesionAs", "title": "Save lesion as…", "category": "Draw", "callback": lambda: None, "synonyms": ["save as"]},
        {"id": "draw.renameLesion", "title": "Rename lesion", "category": "Draw", "callback": lambda: None, "synonyms": ["rename"]},
        {"id": "draw.centerLesion", "title": "Center on lesion", "category": "Draw", "callback": lambda: None, "key": "C", "synonyms": ["center"]},
        {"id": "draw.fillGapsAndSpecks", "title": "Fill gaps and specks", "category": "Draw", "callback": lambda: None, "synonyms": ["cleanup", "clean up"]},
        {"id": "draw.notes", "title": "Notes", "category": "Draw", "callback": lambda: None, "synonyms": ["notes", "metadata", "doi", "note"]},
        {"id": "draw.windowLevel.auto", "title": "Window/level: Auto", "category": "Draw", "callback": lambda: None,
         "synonyms": ["window level", "contrast"], "priority": 3},
        {"id": "draw.windowLevel.brain", "title": "Window/level: Brain", "category": "Draw", "callback": lambda: None,
         "synonyms": ["window level", "contrast"], "priority": 2},
        {"id": "tool.Paint", "title": "Brush", "category": "Draw tool", "callback": lambda: None, "key": "P", "synonyms": ["paint", "brush"]},
    ]


class StudioPaletteTest(ScriptedLoadableModule.ScriptedLoadableModuleTest):
    """Search, the shortcut overlay and the probe. They need the main window."""

    def setUp(self):
        slicer.mrmlScene.Clear(0)
        self.mainWindow = slicer.util.mainWindow()
        self.layoutManager = slicer.app.layoutManager()
        self.initialLayout = self.layoutManager.layout if self.layoutManager is not None else None
        settings = qt.QSettings()
        self.settingsBackup = {key: settings.value(key) for key in _SETTINGS}
        settings.remove("Studio/PaletteMRU")

    def tearDown(self):
        from StudioLib import draw, palette, shell

        if hasattr(self, "_saveLesionDialog"):
            draw.saveLesionDialog = self._saveLesionDialog
            del self._saveLesionDialog
        for provider in palette.providers():
            if provider is _w205Provider:
                palette.unregisterProvider(provider)
        live = shell._shell
        if live is not None:
            live.dispose()
        interactionNode = slicer.app.applicationLogic().GetInteractionNode()
        if interactionNode is not None:
            interactionNode.SwitchToViewTransformMode()
        slicer.mrmlScene.Clear(0)
        if self.layoutManager is not None:
            self.layoutManager.removeAllMaximizedViewNodes()
            if self.initialLayout is not None and self.layoutManager.layout != self.initialLayout:
                self.layoutManager.setLayout(self.initialLayout)
        settings = qt.QSettings()
        for key, value in self.settingsBackup.items():
            if value is None:
                settings.remove(key)
            else:
                settings.setValue(key, value)
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

    def _wait(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            slicer.app.processEvents()
            time.sleep(0.01)

    def _applyStudio(self):
        from StudioLib import shell

        self.assertIsNotNone(self.mainWindow, "Studio tests need the main window")
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        self._pump()
        # The Segment Editor widget (Draw tools) exists in a normal session after +300 ms
        slicer.modules.segmenteditor.widgetRepresentation()
        return studioShell

    def _first(self, studioShell, query, category=None):
        from StudioLib import palette

        results = palette.search(studioShell, query, category)
        return results[0].title if results else None

    def _press(self, widget, key, modifiers=qt.Qt.NoModifier, text=""):
        event = qt.QKeyEvent(qt.QEvent.KeyPress, key, modifiers, text)
        qt.QApplication.sendEvent(widget, event)
        self._pump()

    def _openAndRun(self, studioShell, query):
        """Open Search, type ``query``, press Enter. Return the title that was current."""
        from StudioLib import palette

        studioShell.openPalette()
        live = palette.instance()
        self.assertIsNotNone(live)
        self.assertTrue(live.isOpen())
        live.setQuery(query)
        current = live.currentEntry()
        self._press(live.field, qt.Qt.Key_Return)
        self._pump(6)
        self.assertFalse(live.isOpen(), f"{query}: Enter did not close Search")
        return current.title if current is not None else None

    def _mriVolume(self, name, dims, spacing, origin, value=0, fill=100):
        node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", name)
        array = np.full((dims[2], dims[1], dims[0]), fill, np.int16)
        slicer.util.updateVolumeFromArray(node, array)
        node.SetSpacing(spacing, spacing, spacing)
        node.SetOrigin(*origin)
        node.CreateDefaultDisplayNodes()
        return node

    def _cursorAt(self, viewName, ras):
        """Put the cursor of ``viewName`` at a RAS position (the slice is moved there). Return the probe text."""
        from StudioLib import probe

        sliceWidget = self.layoutManager.sliceWidget(viewName)
        sliceNode = sliceWidget.mrmlSliceNode()
        sliceNode.JumpSliceByCentering(*ras)
        self._pump()
        rasToXy = vtk.vtkMatrix4x4()
        rasToXy.DeepCopy(sliceNode.GetXYToRAS())
        rasToXy.Invert()
        xyz = rasToXy.MultiplyPoint(list(ras) + [1.0])[:3]
        crosshair = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLCrosshairNode")
        crosshair.SetCursorPositionXYZ(list(xyz), sliceNode)
        return probe.instance().update()

    #
    # Search
    #

    def test_01_gate_queries(self):
        """Every USABILITY 4 gate query puts the expected entry first, without and with the W2-05 entries."""
        from StudioLib import palette

        studioShell = self._applyStudio()
        for withW205 in (False, True):
            if withW205:
                palette.registerProvider(_w205Provider)
            for query, expected in GATE_QUERIES.items():
                self.assertEqual(self._first(studioShell, query), expected, f"'{query}' (W2-05 {'present' if withW205 else 'absent'})")
            fourUps = [entry for entry in palette.search(studioShell, "four") if entry.title.lower() in ("four-up", "four-up view")]
            self.assertEqual(len(fourUps), 1, "one Four-Up entry (layouts deduplicated by id)")
            notes = palette.search(studioShell, "notes")
            if withW205:
                self.assertEqual(notes[0].title, "Notes")
            else:
                # Nothing is named "notes" without W2-05: the empty state is shown (help mentions may follow)
                studioShell.openPalette()
                live = palette.instance()
                live.setQuery("notes")
                self.assertTrue(live.isNoMatchShown())
                self.assertIn("No match for", live.emptyLabel.text)
                live.close()
        self.assertEqual(self._first(studioShell, "mni"), "MNI152NLin2009cAsym 2mm brain")
        self.assertEqual(self._first(studioShell, "where is add data"), "File > Add Data")

    def test_02_enter_runs_the_gate_entries(self):
        """Enter runs the first entry of the gate queries."""
        from StudioLib import draw, importer

        studioShell = self._applyStudio()
        self.assertEqual(self._openAndRun(studioShell, "vol"), "Volumes")
        self.assertEqual(studioShell.currentModuleName(), "Volumes")
        self.assertEqual(self._openAndRun(studioShell, "mark"), "Markups")
        self.assertEqual(studioShell.currentModuleName(), "Markups")
        self.assertEqual(self._openAndRun(studioShell, "trans"), "Transforms")
        self.assertEqual(studioShell.currentModuleName(), "Transforms")
        self.assertEqual(self._openAndRun(studioShell, "seg ed"), "Draw")
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        importer.startOnTemplate(shell=studioShell)
        self._pump()
        self.assertEqual(self._openAndRun(studioShell, "four"), "Four-Up")
        self.assertEqual(self.layoutManager.layout, slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
        crosshair = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLCrosshairNode")
        modeBefore = crosshair.GetCrosshairMode()
        self.assertEqual(self._openAndRun(studioShell, "crosshair"), "Show crosshair")
        self.assertNotEqual(crosshair.GetCrosshairMode(), modeBefore)
        displayNode = draw.lesionNodes()[1].GetDisplayNode()
        displayNode.AutoWindowLevelOff()
        displayNode.SetWindowLevel(5.0, 5.0)
        self.assertEqual(self._openAndRun(studioShell, "window level"), "Window/level: Auto")
        self.assertTrue(displayNode.GetAutoWindowLevel())
        saved = []
        self._saveLesionDialog = draw.saveLesionDialog
        draw.saveLesionDialog = lambda shell=None, path=None: saved.append(shell)
        self.assertEqual(self._openAndRun(studioShell, "export"), "Save lesion as NIfTI…")
        self.assertEqual(len(saved), 1)
        self.assertEqual(self._openAndRun(studioShell, "network"), "Map")
        from StudioLib import workspaces

        if workspaces.isAvailable(workspaces.workspace("map")):
            self.assertEqual(studioShell.currentWorkspace(), "map")
        else:
            self.assertIn("Map", studioShell.toastText())
        interactionNode = slicer.app.applicationLogic().GetInteractionNode()
        # "network" opened Map when it is available: Place point stays in that workspace
        workspaceBefore = studioShell.currentWorkspace()
        self.assertEqual(self._openAndRun(studioShell, "fiducial"), "Place point")
        self.assertEqual(interactionNode.GetCurrentInteractionMode(), interactionNode.Place)
        selectionNode = slicer.app.applicationLogic().GetSelectionNode()
        pointList = slicer.mrmlScene.GetNodeByID(selectionNode.GetActivePlaceNodeID())
        self.assertEqual(pointList.GetName(), "Points")
        self.assertEqual(studioShell.currentWorkspace(), workspaceBefore, "Place point stays in the current workspace")
        self.assertTrue(studioShell.handleEscape())
        self.assertEqual(interactionNode.GetCurrentInteractionMode(), interactionNode.ViewTransform)

    def test_03_tiers(self):
        """One ranking for every entry: a command synonym outranks a module synonym; prefix, word starts and MRU."""
        from StudioLib import finder, palette

        studioShell = self._applyStudio()
        # finder.SYNONYMS is the module synonym table (the real one): "fiducial" names Markups
        self.assertIn("Markups", finder.SYNONYMS["fiducial"])
        self.assertLess(finder.RANK_SYNONYM, finder.RANK_MODULE_SYNONYM)
        self.assertLess(finder.RANK_MODULE_SYNONYM, finder.RANK_PREFIX)
        self.assertEqual(self._first(studioShell, "fiducial"), "Place point")
        live = palette._palette
        tiers = {entry.title: tier for entry, tier in palette.rank(live.index.entries, "fiducial")}
        self.assertEqual(tiers["Place point"], finder.TIER_COMMAND_SYNONYM)
        self.assertEqual(tiers["Markups"], finder.TIER_MODULE_SYNONYM)
        titles = [entry.title for entry in palette.search(studioShell, "fiducial")]
        self.assertLess(titles.index("Place point"), titles.index("Markups"))
        tiers = {entry.title: tier for entry, tier in palette.rank(live.index.entries, "window level")}
        self.assertEqual(tiers["Window/level: Auto"], finder.TIER_COMMAND_SYNONYM)
        self.assertEqual(tiers["Volumes"], finder.TIER_MODULE_SYNONYM)
        # Priority within a tier: Auto, Brain, Narrow, Reset
        titles = [entry.title for entry in palette.search(studioShell, "window level")][:4]
        self.assertEqual(titles, ["Window/level: Auto", "Window/level: Brain", "Window/level: Narrow", "Window/level: Reset"])
        # Word starts through an alias, and the workspace replaces its module
        tiers = {entry.id: tier for entry, tier in palette.rank(live.index.entries, "seg ed")}
        self.assertEqual(tiers["workspace.draw"], finder.TIER_WORD_START)
        self.assertNotIn("module.SegmentEditor", live.index.byId)
        self.assertNotIn("module.Studio", live.index.byId)
        # Single letters reach the core modules: the classic layouts the picker does not offer ("Tabbed 3D", "Triple 3D")
        # come after them in the same tier, the picker's layouts do not
        self.assertEqual(self._first(studioShell, "v"), "Volumes")
        self.assertEqual(self._first(studioShell, "t"), "Transforms")
        self.assertEqual(self._first(studioShell, "tr"), "Transforms")
        titles = [entry.title for entry in palette.search(studioShell, "t")]
        self.assertIn("Tabbed 3D", titles)
        self.assertLess(titles.index("Transforms"), titles.index("Tabbed 3D"))
        self.assertEqual(self._first(studioShell, "four"), "Four-Up")
        # MRU reorders within a tier
        self.assertEqual(self._first(studioShell, "vol"), "Volumes")
        self.assertTrue(palette.runEntry(studioShell, "module.VolumeRendering"))
        self.assertEqual(palette.mru()[0], "module.VolumeRendering")
        self.assertEqual(self._first(studioShell, "vol"), "Volume Rendering")
        self.assertTrue(palette.runEntry(studioShell, "module.Volumes"))
        self.assertEqual(self._first(studioShell, "vol"), "Volumes")
        for index in range(palette.MRU_SIZE + 5):
            palette.recordRun(f"test.entry{index}")
        self.assertEqual(len(palette.mru()), palette.MRU_SIZE)

    def test_04_providers(self):
        """One provider contract: dicts, the addendum's zero-argument tuples, PALETTE_ACTIONS; the later source wins."""
        from StudioLib import palette

        studioShell = self._applyStudio()
        calls = []
        shells = []

        def dictProvider(shell):
            shells.append(shell)
            return [{"id": "test.dict", "title": "Studio palette test dict entry", "category": "Test", "callback": lambda: calls.append("dict"),
                     "synonyms": ["zqxdict"], "key": "Ctrl+Shift+F9", "enabled": True},
                    {"id": "draw.saveLesion", "title": "Save lesion (provider)", "category": "Draw", "callback": lambda: calls.append("save"),
                     "synonyms": ["export"]}]

        def tupleProvider():
            return [("Studio palette test tuple entry", "zqxtuple, run everything", lambda: calls.append("tuple"), lambda: True),
                    ("Studio palette test disabled entry", ["zqxdisabled"], lambda: calls.append("disabled"), lambda: False)]

        cardModule = types.ModuleType("StudioLib.libcard_studiopalettetest")
        cardModule.PALETTE_ACTIONS = [{"id": "test.card", "title": "Studio palette test card entry", "category": "Library",
                                       "callback": lambda: calls.append("card"), "synonyms": ["zqxcard"]},
                                      {"id": "test.dict", "title": "Replaced by the provider", "category": "Test", "callback": lambda: None}]
        sys.modules[cardModule.__name__] = cardModule
        palette.registerProvider(dictProvider)
        palette.registerProvider(dictProvider)
        palette.registerProvider(tupleProvider)
        try:
            self.assertEqual(palette.providers().count(dictProvider), 1)
            self.assertEqual(self._first(studioShell, "zqxdict"), "Studio palette test dict entry")
            self.assertIs(shells[0], studioShell)
            self.assertEqual(self._first(studioShell, "zqxtuple"), "Studio palette test tuple entry")
            self.assertEqual(self._first(studioShell, "run everything"), "Studio palette test tuple entry")
            self.assertEqual(self._first(studioShell, "zqxcard"), "Studio palette test card entry")
            # Same id: the provider (registered later) replaces the PALETTE_ACTIONS entry, and the built-in entry
            self.assertEqual(self._first(studioShell, "export"), "Save lesion (provider)")
            live = palette._palette
            entry = live.index.entry("lnm.Studio palette test tuple entry")
            self.assertIsNotNone(entry)
            self.assertEqual(entry.category, "Map")
            self.assertTrue(palette.runEntry(studioShell, "lnm.Studio palette test tuple entry"))
            self.assertTrue(palette.runEntry(studioShell, "test.dict"))
            self.assertTrue(palette.runEntry(studioShell, "test.card"))
            self.assertEqual(calls[-3:], ["tuple", "dict", "card"])
            self.assertFalse(palette.runEntry(studioShell, "lnm.Studio palette test disabled entry"))
            self.assertNotIn("disabled", calls)
            # A failing provider does not break Search
            palette.registerProvider(lambda shell: 1 / 0)
            self.assertEqual(self._first(studioShell, "vol"), "Volumes")
        finally:
            for provider in palette.providers():
                if provider not in (_w205Provider,) and getattr(provider, "__module__", "") == __name__:
                    palette.unregisterProvider(provider)
            del sys.modules[cardModule.__name__]
        self.assertNotIn(dictProvider, palette.providers())

    def test_05_index_and_timing(self):
        """The index: modules, layouts (one per id), actions, tools; first open <= 150 ms, later opens <= 20 ms."""
        from StudioLib import palette

        studioShell = self._applyStudio()
        studioShell.openPalette()
        live = palette.instance()
        self.assertIsNotNone(live)
        self.assertLessEqual(live.firstOpenMs, 150.0)
        live.close()
        timings = []
        for index in range(3):
            studioShell.openPalette()
            timings.append(live.lastOpenMs)
            live.close()
        self.assertLessEqual(min(timings), 20.0, timings)
        moduleManager = slicer.app.moduleManager()
        expectedModules = {name for name in moduleManager.modulesNames()
                           if not moduleManager.module(name).hidden and name not in ("Studio", "SegmentEditor", "LesionNetworkMapping")}
        modules = {entry.moduleName for entry in live.index.entries if entry.kind == palette.KIND_MODULE}
        self.assertEqual(modules, expectedModules)
        layoutEntries = [entry for entry in live.index.entries if entry.kind == palette.KIND_LAYOUT]
        self.assertGreaterEqual(len(layoutEntries), 28)
        layoutMenu = self.mainWindow.findChild("QMenu", "LayoutMenu")
        titles = {entry.title for entry in layoutEntries}
        for path, action in palette._menuActions(layoutMenu):
            text = str(action.text).replace("&", "")
            self.assertTrue(text in titles or f"{path[-1]}: {text}" in titles if path else text in titles, text)
        self.assertEqual(len({entry.id for entry in layoutEntries}), len(layoutEntries))
        for entryId in ("layout.7001", "layout.7002", "layout.7003", "workspace.draw", "workspace.library", "workspace.classic", "action.open",
                        "action.placePoint", "draw.saveLesion", "view.crosshair", "view.linkViews", "view.resetViews", "view.maximize",
                        "view.windowLevel.auto", "action.pythonConsole", "action.errorLog", "action.recentMessages", "tool.Paint"):
            self.assertIn(entryId, live.index.byId, entryId)
        self.assertEqual(live.index.entry("tool.Paint").title, "Brush")
        self.assertTrue(any(entry.kind == palette.KIND_SETTINGS for entry in live.index.entries))
        self.assertTrue(any(entry.kind == palette.KIND_SAMPLE for entry in live.index.entries))
        self.assertTrue(any(entry.kind == palette.KIND_TOOLBAR for entry in live.index.entries))
        # Ctrl+F: modules only (with the workspaces that replace their modules)
        studioShell.openPalette("modules")
        self.assertEqual(live.category, palette.CATEGORY_MODULES)
        live.setQuery("seg")
        kinds = {entry.kind for entry in live.resultEntries()}
        self.assertTrue(kinds <= {palette.KIND_MODULE, palette.KIND_WORKSPACE}, kinds)
        live.setQuery("vol")
        self.assertEqual(live.currentEntry().title, "Volumes")
        self._press(live.field, qt.Qt.Key_Escape)
        self.assertFalse(live.isOpen())
        # The gate harness reads the rows of the list: the current row is the title of the best entry
        studioShell.openPalette()
        live.setQuery("vol")
        view = live.list
        self.assertEqual(str(view.currentIndex().data()), "Volumes")
        self._press(live.field, qt.Qt.Key_Down)
        self.assertNotEqual(str(view.currentIndex().data()), "Volumes")
        self._press(live.field, qt.Qt.Key_K, qt.Qt.ControlModifier)
        self.assertFalse(live.isOpen())

    def test_06_empty_query_favorites_recent(self):
        """Empty query: Recent tools, Recent files and the read-only Favorites; a Recent file re-opens without a new node."""
        from StudioLib import importer, palette

        studioShell = self._applyStudio()
        favoritesBefore = qt.QSettings().value("Modules/FavoriteModules")
        palette.runEntry(studioShell, "module.Transforms")
        studioShell.openPalette()
        live = palette.instance()
        rows = [str(live.list.item(index).text()) for index in range(live.list.count)]
        self.assertEqual(rows[0], "Recent tools")
        self.assertEqual(live.currentEntry().title, "Transforms", "Enter re-opens the last tool")
        favorites = palette.favoriteModules()
        if favorites:
            self.assertIn("Favorites", rows)
        live.close()
        self.assertEqual(qt.QSettings().value("Modules/FavoriteModules"), favoritesBefore, "Favorites are only read")
        # Recent files: loaded from the recently loaded menu, never loaded twice
        directory = qt.QTemporaryDir()
        path = directory.path() + "/StudioPaletteTestScan.nrrd"
        scan = self._mriVolume("StudioPaletteTestScan", (20, 20, 20), 1.0, (0.0, 0.0, 0.0))
        self.assertTrue(slicer.util.saveNode(scan, path))
        slicer.mrmlScene.RemoveNode(scan)
        importer.loadFiles([path], studioShell)
        self._pump()
        live.index.refresh()
        recent = [entry for entry in live.index.entries if entry.kind == palette.KIND_RECENT]
        entry = [entry for entry in recent if entry.id == f"recent:{path}"]
        self.assertTrue(entry, f"{path} is not in the recently loaded files: {[entry.id for entry in recent]}")
        nodesBefore = slicer.mrmlScene.GetNumberOfNodes()
        self.assertTrue(palette.runEntry(studioShell, entry[0].id))
        self._pump()
        self.assertEqual(slicer.mrmlScene.GetNumberOfNodes(), nodesBefore, "re-opening a Recent file adds no node")

    def test_07_commands(self):
        """View commands and window/level presets."""
        from StudioLib import importer, palette

        studioShell = self._applyStudio()
        volume = importer.startOnTemplate(shell=studioShell)[0]
        self._pump()
        displayNode = volume.GetDisplayNode()
        before = (displayNode.GetAutoWindowLevel(), displayNode.GetWindow(), displayNode.GetLevel())
        self.assertTrue(palette.applyWindowLevel("brain", studioShell))
        brain = (displayNode.GetWindow(), displayNode.GetLevel())
        self.assertFalse(displayNode.GetAutoWindowLevel())
        self.assertTrue(palette.applyWindowLevel("narrow", studioShell))
        self.assertLess(displayNode.GetWindow(), brain[0])
        self.assertTrue(palette.applyWindowLevel("reset", studioShell))
        self.assertEqual(bool(displayNode.GetAutoWindowLevel()), bool(before[0]))
        if not before[0]:
            self.assertAlmostEqual(displayNode.GetWindow(), before[1], places=3)
            self.assertAlmostEqual(displayNode.GetLevel(), before[2], places=3)
        self.assertTrue(palette.applyWindowLevel("auto", studioShell))
        self.assertTrue(displayNode.GetAutoWindowLevel())
        linked = palette.viewsLinked()
        palette.toggleLinkViews(studioShell)
        self.assertNotEqual(palette.viewsLinked(), linked)
        palette.toggleLinkViews(studioShell)
        self.assertEqual(palette.viewsLinked(), linked)
        palette.resetViews(studioShell)
        found = palette._intersectingSlicesVisibility()
        if found is not None:
            logic, mode = found
            before = logic.GetIntersectingSlicesEnabled(mode)
            palette.toggleSliceIntersections(studioShell)
            self.assertNotEqual(logic.GetIntersectingSlicesEnabled(mode), before)
            palette.toggleSliceIntersections(studioShell)
        # Show toolbar: the toolbar is shown and stays allowed in Studio
        toolBar = self.mainWindow.findChild("QToolBar", "MarkupsToolBar")
        if toolBar is not None:
            self.assertTrue(palette.runEntry(studioShell, "toolbar.MarkupsToolBar"))
            self._pump()
            self.assertTrue(toolBar.isVisible())
            allowed = slicer.util.settingsValue("Studio/AllowedToolbars", [])
            self.assertIn("MarkupsToolBar", [allowed] if isinstance(allowed, str) else list(allowed))

    #
    # Shortcut overlay
    #

    def test_08_cheat_sheet(self):
        """The overlay lists every live Studio shortcut, the tools in their live order, the Esc ladder and the platform
        notes; it stays open until Esc or Ctrl+/.
        """
        from StudioLib import cheatsheet
        from StudioLib import shell as shellModule

        studioShell = self._applyStudio()
        studioShell.switchTo("draw")
        self._pump()
        studioShell.toggleCheatSheet()
        self._wait(0.2)
        self.assertTrue(cheatsheet.isVisible())
        sheet = cheatsheet.instance()
        self.assertIsNotNone(self.mainWindow.findChild("QWidget", "StudioCheatSheet"))
        content = {section: rows for section, title, rows in cheatsheet.sections(studioShell)}
        listedKeys = {key for rows in content.values() for keys, description in rows for key in keys}
        for shortcut in self.mainWindow.findChildren("QShortcut"):
            if shortcut.property("studioShortcut") and shortcut.isEnabled():
                key = shortcut.key.toString(qt.QKeySequence.PortableText)
                self.assertIn(key, listedKeys, f"{key} ({shortcut.property('studioDescription')}) is not listed")
        drawRows = content[cheatsheet.SECTION_DRAW]
        descriptions = [description for keys, description in drawRows]
        editor = slicer.modules.SegmentEditorWidget.editor
        firstEffect = editor.effectByIndex(1)
        self.assertIn(["1"], [keys for keys, description in drawRows])
        self.assertTrue(any(str(firstEffect.name) in description or description.startswith(("Brush", "Threshold")) for description in descriptions))
        self.assertTrue(any("Smart fill" in description for description in descriptions))
        self.assertTrue(any("Save lesion" in description for description in descriptions), "the Draw keys are listed")
        escapeRows = content[cheatsheet.SECTION_ESCAPE]
        self.assertEqual(len(escapeRows), len(shellModule.ESCAPE_LADDER) + 1)
        platform = " ".join(description for keys, description in content[cheatsheet.SECTION_PLATFORM])
        self.assertIn("F10", platform)
        self.assertIn("Ctrl+F2", platform)
        self.assertIn("AltGr", platform)
        self.assertIn("Ctrl+5", platform)
        viewText = " ".join(description for keys, description in content[cheatsheet.SECTION_VIEWS])
        self.assertIn("Zoom", viewText)
        # A key listed by a live shortcut and by the static tables (Ctrl+Y: Redo) is one row
        for section, rows in content.items():
            sectionDescriptions = [description for keys, description in rows]
            duplicates = {description for description in sectionDescriptions if sectionDescriptions.count(description) > 1}
            self.assertFalse(duplicates, f"{section}: listed twice: {duplicates}")
        # The window is only filtered while the overlay is shown
        self.assertTrue(sheet._watching)
        # Not a timed toast: still open later
        self._wait(1.0)
        self.assertTrue(cheatsheet.isVisible())
        # Search in the overlay
        sheet.field.setText("smart fill")
        shown = sheet.visibleDescriptions()
        self.assertTrue(shown)
        self.assertTrue(all("smart fill" in text for text in shown))
        # Esc in its field, then Esc through the shell's ladder, and Ctrl+/ toggles
        self._press(sheet.field, qt.Qt.Key_Escape)
        self.assertFalse(cheatsheet.isVisible())
        self.assertFalse(sheet._watching)
        studioShell.toggleCheatSheet()
        self.assertTrue(cheatsheet.isVisible())
        self.assertTrue(studioShell.handleEscape())
        self.assertFalse(cheatsheet.isVisible())
        studioShell.toggleCheatSheet()
        self.assertTrue(cheatsheet.isVisible())
        studioShell.toggleCheatSheet()
        self.assertFalse(cheatsheet.isVisible())
        # Classic closes and removes it
        studioShell.toggleCheatSheet()
        studioShell.classic()
        self._pump()
        slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
        self.assertIsNone(cheatsheet.instance())
        widget = self.mainWindow.findChild("QWidget", "StudioCheatSheet")
        self.assertTrue(widget is None or widget.isHidden())

    #
    # Probe
    #

    def test_09_probe_texts(self):
        """The probe in MNI space, in scanner space and over a figure; hidden when the mouse leaves; Classic has none."""
        from StudioLib import importer, probe

        studioShell = self._applyStudio()
        live = probe.instance()
        self.assertIsNotNone(live)
        # An empty view: nothing to probe (no "(scanner)" for world coordinates)
        self.assertEqual(self._cursorAt("Red", (10.0, 20.0, 30.0)), "")
        volume, segmentation = importer.startOnTemplate(shell=studioShell)
        self._pump()
        self.layoutManager.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutOneUpRedSliceView)
        self._pump()
        text = self._cursorAt("Red", (-11.2, -17.8, 9.5))
        self.assertTrue(text.startswith("Left 11.2 · P 17.8 · S 9.5 (MNI) · "), text)
        self.assertNotIn("-", text.split("(MNI)")[0])
        self.assertEqual(live.text("Red"), text)
        # A painted lesion voxel is named
        segmentId = segmentation.GetSegmentation().GetSegmentIdBySegmentName("Lesion")
        array = np.zeros(slicer.util.arrayFromVolume(volume).shape, np.uint8)
        ijk = [round(value) for value in self._rasToIjk(volume, (-11.2, -17.8, 9.5))]
        array[ijk[2], ijk[1], ijk[0]] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentation, segmentId, volume)
        self._pump()
        text = self._cursorAt("Red", (-11.2, -17.8, 9.5))
        self.assertTrue(text.endswith(" · Lesion"), text)
        # The mouse leaves the view: hidden
        slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLCrosshairNode").SetCursorPositionInvalid()
        self.assertEqual(live.update(), "")
        self.assertEqual(live.text("Red"), "")
        # Scanner space: a grid that is not an MNI grid
        scan = self._mriVolume("ProbeScan", (60, 60, 60), 1.0, (-40.3, -50.1, -20.7), fill=0)
        ijk = [round(value) for value in self._rasToIjk(scan, (-11.2, -17.8, 9.5))]
        scanArray = slicer.util.arrayFromVolume(scan)
        scanArray[ijk[2], ijk[1], ijk[0]] = 412
        slicer.util.arrayFromVolumeModified(scan)
        slicer.util.setSliceViewerLayers(background=scan, foreground=None, label=None, fit=True)
        self._pump()
        text = self._cursorAt("Red", (-11.2, -17.8, 9.5))
        self.assertTrue(text.startswith("L 11.2 · P 17.8 · S 9.5 (scanner) · 412"), text)
        self.assertNotIn("Left", text)
        # A figure: no coordinates, the pixel and its value
        figure = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLVectorVolumeNode", "ProbeFigure")
        pixels = np.zeros((1, 100, 200, 3), np.uint8)
        pixels[0, 48, 120, :] = 209
        slicer.util.updateVolumeFromArray(figure, pixels)
        figure.SetAttribute("Studio.Role", "figure")
        figure.CreateDefaultDisplayNodes()
        slicer.util.setSliceViewerLayers(background=figure, foreground=None, label=None, fit=True)
        self._pump()
        text = self._cursorAt("Red", (120.0, 48.0, 0.0))
        self.assertEqual(text, "Figure · pixel (120, 48) · 0.82")
        # Classic: no probe, no actor
        actors = [view.actor for view in live.views.values()]
        studioShell.classic()
        self._pump()
        self.assertIsNone(probe.instance())
        redView = self.layoutManager.sliceWidget("Red").sliceView()
        renderers = redView.renderWindow().GetRenderers()
        for index in range(renderers.GetNumberOfItems()):
            for actor in actors:
                self.assertFalse(renderers.GetItemAsObject(index).HasViewProp(actor))

    def _rasToIjk(self, volume, ras):
        rasToIjk = vtk.vtkMatrix4x4()
        volume.GetRASToIJKMatrix(rasToIjk)
        return rasToIjk.MultiplyPoint(list(ras) + [1.0])[:3]

    def test_10_probe_rate_and_painting(self):
        """The cursor events are coalesced (at most one update per 33 ms) and nothing is computed while a button is down."""
        from StudioLib import importer, probe

        studioShell = self._applyStudio()
        importer.startOnTemplate(shell=studioShell)
        self._pump()
        live = probe.instance()
        sliceWidget = self.layoutManager.sliceWidget("Red")
        sliceNode = sliceWidget.mrmlSliceNode()
        crosshair = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLCrosshairNode")
        self._wait(0.1)
        countBefore = live.updateCount
        startTime = time.time()
        for index in range(60):
            crosshair.SetCursorPositionXYZ([100 + index, 100, 0], sliceNode)
            slicer.app.processEvents()
        self._wait(0.1)
        elapsed = time.time() - startTime
        updates = live.updateCount - countBefore
        self.assertGreaterEqual(updates, 1)
        self.assertLessEqual(updates, int(elapsed * 1000.0 / probe.UPDATE_INTERVAL_MS) + 1, f"{updates} updates in {elapsed:.3f} s")
        # A button is down (painting): no update
        original = probe._mouseButtonDown
        probe._mouseButtonDown = lambda: True
        try:
            countBefore = live.updateCount
            for index in range(20):
                crosshair.SetCursorPositionXYZ([120 + index, 110, 0], sliceNode)
                slicer.app.processEvents()
            self._wait(0.15)
            self.assertEqual(live.updateCount, countBefore)
        finally:
            probe._mouseButtonDown = original
        # The button is released with the mouse still: the text is refreshed once (a painted voxel is named)
        self._wait(0.15)
        self.assertGreater(live.updateCount, countBefore)

    def test_11_formats(self):
        """Pure formatting: never a minus sign; hemisphere words only in MNI space."""
        from StudioLib import probe

        self.assertEqual(probe.formatCoordinates((-11.2, -17.8, 9.5), True), "Left 11.2 · P 17.8 · S 9.5 (MNI)")
        self.assertEqual(probe.formatCoordinates((11.2, 17.8, -9.5), True), "Right 11.2 · A 17.8 · I 9.5 (MNI)")
        self.assertEqual(probe.formatCoordinates((-11.2, -17.8, 9.5), False), "L 11.2 · P 17.8 · S 9.5 (scanner)")
        self.assertEqual(probe.formatCoordinates((0.01, 0, 0), True), "Midline 0.0 · A 0.0 · S 0.0 (MNI)")
        self.assertEqual(probe.formatCoordinates((-24.4, -8.2, 12.0), True, decimals=0), "Left 24 · P 8 · S 12 (MNI)")
        self.assertEqual(probe.formatCoordinates((10, 20, 30), False, spaceName=False), "R 10.0 · A 20.0 · S 30.0")
        self.assertEqual(probe.formatNumber(412.0), "412")
        self.assertEqual(probe.formatNumber(8.3125), "8.31")
        self.assertEqual(probe.formatNumber(1234.56), "1234.6")
        node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "sub-01_desc-T_statmap")
        self.assertEqual(probe.statLabel(node), "T")
        node.SetName("network_zmap")
        self.assertEqual(probe.statLabel(node), "Z")

    def test_12_probe_placement(self):
        """In a narrow view the probe line is raised above the view's bottom texts; in a wide view it stays on the bottom row."""
        from StudioLib import importer, probe

        # Pure placement: a 330 px view, a 200 px line, the bottom-centre letter and the lower-left layer name
        # (left, right, top) of the letter at the view centre and of the layer name, as measured at 1366x768
        def letter(viewWidth):
            return (viewWidth / 2.0 - 4, viewWidth / 2.0 + 4, 19)

        self.assertEqual(probe.probePlacement(975, 200, [letter(975), (0, 251, 32)]), probe.MARGIN)
        self.assertEqual(probe.probePlacement(424, 200, [letter(424), (0, 191, 32)]), 19 + probe.VERTICAL_GAP)
        self.assertEqual(probe.probePlacement(330, 200, [letter(330), (0, 145, 32)]), 32 + probe.VERTICAL_GAP)

        # The Focus layout at 1366x768: the side views are about 330 px wide
        geometry = self.mainWindow.geometry
        maximized = self.mainWindow.isMaximized()
        try:
            self.mainWindow.showNormal()
            self.mainWindow.resize(1366, 768)
            studioShell = self._applyStudio()
            importer.startOnTemplate(shell=studioShell)
            self.layoutManager.setLayout(7001)
            self._wait(0.3)
            live = probe.instance()
            widths = []
            for viewName in ("Red", "Green"):
                text = self._cursorAt(viewName, (-11.2, -17.8, 9.5))
                self.assertTrue(text.startswith("Left 11.2"), text)
                view = live.views[viewName]
                viewWidth, viewHeight = view.renderer.GetSize()
                widths.append(viewWidth)
                relative = [0.0] * 4
                view.actor.GetBoundingBox(view.renderer, relative)
                # The bounding box is relative to the anchor: MARGIN from the right edge, view.bottom above the bottom edge
                box = (viewWidth - probe.MARGIN + relative[0], viewWidth - probe.MARGIN + relative[1], view.bottom + relative[2])
                obstacles = view._bottomTexts()[1](viewWidth, viewHeight, relative[3] - relative[2])
                self.assertTrue(obstacles, f"{viewName}: the layer name and the orientation letter are found")
                for left, right, top in obstacles:
                    overlaps = left < box[1] and right > box[0] and top > box[2]
                    self.assertFalse(overlaps, f"{viewName} ({viewWidth} px): {box} overlaps {(left, right, top)}")
                self.assertGreaterEqual(box[0], 0, f"{viewName}: the line is inside the view")
                if viewWidth > 600:
                    self.assertEqual(view.bottom, probe.MARGIN, f"{viewName}: a wide view keeps the bottom row")
                elif viewWidth < 360:
                    self.assertGreater(view.bottom, probe.MARGIN, f"{viewName}: a narrow view raises the line")
                    # Raised above the bottom-centre letter
                    centre = [top for left, right, top in obstacles if left < viewWidth / 2.0 < right]
                    self.assertTrue(centre, f"{viewName}: the orientation letter is found")
                    self.assertGreater(box[2], max(centre))
            self.assertLess(min(widths), 360, f"the Focus side views are narrow at 1366x768: {widths}")
        finally:
            if maximized:
                self.mainWindow.showMaximized()
            else:
                self.mainWindow.setGeometry(geometry)

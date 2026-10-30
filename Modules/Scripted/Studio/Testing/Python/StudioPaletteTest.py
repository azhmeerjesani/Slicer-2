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
            if provider is _w205Provider or getattr(provider, "__name__", "") == "paletteItems":
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
        # The real app registers the projects provider ("Save a copy as…" has the synonym "export"): so do the gate queries
        from StudioLib import projects_ui

        palette.registerProvider(projects_ui.paletteItems)
        self.assertTrue(any(entry.id == "projects.saveCopy" for entry in palette.search(studioShell, "copy")))
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

    def test_08b_cheat_sheet_title_in_a_narrow_window(self):
        """USABILITY 9.4: in a window of 960x540 logical px (QT_SCALE_FACTOR=2 on 1080p) the title of the overlay is not cut
        off; the hint gives way (it is also the tool tip of the close button), and the search field shrinks.
        """
        from StudioLib import cheatsheet

        studioShell = self._applyStudio()
        studioShell.switchTo("draw")
        self._pump()
        oldSize = self.mainWindow.size
        self.mainWindow.showNormal()
        self.mainWindow.resize(960, 540)
        self._wait(0.3)
        try:
            studioShell.toggleCheatSheet()
            self._wait(0.3)
            self.assertTrue(cheatsheet.isVisible())
            sheet = cheatsheet.instance()
            title = sheet.title
            self.assertGreaterEqual(title.width, title.sizeHint.width(), "the title is cut off")
            self.assertGreaterEqual(sheet.field.width, cheatsheet.SEARCH_MIN_WIDTH)
            if sheet.hint.isVisible():
                self.assertGreaterEqual(sheet.hint.width, sheet.hint.sizeHint.width(), "the hint is cut off")
            self.assertLessEqual(sheet.closeButton.mapTo(sheet.widget, qt.QPoint(sheet.closeButton.width, 0)).x(), sheet.widget.width)
            # Narrower still (a larger font): the hint gives way
            sheet._placeContent(560, 540)
            self.assertFalse(sheet.hint.isVisible())
            self.mainWindow.resize(1920, 1000)
            self._wait(0.3)
            self.assertTrue(sheet.hint.isVisible())
            self.assertGreaterEqual(sheet.hint.width, sheet.hint.sizeHint.width())
        finally:
            if cheatsheet.isVisible():
                studioShell.toggleCheatSheet()
            self.mainWindow.resize(oldSize)

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
                    # A wide view keeps the bottom row, except under the display statement of an axial or coronal view
                    from StudioLib import orientation

                    statementBox = orientation.viewStatementBox(viewName)
                    expected = probe.MARGIN if statementBox is None else statementBox[1] + statementBox[2] + probe.VERTICAL_GAP
                    self.assertEqual(view.bottom, expected, f"{viewName}: a wide view keeps the bottom row")
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

    def test_12b_probe_clear_of_orientation_statement(self):
        """The probe line does not overlap the orientation statement ("Radiological · L on right") at the bottom right of
        an axial or coronal view: it is raised above it. A sagittal view has no statement.
        """
        from StudioLib import importer, orientation, probe

        studioShell = self._applyStudio()
        importer.startOnTemplate(shell=studioShell)
        orientation.update()
        live = probe.instance()
        for viewName, hasStatement in (("Red", True), ("Green", True), ("Yellow", False)):
            text = self._cursorAt(viewName, (-13.9, -19.3, 17.5))
            self.assertTrue(text.startswith("Left 13.9"), text)
            view = live.views[viewName]
            viewWidth, _viewHeight = view.renderer.GetSize()
            statementBox = orientation.viewStatementBox(viewName)
            if not hasStatement:
                self.assertIsNone(statementBox, viewName)
                continue
            self.assertIsNotNone(statementBox, f"{viewName}: the statement is shown")
            # Both boxes as drawn: the actors' own bounding boxes, relative to their anchors at the right edge
            statementActor = orientation._controller.views[viewName].statementActor
            relative = [0.0] * 4
            statementActor.GetBoundingBox(view.renderer, relative)
            statementY = statementActor.GetPositionCoordinate().GetValue()[1]
            statement = (viewWidth - orientation.CORNER_ANNOTATION_MARGIN + relative[0], viewWidth - orientation.CORNER_ANNOTATION_MARGIN + relative[1],
                         statementY + relative[2], statementY + relative[3])
            view.actor.GetBoundingBox(view.renderer, relative)
            probeBox = (viewWidth - probe.MARGIN + relative[0], viewWidth - probe.MARGIN + relative[1],
                        view.bottom + relative[2], view.bottom + relative[3])
            horizontal = probeBox[0] < statement[1] and probeBox[1] > statement[0]
            vertical = probeBox[2] < statement[3] and probeBox[3] > statement[2]
            self.assertFalse(horizontal and vertical, f"{viewName}: the probe {probeBox} overlaps the statement {statement}")

    def test_13_more_tools_in_search(self):
        """Every Draw tool is in Search, also the ones under "More tools" (no key while they are hidden): "th" is
        Threshold, "t" is still Transforms, the guided tours come after the core entries, and choosing a hidden tool
        shows it in Draw.
        """
        from StudioLib import finder, importer, palette

        studioShell = self._applyStudio()
        importer.startOnTemplate(shell=studioShell)
        self._pump()
        widget = slicer.modules.SegmentEditorWidget
        if hasattr(widget, "setMoreToolsShown"):
            widget.setMoreToolsShown(False)
        self._pump()
        palette.search(studioShell, "x")
        live = palette._palette
        threshold = live.index.entry("tool.Threshold")
        self.assertIsNotNone(threshold, "Threshold (under More tools) is in Search")
        self.assertIsNone(threshold.key, "a hidden tool has no positional key")
        self.assertEqual(threshold.group, palette.GROUP_MORE_TOOLS)
        self.assertIsNotNone(live.index.entry("tool.Smoothing"))
        brush = live.index.entry("tool.Paint")
        self.assertEqual((brush.key, brush.group), ("1", finder.GROUP_CORE))
        self.assertEqual(self._first(studioShell, "th"), "Threshold")
        self.assertEqual(self._first(studioShell, "t"), "Transforms")
        titles = [entry.title for entry in palette.search(studioShell, "t")][:6]
        self.assertIn("Threshold", titles)
        self.assertFalse([title for title in titles if title.startswith("Take a tour")], titles)
        if any(entry.kind == palette.KIND_TOUR and entry.id != "tour.whereDidItGo" for entry in live.index.entries):
            self.assertTrue(palette.search(studioShell, "tour")[0].title.startswith("Take a tour"))
        # Choosing a hidden tool arms it and shows it among the tiles of Draw
        self.assertTrue(palette.runEntry(studioShell, "tool.Threshold"))
        self._pump()
        self.assertEqual(widget.editor.activeEffect().name, "Threshold")
        if hasattr(widget, "_moreToolsShown"):
            self.assertTrue(widget._moreToolsShown())
        live.index.refresh()
        self.assertIsNotNone(live.index.entry("tool.Threshold").key, "a shown tool has its positional key")
        widget.editor.setActiveEffect(None)
        if hasattr(widget, "setMoreToolsShown"):
            widget.setMoreToolsShown(False)

    def test_14_settings_entries_ignore_the_restart_marker(self):
        """After a theme or density change CTK shows "Appearance*" in the settings tree: the Search entry keeps the
        plain page name and id (and opens that page), instead of "Settings: Appearance*" (settings.Appearance*).
        """
        from StudioLib import palette

        dialog = slicer.app.settingsDialog()
        tree = dialog.findChild("QTreeWidget")
        item = next((tree.topLevelItem(i) for i in range(tree.topLevelItemCount) if str(tree.topLevelItem(i).text(0)).rstrip("*") == "Appearance"), None)
        self.assertIsNotNone(item, "the settings dialog has an Appearance page")
        original = item.text(0)
        try:
            item.setText(0, "Appearance*")
            self.assertIn("Appearance", palette._settingsPanels(dialog))
            self.assertFalse([title for title in palette._settingsPanels(dialog) if title.endswith("*")])
            palette.search(self._applyStudio(), "x")
            entries = {entry.id: entry.title for entry in palette._palette.index._settingsEntries(dialog)}
            self.assertEqual(entries.get("settings.Appearance"), "Settings: Appearance", entries)
            self.assertFalse([key for key in entries if key.endswith("*")], entries)
        finally:
            item.setText(0, original)

    def test_15_settings_panel_list_scrolls_to_a_panel_chosen_while_hidden(self):
        """Search > "Check for updates" selects the Updates panel while the settings dialog is still hidden: once shown, the
        panel list is scrolled so that the selected entry is fully visible instead of cut off at the bottom of the list.
        """
        self._applyStudio()
        dialog = slicer.app.settingsDialog()
        tree = dialog.findChild("QTreeWidget", "SettingsTreeWidget")
        self.assertIsNotNone(tree)
        panels = []
        for index in range(tree.topLevelItemCount):
            panels.append(str(tree.topLevelItem(index).data(0, qt.Qt.UserRole + 1101) or tree.topLevelItem(index).text(0)))
        self.assertGreater(len(panels), 4, panels)
        try:
            dialog.hide()
            self._pump()
            # A short list (the dialog has a minimum height), so that the last panel's entry is below its visible part
            tree.setMaximumHeight(250)
            dialog.setCurrentPanel(panels[0])
            dialog.setCurrentPanel(panels[-1])
            dialog.show()
            self._wait(0.5)
            item = tree.currentItem()
            self.assertIsNotNone(item)
            self.assertEqual(str(item.data(0, qt.Qt.UserRole + 1101) or item.text(0)), panels[-1])
            rect = tree.visualItemRect(item)
            self.assertGreater(tree.verticalScrollBar().maximum, 0, "the list does not scroll: the test needs a shorter list")
            self.assertGreaterEqual(rect.y(), 0, "the selected entry starts above the list")
            self.assertLessEqual(rect.y() + rect.height(), tree.viewport().height,
                                 f"the selected entry is cut off: y={rect.y()}, h={rect.height()}, viewport {tree.viewport().height}")
        finally:
            dialog.hide()
            tree.setMaximumHeight(16777215)
            self._pump()

    def test_15b_settings_dialog_look_with_qt6_layout_margins(self):
        """Qt 6: QLayout declares the ``contentsMargins`` property, so PythonQt gives a QMargins value and not the getter.

        ``SettingsDialogLook.apply`` called ``layout.contentsMargins()``, which raised "'QMargins' object is not callable"
        each time Settings was shown: the Appearance page and the 16 px dialog margins kept the official look and the panel
        list was not scrolled. Here the layouts behave as with Qt 6 and the look is still applied.
        """
        from StudioLib import shell, ui

        class Qt6Layout:
            """A QLayout as PythonQt wraps it with Qt 6: ``contentsMargins`` is the property value."""

            def __init__(self, layout):
                object.__setattr__(self, "_layout", layout)

            def __getattr__(self, name):
                if name == "contentsMargins":
                    return self._layout.contentsMargins()
                return getattr(self._layout, name)

        class Qt6Owner:
            """A widget whose ``layout()`` is a ``Qt6Layout``, and whose Appearance panel is one too."""

            def __init__(self, widget):
                object.__setattr__(self, "_widget", widget)

            def __getattr__(self, name):
                if name == "layout":
                    return lambda: Qt6Layout(self._widget.layout())
                if name == "findChild":
                    return lambda *args: self._wrapped(self._widget.findChild(*args))
                return getattr(self._widget, name)

            @staticmethod
            def _wrapped(child):
                return Qt6Owner(child) if child is not None and child.objectName == "qSlicerSettingsStylesPanel" else child

        # The helper reads the margins with Qt 5 (method) and Qt 6 (property value)
        probe = qt.QVBoxLayout()
        probe.setContentsMargins(3, 4, 5, 6)
        for candidate in (probe, Qt6Layout(probe)):
            margins = ui.layoutContentsMargins(candidate)
            self.assertEqual((margins.left(), margins.top(), margins.right(), margins.bottom()), (3, 4, 5, 6))

        dialog = qt.QDialog()
        dialogLayout = qt.QGridLayout(dialog)
        panel = qt.QWidget()
        panel.setObjectName("qSlicerSettingsStylesPanel")
        form = qt.QFormLayout(panel)
        label = qt.QLabel("Theme:")
        label.setObjectName("ThemeLabel")
        form.addRow(label, qt.QComboBox())
        dialogLayout.addWidget(panel, 0, 0)
        look = shell.SettingsDialogLook.__new__(shell.SettingsDialogLook)
        look.dialog = Qt6Owner(dialog)
        look.applied = False
        look._saved = {}
        scrolled = []
        look._scrollToCurrentPanel = lambda: scrolled.append(True)
        try:
            look.apply()
            self.assertTrue(look.applied, "the settings dialog look failed on the layout margins")
            self.assertIn("appearance", look._saved, "the Appearance page keeps the official look")
            self.assertIn("margins", look._saved)
            margins = dialogLayout.contentsMargins()
            self.assertEqual((margins.left(), margins.top(), margins.right(), margins.bottom()), (16, 16, 16, 16))
            self.assertEqual(label.minimumHeight, ui.SETTINGS_ROW_HEIGHT)
            self._wait(0.2)
            self.assertTrue(scrolled, "the panel list is not scrolled to the selected panel")
        finally:
            dialog.deleteLater()

    def test_16_search_index_without_qaction_menu(self):
        """Qt 6: QAction.menu() is a template that PythonQt does not wrap, so actions have no menu attribute. The submenus
        of the Layout menu are still found (shell.actionMenu) and Search opens and lists the layouts, as with Qt 5.
        """
        from StudioLib import palette, shell

        class Qt6Action:
            """A QAction as PythonQt wraps it with Qt 6: every attribute except menu."""

            def __init__(self, action):
                object.__setattr__(self, "_action", action)

            def __getattr__(self, name):
                if name in ("menu", "setMenu"):
                    raise AttributeError(f"QAction has no attribute named '{name}'")
                return getattr(self._action, name)

            def __eq__(self, other):
                return self._action == getattr(other, "_action", other)

            __hash__ = None

        class Qt6Menu:
            def __init__(self, menu):
                self._menu = menu

            def actions(self):
                return [Qt6Action(action) for action in self._menu.actions()]

        # The helper, on a plain action, a submenu (QMenu.addMenu) and a separator
        menu = qt.QMenu("Layout")
        plain = menu.addAction("Plain")
        submenu = menu.addMenu("Compare")
        submenu.addAction("2 viewers")
        separator = menu.addSeparator()
        for action, expectedMenu in ((plain, None), (submenu.menuAction(), submenu), (separator, None)):
            self.assertEqual(shell.actionMenu(Qt6Action(action)), expectedMenu, str(action.text))
            self.assertEqual(shell.actionMenu(action), expectedMenu, str(action.text))
        expected = [(path, str(action.text)) for path, action in palette._menuActions(menu)]
        self.assertEqual(expected, [((), "Plain"), (("Compare",), "2 viewers")])
        self.assertEqual([(path, str(action.text)) for path, action in palette._menuActions(Qt6Menu(menu))], expected)

        # Search opens and finds a layout when the Layout menu's actions have no menu()
        realMenuActions = palette._menuActions
        palette._menuActions = lambda menu, path=(): realMenuActions(Qt6Menu(menu) if menu is not None else None, path)
        try:
            studioShell = self._applyStudio()
            layoutMenu = self.mainWindow.findChild("QMenu", "LayoutMenu")
            self.assertIsNotNone(layoutMenu)
            self.assertTrue(palette._menuActions(layoutMenu), "the Layout menu has actions")
            studioShell.openPalette()
            live = palette.instance()
            self.assertIsNotNone(live)
            self.assertTrue(live.isOpen(), "Search did not open")
            live.close()
            self._pump()
            titles = [entry.title for entry in palette.search(studioShell, "conventional")]
            self.assertTrue([title for title in titles if "Conventional" in title], titles)
        finally:
            palette._menuActions = realMenuActions
        menu.deleteLater()

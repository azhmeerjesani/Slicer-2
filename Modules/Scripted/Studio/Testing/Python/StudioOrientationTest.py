"""Laterality cues and module search ranking of the Slicer Studio shell (StudioLib/orientation.py, StudioLib/finder.py).

Run with the main window and testing enabled:

  Slicer --no-splash --testing --python-code "import slicer.testing; slicer.testing.runUnitTest(['<this dir>'], 'StudioOrientationTest'); slicer.app.exit(0)"

Studio never runs by itself under --testing; the tests apply it with ``Shell(force=True)`` and
always restore the Classic interface. They cover:

- the letters of the screen edges of the axial, coronal and sagittal views (radiological display),
  their update after an in-plane rotation and for oblique directions, and after layout changes;
- the removal of every letter actor when Studio is left (Classic shows nothing new);
- the ranking of the module finder ("vol" -> Volumes, "mark" -> Markups, "fiducial" -> Markups...),
  Enter opening the best match, and the classic finder restored in Classic.
"""

import qt
import vtk

import slicer
from slicer import ScriptedLoadableModule


class StudioOrientationTest(ScriptedLoadableModule.ScriptedLoadableModuleTest):
    """Orientation letters and module finder ranking of the Studio shell. They need the main window."""

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
        if self.layoutManager is not None:
            for name in self.layoutManager.sliceViewNames():
                sliceWidget = self.layoutManager.sliceWidget(name)
                if sliceWidget is not None:
                    sliceWidget.mrmlSliceNode().SetOrientationToDefault()
            if self.initialLayout is not None and self.layoutManager.layout != self.initialLayout:
                self.layoutManager.setLayout(self.initialLayout)
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

    def _applyStudio(self):
        from StudioLib import shell

        self.assertIsNotNone(self.mainWindow, "Studio tests need the main window")
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        self._pump()
        return studioShell

    def _ensureLetters(self, studioShell):
        """The letters are on by default (Studio/OrientationLetters); install them if this profile turned them off."""
        from StudioLib import orientation

        if orientation.instance() is None:
            self.assertFalse(orientation.isEnabled(), "Studio did not install the orientation letters")
            orientation._controller = orientation._OrientationLetters(slicer.app.layoutManager())
            orientation._controller.synchronizeViews()
        return orientation.instance()

    def _setFourUp(self):
        self.layoutManager.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
        self._pump()
        for name, orientationName in (("Red", "Axial"), ("Yellow", "Sagittal"), ("Green", "Coronal")):
            self.layoutManager.sliceWidget(name).mrmlSliceNode().SetOrientation(orientationName)
        self._pump()

    @staticmethod
    def _rotateInPlane(sliceNode, degrees):
        """Rotate the slice plane about its normal (the third column of SliceToRAS)."""
        sliceToRAS = vtk.vtkMatrix4x4()
        sliceToRAS.DeepCopy(sliceNode.GetSliceToRAS())
        rotation = vtk.vtkTransform()
        rotation.RotateZ(degrees)
        transform = vtk.vtkTransform()
        transform.SetMatrix(sliceToRAS)
        transform.Concatenate(rotation)  # pre-multiply: rotation in slice coordinates
        sliceNode.GetSliceToRAS().DeepCopy(transform.GetMatrix())
        sliceNode.UpdateMatrices()

    #
    # Orientation letters
    #

    def test_1_direction_letters(self):
        """Letters of RAS directions: one letter near an axis, the dominant letters of oblique directions."""
        from StudioLib import orientation

        self.assertEqual(orientation.directionLetters((1, 0, 0)), "R")
        self.assertEqual(orientation.directionLetters((-1, 0, 0)), "L")
        self.assertEqual(orientation.directionLetters((0, 1, 0)), "A")
        self.assertEqual(orientation.directionLetters((0, -1, 0)), "P")
        self.assertEqual(orientation.directionLetters((0, 0, 1)), "S")
        self.assertEqual(orientation.directionLetters((0, 0, -1)), "I")
        # 10 degrees from R: still "R"
        self.assertEqual(orientation.directionLetters((0.985, 0.174, 0)), "R")
        # 45 degrees between L and A: both, in R/A/S order for equal components
        self.assertEqual(orientation.directionLetters((-0.7071, 0.7071, 0)), "LA")
        # 35 degrees from P toward S: the dominant letter first
        self.assertEqual(orientation.directionLetters((0, -0.819, 0.574)), "PS")
        self.assertEqual(orientation.directionLetters((1, 1, 1)), "RAS")
        self.assertEqual(orientation.directionLetters((0, 0, 0)), "")

    def test_2_default_orientations(self):
        """Axial R..L / A..P, coronal R..L / S..I, sagittal A..P / S..I (radiological display)."""
        from StudioLib import orientation

        studioShell = self._applyStudio()
        self._ensureLetters(studioShell)
        self._setFourUp()
        orientation.update()
        # (left, right, top, bottom)
        self.assertEqual(orientation.viewLetters("Red"), ("R", "L", "A", "P"))
        self.assertEqual(orientation.viewLetters("Green"), ("R", "L", "S", "I"))
        self.assertEqual(orientation.viewLetters("Yellow"), ("A", "P", "S", "I"))
        view = orientation.instance().views["Red"]
        self.assertEqual([actor.GetInput() for actor in view.actors], ["R", "L", "A", "P"])
        for actor in view.actors:
            self.assertTrue(view.renderer.HasViewProp(actor))
            self.assertTrue(actor.GetTextProperty().GetBold())
            self.assertTrue(actor.GetTextProperty().GetShadow())
        # The corner annotation of SliceViewAnnotations is not used
        cornerAnnotation = self.layoutManager.sliceWidget("Red").sliceView().cornerAnnotation()
        for position in range(4, 8):
            self.assertFalse(cornerAnnotation.GetText(position))

    def test_3_rotation_and_flip(self):
        """An in-plane rotation of the axial view rotates the letters (through the observer and the 0 ms timer)."""
        from StudioLib import orientation

        studioShell = self._applyStudio()
        self._ensureLetters(studioShell)
        self._setFourUp()
        orientation.update()
        redNode = self.layoutManager.sliceWidget("Red").mrmlSliceNode()

        self._rotateInPlane(redNode, 90)
        self._pump()
        # Screen +x (was patient left) now points to the patient's anterior or posterior: the four letters rotate
        letters = orientation.viewLetters("Red")
        self.assertEqual(sorted(letters[:2]), ["A", "P"])
        self.assertEqual(sorted(letters[2:]), ["L", "R"])
        self.assertNotEqual(letters, ("R", "L", "A", "P"))
        # A further 90 degrees: upside down (180 degrees from the default)
        self._rotateInPlane(redNode, 90)
        self._pump()
        self.assertEqual(orientation.viewLetters("Red"), ("L", "R", "P", "A"))

        # Oblique: 45 degrees from the default shows two letters on each edge
        redNode.SetOrientationToAxial()
        self._rotateInPlane(redNode, 45)
        self._pump()
        for letters in orientation.viewLetters("Red"):
            self.assertEqual(len(letters), 2, orientation.viewLetters("Red"))

        # Neurological display (screen x flipped to patient right)
        redNode.SetOrientationToAxial()
        flip = vtk.vtkMatrix4x4()
        flip.DeepCopy(redNode.GetSliceToRAS())
        for row in range(3):
            flip.SetElement(row, 0, -flip.GetElement(row, 0))
        redNode.GetSliceToRAS().DeepCopy(flip)
        redNode.UpdateMatrices()
        self._pump()
        self.assertEqual(orientation.viewLetters("Red"), ("L", "R", "A", "P"))

        # Changes of the slice offset do not change the letters
        redNode.SetOrientationToAxial()
        self._pump()
        view = orientation.instance().views["Red"]
        key = view.directionKey
        redNode.SetSliceOffset(redNode.GetSliceOffset() + 10)
        self._pump()
        self.assertEqual(view.directionKey, key)
        self.assertEqual(orientation.viewLetters("Red"), ("R", "L", "A", "P"))

    def test_4_layout_changes(self):
        """Views shown by a layout change get letters."""
        from StudioLib import orientation

        studioShell = self._applyStudio()
        controller = self._ensureLetters(studioShell)
        self.layoutManager.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutOneUpYellowSliceView)
        self._pump()
        self._setFourUp()
        self._pump()
        for name in ("Red", "Green", "Yellow"):
            self.assertIn(name, controller.views)
            self.assertTrue(all(orientation.viewLetters(name)), name)

    def test_4_ruler(self):
        """The bottom letter moves above the slice view ruler while it is shown, and back without it."""
        from StudioLib import orientation

        studioShell = self._applyStudio()
        self._ensureLetters(studioShell)
        self._setFourUp()
        orientation.update()
        redNode = self.layoutManager.sliceWidget("Red").mrmlSliceNode()
        view = orientation.instance().views["Red"]
        bottomReference = view.actors[3].GetPositionCoordinate().GetReferenceCoordinate()
        self.assertEqual(tuple(bottomReference.GetValue())[:2], (0.5, 0.0))
        try:
            for rulerType in (redNode.RulerTypeThin, redNode.RulerTypeThick):
                redNode.SetRulerType(rulerType)
                self._pump()
                expected = orientation.bottomReference(rulerType)
                self.assertGreater(expected[1], 0.0)
                self.assertAlmostEqual(bottomReference.GetValue()[1], expected[1])
            self.assertGreater(orientation.bottomReference(redNode.RulerTypeThick)[1], orientation.bottomReference(redNode.RulerTypeThin)[1])
            # The other edges and the letters do not move
            self.assertEqual(orientation.viewLetters("Red"), ("R", "L", "A", "P"))
            self.assertEqual(tuple(view.actors[0].GetPositionCoordinate().GetReferenceCoordinate().GetValue())[:2], (0.0, 0.5))
        finally:
            redNode.SetRulerType(redNode.RulerTypeNone)
            self._pump()
        self.assertEqual(tuple(bottomReference.GetValue())[:2], (0.5, 0.0))

    def test_5_uninstall_removes_everything(self):
        """Classic shows no letters: every actor, observer and connection is removed."""
        from StudioLib import orientation

        self.assertIsNone(orientation.instance(), "no letters under --testing without Studio")
        studioShell = self._applyStudio()
        controller = self._ensureLetters(studioShell)
        self._setFourUp()
        orientation.update()
        views = list(controller.views.values())
        self.assertGreaterEqual(len(views), 3)
        actors = [(view.renderer, actor) for view in views for actor in view.actors]
        self.assertEqual(len(actors), 4 * len(views))

        studioShell.classic(persist=False)
        self._pump()
        if orientation.instance() is not None:
            # The letters were installed by _ensureLetters() (setting off in this profile)
            orientation.uninstall(None)
        self.assertIsNone(orientation.instance())
        for renderer, actor in actors:
            self.assertFalse(renderer.HasViewProp(actor))
        for view in views:
            self.assertIsNone(view.observerTag)
        self.assertFalse(controller.updateTimer.isActive())
        # Slice node changes after leaving Studio do not recreate anything
        redNode = self.layoutManager.sliceWidget("Red").mrmlSliceNode()
        self._rotateInPlane(redNode, 90)
        self._pump()
        self.assertIsNone(orientation.viewLetters("Red"))

        # In Classic, setEnabled() only stores the setting, and install() shows nothing (a future
        # settings toggle reachable from Classic must not add letters there)
        settings = qt.QSettings()
        hadSetting = settings.contains(orientation.SETTINGS_KEY)
        previousSetting = settings.value(orientation.SETTINGS_KEY)
        try:
            self.assertFalse(studioShell.isActive())
            orientation.setEnabled(studioShell, True)
            self.assertTrue(orientation.isEnabled())
            self.assertIsNone(orientation.instance())
            orientation.install(studioShell)
            self.assertIsNone(orientation.instance())
            orientation.install(None)
            self.assertIsNone(orientation.instance())
            for name in self.layoutManager.sliceViewNames():
                renderers = self.layoutManager.sliceWidget(name).sliceView().renderWindow().GetRenderers()
                for index in range(renderers.GetNumberOfItems()):
                    props = renderers.GetItemAsObject(index).GetViewProps()
                    for propIndex in range(props.GetNumberOfItems()):
                        prop = props.GetItemAsObject(propIndex)
                        objectName = prop.GetObjectName() if hasattr(prop, "GetObjectName") else ""
                        self.assertFalse(str(objectName).startswith("StudioOrientationLetter"), name)
        finally:
            if hadSetting:
                settings.setValue(orientation.SETTINGS_KEY, previousSetting)
            else:
                settings.remove(orientation.SETTINGS_KEY)

    #
    # Module finder ranking
    #

    def test_6_ranking_function(self):
        """rankModules(): exact > synonym > prefix > word start > synonym prefix > substring; core modules first."""
        from StudioLib import finder

        def entry(name, title, group=finder._GROUP_OTHER):
            core = finder.CORE_MODULES.index(name) if name in finder.CORE_MODULES else None
            return finder.ModuleEntry(name, title, finder._GROUP_CORE if core is not None else group, core)

        demoted = finder._GROUP_DEMOTED
        entries = [
            entry("AddScalarVolumes", "Add Scalar Volumes", demoted),
            entry("Volumes", "Volumes"),
            entry("VolumeRendering", "Volume Rendering"),
            entry("LandmarkRegistration", "Landmark Registration", demoted),
            entry("Markups", "Markups"),
            entry("BRAINSTransformConvert", "Transform Convert (BRAINS)", demoted),
            entry("Transforms", "Transforms"),
            entry("SegmentEditor", "Segment Editor"),
            entry("Segmentations", "Segmentations"),
            entry("SegmentStatistics", "Segment Statistics"),
            entry("FiducialRegistration", "Fiducial Registration", demoted),
            entry("Data", "Data"),
            entry("SimpleFilters", "Simple Filters"),
            entry("BRAINSIntensityNormalize", "Intensity Normalize"),
        ]
        self.assertEqual(finder.rankModules("vol", entries)[:2], ["Volumes", "VolumeRendering"])
        self.assertEqual(finder.rankModules("volumes", entries)[0], "Volumes")
        self.assertEqual(finder.rankModules("mark", entries), ["Markups", "LandmarkRegistration"])
        self.assertEqual(finder.rankModules("trans", entries), ["Transforms", "BRAINSTransformConvert"])
        self.assertEqual(finder.rankModules("segment", entries)[0], "SegmentEditor")
        self.assertEqual(finder.rankModules("fiducial", entries), ["Markups", "FiducialRegistration"])
        self.assertEqual(finder.rankModules("seg ed", entries), ["SegmentEditor"])
        self.assertEqual(finder.rankModules("Segment Editor", entries)[0], "SegmentEditor")
        self.assertEqual(finder.rankModules("paint", entries), ["SegmentEditor"])
        self.assertEqual(finder.rankModules("window level", entries), ["Volumes"])
        self.assertEqual(finder.rankModules("", entries), [])
        self.assertEqual(finder.rankModules("xyzzy", entries), [])
        # Title word starts before synonym prefixes: "fil" lists Simple Filters before Data ("files")
        self.assertEqual(finder.rankModules("fil", entries), ["SimpleFilters", "Data"])
        # Spaces, '-' and '_' are ignored, other punctuation must match
        self.assertEqual(finder.rankModules("segment-editor", entries)[0], "SegmentEditor")
        self.assertEqual(finder.rankModules("segment_editor", entries)[0], "SegmentEditor")
        self.assertEqual(finder.rankModules("c++", entries), [])
        self.assertEqual(finder.rankModules("(brains)", entries), ["BRAINSTransformConvert"])

    def test_7_finder_in_studio(self):
        """The search pill's finder selects the best match (Enter opens it); Classic restores the classic finder."""
        from StudioLib import finder

        dialog = finder.finderDialog()
        self.assertIsNotNone(dialog)
        listView = dialog.findChild("QWidget", "ModuleListView")
        filterModel = listView.filterModel()
        classicTitle = dialog.windowTitle

        studioShell = self._applyStudio()
        self.assertIsNotNone(finder.instance())
        self.assertNotEqual(dialog.windowTitle, classicTitle)
        topBar = self.mainWindow.findChild("QToolBar", "StudioTopBar")
        placeholder = topBar.findChild("QLabel", "StudioSearchPlaceholder")
        self.assertEqual(placeholder.text, "Search modules")

        expected = {
            "vol": "Volumes",
            "volumes": "Volumes",
            "mark": "Markups",
            "trans": "Transforms",
            "segment": "SegmentEditor",
            "fiducial": "Markups",
            "seg ed": "SegmentEditor",
            "paint": "SegmentEditor",
        }
        try:
            for query, moduleName in expected.items():
                dialog.setModuleTitleFilterText(query)
                self.assertEqual(dialog.currentModuleName, moduleName, query)
                self.assertEqual(filterModel.data(filterModel.index(0, 0), qt.Qt.UserRole), moduleName, query)
            # Fewer, ranked rows than the classic substring filter: CLI modules after the core modules
            dialog.setModuleTitleFilterText("vol")
            names = [filterModel.data(filterModel.index(row, 0), qt.Qt.UserRole) for row in range(filterModel.rowCount())]
            if "AddScalarVolumes" in names:
                self.assertLess(names.index("Volumes"), names.index("AddScalarVolumes"))
            dialog.setModuleTitleFilterText("xyzzy")
            self.assertEqual(filterModel.rowCount(), 0)
            dialog.setModuleTitleFilterText("")
            self.assertGreater(filterModel.rowCount(), 20)
        finally:
            dialog.setModuleTitleFilterText("")

        studioShell.classic(persist=False)
        self._pump()
        self.assertIsNone(finder.instance())
        self.assertEqual(dialog.windowTitle, classicTitle)
        try:
            # Classic: case-insensitive substring filter in the model order (alphabetical module names)
            dialog.setModuleTitleFilterText("vol")
            names = [filterModel.data(filterModel.index(row, 0), qt.Qt.UserRole) for row in range(filterModel.rowCount())]
            self.assertIn("Volumes", names)
            self.assertIn("CropVolume", names)
            sourceModel = filterModel.sourceModel() if callable(filterModel.sourceModel) else filterModel.sourceModel
            sourceOrder = [sourceModel.data(sourceModel.index(row, 0), qt.Qt.UserRole) for row in range(sourceModel.rowCount())]
            self.assertEqual(names, [name for name in sourceOrder if name in names])
            self.assertEqual(filterModel.sortColumn(), -1)
            self.assertEqual(filterModel.filterCaseSensitivity, qt.Qt.CaseInsensitive)
            self.assertFalse(filterModel.showModules)
        finally:
            dialog.setModuleTitleFilterText("")

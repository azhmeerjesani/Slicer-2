"""Tests of the Studio import routes (U-07): spaces, figures, statistical maps, lesions without a scan, loaded sets,
scans from the Sample Data module, the 1 mm template offer, new lesions, participants tables and Recent bookkeeping.

Run with the main window and testing enabled:

  Slicer --no-splash --testing --python-code "import slicer.testing; slicer.testing.runUnitTest(['<this dir>'], 'StudioImportTest'); slicer.app.exit(0)"

Studio never runs by itself under --testing; the tests apply it with ``Shell(force=True)`` and always restore the
Classic interface. All data is synthetic and written to a temporary folder.
"""

import gzip
import json
import os
import struct
import tempfile
import time

import numpy as np
import qt
import vtk

import slicer
from slicer import ScriptedLoadableModule

#: Grids of the tests: (dims, spacing, direction signs, origin of voxel (0, 0, 0))
GRID_2009C_2MM = ((97, 115, 97), 2.0, (1, 1, 1), (-96.5, -132.5, -78.5))
GRID_NLIN6_2MM = ((91, 109, 91), 2.0, (-1, 1, 1), (90.0, -126.0, -72.0))
#: The 2009c grid shifted by 1.1 mm: not a template grid (scan space)
GRID_SHIFTED = ((97, 115, 97), 2.0, (1, 1, 1), (-95.4, -132.5, -78.5))


class StudioImportTest(ScriptedLoadableModule.ScriptedLoadableModuleTest):
    """Import routes of Studio. They need the main window."""

    def setUp(self):
        slicer.mrmlScene.Clear(0)
        self.mainWindow = slicer.util.mainWindow()
        self.layoutManager = slicer.app.layoutManager()
        self.initialLayout = self.layoutManager.layout if self.layoutManager is not None else None
        self.directory = tempfile.mkdtemp(prefix="StudioImportTest", dir=slicer.app.temporaryPath)
        self.settingsBackup = {}
        for key in ("Studio/Template1mmOffer", "Studio/RecentOpenTimes", "Studio/RecentFolders", "Studio/Draw/Layout"):
            self.settingsBackup[key] = qt.QSettings().value(key)

    def tearDown(self):
        from StudioLib import importer, shell

        controller = importer._figureController
        if controller is not None:
            controller.restore()
        live = shell._shell
        if live is not None:
            live.dispose()
        slicer.mrmlScene.Clear(0)
        importer._closeTemplateBanner()
        if self.layoutManager is not None and self.initialLayout is not None and self.layoutManager.layout != self.initialLayout:
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

    def _wait(self, ms):
        end = time.time() + ms / 1000.0
        while time.time() < end:
            slicer.app.processEvents()
            time.sleep(0.01)

    def _applyStudio(self):
        from StudioLib import shell

        self.assertIsNotNone(self.mainWindow, "Studio tests need the main window")
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        self._pump()
        return studioShell

    def _ijkToRas(self, grid):
        _dims, spacing, signs, origin = grid
        matrix = vtk.vtkMatrix4x4()
        for axis in range(3):
            matrix.SetElement(axis, axis, signs[axis] * spacing)
            matrix.SetElement(axis, 3, origin[axis])
        return matrix

    def _writeVolume(self, name, grid, array=None, labelmap=False, subdirectory=None):
        """Write a NIfTI volume on ``grid`` (voxels of ``array``, z-y-x order) and return its path."""
        dims = grid[0]
        if array is None:
            array = np.zeros((dims[2], dims[1], dims[0]), np.uint8 if labelmap else np.int16)
        className = "vtkMRMLLabelMapVolumeNode" if labelmap else "vtkMRMLScalarVolumeNode"
        node = slicer.mrmlScene.AddNewNodeByClass(className, "StudioImportTestVolume")
        slicer.util.updateVolumeFromArray(node, array)
        node.SetIJKToRASMatrix(self._ijkToRas(grid))
        directory = os.path.join(self.directory, subdirectory) if subdirectory else self.directory
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        self.assertTrue(slicer.util.saveNode(node, path), path)
        slicer.mrmlScene.RemoveNode(node)
        return path

    def _writeMask(self, name, grid, center=(45, 50, 40), size=3, subdirectory=None):
        dims = grid[0]
        array = np.zeros((dims[2], dims[1], dims[0]), np.uint8)
        x, y, z = center
        array[z:z + size, y:y + size, x:x + size] = 1
        return self._writeVolume(name, grid, array, labelmap=True, subdirectory=subdirectory)

    def _writeTMap(self, name="sub-06_desc-T_funcmap.nii.gz"):
        """A float T map on the 2009c 2 mm grid (one positive blob) and return its path."""
        dims = GRID_2009C_2MM[0]
        z, y, x = np.mgrid[0:dims[2], 0:dims[1], 0:dims[0]]
        values = (8.0 * np.exp(-((x - 30) ** 2 + (y - 60) ** 2 + (z - 50) ** 2) / 60.0)).astype(np.float32)
        node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode")
        slicer.util.updateVolumeFromArray(node, values)
        node.SetIJKToRASMatrix(self._ijkToRas(GRID_2009C_2MM))
        path = os.path.join(self.directory, name)
        self.assertTrue(slicer.util.saveNode(node, path))
        slicer.mrmlScene.RemoveNode(node)
        return path

    def _writeFigure(self, name="case1_fig2.png", width=200, height=100):
        image = qt.QImage(width, height, qt.QImage.Format_RGB32)
        image.fill(qt.QColor(255, 255, 255))
        painter = qt.QPainter(image)
        painter.fillRect(20, 20, 40, 30, qt.QColor(200, 200, 0))
        painter.end()
        path = os.path.join(self.directory, name)
        self.assertTrue(image.save(path))
        return path

    @staticmethod
    def _editor():
        return slicer.modules.SegmentEditorWidget.editor

    @staticmethod
    def _composite(viewName):
        return slicer.app.layoutManager().sliceWidget(viewName).mrmlSliceCompositeNode()

    @staticmethod
    def _sliceNode(viewName):
        return slicer.app.layoutManager().sliceWidget(viewName).mrmlSliceNode()

    def _background(self, viewName):
        nodeId = self._composite(viewName).GetBackgroundVolumeID()
        return slicer.mrmlScene.GetNodeByID(nodeId) if nodeId else None

    def _voxels(self, segmentationNode):
        segmentation = segmentationNode.GetSegmentation()
        total = 0
        for index in range(segmentation.GetNumberOfSegments()):
            segmentId = segmentation.GetNthSegmentID(index)
            array = slicer.util.arrayFromSegmentInternalBinaryLabelmap(segmentationNode, segmentId)
            if array is not None:
                labelValue = segmentation.GetSegment(segmentId).GetLabelValue()
                total += int(np.count_nonzero(array == labelValue))
        return total

    def _interactor(self, viewName):
        return slicer.app.layoutManager().sliceWidget(viewName).sliceView().interactor()

    def _stroke(self, viewName, fractions=((0.4, 0.4), (0.45, 0.45), (0.5, 0.5), (0.55, 0.5))):
        """A left-button stroke through the given points (fractions of the view size)."""
        interactor = self._interactor(viewName)
        width, height = interactor.GetRenderWindow().GetSize()
        points = [(int(fx * width), int(fy * height)) for fx, fy in fractions]
        interactor.SetEventInformationFlipY(points[0][0], points[0][1], 0, 0, chr(0), 0, None)
        interactor.InvokeEvent(vtk.vtkCommand.MouseMoveEvent)
        interactor.InvokeEvent(vtk.vtkCommand.LeftButtonPressEvent)
        for x, y in points[1:]:
            interactor.SetEventInformationFlipY(x, y, 0, 0, chr(0), 0, None)
            interactor.InvokeEvent(vtk.vtkCommand.MouseMoveEvent)
        interactor.InvokeEvent(vtk.vtkCommand.LeftButtonReleaseEvent)
        self._pump(6)

    def _wheel(self, viewName, forward=True, control=False):
        interactor = self._interactor(viewName)
        width, height = interactor.GetRenderWindow().GetSize()
        interactor.SetEventInformationFlipY(width // 2, height // 2, 1 if control else 0, 0, chr(0), 0, None)
        interactor.InvokeEvent(vtk.vtkCommand.MouseWheelForwardEvent if forward else vtk.vtkCommand.MouseWheelBackwardEvent)
        interactor.SetEventInformationFlipY(width // 2, height // 2, 0, 0, chr(0), 0, None)
        self._pump(4)

    def _key(self, viewName, keySym):
        interactor = self._interactor(viewName)
        interactor.SetKeySym(keySym)
        interactor.InvokeEvent(vtk.vtkCommand.KeyPressEvent)
        interactor.InvokeEvent(vtk.vtkCommand.KeyReleaseEvent)
        self._pump(4)

    def _routeFigure(self, studioShell, fromLibrary=True):
        from StudioLib import importer

        if fromLibrary:
            studioShell.switchTo("library")
            self._pump()
        # A new file each time (a file that is already open is shown again, not loaded twice)
        self._figureCount = getattr(self, "_figureCount", 0) + 1
        path = self._writeFigure(f"case1_fig{self._figureCount}.png")
        result = importer.handleDrop([path], shell=studioShell)
        self._pump(10)
        self.assertEqual(len(result.figures), 1)
        return result.figures[0]

    def _noticeButton(self, studioShell, text):
        notice = studioShell.inspectorNotice()
        self.assertIsNotNone(notice)
        for button in notice.findChildren("QAbstractButton"):
            if str(button.text) == text:
                return button
        self.fail(f"no button {text!r} in the notice")
        return None

    #
    # Spaces
    #

    def test_space_table(self):
        """The four template grids are MNI, in any storage direction; a 1.1 mm shift and an oblique grid are not."""
        from StudioLib import space

        def grid(dims, spacing, origin, signs=(1, 1, 1)):
            matrix = np.diag([signs[0] * spacing, signs[1] * spacing, signs[2] * spacing, 1.0])
            matrix[:3, 3] = origin
            return dims, matrix

        cases = [
            (grid((97, 115, 97), 2.0, (-96.5, -132.5, -78.5)), ("MNI152NLin2009cAsym", 2)),
            (grid((193, 229, 193), 1.0, (-96.0, -132.0, -78.0)), ("MNI152NLin2009cAsym", 1)),
            (grid((91, 109, 91), 2.0, (90.0, -126.0, -72.0), (-1, 1, 1)), ("MNI152NLin6Asym", 2)),
            (grid((182, 218, 182), 1.0, (90.0, -126.0, -72.0), (-1, 1, 1)), ("MNI152NLin6Asym", 1)),
            # The same NLin6 grid stored RAS
            (grid((91, 109, 91), 2.0, (-90.0, -126.0, -72.0)), ("MNI152NLin6Asym", 2)),
            (grid((97, 115, 97), 2.0, (-95.4, -132.5, -78.5)), None),
            (grid((97, 115, 97), 2.0, (-96.5, -132.5, -78.49)), None),
            # Within 1 mm of a template grid (detect_space's own default tolerance): still scan space
            (grid((91, 109, 91), 2.0, (90.6, -126.0, -72.0), (-1, 1, 1)), None),
        ]
        for (dims, matrix), expected in cases:
            self.assertEqual(space.tableSpace(dims, matrix), expected, (dims, matrix[:3, 3]))
            self.assertEqual(space.spaceOf((dims, matrix)), expected, (dims, matrix[:3, 3]))
        oblique = np.eye(4)
        oblique[:3, :3] = [[0.0, -2.0, 0.0], [2.0, 0.0, 0.0], [0.0, 0.0, 2.0]]
        oblique[:3, :3] = oblique[:3, :3] @ np.array([[np.cos(0.3), -np.sin(0.3), 0], [np.sin(0.3), np.cos(0.3), 0], [0, 0, 1]])
        self.assertIsNone(space.tableSpace((97, 115, 97), oblique))

        # Nodes: a volume, a segmentation (its reference geometry), the Studio.Space attribute, a figure
        volume = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode")
        slicer.util.updateVolumeFromArray(volume, np.zeros((91, 109, 91), np.int16))
        volume.SetIJKToRASMatrix(self._ijkToRas(GRID_NLIN6_2MM))
        self.assertEqual(space.spaceOf(volume), ("MNI152NLin6Asym", 2))
        self.assertTrue(space.isMni(volume))
        segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode")
        segmentation.SetReferenceImageGeometryParameterFromVolumeNode(volume)
        self.assertEqual(space.spaceOf(segmentation), ("MNI152NLin6Asym", 2))
        self.assertEqual(space.hemisphereWord(-20.0, segmentation), "Left")
        self.assertEqual(space.hemisphereWord(20.0, segmentation), "Right")
        self.assertEqual(space.hemisphereWord(20.0, segmentation, partsSpanBoth=True), "Both hemispheres")
        shifted = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode")
        slicer.util.updateVolumeFromArray(shifted, np.zeros((97, 115, 97), np.int16))
        shifted.SetIJKToRASMatrix(self._ijkToRas(GRID_SHIFTED))
        self.assertIsNone(space.spaceOf(shifted))
        self.assertIsNone(space.hemisphereWord(-20.0, shifted))
        shifted.SetAttribute("Studio.Space", "MNI152NLin2009cAsym 2")
        self.assertEqual(space.spaceOf(shifted), ("MNI152NLin2009cAsym", 2))
        shifted.SetAttribute("Studio.Space", "native")
        self.assertIsNone(space.spaceOf(shifted))
        self.assertEqual(space.spaceLabel(("MNI152NLin6Asym", 2)), "MNI152NLin6Asym 2 mm")

    def test_intent_code_detection(self):
        """Statistical maps are recognized by their name or their NIfTI intent code (little and big endian)."""
        from StudioLib import importer

        plain = self._writeVolume("map.nii", GRID_2009C_2MM)
        self.assertEqual(importer.fileKind(plain), "volume")
        with open(plain, "r+b") as f:
            f.seek(68)
            f.write(struct.pack("<h", 3))
        self.assertEqual(importer.niftiIntentCode(plain), 3)
        self.assertTrue(importer.isStatMap(plain))
        self.assertEqual(importer.fileKind(plain), "statmap")
        # Big endian header, gzipped
        header = bytearray(352)
        header[0:4] = struct.pack(">i", 348)
        header[68:70] = struct.pack(">h", 5)
        bigEndian = os.path.join(self.directory, "zscore.nii.gz")
        with gzip.open(bigEndian, "wb") as f:
            f.write(bytes(header))
        self.assertEqual(importer.niftiIntentCode(bigEndian), 5)
        self.assertTrue(importer.isStatMap(bigEndian))
        # Names
        for name in ("sub-05_desc-T_funcmap.nii.gz", "x_statmap.nii", "group_desc-AvgRFz_x.nii.gz", "desc-zmap.nii"):
            self.assertTrue(importer.STATMAP_NAME_PATTERN.search(name), name)
        for name in ("sub-05_T1w.nii.gz", "desc-Tumor.nii.gz", "sub-05_desc-T1_x.nii.gz"):
            self.assertFalse(importer.STATMAP_NAME_PATTERN.search(name), name)
        # Lesion names keep priority; figures and tables
        self.assertEqual(importer.fileKind(os.path.join(self.directory, "sub-05_desc-T_lesion.nii.gz")), "lesion")
        self.assertEqual(importer.fileKind(os.path.join(self.directory, "fig.PNG")), "figure")
        self.assertEqual(importer.fileKind(os.path.join(self.directory, "participants.tsv")), "table")

    #
    # Figures
    #

    def test_figure_routed(self):
        """A dropped PNG: Compare layout, the figure in Yellow, the template in Red as the source, Draw, the question."""
        from StudioLib import importer, space

        studioShell = self._applyStudio()
        figure = self._routeFigure(studioShell)
        self.assertEqual(self.layoutManager.layout, importer.COMPARE_LAYOUT_ID)
        self.assertEqual(self._background("Yellow").GetID(), figure.GetID())
        template = self._background("Red")
        self.assertIsNotNone(template)
        self.assertEqual(template.GetAttribute("Studio.Role"), "template")
        self.assertEqual(self._editor().sourceVolumeNode().GetID(), template.GetID())
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        self.assertEqual(figure.GetAttribute("Studio.Role"), "figure")
        self.assertEqual(figure.GetAttribute("Studio.FigureConvention"), "unknown")
        self.assertEqual(studioShell.inspectorNoticeKey(), "figure-convention")
        self.assertTrue(studioShell.inspectorNotice().isVisible())
        self.assertIn("patient's right", str(studioShell.inspectorNotice().findChild("QLabel", "StudioFigureConventionNoticeText").text))
        self.assertTrue(space.isFigureView("Yellow"))
        self.assertFalse(space.isFigureView("Red"))
        self.assertEqual(space.figureViewNames(), ["Yellow"])
        self.assertEqual(space.figureConvention("Yellow"), "unknown")
        self.assertIsNone(space.spaceOf(figure))
        self.assertEqual(studioShell.toastText(), "Figure shown on the right; the template on the left is where you draw")
        # The figure is fitted and at its slice; Yellow is unlinked and axial
        self.assertAlmostEqual(self._sliceNode("Yellow").GetSliceOffset(), 0.0, places=3)
        self.assertEqual(self._sliceNode("Yellow").GetOrientation(), "Axial")
        self.assertFalse(self._composite("Yellow").GetLinkedControl())
        # The figure pane (D-U4): at least 460 px (700 px on a 1920 px screen) and 45 % of the view area, the template
        # view keeping 240 px
        self._wait(400)
        red, yellow = self.layoutManager.sliceWidget("Red"), self.layoutManager.sliceWidget("Yellow")
        total = red.width + yellow.width
        minimum = importer._FigureReference.figurePaneMinimumWidth(yellow)
        redMinimum = max(importer.TEMPLATE_PANE_MIN_WIDTH, red.minimumSizeHint.width())
        expected = min(max(0.45 * total, minimum), total - redMinimum)
        self.assertGreaterEqual(yellow.width, expected - 3, (red.width, yellow.width, minimum, redMinimum))
        self.assertGreaterEqual(red.width, redMinimum - 3)
        # Never a source: selectInSegmentEditor ignores a figure
        importer.selectInSegmentEditor(self._editor().segmentationNode(), figure)
        self.assertEqual(self._editor().sourceVolumeNode().GetID(), template.GetID())
        # A second figure replaces the first in Yellow and asks again
        importer.answerFigureConvention("radiological")
        second = self._routeFigure(studioShell, fromLibrary=False)
        self.assertEqual(self._background("Yellow").GetID(), second.GetID())
        self.assertEqual(studioShell.inspectorNoticeKey(), "figure-convention")
        self.assertEqual(self.layoutManager.layout, importer.COMPARE_LAYOUT_ID)
        # Another layout and back: the Segment Editor shows its source in every view, Yellow keeps the figure
        from StudioLib import layouts

        self.layoutManager.setLayout(layouts.FOCUS_LAYOUT_ID)
        self._pump(6)
        self.assertEqual(self._background("Yellow").GetID(), second.GetID())
        self.layoutManager.setLayout(importer.COMPARE_LAYOUT_ID)
        self._pump(6)
        self.assertEqual(self._background("Yellow").GetID(), second.GetID())
        self.assertIsNotNone(importer.figureController())
        # A new source volume (another scan opened): the figure stays too
        scan = self._writeVolume("sub-09_T1w.nii.gz", GRID_SHIFTED)
        importer.loadFiles([scan], studioShell)
        self._pump(8)
        self.assertEqual(self._editor().sourceVolumeNode().GetName(), "sub-09_T1w")
        self.assertEqual(self._background("Yellow").GetID(), second.GetID())
        self.assertEqual(self._background("Red").GetName(), "sub-09_T1w")

    def test_figure_guard(self):
        """Paint over the figure changes no voxel, over Red it paints; wheel, keys and Link views keep Yellow at its slice."""
        from StudioLib import importer

        studioShell = self._applyStudio()
        self._routeFigure(studioShell)
        editor = self._editor()
        lesion = editor.segmentationNode()
        editor.setActiveEffectByName("Paint")
        self._pump()
        self.assertEqual(editor.activeEffect().name, "Paint")
        self._stroke("Yellow")
        self.assertEqual(self._voxels(lesion), 0)
        self.assertIn("figure is a reference", studioShell.toastText())
        self._stroke("Red")
        self.assertGreater(self._voxels(lesion), 0)
        # Wheel and slice keys over the figure: the figure has one slice
        for forward in (True, False, True):
            self._wheel("Yellow", forward)
        for keySym in ("Up", "Down", "Prior", "Next", "Home", "End"):
            self._key("Yellow", keySym)
        self.assertAlmostEqual(self._sliceNode("Yellow").GetSliceOffset(), 0.0, places=3)
        # The wheel still scrolls Red
        redOffset = self._sliceNode("Red").GetSliceOffset()
        self._wheel("Red", True)
        self._wheel("Red", True)
        self.assertNotAlmostEqual(self._sliceNode("Red").GetSliceOffset(), redOffset, places=3)
        # Link views: every composite node linked (the view bar's link button), then Red scrolled
        for compositeNode in slicer.util.getNodesByClass("vtkMRMLSliceCompositeNode"):
            compositeNode.SetLinkedControl(True)
        self._pump(4)
        self.assertFalse(self._composite("Yellow").GetLinkedControl())
        self._wheel("Red", True)
        self._wheel("Red", False)
        self._wheel("Red", False)
        self.assertAlmostEqual(self._sliceNode("Yellow").GetSliceOffset(), 0.0, places=3)
        self.assertEqual(self._sliceNode("Yellow").GetViewGroup(), importer.FIGURE_VIEW_GROUP)
        # Without an active effect, a left click over the figure is not blocked (and changes nothing)
        editor.setActiveEffect(None)
        self._pump()
        before = self._voxels(lesion)
        self._stroke("Yellow")
        self.assertEqual(self._voxels(lesion), before)
        # Classic: no guard; Studio again: the guard is back
        studioShell.classic(persist=False)
        self._pump()
        self.assertEqual(importer._figureController._guardTags, [])
        self.assertIsNone(studioShell.inspectorNotice())
        studioShell.studio()
        self._pump()
        self.assertTrue(importer._figureController._guardTags)
        # The unanswered question is shown again
        studioShell.switchTo("draw")
        self._pump()
        self.assertEqual(studioShell.inspectorNoticeKey(), "figure-convention")

    def test_figure_convention_mirror(self):
        """Answering "Right" (neurological) mirrors the figure pane and stores the answer; "Left" leaves it."""
        studioShell = self._applyStudio()
        figure = self._routeFigure(studioShell)
        sliceToRas = self._sliceNode("Yellow").GetSliceToRAS()
        before = [sliceToRas.GetElement(row, 0) for row in range(3)]
        self.assertLess(before[0], 0)
        self._noticeButton(studioShell, "Right").click()
        self._pump(4)
        after = [sliceToRas.GetElement(row, 0) for row in range(3)]
        self.assertEqual(after, [-value for value in before])
        self.assertEqual(figure.GetAttribute("Studio.FigureConvention"), "neurological")
        self.assertNotEqual(studioShell.inspectorNoticeKey(), "figure-convention")
        self.assertAlmostEqual(self._sliceNode("Yellow").GetSliceOffset(), 0.0, places=3)
        from StudioLib import space

        self.assertEqual(space.figureConvention(figure), "neurological")
        # A new figure: asked again, shown unmirrored until answered; "Left" keeps it as it is
        second = self._routeFigure(studioShell, fromLibrary=False)
        self.assertLess(self._sliceNode("Yellow").GetSliceToRAS().GetElement(0, 0), 0)
        self._noticeButton(studioShell, "Left").click()
        self._pump(4)
        self.assertLess(self._sliceNode("Yellow").GetSliceToRAS().GetElement(0, 0), 0)
        self.assertEqual(second.GetAttribute("Studio.FigureConvention"), "radiological")

    def test_figure_removed_restores(self):
        """Removing the figure restores the layout and the Yellow view (orientation, link state, view group, layers)."""
        from StudioLib import importer, layouts

        studioShell = self._applyStudio()
        importer.startOnTemplate(shell=studioShell)
        self._pump(6)
        self.layoutManager.setLayout(layouts.FOCUS_LAYOUT_ID)
        self._pump(4)
        yellow = self._sliceNode("Yellow")
        yellow.SetOrientationToSagittal()
        self._composite("Yellow").SetLinkedControl(True)
        self._pump()
        offsetBefore = yellow.GetSliceOffset()
        backgroundBefore = self._background("Yellow")
        figure = self._routeFigure(studioShell, fromLibrary=False)
        self.assertEqual(self.layoutManager.layout, importer.COMPARE_LAYOUT_ID)
        self.assertEqual(self._background("Red").GetID(), backgroundBefore.GetID())
        slicer.mrmlScene.RemoveNode(figure)
        self._pump(6)
        self.assertEqual(self.layoutManager.layout, layouts.FOCUS_LAYOUT_ID)
        self.assertEqual(yellow.GetOrientation(), "Sagittal")
        self.assertAlmostEqual(yellow.GetSliceOffset(), offsetBefore, places=3)
        self.assertTrue(self._composite("Yellow").GetLinkedControl())
        self.assertEqual(yellow.GetViewGroup(), 0)
        self.assertEqual(self._background("Yellow").GetID(), backgroundBefore.GetID())
        self.assertIsNone(importer.figureController())
        self.assertNotEqual(studioShell.inspectorNoticeKey(), "figure-convention")
        # The guard is gone: the wheel scrolls Yellow again
        self._wheel("Yellow", True)
        self.assertNotAlmostEqual(yellow.GetSliceOffset(), offsetBefore, places=3)
        # The same when Yellow is given another volume by the user
        self._composite("Yellow").SetLinkedControl(False)
        replaced = self._routeFigure(studioShell, fromLibrary=False)
        self._composite("Yellow").SetBackgroundVolumeID(backgroundBefore.GetID())
        self._pump(6)
        self.assertEqual(self.layoutManager.layout, layouts.FOCUS_LAYOUT_ID)
        self.assertEqual(yellow.GetOrientation(), "Sagittal")
        slicer.mrmlScene.RemoveNode(replaced)
        self._pump()
        # A scene saved with a figure: guarded again when it is opened, and restored when the figure leaves
        self._routeFigure(studioShell, fromLibrary=False)
        scenePath = os.path.join(self.directory, "figure.mrb")
        self.assertTrue(slicer.util.saveScene(scenePath))
        # And when the scene is closed
        slicer.mrmlScene.Clear(0)
        self._pump(6)
        self.assertEqual(self.layoutManager.layout, layouts.FOCUS_LAYOUT_ID)
        self.assertEqual(yellow.GetViewGroup(), 0)
        importer.loadFiles([scenePath], studioShell)
        self._pump(8)
        self.assertEqual(self.layoutManager.layout, importer.COMPARE_LAYOUT_ID)
        controller = importer.figureController()
        self.assertIsNotNone(controller)
        self.assertTrue(controller._guardTags)
        self.assertEqual(studioShell.inspectorNoticeKey(), "figure-convention")
        self._wheel("Yellow", True)
        self.assertAlmostEqual(self._sliceNode("Yellow").GetSliceOffset(), 0.0, places=3)
        slicer.mrmlScene.RemoveNode(controller.figure())
        self._pump(6)
        self.assertEqual(self.layoutManager.layout, layouts.FOCUS_LAYOUT_ID)
        self.assertEqual(yellow.GetOrientation(), "Sagittal")
        self.assertEqual(yellow.GetViewGroup(), 0)

    #
    # Statistical maps
    #

    def test_statmap_overlay(self):
        """A T map: a two-sided thresholded overlay with a colour legend on the template, one threshold control, no segmentation."""
        from StudioLib import importer

        studioShell = self._applyStudio()
        dims = GRID_2009C_2MM[0]
        z, y, x = np.mgrid[0:dims[2], 0:dims[1], 0:dims[0]]
        values = (8.0 * np.exp(-((x - 30) ** 2 + (y - 60) ** 2 + (z - 50) ** 2) / 60.0) - 6.0 * np.exp(-((x - 66) ** 2 + (y - 60) ** 2 + (z - 50) ** 2) / 60.0)).astype(np.float32)
        node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode")
        slicer.util.updateVolumeFromArray(node, values)
        node.SetIJKToRASMatrix(self._ijkToRas(GRID_2009C_2MM))
        path = os.path.join(self.directory, "sub-05_desc-T_funcmap.nii.gz")
        self.assertTrue(slicer.util.saveNode(node, path))
        slicer.mrmlScene.RemoveNode(node)
        studioShell.switchTo("library")
        self._pump()
        segmentationsBefore = 0
        result = importer.handleDrop([path], shell=studioShell)
        self._pump(8)
        self.assertEqual(len(result.statmaps), 1)
        statmap = result.statmaps[0]
        self.assertEqual(statmap.GetAttribute("Studio.Role"), "statmap")
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        for viewName in ("Red",):
            self.assertEqual(self._composite(viewName).GetForegroundVolumeID(), statmap.GetID())
            self.assertAlmostEqual(self._composite(viewName).GetForegroundOpacity(), importer.STATMAP_OPACITY)
            self.assertEqual(self._background(viewName).GetAttribute("Studio.Role"), "template")
        overlay = importer.statMapOverlay(statmap)
        self.assertIsNotNone(overlay)
        window = float(np.percentile(np.abs(values), importer.STATMAP_WINDOW_PERCENTILE))
        self.assertAlmostEqual(overlay.window, window, places=3)
        displayNode = statmap.GetDisplayNode()
        self.assertAlmostEqual(displayNode.GetWindow(), 2 * window, places=3)
        self.assertAlmostEqual(displayNode.GetLevel(), 0.0, places=3)
        colorNode = displayNode.GetColorNode()
        self.assertTrue(colorNode.GetName().startswith(importer.STATMAP_COLOR_NODE_NAME))
        self.assertEqual(colorNode.GetNumberOfColors(), 256)
        threshold = overlay.threshold

        def alphas():
            result = []
            for index in range(256):
                color = [0.0, 0.0, 0.0, 0.0]
                colorNode.GetColor(index, color)
                value = -window + 2 * window * index / 255.0
                result.append((value, color[3]))
            return result

        for value, alpha in alphas():
            self.assertEqual(alpha, 0.0 if abs(value) < threshold else 1.0, value)
        # The colour legend is shown
        legend = slicer.modules.colors.logic().GetColorLegendDisplayNode(statmap)
        self.assertIsNotNone(legend)
        self.assertTrue(legend.GetVisibility())
        # No segmentation is bound to the map, and the map is never the source
        for segmentation in slicer.util.getNodesByClass("vtkMRMLSegmentationNode"):
            role = slicer.vtkMRMLSegmentationNode.GetReferenceImageGeometryReferenceRole()
            self.assertNotEqual(segmentation.GetNodeReferenceID(role), statmap.GetID())
        self.assertNotEqual(self._editor().sourceVolumeNode().GetID(), statmap.GetID())
        self.assertGreaterEqual(len(slicer.util.getNodesByClass("vtkMRMLSegmentationNode")), segmentationsBefore)
        # The notice and its control: a new threshold rebuilds the alpha
        self.assertEqual(studioShell.inspectorNoticeKey(), "statmap")
        spinBox = studioShell.inspectorNotice().findChild("QDoubleSpinBox", "StudioStatMapThresholdSpinBox")
        self.assertIsNotNone(spinBox)
        spinBox.setValue(4.0)
        self._pump()
        for value, alpha in alphas():
            self.assertEqual(alpha, 0.0 if abs(value) < 4.0 else 1.0, value)
        self.assertIn("|value| <", studioShell.recentMessages()[-1][1] if studioShell.recentMessages() else "")
        # Remove overlay
        self._noticeButton(studioShell, "Remove overlay").click()
        self._pump(4)
        self.assertFalse(slicer.mrmlScene.IsNodePresent(statmap))
        self.assertFalse(self._composite("Red").GetForegroundVolumeID())
        self.assertNotEqual(studioShell.inspectorNoticeKey(), "statmap")

    #
    # Lesions without a scan
    #

    def test_lesion_only_mni2009(self):
        """A lesion on the bundled template grid alone: the template is loaded and bound as the source, no space line."""
        from StudioLib import importer

        studioShell = self._applyStudio()
        path = self._writeMask("sub-01_lesion.nii.gz", GRID_2009C_2MM)
        result = importer.loadFiles([path], studioShell)
        self._pump(8)
        lesion = result.segmentations[0]
        editor = self._editor()
        self.assertEqual(editor.segmentationNode().GetID(), lesion.GetID())
        source = editor.sourceVolumeNode()
        self.assertIsNotNone(source)
        self.assertEqual(source.GetAttribute("Studio.Role"), "template")
        self.assertEqual(self._background("Red").GetID(), source.GetID())
        self.assertIsNone(importer.lesionSpaceLine(lesion))
        self.assertIsNone(importer.noticeWidget("lesion-space"))
        self.assertTrue(lesion.GetAttribute("Studio.LesionGrid"))
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        self.assertEqual(self._voxels(lesion), 27)

    def test_lesion_only_nlin6(self):
        """A 91x109x91 NLin6Asym mask: bound to the 2009c template, its grid unchanged and recorded, the amber space line."""
        from StudioLib import importer, space

        studioShell = self._applyStudio()
        path = self._writeMask("sub-02_lesion.nii.gz", GRID_NLIN6_2MM)
        result = importer.loadFiles([path], studioShell)
        self._pump(8)
        lesion = result.segmentations[0]
        geometryName = slicer.vtkSegmentationConverter.GetReferenceImageGeometryParameterName()
        recorded = lesion.GetAttribute("Studio.LesionGrid")
        self.assertTrue(recorded)
        editor = self._editor()
        self.assertEqual(editor.segmentationNode().GetID(), lesion.GetID())
        self.assertEqual(editor.sourceVolumeNode().GetAttribute("Studio.Role"), "template")
        self.assertEqual(lesion.GetSegmentation().GetConversionParameter(geometryName), recorded)
        self.assertEqual(space.spaceOf(lesion), ("MNI152NLin6Asym", 2))
        line = importer.lesionSpaceLine(lesion)
        self.assertEqual(line, "Lesion grid MNI152NLin6Asym 2 mm, shown on MNI152NLin2009cAsym (a few mm apart)")
        self.assertEqual(studioShell.inspectorNoticeKey(), "lesion-space")
        self.assertEqual(str(studioShell.inspectorNotice().findChild("QLabel", "StudioLesionSpaceNoticeText").text), line)
        # Painting through the editor keeps the lesion grid
        editor.setActiveEffectByName("Paint")
        self._pump()
        self._stroke("Red")
        self.assertEqual(lesion.GetSegmentation().GetConversionParameter(geometryName), recorded)

    def test_lesion_only_native(self):
        """A lesion in scan space alone is never shown on the template; its sidecar's scan is offered and opens with it."""
        from StudioLib import importer

        studioShell = self._applyStudio()
        path = self._writeMask("sub-03_lesion.nii.gz", GRID_SHIFTED, subdirectory="lesions")
        result = importer.loadFiles([path], studioShell)
        self._pump(8)
        lesion = result.segmentations[0]
        editor = self._editor()
        self.assertEqual(editor.segmentationNode().GetID(), lesion.GetID())
        self.assertIsNone(editor.sourceVolumeNode())
        self.assertFalse([node for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode") if importer._isTemplate(node)])
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        self.assertEqual(studioShell.inspectorNoticeKey(), "native-lesion")
        text = str(studioShell.inspectorNotice().findChild("QLabel", "StudioNativeLesionNoticeText").text)
        self.assertEqual(text, "This lesion is in scan space. Open the scan it was drawn on.")
        self.assertTrue(lesion.GetAttribute("Studio.LesionGrid"))

        # The template is open: the native lesion is still not bound to it (and is hidden over it)
        importer.startOnTemplate(shell=studioShell)
        self._pump(6)
        slicer.mrmlScene.RemoveNode(lesion)
        result = importer.loadFiles([path], studioShell)
        self._pump(8)
        lesion = result.segmentations[0]
        self.assertEqual(editor.segmentationNode().GetID(), lesion.GetID())
        self.assertIsNone(editor.sourceVolumeNode())
        self.assertFalse(lesion.GetDisplayNode().GetVisibility())

        # With a sidecar naming the scan (relative path): "Open with patient_T1.nii.gz" opens the scan with the lesion
        scanPath = self._writeVolume("patient_T1.nii.gz", GRID_SHIFTED, subdirectory="anat")
        with open(importer.sidecarPath(path), "w") as f:
            json.dump({"Source": "../anat/patient_T1.nii.gz", "Space": "scanner"}, f)
        self.assertEqual(os.path.normpath(importer.sidecarSource(path)), os.path.normpath(scanPath))
        # Study lesion sidecars (BIDS keys): "Sources" (a list, BIDS URIs skipped) and "SpatialReference"
        for sidecar in ({"Sources": ["bids::sub-03/anat/sub-03_T1w.nii.gz", "../anat/patient_T1.nii.gz"]},
                        {"SpatialReference": "../anat/patient_T1.nii.gz"}):
            with open(importer.sidecarPath(path), "w") as f:
                json.dump(sidecar, f)
            self.assertEqual(os.path.normpath(importer.sidecarSource(path)), os.path.normpath(scanPath), sidecar)
        with open(importer.sidecarPath(path), "w") as f:
            json.dump({"Source": "../anat/patient_T1.nii.gz", "Space": "scanner"}, f)
        slicer.mrmlScene.RemoveNode(lesion)
        result = importer.loadFiles([path], studioShell)
        self._pump(8)
        lesion = result.segmentations[0]
        button = self._noticeButton(studioShell, "Open with patient_T1.nii.gz")
        nodesBefore = len(slicer.util.getNodesByClass("vtkMRMLSegmentationNode"))
        button.click()
        self._pump(10)
        self.assertEqual(editor.sourceVolumeNode().GetName(), "patient_T1")
        self.assertEqual(editor.segmentationNode().GetID(), lesion.GetID())
        # On the scan's grid: bound to the scan (as if drawn on it)
        self.assertTrue(importer._isBoundTo(lesion, editor.sourceVolumeNode()))
        self.assertEqual(int(np.count_nonzero(slicer.util.arrayFromSegmentBinaryLabelmap(lesion, editor.currentSegmentID()))), 27)
        self.assertTrue(lesion.GetDisplayNode().GetVisibility())
        self.assertEqual(len(slicer.util.getNodesByClass("vtkMRMLSegmentationNode")), nodesBefore)
        self.assertNotEqual(studioShell.inspectorNoticeKey(), "native-lesion")
        self.assertEqual(self._voxels(lesion), 27)

        # "Show on the MNI template anyway": bound on request, with the amber line
        slicer.mrmlScene.Clear(0)
        result = importer.loadFiles([path], studioShell)
        self._pump(8)
        lesion = result.segmentations[0]
        self._noticeButton(studioShell, "Show on the MNI template anyway").click()
        self._pump(8)
        self.assertEqual(editor.sourceVolumeNode().GetAttribute("Studio.Role"), "template")
        self.assertEqual(importer.lesionSpaceLine(lesion), "Shown on the MNI template; the lesion is in scan space")

    def test_loaded_set(self):
        """Six masks loaded at once: one visible, next() changes the visible lesion with no new node, close() removes exactly the set."""
        from StudioLib import importer

        studioShell = self._applyStudio()
        paths = [self._writeMask(f"sub-{index:02d}_lesion.nii.gz", GRID_NLIN6_2MM, center=(30 + 4 * index, 50, 40), subdirectory="set") for index in range(1, 7)]
        scene = slicer.mrmlScene
        nodesBefore = scene.GetNumberOfNodes()
        importer.loadFiles(paths, studioShell)
        self._pump(8)
        loaded = importer.loadedSet()
        self.assertIsNotNone(loaded)
        nodes = loaded.nodes()
        self.assertEqual([node.GetName() for node in nodes], [f"sub-{index:02d} lesion" for index in range(1, 7)])
        self.assertEqual(nodes[0].GetSegmentation().GetNthSegment(0).GetName(), "sub-01 lesion")
        visible = lambda: [node.GetName() for node in nodes if node.GetDisplayNode().GetVisibility()]
        self.assertEqual(visible(), ["sub-01 lesion"])
        self.assertEqual(self._editor().segmentationNode().GetID(), nodes[0].GetID())
        self.assertEqual(self._editor().sourceVolumeNode().GetAttribute("Studio.Role"), "template")
        breadcrumb = str(studioShell.topBar.breadcrumbButton.text)
        self.assertIn("sub-01", breadcrumb)
        self.assertIn("1 of 6", breadcrumb)
        count = scene.GetNumberOfNodes()
        loaded.next()
        self._pump(4)
        self.assertEqual(scene.GetNumberOfNodes(), count)
        self.assertEqual(visible(), ["sub-02 lesion"])
        self.assertEqual(self._editor().segmentationNode().GetID(), nodes[1].GetID())
        self.assertIn("2 of 6", str(studioShell.topBar.breadcrumbButton.text))
        loaded.previous()
        self.assertIsNone(loaded.previous())
        self.assertEqual(studioShell.toastText(), "First lesion of the set")
        loaded.select(5)
        self.assertIsNone(loaded.next())
        self.assertEqual(visible(), ["sub-06 lesion"])
        # Close removes exactly the set: the template stays
        template = self._editor().sourceVolumeNode()
        loaded.close()
        self._pump(4)
        self.assertIsNone(importer.loadedSet())
        self.assertFalse(slicer.util.getNodesByClass("vtkMRMLSegmentationNode"))
        self.assertTrue(scene.IsNodePresent(template))
        self.assertFalse(list(slicer.util.getNodesByClass("vtkMRMLSegmentationDisplayNode")))
        self.assertFalse(list(slicer.util.getNodesByClass("vtkMRMLSegmentationStorageNode")))
        self.assertGreaterEqual(scene.GetNumberOfNodes(), nodesBefore)

    #
    # Scans from the Sample Data module, the 1 mm offer, re-opening, new lesions
    #

    def test_sampledata_scan_to_draw(self):
        """A scan loaded while the Sample Data module is shown opens in Draw with a ready "Lesion" segment."""
        from StudioLib import importer

        studioShell = self._applyStudio()
        path = self._writeVolume("MRHead.nrrd", GRID_SHIFTED)
        slicer.util.selectModule("SampleData")
        self._pump(4)
        self.assertEqual(studioShell.currentModuleName(), "SampleData")
        self.assertIsNone(studioShell.currentWorkspace())
        node = slicer.util.loadVolume(path)
        self._wait(importer.TRANSIENT_LOAD_WINDOW_MS + 300)
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        editor = self._editor()
        self.assertEqual(editor.sourceVolumeNode().GetID(), node.GetID())
        segmentation = editor.segmentationNode()
        self.assertEqual(segmentation.GetSegmentation().GetSegment(editor.currentSegmentID()).GetName(), importer.LESION_SEGMENT_NAME)
        # Two volumes within the window (a multi-volume sample): left where they are
        slicer.util.selectModule("SampleData")
        self._pump(4)
        slicer.util.loadVolume(path)
        slicer.util.loadVolume(path)
        self._wait(importer.TRANSIENT_LOAD_WINDOW_MS + 300)
        self.assertEqual(studioShell.currentModuleName(), "SampleData")

    def test_no_banner_over_views(self):
        """Start on template shows no card over the views; after a stroke the 1 mm offer is a notice line; Not now is kept."""
        from StudioLib import importer

        if importer.cachedTemplatePath(importer.TEMPLATE_SAMPLE_1MM) is not None:
            self.skipTest("the 1 mm template is cached")
        qt.QSettings().remove(importer.TEMPLATE_1MM_OFFER_KEY)
        studioShell = self._applyStudio()
        volume, segmentation = importer.startOnTemplate(shell=studioShell)
        self._pump(6)
        centralWidget = self.mainWindow.centralWidget()
        self.assertFalse(list(centralWidget.findChildren("QFrame", "StudioDownloadBanner")))
        self.assertIsNone(importer.templateOfferWidget())
        # The first stroke: the offer appears in the notice slot (inspector), never over the views
        segmentId = segmentation.GetSegmentation().GetNthSegmentID(0)
        array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentation, segmentId, volume)
        array[40:42, 50:52, 45:47] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentation, segmentId, volume)
        self._wait(600)
        offer = importer.templateOfferWidget()
        self.assertIsNotNone(offer)
        self.assertEqual(studioShell.inspectorNoticeKey(), "template1mm")
        self.assertIs(studioShell.inspectorNotice(), offer)
        self.assertTrue(offer.isVisible())
        self.assertFalse(centralWidget.isAncestorOf(offer))
        self.assertFalse(list(centralWidget.findChildren("QFrame", "StudioDownloadBanner")))
        self.assertIn("1 mm", str(offer.findChild("QLabel", "StudioNoticeText").text))
        # A notice of higher priority is not replaced by the offer: it waits
        figure = self._routeFigure(studioShell, fromLibrary=False)
        self.assertEqual(studioShell.inspectorNoticeKey(), "figure-convention")
        importer.answerFigureConvention("unknown", figure.GetID())
        self._pump()
        self.assertEqual(studioShell.inspectorNoticeKey(), "template1mm")
        # Not now: remembered, and the offer is gone
        notNow = offer.findChild("QToolButton", "StudioDownloadBannerSecondaryButton")
        self.assertEqual(str(notNow.text), "Not now")
        notNow.click()
        self._pump(4)
        self.assertEqual(slicer.util.settingsValue(importer.TEMPLATE_1MM_OFFER_KEY, ""), "dismissed")
        self.assertIsNone(importer.templateOfferWidget())
        self.assertNotEqual(studioShell.inspectorNoticeKey(), "template1mm")
        # Not offered again (a new template lesion, a new stroke)
        slicer.mrmlScene.Clear(0)
        volume, segmentation = importer.startOnTemplate(shell=studioShell)
        array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentation, segmentation.GetSegmentation().GetNthSegmentID(0), volume)
        array[40:42, 50:52, 45:47] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentation, segmentation.GetSegmentation().GetNthSegmentID(0), volume)
        self._wait(600)
        self.assertIsNone(importer.templateOfferWidget())
        # The compatibility API: the offer as a line of the notice slot
        banner = importer.showTemplateDownloadBanner()
        self.assertTrue(banner.embedded)
        self.assertIs(studioShell.inspectorNotice(), banner.widget)
        importer._closeTemplateBanner()
        self._pump()
        self.assertIsNone(studioShell.inspectorNotice())

    def test_reopen_keeps_lesion(self):
        """Re-opening a scan with a traced lesion (Recent, Open…) keeps the same segmentation and adds no node."""
        from StudioLib import importer

        studioShell = self._applyStudio()
        path = self._writeVolume("sub-07_T1w.nii.gz", GRID_SHIFTED)
        importer.loadFiles([path], studioShell)
        self._pump(8)
        editor = self._editor()
        lesion = editor.segmentationNode()
        segmentId = editor.currentSegmentID()
        array = slicer.util.arrayFromSegmentBinaryLabelmap(lesion, segmentId, editor.sourceVolumeNode())
        array[40:44, 50:54, 45:49] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, lesion, segmentId, editor.sourceVolumeNode())
        voxels = self._voxels(lesion)
        count = slicer.mrmlScene.GetNumberOfNodes()
        studioShell.switchTo("library")
        self._pump()
        importer.loadRecentFile({"fileName": path, "fileType": "VolumeFile"}, None, studioShell)
        self._pump(6)
        self.assertEqual(slicer.mrmlScene.GetNumberOfNodes(), count)
        self.assertEqual(editor.segmentationNode().GetID(), lesion.GetID())
        self.assertEqual(self._voxels(lesion), voxels)
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        importer.loadFiles([path], studioShell)
        self._pump(6)
        self.assertEqual(slicer.mrmlScene.GetNumberOfNodes(), count)
        self.assertEqual(editor.segmentationNode().GetID(), lesion.GetID())

    def test_reopen_reference_from_recent(self):
        """A figure or a T map re-opened from Recent (or loaded by a route of 3D Slicer) takes its own route: never the
        Draw source, no segmentation bound to it; a file that is already open is shown again, not loaded twice.
        """
        from StudioLib import importer, space

        studioShell = self._applyStudio()
        role = slicer.vtkMRMLSegmentationNode.GetReferenceImageGeometryReferenceRole()

        def boundTo(volume):
            return [node.GetName() for node in slicer.util.getNodesByClass("vtkMRMLSegmentationNode") if node.GetNodeReferenceID(role) == volume.GetID()]

        # Figure: removed, then re-opened from Recent
        figure = self._routeFigure(studioShell)
        path = figure.GetStorageNode().GetFileName()
        importer.answerFigureConvention("neurological")
        slicer.mrmlScene.RemoveNode(figure)
        self._pump(8)
        studioShell.switchTo("library")
        self._pump()
        result = importer.loadRecentFile({"fileName": path, "fileType": "VolumeFile", "singleFile": True}, None, studioShell)
        self._pump(10)
        self.assertEqual(len(result.figures), 1)
        figure = result.figures[0]
        self.assertTrue(space.isFigure(figure))
        self.assertEqual(self._background("Yellow").GetID(), figure.GetID())
        self.assertEqual(self.layoutManager.layout, importer.COMPARE_LAYOUT_ID)
        self.assertEqual(self._editor().sourceVolumeNode().GetAttribute("Studio.Role"), "template")
        self.assertEqual(boundTo(figure), [])
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        # The same figure again (drop, Recent): not loaded twice, the question is not asked again for a shown figure
        importer.answerFigureConvention("neurological")
        count = slicer.mrmlScene.GetNumberOfNodes()
        again = importer.handleDrop([path], shell=studioShell)
        self._pump(6)
        self.assertEqual(slicer.mrmlScene.GetNumberOfNodes(), count)
        self.assertEqual([node.GetID() for node in again.alreadyOpen], [figure.GetID()])
        self.assertEqual(studioShell.toastText(), f"{figure.GetName()} is already open")
        self.assertNotEqual(studioShell.inspectorNoticeKey(), "figure-convention")
        self.assertGreater(self._sliceNode("Yellow").GetSliceToRAS().GetElement(0, 0), 0, "a neurological figure stays mirrored")
        importer.loadRecentFile({"fileName": path, "fileType": "VolumeFile", "singleFile": True}, None, studioShell)
        self._pump(6)
        self.assertEqual(slicer.mrmlScene.GetNumberOfNodes(), count)

        # Safety net: a picture loaded by a route of 3D Slicer (not tagged) is routed as a figure, never a scan
        slicer.mrmlScene.Clear(0)
        self._pump(6)
        picture = slicer.util.loadVolume(self._writeFigure("other_fig.png"), {"singleFile": True, "show": False})
        self.assertIsNone(picture.GetAttribute("Studio.Role"))
        loadResult = importer.LoadResult()
        loadResult.volumes.append(picture)
        importer._afterLoad(loadResult, studioShell)
        self._pump(8)
        self.assertTrue(space.isFigure(picture))
        self.assertEqual(self._background("Yellow").GetID(), picture.GetID())
        self.assertEqual(self._editor().sourceVolumeNode().GetAttribute("Studio.Role"), "template")
        self.assertEqual(boundTo(picture), [])
        # selectInSegmentEditor never takes a picture as the source, even untagged
        picture.RemoveAttribute("Studio.Role")
        importer.selectInSegmentEditor(self._editor().segmentationNode(), picture)
        self.assertNotEqual(self._editor().sourceVolumeNode().GetID(), picture.GetID())
        picture.SetAttribute("Studio.Role", "figure")

        # T map: overlay removed, then re-opened from Recent
        slicer.mrmlScene.Clear(0)
        self._pump(6)
        mapPath = self._writeTMap()
        studioShell.switchTo("library")
        self._pump()
        result = importer.handleDrop([mapPath], shell=studioShell)
        self._pump(8)
        importer.statMapOverlay(result.statmaps[0]).remove()
        self._pump(6)
        studioShell.switchTo("library")
        self._pump()
        result = importer.loadRecentFile({"fileName": mapPath, "fileType": "VolumeFile", "singleFile": True}, None, studioShell)
        self._pump(10)
        self.assertEqual(len(result.statmaps), 1)
        mapNode = result.statmaps[0]
        self.assertEqual(mapNode.GetAttribute("Studio.Role"), "statmap")
        self.assertEqual(self._composite("Red").GetForegroundVolumeID(), mapNode.GetID())
        self.assertEqual(studioShell.inspectorNoticeKey(), "statmap")
        self.assertNotEqual(self._editor().sourceVolumeNode().GetID(), mapNode.GetID())
        self.assertEqual(boundTo(mapNode), [])
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        # Already open: its overlay is shown again, nothing is loaded
        self._composite("Red").SetForegroundVolumeID(None)
        count = slicer.mrmlScene.GetNumberOfNodes()
        importer.loadRecentFile({"fileName": mapPath, "fileType": "VolumeFile", "singleFile": True}, None, studioShell)
        self._pump(6)
        self.assertEqual(slicer.mrmlScene.GetNumberOfNodes(), count)
        self.assertEqual(self._composite("Red").GetForegroundVolumeID(), mapNode.GetID())
        self.assertEqual(studioShell.toastText(), f"{mapNode.GetName()} is already open")

    def test_loaded_set_follows_editor(self):
        """The loaded set follows the edited lesion: another member updates the breadcrumb, a scan of the user ends the set
        (breadcrumb cleared, the set's MNI masks hidden over the scan), and so does closing the scene.
        """
        from StudioLib import importer

        studioShell = self._applyStudio()
        paths = [self._writeMask(f"sub-{index:02d}_lesion.nii.gz", GRID_NLIN6_2MM, center=(30 + 4 * index, 50, 40), subdirectory="set") for index in range(1, 4)]
        importer.loadFiles(paths, studioShell)
        self._pump(8)
        loaded = importer.loadedSet()
        nodes = loaded.nodes()
        # Another lesion of the set chosen in the Segment Editor
        self._editor().setSegmentationNode(nodes[2])
        self._pump()
        self.assertEqual(loaded.index, 2)
        self.assertIn("3 of 3", str(studioShell.topBar.breadcrumbButton.text))
        self.assertEqual([node.GetDisplayNode().GetVisibility() for node in nodes], [0, 0, 1])
        # A scan of the user: the set ends
        scan = self._writeVolume("sub-09_T1w.nii.gz", GRID_SHIFTED)
        importer.loadFiles([scan], studioShell)
        self._pump(8)
        self.assertIsNone(importer.loadedSet())
        self.assertNotIn("of 3", str(studioShell.topBar.breadcrumbButton.text))
        self.assertEqual(self._editor().sourceVolumeNode().GetName(), "sub-09_T1w")
        self.assertEqual([node.GetDisplayNode().GetVisibility() for node in nodes], [0, 0, 0])
        # A new set, then the scene is closed: no stale set
        slicer.mrmlScene.Clear(0)
        importer.loadFiles(paths, studioShell)
        self._pump(8)
        self.assertIsNotNone(importer.loadedSet())
        slicer.mrmlScene.Clear(0)
        self._pump()
        self.assertIsNone(importer.loadedSet())
        self.assertNotIn("of 3", str(studioShell.topBar.breadcrumbButton.text))

    def test_figure_classic_untouched(self):
        """In Classic, the figure controller leaves the views alone: Link views stays linked, a new Yellow volume stays."""
        from StudioLib import importer

        studioShell = self._applyStudio()
        self._routeFigure(studioShell)
        importer.answerFigureConvention("unknown")
        studioShell.classic(persist=False)
        self._pump(6)
        for compositeNode in slicer.util.getNodesByClass("vtkMRMLSliceCompositeNode"):
            compositeNode.SetLinkedControl(True)
        self._pump(6)
        self.assertTrue(self._composite("Yellow").GetLinkedControl())
        self._composite("Yellow").SetBackgroundVolumeID(self._composite("Red").GetBackgroundVolumeID())
        self._pump(6)
        self.assertEqual(self.layoutManager.layout, importer.COMPARE_LAYOUT_ID)
        self.assertEqual(self._background("Yellow").GetID(), self._background("Red").GetID())
        # Back in Studio: the Compare layout shows a figure in Yellow again (guarded), or the controller let it go
        studioShell.studio()
        self._pump(6)
        controller = importer.figureController()
        if controller is not None:
            self.assertEqual(self._background("Yellow").GetID(), controller.figure().GetID())
            self.assertFalse(self._composite("Yellow").GetLinkedControl())

    def test_sampledata_template(self):
        """The MNI template loaded from the Sample Data module is the template of "Start on MNI template"."""
        from StudioLib import importer

        studioShell = self._applyStudio()
        slicer.util.selectModule("SampleData")
        self._pump(4)
        import SampleData

        volume = SampleData.SampleDataLogic().downloadSample(importer.TEMPLATE_SAMPLE_2MM)
        self._wait(importer.TRANSIENT_LOAD_WINDOW_MS + 400)
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        self.assertEqual(volume.GetAttribute("Studio.Role"), "template")
        editor = self._editor()
        self.assertEqual(editor.sourceVolumeNode().GetID(), volume.GetID())
        self.assertEqual(editor.segmentationNode().GetName(), importer.LESION_SEGMENTATION_NAME)
        _volume, segmentation = importer.startOnTemplate(shell=studioShell)
        self.assertEqual(segmentation.GetID(), editor.segmentationNode().GetID())

    def test_new_lesion_explicit(self):
        """newLesionSegmentation(): "Lesion 2" on the source grid, the previous lesion hidden, Undo restores it."""
        from StudioLib import importer

        studioShell = self._applyStudio()
        volume, first = importer.startOnTemplate(shell=studioShell)
        self._pump(6)
        count = slicer.mrmlScene.GetNumberOfNodes()
        second = importer.newLesionSegmentation(shell=studioShell)
        self._pump()
        self.assertEqual(second.GetName(), "Lesion 2")
        self.assertEqual(second.GetSegmentation().GetNthSegment(0).GetName(), "Lesion 2")
        self.assertTrue(importer._isBoundTo(second, volume))
        self.assertFalse(first.GetDisplayNode().GetVisibility2D())
        self.assertEqual(self._editor().segmentationNode().GetID(), second.GetID())
        self.assertEqual(self._editor().sourceVolumeNode().GetID(), volume.GetID())
        self.assertIn("Lesion 2", studioShell.toastText())
        third = importer.newLesionSegmentation(shell=studioShell)
        self.assertEqual(third.GetName(), "Lesion 3")
        # Undo (the toast's action): the new lesion is removed, the previous one is shown and edited again
        undoButton = self.mainWindow.findChild("QToolButton", "StudioStickyToastAction")
        self.assertIsNotNone(undoButton)
        self.assertEqual(str(undoButton.text), "Undo")
        undoButton.click()
        self._pump(4)
        self.assertFalse(slicer.mrmlScene.IsNodePresent(third))
        self.assertTrue(second.GetDisplayNode().GetVisibility2D())
        self.assertEqual(self._editor().segmentationNode().GetID(), second.GetID())
        importer.undoNewLesion(second.GetID(), first.GetID(), volume.GetID(), first.GetID())
        self.assertEqual(slicer.mrmlScene.GetNumberOfNodes(), count)
        self.assertTrue(first.GetDisplayNode().GetVisibility2D())
        # Start on template keeps reusing the template lesion
        _volume, again = importer.startOnTemplate(shell=studioShell)
        self.assertEqual(again.GetID(), first.GetID())

    #
    # Tables and Recent
    #

    def test_tsv_drop_no_add_data(self):
        """A dropped participants.tsv never opens Add Data (a toast says what it holds); Shift+drop still does."""
        from StudioLib import importer

        studioShell = self._applyStudio()
        path = os.path.join(self.directory, "participants.tsv")
        with open(path, "w") as f:
            f.write("participant_id\tgroup\tage\nsub-01\tpatient\t61\nsub-02\tcontrol\t58\n")
        other = os.path.join(self.directory, "notes.csv")
        with open(other, "w") as f:
            f.write("a,b\n1,2\n")
        calls = []
        original = importer.openAddDataDialog
        importer.openAddDataDialog = lambda paths: calls.append(list(paths))
        try:
            result = importer.handleDrop([path], shell=studioShell)
            self._pump()
            self.assertEqual(calls, [])
            self.assertEqual(result.tables, [os.path.abspath(path)])
            self.assertIn("participants.tsv: 2 participants (group, age)", studioShell.toastText())
            importer.handleDrop([path], addData=True, shell=studioShell)
            self.assertEqual(calls, [[path]])
            importer.handleDrop([other], shell=studioShell)
            self.assertEqual(calls[-1], [os.path.abspath(other)])
        finally:
            importer.openAddDataDialog = original

    def test_recent_times_recorded(self):
        """Opened files are recorded in Studio/RecentOpenTimes and folders in Studio/RecentFolders."""
        from StudioLib import importer

        studioShell = self._applyStudio()
        qt.QSettings().remove(importer.RECENT_OPEN_TIMES_KEY)
        qt.QSettings().remove(importer.RECENT_FOLDERS_KEY)
        path = self._writeVolume("sub-08_T1w.nii.gz", GRID_SHIFTED)
        start = time.time()
        importer.loadFiles([path], studioShell)
        self._pump(4)
        times = importer.recentOpenTimes()
        self.assertIn(os.path.abspath(path), times)
        self.assertGreaterEqual(times[os.path.abspath(path)], start - 1)
        # At most 50, the newest
        for index in range(60):
            importer.recordOpenTimes([os.path.join(self.directory, f"f{index}.nii")], when=1000.0 + index)
        times = importer.recentOpenTimes()
        self.assertEqual(len(times), importer.MAX_RECENT_OPEN_TIMES)
        self.assertIn(os.path.abspath(path), times)
        self.assertNotIn(os.path.join(self.directory, "f0.nii"), times)
        # A folder of NIfTI files
        folder = os.path.join(self.directory, "cohort")
        for index in range(2):
            self._writeMask(f"sub-{index:02d}_lesion.nii.gz", GRID_NLIN6_2MM, subdirectory="cohort")
        self.assertEqual(importer.loadFolder(folder, studioShell), "nifti")
        folders = importer.recentFolders()
        self.assertEqual(folders[0]["path"], os.path.abspath(folder))
        self.assertEqual((folders[0]["kind"], folders[0]["count"]), ("nifti", 2))
        for index in range(12):
            importer.recordFolder(os.path.join(self.directory, f"d{index}"), "nifti", 1)
        self.assertEqual(len(importer.recentFolders()), importer.MAX_RECENT_FOLDERS)
        self.assertEqual(importer.recentFolders()[0]["path"], os.path.join(self.directory, "d11"))

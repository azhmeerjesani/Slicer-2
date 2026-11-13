"""Tests of the Studio import routes (U-07): spaces, figures, statistical maps, lesions without a scan, loaded sets,
scans from the Sample Data module, the 1 mm template offer, new lesions, participants tables and Recent bookkeeping.

Run with the main window and testing enabled:

  Slicer --no-splash --testing --python-code "import slicer.testing; slicer.testing.runUnitTest(['<this dir>'], 'StudioImportTest'); slicer.app.exit(0)"

Studio never runs by itself under --testing; the tests apply it with ``Shell(force=True)`` and always restore the
Classic interface. All data is synthetic and written to a temporary folder.
"""

import gzip
import json
import logging
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

    def _writeVolume(self, name, grid, array=None, labelmap=False, subdirectory=None, ijkToRas=None):
        """Write a NIfTI volume on ``grid`` (voxels of ``array``, z-y-x order) and return its path. ``ijkToRas`` (a
        vtkMatrix4x4) replaces the matrix of ``grid`` (then None), for an oblique or non-axial acquisition.
        """
        if array is None:
            dims = grid[0]
            array = np.zeros((dims[2], dims[1], dims[0]), np.uint8 if labelmap else np.int16)
        className = "vtkMRMLLabelMapVolumeNode" if labelmap else "vtkMRMLScalarVolumeNode"
        node = slicer.mrmlScene.AddNewNodeByClass(className, "StudioImportTestVolume")
        slicer.util.updateVolumeFromArray(node, array)
        node.SetIJKToRASMatrix(ijkToRas if ijkToRas is not None else self._ijkToRas(grid))
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

    def _drawEmptyState(self):
        """The Draw empty state (also its variant for a lesion in scan space, W2-05)."""
        widget = self.mainWindow.findChild("QWidget", "StudioDrawEmptyState")
        self.assertIsNotNone(widget)
        return widget

    def _drawEmptyStateButton(self, text):
        for button in self._drawEmptyState().findChildren("QAbstractButton"):
            if str(button.text) == text:
                return button
        self.fail(f"no button {text!r} in the Draw empty state")
        return None

    def _lesionCardSpaceLine(self):
        """The space line of the Draw Lesion card (W2-05)."""
        label = self.mainWindow.findChild("QLabel", "StudioLesionSpaceLine")
        self.assertIsNotNone(label)
        return label

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
        self.assertEqual(importer.fileKind(os.path.join(self.directory, "sub-05_desc-T_mask.nii.gz")), "lesion")
        self.assertEqual(importer.fileKind(os.path.join(self.directory, "sub-01_lesion_funcmap.nrrd")), "lesion")
        # ... but the maps that Map exports are named after their lesion: they open as maps, never as a lesion (a seed)
        from LesionNetworkMappingLib import export

        for lesionName in ("Lesions-Lesion", "sub-01_lesion", "sub_M2001_lesion.nii"):
            path = export.map_paths(self.directory, lesionName, "Synthetic demo")["T"]
            self.assertTrue(importer.hasStatMapSuffix(path), path)
            self.assertEqual(importer.fileKind(path), "statmap", path)
            with open(plain, "rb") as source, gzip.open(path, "wb") as f:
                f.write(source.read())
            self.assertEqual(importer.fileKind(path), "statmap", path)
        self.assertEqual(importer.fileKind(os.path.join(self.directory, "roi_statmap.nii")), "statmap")
        self.assertEqual(importer.fileKind(os.path.join(self.directory, "fig.PNG")), "figure")
        self.assertEqual(importer.fileKind(os.path.join(self.directory, "participants.tsv")), "table")

    def _write4D(self, name, timePoints=3, dtype=np.int16):
        """Write a 4D NIfTI image (16 x 12 x 8 voxels, ``timePoints`` time points) and return its path."""
        import SimpleITK as sitk

        rng = np.random.default_rng(4)
        frames = []
        for _index in range(timePoints):
            frame = sitk.GetImageFromArray((rng.random((8, 12, 16)) * 100).astype(dtype))
            frame.SetSpacing((2.0, 2.0, 3.0))
            frames.append(frame)
        image = sitk.JoinSeries(frames)
        image.SetSpacing((2.0, 2.0, 3.0, 2.0))
        path = os.path.join(self.directory, name)
        sitk.WriteImage(image, path)
        return path

    def test_4d_nifti_opens_as_sequence(self):
        """A 4D NIfTI image (fMRI, a 4D mask) is opened as a sequence in Classic and Studio, never refused by the volume reader."""
        from StudioLib import importer

        bold = self._write4D("task-rest_bold.nii.gz", timePoints=5, dtype=np.float32)
        mask = self._write4D("sub-21_lesion_mask4d.nii.gz", timePoints=2, dtype=np.uint8)
        plain = self._writeVolume("sub-21_T1w.nii.gz", GRID_2009C_2MM)

        def sequenceNodes():
            return [node for node in slicer.util.getNodesByClass("vtkMRMLSequenceNode") if node.GetName() == "task-rest_bold"]

        # Classic: loaded as a sequence (as Add Data does) and shown in the slice views
        result = importer.loadFiles([bold])
        self._pump()
        self.assertEqual(result.errors, [])
        self.assertEqual(result.addDataPaths, [])
        self.assertEqual(len(sequenceNodes()), 1)
        self.assertEqual(sequenceNodes()[0].GetNumberOfDataNodes(), 5)
        self.assertEqual(len(result.volumes), 1)
        self.assertTrue(result.volumes[0].IsA("vtkMRMLScalarVolumeNode"))
        self.assertEqual(importer._backgroundVolume().GetID(), result.volumes[0].GetID())
        slicer.mrmlScene.Clear(0)

        # Studio: Open... and drop load it as the scan of Draw; a second drop does not load it again
        studioShell = self._applyStudio()
        result = importer.loadFiles([bold], studioShell)
        self._pump()
        self.assertEqual(result.errors, [])
        proxy = result.volumes[0]
        self.assertEqual(importer._backgroundVolume().GetID(), proxy.GetID())
        self.assertEqual(importer.openNodeForFile(bold).GetID(), proxy.GetID())
        result = importer.handleDrop([bold], shell=studioShell)
        self._pump()
        self.assertEqual(result.errors, [])
        self.assertEqual([node.GetID() for node in result.alreadyOpen], [proxy.GetID()])
        self.assertEqual(len(sequenceNodes()), 1)
        # A lesion-named 4D mask cannot be a lesion segmentation: it opens as a sequence instead of failing
        result = importer.handleDrop([mask], shell=studioShell)
        self._pump()
        self.assertEqual(result.errors, [])
        self.assertEqual(result.segmentations, [])
        self.assertEqual(len(result.volumes), 1)
        # Detection: the NIfTI header (dim[4] > 1), whatever the name
        self.assertEqual(importer.niftiDimensions(bold)[:5], [4, 16, 12, 8, 5])
        self.assertTrue(importer.isNifti4D(bold))
        self.assertFalse(importer.isNifti4D(plain))
        self.assertEqual(importer.fileKind(bold), "sequence")
        self.assertEqual(importer.fileKind(mask), "sequence")
        self.assertEqual(importer.fileKind(plain), "volume")
        # A NIfTI-2 header (big endian), and a vector image (dim[4] == 1, dim[5] == 3) that the volume reader reads
        header = bytearray(544)
        header[0:4] = struct.pack(">i", 540)
        header[16:80] = struct.pack(">8q", 4, 10, 10, 10, 7, 1, 1, 1)
        nifti2 = os.path.join(self.directory, "nifti2_bold.nii")
        with open(nifti2, "wb") as f:
            f.write(bytes(header))
        self.assertTrue(importer.isNifti4D(nifti2))
        header = bytearray(352)
        header[0:4] = struct.pack("<i", 348)
        header[40:56] = struct.pack("<8h", 5, 10, 10, 10, 1, 3, 1, 1)
        vector = os.path.join(self.directory, "displacement.nii")
        with open(vector, "wb") as f:
            f.write(bytes(header))
        self.assertFalse(importer.isNifti4D(vector))

    def test_nrrd_sequence_opens_as_sequence(self):
        """BB-1: a NRRD volume sequence (``.seq.nrrd`` as 3D Slicer saves it, or a 4D NRRD with a list axis) opens as a
        sequence, as in 3D Slicer, and its current frame is the scan of Draw: the volume reader would read its frames as one
        vector volume (a colour image) and Draw would have no scan.
        """
        from StudioLib import importer

        # A sequence of 3 frames saved by 3D Slicer's own writer
        sequenceNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSequenceNode", "StudioImportTestSequence")
        rng = np.random.default_rng(5)
        for index in range(3):
            frame = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", f"StudioImportTestFrame{index}")
            slicer.util.updateVolumeFromArray(frame, (rng.random((8, 12, 16)) * 100).astype(np.int16))
            frame.SetSpacing(2.0, 2.0, 3.0)
            sequenceNode.SetDataNodeAtValue(frame, str(index))
            slicer.mrmlScene.RemoveNode(frame)
        seqPath = os.path.join(self.directory, "fmri_run.seq.nrrd")
        self.assertTrue(slicer.util.saveNode(sequenceNode, seqPath))
        slicer.mrmlScene.RemoveNode(sequenceNode)
        # A 4D NRRD with a list axis and a generic extension, written by hand
        listPath = os.path.join(self.directory, "task_bold.nrrd")
        data = (rng.random((3, 8, 12, 16)) * 100).astype("<i2")
        with open(listPath, "wb") as f:
            f.write(b"NRRD0004\ntype: short\ndimension: 4\nspace: left-posterior-superior\nsizes: 16 12 8 3\n"
                    b"space directions: (2,0,0) (0,2,0) (0,0,3) none\nkinds: domain domain domain list\n"
                    b"endian: little\nencoding: raw\nspace origin: (0,0,0)\n\n")
            f.write(data.tobytes())
        plain = os.path.join(self.directory, "sub-31_T1w.nrrd")
        self.assertTrue(slicer.util.saveNode(self._volumeNode(), plain))

        # Detection: as 3D Slicer chooses its reader
        for path in (seqPath, listPath):
            self.assertEqual(slicer.app.coreIOManager().fileType(path), "SequenceFile", path)
            self.assertEqual(importer.fileKind(path), "sequence", path)
            self.assertTrue(importer.isNrrdSequence(path), path)
        self.assertFalse(importer.isNrrdSequence(plain))
        self.assertEqual(importer.fileKind(plain), "volume")
        self.assertEqual(importer.fileKind(os.path.join(self.directory, "brain.seg.nrrd")), "lesion")
        self.assertEqual(importer._baseName(seqPath), "fmri_run")

        # Studio: Open... and drop load it as a sequence, and its current frame is the scan of Draw
        studioShell = self._applyStudio()
        for path, name in ((seqPath, "fmri_run"), (listPath, "task_bold")):
            slicer.mrmlScene.Clear(0)
            self._pump()
            result = importer.handleDrop([path], shell=studioShell) if name == "fmri_run" else importer.loadFiles([path], studioShell)
            self._pump()
            self.assertEqual(result.errors, [], path)
            self.assertEqual(len(result.volumes), 1, path)
            proxy = result.volumes[0]
            self.assertTrue(proxy.IsA("vtkMRMLScalarVolumeNode"), proxy.GetClassName())
            self.assertFalse(proxy.IsA("vtkMRMLVectorVolumeNode"), proxy.GetClassName())
            self.assertEqual(proxy.GetImageData().GetNumberOfScalarComponents(), 1)
            self.assertEqual(slicer.util.getNodesByClass("vtkMRMLVectorVolumeNode"), [])
            sequences = slicer.util.getNodesByClass("vtkMRMLSequenceNode")
            self.assertEqual(len(sequences), 1, path)
            self.assertEqual(sequences[0].GetNumberOfDataNodes(), 3)
            self.assertEqual(importer._backgroundVolume().GetID(), proxy.GetID())
            _segmentationNode, sourceVolume = importer._editedNodes()
            self.assertIsNotNone(sourceVolume, path)
            self.assertEqual(sourceVolume.GetID(), proxy.GetID())

    def _volumeNode(self):
        node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "StudioImportTestPlain")
        slicer.util.updateVolumeFromArray(node, np.zeros((8, 12, 16), np.int16))
        return node

    def test_damaged_study_json_is_reported_in_plain_words(self):
        """A study folder whose study.json is damaged (cut short, empty, not a study) that is dropped on the window or opened
        shows a plain message that study.json cannot be read, never a traceback or a parser message, and opens no study.
        """
        from StudioLib import importer, study_ui

        studioShell = self._applyStudio()
        if not study_ui.isAvailable():
            self.skipTest("studies are not available in this build")
        cases = {
            "truncated": '{"format": "slicer-studio-study", "name": "My stu',
            "empty": "",
            "list": "[1, 2, 3]",
            "garbage": "\x00\xff not json",
        }
        for label, content in cases.items():
            folder = os.path.join(self.directory, f"damaged_{label}")
            os.makedirs(folder)
            with open(os.path.join(folder, "study.json"), "wb") as f:
                f.write(content.encode("latin-1"))
            with self.assertRaises(study_ui.StudyFileError, msg=label) as raised:
                importer.loadFolder(folder, studioShell)
            message = str(raised.exception)
            self.assertIn("study.json", message, label)
            self.assertIn("damaged", message, label)
            self.assertNotIn("Expecting", message, label)
            self.assertNotIn("char ", message, label)
            # The window's drop handler tells the user (a toast), instead of failing silently in the log
            before = len(studioShell.recentMessages())
            importer._DropHandler(studioShell)._load([folder], False)
            self._pump(2)
            messages = studioShell.recentMessages()[before:]
            self.assertEqual(len(messages), 1, label)
            self.assertIn("study.json", messages[0][1], label)
            self.assertIn("damaged", messages[0][1], label)
            self.assertIsNone(study_ui.session(), label)

    def test_damaged_nifti_gz_is_sorted_without_an_exception(self):
        """A ``.nii.gz`` that cannot be decompressed (corrupt deflate stream, truncated, not gzip, empty) is sorted by its name
        and reported by the volume reader: the header readers return None and never raise (gzip raises ``zlib.error``, not an
        ``OSError``, for a corrupt deflate stream).
        """
        import zlib

        from StudioLib import importer

        good = gzip.compress(bytes(range(256)) * 20)
        corrupt = bytearray(good)
        corrupt[20:40] = b"x" * 20
        with self.assertRaises(zlib.error):
            gzip.decompress(bytes(corrupt))  # the damage this test is about
        files = {
            "corrupt_bold.nii.gz": bytes(corrupt),
            "sub-02_lesion_corrupt.nii.gz": bytes(corrupt),
            "truncated.nii.gz": good[:60],
            "notgzip.nii.gz": b"hello" * 100,
            "empty.nii.gz": b"",
        }
        paths = {}
        for name, content in files.items():
            paths[name] = os.path.join(self.directory, name)
            with open(paths[name], "wb") as f:
                f.write(content)
        for name, path in paths.items():
            self.assertIsNone(importer.niftiDimensions(path), name)
            self.assertIsNone(importer.niftiIntentCode(path), name)
            self.assertFalse(importer.isNifti4D(path), name)
            self.assertFalse(importer.isStatMap(path), name)
            self.assertEqual(importer.fileKind(path), "lesion" if "lesion" in name else "volume", name)
        # Open.../drop: the damaged file is a load error (the generic "could not be loaded" message), not an exception
        result = importer.loadFiles([paths["corrupt_bold.nii.gz"]], self._applyStudio())
        self._pump()
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.volumes, [])

    def test_unreadable_nifti_is_refused_with_a_reason_and_no_error(self):
        """BB-1: a dropped ``.nii.gz`` that is empty, is not NIfTI (an HTML page) or no longer exists is refused with the
        reason: one WARNING, no traceback, no ERROR record (the error badge), and the box text names the reason.
        """
        from StudioLib import importer

        studioShell = self._applyStudio()
        shown = []
        original = (slicer.util.errorDisplay, slicer.util.warningDisplay)
        slicer.util.errorDisplay = lambda text, **kw: shown.append(("error", text))
        slicer.util.warningDisplay = lambda text, **kw: shown.append(("warning", text))
        paths = {}
        for name, content, word in (
            ("download.nii.gz", b"", "empty"),
            ("notreally.nii.gz", b"<html><body>404 Not Found</body></html>", "not a NIfTI"),
            ("plain.nii", b"<html>404</html>" * 40, "not a NIfTI"),
            ("gone.nii.gz", None, "no longer exists"),
        ):
            paths[name] = os.path.join(self.directory, name)
            if content is not None:
                with open(paths[name], "wb") as f:
                    f.write(content)
        try:
            for name, word in (("download.nii.gz", "empty"), ("notreally.nii.gz", "not a NIfTI"),
                               ("plain.nii", "not a NIfTI"), ("gone.nii.gz", "no longer exists")):
                recorder = logging.Handler()
                records = []
                recorder.emit = records.append
                logging.getLogger().addHandler(recorder)
                del shown[:]
                try:
                    result = importer.loadFiles([paths[name]], studioShell)
                finally:
                    logging.getLogger().removeHandler(recorder)
                self._pump()
                self.assertEqual(len(result.errors), 1, name)
                self.assertIn(word, result.errors[0][1], name)
                self.assertFalse([r for r in records if "Traceback" in r.getMessage()], name)
                self.assertFalse([r for r in records if r.levelno >= logging.ERROR], name)
                self.assertEqual([kind for kind, _text in shown], ["warning"], name)
                self.assertIn(word, shown[0][1], name)
                self.assertEqual(result.volumes, [], name)
        finally:
            slicer.util.errorDisplay, slicer.util.warningDisplay = original

    def test_truncated_nifti_is_refused_as_damaged(self):
        """BB-1: a NIfTI file whose data stops before the end that its header announces (gzip or plain) is a load error in
        plain words, for a scan and for a lesion: the reader zero-fills the missing voxels and reports success.
        """
        from StudioLib import importer

        def niftiBytes(dims, data):
            header = bytearray(348)
            struct.pack_into("<i", header, 0, 348)
            struct.pack_into("<8h", header, 40, 3, *dims, 1, 1, 1, 1)
            struct.pack_into("<hh", header, 70, 2, 8)  # uint8
            struct.pack_into("<8f", header, 76, 1, 1, 1, 1, 1, 0, 0, 0)
            struct.pack_into("<f", header, 108, 352)
            header[344:348] = b"n+1\0"
            return bytes(header) + bytes(4) + data

        dims = (8, 8, 6)
        data = bytes([1]) * (8 * 8 * 6)
        complete = niftiBytes(dims, data)
        cut = complete[: len(complete) - 100]
        paths = {}
        for name, content in (
            ("sub-01_T1w.nii", complete), ("sub-02_T1w.nii.gz", gzip.compress(complete)),
            ("sub-03_T1w.nii", cut), ("sub-04_T1w.nii.gz", gzip.compress(cut)),
            ("sub-05_lesion.nii.gz", gzip.compress(cut)),
        ):
            paths[name] = os.path.join(self.directory, name)
            with open(paths[name], "wb") as f:
                f.write(content)
        for name in ("sub-01_T1w.nii", "sub-02_T1w.nii.gz"):
            self.assertIsNone(importer._niftiDataShortfall(paths[name]), name)
        studioShell = self._applyStudio()  # once: every shell keeps its own toast widgets in the main window
        for name in ("sub-03_T1w.nii", "sub-04_T1w.nii.gz", "sub-05_lesion.nii.gz"):
            have, need = importer._niftiDataShortfall(paths[name])
            self.assertEqual((have, need), (len(complete) - 100, len(complete)), name)
            recorder = logging.Handler()
            records = []
            recorder.emit = records.append
            logging.getLogger().addHandler(recorder)
            try:
                result = importer.loadFiles([paths[name]], studioShell)
            finally:
                logging.getLogger().removeHandler(recorder)
            self._pump()
            self.assertEqual(len(result.errors), 1, name)
            self.assertIn("damaged", result.errors[0][1], name)
            # a planned refusal is not a defect: no traceback in the log
            self.assertFalse([r for r in records if "Traceback" in r.getMessage()], name)
            self.assertEqual(result.volumes + result.segmentations, [], name)
            self.assertEqual(len(slicer.util.getNodesByClass("vtkMRMLVolumeNode")), 0, name)
        result = importer.loadFiles([paths["sub-02_T1w.nii.gz"]], studioShell)
        self._pump()
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.volumes), 1)

    def test_recent_file_damaged_since_is_refused_as_damaged(self):
        """BB-1: a recent file (File > Recent, Library "Opened from elsewhere", Search) that was cut short after it was
        first opened is refused as damaged with its reason, as with a drop or Open...: no half zero-filled scan is loaded.
        """
        from StudioLib import importer

        header = bytearray(348)
        struct.pack_into("<i", header, 0, 348)
        struct.pack_into("<8h", header, 40, 3, 8, 8, 6, 1, 1, 1, 1)
        struct.pack_into("<hh", header, 70, 2, 8)  # uint8
        struct.pack_into("<8f", header, 76, 1, 1, 1, 1, 1, 0, 0, 0)
        struct.pack_into("<f", header, 108, 352)
        header[344:348] = b"n+1\0"
        complete = bytes(header) + bytes(4) + bytes([1]) * (8 * 8 * 6)
        studioShell = self._applyStudio()
        shown = []
        for name, pack in (("sub-06_T1w.nii", bytes), ("sub-07_T1w.nii.gz", gzip.compress)):
            path = os.path.join(self.directory, name)
            with open(path, "wb") as f:
                f.write(pack(complete))
            result = importer.loadFiles([path], studioShell)
            self._pump()
            self.assertEqual(result.errors, [], name)
            slicer.mrmlScene.Clear(0)
            self._pump()
            with open(path, "wb") as f:
                f.write(pack(complete[: len(complete) // 2]))  # the image data stops halfway
            del shown[:]
            original = slicer.util.errorDisplay, slicer.util.warningDisplay
            slicer.util.errorDisplay = lambda text, *args, **kwargs: shown.append(("error", text))
            slicer.util.warningDisplay = lambda text, *args, **kwargs: shown.append(("warning", text))
            try:
                result = importer.loadRecentFile({"fileName": path, "fileType": "VolumeFile"}, None, studioShell)
                self._pump()
            finally:
                slicer.util.errorDisplay, slicer.util.warningDisplay = original
            self.assertEqual(len(result.errors), 1, name)
            self.assertIn("damaged", result.errors[0][1], name)
            self.assertEqual([kind for kind, text in shown], ["warning"], name)
            self.assertIn("damaged", shown[0][1], name)
            self.assertIn("It was not opened", shown[0][1], name)
            self.assertEqual(result.nodes(), [], name)
            self.assertEqual(slicer.util.getNodesByClass("vtkMRMLVolumeNode"), [], name)

    def test_nifti_with_invalid_header_size_is_refused_as_damaged(self):
        """BB-1: a NIfTI whose header gives a dimension below 1 (dim = 3, -5, 64, 64) is refused in plain words as damaged,
        logged without a traceback: the reader fails on it with a bare error.
        """
        from StudioLib import importer

        header = bytearray(348)
        struct.pack_into("<i", header, 0, 348)
        struct.pack_into("<8h", header, 40, 3, -5, 64, 64, 1, 1, 1, 1)
        struct.pack_into("<hh", header, 70, 2, 8)
        struct.pack_into("<8f", header, 76, 1, 1, 1, 1, 1, 0, 0, 0)
        struct.pack_into("<f", header, 108, 352)
        header[344:348] = b"n+1\0"
        path = os.path.join(self.directory, "negdim_T1w.nii")
        with open(path, "wb") as f:
            f.write(bytes(header) + bytes(4) + bytes(1000))
        self.assertTrue(importer._niftiHasInvalidSize(path))
        studioShell = self._applyStudio()
        recorder = logging.Handler()
        records = []
        recorder.emit = records.append
        logging.getLogger().addHandler(recorder)
        try:
            result = importer.loadFiles([path], studioShell)
        finally:
            logging.getLogger().removeHandler(recorder)
        self._pump()
        self.assertEqual(len(result.errors), 1)
        self.assertIn("damaged", result.errors[0][1])
        self.assertFalse([r for r in records if "Traceback" in r.getMessage() or r.levelno >= logging.ERROR])

    def test_nifti_with_non_finite_geometry_is_refused_as_damaged(self):
        """BB-1: a lesion mask whose sform (sform_code 1) holds a NaN aborted the app (std::bad_alloc in the Segment
        Editor, on the NaN extent). It is refused in plain words as damaged, like other damaged headers; a NaN in a
        quaternion that the header does not use (qform_code 0) does not refuse a file.
        """
        from StudioLib import importer

        def write(name, srowX, quaternB=0.0, qformCode=0):
            header = bytearray(348)
            struct.pack_into("<i", header, 0, 348)
            struct.pack_into("<8h", header, 40, 3, 16, 16, 16, 1, 1, 1, 1)
            struct.pack_into("<hh", header, 70, 2, 8)  # uint8
            struct.pack_into("<8f", header, 76, 1, 1, 1, 1, 1, 0, 0, 0)
            struct.pack_into("<f", header, 108, 352)
            struct.pack_into("<2h", header, 252, qformCode, 1)
            struct.pack_into("<f", header, 256, quaternB)
            struct.pack_into("<12f", header, 280, *srowX, 0, 1, 0, 0, 0, 0, 1, 0)
            header[344:348] = b"n+1\0"
            voxels = bytearray(16 * 16 * 16)
            voxels[16 * 16 * 8 + 16 * 8 + 8] = 1
            path = os.path.join(self.directory, name)
            with open(path, "wb") as f:
                f.write(bytes(header) + bytes(4) + bytes(voxels))
            return path

        nanPath = write("sub-71_lesion.nii", (float("nan"), 0, 0, 0))
        infPath = write("sub-72_lesion.nii", (1, 0, 0, float("inf")))
        unusedPath = write("sub-73_lesion.nii", (1, 0, 0, 0), quaternB=float("nan"), qformCode=0)
        self.assertTrue(importer._niftiHasInvalidGeometry(nanPath))
        self.assertTrue(importer._niftiHasInvalidGeometry(infPath))
        self.assertFalse(importer._niftiHasInvalidGeometry(unusedPath))
        importer._checkNiftiComplete(unusedPath)  # no RefusedFileError
        studioShell = self._applyStudio()
        recorder = logging.Handler()
        records = []
        recorder.emit = records.append
        logging.getLogger().addHandler(recorder)
        try:
            result = importer.loadFiles([nanPath, infPath], studioShell)
        finally:
            logging.getLogger().removeHandler(recorder)
        self._pump()
        self.assertEqual(len(result.errors), 2)
        for _path, message in result.errors:
            self.assertIn("invalid position or orientation", message)
        self.assertFalse([r for r in records if "Traceback" in r.getMessage() or r.levelno >= logging.ERROR])
        self.assertEqual(slicer.mrmlScene.GetNodesByClass("vtkMRMLSegmentationNode").GetNumberOfItems(), 0)

    def test_nifti_with_zero_voxel_size_is_refused_in_plain_words(self):
        """BB-1: a NIfTI with a voxel size of 0 and an all-zero affine reached the ITK reader, which showed the raw 'Slicer
        error' box (a list of ITK readers) and raised the error badge. It is refused in plain words, with no ERROR records;
        a zero voxel size that a valid affine overrides, or on an axis that the image does not have, still opens.
        """
        from StudioLib import importer

        def write(name, pixdim, srow, sformCode=1, dim=3):
            header = bytearray(348)
            struct.pack_into("<i", header, 0, 348)
            struct.pack_into("<8h", header, 40, dim, 8, 8, 6 if dim == 3 else 1, 1, 1, 1, 1)
            struct.pack_into("<hh", header, 70, 2, 8)  # uint8
            struct.pack_into("<8f", header, 76, 1, *pixdim, 1, 0, 0, 0)
            struct.pack_into("<f", header, 108, 352)
            struct.pack_into("<2h", header, 252, 0, sformCode)
            struct.pack_into("<12f", header, 280, *srow)
            header[344:348] = b"n+1\0"
            path = os.path.join(self.directory, name)
            with open(path, "wb") as f:
                f.write(bytes(header) + bytes(4) + bytes([1]) * (8 * 8 * (6 if dim == 3 else 1)))
            return path

        identity = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0)
        zeros = (0,) * 12
        singular = write("sub-15_T1w.nii", (0, 0, 0), zeros)
        zeroNoAffine = write("sub-16_T1w.nii", (1, 0, 1), zeros, sformCode=0)
        overridden = write("sub-17_T1w.nii", (0, 0, 0), identity)
        flat = write("sub-18_T1w.nii", (1, 1, 0), identity, sformCode=0, dim=2)
        self.assertTrue(importer._niftiHasDegenerateGeometry(singular))
        self.assertTrue(importer._niftiHasDegenerateGeometry(zeroNoAffine))
        self.assertFalse(importer._niftiHasDegenerateGeometry(overridden))
        self.assertFalse(importer._niftiHasDegenerateGeometry(flat))
        for path in (overridden, flat):
            importer._checkNiftiComplete(path)  # no RefusedFileError
        studioShell = self._applyStudio()
        recorder = logging.Handler()
        records = []
        recorder.emit = records.append
        logging.getLogger().addHandler(recorder)
        try:
            result = importer.loadFiles([singular, zeroNoAffine], studioShell)
        finally:
            logging.getLogger().removeHandler(recorder)
        self._pump()
        self.assertEqual(len(result.errors), 2)
        for _path, message in result.errors:
            self.assertIn("voxel size of 0", message)
        self.assertFalse([r for r in records if "Traceback" in r.getMessage() or r.levelno >= logging.ERROR])

    def test_unknown_datatype_and_degenerate_nrrd_directions_are_refused_in_plain_words(self):
        """BB-1: a NIfTI with a datatype code that NIfTI does not define, and an NRRD whose space directions are all zero
        or not numbers, fell through to the ITK reader: a bare 'Slicer error' box, an ERROR traceback and the error badge.
        They are refused in plain words with WARNING records only; ordinary files still pass the check.
        """
        from StudioLib import importer

        def niftiPath(name, datatype):
            header = bytearray(348)
            struct.pack_into("<i", header, 0, 348)
            struct.pack_into("<8h", header, 40, 3, 8, 8, 6, 1, 1, 1, 1)
            struct.pack_into("<hh", header, 70, datatype, 8)
            struct.pack_into("<8f", header, 76, 1, 1, 1, 1, 1, 0, 0, 0)
            struct.pack_into("<f", header, 108, 352)
            header[344:348] = b"n+1\0"
            path = os.path.join(self.directory, name)
            with open(path, "wb") as f:
                f.write(bytes(header) + bytes(4) + bytes([1]) * (8 * 8 * 6))
            return path

        def nrrdPath(name, directions):
            path = os.path.join(self.directory, name)
            with open(path, "wb") as f:
                f.write(("NRRD0004\ntype: uchar\ndimension: 3\nspace: left-posterior-superior\nsizes: 4 4 4\n"
                         "space directions: %s\nkinds: domain domain domain\nendian: little\nencoding: raw\n"
                         "space origin: (0,0,0)\n\n" % directions).encode() + bytes(64))
            return path

        unknown = niftiPath("sub-95_T1w.nii", 9999)
        zeroDirections = nrrdPath("sub-96_T1w.nrrd", "(0,0,0) (0,1,0) (0,0,1)")
        nanDirections = nrrdPath("sub-97_T1w.nrrd", "(nan,0,0) (0,1,0) (0,0,1)")
        for path in (niftiPath("sub-98_T1w.nii", 2), nrrdPath("sub-99_T1w.nrrd", "(1,0,0) (0,1,0) (0,0,1)"),
                     nrrdPath("sub-100_T1w.nrrd", "(1,0,0) none (0,0,1)")):
            importer._checkNiftiComplete(path)  # no RefusedFileError
        for path in (unknown, zeroDirections, nanDirections):
            with self.assertRaises(importer.RefusedFileError):
                importer._checkNiftiComplete(path)
        studioShell = self._applyStudio()
        recorder = logging.Handler()
        records = []
        recorder.emit = records.append
        logging.getLogger().addHandler(recorder)
        try:
            result = importer.loadFiles([unknown, zeroDirections, nanDirections], studioShell)
        finally:
            logging.getLogger().removeHandler(recorder)
        self._pump()
        self.assertEqual(len(result.errors), 3)
        for _path, message in result.errors:
            self.assertIn("is damaged", message)
        self.assertFalse([r for r in records if "Traceback" in r.getMessage() or r.levelno >= logging.ERROR])

    def test_uncompressed_nifti_named_nii_gz_opens(self):
        """BB-1: a valid NIfTI saved without compression but named ``.nii.gz`` (a browser or server unpacked a download and
        kept the name) opens as a scan and as a lesion, as in official 3D Slicer, whose reader reads it as plain data. A
        cut-off one is still refused as damaged, and the refusal box names the file once.
        """
        from StudioLib import importer

        header = bytearray(348)
        struct.pack_into("<i", header, 0, 348)
        struct.pack_into("<8h", header, 40, 3, 8, 8, 6, 1, 1, 1, 1)
        struct.pack_into("<hh", header, 70, 2, 8)  # uint8
        struct.pack_into("<8f", header, 76, 1, 1, 1, 1, 1, 0, 0, 0)
        struct.pack_into("<f", header, 108, 352)
        header[344:348] = b"n+1\0"
        complete = bytes(header) + bytes(4) + bytes([1]) * (8 * 8 * 6)
        paths = {}
        for name, content in (
            ("sub-91_T1w.nii.gz", complete), ("sub-91_lesion.nii.gz", complete),
            ("sub-92_T1w.nii.gz", gzip.compress(complete)), ("sub-93_T1w.nii.gz", complete[:-100]),
            ("zero.nii.gz", b""),
        ):
            paths[name] = os.path.join(self.directory, name)
            with open(paths[name], "wb") as f:
                f.write(content)
        for name in ("sub-91_T1w.nii.gz", "sub-92_T1w.nii.gz"):
            self.assertEqual(importer.niftiDimensions(paths[name])[:4], [3, 8, 8, 6], name)
            self.assertIsNone(importer._niftiDataShortfall(paths[name]), name)
            importer._checkNiftiComplete(paths[name])  # no RefusedFileError
        self.assertEqual(importer._niftiDataShortfall(paths["sub-93_T1w.nii.gz"]), (len(complete) - 100, len(complete)))
        studioShell = self._applyStudio()
        for name in ("sub-91_T1w.nii.gz", "sub-92_T1w.nii.gz"):
            result = importer.loadFiles([paths[name]], studioShell)
            self._pump()
            self.assertEqual(result.errors, [], name)
            self.assertEqual(len(result.volumes), 1, name)
        result = importer.loadFiles([paths["sub-91_lesion.nii.gz"]], studioShell)
        self._pump()
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.segmentations), 1)
        shown = []
        original = (slicer.util.errorDisplay, slicer.util.warningDisplay)
        slicer.util.errorDisplay = slicer.util.warningDisplay = lambda text, **kw: shown.append(text)
        try:
            result = importer.loadFiles([paths["sub-93_T1w.nii.gz"], paths["zero.nii.gz"]], studioShell)
        finally:
            slicer.util.errorDisplay, slicer.util.warningDisplay = original
        self._pump()
        self.assertEqual(len(result.errors), 2)
        self.assertTrue(any("damaged" in error for _path, error in result.errors), result.errors)
        self.assertEqual(len(shown), 1)
        self.assertNotIn("zero.nii.gz: zero.nii.gz", shown[0])
        self.assertIn("zero.nii.gz is empty", shown[0])

    def test_header_lying_or_cut_nrrd_and_mha_are_refused_as_damaged(self):
        """BB-1: an NRRD, seg.nrrd, NHDR or MetaImage file whose data is shorter than its header announces is a planned
        refusal like the NIfTI one: plain "damaged" words, a warning without a traceback, no raw reader text.
        """
        import zlib

        from StudioLib import importer

        head = ("NRRD0004\ntype: short\ndimension: 3\nspace: left-posterior-superior\nsizes: {sizes}\n"
                "space directions: (1,0,0) (0,1,0) (0,0,1)\nkinds: domain domain domain\nendian: little\n"
                "encoding: {encoding}\nspace origin: (0,0,0)\n")
        raw = bytes(16 * 16 * 10 * 2)
        files = {}
        files["ok_raw_T1w.nrrd"] = (head.format(sizes="16 16 10", encoding="raw") + "\n").encode() + raw
        files["ok_gz_T1w.nrrd"] = (head.format(sizes="16 16 10", encoding="gzip") + "\n").encode() + gzip.compress(raw)
        files["cut_raw_T1w.nrrd"] = files["ok_raw_T1w.nrrd"][:-100]
        gz = gzip.compress(raw)
        files["cut_gz_T1w.nrrd"] = (head.format(sizes="16 16 10", encoding="gzip") + "\n").encode() + gz[: len(gz) // 2]
        files["lie_gz_T1w.nrrd"] = (head.format(sizes="2048 1024 1024", encoding="gzip") + "\n").encode() + gz
        files["lie_seg.seg.nrrd"] = (head.format(sizes="2048 1024 1024", encoding="gzip")
                                     + "Segment0_ID:=Segment_1\nSegment0_Name:=Lesion\n\n").encode() + gz
        files["lie_detached_T1w.raw"] = raw
        files["lie_detached_T1w.nhdr"] = (head.format(sizes="5000 5000 5000", encoding="raw")
                                          + "data file: lie_detached_T1w.raw\n").encode()
        meta = ("ObjectType = Image\nNDims = 3\nDimSize = {sizes}\nElementType = MET_SHORT\n"
                "CompressedData = {compressed}\nElementDataFile = LOCAL\n")
        files["ok_T1w.mha"] = meta.format(sizes="16 16 10", compressed="False").encode() + raw
        files["cut_T1w.mha"] = files["ok_T1w.mha"][:-100]
        noise = zlib.compress(os.urandom(16 * 16 * 10 * 2))  # incompressible: half of the stream holds about half the data
        files["cut_z_T1w.mha"] = meta.format(sizes="16 16 10", compressed="True").encode() + noise[: len(noise) // 2]
        paths = {}
        for name, content in files.items():
            paths[name] = os.path.join(self.directory, name)
            with open(paths[name], "wb") as f:
                f.write(content)
        for name in ("ok_raw_T1w.nrrd", "ok_gz_T1w.nrrd", "ok_T1w.mha"):
            self.assertIsNone(importer._nrrdMhaDataShortfall(paths[name]), name)
        studioShell = self._applyStudio()
        refused = ("cut_raw_T1w.nrrd", "cut_gz_T1w.nrrd", "lie_gz_T1w.nrrd", "lie_seg.seg.nrrd", "lie_detached_T1w.nhdr",
                   "cut_T1w.mha", "cut_z_T1w.mha")
        for name in refused:
            self.assertIsNotNone(importer._nrrdMhaDataShortfall(paths[name]), name)
            recorder = logging.Handler()
            records = []
            recorder.emit = records.append
            logging.getLogger().addHandler(recorder)
            original = slicer.util.errorDisplay, slicer.util.warningDisplay
            shown = []
            slicer.util.errorDisplay = lambda text, shown=shown, **kwargs: shown.append(
                ("error", text, kwargs.get("detailedText", "")))
            slicer.util.warningDisplay = lambda text, shown=shown, **kwargs: shown.append(
                ("warning", text, kwargs.get("detailedText", "")))
            try:
                result = importer.loadFiles([paths[name]], studioShell)
            finally:
                slicer.util.errorDisplay, slicer.util.warningDisplay = original
                logging.getLogger().removeHandler(recorder)
            self._pump()
            self.assertEqual(len(result.errors), 1, name)
            self.assertIn("damaged", result.errors[0][1], name)
            self.assertFalse([r for r in records if "Traceback" in r.getMessage()], name)
            self.assertFalse([r for r in records if r.levelno >= logging.ERROR], name)
            self.assertEqual([kind for kind, _text, _details in shown], ["warning"], name)
            for word in ("vtkMRML", "itk::", "[nrrd]", "MegaMacro"):
                self.assertNotIn(word, shown[0][2], name)
            self.assertEqual(result.volumes + result.segmentations, [], name)
            self.assertEqual(len(slicer.util.getNodesByClass("vtkMRMLVolumeNode")), 0, name)
        result = importer.loadFiles([paths["ok_gz_T1w.nrrd"]], studioShell)
        self._pump()
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.volumes), 1)

    def test_cut_gzip_stream_nifti_is_refused_as_damaged(self):
        """BB-1: a ``.nii.gz`` whose compressed stream is cut off (a stopped copy or download: no gzip end marker) is a
        load error in plain words like a complete stream with short data, for a scan and for a lesion: Python's gzip
        raises EOFError, but the ITK reader zero-fills the missing voxels and reports success.
        """
        from StudioLib import importer

        dims = (32, 32, 20)
        header = bytearray(348)
        struct.pack_into("<i", header, 0, 348)
        struct.pack_into("<8h", header, 40, 3, *dims, 1, 1, 1, 1)
        struct.pack_into("<hh", header, 70, 4, 16)  # int16
        struct.pack_into("<8f", header, 76, 1, 1, 1, 1, 1, 0, 0, 0)
        struct.pack_into("<f", header, 108, 352)
        header[344:348] = b"n+1\0"
        rng = np.random.default_rng(7)
        scan = rng.integers(100, 1000, size=dims[::-1], dtype="<i2")
        lesion = np.zeros(dims[::-1], "<i2")
        lesion[2:18, 4:28, 4:28] = 1
        paths = {}
        for name, data in (("sub-01_T1w.nii.gz", scan), ("sub-01_lesion.nii.gz", lesion)):
            complete = gzip.compress(bytes(header) + bytes(4) + data.tobytes())
            paths[name] = os.path.join(self.directory, name)
            with open(paths[name], "wb") as f:
                f.write(complete[: len(complete) * 6 // 10])
        need = 352 + 2 * dims[0] * dims[1] * dims[2]
        studioShell = self._applyStudio()  # once: every shell keeps its own toast widgets in the main window
        for name, path in paths.items():
            have, needed = importer._niftiDataShortfall(path)
            self.assertEqual(needed, need, name)
            self.assertLess(have, need, name)
            result = importer.loadFiles([path], studioShell)
            self._pump()
            self.assertEqual(len(result.errors), 1, name)
            self.assertIn("damaged", result.errors[0][1], name)
            self.assertEqual(result.volumes + result.segmentations, [], name)
            self.assertEqual(len(slicer.util.getNodesByClass("vtkMRMLVolumeNode")), 0, name)
            self.assertEqual(len(slicer.util.getNodesByClass("vtkMRMLSegmentationNode")), 0, name)

    def test_allocation_failure_is_found_behind_a_fallback_error_and_a_bare_nifti_error(self):
        """BB-1: a file too large for the memory is refused with the plain "too large" warning (no traceback), also when
        the allocation error is only the ``__context__`` of the error that is raised last (the lesion route falls back to
        a labelmap reader, which fails with "nifti_image_load failed"), or when the NIfTI reader says only
        "nifti_image_load failed" for an intact file whose voxels are more than half of the memory that is left.
        """
        from StudioLib import importer

        try:
            try:
                raise RuntimeError("itkImportImageContainer.hxx: Failed to allocate memory for image")
            except RuntimeError:
                raise RuntimeError("nifti_image_load failed for file: a.nii")
        except RuntimeError as e:
            self.assertTrue(importer._isAllocationFailure(e))
        self.assertFalse(importer._isAllocationFailure(RuntimeError("nifti_image_load failed")))

        header = bytearray(348)
        struct.pack_into("<i", header, 0, 348)
        struct.pack_into("<8h", header, 40, 3, 2048, 1024, 1024, 1, 1, 1, 1)
        struct.pack_into("<hh", header, 70, 2, 8)
        struct.pack_into("<f", header, 108, 352)
        header[344:348] = b"n+1\0"
        path = os.path.join(self.directory, "big_T1w.nii")
        with open(path, "wb") as f:
            f.write(bytes(header) + bytes(4))
            f.truncate(352 + 2048 * 1024 * 1024)  # sparse: no disk used
        self.assertEqual(importer._niftiImageBytes(path), 2048 * 1024 * 1024)
        error = RuntimeError("Failed to load node from file\n- Error: nifti_image_load failed for file: " + path)
        original = importer._availableMemoryBytes
        try:
            importer._availableMemoryBytes = lambda: 3 * 1024**3
            self.assertTrue(importer._isNiftiTooLargeForMemory(path, error))
            importer._availableMemoryBytes = lambda: 8 * 1024**3
            self.assertFalse(importer._isNiftiTooLargeForMemory(path, error))
            importer._availableMemoryBytes = lambda: None
            self.assertFalse(importer._isNiftiTooLargeForMemory(path, error))
        finally:
            importer._availableMemoryBytes = original
        self.assertFalse(importer._isNiftiTooLargeForMemory(path, RuntimeError("something else")))

    def test_int64_nifti_scan_is_refused(self):
        """BB-1: a NIfTI scan with 64-bit integer voxels (datatype 1024 or 1280, what nibabel writes for a numpy integer
        array) is refused in plain words before it is read: read as VTK_LONG, its first slice render ended the
        application. An int32 scan of the same voxels opens, and an int64 lesion is still opened as a lesion.
        """
        from StudioLib import importer

        dims = (12, 10, 8)

        def niftiBytes(datatype, bitpix, data, gz):
            header = bytearray(348)
            struct.pack_into("<i", header, 0, 348)
            struct.pack_into("<8h", header, 40, 3, *dims, 1, 1, 1, 1)
            struct.pack_into("<hh", header, 70, datatype, bitpix)
            struct.pack_into("<8f", header, 76, 1, 1, 1, 1, 1, 0, 0, 0)
            struct.pack_into("<f", header, 108, 352)
            header[344:348] = b"n+1\0"
            content = bytes(header) + bytes(4) + data.tobytes()
            return gzip.compress(content) if gz else content

        rng = np.random.default_rng(11)
        values = rng.integers(0, 1000, size=dims[::-1])
        lesion = np.zeros(dims[::-1], "<i8")
        lesion[2:6, 3:7, 3:9] = 1
        paths = {}
        for name, datatype, bitpix, data in (
            ("sub-01_T1w.nii.gz", 1024, 64, values.astype("<i8")), ("brain_uint64.nii", 1280, 64, values.astype("<u8")),
            ("sub-02_T1w.nii.gz", 8, 32, values.astype("<i4")), ("sub-02_lesion.nii.gz", 1024, 64, lesion),
        ):
            paths[name] = os.path.join(self.directory, name)
            with open(paths[name], "wb") as f:
                f.write(niftiBytes(datatype, bitpix, data, name.endswith(".gz")))
        self.assertEqual(importer._niftiDatatype(paths["sub-01_T1w.nii.gz"]), 1024)
        self.assertEqual(importer._niftiDatatype(paths["sub-02_T1w.nii.gz"]), 8)
        studioShell = self._applyStudio()
        for name in ("sub-01_T1w.nii.gz", "brain_uint64.nii"):
            recorder = logging.Handler()
            records = []
            recorder.emit = records.append
            logging.getLogger().addHandler(recorder)
            try:
                result = importer.loadFiles([paths[name]], studioShell)
            finally:
                logging.getLogger().removeHandler(recorder)
            self._pump()
            self.assertEqual(len(result.errors), 1, name)
            self.assertIn("64-bit integers", result.errors[0][1], name)
            self.assertFalse([r for r in records if "Traceback" in r.getMessage()], name)
            self.assertEqual(result.volumes, [], name)
            self.assertEqual(len(slicer.util.getNodesByClass("vtkMRMLVolumeNode")), 0, name)
        result = importer.loadFiles([paths["sub-02_T1w.nii.gz"]], studioShell)
        self._pump()
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.volumes), 1)
        self.assertEqual(result.volumes[0].GetImageData().GetScalarType(), vtk.VTK_INT)
        result = importer.loadFiles([paths["sub-02_lesion.nii.gz"]], studioShell, pairedLesion=None)
        self._pump()
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.segmentations), 1)

    def test_damaged_seg_nrrd_leaves_nothing_in_the_scene(self):
        """BB-1: a ``.seg.nrrd`` that the reader only partly reads (truncated, header only) is a load error like a damaged
        ``.nii.gz``: the segmentation reader raises but leaves a half-created segmentation node and its storage node, which
        made autosave start an empty "Untitled project". Nothing of the failed file stays in the scene.
        """
        from StudioLib import importer

        dims = GRID_2009C_2MM[0]
        array = np.zeros((dims[2], dims[1], dims[0]), np.uint8)
        array[38:43, 48:53, 43:48] = 1
        labelmap = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode", "StudioImportTestLabels")
        slicer.util.updateVolumeFromArray(labelmap, array)
        labelmap.SetIJKToRASMatrix(self._ijkToRas(GRID_2009C_2MM))
        segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Tumor")
        self.assertTrue(slicer.modules.segmentations.logic().ImportLabelmapToSegmentationNode(labelmap, segmentation))
        good = os.path.join(self.directory, "Tumor.seg.nrrd")
        self.assertTrue(slicer.util.saveNode(segmentation, good))
        slicer.mrmlScene.Clear(0)
        with open(good, "rb") as f:
            content = f.read()
        headerEnd = content.find(b"\n\n") + 2
        self.assertGreater(headerEnd, 2)
        variants = {"quarter.seg.nrrd": content[: len(content) // 4], "headeronly.seg.nrrd": content[:headerEnd]}
        studioShell = self._applyStudio()
        for name, damaged in variants.items():
            path = os.path.join(self.directory, name)
            with open(path, "wb") as f:
                f.write(damaged)
            for load in (lambda path=path: importer.loadFiles([path], studioShell), lambda path=path: importer.loadFiles([path])):
                result = load()
                self._pump()
                self.assertEqual(len(result.errors), 1, name)
                self.assertEqual(result.segmentations, [], name)
                for className in ("vtkMRMLSegmentationNode", "vtkMRMLSegmentationStorageNode", "vtkMRMLSegmentationDisplayNode"):
                    self.assertEqual(slicer.mrmlScene.GetNumberOfNodesByClass(className), 0, f"{name}: {className} left behind")
        # The same file, whole, still loads
        result = importer.loadFiles([good], studioShell)
        self._pump()
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.segmentations), 1)

    def test_lesion_values_beyond_label_range_refused(self):
        """BB-1: a lesion-named image whose values are beyond the label values of a segmentation (float 1e12, int64 2**40,
        float -1e12) crashed the app in the segmentation reader, or failed to allocate (std::bad_alloc, a MemoryError that
        ``loadLesion()`` did not catch) and left an empty segmentation node behind. It is refused with a message and
        nothing stays in the scene; a mask with ordinary values of the same types still opens.
        """
        from StudioLib import importer

        leftovers = ("vtkMRMLSegmentationNode", "vtkMRMLSegmentationStorageNode", "vtkMRMLSegmentationDisplayNode",
                     "vtkMRMLLabelMapVolumeNode", "vtkMRMLVolumeArchetypeStorageNode")

        def assertSceneEmpty(message):
            for className in leftovers:
                self.assertEqual(slicer.mrmlScene.GetNumberOfNodesByClass(className), 0, f"{message}: {className} left behind")

        def writeLesion(name, array):
            if array.dtype != np.int64:
                return self._writeVolume(name, GRID_2009C_2MM, array)
            # A 64-bit integer image (Slicer's volume writer has no numpy long long): NIfTI datatype 1024
            from LesionNetworkMappingLib import nifti

            matrix = self._ijkToRas(GRID_2009C_2MM)
            affine = np.array([[matrix.GetElement(row, column) for column in range(4)] for row in range(4)])
            path = os.path.join(self.directory, name)
            nifti.write_nifti(path, array.transpose(2, 1, 0), affine, dtype=np.int64)
            return path

        dims = GRID_2009C_2MM[0]
        lesion = np.zeros((dims[2], dims[1], dims[0]), bool)
        lesion[38:43, 48:53, 43:48] = True
        studioShell = self._applyStudio()
        for name, dtype, value in (("sub-21_lesion.nii.gz", np.float32, 1e12), ("sub-22_lesion.nii.gz", np.int64, 2**40),
                                   ("sub-23_lesion.nii.gz", np.float32, -1e12), ("sub-24_lesion.nii.gz", np.float32, 3e9)):
            path = writeLesion(name, lesion.astype(dtype) * dtype(value))
            stored = float(dtype(value))
            self.assertEqual(importer._imageValueRange(path), (min(0.0, stored), max(0.0, stored)), name)
            with self.assertRaisesRegex(RuntimeError, "cannot be opened as a lesion"):
                importer.loadLesion(path)
            assertSceneEmpty(name)
            result = importer.loadFiles([path], studioShell)
            self._pump()
            self.assertEqual(result.segmentations, [], name)
            self.assertEqual(len(result.errors), 1, name)
            self.assertIn("cannot be opened as a lesion", result.errors[0][1])
            assertSceneEmpty(name)
        # Ordinary masks of the same types open
        for name, dtype in (("sub-25_lesion.nii.gz", np.float32), ("sub-26_lesion.nii.gz", np.int64)):
            path = writeLesion(name, lesion.astype(dtype))
            segmentationNode = importer.loadLesion(path)
            self.assertEqual(self._voxels(segmentationNode), int(np.count_nonzero(lesion)), name)
            slicer.mrmlScene.RemoveNode(segmentationNode)
        # A reader that fails to allocate (MemoryError) leaves no node: the labelmap route is tried, and if it fails too
        # the error is raised with nothing left in the scene
        slicer.mrmlScene.Clear(0)
        path = os.path.join(self.directory, "sub-25_lesion.nii.gz")

        def failingLoadSegmentation(filename, properties=None, returnNode=False):
            node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", os.path.basename(filename))
            node.SetAndObserveStorageNodeID(slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationStorageNode").GetID())
            raise MemoryError("std::bad_alloc")

        def failingLoadLabelVolume(filename, properties=None, returnNode=False):
            slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode", os.path.basename(filename))
            raise MemoryError("std::bad_alloc")

        originals = slicer.util.loadSegmentation, slicer.util.loadLabelVolume
        try:
            slicer.util.loadSegmentation = failingLoadSegmentation
            segmentationNode = importer.loadLesion(path)
            self.assertEqual(slicer.mrmlScene.GetNumberOfNodesByClass("vtkMRMLSegmentationNode"), 1)
            self.assertEqual(slicer.mrmlScene.GetNumberOfNodesByClass("vtkMRMLLabelMapVolumeNode"), 0)
            self.assertEqual(self._voxels(segmentationNode), int(np.count_nonzero(lesion)))
            slicer.mrmlScene.Clear(0)
            slicer.util.loadLabelVolume = failingLoadLabelVolume
            with self.assertRaises(importer.RefusedFileError) as refused:
                importer.loadLesion(path)
            self.assertIn("too large for this computer's memory", str(refused.exception))
            assertSceneEmpty("MemoryError")
        finally:
            slicer.util.loadSegmentation, slicer.util.loadLabelVolume = originals

    #
    # Figures
    #

    def test_damaged_figure_is_refused_without_template(self):
        """A figure that cannot be read (0 bytes, or a JPEG named .png) is refused with its reason: no template or lesion is
        started and the bare Add Data dialog does not open.
        """
        from StudioLib import importer

        studioShell = self._applyStudio()
        studioShell.switchTo("library")
        self._pump()
        empty = os.path.join(self.directory, "fig_zero.png")
        open(empty, "wb").close()
        jpeg = self._writeFigure("fig_jpeg_named.jpg")
        renamed = os.path.join(self.directory, "fig_jpeg_named.png")
        os.replace(jpeg, renamed)
        shown = []
        addData = []
        original = (slicer.util.errorDisplay, slicer.util.warningDisplay, importer.openAddDataDialog)
        slicer.util.errorDisplay = slicer.util.warningDisplay = lambda text, **kw: shown.append(text)
        importer.openAddDataDialog = addData.append
        try:
            for path in (empty, renamed):
                del shown[:]
                result = importer.handleDrop([path], shell=studioShell)
                self._pump(5)
                name = os.path.basename(path)
                self.assertEqual(len(result.figures), 0, name)
                self.assertEqual(addData, [], name)
                self.assertEqual(len(shown), 1, name)
                self.assertIn(name, shown[0])
                self.assertEqual(slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"), [], name)
                self.assertEqual(slicer.util.getNodesByClass("vtkMRMLSegmentationNode"), [], name)
            self.assertIn("empty (0 bytes)", self._lastEmptyReason(empty))
        finally:
            slicer.util.errorDisplay, slicer.util.warningDisplay, importer.openAddDataDialog = original

    def test_damaged_png_figure_refused_without_error_rows(self):
        """A text file or a cut-off file named .png is refused with the box only: the volume readers never run on it, so no
        ERROR row is logged and the error badge stays down (as for a damaged NIfTI).
        """
        from StudioLib import importer

        studioShell = self._applyStudio()
        studioShell.switchTo("library")
        self._pump()
        textPath = os.path.join(self.directory, "fig_text.png")
        with open(textPath, "w") as stream:
            stream.write("this is not an image\n" * 20)
        good = self._writeFigure("fig_good.png")
        with open(good, "rb") as stream:
            data = stream.read()
        cut = os.path.join(self.directory, "fig_cut.png")
        with open(cut, "wb") as stream:
            stream.write(data[: len(data) // 2])
        import struct

        def tiff(width, height, stripBytes):
            entries = [(256, 4, width), (257, 4, height), (258, 3, 8), (259, 3, 1), (273, 4, 8), (277, 3, 1), (279, 4, stripBytes)]
            ifd = struct.pack("<H", len(entries)) + b"".join(struct.pack("<HHII", t, k, 1, v) for t, k, v in entries)
            return b"II*\x00" + struct.pack("<I", 8 + stripBytes) + b"\x00" * stripBytes + ifd + b"\x00\x00\x00\x00"

        lyingTiff = os.path.join(self.directory, "fig_lying.tif")
        cutTiff = os.path.join(self.directory, "fig_cut.tif")
        okTiff = os.path.join(self.directory, "fig_ok.tif")
        for name, data in ((lyingTiff, tiff(100000, 100000, 5000)), (cutTiff, tiff(50, 50, 2500)[:-30]), (okTiff, tiff(50, 50, 2500))):
            with open(name, "wb") as stream:
                stream.write(data)
        self.assertTrue(importer._tiffIsConsistent(okTiff))
        self.assertFalse(importer._tiffIsConsistent(lyingTiff))
        self.assertFalse(importer._tiffIsConsistent(cutTiff))
        model = slicer.app.errorLogModel()

        def errorRows():
            # the badge counts Warning-and-worse rows of these levels: Critical and Fatal count as well as Error
            return sum(1 for row in range(model.rowCount()) if str(model.index(row, 2).data()).lower() in ("critical", "fatal") or "rror" in str(model.index(row, 2).data()))

        shown = []
        original = (slicer.util.errorDisplay, slicer.util.warningDisplay)
        slicer.util.errorDisplay = slicer.util.warningDisplay = lambda text, **kw: shown.append(text)
        try:
            before = errorRows()
            for path in (textPath, cut, lyingTiff, cutTiff):
                result = importer.handleDrop([path], shell=studioShell)
                self._pump(5)
                self.assertEqual(len(result.figures), 0, path)
            self.assertEqual(len(shown), 4)
            self.assertEqual(errorRows(), before)
        finally:
            slicer.util.errorDisplay, slicer.util.warningDisplay = original

    @staticmethod
    def _lastEmptyReason(path):
        from StudioLib import importer

        shown = []
        original = slicer.util.warningDisplay
        slicer.util.warningDisplay = lambda text, **kw: shown.append(text)
        try:
            importer._routeFigure(path, None, importer.LoadResult())
        finally:
            slicer.util.warningDisplay = original
        return shown[0]

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
        importer.answerFigureConvention("neurological")
        # The Draw Figure card shows the answer at once (confirmed, the answered side)
        from StudioLib import figure as figureLib

        figureCard = figureLib._card
        self.assertIsNotNone(figureCard)
        self.assertEqual(figureCard.conventionState.text, "Confirmed for this paper")
        self.assertTrue(figureCard.conventionGroup.button(1).checked)
        importer.answerFigureConvention("radiological")
        self.assertTrue(figureCard.conventionGroup.button(0).checked)
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

    def test_figure_colour_volume_file_starts_template(self):
        """A colour picture stored as a one-slice volume file (NRRD) that a route of 3D Slicer loaded into every view
        still gets the template on the left to draw on, like a PNG figure (it is not itself the background).
        """
        from StudioLib import importer

        studioShell = self._applyStudio()
        path = os.path.join(self.directory, "fig_rgb2d.nrrd")
        pixels = np.random.RandomState(3).randint(0, 255, size=(120, 160, 3)).astype(np.uint8)
        header = (
            "NRRD0004\ntype: uchar\ndimension: 4\nsizes: 3 160 120 1\nkinds: vector domain domain domain\n"
            "encoding: raw\nspace: left-posterior-superior\nspace directions: none (1,0,0) (0,1,0) (0,0,1)\n"
            "space origin: (0,0,0)\n\n"
        )
        with open(path, "wb") as handle:
            handle.write(header.encode("ascii") + pixels.tobytes())
        loaded = slicer.util.loadVolume(path)  # shown as the background of every view, untagged
        self.assertIsNotNone(loaded)
        self.assertEqual(importer._referenceKind(loaded), "figure")
        result = importer.LoadResult()
        importer._routeReferenceNode(loaded, studioShell, result)
        self._pump(8)
        self.assertEqual(loaded.GetAttribute("Studio.Role"), "figure")
        template = self._background("Red")
        self.assertIsNotNone(template)
        self.assertEqual(template.GetAttribute("Studio.Role"), "template")
        self.assertEqual(self._background("Yellow").GetID(), loaded.GetID())
        self.assertGreaterEqual(len(slicer.util.getNodesByClass("vtkMRMLSegmentationNode")), 1)

    def test_grey_alpha_figure_is_one_grey_component(self):
        """A grey+alpha PNG figure loads as a 1-component grey volume (not a 2-component vector shown in false colours)."""
        from PIL import Image

        from StudioLib import importer

        path = os.path.join(self.directory, "fig_la.png")
        Image.new("LA", (40, 30), (120, 200)).save(path)
        node = importer.loadFigureVolume(path)
        self.assertIsNotNone(node)
        self.assertFalse(node.IsA("vtkMRMLVectorVolumeNode"))
        self.assertEqual(node.GetImageData().GetNumberOfScalarComponents(), 1)
        self.assertEqual(int(node.GetImageData().GetScalarComponentAsDouble(5, 5, 0, 0)), 120)

    def test_figure_keeps_scan_view_slice_and_zoom(self):
        """Opening a figure (Compare layout) leaves the slice and zoom the user was on in the scan view (USABILITY P4):
        only the figure is fitted, in Yellow.
        """
        from StudioLib import importer

        studioShell = self._applyStudio()
        scan = self._writeVolume("sub-12_T1w.nii.gz", GRID_SHIFTED)
        importer.loadFiles([scan], studioShell)
        self._pump(8)
        studioShell.switchTo("draw")
        self._pump(4)
        red = self.layoutManager.sliceWidget("Red")
        sliceNode = red.mrmlSliceNode()
        # The user moves to another slice and zooms in
        sliceNode.SetSliceOffset(sliceNode.GetSliceOffset() + 20.0)
        fieldOfView = list(sliceNode.GetFieldOfView())
        sliceNode.SetFieldOfView(fieldOfView[0] * 0.5, fieldOfView[1] * 0.5, fieldOfView[2])
        self._pump(4)
        offsetBefore, heightBefore = sliceNode.GetSliceOffset(), sliceNode.GetFieldOfView()[1]
        self._routeFigure(studioShell, fromLibrary=False)
        self._wait(500)
        self.assertEqual(self.layoutManager.layout, importer.COMPARE_LAYOUT_ID)
        sliceNode = self.layoutManager.sliceWidget("Red").mrmlSliceNode()
        self.assertAlmostEqual(sliceNode.GetSliceOffset(), offsetBefore, places=3)
        # The pane is narrower, so only the width of the field of view may change
        self.assertAlmostEqual(sliceNode.GetFieldOfView()[1], heightBefore, places=1)

    def test_toast_beside_figure_keeps_corner_text(self):
        """In Compare, a long consequence toast is beside the figure, above the corner text of the drawing view
        (its laterality cue, USABILITY P6), and its text is shortened with an ellipsis, never clipped.
        """
        from StudioLib import hud, importer

        studioShell = self._applyStudio()
        oldSize = self.mainWindow.size
        try:
            self.mainWindow.resize(1366, 768)
            self._pump()
            self._routeFigure(studioShell)
            self._pump()
            centralWidget = self.mainWindow.centralWidget()
            views = dict(hud.viewRects(centralWidget))
            red, figure = views["Red"], views[importer.FIGURE_VIEW_NAME]
            text = "Smooth removed 12% of the lesion · this is a much longer consequence message than usual, to fill the toast"
            studioShell.toast(text, level="consequence", action=lambda: None, actionText="Undo")
            self._pump()
            frame = studioShell._stickyToast.frame
            toastRect = qt.QRect(frame.mapTo(centralWidget, qt.QPoint(0, 0)), frame.size)
            # The bottom corner text of Red (two lines) and the figure are not covered
            cornerBand = qt.QRect(red.left(), red.bottom() - 40, 150, 41)
            self.assertFalse(toastRect.intersects(cornerBand), (toastRect, cornerBand))
            self.assertFalse(toastRect.intersects(figure), (toastRect, figure))
            # The whole text is kept (screen readers, toastText()); the label shows it shortened with an ellipsis
            self.assertEqual(studioShell.toastText(), text)
            label = studioShell._stickyToast.label
            self.assertTrue(label.text.endswith("…"), label.text)
            metrics = label.fontMetrics()
            advance = getattr(metrics, "horizontalAdvance", None)
            shownWidth = advance(label.text) if advance is not None else metrics.width(label.text)
            self.assertLessEqual(shownWidth, label.width, (shownWidth, label.width))
            # The plain toast too
            studioShell.toast(text)
            self._pump()
            plain = studioShell._toast.label
            plainRect = qt.QRect(plain.mapTo(centralWidget, qt.QPoint(0, 0)), plain.size)
            self.assertFalse(plainRect.intersects(cornerBand), (plainRect, cornerBand))
            self.assertFalse(plainRect.intersects(figure), (plainRect, figure))
            self.assertEqual(studioShell.toastText(), text)
        finally:
            self.mainWindow.resize(oldSize)
            self._pump()

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
        """A T map: dropped, it opens in Map (W3-08) over the template with the LNM colour table and no segmentation; loaded
        by a route of 3D Slicer, it is a two-sided thresholded overlay with a colour legend and one threshold control.
        """
        from StudioLib import importer, mappage

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

        # Dropped (or opened from Library): Map shows it
        result = importer.handleDrop([path], shell=studioShell)
        self._pump(8)
        self.assertEqual(len(result.statmaps), 1)
        mapNode = result.statmaps[0]
        self.assertEqual(mapNode.GetAttribute("Studio.Role"), "statmap")
        self.assertEqual(studioShell.currentWorkspace(), "map")
        self.assertEqual(self._composite("Red").GetForegroundVolumeID(), mapNode.GetID())
        self.assertAlmostEqual(self._composite("Red").GetForegroundOpacity(), mappage.MAP_OPACITY)
        self.assertEqual(self._background("Red").GetAttribute("Studio.Role"), "template")
        self.assertTrue(mapNode.GetDisplayNode().GetColorNode().GetName().startswith("LNM Diverging"))
        self.assertIsNone(importer.statMapOverlay(mapNode))
        # The Studio viewer extras (V-03): a colour legend and layer chips over the main view, gone when Map is left
        overlay = mappage.overlay()
        self.assertIsNotNone(overlay)
        mappage.refreshOverlay()
        self._pump(4)
        self.assertEqual(overlay.statisticLabel.text, "T")
        self.assertTrue(overlay.chipLabels["network"].text.startswith("Network T · "))
        pixmap = overlay.bar.pixmap
        pixmap = pixmap() if callable(pixmap) else pixmap  # a property in PythonQt
        self.assertFalse(pixmap.isNull())
        self.assertIs(overlay.legend.parent(), self.mainWindow.centralWidget())
        # The "Lesion outline" chip: a lesion-coloured outline square drawn as a swatch, not a text glyph (VISUAL 3, 4.5)
        lesionChip = overlay.chipLabels["lesion"]
        lesionChip.setText("Lesion outline")
        overlay._paintLesionSwatch()
        self.assertNotIn("\u25a1", lesionChip.text)
        swatch = overlay.lesionSwatch
        self.assertIs(swatch.parent(), lesionChip)
        pixmap = swatch.pixmap
        image = (pixmap() if callable(pixmap) else pixmap).toImage()
        lesionColor = qt.QColor(255, 90, 95)
        opaque = [(x, y) for x in range(image.width()) for y in range(image.height()) if image.pixelColor(x, y).alpha() > 200]
        self.assertGreater(len(opaque), 10)
        self.assertTrue(all(abs(image.pixelColor(x, y).red() - lesionColor.red()) < 8 for x, y in opaque))
        self.assertEqual(image.pixelColor(image.width() // 2, image.height() // 2).alpha(), 0, "an outline, not a filled square")
        studioShell.switchTo("library")
        self._pump(4)
        self.assertIsNone(mappage.overlay())
        studioShell.switchTo("map")
        self._pump(6)
        self.assertIsNotNone(mappage.overlay())
        for segmentation in slicer.util.getNodesByClass("vtkMRMLSegmentationNode"):
            role = slicer.vtkMRMLSegmentationNode.GetReferenceImageGeometryReferenceRole()
            self.assertNotEqual(segmentation.GetNodeReferenceID(role), mapNode.GetID())
        slicer.mrmlScene.Clear(0)
        self._pump(6)

        # Loaded by a route of 3D Slicer (not tagged): the importer's thresholded overlay, in Draw
        studioShell.switchTo("library")
        self._pump()
        loaded = slicer.util.loadVolume(path, {"singleFile": True, "show": False})
        result = importer.LoadResult()
        importer._routeReferenceNode(loaded, studioShell, result)
        self._pump(8)
        self.assertEqual(len(result.statmaps), 1)
        statmap = result.statmaps[0]
        self.assertIs(statmap, loaded)
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

    def test_statmap_left_drag_keeps_map_window(self):
        """In Map a window/level left-drag adjusts the template, never the map: the map's colour table encodes the
        threshold over a symmetric window (SPEC 4.12), so the views keep showing the voxels the legend names (bug bash
        map-left-drag-changes-statistical-map-window-breaks-threshold-display). Leaving Map gives the views back.
        """
        from StudioLib import importer, mappage

        windowLevelWidget = slicer.vtkMRMLWindowLevelWidget
        if not hasattr(windowLevelWidget, "SetLeftDragWindowLevel"):
            self.skipTest("this build has no Studio left-drag window/level (W3-02a)")
        studioShell = self._applyStudio()
        dims = GRID_2009C_2MM[0]
        z, y, x = np.mgrid[0:dims[2], 0:dims[1], 0:dims[0]]
        values = (8.0 * np.exp(-((x - 30) ** 2 + (y - 60) ** 2 + (z - 50) ** 2) / 60.0) - 6.0 * np.exp(-((x - 66) ** 2 + (y - 60) ** 2 + (z - 50) ** 2) / 60.0)).astype(np.float32)
        node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode")
        slicer.util.updateVolumeFromArray(node, values)
        node.SetIJKToRASMatrix(self._ijkToRas(GRID_2009C_2MM))
        path = os.path.join(self.directory, "sub-06_desc-T_funcmap.nii.gz")
        self.assertTrue(slicer.util.saveNode(node, path))
        slicer.mrmlScene.RemoveNode(node)
        studioShell.switchTo("library")
        self._pump()
        result = importer.handleDrop([path], shell=studioShell)
        self._pump(8)
        self.assertEqual(len(result.statmaps), 1)
        mapNode = result.statmaps[0]
        self.assertEqual(studioShell.currentWorkspace(), "map")
        self.assertEqual(self._composite("Red").GetForegroundVolumeID(), mapNode.GetID())
        leftDrag = windowLevelWidget.GetLeftDragWindowLevel()
        windowLevelWidget.SetLeftDragWindowLevel(True)
        try:
            slicer.app.applicationLogic().GetInteractionNode().SetCurrentInteractionMode(slicer.vtkMRMLInteractionNode.ViewTransform)
            mapDisplay = mapNode.GetDisplayNode()
            templateDisplay = self._background("Red").GetDisplayNode()
            mapWindowLevel = (mapDisplay.GetWindow(), mapDisplay.GetLevel())
            self.assertAlmostEqual(mapWindowLevel[1], 0.0)
            templateWindowLevel = (templateDisplay.GetWindow(), templateDisplay.GetLevel())
            # A plain left-drag over the brain (the map's box covers the whole template)
            self._stroke("Red", ((0.4, 0.5), (0.45, 0.52), (0.5, 0.55), (0.55, 0.58), (0.6, 0.6)))
            self.assertEqual((mapDisplay.GetWindow(), mapDisplay.GetLevel()), mapWindowLevel, "a left-drag in Map changed the map's window/level")
            self.assertNotEqual((templateDisplay.GetWindow(), templateDisplay.GetLevel()), templateWindowLevel, "a left-drag in Map did not adjust the template")
            # The explicit Adjust window/level mouse mode does not change the map either
            interactionNode = slicer.app.applicationLogic().GetInteractionNode()
            interactionNode.SetCurrentInteractionMode(slicer.vtkMRMLInteractionNode.AdjustWindowLevel)
            self._stroke("Green", ((0.4, 0.5), (0.5, 0.55), (0.6, 0.6)))
            interactionNode.SetCurrentInteractionMode(slicer.vtkMRMLInteractionNode.ViewTransform)
            self.assertEqual((mapDisplay.GetWindow(), mapDisplay.GetLevel()), mapWindowLevel)
            # Views of a new layout too
            self.layoutManager.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
            self._pump(6)
            managers = mappage._windowLevelManagers()
            self.assertTrue(managers)
            self.assertFalse(any(manager.GetAdjustForegroundWindowLevelEnabled() for manager in managers))
            # Leaving Map gives the views their foreground window/level back
            studioShell.switchTo("library")
            self._pump(4)
            self.assertTrue(all(manager.GetAdjustForegroundWindowLevelEnabled() for manager in mappage._windowLevelManagers()))
        finally:
            windowLevelWidget.SetLeftDragWindowLevel(leftDrag)

    #
    # Lesions without a scan
    #

    def test_line_icons_not_platform_icons(self):
        """VISUAL 3 (Lucide line icons throughout): the clear button of a search field and the folder button of the Saving
        settings carry Lucide icons, and the menu chevron of a text tool button sits vertically centered at its right
        side (not in the bottom-right corner under the text).
        """
        import ctk
        from StudioLib import ui

        edit = qt.QLineEdit()
        ui.setClearButton(edit)
        buttons = [b for b in edit.findChildren("QToolButton") if b.inherits("QLineEditIconButton")]
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].icon.cacheKey(), ui.icon("x", 14).cacheKey())

        directoryButton = ctk.ctkDirectoryButton()
        directoryButton.icon = ui.icon("folder", 16)
        self.assertEqual(directoryButton.icon.cacheKey(), ui.icon("folder", 16).cacheKey())

        button = qt.QToolButton()
        button.setText("Axial")
        button.setPopupMode(qt.QToolButton.InstantPopup)
        button.setMenu(qt.QMenu(button))
        button.setFixedHeight(32)
        button.setAutoRaise(True)
        width = button.sizeHint.width()
        plain = qt.QToolButton()
        plain.setText("Axial")
        self.assertGreater(width, plain.sizeHint.width() + 8, "room is reserved for the chevron")
        button.resize(width, 32)
        image = button.grab().toImage()
        background = image.pixelColor(1, 1)

        def differs(x, y):
            return abs(image.pixelColor(x, y).lightness() - background.lightness()) > 40

        rows = [y for y in range(image.height()) if any(differs(x, y) for x in range(image.width() - 10, image.width()))]
        self.assertTrue(rows, "the chevron is drawn in the strip at the right")
        center = image.height() / 2.0
        self.assertLess(abs((min(rows) + max(rows)) / 2.0 - center), 3.0, f"chevron rows {min(rows)}-{max(rows)} in {image.height()}")
        textColumns = [x for x in range(image.width() - 10) if any(differs(x, y) for y in range(image.height()))]
        self.assertTrue(textColumns and max(textColumns) < image.width() - 10, "the text stays left of the chevron strip")

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
        # The amber line is on the Draw Lesion card (W2-05: draw.showsLesionSpaceLine), not in an inspector notice
        self.assertNotEqual(studioShell.inspectorNoticeKey(), "lesion-space")
        self.assertIsNone(importer.noticeWidget("lesion-space"))
        self.assertEqual(str(self._lesionCardSpaceLine().text), line)
        self.assertTrue(self._lesionCardSpaceLine().isVisible())
        # Painting through the editor keeps the lesion grid
        editor.setActiveEffectByName("Paint")
        self._pump()
        self._stroke("Red")
        self.assertEqual(lesion.GetSegmentation().GetConversionParameter(geometryName), recorded)

    def test_single_slice_scan_draw_says_why(self):
        """A one-slice grayscale scan is shown but never a Draw source (a 2D image is not traced): the empty state says so
        instead of asking for a scan, and "Save lesion" has nothing to save; a 3D scan then traces as usual.
        """
        from StudioLib import importer

        studioShell = self._applyStudio()
        path = self._writeVolume("sub-20_T1w.nii.gz", ((40, 50, 1), 0.8, (1, 1, 1), (0.0, 0.0, 0.0)))
        importer.loadFiles([path], studioShell)
        self._pump(8)
        studioShell.switchTo("draw")
        self._pump(4)
        editor = self._editor()
        self.assertIsNone(editor.sourceVolumeNode())
        emptyState = self._drawEmptyState()
        self.assertTrue(emptyState.isVisible())
        text = str(emptyState.findChild("QLabel", "StudioDrawEmptyStateText").text)
        self.assertIn("sub-20_T1w", text)
        self.assertIn("single slice", text)
        self.assertNotIn("Open a scan in Library", text)
        saveButton = self.mainWindow.findChild("QPushButton", "StudioSaveLesionButton")
        self.assertIsNotNone(saveButton)
        self.assertFalse(saveButton.isEnabled())

        # Without it, the plain empty state comes back
        slicer.mrmlScene.Clear(0)
        self._pump(6)
        studioShell.switchTo("draw")
        self._pump(4)
        text = str(self._drawEmptyState().findChild("QLabel", "StudioDrawEmptyStateText").text)
        self.assertEqual(text, "Open a scan in Library or start on the MNI template")

    def test_colour_volume_draw_says_why(self):
        """A 3D colour (vector) volume is shown but never a Draw source: no empty lesion is created for it, Draw says why
        and "Save lesion" has nothing to save.
        """
        import numpy as np
        import SimpleITK as sitk
        from StudioLib import importer

        studioShell = self._applyStudio()
        array = np.zeros((6, 20, 24, 3), dtype=np.uint8)
        array[..., 0] = 200
        image = sitk.GetImageFromArray(array, isVector=True)
        path = os.path.join(self.directory, "atlas_rgb.mha")
        sitk.WriteImage(image, path)
        importer.loadFiles([path], studioShell)
        self._pump(8)
        self.assertEqual(len(list(slicer.util.getNodesByClass("vtkMRMLVectorVolumeNode"))), 1)
        self.assertEqual(len(list(slicer.util.getNodesByClass("vtkMRMLSegmentationNode"))), 0)
        studioShell.switchTo("draw")
        self._pump(4)
        text = str(self._drawEmptyState().findChild("QLabel", "StudioDrawEmptyStateText").text)
        self.assertIn("atlas_rgb", text)
        self.assertIn("colour image", text)
        self.assertNotIn("Open a scan in Library", text)
        saveButton = self.mainWindow.findChild("QPushButton", "StudioSaveLesionButton")
        self.assertFalse(saveButton.isEnabled())

    def test_native_lesion_offer_ends_with_the_scene(self):
        """Closing the scene clears Draw's "lesion is in scan space" offer: no button acts on the deleted lesion."""
        from StudioLib import draw, importer

        studioShell = self._applyStudio()
        path = self._writeMask("sub-04_lesion.nii.gz", GRID_SHIFTED, subdirectory="lesions")
        importer.loadFiles([path], studioShell)
        self._pump(8)
        self.assertIsNotNone(draw.extension()._nativeOffer)
        slicer.mrmlScene.Clear(0)
        self._pump(8)
        self.assertIsNone(draw.extension()._nativeOffer)
        self.assertFalse(slicer.util.getNodesByClass("vtkMRMLSegmentationNode"))
        emptyState = self._drawEmptyState()
        if emptyState is not None and emptyState.isVisible():
            text = str(emptyState.findChild("QLabel", "StudioDrawEmptyStateText").text)
            self.assertNotIn("scan space", text)

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
        # The Draw empty-state variant offers the scan (W2-05: draw.showNativeLesionState), not an inspector notice
        self.assertNotEqual(studioShell.inspectorNoticeKey(), "native-lesion")
        emptyState = self._drawEmptyState()
        self.assertTrue(emptyState.isVisible())
        text = str(emptyState.findChild("QLabel", "StudioDrawEmptyStateText").text)
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
        button = self._drawEmptyStateButton("Open with patient_T1.nii.gz")
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
        self.assertFalse(self._drawEmptyState().isVisible())
        self.assertEqual(self._voxels(lesion), 27)

        # "Show on the MNI template anyway": bound on request, with the amber line
        slicer.mrmlScene.Clear(0)
        result = importer.loadFiles([path], studioShell)
        self._pump(8)
        lesion = result.segmentations[0]
        self._drawEmptyStateButton("Show on the MNI template anyway").click()
        self._pump(8)
        self.assertEqual(editor.sourceVolumeNode().GetAttribute("Studio.Role"), "template")
        self.assertEqual(importer.lesionSpaceLine(lesion), "Shown on the MNI template; the lesion is in scan space")
        self.assertEqual(str(self._lesionCardSpaceLine().text), "Shown on the MNI template; the lesion is in scan space")

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

    def test_closed_scan_lesion_in_draw_offers_scan(self):
        """Library > Open in Draw on a lesion whose scan was closed shows the native-lesion state, and its scan opens with it."""
        from StudioLib import importer, library

        studioShell = self._applyStudio()
        scanPath = self._writeVolume("patient_T1.nii.gz", GRID_SHIFTED, subdirectory="anat")
        maskPath = self._writeMask("patient_T1_lesion.nii.gz", GRID_SHIFTED, subdirectory="anat")
        result = importer.loadFiles([scanPath, maskPath], studioShell)
        self._pump(8)
        lesion = result.segmentations[0]
        scan = slicer.util.getNode("patient_T1")
        editor = self._editor()
        self.assertEqual(editor.sourceVolumeNode().GetID(), scan.GetID())
        library.closeNode(scan, confirm=False)
        self._pump(4)
        library.continueInDraw(studioShell, lesion)
        self._pump(6)
        self.assertEqual(editor.segmentationNode().GetID(), lesion.GetID())
        self.assertIsNone(editor.sourceVolumeNode())
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        emptyState = self._drawEmptyState()
        self.assertTrue(emptyState.isVisible())
        text = str(emptyState.findChild("QLabel", "StudioDrawEmptyStateText").text)
        self.assertEqual(text, "This lesion is in scan space. Open the scan it was drawn on.")
        # Its "Open scan..." pairs the lesion with the scan, so Draw continues that lesion (no new empty one)
        nodesBefore = len(slicer.util.getNodesByClass("vtkMRMLSegmentationNode"))
        importer.openScanForLesion(lesion, scanPath, studioShell)
        self._pump(8)
        self.assertEqual(editor.segmentationNode().GetID(), lesion.GetID())
        self.assertIsNotNone(editor.sourceVolumeNode())
        self.assertEqual(len(slicer.util.getNodesByClass("vtkMRMLSegmentationNode")), nodesBefore)
        self.assertFalse(self._drawEmptyState().isVisible())

    def test_mni_lesion_dropped_on_native_scan_goes_to_template(self):
        """MNI masks opened while a scan in scan space is shown: never drawn over that scan, but on the template (as in an
        empty scene: one lesion with its space line, several as a loaded set); on a scan on an MNI grid they stay.
        """
        from StudioLib import importer, library

        studioShell = self._applyStudio()
        scanPath = self._writeVolume("patient_T1.nii.gz", GRID_SHIFTED, subdirectory="anat")
        lesionPath = self._writeMask("sub-03_lesion.nii.gz", GRID_NLIN6_2MM)
        importer.loadFiles([scanPath], studioShell)
        self._pump(8)
        scan = slicer.util.getNode("patient_T1")
        self.assertEqual(self._background("Red").GetID(), scan.GetID())
        result = importer.loadFiles([lesionPath], studioShell)
        self._pump(8)
        lesion = result.segmentations[0]
        editor = self._editor()
        self.assertEqual(editor.segmentationNode().GetID(), lesion.GetID())
        source = editor.sourceVolumeNode()
        self.assertIsNotNone(source)
        self.assertEqual(source.GetAttribute("Studio.Role"), "template")
        self.assertEqual(self._background("Red").GetID(), source.GetID())
        self.assertFalse(importer._isBoundTo(lesion, scan))
        self.assertNotEqual(library.lesionScan(lesion), scan)
        self.assertEqual(importer.lesionSpaceLine(lesion), "Lesion grid MNI152NLin6Asym 2 mm, shown on MNI152NLin2009cAsym (a few mm apart)")

        # Several masks: a loaded set on the template, one lesion shown at a time
        slicer.mrmlScene.Clear(0)
        importer.loadFiles([scanPath], studioShell)
        self._pump(8)
        paths = [self._writeMask(f"sub-{index:02d}_lesion.nii.gz", GRID_NLIN6_2MM, center=(30 + 4 * index, 50, 40), subdirectory="set") for index in range(1, 4)]
        importer.loadFiles(paths, studioShell)
        self._pump(8)
        loaded = importer.loadedSet()
        self.assertIsNotNone(loaded)
        nodes = loaded.nodes()
        self.assertEqual(len(nodes), 3)
        self.assertEqual([node.GetName() for node in nodes if node.GetDisplayNode().GetVisibility()], ["sub-01 lesion"])
        self.assertEqual(self._editor().sourceVolumeNode().GetAttribute("Studio.Role"), "template")

        # A scan of the user on an MNI grid (a normalized scan): the MNI lesion is drawn on it, as before
        slicer.mrmlScene.Clear(0)
        normalizedPath = self._writeVolume("sub-04_space-MNI152NLin6Asym_T1w.nii.gz", GRID_NLIN6_2MM, subdirectory="anat")
        importer.loadFiles([normalizedPath], studioShell)
        self._pump(8)
        normalized = slicer.util.getNode("sub-04_space-MNI152NLin6Asym_T1w")
        result = importer.loadFiles([lesionPath], studioShell)
        self._pump(8)
        lesion = result.segmentations[0]
        self.assertEqual(self._editor().segmentationNode().GetID(), lesion.GetID())
        self.assertEqual(self._editor().sourceVolumeNode().GetID(), normalized.GetID())
        self.assertEqual(self._background("Red").GetID(), normalized.GetID())
        # ... but a lesion of another BIDS subject (sub-03) is not bound to the scan of sub-04
        self.assertFalse(importer._isBoundTo(lesion, normalized))

    def test_command_line_files_take_studio_routes(self):
        """Files given on the command line (``Slicer scan.nii.gz lesion.nii.gz``) take Studio's import routes, not the core
        loader that reads every image as a plain volume: the Studio module turns the core handling off when Studio applies
        at startup, and opens them as a drop once Studio is applied. Classic keeps 3D Slicer's handling.
        """
        import Studio
        from StudioLib import importer

        app = slicer.app
        # The core handling can be turned off from Python (it is on in the application, off under --testing)
        self.assertTrue(hasattr(app, "setURIArgumentHandlingEnabled"))
        wasEnabled = app.isURIArgumentHandlingEnabled()
        scan = self._writeVolume("sub-01_T1w.nii.gz", GRID_SHIFTED)
        lesion = self._writeMask("sub-01_lesion.nii.gz", GRID_SHIFTED)

        class Options:
            def __init__(self, arguments, pythonScript=""):
                self.unparsedArguments = arguments
                self.pythonScript = pythonScript
                self.extraPythonScript = ""

        try:
            app.setURIArgumentHandlingEnabled(True)
            # Not taken over: Classic at startup, or a Python script (the arguments are its own)
            self.assertEqual(Studio.takeOverCommandLineArguments(willApply=False, options=Options([scan, lesion])), [])
            self.assertEqual(Studio.takeOverCommandLineArguments(willApply=True, options=Options([scan], "run.py")), [])
            self.assertTrue(app.isURIArgumentHandlingEnabled())
            self.assertIsNone(Studio._commandLineArguments)
            # Studio at startup: the core loader does not read them, Studio keeps them for after the apply
            self.assertEqual(Studio.takeOverCommandLineArguments(willApply=True, options=Options([scan, lesion])), [scan, lesion])
            self.assertFalse(app.isURIArgumentHandlingEnabled())
            self.assertEqual(Studio._commandLineArguments, [scan, lesion])
        finally:
            Studio._commandLineArguments = None
            app.setURIArgumentHandlingEnabled(wasEnabled)

        # Studio: the lesion is a lesion segmentation on its scan in Draw (not a grey scan over it); a missing file is ignored
        studioShell = self._applyStudio()
        warnings = []
        warningDisplay = slicer.util.warningDisplay
        slicer.util.warningDisplay = lambda text, *args, **kwargs: warnings.append(str(text))
        try:
            result = importer.openCommandLineArguments([scan, lesion, os.path.join(self.directory, "missing.nii.gz")], studioShell)
        finally:
            slicer.util.warningDisplay = warningDisplay
        self._pump(8)
        # The missing file is named, not ignored silently
        self.assertEqual(len(warnings), 1)
        self.assertIn("missing.nii.gz", warnings[0])
        self.assertIn("cannot be opened", warnings[0])
        self.assertIsNotNone(result)
        self.assertEqual(result.errors, [])
        scalars = lambda: sorted(node.GetName() for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")
                                 if node.GetClassName() == "vtkMRMLScalarVolumeNode")
        self.assertEqual(scalars(), ["sub-01_T1w"])
        self.assertEqual([node.GetName() for node in slicer.util.getNodesByClass("vtkMRMLSegmentationNode")], ["sub-01_lesion"])
        self.assertEqual(slicer.mrmlScene.GetNodeByID(self._composite("Red").GetBackgroundVolumeID()).GetName(), "sub-01_T1w")
        self.assertEqual(slicer.util.selectedModule(), "SegmentEditor")
        self.assertEqual(self._editor().segmentationNode().GetName(), "sub-01_lesion")
        # A web address is not "moved or deleted", and several problems are one box (not one modal each)
        warnings = []
        slicer.util.warningDisplay = lambda text, *args, **kwargs: warnings.append(str(text))
        try:
            importer.openCommandLineArguments(["does_not_exist.nii.gz", "https://example.org/data/scan.nii.gz"], studioShell)
            self.assertEqual(len(warnings), 1)
            self.assertIn("does_not_exist.nii.gz was moved or deleted", warnings[0])
            self.assertIn("web addresses", warnings[0])
            self.assertNotIn("scan.nii.gz was moved or deleted", warnings[0])
            warnings[:] = []
            importer.openCommandLineArguments(["https://example.org/a.nii.gz"], studioShell)
            self.assertEqual(len(warnings), 1)
            self.assertNotIn("moved or deleted", warnings[0])
            warnings[:] = []
            importer.openCommandLineArguments(["a_missing.nii.gz", "b_missing.nii.gz"], studioShell)
            self.assertEqual(len(warnings), 1)
            self.assertIn("a_missing.nii.gz", warnings[0])
            self.assertIn("b_missing.nii.gz", warnings[0])
        finally:
            slicer.util.warningDisplay = warningDisplay
        # A folder takes the Open folder... route (not the core loader's "Adding data failed" box)
        slicer.mrmlScene.Clear(0)
        self._pump()
        self._writeVolume("sub-02_T1w.nii.gz", GRID_SHIFTED, subdirectory="cli folder")
        calls = []
        loadFolder = importer.loadFolder
        importer.loadFolder = lambda path, shell=None: calls.append(path)
        try:
            importer.openCommandLineArguments([os.path.join(self.directory, "cli folder")], studioShell)
        finally:
            importer.loadFolder = loadFolder
        self.assertEqual(calls, [os.path.join(self.directory, "cli folder")])
        # Files that sit in a Slicer Studio project (a double-click on its scan) open that project, not a duplicate
        from StudioLib import projects as studioProjects
        projectFolder = os.path.join(self.directory, "Projects", "held project")
        os.makedirs(os.path.join(projectFolder, "Images"), exist_ok=True)
        for name in ("held project.mrml", os.path.join("Images", "held.nii.gz")):
            open(os.path.join(projectFolder, name), "w").close()
        calls = []
        loadFolder = importer.loadFolder
        handleDrop = importer.handleDrop
        projectsFolder = studioProjects.projectsFolder
        importer.loadFolder = lambda path, shell=None: calls.append(("folder", path))
        importer.handleDrop = lambda paths, addData=False, shell=None: calls.append(("drop", paths))
        studioProjects.projectsFolder = lambda rootPath=None: os.path.join(self.directory, "Projects")
        try:
            inside = os.path.join(projectFolder, "Images", "held.nii.gz")
            importer.openCommandLineArguments([inside], studioShell)
            importer.openCommandLineArguments([scan, inside], studioShell)
        finally:
            importer.loadFolder = loadFolder
            importer.handleDrop = handleDrop
            studioProjects.projectsFolder = projectsFolder
        self.assertEqual(calls[0], ("folder", projectFolder))
        self.assertEqual(calls[1][0], "drop")  # a mix with a file from elsewhere is not one project

        studioShell.dispose()
        self._pump()

        # Classic: 3D Slicer's core loader, unchanged (both images are volumes)
        slicer.mrmlScene.Clear(0)
        self._pump()
        self.assertIsNone(importer.openCommandLineArguments([scan, lesion]))
        self._pump()
        self.assertEqual(scalars(), ["sub-01_T1w", "sub-01_lesion"])
        self.assertFalse(slicer.util.getNodesByClass("vtkMRMLSegmentationNode"))

    def test_files_of_a_closed_project_open_that_project(self):
        """A scan or lesion of a Slicer Studio project that is not open, dropped or chosen with Open..., opens that project
        in place (A-09), not a second project (or the open one) with copies of its files. Files of the open project, a mix
        with a file from elsewhere and a scan opened for a lesion ("Open with") keep their routes.
        """
        from StudioLib import autosave
        from StudioLib import importer
        from StudioLib import projects as studioProjects

        studioShell = self._applyStudio()
        projectFolder = os.path.join(self.directory, "Projects", "closed project")
        scan = self._writeVolume("sub-09_T1w.nii.gz", GRID_SHIFTED, subdirectory=os.path.join("Projects", "closed project", "Images"))
        lesion = self._writeMask("sub-09_lesion.nii.gz", GRID_SHIFTED, subdirectory=os.path.join("Projects", "closed project", "Segmentations"))
        open(os.path.join(projectFolder, "closed project.mrml"), "w").close()
        outside = self._writeVolume("sub-10_T1w.nii.gz", GRID_SHIFTED)

        class OpenProject:
            path = projectFolder

        class Service:
            project = None

            def currentProject(self):
                return self.project

            def rootUnwritable(self):
                return False

        service = Service()
        calls = []
        saved = (importer.loadFolder, importer._loadScene, studioProjects.projectsFolder, autosave.instance, qt.QFileDialog.getOpenFileNames)
        importer.loadFolder = lambda path, shell=None: calls.append(("folder", path))
        importer._loadScene = lambda path, shell: calls.append(("scene", path)) or True
        studioProjects.projectsFolder = lambda rootPath=None: os.path.join(self.directory, "Projects")
        autosave.instance = lambda: service
        qt.QFileDialog.getOpenFileNames = lambda *args, **kwargs: [scan]
        try:
            # Drop of the scan, drop of the lesion, Open... of the scan, and the scan with its lesion: the project opens
            for route in (lambda: importer.handleDrop([scan], shell=studioShell), lambda: importer.handleDrop([lesion], shell=studioShell),
                          lambda: importer.openFilesDialog(studioShell), lambda: importer.loadFiles([scan, lesion], studioShell)):
                del calls[:]
                result = route()
                self.assertEqual(calls, [("folder", projectFolder)])
                self.assertEqual(result.scenes, [os.path.join(projectFolder, "closed project.mrml")])
                self.assertFalse(result.nodes())
            self._pump()
            # The Library caption asks the (stubbed) service too: the stub must answer the whole API it is asked, or the Qt
            # event filter swallows an AttributeError and logs a traceback while the suite still passes
            for name in ("currentProject", "rootUnwritable"):
                self.assertTrue(callable(getattr(service, name, None)), name)
            # Not that project: a mix with a file from elsewhere, a scan opened for its lesion, the open project's own scan
            del calls[:]
            self.assertIsNone(importer._closedProjectHoldingAll([scan, outside], ["volume", "volume"]))
            self.assertIsNone(importer._closedProjectHoldingAll([os.path.join(projectFolder, "closed project.mrml")], ["scene"]))
            service.project = OpenProject()
            self.assertIsNone(importer._closedProjectHoldingAll([scan], ["volume"]))
            service.project = None
            self.assertEqual(importer._closedProjectHoldingAll([scan], ["volume"]), projectFolder)
            result = importer.loadFiles([scan], studioShell, pairedLesion=slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode"))
            self.assertEqual(calls, [])
            self.assertEqual(len(result.volumes), 1)
        finally:
            importer.loadFolder, importer._loadScene, studioProjects.projectsFolder, autosave.instance, qt.QFileDialog.getOpenFileNames = saved
        self._pump()
        studioShell.dispose()
        self._pump()

    def test_studio_folder_argument_shows_the_library(self):
        """The Slicer Studio folder or its Projects folder as an argument is recognised, not offered to Add Data empty."""
        from StudioLib import importer
        from StudioLib import projects as studioProjects

        studioShell = self._applyStudio()
        rootPath = os.path.join(self.directory, "StudioRoot")
        os.makedirs(os.path.join(rootPath, "Projects", "p one"), exist_ok=True)
        calls = []
        openAddData = importer.openAddDataDialog
        root = studioProjects.root
        importer.openAddDataDialog = lambda paths=None, *a, **k: calls.append(paths)
        studioProjects.root = lambda: rootPath
        try:
            for folder in (rootPath, os.path.join(rootPath, "Projects")):
                self.assertEqual(importer.loadFolder(folder, studioShell), "library")
                self.assertEqual(studioShell.currentWorkspace(), "library")
        finally:
            importer.openAddDataDialog = openAddData
            studioProjects.root = root
        self.assertEqual(calls, [])

    def test_file_open_event_opens_the_file(self):
        """macOS gives Finder's Open With, a double click and a Dock drop as QFileOpenEvent: the file opens like Open... does."""
        import Studio

        scan = self._writeVolume("sub-01_T1w.nii.gz", GRID_SHIFTED)
        lesion = self._writeMask("sub-01_lesion.nii.gz", GRID_SHIFTED)
        studioShell = self._applyStudio()
        wasStarted = Studio._startupCompleted
        Studio.installFileOpenFilter()
        try:
            # Before startup is completed the path is kept, not lost
            Studio._startupCompleted = False
            self.assertTrue(qt.QApplication.sendEvent(slicer.app, qt.QFileOpenEvent(scan)))
            self._pump(4)
            self.assertEqual(Studio._pendingFileOpens, [scan])
            self.assertFalse(slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"))
            Studio._startupCompleted = True
            Studio._openPendingFileOpens()
            self._pump(8)
            self.assertEqual(Studio._pendingFileOpens, [])
            self.assertEqual([node.GetName() for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")], ["sub-01_T1w"])
            # With the scan open, a lesion is a lesion segmentation on it (the routing of Open...)
            self.assertTrue(qt.QApplication.sendEvent(slicer.app, qt.QFileOpenEvent(lesion)))
            self._wait(Studio._FILE_OPEN_BATCH_MS + 400)
            self.assertIn("sub-01_lesion", [node.GetName() for node in slicer.util.getNodesByClass("vtkMRMLSegmentationNode")])
            self.assertEqual([node.GetName() for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")], ["sub-01_T1w"])
        finally:
            Studio._startupCompleted = wasStarted
            del Studio._pendingFileOpens[:]
            studioShell.dispose()
            self._pump()

    def test_file_open_events_of_one_selection_open_as_one_batch(self):
        """Finder sends one QFileOpenEvent per selected file: a scan and its lesion open together, with no empty lesion."""
        import Studio

        scan = self._writeVolume("sub-01_T1w.nii.gz", GRID_SHIFTED)
        lesion = self._writeMask("sub-01_lesion.nii.gz", GRID_SHIFTED)
        studioShell = self._applyStudio()
        wasStarted = Studio._startupCompleted
        Studio.installFileOpenFilter()
        try:
            Studio._startupCompleted = True
            self.assertTrue(qt.QApplication.sendEvent(slicer.app, qt.QFileOpenEvent(scan)))
            self.assertTrue(qt.QApplication.sendEvent(slicer.app, qt.QFileOpenEvent(lesion)))
            self._wait(Studio._FILE_OPEN_BATCH_MS + 800)
            self.assertEqual(Studio._pendingFileOpens, [])
            self.assertEqual([node.GetName() for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")], ["sub-01_T1w"])
            self.assertEqual([node.GetName() for node in slicer.util.getNodesByClass("vtkMRMLSegmentationNode")], ["sub-01_lesion"])
        finally:
            Studio._startupCompleted = wasStarted
            del Studio._pendingFileOpens[:]
            studioShell.dispose()
            self._pump()

    def test_two_subjects_one_load_each_lesion_on_its_scan(self):
        """Two subjects opened at once (scan and lesion each): every lesion is bound to its own scan, and Draw edits it there."""
        from StudioLib import importer, library

        studioShell = self._applyStudio()
        gridOther = ((80, 96, 60), 1.5, (1, 1, 1), (-60.0, -72.0, -45.0))
        paths = {
            "sub-01_T1w": self._writeVolume("sub-01_T1w.nii.gz", GRID_SHIFTED),
            "sub-01_lesion": self._writeMask("sub-01_lesion.nii.gz", GRID_SHIFTED),
            "sub-02_T1w": self._writeVolume("sub-02_T1w.nii.gz", gridOther),
            "sub-02_lesion": self._writeMask("sub-02_lesion.nii.gz", gridOther, center=(30, 40, 20)),
            "sub-03_lesion": self._writeMask("sub-03_lesion.nii.gz", GRID_SHIFTED, center=(20, 20, 20)),
        }
        role = slicer.vtkMRMLSegmentationNode.GetReferenceImageGeometryReferenceRole()
        node = slicer.util.getNode
        reference = lambda name: (node(name).GetNodeReference(role).GetName() if node(name).GetNodeReference(role) is not None else None)
        background = lambda: slicer.mrmlScene.GetNodeByID(self._composite("Red").GetBackgroundVolumeID()).GetName()
        for order in (("01", "02"), ("02", "01")):
            slicer.mrmlScene.Clear(0)
            self._pump()
            importer.loadFiles([paths[f"sub-{subject}_{kind}"] for subject in order for kind in ("T1w", "lesion")], studioShell)
            self._pump(8)
            first, second = order
            self.assertEqual(reference(f"sub-{first}_lesion"), f"sub-{first}_T1w")
            self.assertEqual(reference(f"sub-{second}_lesion"), f"sub-{second}_T1w")
            self.assertEqual(library.lesionScan(node(f"sub-{second}_lesion")).GetName(), f"sub-{second}_T1w")
            # Open now > Open in Draw on the second subject's lesion: on its own scan
            library.continueInDraw(studioShell, segmentationNode=node(f"sub-{second}_lesion"))
            self._pump(8)
            self.assertEqual(self._editor().segmentationNode().GetName(), f"sub-{second}_lesion")
            self.assertEqual(self._editor().sourceVolumeNode().GetName(), f"sub-{second}_T1w")
            self.assertEqual(background(), f"sub-{second}_T1w")
        # Two scans and only the second subject's lesion: Draw shows the lesion on its own scan at once
        slicer.mrmlScene.Clear(0)
        self._pump()
        importer.loadFiles([paths["sub-01_T1w"], paths["sub-02_T1w"], paths["sub-02_lesion"]], studioShell)
        self._pump(8)
        self.assertEqual(reference("sub-02_lesion"), "sub-02_T1w")
        self.assertEqual(self._editor().segmentationNode().GetName(), "sub-02_lesion")
        self.assertEqual(self._editor().sourceVolumeNode().GetName(), "sub-02_T1w")
        self.assertEqual(background(), "sub-02_T1w")
        # A lesion of another BIDS subject is never bound to a scan just because it shares the scan's grid
        slicer.mrmlScene.Clear(0)
        self._pump()
        importer.loadFiles([paths["sub-01_T1w"], paths["sub-01_lesion"], paths["sub-03_lesion"]], studioShell)
        self._pump(8)
        self.assertEqual(reference("sub-01_lesion"), "sub-01_T1w")
        self.assertIsNone(reference("sub-03_lesion"))
        # ... also when it is the only lesion of the load (with the scan, or dropped alone while the scan is open): it is not
        # edited nor shown on the other subject's scan, and the user is told
        for together in (True, False):
            slicer.mrmlScene.Clear(0)
            self._pump()
            if together:
                importer.loadFiles([paths["sub-01_T1w"], paths["sub-03_lesion"]], studioShell)
            else:
                importer.loadFiles([paths["sub-01_T1w"]], studioShell)
                self._pump(8)
                importer.loadFiles([paths["sub-03_lesion"]], studioShell)
            self._pump(8)
            self.assertIsNone(reference("sub-03_lesion"))
            self.assertIsNone(library.lesionScan(node("sub-03_lesion")))
            self.assertFalse(self._editor().segmentationNode().GetName() == "sub-03_lesion"
                             and self._editor().sourceVolumeNode() is not None)
            self.assertFalse(node("sub-03_lesion").GetDisplayNode().GetVisibility())
            self.assertEqual(background(), "sub-01_T1w")
            self.assertIn("sub-03_lesion is a lesion of sub-03", " ".join(text for _time, text, _level in studioShell.recentMessages()[-3:]))

    def test_two_patient_folders_one_load_each_lesion_on_its_folders_scan(self):
        """Two patients opened at once with generic names on one grid (patient01/T1 + lesion, patient02/T1 + lesion): the
        folder is the only pairing, so every lesion is bound to the scan of its own folder, never to the first scan loaded.
        """
        from StudioLib import importer, library

        studioShell = self._applyStudio()
        paths = {}
        for patient, center in (("patient01", (45, 50, 40)), ("patient02", (30, 40, 20))):
            paths[f"{patient}/T1"] = self._writeVolume("T1.nii.gz", GRID_SHIFTED, subdirectory=f"patients/{patient}")
            paths[f"{patient}/lesion"] = self._writeMask("lesion.nii.gz", GRID_SHIFTED, center=center, subdirectory=f"patients/{patient}")
        role = slicer.vtkMRMLSegmentationNode.GetReferenceImageGeometryReferenceRole()
        folder = lambda node: os.path.basename(os.path.dirname(node.GetStorageNode().GetFileName()))
        for order in (("patient01", "patient02"), ("patient02", "patient01")):
            slicer.mrmlScene.Clear(0)
            self._pump()
            importer.loadFiles([paths[f"{patient}/{kind}"] for patient in order for kind in ("T1", "lesion")], studioShell)
            self._pump(8)
            lesions = [node for node in slicer.util.getNodesByClass("vtkMRMLSegmentationNode") if not node.GetHideFromEditors()]
            self.assertEqual(sorted(folder(lesion) for lesion in lesions), ["patient01", "patient02"])
            for lesion in lesions:
                scan = lesion.GetNodeReference(role)
                self.assertIsNotNone(scan, folder(lesion))
                self.assertEqual(folder(scan), folder(lesion))
                self.assertEqual(folder(library.lesionScan(lesion)), folder(lesion))

    def test_sagittal_scan_lesion_gaps_along_view_axis(self):
        """A sagittal 3D acquisition (k = left-right): the Lesion card and participants.tsv count the gap slices along
        the view's slicing axis (S for the axial Red view), not along k, and N jumps to the slices the card counts
        (SPEC 5.6).
        """
        from StudioLib import draw, importer, lesioncard, study_ui

        studioShell = self._applyStudio()
        # i = A, j = S, k = R; 1 mm voxels
        matrix = vtk.vtkMatrix4x4()
        for row, column in ((0, 0), (1, 1), (2, 2)):
            matrix.SetElement(row, column, 0.0)
        for row, column in ((0, 2), (1, 0), (2, 1)):
            matrix.SetElement(row, column, 1.0)
        for row, origin in enumerate((-15.0, -20.0, -20.0)):
            matrix.SetElement(row, 3, origin)
        ijkToRas = np.array([[matrix.GetElement(row, column) for column in range(4)] for row in range(4)])
        # Two slabs, two empty axial slices between them (j = 16 and 17: S = -4 and -3 mm)
        mask = np.zeros((30, 40, 40), np.uint8)
        mask[5:12, 10:16, 15:22] = 1
        mask[5:12, 18:24, 15:22] = 1
        # participants.tsv: along S
        self.assertEqual(study_ui.metricsValues(mask, ijkToRas)["gaps"], 2)

        scan = np.full(mask.shape, 300, np.int16)
        scan[mask != 0] = 120
        paths = [self._writeVolume("sub-31_T1w.nii.gz", None, scan, ijkToRas=matrix),
                 self._writeVolume("sub-31_lesion.nii.gz", None, mask, labelmap=True, ijkToRas=matrix)]
        importer.loadFiles(paths, studioShell)
        self._pump(8)
        editor = self._editor()
        segmentationNode = editor.segmentationNode()
        self.assertEqual(segmentationNode.GetName(), "sub-31_lesion")
        self.assertEqual(editor.sourceVolumeNode().GetName(), "sub-31_T1w")
        self._wait(800)
        self._pump()
        self.assertEqual(self._sliceNode("Red").GetOrientation(), "Axial")
        self.assertEqual(len(draw.viewGaps(draw.lesionMetrics(segmentationNode, compute=True))), 2)
        card = draw.extension().card
        self.assertEqual(card._state["stats"]["gaps"], 2)
        self.assertIn("2 gaps", card.fixesLabel.text)
        self.assertIn("Fill", card.fixesLabel.text)
        # N in the Red view goes to a slice the card counts: S = -4 or -3 mm
        self.assertIsNotNone(draw.jumpToGap(1, studioShell, "Red"))
        self.assertIn(round(self._sliceNode("Red").GetSliceOffset()), (-4, -3))
        # The metrics give the gaps along every array axis; the view picks its own
        self.assertEqual(lesioncard.nearestArrayAxis(ijkToRas, (0.0, 0.0, 1.0)), (1, 1.0))
        metrics = lesioncard.computeLesionMetrics(mask, None, ijkToRas)
        self.assertEqual(metrics["gaps"], [])  # along k (left-right): none
        self.assertEqual(metrics["gaps_by_axis"], ([], [16, 17], []))

    def test_lesion_card_volume_is_never_in_exponent_notation(self):
        """One 0.4 mm voxel (6.4e-05 mL) reads "0.000064 mL", not "6.4e-05 mL"."""
        from StudioLib import lesioncard

        self.assertEqual(lesioncard.formatMl(0.4**3 / 1000.0), "0.000064 mL")
        self.assertEqual(lesioncard.formatMl(0.042), "0.042 mL")
        self.assertEqual(lesioncard.formatMl(2.274), "2.27 mL")
        self.assertEqual(lesioncard.formatMl(0.0), "0 mL")
        self.assertEqual(lesioncard.formatMl(1e-9), "<0.000001 mL")
        for ml in (0.099, 0.0099, 1e-4, 9.96e-5, 2.7e-5, 1.5e-6, 1e-6):
            self.assertNotIn("e", lesioncard.formatMl(ml), ml)

    def test_gaps_follow_the_shown_view_when_red_is_hidden(self):
        """With the Sagittal view maximized the Red view is hidden: N moves the Sagittal view to a gap slice that the
        card counts, along the x axis of that view (not the hidden Axial view, not S).
        """
        from StudioLib import draw, importer, layouts

        studioShell = self._applyStudio()
        # i = A, j = S, k = R; 1 mm voxels (a sagittal 3D acquisition)
        matrix = vtk.vtkMatrix4x4()
        for row, column in ((0, 0), (1, 1), (2, 2)):
            matrix.SetElement(row, column, 0.0)
        for row, column in ((0, 2), (1, 0), (2, 1)):
            matrix.SetElement(row, column, 1.0)
        for row, origin in enumerate((-15.0, -20.0, -20.0)):
            matrix.SetElement(row, 3, origin)
        # Two slabs with four empty sagittal slices between them (k = 8..11: x = -7..-4 mm), no gap along S or A
        mask = np.zeros((30, 40, 40), np.uint8)
        mask[5:8, 10:24, 15:22] = 1
        mask[12:15, 10:24, 15:22] = 1
        scan = np.full(mask.shape, 300, np.int16)
        scan[mask != 0] = 120
        paths = [self._writeVolume("sub-32_T1w.nii.gz", None, scan, ijkToRas=matrix),
                 self._writeVolume("sub-32_lesion.nii.gz", None, mask, labelmap=True, ijkToRas=matrix)]
        importer.loadFiles(paths, studioShell)
        self._pump(8)
        self._wait(800)
        self._pump()
        segmentationNode = self._editor().segmentationNode()
        metrics = draw.lesionMetrics(segmentationNode, compute=True)
        self.assertEqual([len(gaps) for gaps in metrics["gaps_by_axis"]], [4, 0, 0])
        # The layout shows Red: gaps along S, none
        self.assertEqual(draw.gapViewName(studioShell), "Red")
        self.assertEqual(len(draw.viewGaps(metrics)), 0)
        # Sagittal maximized: Red is hidden, the card and N follow the Sagittal view
        self.assertEqual(layouts.maximizeView("Yellow"), "Yellow")
        self._pump(8)
        self.assertFalse(slicer.app.layoutManager().sliceWidget("Red").isVisible())
        self.assertEqual(draw.gapViewName(studioShell), "Yellow")
        self.assertEqual(len(draw.viewGaps(metrics)), 4)
        self._wait(300)
        self._pump()
        self.assertEqual(draw.extension().card._state["stats"]["gaps"], 4)
        redBefore = self._sliceNode("Red").GetSliceOffset()
        yellow = self._sliceNode("Yellow")
        yellow.JumpSliceByOffsetting(-14.0, 0.0, 0.0)
        self._pump()
        target = draw.jumpToGap(1, studioShell)
        self.assertIsNotNone(target)
        self.assertIn(round(yellow.GetSliceOffset()), (4, 5, 6, 7))
        self.assertEqual(self._sliceNode("Red").GetSliceOffset(), redBefore, "the hidden Red view does not move")
        # Back to the default layout: Red again
        layouts.restoreMaximized()
        self._pump(8)
        self.assertEqual(draw.gapViewName(studioShell), "Red")

        # Red reoriented to Sagittal: the card counts the gaps along the new slicing axis, without an edit
        red = self._sliceNode("Red")
        self._wait(300)
        self._pump()
        self.assertEqual(draw.extension().card._state["stats"]["gaps"], 0)
        red.SetOrientation("Sagittal")
        self._wait(300)
        self._pump()
        self.assertEqual(draw.extension().card._state["stats"]["gaps"], 4, "the card follows the Red view's orientation")
        red.SetOrientation("Axial")
        self._wait(300)
        self._pump()
        self.assertEqual(draw.extension().card._state["stats"]["gaps"], 0)

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

    def test_sampledata_second_load_is_already_open(self):
        """The same sample loaded a second time (Sample Data reads its cache file again) is not added twice: the open scan
        becomes current again and keeps its lesion.
        """
        from StudioLib import importer

        studioShell = self._applyStudio()
        path = self._writeVolume("MRHeadTwice.nrrd", GRID_SHIFTED)
        slicer.util.selectModule("SampleData")
        self._pump(4)
        first = slicer.util.loadVolume(path)
        self._wait(importer.TRANSIENT_LOAD_WINDOW_MS + 300)
        editor = self._editor()
        lesion = editor.segmentationNode()
        self.assertEqual(editor.sourceVolumeNode().GetID(), first.GetID())
        volumeCount = len(slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"))
        segmentationCount = len(slicer.util.getNodesByClass("vtkMRMLSegmentationNode"))
        studioShell.switchTo("library")
        slicer.util.selectModule("SampleData")
        self._pump(4)
        slicer.util.loadVolume(path)
        self._wait(importer.TRANSIENT_LOAD_WINDOW_MS + 300)
        self.assertEqual(len(slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")), volumeCount)
        self.assertEqual(len(slicer.util.getNodesByClass("vtkMRMLSegmentationNode")), segmentationCount)
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        self.assertEqual(editor.sourceVolumeNode().GetID(), first.GetID())
        self.assertEqual(editor.segmentationNode().GetID(), lesion.GetID())

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

    def test_template_offer_keeps_its_height_after_a_classic_round_trip(self):
        """The 1 mm offer is not left as a tall empty box in its capped notice slot after Classic and back."""
        from StudioLib import importer

        if importer.cachedTemplatePath(importer.TEMPLATE_SAMPLE_1MM) is not None:
            self.skipTest("the 1 mm template is cached")
        qt.QSettings().remove(importer.TEMPLATE_1MM_OFFER_KEY)
        studioShell = self._applyStudio()
        volume, segmentation = importer.startOnTemplate(shell=studioShell)
        self._pump(6)
        segmentId = segmentation.GetSegmentation().GetNthSegmentID(0)
        array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentation, segmentId, volume)
        array[40:42, 50:52, 45:47] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentation, segmentId, volume)
        self._wait(600)
        offer = importer.templateOfferWidget()
        self.assertIsNotNone(offer)
        before = offer.height
        studioShell.classic(persist=False)
        self._pump(6)
        studioShell.studio()
        self._pump(6)
        studioShell.switchTo("draw")
        self._wait(600)
        offer = importer.templateOfferWidget()
        self.assertIsNotNone(offer)
        slot = self.mainWindow.findChild("QWidget", "StudioInspectorNotice")
        self.assertTrue(offer.isVisible())
        self.assertLessEqual(offer.height, slot.height, "the offer is %d px tall in a %d px slot" % (offer.height, slot.height))
        self.assertEqual(offer.height, before)
        importer._closeTemplateBanner()
        self._pump()

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

    def test_reopen_scan_continues_bound_lesion_when_draw_edits_nothing(self):
        """BB-1: a scan that is already open, opened again (Open..., Recent) while Draw edits nothing (as after its
        project is reopened), continues the lesion bound to it, whatever the lesion's name: no new empty "Lesions (...)".
        """
        from StudioLib import importer

        studioShell = self._applyStudio()
        scanPath = self._writeVolume("sub-09_T1w.nii.gz", GRID_SHIFTED)
        maskPath = self._writeMask("sub-09_lesion.nii.gz", GRID_SHIFTED)
        importer.loadFiles([scanPath, maskPath], studioShell)
        self._pump(8)
        editor = self._editor()
        lesion = editor.segmentationNode()
        scan = editor.sourceVolumeNode()
        self.assertEqual(lesion.GetName(), "sub-09_lesion")
        self.assertTrue(importer._isBoundTo(lesion, scan))
        voxels = self._voxels(lesion)
        self.assertGreater(voxels, 0)
        for route in ("open", "recent"):
            # The reopened project: the scan is Draw's source volume, no segmentation is edited
            editor.setSegmentationNode(None)
            self._pump()
            self.assertIsNone(editor.segmentationNode())
            studioShell.switchTo("library")
            self._pump()
            count = slicer.mrmlScene.GetNumberOfNodes()
            if route == "open":
                result = importer.loadFiles([scanPath], studioShell)
            else:
                result = importer.loadRecentFile({"fileName": scanPath, "fileType": "VolumeFile"}, None, studioShell)
            self._pump(6)
            self.assertEqual([node.GetID() for node in result.alreadyOpen], [scan.GetID()], route)
            self.assertEqual(slicer.mrmlScene.GetNumberOfNodes(), count, route)
            self.assertEqual(editor.segmentationNode().GetID(), lesion.GetID(), route)
            self.assertEqual(editor.sourceVolumeNode().GetID(), scan.GetID(), route)
            self.assertEqual(self._voxels(lesion), voxels, route)
            self.assertIsNone(slicer.mrmlScene.GetFirstNodeByName(importer.scanLesionSegmentationName(scan)), route)
            self.assertEqual(studioShell.currentWorkspace(), "draw", route)

    def test_lesion_open_as_label_map_is_not_loaded_again(self):
        """BB-1: a lesion file that Classic's Add Data opened as a label map, opened again in Studio (drop, Open...,
        Recent), is already open: the label map becomes the lesion ("<name> is already open"), no second copy is loaded.
        """
        from StudioLib import importer, shell

        scanPath = self._writeVolume("sub-05_T1w.nii.gz", GRID_SHIFTED)
        maskPath = self._writeMask("sub-05_lesion.nii.gz", GRID_SHIFTED)
        for route in ("open", "recent"):
            if shell._shell is not None:
                shell._shell.dispose()
            slicer.mrmlScene.Clear(0)
            self._pump()
            # Classic: 3D Slicer's Add Data reads the lesion as a label map
            scan = slicer.util.loadVolume(scanPath)
            labelMap = slicer.util.loadLabelVolume(maskPath)
            studioShell = self._applyStudio()
            if route == "open":
                result = importer.loadFiles([maskPath], studioShell)
            else:
                result = importer.loadRecentFile({"fileName": maskPath, "fileType": "VolumeFile", "labelmap": True}, None,
                                                 studioShell)
            self._pump(6)
            lesions = slicer.util.getNodesByClass("vtkMRMLSegmentationNode")
            self.assertEqual([node.GetName() for node in lesions], ["sub-05_lesion"], route)
            self.assertEqual([node.GetID() for node in result.alreadyOpen], [lesions[0].GetID()], route)
            self.assertEqual(result.segmentations, [], route)
            self.assertIsNone(slicer.mrmlScene.GetNodeByID(labelMap.GetID()), route)
            self.assertEqual(slicer.util.getNodesByClass("vtkMRMLLabelMapVolumeNode"), [], route)
            self.assertGreater(self._voxels(lesions[0]), 0, route)
            self.assertEqual(studioShell.toastText(), "sub-05_lesion is already open", route)
            self.assertTrue(importer._isBoundTo(lesions[0], scan), route)

    def test_scan_dropped_after_its_lesion_is_the_lesion_scan(self):
        """BB-1: a lesion opened alone (unbound), then its own scan dropped (same grid, and same BIDS subject or, with
        generic names, same folder): the lesion is bound to the scan and Draw edits it, as when both are dropped together
        (no new empty "Lesions (<scan>)"). The scan of another BIDS subject on the same grid never adopts it.
        """
        from StudioLib import importer

        studioShell = self._applyStudio()
        role = slicer.vtkMRMLSegmentationNode.GetReferenceImageGeometryReferenceRole()
        cases = (("sub-12_lesion.nii.gz", "sub-12_T1w.nii.gz", None, "sub-12_lesion"),
                 ("lesion.nii.gz", "T1.nii.gz", "patient01", "lesion"))
        for maskName, scanName, folder, lesionName in cases:
            slicer.mrmlScene.Clear(0)
            self._pump()
            maskPath = self._writeMask(maskName, GRID_SHIFTED, subdirectory=folder)
            scanPath = self._writeVolume(scanName, GRID_SHIFTED, subdirectory=folder)
            importer.handleDrop([maskPath], shell=studioShell)
            self._pump(6)
            lesion = slicer.util.getFirstNodeByClassByName("vtkMRMLSegmentationNode", lesionName)
            self.assertIsNotNone(lesion, scanName)
            self.assertIsNone(lesion.GetNodeReference(role), scanName)
            voxels = self._voxels(lesion)
            importer.handleDrop([scanPath], shell=studioShell)
            self._pump(6)
            scan = self._editor().sourceVolumeNode()
            self.assertIsNotNone(scan, scanName)
            self.assertEqual(scan.GetName(), scanName.split(".")[0])
            self.assertEqual(self._editor().segmentationNode().GetID(), lesion.GetID(), scanName)
            self.assertTrue(importer._isBoundTo(lesion, scan), scanName)
            self.assertEqual(self._voxels(lesion), voxels, scanName)
            self.assertEqual(len(slicer.util.getNodesByClass("vtkMRMLSegmentationNode")), 1, scanName)
            self.assertIsNone(slicer.mrmlScene.GetFirstNodeByName(importer.scanLesionSegmentationName(scan)), scanName)
        # Another BIDS subject on the same grid: the lesion stays unbound, Draw edits a lesion of that scan
        slicer.mrmlScene.Clear(0)
        self._pump()
        maskPath = self._writeMask("sub-13_lesion.nii.gz", GRID_SHIFTED)
        otherScanPath = self._writeVolume("sub-14_T1w.nii.gz", GRID_SHIFTED)
        importer.handleDrop([maskPath], shell=studioShell)
        self._pump(6)
        importer.handleDrop([otherScanPath], shell=studioShell)
        self._pump(6)
        lesion = slicer.util.getFirstNodeByClassByName("vtkMRMLSegmentationNode", "sub-13_lesion")
        self.assertIsNone(lesion.GetNodeReference(role))
        self.assertNotEqual(self._editor().segmentationNode().GetID(), lesion.GetID())

    def _writeDicomSeries(self, folderName, slices=6, size=24):
        """A synthetic axial MR series of one series UID written with pydicom; return its folder (None without pydicom)."""
        try:
            import pydicom
            from pydicom.dataset import FileDataset, FileMetaDataset
            from pydicom.uid import ExplicitVRLittleEndian, generate_uid
        except ImportError:
            return None
        folder = os.path.join(self.directory, folderName)
        os.makedirs(folder, exist_ok=True)
        studyUid, seriesUid, frameUid = generate_uid(), generate_uid(), generate_uid()
        for number in range(slices):
            meta = FileMetaDataset()
            meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.4"
            meta.MediaStorageSOPInstanceUID = generate_uid()
            meta.TransferSyntaxUID = ExplicitVRLittleEndian
            path = os.path.join(folder, f"slice{number:02d}.dcm")
            dataset = FileDataset(path, {}, file_meta=meta, preamble=b"\0" * 128)
            dataset.SOPClassUID = meta.MediaStorageSOPClassUID
            dataset.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
            dataset.PatientName = "Import^Test"
            dataset.PatientID = "IMPORT001"
            dataset.StudyInstanceUID, dataset.SeriesInstanceUID, dataset.FrameOfReferenceUID = studyUid, seriesUid, frameUid
            dataset.SeriesDescription = "T1"
            dataset.Modality = "MR"
            dataset.SeriesNumber = 2
            dataset.InstanceNumber = number + 1
            dataset.ImagePositionPatient = [-24.0, -24.0, float(number) * 3.0]
            dataset.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
            dataset.PixelSpacing = [2.0, 2.0]
            dataset.SliceThickness = 3.0
            dataset.Rows = dataset.Columns = size
            dataset.SamplesPerPixel = 1
            dataset.PhotometricInterpretation = "MONOCHROME2"
            dataset.BitsAllocated = dataset.BitsStored = 16
            dataset.HighBit = 15
            dataset.PixelRepresentation = 0
            dataset.PixelData = np.full((size, size), 100 + number, np.uint16).tobytes()
            pydicom.dcmwrite(path, dataset, enforce_file_format=True)
        return folder

    def test_dicom_folder_reopened_keeps_series_and_lesion(self):
        """BB-1: a DICOM folder of one series dropped again, or reopened from its Recent folder row, does not load the
        series again: its scan becomes current and Draw keeps the traced lesion (no "..._1" copy, no new empty lesion).
        """
        from DICOMLib import DICOMUtils
        from StudioLib import importer

        folder = self._writeDicomSeries("dicom one series")
        if folder is None:
            return  # pydicom is not available in this build
        studioShell = self._applyStudio()
        # A temporary database; the original one is restored as it was
        settings = qt.QSettings()
        settingsKey = slicer.dicomDatabaseDirectorySettingsKey
        originalSetting = settings.value(settingsKey)
        originalOpen = bool(slicer.dicomDatabase is not None and slicer.dicomDatabase.isOpen)
        DICOMUtils.openTemporaryDatabase(os.path.join(self.directory, "dicom database"))
        try:
            self.assertEqual(importer.loadFolder(folder, studioShell), "dicom")
            self._pump(8)
            editor = self._editor()
            lesion = editor.segmentationNode()
            scan = editor.sourceVolumeNode()
            self.assertIsNotNone(lesion)
            self.assertIsNotNone(scan)
            self.assertTrue(scan.GetAttribute("DICOM.instanceUIDs"))
            segmentId = editor.currentSegmentID()
            array = slicer.util.arrayFromSegmentBinaryLabelmap(lesion, segmentId, scan)
            array[2:4, 8:12, 8:12] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(array, lesion, segmentId, scan)
            voxels = self._voxels(lesion)
            self.assertGreater(voxels, 0)
            count = slicer.mrmlScene.GetNumberOfNodes()
            scans = [node.GetID() for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")]
            for route in ("drop", "recent"):
                studioShell.switchTo("library")
                self._pump()
                if route == "drop":
                    importer.handleDrop([folder], shell=studioShell)
                else:
                    # The Recent folder row (RecentFolderEntry.load) opens the folder again with loadFolder()
                    self.assertEqual(importer.loadFolder(folder, studioShell), "dicom")
                self._pump(8)
                self.assertEqual([node.GetID() for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")], scans, route)
                self.assertEqual(slicer.mrmlScene.GetNumberOfNodes(), count, route)
                self.assertEqual(editor.segmentationNode().GetID(), lesion.GetID(), route)
                self.assertEqual(editor.sourceVolumeNode().GetID(), scan.GetID(), route)
                self.assertEqual(self._voxels(lesion), voxels, route)
                self.assertEqual(studioShell.currentWorkspace(), "draw", route)
                self.assertEqual(studioShell.toastText(), f"{scan.GetName()} is already open", route)
            seriesUID = slicer.dicomDatabase.seriesForFile(os.path.join(folder, "slice00.dcm"))
            self.assertEqual([node.GetID() for node in importer.openNodesForDicomSeries(seriesUID)], [scan.GetID()])
        finally:
            if slicer.dicomDatabase.isOpen:
                slicer.dicomDatabase.closeDatabase()
            if originalSetting:
                settings.setValue(settingsKey, originalSetting)
                if originalOpen and os.path.isdir(str(originalSetting)):
                    DICOMUtils.openDatabase(str(originalSetting))
            else:
                settings.remove(settingsKey)

    def test_recent_lesion_node_name_has_no_extension(self):
        """A lesion mask re-opened from Recent is named like one opened with Open... (no ".nii.gz" in the node name,
        the toast or the project file name).
        """
        from StudioLib import importer

        studioShell = self._applyStudio()
        path = self._writeMask("sub-08_lesion.nii.gz", GRID_SHIFTED)
        opened = importer.loadLesion(path)
        self.assertEqual(opened.GetName(), "sub-08_lesion")
        slicer.mrmlScene.Clear(0)  # as New project does: a removed node would leave its name counted in the scene
        self._pump(6)
        studioShell.switchTo("library")
        self._pump()
        result = importer.loadRecentFile({"fileName": path, "fileType": "SegmentationFile"}, None, studioShell)
        self._pump(6)
        self.assertEqual([node.GetName() for node in result.segmentations], ["sub-08_lesion"])
        self.assertEqual(studioShell.toastText(), "Loaded sub-08_lesion")

    def test_recent_lesion_opens_as_with_open(self):
        """BB-1: a lesion re-opened from Recent (recorded as a segmentation) is the lesion that Open... shows: a
        probabilistic mask keeps every voxel above 0 (the reader would truncate it to an empty lesion) with its toast,
        and the Notes and SourceReference of the sidecar are restored, for a 0/1 mask too.
        """
        from StudioLib import draw, importer

        studioShell = self._applyStudio()
        dims = GRID_2009C_2MM[0]
        fractional = np.zeros((dims[2], dims[1], dims[0]), np.float32)
        fractional[38:43, 48:53, 43:48] = 0.6
        fractional[40, 50, 45] = 0.9
        binary = np.zeros((dims[2], dims[1], dims[0]), np.uint8)
        binary[30:35, 40:45, 40:45] = 1
        for name, array, isFractional in (("sub-21_lesion.nii.gz", fractional, True), ("sub-22_lesion.nii.gz", binary, False)):
            path = self._writeVolume(name, GRID_2009C_2MM, array)
            with open(importer.sidecarPath(path), "w", encoding="utf-8") as f:
                json.dump({"Notes": f"notes of {name}", "SourceReference": "Fig. 3"}, f)
            slicer.mrmlScene.Clear(0)
            self._pump(6)
            studioShell.switchTo("library")
            self._pump()
            result = importer.loadRecentFile({"fileName": path, "fileType": "SegmentationFile"}, None, studioShell)
            self._pump(8)
            self.assertEqual(len(result.segmentations), 1, name)
            lesion = result.segmentations[0]
            self.assertEqual(lesion.GetName(), name[:-len(".nii.gz")])
            self.assertEqual(self._voxels(lesion), int(np.count_nonzero(array > 0)), name)
            self.assertEqual(bool(lesion.GetAttribute(importer.LESION_FRACTIONAL_ATTRIBUTE)), isFractional, name)
            self.assertEqual(lesion.GetAttribute(draw.NOTES_ATTRIBUTE), f"notes of {name}", name)
            self.assertEqual(lesion.GetAttribute(draw.SOURCE_REFERENCE_ATTRIBUTE), "Fig. 3", name)
            self.assertEqual(draw.lesionSaveState(lesion)[0], "saved", name)
            if isFractional:
                self.assertTrue(any("fractional values" in text for _time, text, _level in studioShell.recentMessages()),
                                studioShell.recentMessages())
            # Opened again from Recent while it is open: shown again, not loaded twice
            count = slicer.mrmlScene.GetNumberOfNodes()
            again = importer.loadRecentFile({"fileName": path, "fileType": "SegmentationFile"}, None, studioShell)
            self._pump(4)
            self.assertEqual([node.GetID() for node in again.alreadyOpen], [lesion.GetID()], name)
            self.assertEqual(slicer.mrmlScene.GetNumberOfNodes(), count, name)

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

        # T map (opens in Map since W3-08): removed, then re-opened from Recent
        slicer.mrmlScene.Clear(0)
        self._pump(6)
        mapPath = self._writeTMap()
        studioShell.switchTo("library")
        self._pump()
        result = importer.handleDrop([mapPath], shell=studioShell)
        self._pump(8)
        self.assertEqual(studioShell.currentWorkspace(), "map")
        slicer.mrmlScene.RemoveNode(result.statmaps[0])
        self._pump(6)
        studioShell.switchTo("library")
        self._pump()
        result = importer.loadRecentFile({"fileName": mapPath, "fileType": "VolumeFile", "singleFile": True}, None, studioShell)
        self._pump(10)
        self.assertEqual(len(result.statmaps), 1)
        mapNode = result.statmaps[0]
        self.assertEqual(mapNode.GetAttribute("Studio.Role"), "statmap")
        self.assertEqual(self._composite("Red").GetForegroundVolumeID(), mapNode.GetID())
        self.assertNotEqual(self._editor().sourceVolumeNode().GetID() if self._editor().sourceVolumeNode() else None, mapNode.GetID())
        self.assertEqual(boundTo(mapNode), [])
        self.assertEqual(studioShell.currentWorkspace(), "map")
        # Already open: Map shows it again, nothing is loaded
        studioShell.switchTo("library")
        self._pump()
        self._composite("Red").SetForegroundVolumeID(None)
        count = slicer.mrmlScene.GetNumberOfNodes()
        importer.loadRecentFile({"fileName": mapPath, "fileType": "VolumeFile", "singleFile": True}, None, studioShell)
        self._pump(6)
        self.assertEqual(slicer.mrmlScene.GetNumberOfNodes(), count)
        self.assertEqual(studioShell.currentWorkspace(), "map")
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

    def test_figure_view_shows_no_lesion(self):
        """BB-1 figure-view-paints-lesion-over-unfitted-figure: a figure view shows the picture only. The template lesion
        (and any segmentation or model) is not drawn over the figure, whose single slice lies at z = 0 with the picture's
        pixel coordinates; it stays in the other views. The own views come back when the figure leaves, in Classic, and
        after a scene saved with the figure is opened again.
        """
        from StudioLib import figure as figureModule
        from StudioLib import importer

        studioShell = self._applyStudio()
        _template, lesion = importer.startOnTemplate(shell=studioShell)
        self._pump(6)
        display = lesion.GetDisplayNode()
        self.assertEqual(list(display.GetViewNodeIDs()), [])
        red, yellow, green = (f"vtkMRMLSliceNode{name}" for name in ("Red", "Yellow", "Green"))
        threeD = slicer.util.getNodesByClass("vtkMRMLViewNode")[0].GetID()
        figure = self._routeFigure(studioShell, fromLibrary=False)
        self._pump(6)
        self.assertEqual(self._background("Yellow").GetID(), figure.GetID())
        self.assertFalse(display.IsDisplayableInView(yellow), list(display.GetViewNodeIDs()))
        for viewId in (red, green, threeD):
            self.assertTrue(display.IsDisplayableInView(viewId), viewId)
        self.assertEqual(display.GetAttribute(importer.VIEWS_BEFORE_FIGURE_ATTRIBUTE), "")
        # A segmentation or a model added while the figure is shown is not drawn over it either
        mask = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "brain_mask")
        mask.CreateDefaultDisplayNodes()
        model = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode", "surface")
        model.CreateDefaultDisplayNodes()
        # Views chosen by the user keep their other views (only the figure view leaves)
        chosen = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "chosen")
        chosen.CreateDefaultDisplayNodes()
        chosen.GetDisplayNode().SetViewNodeIDs([red, yellow])
        self._pump(6)
        for node in (mask, model):
            self.assertFalse(node.GetDisplayNode().IsDisplayableInView(yellow), node.GetName())
            self.assertTrue(node.GetDisplayNode().IsDisplayableInView(red), node.GetName())
        self.assertEqual(list(chosen.GetDisplayNode().GetViewNodeIDs()), [red])
        # The trace views (L-29): Red (template with the figure over it) shows the lesion, Green (the figure alone) not
        session = figureModule.session()
        self.assertIsNotNone(session)
        session.enterTrace()
        self._pump(8)
        self.assertEqual(self._background("Green").GetID(), session.panel().figureId)
        self.assertFalse(display.IsDisplayableInView(green), list(display.GetViewNodeIDs()))
        self.assertTrue(display.IsDisplayableInView(red))
        self.assertTrue(display.IsDisplayableInView(yellow))
        session.leaveTrace()
        self._pump(8)
        # Classic: the views are the user's (official behaviour); back in Studio the figure view is left out again
        studioShell.classic(persist=False)
        self._pump(6)
        self.assertEqual(list(display.GetViewNodeIDs()), [])
        self.assertIsNone(display.GetAttribute(importer.VIEWS_BEFORE_FIGURE_ATTRIBUTE))
        self.assertEqual(list(chosen.GetDisplayNode().GetViewNodeIDs()), [red, yellow])
        studioShell.studio()
        self._pump(8)
        if self._background("Yellow") is not None and self._background("Yellow").GetAttribute("Studio.Role") == "figure":
            self.assertFalse(display.IsDisplayableInView(yellow))
        # A scene saved with the figure shown: opened again, the lesion gets its own views back when the figure leaves
        self._composite("Yellow").SetBackgroundVolumeID(figure.GetID())
        self._pump(6)
        self.assertFalse(display.IsDisplayableInView(yellow))
        scenePath = os.path.join(self.directory, "figure_lesion.mrb")
        self.assertTrue(slicer.util.saveScene(scenePath))
        slicer.mrmlScene.Clear(0)
        self._pump(6)
        importer.loadFiles([scenePath], studioShell)
        self._pump(10)
        loaded = slicer.mrmlScene.GetFirstNodeByName(lesion.GetName())
        self.assertIsNotNone(loaded)
        figures = [node for node in slicer.util.getNodesByClass("vtkMRMLVolumeNode") if node.GetAttribute("Studio.Role") == "figure"]
        self.assertTrue(figures)
        for node in figures:
            slicer.mrmlScene.RemoveNode(node)
        self._pump(8)
        self.assertEqual(list(loaded.GetDisplayNode().GetViewNodeIDs()), [])
        self.assertIsNone(loaded.GetDisplayNode().GetAttribute(importer.VIEWS_BEFORE_FIGURE_ATTRIBUTE))

    def test_figure_view_bookkeeping_is_not_an_edit(self):
        """Taking a figure view out of a segmentation's views (a project from an earlier build opens with a figure) is
        bookkeeping: the autosave fingerprint stays the same, so opening the project does not rewrite it.
        """
        from StudioLib import autosave_io, importer

        mask = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "bookkeeping_mask")
        mask.CreateDefaultDisplayNodes()
        self._pump(4)
        exclusion = importer._FigureViewExclusion.__new__(importer._FigureViewExclusion)
        exclusion._applied = {}
        display = mask.GetDisplayNode()
        views = [node.GetID() for node in slicer.util.getNodesByClass("vtkMRMLAbstractViewNode")]
        self.assertGreaterEqual(len(views), 2)
        before = autosave_io.sceneFingerprint(slicer.mrmlScene, autosave_io.sentinelIds())
        exclusion._updateDisplayNode(display, [views[0]], views[1:])
        self.assertIsNotNone(display.GetAttribute(importer.VIEWS_BEFORE_FIGURE_ATTRIBUTE))
        self.assertEqual(autosave_io.sceneFingerprint(slicer.mrmlScene, autosave_io.sentinelIds()), before)
        exclusion._updateDisplayNode(display, [], views)
        self.assertEqual(autosave_io.sceneFingerprint(slicer.mrmlScene, autosave_io.sentinelIds()), before)
        # A later change by the user still counts as an edit
        display.SetOpacity(0.3)
        self.assertNotEqual(autosave_io.sceneFingerprint(slicer.mrmlScene, autosave_io.sentinelIds()), before)

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

    def test_new_lesion_undo_keeps_painted_strokes(self):
        """The "New lesion" toast's Undo never deletes strokes painted in the new lesion meanwhile: it is refused with a
        warning and the lesion stays; an untouched new lesion is still removed.
        """
        from StudioLib import importer

        studioShell = self._applyStudio()
        volume, first = importer.startOnTemplate(shell=studioShell)
        self._pump(6)
        second = importer.newLesionSegmentation(shell=studioShell)
        self._pump()
        segmentId = second.GetSegmentation().GetNthSegmentID(0)
        array = slicer.util.arrayFromSegmentBinaryLabelmap(second, segmentId, volume)
        array[10:12, 10:12, 10:12] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, second, segmentId, volume)
        self._pump()
        self.assertFalse(importer.isSegmentationEmpty(second))
        # The toast's own Undo button, as the user clicks it
        undoButton = self.mainWindow.findChild("QToolButton", "StudioStickyToastAction")
        self.assertIsNotNone(undoButton)
        undoButton.click()
        self._pump(4)
        self.assertTrue(slicer.mrmlScene.IsNodePresent(second))
        self.assertFalse(importer.isSegmentationEmpty(second))
        self.assertIn("Lesion 2", studioShell.toastText())
        self.assertIn("strokes", studioShell.toastText())
        self.assertFalse(first.GetDisplayNode().GetVisibility2D())
        self.assertFalse(importer.undoNewLesion(second.GetID(), first.GetID(), volume.GetID(), first.GetID(), shell=studioShell))
        self.assertTrue(slicer.mrmlScene.IsNodePresent(second))
        # Untouched: removed, the previous lesion is shown again
        third = importer.newLesionSegmentation(shell=studioShell)
        self.assertTrue(importer.undoNewLesion(third.GetID(), second.GetID(), volume.GetID(), None, shell=studioShell))
        self.assertFalse(slicer.mrmlScene.IsNodePresent(third))

    def test_template_again_after_save_never_overwrites(self):
        """Start on template after a saved, unchanged lesion starts a new lesion: Save never re-saves it silently to
        the previous case's file. A lesion with unsaved changes is reused as it is (never hidden or re-targeted).
        """
        import hashlib

        from StudioLib import draw, importer

        def paint(segmentation, volume, box):
            segmentId = segmentation.GetSegmentation().GetNthSegmentID(0)
            array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentation, segmentId, volume)
            array[box] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentation, segmentId, volume)
            self._pump()

        def digest(path):
            with open(path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()

        studioShell = self._applyStudio()
        volume, first = importer.startOnTemplate(shell=studioShell)
        self._pump(6)
        paint(first, volume, np.s_[40:44, 50:54, 40:44])
        pathA = os.path.join(self.directory, "caseA_lesion.nii.gz")
        self.assertEqual(draw.saveLesionDialog(studioShell, path=pathA), pathA)
        self.assertEqual(draw.lesionSaveState(first)[0], "saved")
        savedA = digest(pathA)
        # Changed after the save: the same lesion is reused (its strokes stay visible, Save keeps its own file)
        studioShell.switchTo("library")
        self._pump()
        paint(first, volume, np.s_[44:46, 50:54, 40:44])
        self.assertEqual(draw.lesionSaveState(first)[0], "unsaved")
        _volume, again = importer.startOnTemplate(shell=studioShell)
        self._pump(4)
        self.assertEqual(again.GetID(), first.GetID())
        self.assertTrue(first.GetDisplayNode().GetVisibility2D())
        self.assertEqual(draw.saveLesionNow(studioShell), pathA)
        self.assertEqual(draw.lesionSaveState(first)[0], "saved")
        savedA = digest(pathA)
        # Saved and unchanged: the next case starts on a new lesion, the saved one is hidden and keeps its file
        studioShell.switchTo("library")
        self._pump()
        volume2, second = importer.startOnTemplate(shell=studioShell)
        self._pump(6)
        self.assertEqual(volume2.GetID(), volume.GetID())
        self.assertNotEqual(second.GetID(), first.GetID())
        self.assertTrue(importer._isBoundTo(second, volume))
        self.assertFalse(first.GetDisplayNode().GetVisibility2D())
        self.assertEqual(self._editor().segmentationNode().GetID(), second.GetID())
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        self.assertEqual(draw.lesionSaveState(second)[0], "never")
        self.assertEqual(draw.lesionSaveState(first)[:2], ("saved", pathA))
        # The Save call to action on the new lesion asks for a file (here: cancelled); case A is untouched
        paint(second, volume2, np.s_[20:30, 20:30, 20:30])
        dialogs = []
        original = draw.saveLesionDialog
        draw.saveLesionDialog = lambda shell=None, path=None: dialogs.append(path)
        try:
            self.assertIsNone(draw.saveLesionNow(studioShell))
        finally:
            draw.saveLesionDialog = original
        self.assertEqual(dialogs, [None])
        self.assertEqual(digest(pathA), savedA)
        # A new lesion that is not saved yet is reused by the next Start on template (no "Lesion 3")
        _volume, third = importer.startOnTemplate(shell=studioShell)
        self._pump(4)
        self.assertEqual(third.GetID(), second.GetID())
        self.assertEqual(digest(pathA), savedA)

    #
    # Tables and Recent
    #

    def test_loaded_lesion_painted_is_unsaved(self):
        """A lesion loaded from its NIfTI file is "Saved"; painted, it has "Unsaved changes" (not "Not saved yet"), and
        the Save call to action still asks for a file instead of overwriting the loaded one.
        """
        from StudioLib import draw, importer

        maskNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode", "mask")
        mask = np.zeros((20, 22, 24), np.uint8)
        mask[8:12, 8:12, 8:12] = 1
        slicer.util.updateVolumeFromArray(maskNode, mask)
        path = os.path.join(self.directory, "sub-09_lesion.nii.gz")
        self.assertTrue(slicer.util.saveNode(maskNode, path))
        slicer.mrmlScene.RemoveNode(maskNode)
        with open(path, "rb") as f:
            before = f.read()
        segmentationNode = importer.loadLesion(path)
        self._pump()
        state, statePath, _time = draw.lesionSaveState(segmentationNode)
        self.assertEqual(state, "saved")
        self.assertEqual(os.path.normcase(os.path.abspath(statePath)), os.path.normcase(os.path.abspath(path)))
        # Paint: one more voxel
        segmentId = segmentationNode.GetSegmentation().GetNthSegmentID(0)
        referenceNode = slicer.util.loadLabelVolume(path, {"show": False})
        array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, referenceNode)
        array[0, 0, 0] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, referenceNode)
        slicer.mrmlScene.RemoveNode(referenceNode)
        self._pump()
        self.assertEqual(draw.lesionSaveState(segmentationNode)[0], "unsaved")
        text, toolTip = draw.saveStateText(segmentationNode)
        self.assertTrue(text.startswith("Unsaved changes"), text)
        self.assertIn("sub-09_lesion.nii.gz", text)
        self.assertEqual(os.path.basename(toolTip), "sub-09_lesion.nii.gz")
        # No record of a save in this session: Save asks for a file (here: cancelled) and the loaded file is untouched
        dialogs = []
        original = draw.saveLesionDialog
        draw.saveLesionDialog = lambda shell=None, path=None: dialogs.append(path)
        originalNodes = draw.lesionNodes
        draw.lesionNodes = lambda: (segmentationNode, None)
        try:
            self.assertIsNone(draw.saveLesionNow())
        finally:
            draw.saveLesionDialog = original
            draw.lesionNodes = originalNodes
        self.assertEqual(dialogs, [None])
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_probabilistic_lesion_keeps_its_voxels(self):
        """BB-1: a float lesion file with fractional values (a probabilistic mask, ``desc-lesionProb``) opens with every
        voxel above 0 as the lesion, where the segmentation reader truncates values below 1 to 0 (an empty lesion, or
        only the voxels equal to 1). It is "Saved · <file>" until painted, the file is unchanged and the load toast says
        so. A float file with only 0 and 1 is read as before.
        """
        from StudioLib import draw, importer

        studioShell = self._applyStudio()
        dims = GRID_2009C_2MM[0]
        fractional = np.zeros((dims[2], dims[1], dims[0]), np.float32)
        fractional[38:43, 48:53, 43:48] = 0.3
        fractional[39:42, 49:52, 44:47] = 0.6
        fractional[40, 50, 45] = 0.95
        mixed = np.zeros_like(fractional)
        mixed[38:43, 48:53, 43:48] = 0.6
        mixed[40, 50, 45] = 1.0
        ones = (fractional > 0).astype(np.float32)
        for name, array in (("sub-11_desc-lesionProb_mask.nii.gz", fractional), ("sub-12_desc-lesionProb_mask.nii.gz", mixed)):
            path = self._writeVolume(name, GRID_2009C_2MM, array)
            with open(path, "rb") as f:
                before = f.read()
            lesion = importer.loadLesion(path)
            self._pump()
            self.assertEqual(self._voxels(lesion), int(np.count_nonzero(array > 0)), name)
            self.assertEqual(lesion.GetSegmentation().GetNumberOfSegments(), 1, name)
            segmentId = lesion.GetSegmentation().GetNthSegmentID(0)
            self.assertEqual(lesion.GetSegmentation().GetSegment(segmentId).GetName(), importer.LESION_SEGMENT_NAME)
            self.assertTrue(lesion.GetAttribute(importer.LESION_FRACTIONAL_ATTRIBUTE), name)
            # The voxels are where the file has them (the file's grid, as the volume reader places it)
            reference = slicer.util.loadVolume(path, {"show": False})
            shown = slicer.util.arrayFromSegmentBinaryLabelmap(lesion, segmentId, reference)
            np.testing.assert_array_equal(shown > 0, slicer.util.arrayFromVolume(reference) > 0)
            slicer.mrmlScene.RemoveNode(reference)
            self.assertEqual(draw.lesionSaveState(lesion)[0], "saved", name)
            with open(path, "rb") as f:
                self.assertEqual(f.read(), before, name)
            slicer.mrmlScene.RemoveNode(lesion)
        # Painted: "Unsaved changes", as any loaded lesion
        path = os.path.join(self.directory, "sub-11_desc-lesionProb_mask.nii.gz")
        lesion = importer.loadLesion(path)
        segmentId = lesion.GetSegmentation().GetNthSegmentID(0)
        reference = slicer.util.loadVolume(path, {"show": False})
        array = slicer.util.arrayFromSegmentBinaryLabelmap(lesion, segmentId, reference)
        array[0, 0, 0] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, lesion, segmentId, reference)
        slicer.mrmlScene.RemoveNode(reference)
        self._pump()
        self.assertEqual(draw.lesionSaveState(lesion)[0], "unsaved")
        slicer.mrmlScene.RemoveNode(lesion)
        # Opened from Library or a drop: the toast says how the file is shown
        result = importer.loadFiles([os.path.join(self.directory, "sub-12_desc-lesionProb_mask.nii.gz")], studioShell)
        self._pump(8)
        self.assertEqual(self._voxels(result.segmentations[0]), int(np.count_nonzero(mixed > 0)))
        self.assertTrue(any("fractional values" in text for _time, text, _level in studioShell.recentMessages()), studioShell.recentMessages())
        # Control: a float file with only 0 and 1 is an ordinary mask
        path = self._writeVolume("sub-13_lesion.nii.gz", GRID_2009C_2MM, ones)
        lesion = importer.loadLesion(path)
        self.assertEqual(self._voxels(lesion), int(np.count_nonzero(ones)))
        self.assertFalse(lesion.GetAttribute(importer.LESION_FRACTIONAL_ATTRIBUTE))
        self.assertEqual(draw.lesionSaveState(lesion)[0], "saved")

    def test_probabilistic_lesion_is_mapped_with_its_weights(self):
        """BB-1: Map analyses a probabilistic lesion file (``desc-lesionProb``) opened in Studio with its values as
        weights, as the engine command line and a cohort map it (``engine.is_fractional_seed``), not the binary mask of
        every voxel above 0 that is shown: the exported seed keeps the values, the run gets --probabilistic and the Seed
        line shows the weighted volume. Once the lesion is edited, or for a fractional file with another name, it is mapped
        as its (binary) segment and the Seed card says so.
        """
        import LesionNetworkMapping
        from LesionNetworkMappingLib import nifti
        from StudioLib import importer

        dims = GRID_2009C_2MM[0]
        values = np.zeros((dims[2], dims[1], dims[0]), np.float32)
        values[36:45, 46:55, 41:50] = 0.05
        values[38:43, 48:53, 43:48] = 1.0
        voxelMl = 8.0 / 1000.0
        logic = LesionNetworkMapping.LesionNetworkMappingLogic()
        path = self._writeVolume("sub-21_space-MNI152NLin2009cAsym_desc-lesionProb_mask.nii.gz", GRID_2009C_2MM, values)
        lesion = importer.loadLesion(path)
        self._pump()
        # A project autosave stores the lesion as a .seg.nrrd: the values are still read from the file it was opened from
        lesion.GetStorageNode().SetFileName(os.path.join(self.directory, "Untitled project", "Segmentations", "sub-21.seg.nrrd"))
        segmentId = lesion.GetSegmentation().GetNthSegmentID(0)
        data, _affine, extrusion = logic.analysisSeedArray(lesion, segmentId)
        self.assertIsNone(extrusion)
        self.assertEqual(data.dtype.kind, "f")
        np.testing.assert_allclose(data, values.transpose(2, 1, 0), atol=1e-6)
        check = logic.checkSeed(lesion, segmentId)
        self.assertTrue(check["ok"], check)
        self.assertTrue(check["weighted"])
        self.assertIn("weights", check["weightNotice"])
        self.assertAlmostEqual(check["volumeMl"], float(values.sum(dtype=np.float64)) * voxelMl, places=4)
        self.assertLess(check["volumeMl"], np.count_nonzero(values) * voxelMl / 2)
        exported = logic.exportSeedLesion(lesion, segmentId, os.path.join(self.directory, "run", "lesion", "sub-21.nii.gz"))
        written, _affine, _header = nifti.read_nifti(exported)
        np.testing.assert_allclose(written, values.transpose(2, 1, 0), atol=1e-6)
        self.assertTrue(LesionNetworkMapping.LesionNetworkMappingWidget._seedFileIsWeighted(exported))
        self.assertIn("--probabilistic", logic.engineArguments("bundle", exported, self.directory, probabilistic=True))
        self.assertNotIn("--probabilistic", logic.engineArguments("bundle", exported, self.directory))
        # Edited: the segment is the lesion now (binary), and the Seed card says so
        reference = slicer.util.loadVolume(path, {"show": False})
        array = slicer.util.arrayFromSegmentBinaryLabelmap(lesion, segmentId, reference)
        array[0, 0, 0] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, lesion, segmentId, reference)
        slicer.mrmlScene.RemoveNode(reference)
        self._pump()
        data, _affine, _extrusion = logic.analysisSeedArray(lesion, segmentId)
        self.assertEqual(data.dtype, np.uint8)
        check = logic.checkSeed(lesion, segmentId)
        self.assertFalse(check["weighted"])
        self.assertIn("binary mask", check["weightNotice"])
        self.assertAlmostEqual(check["volumeMl"], (np.count_nonzero(values) + 1) * voxelMl, places=4)
        exported = logic.exportSeedLesion(lesion, segmentId, os.path.join(self.directory, "run2", "lesion", "sub-21.nii.gz"))
        self.assertFalse(LesionNetworkMapping.LesionNetworkMappingWidget._seedFileIsWeighted(exported))
        slicer.mrmlScene.RemoveNode(lesion)
        # A fractional file not named desc-lesionProb: the command line binarizes it, and so does Map (with the note)
        path = self._writeVolume("sub-22_lesion.nii.gz", GRID_2009C_2MM, values)
        lesion = importer.loadLesion(path)
        self._pump()
        self.assertTrue(lesion.GetAttribute(importer.LESION_FRACTIONAL_ATTRIBUTE))
        check = logic.checkSeed(lesion, lesion.GetSegmentation().GetNthSegmentID(0))
        self.assertFalse(check["weighted"])
        self.assertIn("binary mask", check["weightNotice"])
        self.assertAlmostEqual(check["volumeMl"], np.count_nonzero(values) * voxelMl, places=4)
        slicer.mrmlScene.RemoveNode(lesion)
        # An ordinary 0/1 lesion has no note
        lesion = importer.loadLesion(self._writeMask("sub-23_lesion.nii.gz", GRID_2009C_2MM))
        check = logic.checkSeed(lesion, lesion.GetSegmentation().GetNthSegmentID(0))
        self.assertFalse(check["weighted"])
        self.assertEqual(check["weightNotice"], "")

    def test_sidecar_notes_reopen(self):
        """The Notes and SourceReference of a lesion's sidecar are on the lesion opened again (the Lesion card shows
        them), and Save lesion to the same file keeps them; a value cleared in the card is written cleared.
        """
        from StudioLib import draw, importer

        self.assertEqual(dict(importer.LESION_SIDECAR_TEXT_ATTRIBUTES),
                         {"Notes": draw.NOTES_ATTRIBUTE, "SourceReference": draw.SOURCE_REFERENCE_ATTRIBUTE})
        studioShell = self._applyStudio()
        path = self._writeMask("sub-09_lesion.nii.gz", GRID_2009C_2MM)
        with open(importer.sidecarPath(path), "w", encoding="utf-8") as f:
            json.dump({"Space": "MNI152NLin2009cAsym", "Notes": "periventricular, check slice 40",
                       "SourceReference": "10.1000/xyz Fig 2B", "GeneratedBy": "Slicer Studio"}, f)
        result = importer.loadFiles([path], studioShell)
        self._pump(8)
        lesion = result.segmentations[0]
        self.assertEqual(lesion.GetAttribute(draw.NOTES_ATTRIBUTE), "periventricular, check slice 40")
        self.assertEqual(lesion.GetAttribute(draw.SOURCE_REFERENCE_ATTRIBUTE), "10.1000/xyz Fig 2B")
        self.assertEqual(self._editor().segmentationNode().GetID(), lesion.GetID())
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        self._pump(4)
        card = draw.extension().card
        self.assertEqual(card.notesEdit.toPlainText(), "periventricular, check slice 40")
        self.assertEqual(card.sourceReferenceEdit.text, "10.1000/xyz Fig 2B")

        def sidecar():
            with open(importer.sidecarPath(path), encoding="utf-8") as f:
                return json.load(f)

        # Save lesion to the same file keeps them
        self.assertEqual(draw.saveLesionDialog(studioShell, path=path), path)
        self.assertEqual(sidecar()["Notes"], "periventricular, check slice 40")
        self.assertEqual(sidecar()["SourceReference"], "10.1000/xyz Fig 2B")
        # A lesion without the attributes (not opened by Studio) re-saved to its own file keeps the sidecar's text
        lesion.RemoveAttribute(draw.NOTES_ATTRIBUTE)
        lesion.RemoveAttribute(draw.SOURCE_REFERENCE_ATTRIBUTE)
        draw.writeSidecar(path, lesion)
        self.assertEqual(sidecar()["Notes"], "periventricular, check slice 40")
        self.assertEqual(sidecar()["SourceReference"], "10.1000/xyz Fig 2B")
        # ... but saved over another lesion's file, it does not take that file's text
        otherPath = os.path.join(os.path.dirname(path), "sub-10_lesion.nii.gz")
        with open(importer.sidecarPath(otherPath), "w", encoding="utf-8") as f:
            json.dump({"Notes": "another lesion", "SourceReference": "another source"}, f)
        draw.writeSidecar(otherPath, lesion)
        with open(importer.sidecarPath(otherPath), encoding="utf-8") as f:
            otherSidecar = json.load(f)
        self.assertEqual([otherSidecar["Notes"], otherSidecar["SourceReference"]], ["", ""])
        # Cleared in the card: written cleared
        lesion.SetAttribute(draw.NOTES_ATTRIBUTE, "")
        lesion.SetAttribute(draw.SOURCE_REFERENCE_ATTRIBUTE, "")
        draw.writeSidecar(path, lesion)
        self.assertEqual([sidecar()["Notes"], sidecar()["SourceReference"]], ["", ""])

    def _windowDrag(self, widget, mimeData):
        """Drag ``mimeData`` over the centre of ``widget`` and drop it, through the main window's QWindow (Qt then picks the
        widget under the cursor, like a drag from the file manager). Returns the drop action of the drop, or None if this
        PythonQt cannot make drag events.
        """
        window = self.mainWindow.windowHandle()
        self.assertIsNotNone(window, "the main window is shown")
        parent = widget.parentWidget()
        while parent is not None and not parent.inherits("QScrollArea"):
            parent = parent.parentWidget()
        if parent is not None:
            parent.ensureWidgetVisible(widget)
            self._pump()
        self.assertFalse(widget.visibleRegion().isEmpty(), f"{widget.objectName} is in view")
        point = self.mainWindow.mapFromGlobal(widget.mapToGlobal(qt.QPoint(widget.width // 2, widget.height // 2)))
        actions = qt.Qt.CopyAction | qt.Qt.MoveAction | qt.Qt.LinkAction
        try:
            enter = qt.QDragEnterEvent(point, actions, mimeData, qt.Qt.LeftButton, qt.Qt.NoModifier)
            move = qt.QDragMoveEvent(point, actions, mimeData, qt.Qt.LeftButton, qt.Qt.NoModifier)
            drop = qt.QDropEvent(qt.QPointF(point), actions, mimeData, qt.Qt.LeftButton, qt.Qt.NoModifier)
        except (TypeError, ValueError):
            # This PythonQt cannot make the events (QFlags arguments)
            return None
        for event in (enter, move, drop):
            qt.QApplication.sendEvent(window, event)
        self._pump()
        return drop.dropAction() if drop.isAccepted() else qt.Qt.IgnoreAction

    def test_file_dropped_on_a_text_field_is_opened_not_typed_in(self):
        """A file dropped on a text field of the window (Draw Notes and Source) is opened like anywhere else on the window,
        never typed in as its path (BB-1: the Notes took "file:///...", saved into the project scene); a dragged text is
        still typed in, and Classic is unchanged.
        """
        from StudioLib import importer

        studioShell = self._applyStudio()
        grid = ((20, 24, 28), 2.0, (1, 1, 1), (-20.0, -24.0, -28.0))
        scan = self._writeVolume("scan.nii.gz", grid)
        lesion = self._writeMask("sub-08_lesion.nii.gz", grid, center=(5, 6, 7))
        importer.loadFiles([scan], studioShell)
        studioShell.switchTo("draw")
        self._wait(300)
        self.mainWindow.findChild("QPushButton", "StudioDrawNotesRow").click()
        self._wait(300)
        notes = self.mainWindow.findChild("QPlainTextEdit", "StudioLesionNotesEdit")
        source = self.mainWindow.findChild("QLineEdit", "StudioLesionSourceReferenceEdit")
        self.assertTrue(notes.isVisible() and source.isVisible())
        dropped = []
        original = importer.handleDrop
        importer.handleDrop = lambda paths, addData=False, shell=None: dropped.append(list(paths))
        initialSize = qt.QSize(self.mainWindow.size)
        try:
            # The fields are in view
            self.mainWindow.resize(1600, 1000)
            # Files only (a multi-line field took it), and files with text (a line edit took it)
            urls = qt.QMimeData()
            urls.setUrls([qt.QUrl.fromLocalFile(lesion)])
            action = self._windowDrag(notes, urls)
            if action is None:
                return
            self.assertEqual(action, qt.Qt.CopyAction)
            self._wait(50)
            self.assertEqual(str(notes.toPlainText()), "")
            self.assertEqual(dropped, [[lesion]])
            urlsAndText = qt.QMimeData()
            urlsAndText.setUrls([qt.QUrl.fromLocalFile(lesion)])
            urlsAndText.setText(lesion)
            for field in (source, notes):
                self.assertEqual(self._windowDrag(field, urlsAndText), qt.Qt.CopyAction)
                self._wait(50)
            self.assertEqual(str(source.text), "")
            self.assertEqual(str(notes.toPlainText()), "")
            self.assertEqual(dropped, [[lesion]] * 3)
            # A dragged text (no file) is still typed in
            text = qt.QMimeData()
            text.setText("doi:10.1000/182")
            self._windowDrag(source, text)
            self._wait(50)
            self.assertEqual(str(source.text), "doi:10.1000/182")
            self.assertEqual(len(dropped), 3)
            # Nothing stays installed on the application between drags, and Classic removes the Studio drop handling
            self.assertFalse(importer._dropHandler.textFieldRouter.armed)
            studioShell.dispose()
            self._pump()
            self.assertIsNone(importer._dropHandler)
        finally:
            importer.handleDrop = original
            self.mainWindow.resize(initialSize)

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

    def test_semicolon_csv_participants_table_is_recognized(self):
        """A participants.csv that Excel saved in a decimal-comma locale ("CSV (semicolon separated)") is a participants
        table like the comma CSV: it never opens Add Data, its columns are named, and its decimal commas read as numbers
        (BB-1: it was read as one column "participant_id;age;..." and went to Add Data).
        """
        from StudioLib import importer, library, study_ui

        studioShell = self._applyStudio()
        path = os.path.join(self.directory, "participants.csv")
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            f.write("participant_id;age;aphasia_score;group\r\nsub-01;54,5;1,25;Patient\r\nsub-02;61,75;0,125;Kontrolle\r\n")
        self.assertEqual(importer.readTableHeader(path), (["participant_id", "age", "aphasia_score", "group"], 2))
        self.assertTrue(importer.isParticipantsTable(path))
        self.assertEqual(library.participantsLine(path), "participants.csv · 2 rows · age, aphasia_score, group")
        columns, rows = study_ui.readTable(path)
        self.assertEqual(columns, ["participant_id", "age", "aphasia_score", "group"])
        self.assertEqual([(row["participant_id"], row["age"], row["aphasia_score"], row["group"]) for row in rows],
                         [("sub-01", "54.5", "1.25", "Patient"), ("sub-02", "61.75", "0.125", "Kontrolle")])
        # A comma CSV keeps its commas inside quotes, a tab CSV is still read
        comma = os.path.join(self.directory, "comma", "participants.csv")
        os.makedirs(os.path.dirname(comma))
        with open(comma, "w", encoding="utf-8") as f:
            f.write('participant_id,"note, with comma",group\nsub-01,"a;b",case\n')
        self.assertEqual(importer.readTableHeader(comma), (["participant_id", "note, with comma", "group"], 1))
        self.assertEqual(study_ui.readTable(comma)[1], [{"participant_id": "sub-01", "note, with comma": "a;b", "group": "case"}])
        calls = []
        original = importer.openAddDataDialog
        importer.openAddDataDialog = lambda paths: calls.append(list(paths))
        try:
            result = importer.handleDrop([path], shell=studioShell)
            self._pump()
            self.assertEqual(calls, [])
            self.assertEqual(result.tables, [os.path.abspath(path)])
        finally:
            importer.openAddDataDialog = original

    def test_mixed_drop_lesion_stays_the_draw_lesion(self):
        """A drop with a scan, its lesion and a file that Studio does not open itself (markups, a .mgz scan): only that file
        goes to Add Data (where a lesion would be read as a label map), and Draw edits the dropped lesion on the scan.
        """
        from StudioLib import importer

        studioShell = self._applyStudio()
        scan = self._writeVolume("sub-01_T1w.nii.gz", GRID_SHIFTED)
        lesion = self._writeMask("sub-01_lesion.nii.gz", GRID_SHIFTED)
        mgzScan = self._writeVolume("sub-01_T1w.mgz", GRID_SHIFTED)
        markupsNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "targets")
        markupsNode.AddControlPoint(0.0, 0.0, 0.0)
        markups = os.path.join(self.directory, "targets.mrk.json")
        self.assertTrue(slicer.util.saveNode(markupsNode, markups))
        slicer.mrmlScene.RemoveNode(markupsNode)
        calls = []

        def addData(paths):
            # As the Add Data dialog: a lesion name is read as a label map (W3-04), a markups file as markups
            calls.append([os.path.basename(path) for path in paths])
            for path in paths:
                if path.endswith(".mrk.json"):
                    slicer.util.loadMarkups(path)
                elif importer.isLesionName(path):
                    slicer.util.loadLabelVolume(path)
                else:
                    slicer.util.loadVolume(path)

        original = importer.openAddDataDialog
        importer.openAddDataDialog = addData
        try:
            for paths, toAddData, scanName in (([scan, lesion, markups], ["targets.mrk.json"], "sub-01_T1w"),
                                               ([mgzScan, lesion], ["sub-01_T1w.mgz"], "sub-01_T1w")):
                slicer.mrmlScene.Clear(0)
                self._pump()
                calls.clear()
                result = importer.handleDrop(paths, shell=studioShell)
                self._pump(8)
                self.assertEqual(calls, [toAddData])
                self.assertEqual([node.GetName() for node in result.segmentations], ["sub-01_lesion"])
                self.assertEqual(slicer.util.getNodesByClass("vtkMRMLLabelMapVolumeNode"), [])
                self.assertEqual(self._editor().segmentationNode().GetName(), "sub-01_lesion")
                self.assertEqual(self._editor().sourceVolumeNode().GetName(), scanName)
                self.assertTrue(self._editor().segmentationNode().GetDisplayNode().GetVisibility())
                if toAddData == ["targets.mrk.json"]:
                    self.assertEqual(len(slicer.util.getNodesByClass("vtkMRMLMarkupsFiducialNode")), 1)
        finally:
            importer.openAddDataDialog = original

    def test_mgz_scan_dropped_twice_is_not_loaded_twice(self):
        """A FreeSurfer .mgz scan that is already open is not sent to Add Data again: its node becomes current again, as
        for a NIfTI scan (BB-1: T1 and T1_1 were created, and the project held the scan twice). A model dropped twice still
        goes to Add Data (3D Slicer's route).
        """
        from StudioLib import importer

        studioShell = self._applyStudio()
        mgzScan = self._writeVolume("sub-01_T1w.mgz", GRID_SHIFTED)
        niftiScan = self._writeVolume("sub-02_T1w.nii.gz", GRID_SHIFTED)
        calls = []

        def addData(paths):
            calls.append([os.path.basename(path) for path in paths])
            for path in paths:
                slicer.util.loadVolume(path)

        original = importer.openAddDataDialog
        importer.openAddDataDialog = addData
        try:
            first = importer.handleDrop([mgzScan], shell=studioShell)
            self._pump(8)
            self.assertEqual(calls, [["sub-01_T1w.mgz"]])
            self.assertEqual(first.addDataPaths, [mgzScan])
            scanNode = slicer.util.getNode("sub-01_T1w")
            self.assertIsNotNone(importer.openNodeForFile(mgzScan))
            # The second drop: no Add Data, no new node, the scan is current again
            importer.handleDrop([niftiScan], shell=studioShell)
            self._pump(8)
            again = importer.handleDrop([mgzScan], shell=studioShell)
            self._pump(8)
            self.assertEqual(calls, [["sub-01_T1w.mgz"]])
            self.assertEqual([node.GetID() for node in again.alreadyOpen], [scanNode.GetID()])
            self.assertEqual(sorted(node.GetName() for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")),
                             ["sub-01_T1w", "sub-02_T1w"])
            self.assertEqual(self._editor().sourceVolumeNode().GetID(), scanNode.GetID())
            # Together with a new .mgz scan, only the new one goes to Add Data
            newMgz = self._writeVolume("sub-03_T1w.mgz", GRID_SHIFTED)
            importer.handleDrop([mgzScan, newMgz], shell=studioShell)
            self._pump(8)
            self.assertEqual(calls[-1], ["sub-03_T1w.mgz"])
            self.assertEqual(len(slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")), 3)
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

    def test_segmentation_empty_check_is_not_a_full_scan_each_time(self):
        """BB-1: ``isSegmentationEmpty`` (asked on every Library entry) scans the labelmap in blocks and remembers the
        answer until the labelmap changes, where it built a full-size comparison array on every call (0.3 s with a
        0.5 mm template, over the 50 ms budget of a workspace switch). The answer stays right after every edit.
        """
        from vtk.util import numpy_support

        from StudioLib import importer

        volume = slicer.util.addVolumeFromArray(np.zeros((12, 14, 16), np.float32), name="BBScan")
        segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "BBLesion")
        segmentation.CreateDefaultDisplayNodes()
        segmentation.SetReferenceImageGeometryParameterFromVolumeNode(volume)
        segmentId = segmentation.GetSegmentation().AddEmptySegment("Lesion")
        self.assertTrue(importer.isSegmentationEmpty(segmentation))

        scans = []
        original = numpy_support.vtk_to_numpy
        numpy_support.vtk_to_numpy = lambda *args, **kwargs: scans.append(1) or original(*args, **kwargs)
        blockBackup = getattr(importer, "_LABEL_VALUE_SCAN_BLOCK_VOXELS", None)
        try:
            importer._LABEL_VALUE_SCAN_BLOCK_VOXELS = 100  # many blocks
            painted = slicer.util.arrayFromSegmentBinaryLabelmap(segmentation, segmentId, volume)
            painted[11, 13, 15] = 1  # in the last block
            slicer.util.updateSegmentBinaryLabelmapFromArray(painted, segmentation, segmentId, volume)
            scans.clear()
            self.assertFalse(importer.isSegmentationEmpty(segmentation))
            self.assertEqual(len(scans), 1)
            for index in range(3):
                self.assertFalse(importer.isSegmentationEmpty(segmentation))
            self.assertEqual(len(scans), 1, "an unchanged labelmap is not scanned again")
            # Erasing is noticed
            painted[:] = 0
            slicer.util.updateSegmentBinaryLabelmapFromArray(painted, segmentation, segmentId, volume)
            self.assertTrue(importer.isSegmentationEmpty(segmentation))
            # Painting again is noticed
            painted[0, 0, 0] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(painted, segmentation, segmentId, volume)
            self.assertFalse(importer.isSegmentationEmpty(segmentation))
        finally:
            numpy_support.vtk_to_numpy = original
            if blockBackup is not None:
                importer._LABEL_VALUE_SCAN_BLOCK_VOXELS = blockBackup

    def test_template_loads_when_the_cache_cannot_be_written(self):
        """The bundled 2 mm template opens although SampleData cannot copy it into its cache (a read-only or full cache
        folder); a template that has to be downloaded fails with a plain message, not "list index out of range".
        """
        import SampleData
        from StudioLib import importer

        def cannotWrite(self, *args, **kwargs):
            raise ValueError("cache folder is not writable")

        original = SampleData.SampleDataLogic.downloadFileIntoCache
        SampleData.SampleDataLogic.downloadFileIntoCache = cannotWrite
        try:
            # Bundled template: loaded from where it is installed
            volume = importer._templateVolume(importer.TEMPLATE_SAMPLE_2MM)
            self.assertIsNotNone(volume)
            self.assertEqual(volume.GetName(), importer._templateNodeName(importer.TEMPLATE_SAMPLE_2MM))
            self.assertIsNone(volume.GetStorageNode(), "the bundled file is not the storage of the scene node")
            slicer.mrmlScene.RemoveNode(volume)
            # Downloaded template: a plain message that says what to check, shown without a traceback in the log
            with self.assertRaises(RuntimeError) as raised:
                importer._templateVolume(importer.TEMPLATE_SAMPLE_1MM)
            self.assertTrue(getattr(raised.exception, "user_problem", False))
            self.assertNotIn("list index", str(raised.exception))
            self.assertIn("MNI152NLin2009cAsym 1mm T1w", str(raised.exception))
        finally:
            SampleData.SampleDataLogic.downloadFileIntoCache = original

    def test_volume_under_grid_transform_has_no_error_flood_and_a_toast(self):
        """A volume under a non-linear (grid) transform, as a DICOM series with a missing slice is loaded: the flat
        slice bar logs no "expected linear transforms" error for it, and the load toast says that a slice is missing
        (the DICOM plugin only logs it).
        """
        import numpy as np
        import vtk
        import vtk.util.numpy_support as vtkNumpy
        from StudioLib import importer

        studioShell = self._applyStudio()
        volume = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "gap scan")
        image = vtk.vtkImageData()
        image.SetDimensions(8, 8, 8)
        image.AllocateScalars(vtk.VTK_SHORT, 1)
        image.GetPointData().GetScalars().FillComponent(0, 5)
        volume.SetAndObserveImageData(image)
        volume.CreateDefaultDisplayNodes()
        grid = vtk.vtkImageData()
        grid.SetDimensions(3, 3, 3)
        grid.AllocateScalars(vtk.VTK_DOUBLE, 3)
        vtkNumpy.vtk_to_numpy(grid.GetPointData().GetScalars())[:] = np.random.RandomState(0).uniform(-0.5, 0.5, (27, 3))
        transform = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLGridTransformNode", "gap scan acquisition transform")
        transform.GetTransformFromParent().SetDisplacementGridData(grid)
        volume.SetAndObserveTransformNodeID(transform.GetID())
        self.assertFalse(transform.IsTransformToWorldLinear())
        self.assertEqual(importer.irregularGeometryVolumes([volume]), [volume])

        for name in ("Red", "Green", "Yellow"):
            slicer.app.layoutManager().sliceWidget(name).sliceController().setProperty("flatStyle", True)
            composite = slicer.app.layoutManager().sliceWidget(name).sliceLogic().GetSliceCompositeNode()
            composite.SetBackgroundVolumeID(volume.GetID())
        model = slicer.app.errorLogModel()
        before = model.rowCount()
        self._pump(30)
        self._wait(300)
        messages = [str(model.index(row, 4).data() or "") for row in range(before, model.rowCount())]
        self.assertEqual([m for m in messages if "expected linear" in m or "not linear" in m], [])

        result = importer.LoadResult()
        result.volumes.append(volume)
        importer._loadToast(result, studioShell)
        self.assertIn("slice is missing", studioShell.toastText())
        # A volume without a transform keeps the plain toast
        volume.SetAndObserveTransformNodeID(None)
        self.assertEqual(importer.irregularGeometryVolumes([volume]), [])
        importer._loadToast(result, studioShell)
        self.assertIn("Loaded gap scan", studioShell.toastText())

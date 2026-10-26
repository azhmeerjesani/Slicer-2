"""Tests of the Library workspace of Slicer Studio (U-02): the current work, the loaded set, what is open, Recent, the folder
card and the footer line.

Run with the main window and testing enabled:

  Slicer --no-splash --testing --python-code "import slicer.testing; slicer.testing.runUnitTest(['<this dir>'], 'StudioLibraryTest'); slicer.app.exit(0)"

Studio never runs by itself under --testing; the tests apply it with ``Shell(force=True)`` and always dispose it. All data is
synthetic (NumPy arrays written with ``slicer.util.saveNode``) and written to temporary folders.
"""

import json
import os
import shutil
import statistics
import tempfile
import time

import numpy as np
import qt
import vtk

import slicer
from slicer import ScriptedLoadableModule

#: Grids of the tests: (dims, spacing, direction signs, origin of voxel (0, 0, 0))
GRID_NLIN6_2MM = ((91, 109, 91), 2.0, (-1, 1, 1), (90.0, -126.0, -72.0))
GRID_NLIN6_1MM = ((182, 218, 182), 1.0, (-1, 1, 1), (91.0, -126.0, -72.0))
#: A scan grid: 1.1 mm voxels, the origin 37 mm from where an MNI grid would put it
GRID_NATIVE = ((48, 56, 48), 1.1, (1, 1, 1), (-40.0 + 37.0, -30.0, -20.0))


class StudioLibraryTest(ScriptedLoadableModule.ScriptedLoadableModuleTest):
    """Library with data. They need the main window."""

    SETTINGS = ("Studio/Template1mmOffer", "Studio/RecentOpenTimes", "Studio/RecentFolders", "Studio/Draw/Layout")

    def setUp(self):
        slicer.mrmlScene.Clear(0)
        self.mainWindow = slicer.util.mainWindow()
        self.layoutManager = slicer.app.layoutManager()
        self.initialLayout = self.layoutManager.layout if self.layoutManager is not None else None
        # A folder outside the temporary folder of the application: Recent never lists files of that one
        self.directory = tempfile.mkdtemp(prefix="StudioLibraryTest")
        self.settingsBackup = {key: qt.QSettings().value(key) for key in self.SETTINGS}
        self.cleanup = []

    def tearDown(self):
        from StudioLib import importer, library, shell

        for function in self.cleanup:
            try:
                function()
            except Exception:
                pass
        library._footerLines.clear()
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
        shutil.rmtree(self.directory, ignore_errors=True)
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

    def _pump(self, count=6):
        for _step in range(count):
            slicer.app.processEvents()

    def _wait(self, milliseconds):
        loop = qt.QEventLoop()
        qt.QTimer.singleShot(milliseconds, loop.quit)
        loop.exec_()

    def _applyStudio(self):
        from StudioLib import shell

        self.assertIsNotNone(self.mainWindow, "Studio tests need the main window")
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        self._pump()
        return studioShell

    def _library(self, studioShell):
        """Switch to Library and return its canvas, with the page refreshed."""
        from StudioLib import library

        studioShell.switchTo("library")
        self._pump(8)
        canvas = library.canvas()
        canvas.refreshData()
        self._pump(4)
        return canvas

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
        node = slicer.mrmlScene.AddNewNodeByClass(className, "StudioLibraryTestVolume")
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

    def _fill(self, segmentationNode, volumeNode, center=None, size=6, rasCenter=None):
        """Put a cube of voxels into the first segment (of the grid of ``volumeNode``). ``rasCenter``: the cube is there."""
        segmentId = segmentationNode.GetSegmentation().GetNthSegmentID(0)
        array = slicer.util.arrayFromVolume(volumeNode)
        mask = np.zeros(array.shape, np.uint8)
        if rasCenter is not None:
            rasToIjk = vtk.vtkMatrix4x4()
            volumeNode.GetRASToIJKMatrix(rasToIjk)
            ijk = [round(v) for v in rasToIjk.MultiplyPoint([*rasCenter, 1.0])[:3]]
            center = (ijk[2], ijk[1], ijk[0])
        elif center is None:
            center = tuple(n // 2 for n in array.shape)
        half = size // 2
        k, j, i = center
        mask[max(0, k - half):k + half, max(0, j - half):j + half, max(0, i - half):i + half] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(mask, segmentationNode, segmentId, volumeNode)
        return int(mask.sum())

    def _template(self, studioShell, fill=False, rasCenter=(-40.0, -20.0, 10.0)):
        """Start on the template (the Start button of Library). Return (template volume, lesion)."""
        from StudioLib import importer

        volumeNode, segmentationNode = importer.startOnTemplate(shell=studioShell)
        self._pump(8)
        if fill:
            self._fill(segmentationNode, volumeNode, rasCenter=rasCenter, size=8)
            self._pump(4)
        return volumeNode, segmentationNode

    @staticmethod
    def _labels(canvas, objectName):
        return [label for label in canvas.widget.findChildren("QLabel", objectName) if label.isVisible()]

    @staticmethod
    def _plain(label):
        return str(label.property("plainText") or label.text)

    def _visibleSegmentations(self):
        return [node for node in slicer.util.getNodesByClass("vtkMRMLSegmentationNode")
                if node.GetDisplayNode() is not None and node.GetDisplayNode().GetVisibility() and node.GetDisplayNode().GetVisibility2D()]

    @staticmethod
    def _editor():
        return slicer.modules.SegmentEditorWidget.editor

    def _panel(self):
        """The Library panel of the Studio module (the inspector), built."""
        widget = slicer.util.getModuleWidget("Studio")
        panel = widget.libraryPanel
        self.assertIsNotNone(panel)
        panel.build()
        panel.refresh()
        self._pump(4)
        return panel

    #
    # Current work
    #

    def test_current_work_card(self):
        """Start on the MNI template: Library shows Current work with the template and its lesion, no empty drop zone and
        no Open buttons in the inspector; Continue in Draw returns to Draw with the same lesion and segment selected.
        """
        studioShell = self._applyStudio()
        volumeNode, segmentationNode = self._template(studioShell)
        editor = self._editor()
        segmentId = editor.currentSegmentID()
        self.assertTrue(segmentId)
        canvas = self._library(studioShell)
        self.assertEqual(studioShell.currentWorkspace(), "library")
        self.assertTrue(canvas.currentWork.widget.isVisible())
        self.assertTrue(canvas.hasData())
        # The 480x220 drop zone is gone (V-02): files are dropped anywhere, as the header caption says; the page starts at the top
        self.assertTrue(canvas.isStrip())
        self.assertFalse(canvas.dropFrame.isVisible())
        self.assertFalse(canvas.dropFull.isVisible())
        self.assertTrue(canvas.captionDropHint.isVisible())
        self.assertIn("anywhere on the window", canvas.captionDropHint.text)
        scanNames = self._labels(canvas, "StudioCurrentWorkScanName")
        self.assertEqual([self._plain(label) for label in scanNames], [volumeNode.GetName()])
        self.assertIn("MNI template", self._plain(self._labels(canvas, "StudioCurrentWorkScanFolder")[0]))
        lesionNames = self._labels(canvas, "StudioCurrentWorkLesionName")
        self.assertEqual([label.property("fullText") for label in lesionNames], ["Lesion"])
        self.assertEqual(canvas.currentWork.closeButton.text, "Close")
        self.assertEqual(canvas.currentWork.continueButton.text, "Continue in Draw →")
        self.assertTrue(canvas.currentWork.continueButton.isVisible())
        # The inspector lists what is open and has no Open buttons (they are on the canvas only)
        panel = self._panel()
        self.assertFalse(panel.openCard.isVisible())
        self.assertFalse(panel.recentCard.isVisible())
        self.assertTrue(panel.openNowCard.widget.isVisible())
        self.assertEqual(panel.openNowCard.rowNames(), [volumeNode.GetName(), segmentationNode.GetName()])
        for name in ("StudioOpenButton", "StudioOpenFolderButton", "StudioStartOnTemplateButton"):
            self.assertFalse([button for button in panel.widget.findChildren("QPushButton", name) if button.isVisible()], name)
        # One click: back in Draw, the same lesion and segment, the same source
        canvas.currentWork.continueButton.click()
        self._pump(8)
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        self.assertEqual(editor.segmentationNode().GetID(), segmentationNode.GetID())
        self.assertEqual(editor.currentSegmentID(), segmentId)
        self.assertEqual(editor.sourceVolumeNode().GetID(), volumeNode.GetID())

    def test_empty_state_unchanged(self):
        """Nothing loaded: the 480x220 drop zone, the three buttons and Recent; a stray segmentation changes nothing."""
        from StudioLib import library

        studioShell = self._applyStudio()
        canvas = self._library(studioShell)
        self.assertFalse(canvas.isStrip())
        self.assertEqual((canvas.dropZone.width, canvas.dropZone.height), (library.COLUMN_WIDTH, library.DROP_ZONE_HEIGHT))
        self.assertTrue(canvas.dropFull.isVisible())
        self.assertFalse(canvas.dropCompact.isVisible())
        self.assertFalse(canvas.dataSection.isVisible())
        self.assertTrue(canvas.openButton.isVisible())
        self.assertTrue(canvas.recentList.widget.isVisible())
        self.assertFalse(canvas.footerFrame.isVisible())
        self.assertTrue(studioShell._panelDock().isHidden())

    def test_stray_segmentation_ignored(self):
        """The empty segmentation that the classic Segment Editor creates keeps the empty state; a figure is not data."""
        from StudioLib import library

        studioShell = self._applyStudio()
        stray = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Segmentation")
        stray.CreateDefaultDisplayNodes()
        self.assertTrue(library.isStraySegmentation(stray))
        self.assertFalse(library.hasData())
        canvas = self._library(studioShell)
        self.assertFalse(canvas.isStrip())
        self.assertFalse(canvas.dataSection.isVisible())
        self.assertTrue(studioShell._panelDock().isHidden())
        self.assertTrue(library.isEmptyState(studioShell))
        # A segment without voxels and without a geometry is the same
        stray.GetSegmentation().AddEmptySegment("Segment_1")
        self.assertTrue(library.isStraySegmentation(stray))
        self.assertFalse(library.hasData())
        # A figure (a picture next to the template) is not data either
        figure = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "case1_fig2")
        slicer.util.updateVolumeFromArray(figure, np.zeros((1, 8, 8), np.int16))
        figure.SetAttribute("Studio.Role", "figure")
        self.assertFalse(library.hasData())
        # A reference geometry, or voxels, make the segmentation data
        scan = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "scan")
        slicer.util.updateVolumeFromArray(scan, np.zeros((8, 8, 8), np.int16))
        stray.SetReferenceImageGeometryParameterFromVolumeNode(scan)
        self.assertFalse(library.isStraySegmentation(stray))
        slicer.mrmlScene.RemoveNode(scan)
        slicer.mrmlScene.RemoveNode(stray)
        self.assertFalse(library.hasData())

    def test_has_data_ignores_figure_only(self):
        from StudioLib import library

        figure = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "fig")
        slicer.util.updateVolumeFromArray(figure, np.zeros((1, 4, 4), np.int16))
        figure.SetAttribute("Studio.Role", "figure")
        self.assertFalse(library.hasData())
        slicer.mrmlScene.RemoveNode(figure)
        scan = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "scan")
        slicer.util.updateVolumeFromArray(scan, np.zeros((4, 4, 4), np.int16))
        self.assertTrue(library.hasData())

    #
    # Save states
    #

    def test_saved_states(self):
        """The save line follows draw.lesionSaveState: Saved, Unsaved changes, Not saved yet and, after the scene was saved
        and opened again, Last saved as … (never Saved or Unsaved changes).
        """
        from StudioLib import draw

        studioShell = self._applyStudio()
        volumeNode, segmentationNode = self._template(studioShell, fill=True)
        canvas = self._library(studioShell)
        state = self._labels(canvas, "StudioCurrentWorkLesionState")[0]
        self.assertEqual(self._plain(state), "Not saved yet")
        self.assertEqual(canvas.currentWork.closeButton.text, "Close… (unsaved)")
        path = os.path.join(self.directory, "sub-01_lesion.nii.gz")
        self.assertEqual(draw.saveLesionDialog(studioShell, path=path), path)
        canvas = self._library(studioShell)
        state = self._labels(canvas, "StudioCurrentWorkLesionState")[0]
        self.assertTrue(self._plain(state).startswith("Saved · sub-01_lesion.nii.gz"), self._plain(state))
        self.assertEqual(canvas.currentWork.closeButton.text, "Close")
        # One more voxel
        segmentId = segmentationNode.GetSegmentation().GetNthSegmentID(0)
        array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, volumeNode)
        array = np.array(array)
        zero = np.argwhere(array == 0)[0]
        array[tuple(zero)] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, volumeNode)
        self._pump(4)
        canvas = self._library(studioShell)
        state = self._labels(canvas, "StudioCurrentWorkLesionState")[0]
        self.assertTrue(self._plain(state).startswith("Unsaved changes"), self._plain(state))
        self.assertEqual(canvas.currentWork.closeButton.text, "Close… (unsaved)")
        # Save again, keep the scene, close, reload: only the attributes of the earlier save are known
        self.assertEqual(draw.saveLesionDialog(studioShell, path=path), path)
        scenePath = os.path.join(self.directory, "state.mrb")
        self.assertTrue(slicer.util.saveScene(scenePath))
        slicer.mrmlScene.Clear(0)
        self._pump(4)
        slicer.util.loadScene(scenePath)
        self._pump(8)
        canvas = self._library(studioShell)
        lesions = list(slicer.util.getNodesByClass("vtkMRMLSegmentationNode"))
        self.assertEqual(len(lesions), 1)
        state = self._labels(canvas, "StudioCurrentWorkLesionState")[0]
        text = self._plain(state)
        self.assertTrue(text.startswith("Last saved as sub-01_lesion.nii.gz"), text)
        self.assertNotIn("Saved ·", text)
        self.assertNotIn("Unsaved", text)

    #
    # Recent
    #

    def test_recent_excludes_cache(self):
        """Recent never lists a file under the cache or temporary folders, and says "opened … ago" for files opened in Studio."""
        from StudioLib import importer, library

        inside = {}
        for label, folder in (("cache", slicer.app.cachePath), ("temporary", slicer.app.temporaryPath)):
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, "StudioLibraryTest-%s.nii.gz" % label)
            with open(path, "wb") as f:
                f.write(b"x")
            inside[label] = path
            self.cleanup.append(lambda path=path: os.remove(path))
        opened = os.path.join(self.directory, "sub-01_opened.nii.gz")
        modified = os.path.join(self.directory, "sub-02_modified.nii.gz")
        longName = os.path.join(self.directory, "sub-03_space-MNI152NLin6Asym_res-02_desc-lesion_mask_AvgR.nii.gz")
        for path in (opened, modified, longName):
            with open(path, "wb") as f:
                f.write(b"x")
        self.assertTrue(library.isInternalPath(inside["cache"]))
        self.assertTrue(library.isInternalPath(inside["temporary"]))
        self.assertFalse(library.isInternalPath(opened))

        menu = qt.QMenu()
        for path in (inside["cache"], inside["temporary"], opened, modified, longName):
            action = menu.addAction(os.path.basename(path))
            action.setProperty("fileParameters", {"fileName": path, "fileType": "VolumeFile"})
        original = library.recentlyLoadedMenu
        library.recentlyLoadedMenu = lambda: menu
        self.cleanup.append(lambda: setattr(library, "recentlyLoadedMenu", original))
        importer.recordOpenTimes([opened], when=time.time() - 120)
        entries = library.recentEntries()
        self.assertEqual([entry.path for entry in entries], [opened, modified, longName])
        self.assertTrue(entries[0].timeText().startswith("opened 2 min ago"), entries[0].timeText())
        self.assertTrue(entries[1].timeText().startswith("modified "), entries[1].timeText())
        # Rows: the same text, the end of a long file name stays visible when the row is narrow
        recent = library.RecentList()
        host = qt.QWidget()
        hostLayout = qt.QVBoxLayout(host)
        hostLayout.addWidget(recent.widget)
        host.setFixedWidth(330)
        host.show()
        self._pump(6)
        recent.refresh()
        self._pump(6)
        names = {row[0]: row for row in recent.rows}
        timeTexts = [str(row[3].text) for row in recent.rows if not row[0].isHidden()]
        self.assertTrue(timeTexts[0].startswith("opened"), timeTexts)
        self.assertTrue(timeTexts[1].startswith("modified"), timeTexts)
        shownNames = [str(row[1].text) for row in recent.rows if not row[0].isHidden()]
        self.assertTrue(shownNames[2].endswith(os.path.basename(longName)[-library.RECENT_NAME_KEEP_CHARS:]), shownNames)
        self.assertEqual(len(names), len(recent.rows))
        recent.dispose()
        host.hide()
        host.deleteLater()

    def test_recent_folders_first(self):
        """The folders opened in Studio come first as rows that open the folder card again."""
        from StudioLib import importer, library

        folder = os.path.join(self.directory, "cohort")
        os.makedirs(folder)
        original = library.recentlyLoadedMenu
        library.recentlyLoadedMenu = lambda: None
        self.cleanup.append(lambda: setattr(library, "recentlyLoadedMenu", original))
        importer.recordFolder(folder, "nifti", 20)
        recent = library.RecentList()
        recent.refresh()
        self.assertGreaterEqual(len(recent.entries), 1)
        first = recent.entries[0]
        self.assertEqual(first.kind, "folder")
        self.assertEqual(first.name, "cohort")
        self.assertIn("20 NIfTI files", first.folder)
        self.assertEqual(recent.rows[0][0].property("recentKind"), "folder")
        # A folder that is gone is not listed
        shutil.rmtree(folder)
        recent.refresh()
        self.assertTrue(all(entry.kind != "folder" for entry in recent.entries))
        recent.dispose()

    #
    # Folder card
    #

    def _writeTinyFiles(self, count, subdirectory="cohort"):
        grid = ((4, 4, 4), 1.0, (1, 1, 1), (0.0, 0.0, 0.0))
        for index in range(count):
            self._writeVolume("sub-%02d_T1w.nii.gz" % (index + 1), grid, np.full((4, 4, 4), index + 1, np.int16), subdirectory=subdirectory)
        return os.path.join(self.directory, subdirectory)

    def test_folder_card_large(self):
        """A folder of more than 20 files: the reason is in the card and "Open one subject…" is enabled."""
        from StudioLib import importer, library

        studioShell = self._applyStudio()
        folder = self._writeTinyFiles(25)
        with open(os.path.join(folder, "participants.tsv"), "w", encoding="utf-8") as f:
            f.write("participant_id\tgroup\tage\n" + "".join("sub-%02d\tpatient\t%d\n" % (i + 1, 40 + i) for i in range(25)))
        before = slicer.mrmlScene.GetNumberOfNodes()
        self.assertEqual(importer.loadFolder(folder, studioShell), "nifti")
        self._pump(6)
        self.assertEqual(studioShell.currentWorkspace(), "library")
        card = library.canvas().folderCard()
        self.assertIsNotNone(card)
        self.assertTrue(card.widget.isVisible())
        self.assertTrue(card.reasonLabel.isVisible())
        self.assertIn("25 files", card.reasonLabel.text)
        self.assertIn("open one subject at a time", card.reasonLabel.text)
        self.assertTrue(card.openOneButton.isVisible())
        self.assertTrue(card.openOneButton.isEnabled())
        self.assertFalse(card.loadAllButton.isEnabled())
        self.assertTrue(card.participantsLabel.isVisible())
        self.assertIn("participants.tsv · 25 rows · group, age", card.participantsLabel.text)
        if library._studyUi() is None:
            self.assertIsNone(card.createStudyButton)
        self.assertEqual(slicer.mrmlScene.GetNumberOfNodes(), before)
        # Open one subject: the file dialog starts in the folder, the picked file is loaded
        asked = []
        original = library._pickFiles
        library._pickFiles = lambda startFolder: asked.append(startFolder) or [os.path.join(folder, "sub-03_T1w.nii.gz")]
        self.cleanup.append(lambda: setattr(library, "_pickFiles", original))
        card.openOneButton.click()
        self._pump(10)
        self.assertEqual(asked, [folder])
        names = [node.GetName() for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")]
        self.assertEqual(len(names), 1)
        self.assertIn("sub-03", names[0])
        self.assertIsNone(library.canvas().folderCard())

    def test_folder_card_small(self):
        """Up to 20 files: Load all and Open one subject… are there; the primary button is Create study when studies are
        available (W3-07: the study is the way on for a folder of subjects; without studies it is Load all).
        """
        from StudioLib import importer, library

        studioShell = self._applyStudio()
        folder = self._writeTinyFiles(5, "small")
        importer.loadFolder(folder, studioShell)
        self._pump(6)
        card = library.canvas().folderCard()
        self.assertIsNotNone(card)
        self.assertIsNone(card.reasonLabel)
        self.assertTrue(card.loadAllButton.isEnabled())
        self.assertTrue(card.openOneButton.isEnabled())
        self.assertIsNotNone(card.createStudyButton)  # StudioLib.study_ui is part of this build
        self.assertEqual(card.createStudyButton.property("slicerRole"), "primary")
        self.assertEqual(card.loadAllButton.property("slicerRole"), "secondary")
        self.assertEqual(card.openOneButton.property("slicerRole"), "secondary")
        card.close()
        self._pump(4)

    #
    # Loaded set
    #

    def _writeCohort(self, count=6, grid=GRID_NLIN6_2MM, folder="cohort", participants=True):
        paths = []
        for index in range(count):
            paths.append(self._writeMask("sub-%02d_lesion.nii.gz" % (index + 1), grid, center=(10 + 5 * index, 50, 40), subdirectory=folder))
        directory = os.path.join(self.directory, folder)
        if participants:
            with open(os.path.join(directory, "participants.tsv"), "w", encoding="utf-8") as f:
                f.write("participant_id\tgroup\tage\tsex\tsymptom_score\n" + "".join(
                    "sub-%02d\tpatient\t%d\tF\t%d\n" % (i + 1, 40 + i, i) for i in range(count)))
        return paths

    def _segmentationFamily(self):
        counts = {}
        for className in ("vtkMRMLSegmentationNode", "vtkMRMLSegmentationDisplayNode", "vtkMRMLSegmentationStorageNode"):
            counts[className] = len(slicer.util.getNodesByClass(className))
        return counts

    def test_loaded_set(self):
        """Six lesion masks loaded at once: the card counts them, one lesion is shown after Review in Draw, Close set removes
        exactly the nodes of the load.
        """
        from StudioLib import importer, library

        studioShell = self._applyStudio()
        paths = self._writeCohort(6)
        before = self._segmentationFamily()
        self.assertEqual(before, {"vtkMRMLSegmentationNode": 0, "vtkMRMLSegmentationDisplayNode": 0, "vtkMRMLSegmentationStorageNode": 0})
        importer.loadFiles(paths, studioShell)
        self._pump(10)
        loaded = importer.loadedSet()
        self.assertIsNotNone(loaded)
        self.assertEqual(len(loaded), 6)
        canvas = self._library(studioShell)
        card = canvas.loadedSet
        self.assertTrue(card.widget.isVisible())
        summary = str(card.summaryLabel.text)
        self.assertIn("6 lesion masks", summary)
        self.assertIn("grid 91\u00d7109\u00d791", summary)
        self.assertIn("MNI152NLin6Asym 2 mm", summary)
        self.assertTrue(card.tableLabel.isVisible())
        self.assertIn("participants.tsv · 6 rows · group, age, sex, symptom_score", card.tableLabel.text)
        subjects = card.subjects()
        self.assertEqual(len(subjects), 6)
        self.assertTrue(subjects[0].startswith("●"), subjects)
        self.assertTrue(subjects[0].endswith("sub-01"))
        self.assertFalse(any(subject.startswith("●") for subject in subjects[1:]))
        # The set is not a lesion of the Current work card
        self.assertFalse(canvas.currentWork.widget.isVisible() and self._labels(canvas, "StudioCurrentWorkLesionName"))
        # The inspector has one row for the set
        panel = self._panel()
        self.assertEqual([item["kind"] for item in library.openNowItems() if item["kind"] == "set"], ["set"])
        self.assertIn("6 lesion masks", panel.openNowCard.rowNames())
        # Review in Draw: one lesion at a time
        card.reviewButton.click()
        self._pump(10)
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        self.assertEqual(len(self._visibleSegmentations()), 1)
        loaded.next()
        self._pump(4)
        self.assertEqual(len(self._visibleSegmentations()), 1)
        self.assertEqual(loaded.index, 1)
        # The card marks the current subject
        canvas = self._library(studioShell)
        subjects = canvas.loadedSet.subjects()
        self.assertTrue(subjects[1].startswith("●"), subjects)
        # Close set removes exactly the six lesions with their display and storage nodes
        canvas.loadedSet.closeButton.click()
        self._pump(8)
        self.assertEqual(self._segmentationFamily(), before)
        self.assertIsNone(importer.loadedSet())
        self.assertFalse(canvas.loadedSet.widget.isVisible())

    def test_loaded_set_close_asks_for_unsaved_edits(self):
        """Closing a set asks only when one of its lesions has edits that are not saved."""
        from StudioLib import importer, library

        studioShell = self._applyStudio()
        importer.loadFiles(self._writeCohort(3, participants=False), studioShell)
        self._pump(10)
        loaded = importer.loadedSet()
        asked = []
        original = library._confirmClose
        library._confirmClose = lambda text, informative="": asked.append(text) or False
        self.cleanup.append(lambda: setattr(library, "_confirmClose", original))
        # Unchanged lesions: no question
        self.assertTrue(all(not library.isLesionUnsaved(node) for node in loaded.nodes()))
        # Edit one: asked, and the user keeps the set
        lesion = loaded.nodes()[1]
        segmentId = lesion.GetSegmentation().GetNthSegmentID(0)
        # A volume on the grid of the lesion edits it (and goes again)
        dims = GRID_NLIN6_2MM[0]
        grid = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "grid")
        slicer.util.updateVolumeFromArray(grid, np.zeros((dims[2], dims[1], dims[0]), np.int16))
        grid.SetIJKToRASMatrix(self._ijkToRas(GRID_NLIN6_2MM))
        array = np.array(slicer.util.arrayFromSegmentBinaryLabelmap(lesion, segmentId, grid))
        array[tuple(np.argwhere(array == 0)[0])] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, lesion, segmentId, grid)
        slicer.mrmlScene.RemoveNode(grid)
        self._pump(4)
        self.assertTrue(library.isLesionUnsaved(lesion))
        self.assertFalse(library.closeLoadedSet())
        self.assertEqual(len(asked), 1)
        self.assertIn("1 lesions", asked[0])
        self.assertEqual(len(loaded), 3)
        # The user closes it
        library._confirmClose = lambda text, informative="": True
        self.assertTrue(library.closeLoadedSet())
        self.assertEqual(self._segmentationFamily()["vtkMRMLSegmentationNode"], 0)

    def test_space_label_both(self):
        """The grid line of the set names the space when there is one, else says the space is unknown (both texts render)."""
        from StudioLib import importer, library, space

        studioShell = self._applyStudio()
        importer.loadFiles(self._writeCohort(3, participants=False), studioShell)
        self._pump(10)
        first = importer.loadedSet().nodes()[0]
        self.assertEqual(library._gridText(first), "grid 91\u00d7109\u00d791 · MNI152NLin6Asym 2 mm")
        original = space.spaceOf
        self.cleanup.append(lambda: setattr(space, "spaceOf", original))
        space.spaceOf = lambda node: None
        self.assertEqual(library._gridText(first), "grid 91\u00d7109\u00d791, 2 mm, space unknown")
        canvas = self._library(studioShell)
        self.assertIn("space unknown", canvas.loadedSet.summaryLabel.text)
        space.spaceOf = lambda node: ("MNI152NLin6Asym", 2)
        canvas.loadedSet._signature = None
        canvas = self._library(studioShell)
        self.assertIn("MNI152NLin6Asym 2 mm", canvas.loadedSet.summaryLabel.text)

    #
    # Side words
    #

    def test_hemisphere_mni_only(self):
        """A lesion in x < 0 on the template grid reads "Left"; the same lesion on a grid that is not an MNI grid has no side word."""
        from StudioLib import importer, library

        studioShell = self._applyStudio()
        _volumeNode, segmentationNode = self._template(studioShell, fill=True, rasCenter=(-40.0, -20.0, 10.0))
        summary = library.lesionSummary(segmentationNode, compute=True)
        self.assertEqual(summary["side"], "Left")
        self.assertTrue(summary["text"].startswith("Left · "), summary["text"])
        self.assertTrue(summary["text"].endswith("1 part"), summary["text"])
        canvas = self._library(studioShell)
        metrics = self._labels(canvas, "StudioCurrentWorkLesionMetrics")
        self.assertTrue(self._plain(metrics[0]).startswith("Left · "), self._plain(metrics[0]))
        # The same cube on a scan grid that is not MNI (1.1 mm voxels, origin 37 mm away)
        slicer.mrmlScene.Clear(0)
        self._pump(4)
        dims = GRID_NATIVE[0]
        scan = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "patient_T1")
        slicer.util.updateVolumeFromArray(scan, np.zeros((dims[2], dims[1], dims[0]), np.int16))
        scan.SetIJKToRASMatrix(self._ijkToRas(GRID_NATIVE))
        lesion = importer.createLesionSegmentation(scan, "Lesions (patient_T1)")
        self._fill(lesion, scan, rasCenter=(-25.0, 0.0, 0.0), size=10)
        summary = library.lesionSummary(lesion, compute=True)
        self.assertIsNone(summary["side"])
        self.assertNotIn("Left", summary["text"])
        self.assertNotIn("Right", summary["text"])
        self.assertTrue(summary["text"].endswith("mL · 1 part"), summary["text"])
        importer.selectInSegmentEditor(lesion, scan)
        canvas = self._library(studioShell)
        metrics = self._labels(canvas, "StudioCurrentWorkLesionMetrics")
        self.assertEqual(len(metrics), 1)
        self.assertNotIn("Left", self._plain(metrics[0]))
        self.assertNotIn("Right", self._plain(metrics[0]))
        self.assertNotIn("hemisphere", self._plain(metrics[0]))
        self.assertEqual(self._plain(self._labels(canvas, "StudioCurrentWorkScanName")[0]), "patient_T1")

    def test_metrics_cold_cache(self):
        """A cold metrics cache reads "…" and is computed after the switch (one timer tick), not in it."""
        from StudioLib import draw, library

        studioShell = self._applyStudio()
        _volumeNode, segmentationNode = self._template(studioShell, fill=True)
        draw._metricsCache.clear()
        self.assertIsNone(draw.lesionMetrics(segmentationNode))
        studioShell.switchTo("library")
        canvas = library.canvas()
        canvas.refreshData()
        metrics = self._labels(canvas, "StudioCurrentWorkLesionMetrics")
        self.assertEqual(self._plain(metrics[0]), "…")
        self._pump(10)
        self._wait(50)
        self.assertNotEqual(self._plain(self._labels(canvas, "StudioCurrentWorkLesionMetrics")[0]), "…")
        self.assertIsNotNone(draw.lesionMetrics(segmentationNode))

    #
    # New lesion, Close
    #

    def test_new_lesion_label(self):
        """With a non-empty lesion on the template Library offers "New lesion on template": it adds a lesion and keeps the old."""
        from StudioLib import library

        studioShell = self._applyStudio()
        volumeNode, segmentationNode = self._template(studioShell)
        canvas = self._library(studioShell)
        # An empty template lesion: the plain Start on MNI template continues it, no "New lesion" button
        self.assertFalse(canvas.currentWork.newLesionRow.isVisible())
        self._fill(segmentationNode, volumeNode)
        canvas = self._library(studioShell)
        self.assertTrue(canvas.currentWork.newLesionRow.isVisible())
        self.assertEqual(canvas.currentWork.newLesionButton.text, "New lesion on template")
        before = len(slicer.util.getNodesByClass("vtkMRMLSegmentationNode"))
        canvas.currentWork.newLesionButton.click()
        self._pump(10)
        self.assertEqual(len(slicer.util.getNodesByClass("vtkMRMLSegmentationNode")), before + 1)
        self.assertTrue(slicer.mrmlScene.IsNodePresent(segmentationNode))
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        edited = self._editor().segmentationNode()
        self.assertNotEqual(edited.GetID(), segmentationNode.GetID())
        canvas = self._library(studioShell)
        # Both lesions are listed; the old one is hidden
        names = [label.property("fullText") for label in self._labels(canvas, "StudioCurrentWorkLesionName")]
        self.assertEqual(len(names), 2)
        self.assertTrue(any(name.endswith("hidden") for name in names), names)
        self.assertFalse(library.OpenNowCard.isShown(segmentationNode))

    def test_close_button(self):
        """Close runs File > Close scene (module function ``closeScene``)."""
        from StudioLib import library

        studioShell = self._applyStudio()
        self._template(studioShell)
        canvas = self._library(studioShell)
        calls = []
        original = library.closeScene
        library.closeScene = lambda: calls.append(1)
        self.cleanup.append(lambda: setattr(library, "closeScene", original))
        canvas.currentWork.closeButton.click()
        self.assertEqual(calls, [1])

    def test_scene_close_empties_the_page(self):
        """Closing the scene brings the empty state back."""
        studioShell = self._applyStudio()
        self._template(studioShell)
        canvas = self._library(studioShell)
        self.assertTrue(canvas.isStrip())
        slicer.mrmlScene.Clear(0)
        self._pump(10)
        self.assertFalse(canvas.isStrip())
        self.assertEqual((canvas.dropZone.width, canvas.dropZone.height), (480, 220))
        self.assertTrue(studioShell._panelDock().isHidden())

    #
    # Open now
    #

    def test_open_now_card(self):
        """The inspector lists what is open; a lesion has an eye bound to its display node, every row a ⋯ menu with Close."""
        from StudioLib import library

        studioShell = self._applyStudio()
        volumeNode, segmentationNode = self._template(studioShell)
        self._library(studioShell)
        panel = self._panel()
        card = panel.openNowCard
        self.assertEqual(card.rowNames(), [volumeNode.GetName(), segmentationNode.GetName()])
        rows = card._rows
        self.assertEqual(rows[0][0].height, library.OPEN_NOW_ROW_HEIGHT)
        self.assertIsNone(rows[0][1])
        eye = rows[1][1]
        self.assertIsNotNone(eye)
        self.assertTrue(library.OpenNowCard.isShown(segmentationNode))
        eye.click()
        self._pump(4)
        self.assertFalse(segmentationNode.GetDisplayNode().GetVisibility())
        eye.click()
        self._pump(4)
        self.assertTrue(segmentationNode.GetDisplayNode().GetVisibility())
        # A change made elsewhere moves the eye
        segmentationNode.GetDisplayNode().SetVisibility2D(False)
        self._pump(4)
        self.assertEqual(eye.property("shown"), False)
        segmentationNode.GetDisplayNode().SetVisibility2D(True)
        # The menus
        menu = rows[1][0].findChild("QMenu", "StudioOpenNowMenu")
        actions = {str(action.objectName): action for action in menu.actions()}
        self.assertEqual(set(actions), {"StudioOpenNowRename", "StudioOpenNowOpenInDraw", "StudioOpenNowShowInData", "StudioOpenNowExportNifti",
                                        "StudioOpenNowClose"})
        self.assertEqual(actions["StudioOpenNowClose"].text, "Close")
        # "Export as NIfTI…" (A-11): the empty lesion has nothing to write (disabled, reason as tooltip); the template can be written
        self.assertFalse(actions["StudioOpenNowExportNifti"].enabled)
        self.assertTrue(actions["StudioOpenNowExportNifti"].toolTip)
        volumeActions = {str(action.objectName): action for action in rows[0][0].findChild("QMenu", "StudioOpenNowMenu").actions()}
        self.assertTrue(volumeActions["StudioOpenNowExportNifti"].enabled)
        self.assertFalse(any("elete" in str(action.text) for row in rows for action in row[0].findChild("QMenu", "StudioOpenNowMenu").actions()))
        # Rename
        library.renameNode(segmentationNode, "Infarct")
        self.assertEqual(segmentationNode.GetName(), "Infarct")
        self.assertEqual(segmentationNode.GetSegmentation().GetNthSegment(0).GetName(), "Infarct")
        card.refresh()
        self.assertEqual(card.rowNames(), [volumeNode.GetName(), "Infarct"])
        # Close the (empty) lesion row: no question, the lesion with its display node goes
        actions = {str(action.objectName): action for action in card._rows[1][0].findChild("QMenu", "StudioOpenNowMenu").actions()}
        actions["StudioOpenNowClose"].trigger()
        self._pump(6)
        self.assertEqual(len(slicer.util.getNodesByClass("vtkMRMLSegmentationNode")), 0)
        self.assertEqual(len(slicer.util.getNodesByClass("vtkMRMLSegmentationDisplayNode")), 0)

    def test_close_row_asks_for_unsaved_lesion(self):
        from StudioLib import library

        studioShell = self._applyStudio()
        _volumeNode, segmentationNode = self._template(studioShell, fill=True)
        asked = []
        original = library._confirmClose
        library._confirmClose = lambda text, informative="": asked.append(text) or False
        self.cleanup.append(lambda: setattr(library, "_confirmClose", original))
        self.assertFalse(library.closeNode(segmentationNode))
        self.assertEqual(len(asked), 1)
        self.assertTrue(slicer.mrmlScene.IsNodePresent(segmentationNode))
        library._confirmClose = lambda text, informative="": True
        self.assertTrue(library.closeNode(segmentationNode))
        self.assertFalse(slicer.mrmlScene.IsNodePresent(segmentationNode))

    def test_panel_in_classic(self):
        """Without Studio (Classic) the module panel keeps the Open buttons and Recent, and no canvas exists."""
        from StudioLib import library, shell

        self.assertIsNone(shell.instance())
        panel = self._panel()
        self.assertFalse(panel.withCanvas())
        self.assertFalse(panel.openCard.isHidden())
        self.assertFalse(panel.recentCard.isHidden())
        self.assertIsNotNone(panel.openButton)
        self.assertIsNotNone(panel.templateButton)
        # With data, what is open is listed too
        scan = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "scan")
        slicer.util.updateVolumeFromArray(scan, np.zeros((4, 4, 4), np.int16))
        panel.refresh()
        self.assertFalse(panel.openNowCard.widget.isHidden())
        self.assertEqual(panel.openNowCard.rowNames(), ["scan"])
        self.assertFalse(panel.openCard.isHidden())
        self.assertTrue(library.hasData())

    def test_template_offer_line(self):
        """The 1 mm template offer is the last line of the Current work card (never over the views); its buttons work."""
        from StudioLib import importer

        studioShell = self._applyStudio()
        _volumeNode, _segmentationNode = self._template(studioShell, fill=True)
        banner = importer.showTemplateDownloadBanner()
        if banner is None:
            self.skipTest("the 1 mm template is not offered in this build")
        canvas = self._library(studioShell)
        lines = [frame for frame in canvas.widget.findChildren("QFrame", "StudioCurrentWorkOffer") if frame.isVisible()]
        self.assertEqual(len(lines), 1)
        text = [label.text for label in lines[0].findChildren("QLabel", "StudioNoticeText")]
        self.assertTrue(any("1 mm template" in t for t in text), text)
        buttons = {str(button.text): button for button in lines[0].findChildren("QToolButton", "StudioNoticeButton")}
        self.assertEqual(set(buttons), {"Download", "Not now", "Details"})
        # The line is the last one of the card
        card = canvas.currentWork.widget
        self.assertGreater(lines[0].mapTo(card, qt.QPoint(0, 0)).y(), canvas.currentWork.dataLink.mapTo(card, qt.QPoint(0, 0)).y())
        buttons["Not now"].click()
        self._pump(6)
        self.assertEqual(str(qt.QSettings().value("Studio/Template1mmOffer")), "dismissed")
        canvas = self._library(studioShell)
        self.assertFalse([frame for frame in canvas.widget.findChildren("QFrame", "StudioCurrentWorkOffer") if frame.isVisible()])

    def test_dialogs(self):
        """The confirmation before a lesion with unsaved changes is closed, and the Rename dialog (answered with timers)."""
        from StudioLib import library

        def answerWith(callback):
            def run():
                modal = slicer.app.activeModalWidget()
                self.assertIsNotNone(modal)
                callback(modal)
            qt.QTimer.singleShot(200, run)

        def clickButton(text):
            def click(modal):
                for button in modal.findChildren("QAbstractButton"):
                    if str(button.text).replace("&", "") == text:
                        button.click()
                        return
                self.fail(f"no button {text}")
            return click

        answerWith(clickButton("Close"))
        self.assertTrue(library._confirmClose("Lesion has changes that are not saved.", "Save it first."))
        answerWith(clickButton("Cancel"))
        self.assertFalse(library._confirmClose("Lesion has changes that are not saved."))

        def rename(modal):
            modal.findChild("QLineEdit").setText("Infarct")
            modal.accept()

        answerWith(rename)
        self.assertEqual(library._askName("Lesion"), "Infarct")
        answerWith(lambda modal: modal.reject())
        self.assertIsNone(library._askName("Lesion"))

    #
    # Footer line
    #

    def test_footer_line(self):
        """One 32 px text2 line at the bottom of the canvas, with link-style actions; it survives a new canvas."""
        from StudioLib import library

        studioShell = self._applyStudio()
        canvas = self._library(studioShell)
        self.assertFalse(canvas.footerFrame.isVisible())
        clicked = []
        library.setFooterLine("research", "Research use only, not for clinical use.", [("Don't show again", lambda: clicked.append(1))])
        self._pump(4)
        self.assertTrue(canvas.footerFrame.isVisible())
        self.assertEqual(canvas.footerText.text, "Research use only, not for clinical use.")
        self.assertGreaterEqual(canvas.footerFrame.height, library.FOOTER_LINE_HEIGHT)
        self.assertEqual(canvas.footerLineKey(), "research")
        links = [button for button in canvas.footerFrame.findChildren("QPushButton", "StudioLibraryFooterAction") if button.isVisible()]
        self.assertEqual([str(button.text) for button in links], ["Don't show again"])
        links[0].click()
        self.assertEqual(clicked, [1])
        # Below Recent
        recentBottom = canvas.recentList.widget.mapTo(canvas.widget, qt.QPoint(0, canvas.recentList.widget.height)).y()
        footerTop = canvas.footerFrame.mapTo(canvas.widget, qt.QPoint(0, 0)).y()
        self.assertGreater(footerTop, recentBottom)
        # A line set later hides the first one until it is cleared
        library.setFooterLine("other", "Another line")
        self.assertEqual(canvas.footerText.text, "Another line")
        library.clearFooterLine("other")
        self.assertEqual(canvas.footerText.text, "Research use only, not for clinical use.")
        # The canvas is created again (Library disposes it): the line is applied again
        canvas.dispose()
        library._canvas = None
        studioShell.switchTo("draw")
        studioShell.switchTo("library")
        self._pump(8)
        again = library.canvas()
        self.assertIsNot(again, canvas)
        self.assertEqual(again.footerLineKey(), "research")
        library.clearFooterLine("research")
        self.assertFalse(again.footerFrame.isVisible())
        self.assertIsNone(again.footerLineKey())

    #
    # Speed
    #

    def test_switch_budget(self):
        """With a 1 mm lesion of 8000 voxels loaded, showing Library costs little: the hook of Library (``library.enter``: the
        canvas, the Current work card, the metrics from the cache) stays under 15 ms, and the whole switch from Draw to Library
        under 75 ms. The budget of StudioBench is 50 ms for a workspace switch; it also holds the module switch, the
        paint and the layout of the shell (about 55 ms with the old Library page as well), which Library does not own.
        """
        from StudioLib import importer, library

        studioShell = self._applyStudio()
        array = np.zeros((182, 218, 182), np.uint8)
        array[100:120, 100:120, 60:80] = 1
        path = self._writeVolume("sub-01_lesion.nii.gz", GRID_NLIN6_1MM, array, labelmap=True)
        importer.loadFiles([path], studioShell)
        self._pump(10)
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        lesion = self._editor().segmentationNode()
        self.assertIsNotNone(lesion)
        hook = []
        original = library.enter

        def timedEnter(shell):
            start = time.perf_counter()
            try:
                return original(shell)
            finally:
                hook.append((time.perf_counter() - start) * 1000.0)

        library.enter = timedEnter
        self.cleanup.append(lambda: setattr(library, "enter", original))
        times = []
        for repetition in range(7):
            studioShell.switchTo("draw")
            self._pump(4)
            start = time.perf_counter()
            studioShell.switchTo("library")
            self._pump(2)
            times.append((time.perf_counter() - start) * 1000.0)
        library.enter = original
        median = statistics.median(times[1:])
        hookMedian = statistics.median(hook[1:])
        print(f"Draw -> Library with a 1 mm lesion: median {median:.1f} ms (runs {[round(t, 1) for t in times]}), "
              f"library.enter median {hookMedian:.1f} ms")
        canvas = library.canvas()
        self._wait(100)
        metrics = [self._plain(label) for label in self._labels(canvas, "StudioCurrentWorkLesionMetrics")]
        self.assertEqual(len(metrics), 1)
        self.assertIn("8.00 mL", metrics[0])
        self.assertLessEqual(hookMedian, 15.0, hook)
        self.assertLessEqual(median, 75.0, times)

    #
    # Tours
    #

    def test_library_tour_shows_current_work(self):
        """The Library tour goes through the Current work card when data is open, and leaves the step out when nothing is open."""
        from StudioLib import tour

        studioShell = self._applyStudio()
        self.cleanup.append(tour.stopTour)
        self._library(studioShell)
        tour.startTour("library")
        self._pump(6)
        player = tour.activePlayer()
        stepsEmpty = []
        for _step in range(10):
            stepsEmpty.append(player.currentStep().id)
            if not player.active:
                break
            player.next()
            if not player.active:
                break
        self.assertNotIn("current-work", stepsEmpty)
        tour.stopTour()
        self._pump(4)
        self._template(studioShell)
        self._library(studioShell)
        tour.startTour("library")
        self._pump(6)
        player = tour.activePlayer()
        seen = []
        for _step in range(10):
            seen.append(player.currentStep().id)
            if player.currentStep().id == "current-work":
                self.assertEqual(player.target.objectName, "StudioCurrentWorkCard")
                break
            player.next()
            if not player.active:
                break
        self.assertIn("current-work", seen)
        # The first step still finds the drop strip
        self.assertEqual(seen[0], "drop")
        tour.stopTour()

    def test_data_attributes_not_written(self):
        """Library writes no setting by itself (only the importer writes its Studio/* keys)."""
        studioShell = self._applyStudio()
        before = {key: qt.QSettings().value(key) for key in ("Studio/RecentFolders", "Studio/RecentOpenTimes")}
        self._template(studioShell)
        self._library(studioShell)
        after = {key: qt.QSettings().value(key) for key in before}
        self.assertEqual(json.dumps(before, default=str), json.dumps(after, default=str))

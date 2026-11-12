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
        hint = canvas.captionDropHint
        self.assertIn("anywhere on the window", hint.property("fullText"))
        self.assertIn(hint.property("fullText"), hint.toolTip)
        # In a narrow title column (1366x768 with the inspector open) the hint is elided, not cut in the middle of a letter
        hint.setFixedWidth(150)
        slicer.app.processEvents()
        self.assertTrue(hint.text.endswith("\u2026") or "\u2026" in hint.text, hint.text)
        self.assertLess(len(hint.text), len(hint.property("fullText")))
        self.assertLessEqual(qt.QFontMetrics(hint.font).horizontalAdvance(hint.text), 150)
        hint.setMinimumWidth(0)
        hint.setMaximumWidth(16777215)
        slicer.app.processEvents()
        scanNames = self._labels(canvas, "StudioCurrentWorkScanName")
        self.assertEqual([self._plain(label) for label in scanNames], [volumeNode.GetName()])
        self.assertIn("MNI template", self._plain(self._labels(canvas, "StudioCurrentWorkScanFolder")[0]))
        lesionNames = self._labels(canvas, "StudioCurrentWorkLesionName")
        self.assertEqual([label.property("fullText") for label in lesionNames], [segmentationNode.GetName()])
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

    def test_lesion_row_name_is_not_elided_when_the_row_has_room(self):
        """A hidden lesion's name ("sub-02_lesion · hidden") shows whole in a row with room, also in the compact layout."""
        from StudioLib import library
        node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "sub-02_lesion")
        try:
            for width in (240, 700):
                block = library._LesionBlock(node, 0)
                parent = qt.QWidget()
                layout = qt.QVBoxLayout(parent)
                layout.setContentsMargins(0, 0, 0, 0)
                layout.addWidget(block.widget)
                block.metricsLabel.setProperty("fullText", "Left \u00b7 4.22 mL \u00b7 1 part")
                block.setChipWidth("Saved in the project")
                title = "sub-02_lesion \u00b7 hidden"
                block.setName(title, "sub-02_lesion")
                parent.resize(width, 80)
                parent.show()
                self._pump(4)
                need = qt.QFontMetrics(block.nameLabel.font).horizontalAdvance(title)
                self.assertGreaterEqual(block.nameLabel.width, need, (width, block.mode, block.nameLabel.width, need))
                self.assertEqual(block.nameLabel.text, title)
                parent.hide()
                parent.deleteLater()
        finally:
            slicer.mrmlScene.RemoveNode(node)

    def test_metrics_tooltip_uses_singular_for_one(self):
        """The Continue card's metrics tooltip says "1 part" and "1 gap" (singular), and plurals otherwise."""
        from StudioLib import draw, library

        original = draw.viewGaps
        try:
            for voxels, parts, gaps, expected in (
                (8, 1, 0, "8 voxels \u00b7 1 part \u00b7 0 gaps"),
                (1, 2, 1, "1 voxel \u00b7 2 parts \u00b7 1 gap"),
                (5, 3, 2, "5 voxels \u00b7 3 parts \u00b7 2 gaps"),
            ):
                draw.viewGaps = lambda metrics, count=gaps: [None] * count
                tip = library.CurrentWorkCard._metricsToolTip({"metrics": {"voxels": voxels, "parts": parts}})
                self.assertEqual(tip, expected)
        finally:
            draw.viewGaps = original
        self.assertEqual(library.CurrentWorkCard._metricsToolTip({"metrics": None}), "")

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

    def test_docs_describe_the_empty_library_that_exists(self):
        """BB-1: the user guide and README describe the empty Library as it is: the drop zone text, the three buttons and the
        two lists of the canvas are the ones on screen, and the old names (Start on MNI template for the Library button, the
        Recent list, "Drop scans, lesion masks or a folder") are gone.
        """
        studioShell = self._applyStudio()
        canvas = self._library(studioShell)
        self.assertFalse(canvas.isStrip())
        zoneTitle = canvas.dropZone.findChild("QLabel", "StudioDropZoneTitle").text
        titles = [str(label.text) for label in canvas.widget.findChildren("QLabel", "StudioLibrarySectionTitle")]
        for expected in ("All projects", "Opened from elsewhere"):
            self.assertIn(expected, titles)

        root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", ".."))
        contents = {}
        for name in ("README.md", "Docs/user_guide/modules/studio.md"):
            path = os.path.join(root, *name.split("/"))
            if not os.path.isfile(path):
                self.skipTest("the documentation is not next to this build")
            with open(path, encoding="utf-8") as stream:
                contents[name] = " ".join(stream.read().split())

        studio = contents["Docs/user_guide/modules/studio.md"]
        paragraph = studio.split("**Empty Library.**", 1)[1].split("- Dropping a **scan**", 1)[0]
        for text in (zoneTitle, str(canvas.openButton.text), str(canvas.openFolderButton.text), str(canvas.templateButton.text),
                     "All projects", "Opened from elsewhere"):
            self.assertIn(text, paragraph)
        readme = contents["README.md"]
        for text in ("*Start on template*", "*Opened from elsewhere*", "*All projects*"):
            self.assertIn(text, readme)
        stale = ("Drop scans, lesion masks or a folder", "**Start on MNI template**", "*Start on MNI template*", "**Recent** list",
                 "*Recent* lists", "from *Recent*")
        for name, content in contents.items():
            for phrase in stale:
                self.assertNotIn(phrase, content, f"{name} still says {phrase!r}")

    def test_docs_describe_the_lesion_card_that_exists(self):
        """BB-1: the user guide and README describe the Lesion card as it is (VISUAL 4.4): three stat cells (Volume, Parts and
        gaps, Side) and "Fill gaps and specks" as an item of the lesion's menu; there is no live line drawn on the card and no
        "Clean up" control, and in scan space the Side cell says "Unknown".
        """
        from StudioLib import lesioncard
        root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", ".."))
        contents = {}
        for name in ("README.md", "Docs/user_guide/modules/studio.md"):
            path = os.path.join(root, *name.split("/"))
            if not os.path.isfile(path):
                self.skipTest("the documentation is not next to this build")
            with open(path, encoding="utf-8") as stream:
                contents[name] = " ".join(stream.read().split())
        # What the card has: the stat cells and the menu item (the cleanup button exists but is hidden by design)
        source = os.path.join(os.path.dirname(os.path.abspath(lesioncard.__file__)), "lesioncard.py")
        with open(source, encoding="utf-8") as stream:
            code = stream.read()
        for text in ('_("Volume")', '_("Side")', '_("Unknown")', '_("Fill gaps and specks")'):
            self.assertIn(text, code)
        self.assertIn("self.cleanupButton.hide()", code)
        studio = contents["Docs/user_guide/modules/studio.md"]
        card = studio.split("1. The **Lesion card**", 1)[1].split("2. **Tools**", 1)[0]
        for text in ("**Volume**", "**Parts**", "**Side**", "Unknown", "**Fill gaps and specks**", "**⋯** menu"):
            self.assertIn(text, card)
        for name, content in contents.items():
            for phrase in ("**Clean up**", "*Clean up*", "a live line", "no side word is shown", "reads out the lesion as you draw"):
                self.assertNotIn(phrase, content, f"{name} still says {phrase!r}")
        readme = contents["README.md"]
        self.assertIn("three stat cells", readme)
        self.assertIn("Fill gaps and specks* (an item of the lesion's ⋯ menu)", readme)

    def test_docs_list_the_subject_list_columns_and_inspector_cards_that_exist(self):
        """BB-1: the user guide names every column of the subject list (the six standard ones and the ones of the Library
        extensions) and says the inspector shows its cards on every study, not "when the study needs them".
        """
        from StudioLib import library
        root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", ".."))
        path = os.path.join(root, "Docs", "user_guide", "modules", "studio.md")
        if not os.path.isfile(path):
            self.skipTest("the documentation is not next to this build")
        with open(path, encoding="utf-8") as stream:
            studio = " ".join(stream.read().split())
        paragraph = studio.split("**With a study open**", 1)[1].split("| Key (in the subject list or grid)", 1)[0]
        headers = [header for _key, header in library.STUDY_COLUMNS]
        for module in library.libcardModules():
            for column in getattr(module, "COLUMNS", None) or ():
                headers.append(column["header"] if isinstance(column, dict) else column[0])
        self.assertGreaterEqual(len(headers), 10)
        for header in headers:
            self.assertIn(header, paragraph, f"the subject list column {header!r} is not in the user guide")
        cards = studio.split("The inspector shows these cards", 1)[1].split("### Draw", 1)[0]
        for card in ("Study", "Columns", "Review", "Sources", "Agreement", "Cohort diagnostics", "Control sets", "Analysis",
                     "Share", "Import", "Subject", "Case", "Batch"):
            self.assertIn(f"**{card}**", cards)
        self.assertNotIn("appear when the study needs them", studio)
        self.assertNotIn("columns ID, Group, Lesion mL, Space, Status and QC", studio)

    def test_button_icons_are_16_px_at_every_pixel_ratio(self):
        """The icons of the Library buttons are 16 logical px (VISUAL 3). Without an icon size a button uses the style's 24 px:
        a 16 px icon that has a pixmap for a device pixel ratio above 1 is then drawn at 24 px (1.5 times larger than at 1x).
        """
        studioShell = self._applyStudio()
        canvas = self._library(studioShell)
        for button in (canvas.openButton, canvas.openFolderButton, canvas.templateButton, canvas.templateMenuButton):
            size = button.iconSize
            self.assertEqual((size.width(), size.height()), (16, 16), button.objectName)
            self.assertFalse(button.icon.isNull(), button.objectName)
            actual = button.icon.actualSize(size)
            self.assertEqual((actual.width(), actual.height()), (16, 16), button.objectName)

    def test_open_button_icon_follows_the_highlighted_text_of_the_palette(self):
        """The icon of the primary Open... button has the ink of its text: HighlightedText of the palette. With a light user accent
        color the text is dark and the icon used to stay white (1.4:1 on yellow) because the "onAccent" tint was hard-coded (BB-1).
        """
        from StudioLib import ui

        def inkOf(icon):
            image = icon.pixmap(16, 16).toImage()
            best = (0, None)
            for y in range(image.height()):
                for x in range(image.width()):
                    color = qt.QColor(image.pixel(x, y))
                    alpha = qt.QColor.fromRgba(image.pixel(x, y)).alpha()
                    if alpha > best[0]:
                        best = (alpha, color.name())
            return best[1]

        studioShell = self._applyStudio()
        canvas = self._library(studioShell)
        original = qt.QPalette(slicer.app.palette())

        def restore():
            slicer.app.setPalette(original)
            ui._iconCache.clear()
            canvas._refreshIcons()

        self.cleanup.append(restore)
        for accent, ink in (("#f5d90a", "#1c1c1f"), ("#2158bd", "#ffffff")):
            palette = qt.QPalette(original)
            for group in (qt.QPalette.Active, qt.QPalette.Inactive, qt.QPalette.Disabled):
                palette.setColor(group, qt.QPalette.Highlight, qt.QColor(accent))
                palette.setColor(group, qt.QPalette.HighlightedText, qt.QColor(ink))
            slicer.app.setPalette(palette)
            canvas._refreshIcons()
            self.assertEqual(inkOf(canvas.openButton.icon), ink, accent)
            self.assertEqual(inkOf(ui.icon("import", 16, "onAccent")), ink, accent)

    def test_open_now_row_icons_are_16_px_at_every_pixel_ratio(self):
        """The eye and the more button of the Open now rows draw their 16 px icons at 16 logical px, as the header buttons do:
        without an icon size they use the style's 24 px and, at a device pixel ratio of 2, draw the icons at 24 px (BB-1).
        """
        studioShell = self._applyStudio()
        self._template(studioShell)
        self._library(studioShell)
        card = self._panel().openNowCard
        checked = []
        for row, eye, _item in card._rows:
            more = row.findChild("QToolButton", "StudioOpenNowMenuButton")
            for button in (eye, more):
                if button is None:
                    continue
                size = button.iconSize
                self.assertEqual((size.width(), size.height()), (16, 16), button.objectName)
                self.assertFalse(button.icon.isNull(), button.objectName)
                actual = button.icon.actualSize(size)
                self.assertEqual((actual.width(), actual.height()), (16, 16), button.objectName)
                checked.append(str(button.objectName))
        self.assertIn("StudioOpenNowEye", checked)
        self.assertIn("StudioOpenNowMenuButton", checked)

    def test_empty_state_text_follows_a_larger_application_font(self):
        """With a large (18 px) application font the drop zone headline and formats line wrap and the zone grows: nothing is cut
        (USABILITY 9.4, BB-1). The default font keeps the 480 x 220 zone.
        """
        from StudioLib import library

        studioShell = self._applyStudio()
        canvas = self._library(studioShell)
        defaultFrameHeight = canvas.dropFrame.height
        base = qt.QFont(qt.QApplication.font())
        large = qt.QFont(base)
        large.setPixelSize(18)
        try:
            qt.QApplication.setFont(large)
            self._wait(300)
            self._pump(8)
            self.assertEqual(canvas.dropZone.width, library.COLUMN_WIDTH)
            self.assertGreaterEqual(canvas.dropZone.height, library.DROP_ZONE_HEIGHT)
            self.assertGreaterEqual(canvas.dropFrame.height, defaultFrameHeight)
            for name in ("StudioDropZoneTitle", "StudioDropZoneHint"):
                label = canvas.dropZone.findChild("QLabel", name)
                self.assertTrue(label.wordWrap, name)
                self.assertLessEqual(label.width, library.COLUMN_WIDTH, name)
                self.assertGreaterEqual(label.height, label.heightForWidth(label.width), f"{name} is cut")
            # The buttons stay inside the zone
            frame = canvas.buttonRow
            bottom = frame.mapTo(canvas.dropZone, qt.QPoint(0, frame.height)).y()
            self.assertLessEqual(bottom, canvas.dropZone.height)
        finally:
            qt.QApplication.setFont(base)
            self._wait(300)
            self._pump(8)
        self.assertEqual((canvas.dropZone.width, canvas.dropZone.height), (library.COLUMN_WIDTH, library.DROP_ZONE_HEIGHT))
        self.assertEqual(canvas.dropFrame.height, defaultFrameHeight)

    def test_classic_label_map_is_a_lesion_in_draw(self):
        """A mask that Classic opened is a label map volume: Draw continues it as a lesion (not a new empty one), and the
        row's "Open in Draw" is enabled.
        """
        from StudioLib import library

        studioShell = self._applyStudio()
        scan = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "sub-02_T1w")
        slicer.util.updateVolumeFromArray(scan, np.zeros((10, 12, 14), np.int16))
        mask = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode", "sub-02_lesion")
        array = np.zeros((10, 12, 14), np.uint8)
        array[3:6, 4:7, 5:8] = 1
        slicer.util.updateVolumeFromArray(mask, array)
        self.assertEqual(library.volumeKind(mask), "labelmap")
        self.assertEqual(library.lesionNodes(), [])
        lesion = library.continueInDraw(studioShell)
        self._pump()
        self.assertIsNotNone(lesion)
        self.assertEqual(lesion.GetName(), "sub-02_lesion")
        self.assertEqual([node.GetName() for node in library.lesionNodes()], ["sub-02_lesion"])
        self.assertEqual(len(slicer.util.getNodesByClass("vtkMRMLLabelMapVolumeNode")), 0)
        self.assertFalse(library._importer().isSegmentationEmpty(lesion))
        self.assertTrue(library._sameNode(library.lesionScan(lesion), scan))
        self.assertEqual(slicer.util.arrayFromSegmentBinaryLabelmap(lesion, lesion.GetSegmentation().GetNthSegmentID(0)).sum(), 3 * 3 * 3)

    def test_classic_label_map_lesion_keeps_its_source_file_for_redrops(self):
        """The lesion made from a Classic label map takes over the label map's source file in the project's records, so that
        dropping that file again finds the lesion (``openNodeForFile()``) instead of loading a second copy.
        """
        import types
        from unittest import mock

        from StudioLib import autosave_io, library

        writer = autosave_io.ProjectWriter.__new__(autosave_io.ProjectWriter)
        writer.project = types.SimpleNamespace(path="/tmp/studio-test-project")
        writer.sources = {}
        self._applyStudio()
        mask = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode", "sub-02_lesion")
        array = np.zeros((10, 12, 14), np.uint8)
        array[3:6, 4:7, 5:8] = 1
        slicer.util.updateVolumeFromArray(mask, array)
        # The project took the file over: the label map's record names the original file
        writer.sources[mask.GetID()] = autosave_io.sourceId("/tmp/outside/sub-02_lesion.nii.gz")
        with mock.patch.object(library, "_projectWriter", return_value=writer):
            lesion = library.labelMapToLesion(mask)
        self.assertIsNotNone(lesion)
        self.assertEqual(writer.nodeIdsForSource("/tmp/outside/sub-02_lesion.nii.gz"), [lesion.GetID()])
        self.assertEqual(list(writer.sources), [lesion.GetID()])

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

    def test_continue_card_follows_the_project_that_attaches_later(self):
        """Closing a study re-attaches the open project after the Library drew the Continue card without it: the card said
        "Close… (unsaved)" and "Not saved yet" until a workspace switch (BB-1). The card is drawn again when the Projects
        section lists the projects again.
        """
        from StudioLib import library, projects

        studioShell = self._applyStudio()
        self._template(studioShell, fill=True)
        canvas = self._library(studioShell)
        self.assertEqual(canvas.currentWork.closeButton.text, "Close… (unsaved)")
        original = library._projectService
        self.cleanup.append(lambda: setattr(library, "_projectService", original))

        def attached():
            return object()  # a project is attached now, the card has not heard of it

        library._projectService = attached
        self.assertEqual(canvas.currentWork.closeButton.text, "Close… (unsaved)")
        projects.notifyChanged()
        deadline = time.time() + 10
        while canvas.currentWork.closeButton.text != "Close" and time.time() < deadline:
            self._wait(500)
        self.assertEqual(canvas.currentWork.closeButton.text, "Close")

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

    def test_lesion_row_narrow_keeps_name_readable(self):
        """In a narrow Continue card (220 px for the row, as at 1366 x 768) the name keeps room for its start and end
        ("sub-PC…lesion", not "s…n"), and the metrics and the state chip stay readable (second line below the name).
        """
        studioShell = self._applyStudio()
        _volumeNode, segmentationNode = self._template(studioShell, fill=True)
        name = "sub-PCS0042_ses-followup12months_lesion"
        segmentationNode.SetName(name)
        canvas = self._library(studioShell)
        card = canvas.currentWork
        block = next(iter(card._blocks.values()))
        block.widget.setFixedWidth(220)
        self._pump(6)
        card.refresh()
        self._pump(4)
        self.assertEqual(str(block.nameLabel.property("fullText")), name)
        self.assertGreaterEqual(block.nameLabel.width, 100, block.nameLabel.text)
        shown = block.nameLabel.text.replace("\u2026", "")
        self.assertGreaterEqual(len(shown), 10, block.nameLabel.text)
        self.assertTrue(shown.startswith("sub-PC") and shown.endswith("lesion"), block.nameLabel.text)
        self.assertEqual(block.stateLabel.text, "Not saved yet")
        self.assertIn("Not saved yet", block.stateLabel.toolTip)
        # A wide row shows the whole chip text again
        block.widget.setFixedWidth(700)
        self._pump(6)
        self.assertEqual(block.stateLabel.text, "Not saved yet")

    def test_lesion_row_narrow_keeps_metrics_and_state_readable(self):
        """At 1366 x 768 the text column of the Continue card is about 214 px wide: the lesion's volume (the card's key
        number) and the save state are shown whole, on a second line, and only the long name is elided (the "…" and "Sav…"
        bug); a wide row has everything on one line.
        """
        studioShell = self._applyStudio()
        _volumeNode, segmentationNode = self._template(studioShell, fill=True)
        segmentationNode.SetName("sub-PCS0042_ses-baseline_acq-highres3DMPRAGE_T1w_desc-manualtrace_lesion")
        canvas = self._library(studioShell)
        card = canvas.currentWork
        block = next(iter(card._blocks.values()))
        self.assertTrue(str(block.metricsLabel.property("fullText")))
        for width in (214, 260):
            block.widget.setFixedWidth(width)
            self._pump(6)
            card.refresh()
            self._pump(4)
            metrics = str(block.metricsLabel.property("fullText"))
            self.assertEqual(block.metricsLabel.text, metrics, (width, block.metricsLabel.width))
            self.assertEqual(block.stateLabel.text, "Not saved yet", (width, block.stateLabel.width))
            self.assertTrue(block.compact, width)
            shown = block.nameLabel.text.replace("\u2026", "")
            self.assertTrue(shown.startswith("sub-PC") and shown.endswith("lesion"), block.nameLabel.text)
            # Nothing is wider than the row
            for label in (block.nameLabel, block.metricsLabel, block.stateLabel):
                self.assertLessEqual(label.mapTo(block.widget, qt.QPoint(label.width, 0)).x(), width, label.objectName)
        block.widget.setFixedWidth(900)
        self._pump(6)
        self.assertFalse(block.compact)
        self.assertEqual(block.metricsLabel.text, str(block.metricsLabel.property("fullText")))
        self.assertEqual(block.metricsLabel.mapTo(block.widget, qt.QPoint(0, 0)).y(), block.nameLabel.mapTo(block.widget, qt.QPoint(0, 0)).y())

    def test_lesion_row_narrow_is_left_aligned(self):
        """In a compact or stacked row the swatch and the name start at the row's left edge (not centred in the spare width
        of the name's line, which made the rows of the 1366 x 768 Continue card ragged).
        """
        studioShell = self._applyStudio()
        _volumeNode, segmentationNode = self._template(studioShell, fill=True)
        segmentationNode.SetName("L1")
        canvas = self._library(studioShell)
        card = canvas.currentWork
        block = next(iter(card._blocks.values()))
        checked = False
        for width in (214, 260, 330, 400):
            block.widget.setFixedWidth(width)
            self._pump(6)
            card.refresh()
            self._pump(4)
            if not block.compact:
                continue
            swatchX = block.swatch.mapTo(block.widget, qt.QPoint(0, 0)).x()
            nameX = block.nameLabel.mapTo(block.widget, qt.QPoint(0, 0)).x()
            self.assertEqual(swatchX, block.indent, (width, block.mode))
            self.assertEqual(nameX, block.indent + block.SWATCH + block.SPACING, (width, block.mode))
            checked = True
        self.assertTrue(checked)

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

    def test_failed_load_not_in_opened_from_elsewhere(self):
        """The IO manager lists a file in RecentlyLoadedFiles even when it could not be loaded: Opened from elsewhere does not."""
        from StudioLib import importer, library

        broken = os.path.join(self.directory, "broken_T1w.nhdr")
        with open(os.path.join(self.directory, "broken_T1w.raw"), "wb") as f:
            f.write(bytes(100))
        with open(broken, "w") as f:
            f.write(
                "NRRD0004\ntype: short\ndimension: 3\nspace: left-posterior-superior\nsizes: 64 64 64\n"
                "space directions: (1,0,0) (0,1,0) (0,0,1)\nkinds: domain domain domain\nendian: little\nencoding: raw\n"
                "space origin: (0,0,0)\ndata file: broken_T1w.raw\n",
            )
        names = lambda: [os.path.basename(entry.path) for entry in library.recentEntries(limit=50)]
        shown = []
        originals = (slicer.util.errorDisplay, slicer.util.warningDisplay)
        slicer.util.errorDisplay = slicer.util.warningDisplay = lambda text, **kwargs: shown.append(text)
        self.cleanup.append(lambda: setattr(slicer.util, "errorDisplay", originals[0]))
        self.cleanup.append(lambda: setattr(slicer.util, "warningDisplay", originals[1]))
        try:
            result = importer.handleDrop([broken])
        finally:
            slicer.util.errorDisplay, slicer.util.warningDisplay = originals
        self._pump(6)
        self.assertTrue(shown and result.errors, "the file must be refused")
        self.assertTrue(importer.isFailedLoad(broken))
        self.assertNotIn("broken_T1w.nhdr", names())
        # Nothing of it stays in the stored list (shared with official installations)
        settings = qt.QSettings()
        stored = []
        for index in range(settings.beginReadArray("RecentlyLoadedFiles/RecentFiles")):
            settings.setArrayIndex(index)
            stored.append(os.path.basename(str(dict(settings.value("file") or {}).get("fileName", ""))))
        settings.endArray()
        self.assertNotIn("broken_T1w.nhdr", stored)
        # A later successful load of the same path lists it again
        importer._noteLoadResult(broken, True)
        self.assertFalse(importer.isFailedLoad(broken))

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

    def test_recent_rows_follow_the_studio_folder(self):
        """After Settings > Saving > Change... > "Keep them where they are" the projects of the old folder are files from
        elsewhere: the Library rows read again without a new recent file (A-09 step 8).
        """
        from StudioLib import library

        oldRoot = os.path.join(self.directory, "A", "Slicer Studio")
        newRoot = os.path.join(self.directory, "B", "Slicer Studio")
        project = os.path.join(oldRoot, "Projects", "MRHead", "MRHead.mrml")
        os.makedirs(os.path.dirname(project))
        os.makedirs(newRoot)
        with open(project, "w", encoding="utf-8") as f:
            f.write("x")
        menu = qt.QMenu()
        action = menu.addAction("MRHead.mrml")
        action.setProperty("fileParameters", {"fileName": project, "fileType": "SceneFile"})
        roots = [os.path.realpath(oldRoot)]
        originalMenu, originalRoot = library.recentlyLoadedMenu, library._rootFolder
        library.recentlyLoadedMenu = lambda: menu
        library._rootFolder = lambda: roots[0]
        self.cleanup.append(lambda: setattr(library, "recentlyLoadedMenu", originalMenu))
        self.cleanup.append(lambda: setattr(library, "_rootFolder", originalRoot))
        recent = library.RecentList(includeStudies=False, outsideRootOnly=True)
        self.cleanup.append(recent.dispose)
        recent.refresh(force=False)
        # Inside the Slicer Studio folder: listed in Projects, not here
        self.assertEqual([e.path for e in recent.entries if e.kind == "file"], [])
        # Change... > Keep: the folder is another one, the menu and the recorded times did not change
        roots[0] = os.path.realpath(newRoot)
        recent.refresh(force=False)
        self.assertEqual([e.path for e in recent.entries if e.kind == "file"], [project])

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

    def test_folder_card_group_mapping_offers_every_value(self):
        """A group column with more than 8 distinct values: every value gets a combo (none silently becomes group n/a)."""
        from StudioLib import importer, library

        studioShell = self._applyStudio()
        folder = self._writeTinyFiles(10, "manygroups")
        values = ["patient", "HC", "stroke", "control", "TIA", "MS", "tumour", "healthy", "ICH", "SAH"]
        with open(os.path.join(folder, "participants.tsv"), "w", encoding="utf-8") as f:
            f.write("participant_id\tgroup\n" + "".join("sub-%02d\t%s\n" % (i + 1, value) for i, value in enumerate(values)))
        importer.loadFolder(folder, studioShell)
        self._pump(6)
        card = library.canvas().folderCard()
        self.assertIsNotNone(card)
        if card.createStudyButton is None:
            return
        self.assertEqual(sorted(card.groupCombos), sorted(values))
        self.assertEqual(sorted(card.groupMapping()), sorted(values))
        # The rows scroll inside the card; the last value's combo is one of the card's widgets
        self.assertIsNotNone(card.widget.findChild("QScrollArea", "StudioFolderCardGroupScroll"))
        card.close()
        self._pump(4)

    def test_folder_card_excel_tables(self):
        """A participants.tsv saved by Excel: Windows-1252 and UTF-16 say "save as UTF-8" (no raw codec error, no garbled
        card line, no ERROR log); trailing empty columns are ignored like extra empty fields in a row.
        """
        import logging

        from StudioLib import importer, library

        studioShell = self._applyStudio()
        records = []

        class Recorder(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = Recorder(level=logging.DEBUG)
        logging.getLogger().addHandler(handler)
        try:
            for case, (encoding, tail) in {"cp1252": ("cp1252", ""), "utf16": ("utf-16", ""), "trailing": ("utf-8", "\t\t")}.items():
                folder = self._writeTinyFiles(4, "excel-" + case)
                lines = ["participant_id\tgroup\tnotes" + tail] + ["sub-%02d\t%s\tcafé Müller%s" % (i + 1, "patient" if i < 2 else "HC", tail)
                                                                 for i in range(4)]
                with open(os.path.join(folder, "participants.tsv"), "w", encoding=encoding, newline="") as f:
                    f.write("\r\n".join(lines) + "\r\n")
                del records[:]
                importer.loadFolder(folder, studioShell)
                self._pump(6)
                card = library.canvas().folderCard()
                self.assertIsNotNone(card, case)
                if card.createStudyButton is None:
                    card.close()
                    continue
                if case == "trailing":
                    self.assertIn("participants.tsv · 4 rows · group, notes", card.participantsLabel.text)
                    self.assertIsNotNone(card.createStudy())
                    library._studyUi().closeStudy()
                else:
                    self.assertIsNone(card.participantsLabel)  # no garbled columns
                    problem = card.widget.findChild("QLabel", "StudioFolderCardTableProblem")
                    self.assertIsNotNone(problem, case)
                    self.assertIn("UTF-8", problem.text)
                    self.assertNotIn("codec", problem.text)
                    self.assertIsNone(card.createStudy())  # said in a message box, no study
                    self.assertFalse(os.path.exists(os.path.join(folder, "study.json")), case)
                self.assertEqual([r.getMessage()[:200] for r in records if r.levelno >= logging.ERROR], [], case)
                card = library.canvas().folderCard()
                if card is not None:
                    card.close()
                self._pump(4)
        finally:
            logging.getLogger().removeHandler(handler)

    def test_folder_card_participants_csv_is_imported_by_create_study(self):
        """A participants.csv next to the files: the card shows it and its group mapping, and Create study imports it
        (BB-1: only a participants.tsv was adopted, so the CSV's columns and the mapping were dropped silently).
        """
        import csv

        from StudioLib import importer, library

        studioShell = self._applyStudio()
        folder = self._writeTinyFiles(3, "csvtable")
        with open(os.path.join(folder, "participants.csv"), "w", encoding="utf-8") as f:
            f.write("participant_id,age,aphasia_score,group\nsub-01,54.5,1.25,Patient\nsub-02,61.75,0.125,Kontrolle\nsub-03,70,2,Patient\n")
        importer.loadFolder(folder, studioShell)
        self._pump(6)
        card = library.canvas().folderCard()
        self.assertIsNotNone(card)
        if card.createStudyButton is None:
            card.close()
            return
        self.assertIn("participants.csv · 3 rows · age, aphasia_score, group", card.participantsLabel.text)
        self.assertEqual(card.groupColumn, "group")
        for value, group in (("Patient", "case"), ("Kontrolle", "control")):
            combo = card.groupCombos[value]
            combo.setCurrentIndex(combo.findData(group))
        self.assertEqual(card.groupMapping(), {"Patient": "case", "Kontrolle": "control"})
        card.createStudyButton.click()
        self._pump(6)
        self.cleanup.append(lambda: library._studyUi().closeStudy())
        self.assertIsNone(library.canvas().folderCard())
        with open(os.path.join(folder, "participants.tsv"), encoding="utf-8") as f:
            rows = {row["participant_id"]: row for row in csv.DictReader(f, delimiter="\t")}
        self.assertEqual(sorted(rows), ["sub-01", "sub-02", "sub-03"])
        self.assertEqual({sid: row.get("age") for sid, row in rows.items()}, {"sub-01": "54.5", "sub-02": "61.75", "sub-03": "70"})
        self.assertEqual({sid: row.get("aphasia_score") for sid, row in rows.items()}, {"sub-01": "1.25", "sub-02": "0.125", "sub-03": "2"})
        self.assertEqual({sid: row["group"] for sid, row in rows.items()}, {"sub-01": "case", "sub-02": "control", "sub-03": "case"})
        # The user's table is read, never rewritten
        with open(os.path.join(folder, "participants.csv"), encoding="utf-8") as f:
            self.assertTrue(f.read().startswith("participant_id,age,aphasia_score,group\nsub-01,54.5,1.25,Patient\n"))

    def test_participants_csv_is_imported_by_every_create_study_route(self):
        """A participants.csv in the study folder supplies the groups and the scores on the routes without the folder card:
        the "Create study" of the opened (dropped) participants.csv, New study… and Create study of a loaded set. Each
        showed no group step and made a study with group n/a and without the CSV's columns (BB-1).
        """
        import csv

        from StudioLib import importer, study_ui

        studioShell = self._applyStudio()
        if not study_ui.isAvailable():
            self.skipTest("needs the lesion network mapping library")
        toasts = []
        studioShell.toast = lambda text, *args, **kwargs: toasts.append((str(text), kwargs))
        shown = []

        def fakeExec(dialog):
            # The group-mapping step: record its title, accept the proposed mapping
            shown.append([str(label.text) for label in dialog.findChildren("QLabel") if label.objectName == "StudioGroupMappingTitle"])
            return True

        originalExec = study_ui._execDialog
        study_ui._execDialog = fakeExec
        self.cleanup.append(lambda: setattr(study_ui, "_execDialog", originalExec))
        self.cleanup.append(study_ui.closeStudy)

        def writeCsv(folder):
            with open(os.path.join(folder, "participants.csv"), "w", encoding="utf-8") as f:
                f.write("participant_id,group,nihss,age\nsub-01,Patient,12,54.5\nsub-02,Patient,7,61\nsub-03,Control,3,70\n")

        def check(folder, route):
            self.assertIsNotNone(study_ui.session(), route)
            with open(os.path.join(folder, "participants.tsv"), encoding="utf-8") as f:
                rows = {row["participant_id"]: row for row in csv.DictReader(f, delimiter="\t")}
            self.assertEqual({sid: row["group"] for sid, row in rows.items()}, {"sub-01": "case", "sub-02": "case", "sub-03": "control"}, route)
            self.assertEqual({sid: row.get("nihss") for sid, row in rows.items()}, {"sub-01": "12", "sub-02": "7", "sub-03": "3"}, route)
            self.assertEqual({sid: row.get("age") for sid, row in rows.items()}, {"sub-01": "54.5", "sub-02": "61", "sub-03": "70"}, route)
            self.assertEqual(shown[-1], ["participants.csv has groups in column 'group'. Choose the study group of each value:"], route)
            self.assertTrue(any("participants.csv imported" in text for text, _kwargs in toasts), (route, toasts))
            # The user's table is read, never rewritten
            with open(os.path.join(folder, "participants.csv"), encoding="utf-8") as f:
                self.assertTrue(f.read().startswith("participant_id,group,nihss,age\nsub-01,Patient,12,54.5\n"), route)
            study_ui.closeStudy()
            self._pump(4)
            del toasts[:]

        # 1. The participants.csv is dropped with no study open: its toast offers Create study
        folder = self._writeTinyFiles(3, "dropped")
        writeCsv(folder)
        study_ui.openParticipantsTable(os.path.join(folder, "participants.csv"), studioShell)
        offers = [kwargs["action"] for _text, kwargs in toasts if kwargs.get("actionText") == "Create study"]
        self.assertEqual(len(offers), 1, toasts)
        offers[0]()
        self._pump(4)
        check(folder, "Create study of the dropped participants.csv")

        # 2. New study… on a folder with a participants.csv
        folder = self._writeTinyFiles(3, "newstudy")
        writeCsv(folder)
        realQt = study_ui.qt

        class FileDialog:
            @staticmethod
            def getExistingDirectory(*args):
                return folder

        class QtProxy:
            def __getattr__(self, name):
                return FileDialog if name == "QFileDialog" else getattr(realQt, name)

        study_ui.qt = QtProxy()
        try:
            study_ui.newStudyDialog(studioShell)
        finally:
            study_ui.qt = realQt
        self._pump(4)
        check(folder, "New study…")

        # 3. Create study of a loaded set of lesion masks with a participants.csv next to them
        paths = self._writeCohort(3, folder="loadedset", participants=False)
        folder = os.path.dirname(paths[0])
        writeCsv(folder)
        importer.loadFiles(paths, studioShell)
        self._pump(10)
        card = self._library(studioShell).loadedSet
        self.assertIsNotNone(card.createStudyButton)
        card.createStudyButton.click()
        self._pump(6)
        check(folder, "Create study of a loaded set")

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

    def test_close_lesion_in_project_does_not_ask_about_unsaved_changes(self):
        """In a project the lesion is stored with the project: closing it never says it has changes that are not saved."""
        from StudioLib import library

        node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "sub-81_lesion")
        node.GetSegmentation().AddEmptySegment("lesion")
        dims = GRID_NLIN6_2MM[0]
        grid = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "grid")
        slicer.util.updateVolumeFromArray(grid, np.zeros((dims[2], dims[1], dims[0]), np.int16))
        grid.SetIJKToRASMatrix(self._ijkToRas(GRID_NLIN6_2MM))
        array = np.zeros((dims[2], dims[1], dims[0]), np.uint8)
        array[1, 1, 1] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, node, node.GetSegmentation().GetNthSegmentID(0), grid)
        slicer.mrmlScene.RemoveNode(grid)
        asked = []
        originalConfirm, originalService = library._confirmClose, library._projectService
        library._confirmClose = lambda text, informative="": asked.append(text) or False
        self.cleanup.append(lambda: setattr(library, "_confirmClose", originalConfirm))
        self.cleanup.append(lambda: setattr(library, "_projectService", originalService))
        # Outside a project the voxels are unsaved
        library._projectService = lambda: None
        self.assertTrue(library.isLesionUnsaved(node))
        # In a project they are not, and closing does not ask
        library._projectService = object  # any object: a project is open
        self.assertFalse(library.isLesionUnsaved(node))
        self.assertTrue(library.closeNode(node))
        self.assertEqual(asked, [])

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

    def test_lesion_metrics_are_elided_when_cut(self):
        """The metrics of the Continue card ("Left · 4.22 mL · 1 part") end with an ellipsis when the row is too narrow for
        them, instead of being cut off mid-word at the state chip; the whole line is the tool tip and ``plainText``.
        """
        studioShell = self._applyStudio()
        self._template(studioShell, fill=True, rasCenter=(-40.0, -20.0, 10.0))
        canvas = self._library(studioShell)
        label = self._labels(canvas, "StudioCurrentWorkLesionMetrics")[0]
        whole = self._plain(label)
        self.assertTrue(whole.endswith("1 part"), whole)
        label.setFixedWidth(400)
        self._pump(6)
        self.assertEqual(str(label.text), whole)
        label.setFixedWidth(90)
        self._pump(6)
        shown = str(label.text)
        self.assertTrue(shown.endswith("…"), f"the cut metrics have no ellipsis: {shown!r}")
        self.assertLessEqual(qt.QFontMetrics(label.font).horizontalAdvance(shown), label.width)
        self.assertEqual(self._plain(label), whole)
        self.assertIn(whole, str(label.toolTip))
        # The row refresh keeps the elision
        canvas.refreshData()
        self._pump(4)
        label = self._labels(canvas, "StudioCurrentWorkLesionMetrics")[0]
        self.assertEqual(self._plain(label), whole)

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

    def test_lesion_rows_tell_files_apart(self):
        """Every imported mask's segment is named "Lesion": the Continue card names each lesion row by its segmentation node."""
        from StudioLib import library

        studioShell = self._applyStudio()
        volumeNode, _firstLesion = self._template(studioShell)
        names = ["patient_T1_lesion", "patient_T1_lesion-rater2"]
        for name in names:
            node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", name)
            node.CreateDefaultDisplayNodes()
            node.SetReferenceImageGeometryParameterFromVolumeNode(volumeNode)
            node.GetSegmentation().AddEmptySegment("", "Lesion")
            self.assertEqual(library.lesionTitle(node), name)
        self._pump(4)
        canvas = self._library(studioShell)
        shown = [label.property("fullText") for label in self._labels(canvas, "StudioCurrentWorkLesionName")]
        self.assertEqual(len(shown), 3, shown)
        self.assertEqual(len(set(shown)), 3, shown)
        for name in names:
            self.assertTrue(any(text.startswith(name) for text in shown), (name, shown))

    def test_narrow_window_reflows_the_page(self):
        """USABILITY 9.4: at QT_SCALE_FACTOR=2 on 1080p (a window of 960x540 logical px, a viewport of about 600 px) no control
        of the Library page is cut off: the header buttons go under the title, the actions of the Continue card under its
        content, a long folder path is elided; a wide window brings the side-by-side layout back.
        """
        studioShell = self._applyStudio()
        self._template(studioShell)
        oldSize = self.mainWindow.size
        self.mainWindow.showNormal()
        self.cleanup.append(lambda: self.mainWindow.resize(oldSize))
        self.mainWindow.resize(960, 540)
        self._wait(300)
        canvas = self._library(studioShell)
        self._wait(200)
        scroll = canvas.scrollArea
        viewport = scroll.viewport()
        self.assertLess(viewport.width, 700, "the window did not get narrow")
        right = viewport.mapToGlobal(qt.QPoint(viewport.width, 0)).x()
        cut = []
        for button in canvas.widget.findChildren("QAbstractButton"):
            if not button.isVisible() or not button.isWidgetType():
                continue
            buttonRight = button.mapToGlobal(qt.QPoint(button.width, 0)).x()
            if buttonRight > right + 1:
                cut.append(button.objectName or button.text)
        self.assertEqual(cut, [], "controls past the right edge of the Library page")
        self.assertTrue(canvas.isNarrow())
        self.assertGreaterEqual(canvas.currentWork.stage(), 1)
        self.assertTrue(canvas.currentWork.continueButton.isVisible())
        self.assertLessEqual(canvas.currentWork.continueButton.mapToGlobal(qt.QPoint(canvas.currentWork.continueButton.width, 0)).x(),
                             right + 1)
        self.assertEqual(scroll.horizontalScrollBar().maximum, 0, "the page needs a horizontal scroll bar")
        # The folder path is elided to the room of the title column; the whole path is its tool tip
        self.assertLessEqual(canvas.captionLabel.width, canvas.header.width)

        self.mainWindow.resize(1920, 1000)
        self._wait(300)
        self.assertFalse(canvas.isNarrow())
        self.assertEqual(canvas.currentWork.stage(), 0)
        self.assertTrue(canvas.currentWork.continueButton.isVisible())
        # Side by side again: the action column is right of the content
        self.assertGreater(canvas.currentWork.continueButton.mapTo(canvas.currentWork.widget, qt.QPoint(0, 0)).x(),
                           canvas.currentWork.widget.width // 2)

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

    def test_open_now_closed_surface_segmentation(self):
        """A segmentation whose source is a closed surface (official 3D Slicer saves one when a model is imported) is not
        empty, gets an Open now row with an enabled NIfTI export, and its summary gives the enclosed volume.
        """
        import importlib

        from StudioLib import draw, library, niftiexport

        nc = importlib.import_module("LesionNetworkMappingUI.normalize_controller")
        studioShell = self._applyStudio()
        sphere = vtk.vtkSphereSource()
        sphere.SetRadius(15.0)
        sphere.SetCenter(10.0, 0.0, 0.0)
        sphere.SetThetaResolution(24)
        sphere.SetPhiResolution(24)
        sphere.Update()
        model = slicer.modules.models.logic().AddModel(sphere.GetOutput())
        segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "SurfaceSeg")
        segmentationNode.CreateDefaultDisplayNodes()
        closedSurface = slicer.vtkSegmentationConverter.GetSegmentationClosedSurfaceRepresentationName()
        segmentationNode.GetSegmentation().SetSourceRepresentationName(closedSurface)
        self.assertTrue(slicer.modules.segmentations.logic().ImportModelToSegmentationNode(model, segmentationNode))
        slicer.mrmlScene.RemoveNode(model)
        segmentId = segmentationNode.GetSegmentation().GetNthSegmentID(0)
        self.assertIsNone(segmentationNode.GetBinaryLabelmapInternalRepresentation(segmentId))
        self.assertFalse(draw.isLesionEmpty(segmentationNode))
        self.assertEqual(niftiexport.unavailableReason(segmentationNode), "")
        self.assertFalse(nc.lesionIsEmpty(segmentationNode, segmentId))
        summary = library.lesionSummary(segmentationNode, compute=True)
        self.assertTrue(summary["text"].endswith("mL · 1 part"), summary["text"])
        volume = summary["metrics"]["mL"]
        self.assertAlmostEqual(volume, 4.0 / 3.0 * np.pi * 15.0 ** 3 / 1000.0, delta=0.5)
        self._library(studioShell)
        self.assertIn("SurfaceSeg", self._panel().openNowCard.rowNames())
        # An empty closed surface segment is empty
        emptyId = segmentationNode.GetSegmentation().AddEmptySegment("", "Nothing")
        self.assertTrue(draw.isLesionEmpty(segmentationNode, [emptyId]))
        self.assertTrue(nc.lesionIsEmpty(segmentationNode, emptyId))

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

    def _write4D(self, name, timePoints=3):
        """Write a 4D NIfTI image (16 x 12 x 8 voxels, ``timePoints`` time points) and return its path."""
        import SimpleITK as sitk

        rng = np.random.default_rng(4)
        frames = []
        for _index in range(timePoints):
            frame = sitk.GetImageFromArray((rng.random((8, 12, 16)) * 100).astype(np.int16))
            frame.SetSpacing((2.0, 2.0, 3.0))
            frames.append(frame)
        image = sitk.JoinSeries(frames)
        image.SetSpacing((2.0, 2.0, 3.0, 2.0))
        path = os.path.join(self.directory, name)
        sitk.WriteImage(image, path)
        return path

    def test_close_4d_image_row(self):
        """Close on a 4D image row (a sequence) removes its proxy volume, sequence and browser: the row does not come back."""
        from StudioLib import importer

        studioShell = self._applyStudio()
        proxyNode = importer.loadSequence(self._write4D("bold_4d.nii.gz"))
        self._library(studioShell)
        card = self._panel().openNowCard
        card.refresh()
        self.assertIn("bold_4d", card.rowNames())
        row = next(row for row in card._rows if row[2]["name"] == "bold_4d")
        actions = {str(action.objectName): action for action in row[0].findChild("QMenu", "StudioOpenNowMenu").actions()}
        actions["StudioOpenNowClose"].trigger()
        self._pump(6)
        self._wait(300)
        card.refresh()
        self.assertNotIn("bold_4d", card.rowNames())
        self.assertFalse(slicer.mrmlScene.IsNodePresent(proxyNode))
        self.assertEqual(slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"), [])
        self.assertEqual(slicer.util.getNodesByClass("vtkMRMLSequenceNode"), [])
        self.assertEqual(slicer.util.getNodesByClass("vtkMRMLSequenceBrowserNode"), [])
        self.assertEqual(slicer.util.getNodesByClass("vtkMRMLSequenceStorageNode"), [])

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

    def test_inspector_shows_project_card_after_classic_round_trip(self):
        """Back from Classic the Library inspector shows the open project's card again, not the Classic cards."""
        from StudioLib import library, projects

        studioShell = self._applyStudio()
        self._template(studioShell, fill=True)
        project = projects.createProject("roundtrip", rootPath=self.directory)
        previous = projects.currentProject()
        projects.setCurrentProject(project)
        self.cleanup.append(lambda: projects.setCurrentProject(previous))
        self._library(studioShell)
        panel = self._panel()
        self.assertTrue(panel.overviewCard.widget.isVisible())
        # Classic: the panel shows the Open buttons and Recent and hides the project card (the panel stays shown)
        original = library._shell
        library._shell = lambda: None
        try:
            panel.refresh()
            self.assertTrue(panel.overviewCard.widget.isHidden())
        finally:
            library._shell = original
        # Back in Studio: entering Library refreshes the panel, no Show event comes
        library.enter(studioShell)
        self._pump(4)
        self.assertTrue(panel.overviewCard.widget.isVisible())
        self.assertTrue(panel.openCard.isHidden())

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

    def test_study_card_template_line_without_a_template(self):
        """A study made headlessly has "template": null: the STUDY card says 'not set', not 'Tracing template: —  mm'."""
        from StudioLib import library

        self.assertEqual(library.studyTemplateText(None), "Tracing template: not set")
        self.assertEqual(library.studyTemplateText({}), "Tracing template: not set")
        self.assertEqual(library.studyTemplateText({"id": "MNI152NLin6Asym"}), "Tracing template: MNI152NLin6Asym")
        self.assertEqual(library.studyTemplateText({"id": "MNI152NLin6Asym", "res": 1}), "Tracing template: MNI152NLin6Asym 1 mm")
        self.assertEqual(library.studyTemplateText({"id": "MNI152NLin6Asym", "res": "2"}), "Tracing template: MNI152NLin6Asym 2 mm")

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

    def test_caption_folder_no_leading_separator(self):
        """The caption "Saved automatically in ..." never starts with a separator, inside or outside the home folder."""
        from StudioLib import library, projects

        sep = " \u203a "
        self.assertEqual(library._captionFolder("/tmp/claude-0/Root"), "tmp" + sep + "claude-0" + sep + "Root")
        self.assertEqual(library._captionFolder("/Volumes/Data/Slicer Studio/"), "Volumes" + sep + "Data" + sep + "Slicer Studio")
        self.assertEqual(library._captionFolder("~/Documents/Slicer Studio"), "Documents" + sep + "Slicer Studio")
        self.assertEqual(library._captionFolder("~"), "~")
        # On the page: a projects folder outside the home folder
        saved = os.environ.get("SLICER_STUDIO_PROJECTS_ROOT")

        def restore():
            if saved is None:
                os.environ.pop("SLICER_STUDIO_PROJECTS_ROOT", None)
            else:
                os.environ["SLICER_STUDIO_PROJECTS_ROOT"] = saved

        self.cleanup.append(restore)
        root = os.path.join(self.directory, "Projects Outside")
        os.makedirs(root, exist_ok=True)
        os.environ["SLICER_STUDIO_PROJECTS_ROOT"] = root
        self.assertTrue(projects.isEnabled())
        studioShell = self._applyStudio()
        canvas = self._library(studioShell)
        canvas._refreshCaption()
        text = canvas._captionText
        self.assertTrue(text.startswith("Saved automatically in "), text)
        self.assertNotIn("  ", text)
        self.assertFalse(text.startswith("Saved automatically in " + sep.strip()), text)
        self.assertFalse(text.startswith("Saved automatically in " + sep), text)
        self.assertIn(root, str(canvas.captionLabel.toolTip))

    def test_caption_follows_a_changed_projects_folder(self):
        """The caption under "Projects" names the new folder as soon as the folder is chosen or changed, without leaving Library."""
        from StudioLib import projects

        saved = os.environ.get("SLICER_STUDIO_PROJECTS_ROOT")

        def restore():
            if saved is None:
                os.environ.pop("SLICER_STUDIO_PROJECTS_ROOT", None)
            else:
                os.environ["SLICER_STUDIO_PROJECTS_ROOT"] = saved
            projects.notifyChanged()

        self.cleanup.append(restore)
        first = os.path.join(self.directory, "CaptionFirst", "Slicer Studio")
        second = os.path.join(self.directory, "CaptionSecond", "Slicer Studio")
        os.makedirs(first, exist_ok=True)
        os.makedirs(second, exist_ok=True)
        os.environ["SLICER_STUDIO_PROJECTS_ROOT"] = first
        studioShell = self._applyStudio()
        canvas = self._library(studioShell)
        self.assertIn("CaptionFirst", canvas._captionText)
        self.assertIn(first, str(canvas.captionLabel.toolTip))
        # The folder changes while Library stays open (the first-start chooser, Settings > Saving > Change...)
        os.environ["SLICER_STUDIO_PROJECTS_ROOT"] = second
        projects.notifyChanged()
        self._wait(900)
        self._pump(4)
        self.assertIn("CaptionSecond", canvas._captionText)
        self.assertNotIn("CaptionFirst", canvas._captionText)
        self.assertIn(second, str(canvas.captionLabel.toolTip))

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

    def test_inspector_labels_are_sentence_case(self):
        """The Sources card title and the headers of the tutorials dialog are sentence case, 12 px (VISUAL 2.4): all-caps is only for
        "CONTINUE" and the group headers of Search and the folder chooser.
        """
        from StudioLib import libcard_sources, libcard_tutorials

        card = libcard_sources.SourcesCard(None)
        title = card.widget.findChild("QLabel", "StudioSourcesLibraryTitle")
        header = libcard_tutorials.TutorialsDialog._header("What you will learn")
        for label, text in ((title, "Sources"), (header, "What you will learn")):
            self.assertEqual(label.text, text)
            self.assertNotEqual(label.font.capitalization(), qt.QFont.AllUppercase, text)
            self.assertEqual(label.font.letterSpacing(), 0.0, text)

    def test_sources_unreadable_records_tsv_is_never_overwritten(self):
        """A records.tsv that cannot be read (not UTF-8) stays as it is: Import refuses, the error stays shown, the stage log is kept (BB-1)."""
        from LesionNetworkMappingLib import sources
        from LesionNetworkMappingLib.study import Study
        from StudioLib import libcard_sources, study_ui

        path = os.path.join(self.directory, "Lit")
        Study.create(path, name="Lit").save()
        session = study_ui.openStudy(path, None)
        self.cleanup.append(study_ui.closeStudy)
        toasts = []
        original = libcard_sources._toast
        libcard_sources._toast = lambda *args, **kwargs: toasts.append(args)
        self.cleanup.append(lambda: setattr(libcard_sources, "_toast", original))
        card = libcard_sources.SourcesCard(session)
        self.cleanup.append(card.widget.close)
        nbib = "\n\n".join(f"PMID- {pmid}\nTI  - Caf\u00e9 study {t}\nAU  - M\u00fcller {t}\nDP  - 2020\nTA  - Brain" for pmid, t in (("111111", "A"), ("222222", "B")))
        self.assertIsNotNone(card.importRecords(text=nbib, database="PubMed", query="q"))
        card.table.selectRow(0)
        card.advanceSelected()
        recordsPath = os.path.join(path, "sources", "records.tsv")
        logPath = os.path.join(path, "sources", "stage_log.tsv")
        self.assertTrue(os.path.isfile(logPath))
        with open(recordsPath, encoding="utf-8") as f:
            text = f.read()
        with open(recordsPath, "wb") as f:
            f.write(text.encode("cp1252"))  # as a spreadsheet saves it on Windows
        with open(recordsPath, "rb") as f:
            damaged = f.read()
        with open(logPath, "rb") as f:
            log = f.read()

        fresh = libcard_sources.SourcesCard(session)
        self.cleanup.append(fresh.widget.close)
        fresh.widget.show()
        fresh.refresh()  # no exception: the PRISMA counts use the empty stand-in, not the file again
        self._pump()
        self.assertTrue(fresh.errorLabel.isVisible())
        self.assertIn("cannot be read", fresh.errorLabel.text)
        self.assertIsNone(fresh.importRecords(text="10.9999/after.damage\n"))
        self._pump()
        self.assertTrue(fresh.errorLabel.isVisible())
        self.assertIn("cannot be read", fresh.errorLabel.text)
        self.assertIn("Nothing was imported", fresh.errorLabel.text)
        with open(recordsPath, "rb") as f:
            self.assertEqual(f.read(), damaged)
        with open(logPath, "rb") as f:
            self.assertEqual(f.read(), log)
        with self.assertRaises(sources.SourcesError):
            fresh.records.save()  # the library refuses too
        with open(recordsPath, "rb") as f:
            self.assertEqual(f.read(), damaged)

        # Once the file is saved as UTF-8 again, the records come back and the error goes
        with open(recordsPath, "w", encoding="utf-8") as f:
            f.write(text)
        fresh.refresh()
        self._pump()
        self.assertEqual(len(fresh.records), 2)
        self.assertIsNone(fresh.records.unreadable)
        self.assertIsNotNone(fresh.importRecords(text="10.9999/after.fix\n"))
        self.assertFalse(fresh.errorLabel.isVisible())
        self.assertEqual(len(fresh.records), 3)

    def test_sources_unreadable_stage_log_is_reported_and_kept(self):
        """A stage_log.tsv that cannot be read is said on the card, P/X/U and Import do not write over it, and once it is
        fixed (records.tsv unchanged) the card reads it again with the full-text exclusion in place (BB-1).
        """
        from LesionNetworkMappingLib import sources
        from LesionNetworkMappingLib.study import Study
        from StudioLib import libcard_sources, study_ui

        path = os.path.join(self.directory, "LitLog")
        Study.create(path, name="LitLog").save()
        session = study_ui.openStudy(path, None)
        self.cleanup.append(study_ui.closeStudy)
        original = libcard_sources._toast
        libcard_sources._toast = lambda *args, **kwargs: None
        self.cleanup.append(lambda: setattr(libcard_sources, "_toast", original))
        card = libcard_sources.SourcesCard(session)
        self.cleanup.append(card.widget.close)
        self.assertIsNotNone(card.importRecords(text="10.9999/one\n10.9999/two\n"))
        records = card.load()
        first, _second = records.ids()
        records.advance(first)
        records.advance(first)
        records.exclude(first, "No usable lesion imaging")
        records.save()
        logPath = records.log_path
        with open(records.path, "rb") as f:
            table = f.read()
        with open(logPath, "rb") as f:
            good = f.read()
        with open(logPath, "ab") as f:
            f.write(b"x\ty\tz\te1\te2\te3\n")
        with open(logPath, "rb") as f:
            damaged = f.read()

        fresh = libcard_sources.SourcesCard(session)
        self.cleanup.append(fresh.widget.close)
        fresh.widget.show()
        fresh.refresh()
        self._pump()
        self.assertTrue(fresh.errorLabel.isVisible())
        self.assertIn("stage_log.tsv", fresh.errorLabel.text)
        self.assertIsNone(fresh.importRecords(text="10.9999/after.damage\n"))
        with self.assertRaises(sources.SourcesError):
            fresh.records.save()
        with open(logPath, "rb") as f:
            self.assertEqual(f.read(), damaged)
        with open(records.path, "rb") as f:
            self.assertEqual(f.read(), table)

        with open(logPath, "wb") as f:
            f.write(good)
        fresh.refresh()
        self._pump()
        self.assertIsNone(fresh.records.unreadable)
        self.assertEqual(len(fresh.records), 2)
        self.assertFalse(fresh.errorLabel.isVisible())
        counts = sources.prisma_counts(session.study, fresh.records)
        self.assertEqual((counts["excluded_screening"], counts["fulltext_excluded"]), (0, 1))

    def test_sources_prisma_export_on_read_only_study_is_plain(self):
        """A read-only study disables PRISMA export, and a permission error is said in plain words without a logged traceback (BB-1)."""
        import errno
        import logging
        import types
        from unittest import mock

        from StudioLib import libcard_sources

        card = libcard_sources.SourcesCard(None)
        errors = []
        card._showError = errors.append
        records = []

        class Handler(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = Handler()
        logging.getLogger().addHandler(handler)
        try:

            def denied():
                raise PermissionError(errno.EACCES, "Permission denied", "/x/derivatives/prisma")

            self.assertIsNone(card._run(denied))
        finally:
            logging.getLogger().removeHandler(handler)
        self.assertIn("read-only", errors[-1])
        self.assertNotIn("Errno", errors[-1])
        self.assertTrue(records)
        for record in records:
            self.assertIsNone(record.exc_info)
            self.assertLess(record.levelno, logging.ERROR)
        # Button state: records present, but the study folder cannot be written
        card.records = [1]
        card.selectedId = lambda: None
        card._study = lambda: types.SimpleNamespace(path="/tmp")
        with mock.patch.object(libcard_sources.os, "access", return_value=False):
            card._updateButtons()
        self.assertFalse(card.prismaButton.isEnabled())
        self.assertIn("read-only", card.prismaButton.toolTip)
        with mock.patch.object(libcard_sources.os, "access", return_value=True):
            card._updateButtons()
        self.assertTrue(card.prismaButton.isEnabled())

    def test_tutorials_options_form_has_no_empty_row_when_scans_do_not_apply(self):
        """Tutorial cohorts: for a cohort without the scans option, "New study in" sits one normal form spacing under "Subjects".
        The scans checkbox and its note used to be two rows with an empty label that stayed visible and kept the spacing.
        """
        from StudioLib import libcard_tutorials

        tutorialsDialog = libcard_tutorials.TutorialsDialog()
        self.cleanup.append(tutorialsDialog.dialog.deleteLater)
        tutorialsDialog.dialog.show()
        self.cleanup.append(tutorialsDialog.dialog.hide)
        for row in range(tutorialsDialog.listWidget.count):
            tutorialsDialog.listWidget.setCurrentRow(row)
            slicer.app.processEvents()
            entry = tutorialsDialog.entry()
            if entry["source"]["type"] == "folder":
                continue
            applies = bool(entry.get("native"))
            self.assertEqual(tutorialsDialog.scansRow.isVisibleTo(tutorialsDialog.dialog), applies, entry["id"])
            if not applies:
                top = tutorialsDialog.limitCombo.mapTo(tutorialsDialog.dialog, qt.QPoint(0, 0)).y()
                rowAfter = tutorialsDialog.folderRow.mapTo(tutorialsDialog.dialog, qt.QPoint(0, 0)).y()
                gap = rowAfter - (top + tutorialsDialog.limitCombo.height)
                self.assertLessEqual(gap, tutorialsDialog.limitRow.verticalSpacing() + 2, "%s: gap %d" % (entry["id"], gap))

    def test_tutorials_one_subject_or_one_file_texts_are_singular(self):
        """Tutorial cohorts: a count of one reads "1 file" in the download status and "1 subject" in the toast and the limit combo."""
        import inspect

        from StudioLib import libcard_tutorials

        tutorialsDialog = libcard_tutorials.TutorialsDialog()
        self.cleanup.append(tutorialsDialog.dialog.deleteLater)
        tutorialsDialog._onEvent({"event": "progress", "stage": "download", "done": 0, "total": 1})
        self.assertIn("0 of 1 file", tutorialsDialog.statusLabel.text)
        self.assertNotIn("1 files", tutorialsDialog.statusLabel.text)
        tutorialsDialog._onEvent({"event": "progress", "stage": "download", "done": 1, "total": 3})
        self.assertIn("1 of 3 files", tutorialsDialog.statusLabel.text)
        source = inspect.getsource(libcard_tutorials.TutorialsDialog)
        for singular in ("All {n} subject", "Tutorial study ready · {n} subject"):
            self.assertIn('_("%s")' % singular, source, singular)

    def test_tutorials_delete_downloads_is_disabled_while_downloading(self):
        """Tutorial cohorts: "Delete downloads" is disabled while the cohort downloads (freeCache does nothing then), like the other fields."""
        import types

        from StudioLib import libcard_tutorials

        tutorialsDialog = libcard_tutorials.TutorialsDialog()
        self.cleanup.append(tutorialsDialog.dialog.deleteLater)
        tutorialsDialog.select("joutsa2022_addiction")
        tutorialsDialog.job = types.SimpleNamespace(isFinished=lambda: False)
        tutorialsDialog._updateButtons()
        self.assertFalse(tutorialsDialog.cacheButton.isEnabled())
        tutorialsDialog.job = types.SimpleNamespace(isFinished=lambda: True)
        tutorialsDialog._updateButtons()
        self.assertTrue(tutorialsDialog.cacheButton.isEnabled())

    def test_tutorials_relative_or_tilde_folder_never_builds_inside_the_application(self):
        """Tutorial cohorts: "~/name" is expanded to the home folder and a relative folder is refused, for the new study and for
        the downloaded folder. The build job runs in the module folder, so both were built inside the application before.
        """
        import types

        from LesionNetworkMappingUI import jobrunner
        from StudioLib import libcard_tutorials

        home = os.path.realpath(os.path.expanduser("~"))
        work = tempfile.mkdtemp()
        self.cleanup.append(lambda: shutil.rmtree(work, ignore_errors=True))
        submitted = []
        fakeJob = types.SimpleNamespace(isFinished=lambda: True)
        savedInstance = jobrunner.instance
        jobrunner.instance = lambda: types.SimpleNamespace(submit=lambda spec: submitted.append(spec) or fakeJob)
        self.cleanup.append(lambda: setattr(jobrunner, "instance", savedInstance))
        tutorialsDialog = libcard_tutorials.TutorialsDialog()
        self.cleanup.append(tutorialsDialog.dialog.deleteLater)
        button = tutorialsDialog.downloadButton
        tutorialsDialog.select("joutsa2022_addiction")
        name = "studio-test-tilde-%d" % os.getpid()

        tutorialsDialog.folderEdit.setText("relstudy")
        self.assertFalse(button.enabled, "a relative study folder must not start a build")
        self.assertIn("not a full path", button.toolTip)
        self.assertFalse(tutorialsDialog.start())
        self.assertEqual(submitted, [])

        tutorialsDialog.folderEdit.setText("~/" + name)
        self.assertTrue(button.enabled, button.toolTip)
        self.assertEqual(tutorialsDialog._options()[0], os.path.join(home, name))
        self.assertTrue(tutorialsDialog.start())
        self.assertEqual(len(submitted), 1)
        dest = submitted[0].args[submitted[0].args.index("--dest") + 1]
        self.assertEqual(os.path.realpath(dest), os.path.join(home, name))
        self.assertFalse(os.path.exists(os.path.join(home, name)), "the fake job builds nothing")

        # The "not empty" check reads the folder that the job gets
        os.makedirs(os.path.join(work, "full"))
        open(os.path.join(work, "full", "file.txt"), "w").close()
        tutorialsDialog.job = None
        tutorialsDialog.folderEdit.setText(os.path.join(work, "full"))
        self.assertFalse(button.enabled)
        self.assertIn("not empty", button.toolTip)

        # A file typed as the study folder gets its own reason, not "not empty"
        tutorialsDialog.folderEdit.setText(os.path.join(work, "full", "file.txt"))
        self.assertFalse(button.enabled)
        self.assertIn("is a file", button.toolTip)
        self.assertNotIn("not empty", button.toolTip)

        # A cohort only linked (LESYMAP): the downloaded folder is refused when relative
        tutorialsDialog.select("lesymap_groundtruth")
        tutorialsDialog.folderEdit.setText(os.path.join(work, "new"))
        tutorialsDialog.sourceEdit.setText("downloaded")
        self.assertFalse(button.enabled)
        self.assertIn("not a full path", button.toolTip)
        tutorialsDialog.sourceEdit.setText(work)
        self.assertTrue(button.enabled, button.toolTip)

    def test_tutorials_status_shows_notes_without_work_package_code(self):
        """Tutorial cohorts: the notes of the build (for example a scan that could not be downloaded) are shown in the dialog,
        and no text shows the work-package id "A-03" (it said "1 note, see the job log" and the log could not be opened).
        """
        import types

        from StudioLib import libcard_tutorials

        tutorialsDialog = libcard_tutorials.TutorialsDialog()
        self.cleanup.append(tutorialsDialog.dialog.deleteLater)
        tutorialsDialog.select("joutsa2022_addiction")
        self.assertNotIn("A-03", tutorialsDialog.nativeLabel.text)
        tutorialsDialog._jobId = "joutsa2022_addiction"
        tutorialsDialog._warnings = 0
        tutorialsDialog._notes = []
        tutorialsDialog._startOptions = tutorialsDialog._options()
        tutorialsDialog._onEvent({"event": "warning", "code": "tutorial", "message": "sub-N03: the scan could not be downloaded (HTTP 404)."})
        job = types.SimpleNamespace(succeeded=lambda: True, outputs=[], results=[{"kind": "study", "subjects": 3, "native": True}], state="done")
        tutorialsDialog._onDone(job)
        text = tutorialsDialog.statusLabel.text
        self.assertNotIn("A-03", text)
        self.assertNotIn("job log", text)
        self.assertIn("Library > Normalization", text)
        self.assertIn("sub-N03: the scan could not be downloaded", text)

    def test_tutorials_refuses_studio_folders_projects_and_other_studies(self):
        """Tutorial cohorts: "New study in" follows the rule of New study and Create study (study_ui.studyFolderProblem, A-09).
        A folder in Projects/, inside a project or inside another study was built: the study in Projects/ then showed in
        Library as a broken "Empty" project, and a nested study landed in another study's derivatives/.
        """
        import types

        from LesionNetworkMappingLib import study as studyModule
        from LesionNetworkMappingUI import jobrunner
        from StudioLib import libcard_tutorials, projects, study_ui

        saved = os.environ.get("SLICER_STUDIO_PROJECTS_ROOT")

        def restore():
            if saved is None:
                os.environ.pop("SLICER_STUDIO_PROJECTS_ROOT", None)
            else:
                os.environ["SLICER_STUDIO_PROJECTS_ROOT"] = saved

        self.cleanup.append(restore)
        root = os.path.join(self.directory, "Slicer Studio")
        os.makedirs(root)
        os.environ["SLICER_STUDIO_PROJECTS_ROOT"] = root
        self.assertTrue(projects.isEnabled())
        project = projects.createProject("myproject").path
        outer = os.path.join(self.directory, "outer-study")
        studyModule.Study.create(outer, name="outer-study")
        submitted = []
        fakeJob = types.SimpleNamespace(isFinished=lambda: True)
        savedInstance = jobrunner.instance
        jobrunner.instance = lambda: types.SimpleNamespace(submit=lambda spec: submitted.append(spec) or fakeJob)
        self.cleanup.append(lambda: setattr(jobrunner, "instance", savedInstance))
        tutorialsDialog = libcard_tutorials.TutorialsDialog()
        self.cleanup.append(tutorialsDialog.dialog.deleteLater)
        button = tutorialsDialog.downloadButton
        tutorialsDialog.select("joutsa2022_addiction")
        refused = {
            os.path.join(projects.projectsFolder(), "tutorial-in-projects"): "part of the project",
            os.path.join(project, "x"): "part of the project myproject",
            os.path.join(outer, "derivatives", "inner-study"): "inside the study outer-study",
            projects.studiesFolder(): "Studies/",
        }
        for folder, words in refused.items():
            tutorialsDialog.folderEdit.setText(folder)
            self.assertFalse(button.enabled, folder)
            self.assertIn(study_ui.studyFolderProblem(folder), button.toolTip, folder)
            self.assertIn(words, button.toolTip, folder)
            self.assertFalse(tutorialsDialog.start(), folder)
        self.assertEqual(submitted, [])
        # The button state is checked again when the build starts: the folder became a project after it was typed
        late = os.path.join(projects.projectsFolder(), "late")
        tutorialsDialog.folderEdit.setText(os.path.join(root, "Studies", "late"))
        self.assertTrue(button.enabled, button.toolTip)
        tutorialsDialog.folderEdit.blockSignals(True)
        tutorialsDialog.folderEdit.setText(late)
        tutorialsDialog.folderEdit.blockSignals(False)
        self.assertFalse(tutorialsDialog.start())
        self.assertFalse(button.enabled)
        self.assertEqual(submitted, [])
        # A new folder inside Studies/ (the A-09 place of a study) is still built
        ok = os.path.join(projects.studiesFolder(), "ok-study")
        tutorialsDialog.folderEdit.setText(ok)
        self.assertTrue(button.enabled, button.toolTip)
        self.assertTrue(tutorialsDialog.start())
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0].args[submitted[0].args.index("--dest") + 1], ok)

    def test_control_set_toasts_use_the_singular_for_one(self):
        """One linked lesion reads '1 control lesion linked', one removed row '1 control row removed' (not the plural)."""
        import types

        from StudioLib import libcard_controls

        toasts = []
        study = types.SimpleNamespace()
        session = types.SimpleNamespace(study=study, flush=lambda: None)
        saved = libcard_controls._toast, libcard_controls._notify, libcard_controls.registryRoot
        libcard_controls._toast = lambda text, ms=3000, **kw: toasts.append(text)
        libcard_controls._notify = lambda session: None
        libcard_controls.registryRoot = lambda: ""
        try:
            for added, updated, removed in ((["a"], [], []), (["a", "b"], [], []), ([], ["a"], []), ([], ["a", "b"], [])):
                study.link_control_set = lambda name, ids=None, root=None, a=added, u=updated, r=removed: {"added": a, "updated": u, "removed": r, "skipped": []}
                libcard_controls.linkSet(session, {"name": "ctl"})
            for gone in (["a"], ["a", "b"]):
                study.unlink_control_set = lambda name, g=gone: g
                libcard_controls.unlinkSet(session, "ctl")
        finally:
            libcard_controls._toast, libcard_controls._notify, libcard_controls.registryRoot = saved
        self.assertEqual(
            toasts,
            [
                "1 control lesion linked (no files copied)",
                "2 control lesions linked (no files copied)",
                "1 control row updated",
                "2 control rows updated",
                "1 control row removed from the study (the control set is kept)",
                "2 control rows removed from the study (the control set is kept)",
            ],
        )

    def test_control_sets_list_elides_long_rows_without_a_scroll_bar(self):
        """A row of the Control sets list that does not fit is elided with an ellipsis: the list has no horizontal scroll bar (the
        row was as wide as its text, so it was cut mid-word at the panel edge and the bar took height from the fixed-height list).
        """
        import types

        from StudioLib import libcard_controls

        entries = [{"name": "ctlset", "n": 24, "space": "MNI152NLin2009cAsym", "licence_note": "A licence note that is long enough " * 4}]
        saved = libcard_controls._session, libcard_controls.registeredSets
        libcard_controls._session = lambda: types.SimpleNamespace(study=types.SimpleNamespace(control_sets=[]))
        libcard_controls.registeredSets = lambda: entries
        host = qt.QWidget()
        try:
            card = libcard_controls.ControlSetsCard(None)
            card._renderSelection = lambda: None  # only the list is under test
            layout = qt.QVBoxLayout(host)
            layout.addWidget(card.widget)
            host.setFixedWidth(300)
            host.show()
            card.refresh()
            slicer.app.processEvents()
        finally:
            libcard_controls._session, libcard_controls.registeredSets = saved
        self.cleanup.append(host.deleteLater)
        self.assertEqual(card.list.horizontalScrollBarPolicy, qt.Qt.ScrollBarAlwaysOff)
        self.assertFalse(card.list.horizontalScrollBar().isVisible())
        self.assertFalse(card.list.verticalScrollBar().isVisible(), "the one row fits the fixed-height list")
        self.assertEqual(card.list.textElideMode, qt.Qt.ElideRight)
        self.assertTrue(card.list.sizeHintForColumn(0) > card.list.viewport().width, "the row text is wider than the list")

    def test_assign_button_returns_when_the_form_closes(self):
        """The "Assign second tracing…" row of the Agreement card and of the BATCH card is shown again when the form closes (Cancel,
        Assign, Escape). Assign saves and refreshes the card while the form is still open, so the row stayed hidden afterwards.
        """
        from StudioLib import libcard_agreement

        card = libcard_agreement.AgreementCard(None)
        batch = libcard_agreement.BatchSection(None)
        host = qt.QWidget()
        layout = qt.QVBoxLayout(host)
        layout.addWidget(card.widget)
        layout.addWidget(batch.widget)
        host.show()
        self.cleanup.append(host.deleteLater)
        for form, row, open_ in ((card.form, card.assignRow, card._showForm), (batch.form, batch.row, batch._showForm)):
            for close in (form.hide, form.cancelButton.click):
                open_()
                self.assertTrue(form.isOpen())
                self.assertTrue(row.isHidden())
                # a refresh while the form is open keeps the row hidden, as Assign does
                if form is batch.form:
                    batch.refresh(["sub-01"])
                    self.assertTrue(row.isHidden())
                close()
                self.assertFalse(form.isOpen())
                self.assertFalse(row.isHidden(), form.widget.objectName)

    def test_agreement_card_does_not_ask_to_assign_when_subjects_are_assigned(self):
        """Once subjects are assigned (todo > 0, no pair yet) the Agreement card does not tell the user to assign again."""
        import types

        from StudioLib import libcard_agreement

        def hint(todo):
            saved = libcard_agreement._session, libcard_agreement.counts
            libcard_agreement._session = lambda: types.SimpleNamespace(study=types.SimpleNamespace(path="x"))
            libcard_agreement.counts = lambda study: {"todo": todo, "pairs": 0, "adjudicate": 0, "consensus": 0}
            try:
                card = libcard_agreement.AgreementCard(None)
                card.refresh()
                return str(card.metrics.text)
            finally:
                libcard_agreement._session, libcard_agreement.counts = saved

        self.assertNotIn("assign", hint(6).lower())
        self.assertIn("assign second tracings", hint(0))

    def test_compute_agreement_on_a_read_only_study_says_so_plainly(self):
        """BB-1: Compute agreement on a read-only study toasted "[Errno 13] Permission denied: '.../derivatives/qc'" and logged
        a traceback: it is refused up front with the plain read-only message, and a study that turns read-only while the
        agreement is computed is told the same way.
        """
        import types

        from StudioLib import libcard_agreement, review, study_ui

        class _Session:
            def __init__(self):
                self.study = types.SimpleNamespace(path="/study", name="Study")
                self.marked = False

            def _requireWritable(self, notDone):
                raise study_ui.ReadOnlyStudyError(f"The study is read-only. {notDone}")

            def _markReadOnly(self):
                self.marked = True

        toasts = []
        saved = (libcard_agreement._session, review._toast, libcard_agreement._rules)
        session = _Session()
        try:
            libcard_agreement._session = lambda: session
            review._toast = lambda text, *a, **k: toasts.append(text)
            self.assertFalse(libcard_agreement.computeAgreement())
            self.assertEqual(len(toasts), 1, toasts)
            self.assertIn("read-only", toasts[0])
            self.assertNotIn("Errno", toasts[0])

            def denied(study, computed=None):
                raise PermissionError(13, "Permission denied", "/study/derivatives/qc")

            libcard_agreement._rules = lambda: types.SimpleNamespace(summary=denied)
            session.setValues = session.flush = lambda *a, **k: None
            del toasts[:]
            libcard_agreement._computed("/study", {}, None)
            self.assertEqual(len(toasts), 1, toasts)
            self.assertIn("read-only", toasts[0])
            self.assertNotIn("Errno", toasts[0])
            self.assertNotIn("/study", toasts[0])
            self.assertTrue(session.marked)
        finally:
            libcard_agreement._session, review._toast, libcard_agreement._rules = saved

    def test_compute_agreement_reads_no_mask_on_the_gui_thread(self):
        """"Compute agreement" reads the masks and computes the metrics in a worker thread, with its progress and Stop on the card;
        the main thread writes interrater.tsv and the rows when it has finished (BB-1: every pair was computed on the GUI thread,
        about 0.4 s per pair on 1 mm data, and the application froze for seconds with no progress and no way to stop it).
        """
        import datetime
        import threading
        import types

        from LesionNetworkMappingLib import agreement, nifti
        from LesionNetworkMappingLib.study import Study
        from StudioLib import libcard_agreement, review

        shape = (40, 40, 40)
        affine = np.array([[1.0, 0, 0, -20.0], [0, 1.0, 0, -30.0], [0, 0, 1.0, -10.0], [0, 0, 0, 1.0]])

        def cube(center, half=3):
            mask = np.zeros(shape, dtype=np.uint8)
            mask[tuple(slice(c - half, c + half + 1) for c in center)] = 1
            return mask

        study = Study.create(os.path.join(self.directory, "study"), space="MNI152NLin6Asym")
        old = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=10)).isoformat(timespec="seconds")
        centers = {"sub-01": (12, 20, 20), "sub-02": (28, 20, 20), "sub-03": (20, 12, 20), "sub-04": (20, 28, 20)}
        for sid, center in centers.items():
            relative = f"lesions/{sid}_space-MNI152NLin6Asym_desc-lesion_mask.nii.gz"
            os.makedirs(os.path.join(study.path, "lesions"), exist_ok=True)
            nifti.write_nifti(os.path.join(study.path, relative), cube(center), affine, dtype=np.uint8)
            study.add_subject(sid, group="case", status="traced", lesion=relative, space="MNI152NLin6Asym", tracer="Ana Bell", modified=old)
        scan = os.path.join(study.path, "template.nii.gz")
        nifti.write_nifti(scan, np.zeros(shape, dtype=np.float32), affine)
        agreement.assign(study, sids=list(centers), rater="Carl Diaz")
        for sid, center in centers.items():
            shifted = (center[0] + 1, center[1], center[2])
            agreement.save_second_tracing(study, sid, np.ascontiguousarray(cube(shifted).transpose(2, 1, 0)), affine, scan, "Carl Diaz", compute=False)
        study.save()
        expected = agreement.summary(study, write=False, update_rows=False)
        self.assertEqual(expected["n_pairs"], 4)

        flushed, toasts = [], []
        session = types.SimpleNamespace(study=study, setValues=lambda *args, **kwargs: None, flush=lambda: flushed.append(True))
        saved = (libcard_agreement._session, review._toast, agreement.load_mask)
        gate = threading.Event()
        reads = []

        def loadMask(path):
            onMain = threading.current_thread() is threading.main_thread()
            reads.append(onMain)
            if not onMain:
                gate.wait(30)  # holds the worker while the test looks at the card
            return saved[2](path)

        def restore():
            gate.set()
            libcard_agreement.waitForAgreement(30)
            libcard_agreement._session, review._toast, agreement.load_mask = saved

        libcard_agreement._session = lambda: session
        review._toast = lambda text, *args, **kwargs: toasts.append(text)
        agreement.load_mask = loadMask
        self.cleanup.append(restore)
        card = libcard_agreement._cards[libcard_agreement._createCard(None)]  # as the Library creates it
        host = qt.QWidget()
        qt.QVBoxLayout(host).addWidget(card.widget)
        host.show()
        self.cleanup.append(host.deleteLater)
        card.refresh()
        self._pump()
        self.assertTrue(card.computeRow.isVisible())
        tsv = os.path.join(study.path, *agreement.INTERRATER_TSV)

        card.computeButton.click()
        self._wait(100)
        self.assertNotIn(True, reads, "no mask is read on the GUI thread")
        self.assertTrue(libcard_agreement.isComputing(), "the worker waits at the gate")
        self.assertFalse(os.path.exists(tsv))
        self.assertTrue(card.progressRow.isVisible())
        self.assertFalse(card.computeButton.enabled)
        self.assertTrue(str(card.progressLabel.text).startswith("Computing agreement"), card.progressLabel.text)
        gate.set()
        self.assertTrue(libcard_agreement.waitForAgreement(60))
        self.assertNotIn(True, reads, "no mask is read on the GUI thread")
        self.assertGreaterEqual(len(reads), 8)
        self.assertEqual(toasts[-1], libcard_agreement.written_text(4))
        self.assertTrue(flushed)
        data = agreement.read_summary(study)
        self.assertEqual(data["n_pairs"], 4)
        self.assertEqual(data["median"], expected["median"])
        self.assertEqual(data["baseline"], expected["baseline"])
        with open(tsv, encoding="utf-8") as f:
            rows = f.read().splitlines()[1:]
        dice = {row.split("\t")[0]: float(row.split("\t")[agreement.TSV_COLUMNS.index("dice")]) for row in rows}
        self.assertEqual(dice, {pair["participant_id"]: pair["dice"] for pair in expected["pairs"]})
        self.assertAlmostEqual(float(study.subject("sub-01")["rater2_dice"]), expected["pairs"][0]["dice"], places=4)
        self._pump()
        self.assertFalse(card.progressRow.isVisible())
        self.assertTrue(card.computeButton.enabled)

        # Stop: the worker stops before the next pair and the last interrater.tsv stays
        with open(tsv, encoding="utf-8") as f:
            before = f.read()
        gate.clear()
        del reads[:]
        card.computeButton.click()
        self._wait(100)
        self.assertTrue(libcard_agreement.isComputing())
        card.stopLink.click()
        gate.set()
        self.assertTrue(libcard_agreement.waitForAgreement(60))
        self.assertEqual(toasts[-1], "Agreement not computed: stopped")
        self.assertLess(len(reads), 8, "stopped before every pair was read")
        with open(tsv, encoding="utf-8") as f:
            self.assertEqual(f.read(), before)

    def test_corrupt_lesion_thumbnail_is_named_unreadable(self):
        """A damaged lesion file gives a one-line log message and the card note "Lesion file cannot be read" (BB-1)."""
        import logging
        import types

        from StudioLib import library, thumbnails

        grid = GRID_NLIN6_2MM
        good = self._writeMask("sub-01_lesion.nii.gz", grid)
        bad = os.path.join(self.directory, "sub-02_lesion.nii.gz")
        with open(good, "rb") as f:
            blob = f.read()
        with open(bad, "wb") as f:  # a valid gzip header with a damaged deflate stream
            f.write(blob[:20] + bytes(b ^ 0xFF for b in blob[20:60]) + blob[60:])
        plain = os.path.join(self.directory, "sub-03_lesion.nii.gz")
        with open(plain, "w") as f:  # not gzip at all
            f.write("not a NIfTI image")
        truncated = os.path.join(self.directory, "sub-04_lesion.nii.gz")
        with open(truncated, "wb") as f:
            f.write(blob[: len(blob) // 2])

        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = Capture(level=logging.DEBUG)
        logging.getLogger().addHandler(handler)
        self.cleanup.append(lambda: logging.getLogger().removeHandler(handler))
        service = thumbnails.ThumbnailService(self.directory)
        self.cleanup.append(service.stop)
        requests = {name: service.requestFor(name, path, None) for name, path in (("good", good), ("bad", bad), ("plain", plain), ("truncated", truncated))}
        for request in requests.values():
            service.request(request)
        deadline = time.time() + 60
        while not service.isIdle() and time.time() < deadline:
            self._wait(50)
        self._pump()
        self.assertIsNotNone(service.pixmap(requests["good"].key))
        self.assertFalse(service.lesionUnreadable(requests["good"].key))
        for name in ("bad", "plain", "truncated"):
            key = requests[name].key
            self.assertIsNotNone(service.failed(key), name)
            self.assertTrue(service.lesionUnreadable(key), name)
            info = types.SimpleNamespace(note="No preview", request=requests[name], thumbKey=key)
            self.assertEqual(library._CardInfo.failedNote(info, service), "Lesion file cannot be read", name)
        # A lesion that was fine, or a failure that is not about the lesion, keeps the card's own note
        info = types.SimpleNamespace(note="No preview", request=requests["good"], thumbKey=requests["good"].key)
        self.assertEqual(library._CardInfo.failedNote(info, service), "No preview")
        self.assertEqual(library._CardInfo.failedNote(types.SimpleNamespace(note="No preview", request=None, thumbKey=None), service), "No preview")
        # One line per damaged file, no traceback (an expected error with user data)
        ours = [r for r in records if "no thumbnail for" in r.getMessage()]
        self.assertEqual(len(ours), 3, [r.getMessage() for r in ours])
        for record in ours:
            message = record.getMessage()
            self.assertEqual(record.levelno, logging.WARNING)
            self.assertNotIn("Traceback", message)
            self.assertNotIn("\n", message)
            self.assertIn("cannot be read", message)

    def test_search_lesion_set_commands_scroll_to_the_cohort_card(self):
        """Search > Check lesion set scrolls the inspector to the Cohort card, which sits below the visible part."""
        from StudioLib import libcard_cohort

        scroll = qt.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.resize(300, 200)
        content = qt.QWidget()
        layout = qt.QVBoxLayout(content)
        filler = qt.QWidget()
        filler.setMinimumHeight(1200)
        layout.addWidget(filler)
        cardWidget = qt.QWidget()
        cardWidget.setMinimumHeight(100)
        layout.addWidget(cardWidget)
        scroll.setWidget(content)
        scroll.show()
        slicer.app.processEvents()
        self.addCleanup(scroll.deleteLater)
        calls = []

        class FakeCard:
            widget = cardWidget
            showCard = libcard_cohort.CohortCard.showCard

            def run(self):
                calls.append("run")
                return True

            def overview(self):
                calls.append("overview")
                return True

        card = FakeCard()
        original = (libcard_cohort._session, libcard_cohort.liveCard)
        libcard_cohort._session = object
        libcard_cohort.liveCard = lambda: card
        try:
            self.assertEqual(scroll.verticalScrollBar().value, 0)
            self.assertTrue(libcard_cohort.checkLesionSetAction())
            slicer.app.processEvents()
            top = cardWidget.mapTo(scroll.viewport(), qt.QPoint(0, 0)).y()
            self.assertEqual(calls, ["run"])
            self.assertTrue(0 <= top < scroll.viewport().height, f"the card's top is at y={top}")
        finally:
            libcard_cohort._session, libcard_cohort.liveCard = original

    def test_cohort_duplicates_row_lists_only_lesions_the_check_used(self):
        """Intake duplicate flags of lesions left out of the check (not approved in review) are not listed in the Duplicates row."""
        from types import SimpleNamespace

        from StudioLib import libcard_cohort

        rows = [{"participant_id": "sub-01", "qc_flags": "duplicate"}, {"participant_id": "sub-02", "qc_flags": ""},
                {"participant_id": "sub-12", "qc_flags": "x;duplicate"}]
        study = SimpleNamespace(subjects=rows)
        thread = {"used": ["sub-01", "sub-02"], "excluded": {"not approved in review": ["sub-12"]}}
        self.assertEqual(libcard_cohort.intakeDuplicateFlags(study, thread), ["sub-01"])
        recorded = {"excluded": {"not approved in review": ["sub-12"]}}
        self.assertEqual(libcard_cohort.intakeDuplicateFlags(study, recorded), ["sub-01"])
        self.assertEqual(libcard_cohort.intakeDuplicateFlags(study, {"used": ["sub-12"]}), ["sub-12"])

    def test_cohort_subset_line_uses_singular_for_one(self):
        """The subset line says "1 case + 1 control" and "1 case without a control", not "1 cases + 1 controls"."""
        from StudioLib import libcard_cohort

        self.assertEqual(
            libcard_cohort.subsetText("volume_matched-2", 1, 1, -0.55, 0.0, 1),
            "Subset volume_matched-2: 1 case + 1 control · volume SMD -0.55 → +0.00 · 1 case without a control",
        )
        self.assertEqual(
            libcard_cohort.subsetText("volume_matched", 5, 5, -0.58, -0.59, 2),
            "Subset volume_matched: 5 cases + 5 controls · volume SMD -0.58 → -0.59 · 2 cases without a control",
        )
        self.assertEqual(libcard_cohort.subsetText("s", 2, 1), "Subset s: 2 cases + 1 control")

    def test_network_map_result_is_a_map_not_a_scan(self):
        """Map network results (LNM.Result, computing/partial/finished) are maps, never among the scans to trace on."""
        import numpy as np
        from StudioLib import library

        scan = slicer.util.addVolumeFromArray(np.zeros((4, 4, 4), dtype=np.uint8), name="plain scan")
        result = slicer.util.addVolumeFromArray(np.zeros((4, 4, 4), dtype=np.float32), name="Lesions-LNM T N=50 (partial)")
        result.SetAttribute("LNM.Result", "1")
        self.assertEqual(library.volumeKind(scan), "scan")
        self.assertEqual(library.volumeKind(result), "statmap")
        scans = library.scanNodes()
        self.assertIn(scan, scans)
        self.assertNotIn(result, scans)

    def test_review_and_agreement_counts_use_singular_for_one(self):
        """The Review, Agreement and adjudication texts say "1 change requested", "1 pair", "1 lesion" and "1 second tracing", not
        "1 changes requested", "1 pairs", "lesion(s)" or "tracing(s)".
        """
        from StudioLib import adjudicate, libcard_agreement, libcard_review

        self.assertEqual(libcard_review.changes_text(1), "1 change requested")
        self.assertEqual(libcard_review.changes_text(0), "0 changes requested")
        self.assertEqual(libcard_review.changes_text(2), "2 changes requested")
        self.assertIn("leave out 1 lesion that", libcard_review.gate_text(1))
        self.assertIn("lists it;", libcard_review.gate_text(1))
        self.assertIn("leave out 3 lesions that", libcard_review.gate_text(3))
        self.assertEqual(libcard_agreement.pairs_text(1), "1 pair")
        self.assertEqual(libcard_agreement.pairs_text(0), "0 pairs")
        self.assertEqual(libcard_agreement.pairs_text(4), "4 pairs")
        self.assertEqual(libcard_agreement.assigned_text(1), "1 second tracing assigned")
        self.assertEqual(libcard_agreement.assigned_text(6), "6 second tracings assigned")
        self.assertEqual(libcard_agreement.written_text(1), "Agreement of 1 pair written to derivatives/qc/interrater.tsv")
        self.assertEqual(libcard_agreement.written_text(2), "Agreement of 2 pairs written to derivatives/qc/interrater.tsv")
        # The position of the notice title never breaks across lines (no-break spaces)
        self.assertEqual(adjudicate.noticeTitle("Adjudicate", "sub-01", 1, 1), "Adjudicate · sub-01 · 1\u00a0of\u00a01")
        self.assertEqual(adjudicate.left_out_text(1, "why"), "1 second tracing left out: why")
        self.assertEqual(adjudicate.left_out_text(2, "why"), "2 second tracings left out: why")
        # The pair opens on a tracing even when the two do not overlap (the union centroid lies between them)
        import numpy as np
        maskA = np.zeros((20, 20, 40), dtype=bool)
        maskB = np.zeros_like(maskA)
        maskA[8:12, 8:12, 4:8] = True
        maskB[8:12, 8:12, 30:34] = True
        affine = np.diag([1.0, 1.0, 1.0, 1.0])
        center = adjudicate.jump_center(maskA, maskB, affine)
        self.assertTrue(maskA[tuple(np.rint(center).astype(int))] or maskB[tuple(np.rint(center).astype(int))], center)
        maskB[8:12, 8:12, 8:30] = True
        center = adjudicate.jump_center(maskA, maskB, affine)
        self.assertTrue(maskB[tuple(np.rint(center).astype(int))], center)
        self.assertIsNone(adjudicate.jump_center(np.zeros_like(maskA), np.zeros_like(maskA), affine))
        for module in (libcard_review, libcard_agreement, adjudicate):
            with open(module.__file__) as f:
                source = f.read()
            self.assertNotIn("lesion(s)", source, module.__name__)
            self.assertNotIn("tracing(s)", source, module.__name__)

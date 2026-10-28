"""Visual design of Slicer Studio (V-01, VISUAL.md): design tokens, card geometry and text that fits.

Run with the main window and testing enabled:

  Slicer --no-splash --testing --python-code "import slicer.testing; slicer.testing.runUnitTest(['<this dir>'], 'StudioVisualTest'); slicer.app.exit(0)"

Each check runs in the dark and in the light theme (the "Dark Slicer" and "Light Slicer" styles, switched at run time
without writing a setting):

- (a) tokens: the Python fallback tables of ``ui.py`` are the tokens of ``qSlicerStyle``; text tokens reach 4.5:1 on
  every surface they are used on, icons and control borders 3:1, white text on the accent 4.5:1;
- (b) cards: a card has the card role, 14 px padding, a rounded corner (the corner pixel is not the card fill) and the
  card fill inside; the ``ui`` radii and spacing constants match VISUAL.md;
- (c) switches: the switch track is on the right edge of a ``ui.switch()`` row, the label on the left;
- (d) text that fits: in a 1366 x 768 window, every visible plain-text label and text button of the Studio surfaces
  (top bar, Library, Draw, Map) shows its whole text (or elides it on purpose with an ellipsis);
- (e) the view bars: the flat bar is a pill with the view dot, and the slice readout keeps its plain text; the corner text
  of a flat view is the Studio text only while Studio is shown (Classic keeps the "L:", "F:" and "B:" lines).
"""

import itertools
import os
import tempfile
import time

import numpy as np
import qt

import slicer
from slicer.ScriptedLoadableModule import ScriptedLoadableModuleTest

STYLE_NAMES = {"dark": "Dark Slicer", "light": "Light Slicer"}

#: (foreground token, background tokens, minimum ratio). "white" is #ffffff.
CONTRAST_RULES = (
    ("text", ("canvas", "card", "card-low", "window", "selected"), 4.5),
    ("text2", ("canvas", "card", "card-low", "window", "selected"), 4.5),
    ("text3", ("canvas", "card", "card-low", "window"), 4.5),
    ("accent-fg", ("canvas", "card", "card-low", "window"), 4.5),
    ("white", ("accent",), 4.5),
    ("success", ("card", "card-low", "window"), 3.0),
    ("warning", ("card", "card-low", "window"), 3.0),
    ("danger", ("card", "card-low", "window"), 3.0),
    ("control-border", ("card", "card-low", "window"), 3.0),
)

#: Widgets that may show part of their text by design (an icon-only button, a scrolled field...)
CLIP_EXEMPT_CLASSES = ("QLineEdit", "QTextEdit", "QPlainTextEdit", "QTextBrowser", "QComboBox", "QSpinBox", "QDoubleSpinBox")


class StudioVisualTest(ScriptedLoadableModuleTest):
    def setUp(self):
        slicer.mrmlScene.Clear(0)
        self.mainWindow = slicer.util.mainWindow()
        self.layoutManager = slicer.app.layoutManager()
        self.initialLayout = self.layoutManager.layout if self.layoutManager is not None else None
        self.initialGeometry = self.mainWindow.saveGeometry() if self.mainWindow is not None else None
        self.initialStyleName = slicer.app.style().objectName if slicer.app.style() is not None else ""
        self._restores = []

    def tearDown(self):
        from StudioLib import shell

        for restore in reversed(self._restores):
            restore()
        self._restores = []
        live = shell._shell
        if live is not None:
            live.dispose()
        if self.layoutManager is not None and self.initialLayout is not None and self.layoutManager.layout != self.initialLayout:
            self.layoutManager.setLayout(self.initialLayout)
        if self.initialGeometry is not None:
            self.mainWindow.restoreGeometry(self.initialGeometry)
        slicer.mrmlScene.Clear(0)
        slicer.app.processEvents()

    def runTest(self):
        only = os.environ.get("STUDIO_TEST_ONLY", "")
        for name in sorted(name for name in dir(self) if name.startswith("test_") and only in name):
            self.setUp()
            try:
                getattr(self, name)()
            finally:
                self.tearDown()

    #
    # Helpers
    #

    def _pump(self, count=4):
        for _index in range(count):
            slicer.app.processEvents()

    def _wait(self, condition, milliseconds=2500):
        end = time.time() + milliseconds / 1000.0
        while time.time() < end:
            slicer.app.processEvents()
            if condition():
                return True
            time.sleep(0.01)
        return bool(condition())

    def _setTheme(self, theme):
        """Switch the application style and palette at run time (no setting is written); restored in tearDown."""
        application = slicer.app
        previous = application.style()
        previousName = previous.objectName if previous is not None else ""
        style = qt.QStyleFactory.create(STYLE_NAMES[theme])
        if style is None:
            self.skipTest(f"style {STYLE_NAMES[theme]} is not available")
        application.removeEventFilter(previous)
        application.setStyle(style)
        application.installEventFilter(application.style())
        application.setPalette(style.standardPalette())
        self._pump()

        def restore():
            if not previousName:
                return
            original = qt.QStyleFactory.create(previousName)
            if original is None:
                return
            application.removeEventFilter(application.style())
            application.setStyle(original)
            application.installEventFilter(application.style())
            application.setPalette(original.standardPalette())
            self._pump()

        if not any(getattr(item, "__name__", "") == "_restoreTheme" for item in self._restores):
            restore.__name__ = "_restoreTheme"
            self._restores.append(restore)

    def _applyStudio(self):
        from StudioLib import shell

        self.assertIsNotNone(self.mainWindow, "Studio tests need the main window")
        qt.QSettings().setValue("Studio/Draw/Layout", 7001)
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        self._pump()
        return studioShell

    def _addVolume(self, name="sub-01_T1w", shape=(24, 40, 40)):
        array = np.zeros(shape, np.int16)
        array[4:-4, 8:-8, 8:-8] = 200
        node = slicer.util.addVolumeFromArray(array, name=name)
        node.SetSpacing(2.0, 2.0, 2.0)
        return node

    @staticmethod
    def _textWidth(widget, text):
        metrics = widget.fontMetrics()
        advance = getattr(metrics, "horizontalAdvance", None)
        return advance(text) if advance is not None else metrics.width(text)

    def _clipped(self, root):
        """(objectName, class, text, needed, width) of visible plain-text labels and text buttons under ``root`` whose
        whole text does not fit.
        """
        problems = []
        for widget in root.findChildren("QWidget"):
            if not widget.isVisible() or widget.width <= 1 or widget.height <= 1:
                continue
            className = widget.metaObject().className()
            if className in CLIP_EXEMPT_CLASSES or widget.inherits("QAbstractSpinBox") or widget.inherits("QLineEdit"):
                continue
            if widget.inherits("QLabel"):
                text = str(widget.text or "")
                if not text.strip() or widget.wordWrap or "\n" in text or text.endswith("…") or text.endswith("..."):
                    continue
                if widget.textFormat == qt.Qt.RichText or (widget.textFormat == qt.Qt.AutoText and qt.Qt.mightBeRichText(text)):
                    continue
                margins = widget.contentsMargins()
                available = widget.width - margins.left() - margins.right() - 2 * max(0, widget.margin) - max(0, widget.indent)
                needed = self._textWidth(widget, text)
                if needed > available + 1:
                    problems.append((widget.objectName, className, text, needed, available))
            elif widget.inherits("QPushButton") or widget.inherits("QToolButton"):
                text = str(widget.text or "").replace("&&", "\0").replace("&", "").replace("\0", "&")
                if not text.strip() or text.endswith("…") or text.endswith("..."):
                    continue
                if widget.inherits("QToolButton") and widget.toolButtonStyle == qt.Qt.ToolButtonIconOnly:
                    continue
                if widget.sizePolicy.horizontalPolicy() == qt.QSizePolicy.Ignored:
                    continue
                needed = widget.sizeHint.width()
                # The size hint has the style's padding: allow 4 px of it to be eaten before the text is cut
                if needed > widget.width + 4:
                    problems.append((widget.objectName, className, text, needed, widget.width))
        return problems

    #
    # (a) tokens
    #

    def test_tokens(self):
        from StudioLib import ui

        for theme in ("dark", "light"):
            self._setTheme(theme)
            self.assertEqual(ui.isDark(), theme == "dark", theme)
            style = slicer.app.style()
            styleToken = getattr(style, "token", None)
            fallback = ui._DARK_TOKENS if theme == "dark" else ui._LIGHT_TOKENS
            if styleToken is not None:
                for name, value in fallback.items():
                    self.assertEqual(styleToken(name).name(), qt.QColor(value).name(), f"{theme} {name}: qSlicerStyle and ui.py differ")

            def color(name):
                return qt.QColor("#ffffff") if name == "white" else ui.token(name)

            for foreground, backgrounds, minimum in CONTRAST_RULES:
                for background in backgrounds:
                    ratio = ui.contrastRatio(color(foreground), color(background))
                    self.assertGreaterEqual(ratio, minimum, f"{theme}: {foreground} on {background} is {ratio:.2f}:1 (< {minimum})")
            # The toast pill (dark in both themes) and its icons
            from StudioLib import hud

            background, text, _border = hud.toastColors()
            self.assertGreaterEqual(ui.contrastRatio(text, background), 4.5, theme)
            for _name, iconColor in list(hud.TOAST_ICONS.values()) + [hud.TOAST_DEFAULT_ICON]:
                self.assertGreaterEqual(ui.contrastRatio(qt.QColor(iconColor), background), 3.0, f"{theme} toast icon {iconColor}")

    #
    # (b) cards and constants
    #

    def test_cards(self):
        from StudioLib import ui

        self.assertEqual(ui.RADIUS, {"key": 5, "control": 7, "tile": 9, "card": 10, "cardLow": 12, "dialog": 14})
        self.assertEqual((ui.INSPECTOR_PADDING, ui.CARD_PADDING, ui.GROUP_SPACING), (16, 14, 20))
        self.assertEqual((ui.ROW_HEIGHT, ui.LIST_ROW_HEIGHT, ui.SETTINGS_ROW_HEIGHT), (32, 34, 44))
        for theme in ("dark", "light"):
            self._setTheme(theme)
            host = qt.QWidget()
            host.setAttribute(qt.Qt.WA_DontShowOnScreen, True)
            host.setAutoFillBackground(True)
            hostPalette = host.palette
            hostPalette.setColor(qt.QPalette.Window, ui.token("canvas"))
            host.setPalette(hostPalette)
            layout = qt.QVBoxLayout(host)
            layout.setContentsMargins(10, 10, 10, 10)
            frame, cardLayout = ui.card("Lesion")
            cardLayout.addWidget(qt.QLabel("Body"))
            layout.addWidget(frame)
            host.resize(260, 140)
            host.show()
            self._pump()
            try:
                self.assertEqual(frame.property("slicerRole"), "card")
                margins = cardLayout.contentsMargins()
                self.assertEqual((margins.left(), margins.right(), margins.bottom()), (ui.CARD_PADDING,) * 3)
                title = frame.findChild("QLabel", "StudioCardTitle")
                self.assertIsNotNone(title)
                self.assertEqual(title.text, "Lesion", "sentence case, not upper case")
                self.assertNotEqual(title.font.capitalization(), qt.QFont.AllUppercase)
                image = frame.grab().toImage()
                corner = qt.QColor(image.pixel(0, 0))
                inside = qt.QColor(image.pixel(frame.width // 2, frame.height - 4))
                card = ui.token("card")
                self.assertLessEqual(abs(inside.red() - card.red()) + abs(inside.green() - card.green()) + abs(inside.blue() - card.blue()), 6,
                                     f"{theme}: card fill {inside.name()} != {card.name()}")
                self.assertNotEqual(corner.name(), inside.name(), f"{theme}: the card corner is not rounded")
            finally:
                host.hide()
                host.deleteLater()
                self._pump()

    #
    # (b2) role text on a transparent background
    #

    def _roleTextColor(self, host, label):
        """The text color painted by the label (its rectangle in a grab of the host): the pixel farthest from the most common one."""
        from collections import Counter

        host.show()
        self._pump()
        image = host.grab().toImage()
        rect = label.geometry
        pixels = []
        for y in range(rect.top(), rect.bottom() + 1):
            for x in range(rect.left(), rect.right() + 1):
                color = qt.QColor(image.pixel(x, y))
                pixels.append((color.red(), color.green(), color.blue()))
        background = Counter(pixels).most_common(1)[0][0]
        foreground = max(pixels, key=lambda p: sum(abs(p[i] - background[i]) for i in range(3)))
        return qt.QColor(*foreground), qt.QColor(*background)

    def test_role_text_on_transparent_background(self):
        """A stylesheet "background: transparent" (a parent panel's QLabel rule) must not make the light theme paint role
        text with the dark tokens: the transparent window brush is black, which isDark() read as a dark palette.
        """
        from StudioLib import ui

        for theme in ("dark", "light"):
            self._setTheme(theme)
            for role, token in (("caption", "text3"), ("secondaryText", "text2"), ("kbd", "text2")):
                host = qt.QWidget()
                host.setAttribute(qt.Qt.WA_DontShowOnScreen, True)
                host.setAutoFillBackground(True)
                hostPalette = host.palette
                hostPalette.setColor(qt.QPalette.Window, ui.token("card"))
                host.setPalette(hostPalette)
                layout = qt.QVBoxLayout(host)
                label = qt.QLabel("Volume")
                label.setProperty("slicerRole", role)
                label.setStyleSheet("QLabel { background: transparent; }")
                layout.addWidget(label)
                try:
                    painted, background = self._roleTextColor(host, label)
                    self.assertEqual(label.palette.color(qt.QPalette.Window).alpha(), 0, "the label has a transparent window brush")
                    ratio = ui.contrastRatio(painted, background)
                    self.assertGreaterEqual(ratio, 4.5, f"{theme} {role}: text {painted.name()} on {background.name()} is {ratio:.2f}:1")
                    # The antialiased edge pixels are blended: the darkest/lightest pixel is within a few levels of the token
                    expected = ui.token(token)
                    self.assertLessEqual(abs(painted.red() - expected.red()) + abs(painted.green() - expected.green()) + abs(painted.blue() - expected.blue()), 60,
                                         f"{theme} {role}: painted {painted.name()}, token {token} is {expected.name()}")
                finally:
                    host.hide()
                    host.deleteLater()
                    self._pump()
        # A dark panel in the light theme: the closest opaque ancestor decides, so the dark tokens are right there
        self._setTheme("light")
        panel = qt.QWidget()
        panel.setAttribute(qt.Qt.WA_DontShowOnScreen, True)
        panel.setAutoFillBackground(True)
        panelPalette = panel.palette
        panelPalette.setColor(qt.QPalette.Window, qt.QColor("#18181b"))
        panel.setPalette(panelPalette)
        layout = qt.QVBoxLayout(panel)
        label = qt.QLabel("Volume")
        label.setProperty("slicerRole", "caption")
        label.setStyleSheet("QLabel { background: transparent; }")
        layout.addWidget(label)
        try:
            painted, background = self._roleTextColor(panel, label)
            self.assertGreaterEqual(ui.contrastRatio(painted, background), 4.5, f"dark panel in the light theme: {painted.name()} on {background.name()}")
        finally:
            panel.hide()
            panel.deleteLater()
            self._pump()

    #
    # (c) switches
    #

    def test_switch(self):
        from StudioLib import ui

        for theme in ("dark", "light"):
            self._setTheme(theme)
            host = qt.QWidget()
            host.setAttribute(qt.Qt.WA_DontShowOnScreen, True)
            layout = qt.QVBoxLayout(host)
            switch = ui.switch("Check for updates", "StudioVisualSwitch")
            layout.addWidget(switch)
            host.resize(320, 60)
            host.show()
            self._pump()
            try:
                self.assertEqual(switch.property("slicerRole"), "switch")
                self.assertGreaterEqual(switch.height, ui.ROW_HEIGHT)
                option = qt.QStyleOptionButton()
                option.initFrom(switch)
                option.rect = switch.rect
                option.text = switch.text
                style = switch.style()
                indicator = style.subElementRect(qt.QStyle.SE_CheckBoxIndicator, option, switch)
                contents = style.subElementRect(qt.QStyle.SE_CheckBoxContents, option, switch)
                if style.metaObject().className() == "qSlicerStyle" or hasattr(style, "token"):
                    self.assertGreaterEqual(indicator.right(), switch.width - 4, f"{theme}: switch track not at the right edge {indicator}")
                    self.assertLess(contents.left(), indicator.left(), theme)
                    self.assertEqual(indicator.width(), 30, theme)
            finally:
                host.hide()
                host.deleteLater()
                self._pump()

    def test_appearance_switches_centred_on_their_labels(self):
        """Settings > Appearance (VISUAL 4.7): the three switches are as tall as the 44 px label rows, so each one is
        centred on its label; Classic gives the check boxes their natural height back.
        """
        from StudioLib import shell, ui

        self._setTheme("dark")
        self._applyStudio()
        dialog = slicer.app.settingsDialog()
        dialog.setCurrentPanel("Appearance")
        dialog.show()
        self._pump(6)
        try:
            panel = dialog.findChild("QWidget", "qSlicerSettingsStylesPanel")
            self.assertIsNotNone(panel)
            pairs = (("ShowToolTipsLabel", "ShowToolTipsCheckBox"), ("ShowToolButtonTextLabel", "ShowToolButtonTextCheckBox"),
                     ("RestoreUILabel", "RestoreUICheckBox"))
            for labelName, boxName in pairs:
                label = panel.findChild("QLabel", labelName)
                box = panel.findChild("QCheckBox", boxName)
                self.assertEqual(box.property("slicerRole"), "switch", boxName)
                self.assertGreaterEqual(box.minimumHeight, ui.SETTINGS_ROW_HEIGHT, boxName)
                labelCentre = label.mapToGlobal(qt.QPoint(0, label.height // 2)).y()
                boxCentre = box.mapToGlobal(qt.QPoint(0, box.height // 2)).y()
                self.assertLessEqual(abs(labelCentre - boxCentre), 2, f"{boxName}: switch centre {boxCentre}, label centre {labelCentre}")
            # Classic: the natural height again
            live = shell._shell
            live._settingsLook.restore()
            self._pump()
            for _labelName, boxName in pairs:
                self.assertLess(panel.findChild("QCheckBox", boxName).minimumHeight, ui.SETTINGS_ROW_HEIGHT, boxName)
        finally:
            dialog.hide()
            self._pump()

    #
    # (d) text that fits at 1366 x 768
    #

    def test_no_clipped_text_1366(self):
        from StudioLib import shell

        # The check finds a label that is too narrow for its text, and accepts one that fits
        host = qt.QWidget()
        host.setAttribute(qt.Qt.WA_DontShowOnScreen, True)
        hostLayout = qt.QHBoxLayout(host)
        narrow = qt.QLabel("A label much longer than its width")
        narrow.setObjectName("StudioVisualNarrow")
        narrow.setFixedWidth(40)
        hostLayout.addWidget(narrow)
        wide = qt.QLabel("Fits")
        wide.setObjectName("StudioVisualWide")
        hostLayout.addWidget(wide)
        host.show()
        self._pump()
        found = [problem[0] for problem in self._clipped(host)]
        host.hide()
        host.deleteLater()
        self.assertEqual(found, ["StudioVisualNarrow"])

        for theme in ("dark", "light"):
            self._setTheme(theme)
            studioShell = self._applyStudio()
            self.mainWindow.showNormal()
            self.mainWindow.resize(1366, 768)
            self._wait(lambda: self.mainWindow.width <= 1366, 2000)
            self._pump(10)
            problems = {}
            for workspace in ("library", "draw", "map"):
                studioShell.switchTo(workspace)
                self._pump(10)
                for problem in self._clipped(self.mainWindow):
                    problems.setdefault(problem[:3], (workspace,) + problem)
            # With a scan: the Library page, Draw and Map with data
            self._addVolume()
            self._pump(6)
            for workspace in ("library", "draw", "map"):
                studioShell.switchTo(workspace)
                self._pump(10)
                for problem in self._clipped(self.mainWindow):
                    problems.setdefault(problem[:3], (workspace,) + problem)
            studioShell.dispose()
            self.assertIsNone(shell._shell if shell._shell is not studioShell else None)
            slicer.mrmlScene.Clear(0)
            self._pump()
            self.assertEqual(list(problems.values()), [], f"{theme}: clipped text at 1366 x 768 (workspace, objectName, class, text, needed, width)")

    def _paintLesion(self, center, radius):
        """Fill a ball of ``radius`` mm around RAS ``center`` in the lesion of Draw."""
        import vtk

        from StudioLib import draw

        segmentation, source = draw.lesionNodes()
        segmentId = segmentation.GetSegmentation().GetNthSegmentID(0)
        array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentation, segmentId, source)
        matrix = vtk.vtkMatrix4x4()
        source.GetRASToIJKMatrix(matrix)
        ijk = [sum(matrix.GetElement(row, column) * (list(center) + [1.0])[column] for column in range(4)) for row in range(3)]
        k, j, i = np.indices(array.shape)
        spacing = source.GetSpacing()
        distance = ((i - ijk[0]) * spacing[0]) ** 2 + ((j - ijk[1]) * spacing[1]) ** 2 + ((k - ijk[2]) * spacing[2]) ** 2
        array[distance <= radius * radius] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentation, segmentId, source)

    def test_lesion_card_rows_1366(self):
        """In a short inspector the rows of the lesion card do not overlap, also after Draw was left and entered again.

        The module panel keeps a stale height-for-width of a module widget that was shown before (its layout item is
        not deleted when the module is removed), so the inspector content was laid out at its minimum height and the
        word-wrapped rows of the card were squeezed: the stat cells were drawn over the save state.
        """
        from StudioLib import draw, importer

        studioShell = self._applyStudio()
        self.mainWindow.showNormal()
        self.mainWindow.resize(1366, 768)
        self._wait(lambda: self.mainWindow.width <= 1366, 2000)
        importer.startOnTemplate(shell=studioShell)
        self._restores.append(importer._closeTemplateBanner)
        self._pump(10)
        self._paintLesion((-30.0, -10.0, 20.0), 10.0)
        draw.session(studioShell).refreshPanel(computeMetrics=True)
        self._pump(10)
        for workspace in ("draw", "library", "draw"):
            studioShell.switchTo(workspace)
            self._pump(10)
            draw.session(studioShell).refreshPanel(computeMetrics=True)
            self._wait(lambda: False, 300)
            if workspace != "draw":
                continue
            card = self.mainWindow.findChild("QFrame", "StudioLesionCard")
            self.assertIsNotNone(card)
            stats = card.findChild("QFrame", "StudioLesionStats")
            self.assertTrue(stats.isVisible(), "a drawn lesion shows the stat cells")
            self.assertGreaterEqual(card.height, card.heightForWidth(card.width), "the card is squeezed below the height of its rows")
            rows = [child for child in card.children() if child.isWidgetType() and child.isVisible()]
            overlaps = []
            for index, first in enumerate(rows):
                for second in rows[index + 1:]:
                    if first.geometry.intersects(second.geometry):
                        overlaps.append((first.objectName, second.objectName))
            self.assertEqual(overlaps, [], "rows of the lesion card overlap at 1366 x 768")
            # The stat cells stay inside the readout line that holds them
            readout = card.findChild("QLabel", "StudioLesionReadout")
            self.assertLessEqual(stats.geometry.bottom(), readout.height - 1)

    def test_draw_size_row_keeps_its_size_with_two_images_1366(self):
        """With a second image the source chip "on <scan> ▾" does not squeeze the Size control (1 / 3 / 6 / 12).

        The chip shared the row of the segmented control and the row (an Ignored size policy) let the control shrink
        below its size hint: its segments overlapped and "12" was cut. The chip has a row of its own and is elided.
        """
        from StudioLib import draw

        studioShell = self._applyStudio()
        self.mainWindow.showNormal()
        self.mainWindow.resize(1366, 768)
        self._wait(lambda: self.mainWindow.width <= 1366, 2000)
        from StudioLib import importer

        # A scan opened with the importer (it switches to Draw, whose lesion uses it), then a second image
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "sub-0123_ses-baseline_acq-mprage_T1w.nrrd")
            slicer.util.saveNode(self._addVolume("sub-0123_ses-baseline_acq-mprage_T1w"), path)
            slicer.mrmlScene.Clear(0)
            importer.loadFiles([path], studioShell)
            self._pump(8)
        self._wait(lambda: studioShell.currentWorkspace() == "draw", 5000)
        first = draw.lesionNodes()[1]
        self._addVolume("sub-0123_ses-followup_T1w")
        studioShell.switchTo("library")
        self._pump(4)
        studioShell.switchTo("draw")
        self._pump(10)
        draw.session(studioShell).refreshPanel()
        slicer.modules.SegmentEditorWidget.editor.setActiveEffectByName("Paint")
        self._pump(10)
        draw.session(studioShell).refreshPanel()
        self._pump(6)
        sizes = self.mainWindow.findChild("QFrame", "StudioBrushSizes")
        chip = self.mainWindow.findChild("QToolButton", "StudioSourceChip")
        row = self.mainWindow.findChild("QWidget", "StudioDrawChipsRow")
        self.assertIsNotNone(sizes)
        self.assertTrue(sizes.isVisible() and chip.isVisible(), "the size control and the source chip are shown")
        self.assertGreaterEqual(sizes.width, sizes.sizeHint.width(), "the size control is squeezed below its size hint")
        buttons = [button for button in sizes.findChildren("QToolButton") if button.isVisible()]
        self.assertEqual(len(buttons), 4)
        for left, right in itertools.pairwise(buttons):
            self.assertLess(left.geometry.right(), right.geometry.left(), "segments of the size control overlap")
        for button in buttons:
            self.assertGreaterEqual(button.width, button.sizeHint.width() - 1, f"segment {button.text} is narrower than its hint")
        self.assertLessEqual(chip.geometry.bottom(), row.height, "the source chip lies inside the size row")
        self.assertTrue(
            chip.mapTo(row, qt.QPoint(0, 0)).y() >= sizes.mapTo(row, qt.QPoint(0, 0)).y() + sizes.height - 1,
            "the source chip lies under the size control, not beside it",
        )
        self.assertLessEqual(chip.width, row.width, "the source chip is wider than its row")
        self.assertTrue(chip.text.startswith("on ") and chip.text.endswith("▾"))
        self.assertIn(first.GetName(), chip.toolTip, "the tooltip has the full scan name")

    def test_tour_offer_in_canvas_1366(self):
        """The first-start tour offer lies whole inside the Library canvas at 1366 x 768, off the content of the page."""
        from StudioLib import tour

        settings = qt.QSettings()
        saved = {}
        settings.beginGroup("Studio/Tour")
        for key in settings.allKeys():
            saved[key] = settings.value(key)
        settings.endGroup()

        def restoreTourSettings():
            settings.remove("Studio/Tour")
            for key, value in saved.items():
                settings.setValue(f"Studio/Tour/{key}", value)

        self._restores.append(restoreTourSettings)
        studioShell = self._applyStudio()
        self.mainWindow.showNormal()
        self.mainWindow.resize(1366, 768)
        self._wait(lambda: self.mainWindow.width <= 1366, 2000)
        settings.remove("Studio/Tour")
        studioShell.switchTo("library")
        self._pump(10)
        self.assertTrue(tour.offerTour())
        self._wait(lambda: False, 300)
        card = tour.plugin().offerWidget()
        self.assertIsNotNone(card)
        try:
            canvas = card.parentWidget()
            self.assertTrue(canvas.rect.contains(card.geometry), f"the tour offer {card.geometry} is not inside the canvas {canvas.rect}")
            column = canvas.findChild("QWidget", "StudioLibraryColumn")
            if column is not None and column.isVisible():
                covered = [rect for rect in tour._pageContentRects(column, canvas) if rect.intersects(card.geometry)]
                self.assertEqual(covered, [], "the tour offer covers the content of the Library page")
        finally:
            card.findChild("QPushButton", "StudioTourOfferNotNowButton").click()
            self._pump()

    #
    # (e) view bars
    #

    def test_map_panel(self):
        """Map in Studio (V-03): the seed modes are a row of short labels above the steps, still shown after the Seed step
        collapsed and Map was left and entered again; the peaks table names the region; the Classic look restores them.
        """
        studioShell = self._applyStudio()
        studioShell.switchTo("map")
        self._pump(10)
        widget = getattr(slicer.modules, "LesionNetworkMappingWidget", None)
        if widget is None:
            self.skipTest("the Map module is not available")
        frame = widget.seedModeFrame
        row = widget._seedModeRow
        self.assertIsNotNone(row)
        self.assertIs(frame.parentWidget(), row)
        self.assertTrue(frame.isVisible())
        labels = [str(button.text).split(" (")[0] for button in widget.seedModeGroup.buttons()]
        self.assertTrue(set(labels) <= {"Lesion", "Cohort", "Coords", "Stim"}, labels)
        seedCard = widget._cardWidgets["seed"][1]
        # Collapsed while the frame is in it (Classic), then Studio again: the frame is shown in the row
        studioShell.switchTo("library")
        self._pump(6)
        self.assertIs(frame.parentWidget(), seedCard)
        seedCard.collapsed = True
        self._pump(4)
        seedCard.collapsed = False
        seedCard.collapsed = True
        studioShell.switchTo("map")
        self._pump(10)
        self.assertIs(frame.parentWidget(), row)
        self.assertTrue(frame.isVisible())
        self.assertEqual(str(widget.peaksTable.horizontalHeaderItem(0).text()), "Peak region")
        seedCard.collapsed = False

    def test_map_done_steps_collapse_on_entry(self):
        """A step that is already done when the panel takes the Studio look (a connectome remembered from an earlier
        session) collapses to its row then, not only at its next change (V-03 review).
        """
        studioShell = self._applyStudio()
        studioShell.switchTo("map")
        self._pump(10)
        widget = getattr(slicer.modules, "LesionNetworkMappingWidget", None)
        if widget is None:
            self.skipTest("the Map module is not available")
        connectome = widget._cardWidgets["connectome"][1]
        # The Classic look of the panel: the steps are remembered but never collapsed
        widget.setStudioPresentation(False, refresh=False)
        savedBundle = widget.bundle
        savedAccepted = bool(widget.acceptTermsCheckBox.checked)

        class Bundle:
            name = "Test connectome"
            n_subjects = 10

        widget.bundle = Bundle()
        widget.acceptTermsCheckBox.blockSignals(True)
        widget.acceptTermsCheckBox.setChecked(True)
        widget.acceptTermsCheckBox.blockSignals(False)
        try:
            widget._updateSteps()
            self.assertEqual(widget._stepStates["connectome"][0], "done")
            connectome.collapsed = False
            widget.setStudioPresentation(True, refresh=False)
            self.assertTrue(bool(connectome.collapsed))
        finally:
            widget.bundle = savedBundle
            widget.acceptTermsCheckBox.blockSignals(True)
            widget.acceptTermsCheckBox.setChecked(savedAccepted)
            widget.acceptTermsCheckBox.blockSignals(False)
            widget._updateSteps()
            connectome.collapsed = False

    def test_map_overlay_follows_layout(self):
        """The map legend and layer chips hide while a layout hides the main view and come back with it (V-03 review)."""
        from StudioLib import mappage

        studioShell = self._applyStudio()
        studioShell.switchTo("map")
        self._pump(10)
        overlay = mappage.overlay()
        if overlay is None:
            self.skipTest("the Map overlay is not available")
        # A map in the views, as the Map module reports it
        state = {"statistic": "T", "window": 4.0, "level": 0.0, "threshold": None, "thresholdText": "Unthresholded", "sign": "both",
                 "opacity": 0.8, "outline": False}
        refresh = overlay.refresh

        def place():
            overlay._state = state
            overlay.place()

        overlay.refresh = place
        self._restores.append(lambda: setattr(overlay, "refresh", refresh))
        initialLayout = self.layoutManager.layout
        self._restores.append(lambda: self.layoutManager.setLayout(initialLayout))
        self.layoutManager.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
        self._pump(6)
        place()
        self._pump(4)
        self.assertTrue(overlay.legend.isVisible())
        self.layoutManager.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutOneUp3DView)
        self._pump(6)
        self.assertFalse(overlay.legend.isVisible())
        self.assertFalse(overlay.chips.isVisible())
        self.layoutManager.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
        self._pump(6)
        self.assertTrue(self._wait(overlay.legend.isVisible, 2000))
        self.assertTrue(overlay.chips.isVisible())

    def test_map_overlay_follows_one_up_sagittal_coronal(self):
        """The map legend and layer chips follow the only slice view of a one-up Sagittal or Coronal layout, not Red only."""
        from StudioLib import mappage

        studioShell = self._applyStudio()
        studioShell.switchTo("map")
        self._pump(10)
        overlay = mappage.overlay()
        if overlay is None:
            self.skipTest("the Map overlay is not available")
        state = {"statistic": "T", "window": 4.0, "level": 0.0, "threshold": None, "thresholdText": "Unthresholded", "sign": "both",
                 "opacity": 0.8, "outline": False}
        refresh = overlay.refresh

        def place():
            overlay._state = state
            overlay.place()

        overlay.refresh = place
        self._restores.append(lambda: setattr(overlay, "refresh", refresh))
        initialLayout = self.layoutManager.layout
        self._restores.append(lambda: self.layoutManager.setLayout(initialLayout))
        layouts = (("Red", slicer.vtkMRMLLayoutNode.SlicerLayoutOneUpRedSliceView),
                   ("Yellow", slicer.vtkMRMLLayoutNode.SlicerLayoutOneUpYellowSliceView),
                   ("Green", slicer.vtkMRMLLayoutNode.SlicerLayoutOneUpGreenSliceView),
                   ("Red", slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView))
        for name, layout in layouts:
            self.layoutManager.setLayout(layout)
            self._pump(6)
            place()
            self._pump(4)
            self.assertTrue(self.layoutManager.sliceWidget(name).isVisible(), name)
            self.assertTrue(self._wait(overlay.legend.isVisible, 2000), f"legend in the layout of {name}")
            self.assertTrue(overlay.chips.isVisible(), f"chips in the layout of {name}")
            # The legend sits inside the view that is shown
            view = self.layoutManager.sliceWidget(name).sliceView()
            topLeft = overlay.parent.mapFromGlobal(view.mapToGlobal(qt.QPoint(0, 0)))
            self.assertGreaterEqual(overlay.legend.x, topLeft.x())
            self.assertLessEqual(overlay.legend.x + overlay.legend.width, topLeft.x() + view.width)
        # A layout without slice views still hides them
        self.layoutManager.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutOneUp3DView)
        self._pump(6)
        self.assertFalse(overlay.legend.isVisible())
        self.assertFalse(overlay.chips.isVisible())

    def test_flat_view_bar(self):
        studioShell = self._applyStudio()
        volume = self._addVolume()
        slicer.util.setSliceViewerLayers(background=volume, fit=True)
        studioShell.switchTo("draw")
        self._pump(10)
        sliceWidget = self.layoutManager.sliceWidget("Red")
        controller = sliceWidget.sliceController()
        flatBefore = controller.flatStyle
        self._restores.append(lambda: controller.setProperty("flatStyle", flatBefore))
        controller.setProperty("flatStyle", True)
        self._pump(6)
        bar = controller.barWidget()
        self.assertTrue(bool(bar.property("slicerFlatViewBar")))
        self.assertEqual(bar.height, 34)
        dot = slicer.util.findChild(bar, "ViewLabel")
        self.assertEqual((dot.width, dot.height), (8, 8))
        readout = slicer.util.findChild(bar, "SliceIndexLabel")
        self.assertTrue(self._wait(lambda: " / " in str(readout.text), 2000), readout.text)
        if "<span" in str(readout.text):
            # The full readout: the position, then the slice index in the tertiary text color
            self.assertIn("white-space: pre", str(readout.text))
        # The slice index follows the theme: its color is the text3 token of the theme shown, not of the theme the
        # text was built in
        from StudioLib import ui

        # One view: the bar is wide enough for the full readout
        self.layoutManager.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutOneUpRedSliceView)
        self.assertTrue(self._wait(lambda: "<span" in str(readout.text), 2000), readout.text)
        for theme in ("dark", "light", "dark"):
            self._setTheme(theme)
            self._pump(6)
            text = str(readout.text)
            expected = "#ff" + ui.token("text3").name()[1:]
            self.assertIn(f"color: {expected}", text, f"{theme}: {text}")
        # The classic presentation is restored without the flat property
        controller.setProperty("flatStyle", False)
        self._pump(4)
        self.assertFalse(bool(bar.property("slicerFlatViewBar")))
        self.assertNotEqual(bar.height, 34)

    def _bottomLeftCornerText(self, sliceViewName="Red"):
        """The bottom-left corner text of a slice view once the pending annotation updates are done."""
        sliceAnnotations = slicer.modules.DataProbeInstance.infoWidget.sliceAnnotations
        self._pump(4)
        sliceAnnotations.updatePendingViewAnnotations()
        return str(self.layoutManager.sliceWidget(sliceViewName).sliceView().cornerAnnotation().GetText(0))

    def test_flat_view_corner_text_classic(self):
        """Classic keeps the corner text of official 3D Slicer on flat view bars (D13): "L:", "F:" with the opacity and
        "B:" lines. Studio names only the image under the layers (VISUAL 4.3), and switching back to Classic brings the
        official lines back without another change of the view.
        """
        dataProbe = getattr(slicer.modules, "DataProbeInstance", None)
        if dataProbe is None or dataProbe.infoWidget is None:
            self.skipTest("the Data Probe is not shown")
        sliceAnnotations = dataProbe.infoWidget.sliceAnnotations
        if not sliceAnnotations.sliceViewAnnotationsEnabled or not sliceAnnotations.bottomLeft:
            self.skipTest("the bottom-left slice view annotations are turned off in this profile")
        self.layoutManager.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutOneUpRedSliceView)
        controller = self.layoutManager.sliceWidget("Red").sliceController()
        flatBefore = controller.flatStyle
        self._restores.append(lambda: controller.setProperty("flatStyle", flatBefore))
        controller.setProperty("flatStyle", True)
        background = self._addVolume("MR_head")
        foreground = self._addVolume("Overlay")
        label = slicer.modules.volumes.logic().CreateAndAddLabelVolume(slicer.mrmlScene, background, "Labels")
        slicer.util.setSliceViewerLayers(background=background, foreground=foreground, foregroundOpacity=0.4, label=label, fit=True)
        official = f"L: {label.GetName()} (100%)\nF: Overlay (40%)\nB: MR_head\n"

        # Classic with a flat view bar: the text of official 3D Slicer
        self.assertTrue(controller.flatStyle)
        self.assertEqual(self._bottomLeftCornerText(), official)

        # Studio: the readable name of the image alone
        studioShell = self._applyStudio()
        controller.setProperty("flatStyle", True)
        self._pump(4)
        self.assertEqual(self._bottomLeftCornerText(), "MR head\n")

        # Back to Classic (Ctrl+Alt+0): the official lines again, with no other change of the view
        studioShell.classic(persist=False)
        self.assertTrue(controller.flatStyle)
        self.assertEqual(self._bottomLeftCornerText(), official)

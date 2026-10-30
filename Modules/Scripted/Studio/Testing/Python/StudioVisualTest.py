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

    def test_draw_tool_tile_icons_all_vendored(self):
        """Every icon of draw.EFFECT_APPEARANCE is a vendored Lucide file (a missing one, formerly spline.svg of Auto outline,
        makes effectAppearance() fall back to the effect's own coloured raster icon).
        """
        from StudioLib import draw, ui

        missing = [f"{name}: {icon}" for name, (_text, icon) in draw.EFFECT_APPEARANCE.items() if not os.path.isfile(ui.iconPath(icon))]
        self.assertEqual(missing, [], "Lucide icons of the Draw tool tiles are missing")

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
                margins = ui.layoutContentsMargins(cardLayout)
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

    def test_lesion_stat_caption_is_elided_when_cut(self):
        """A stat caption that does not fit its cell ("Side · scanner space") ends with an ellipsis (VISUAL 6: no clipped
        text); the whole caption is its tool tip, and it shows whole again in a wide cell.
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
        card = draw.lesionCard()
        card._setStats({"ml": 1.2, "parts": 1, "gaps": 0, "side": None, "sideNote": "scanner space", "sideLevel": "warn"})
        caption = card.statValues["side"][1]
        whole = "Side · scanner space"
        self.assertEqual(str(caption.property("studioFullText")), whole)
        card.statsFrame.setFixedWidth(600)
        self._pump(6)
        self.assertEqual(str(caption.text), whole)
        card.statsFrame.setFixedWidth(180)
        self._pump(6)
        shown = str(caption.text)
        self.assertTrue(shown.endswith("…"), f"the cut caption has no ellipsis: {shown!r}")
        self.assertLessEqual(self._textWidth(caption, shown), caption.width)
        self.assertIn(whole, str(caption.toolTip))
        card.statsFrame.setMaximumWidth(16777215)
        card.statsFrame.setMinimumWidth(0)
        self._pump(4)
        draw.session(studioShell).refreshPanel(computeMetrics=True)

    def test_lesion_name_is_elided_when_long(self):
        """A lesion name that does not fit the name field of the Lesion card ends with an ellipsis (VISUAL 6: no clipped text);
        it never starts in a half letter after a rename, the whole name is the tool tip, and it is whole while it is edited.
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
        card = draw.lesionCard()
        edit = card.nameEdit
        self.assertTrue(edit.isVisible())
        long = "Left MCA infarct sub-patient042 acute"
        edit.beginEdit()
        edit.setText(long)
        edit.editingFinished()
        self._pump(4)
        draw.session(studioShell).refreshPanel(computeMetrics=True)
        self._pump(6)
        edit.endEdit()
        self.assertEqual(draw.lesionNodes()[0].GetName(), long, "the rename was not committed")
        self.assertEqual(edit.name(), long, "the card does not show the renamed lesion")
        shown = str(edit.text)
        self.assertTrue(shown.endswith("…"), f"the long name has no ellipsis: {shown!r}")
        self.assertTrue(shown.startswith("Left MCA"), f"the name does not start at its beginning: {shown!r}")
        self.assertLessEqual(self._textWidth(edit, shown), edit.width - edit.TEXT_MARGINS)
        self.assertEqual(edit.cursorPosition, 0)
        self.assertIn(long, str(edit.toolTip))
        # A short name shows whole, with the usual tool tip
        edit.setName("Lesion")
        self.assertEqual(str(edit.text), "Lesion")
        # Edited: the whole name
        edit.setName(long)
        edit.beginEdit()
        self.assertEqual(str(edit.text), long)
        edit.endEdit()
        self.assertTrue(str(edit.text).endswith("…"))
        # With several lesions the switcher button elides the name the same way
        card.refresh({"name": long, "lesions": [("a", long, True), ("b", "Second", True)]})
        self._pump(6)
        self.assertTrue(card.switcher.isVisible())
        buttonText = str(card.switcher.text)
        self.assertTrue(buttonText.endswith("… ▾"), f"the switcher shows the name clipped: {buttonText!r}")
        self.assertIn(long, str(card.switcher.toolTip))
        card.refresh({"name": "Lesion", "lesions": []})
        draw.session(studioShell).refreshPanel(computeMetrics=True)

    def test_draw_inspector_padding(self):
        """The Lesion card, the section label and the tool tiles of Draw sit on one 16 px inspector padding (VISUAL 2.7, 4.4).

        The card ran from edge to edge of the inspector and the tiles sat in a square 1 px box (the frame of the tool grid)
        that started 9 px from the edge: three left edges in one column. The classic presentation keeps the frame.
        """
        from StudioLib import draw, importer, ui

        studioShell = self._applyStudio()
        self.mainWindow.showNormal()
        self.mainWindow.resize(1366, 768)
        self._wait(lambda: self.mainWindow.width <= 1366, 2000)
        importer.startOnTemplate(shell=studioShell)
        self._restores.append(importer._closeTemplateBanner)
        self._pump(10)
        editor = slicer.modules.SegmentEditorWidget.editor
        scrollArea = self.mainWindow.findChild("QDockWidget", "PanelDockWidget").findChild("QScrollArea", "ScrollArea")
        viewport = scrollArea.viewport()
        padding = ui.INSPECTOR_PADDING
        for theme in ("dark", "light"):
            self._setTheme(theme)
            self._pump(6)
            left = viewport.mapToGlobal(qt.QPoint(0, 0)).x()
            right = left + viewport.width

            def edges(widget, left=left, right=right):
                x = widget.mapToGlobal(qt.QPoint(0, 0)).x()
                return x - left, right - (x + widget.width)

            card = draw.lesionCard().widget
            label = self.mainWindow.findChild("QLabel", "StudioDrawToolsLabel")
            frame = editor.findChild("QFrame", "EffectsGroupBox")
            tiles = [button for button in frame.findChildren("QToolButton") if button.isVisible() and button.property("slicerRole") == "toolTile"]
            self.assertGreaterEqual(len(tiles), 4, theme)
            self.assertEqual(edges(card), (padding, padding), f"{theme}: the Lesion card is not inset by the inspector padding")
            self.assertEqual(edges(label)[0], padding, theme)
            self.assertEqual(min(edges(tile)[0] for tile in tiles), padding, f"{theme}: the tool tiles are not on the left edge of the cards")
            self.assertEqual(min(edges(tile)[1] for tile in tiles), padding, f"{theme}: the tool tiles do not end at the right padding")
            self.assertEqual(frame.frameShape, qt.QFrame.NoFrame, f"{theme}: the tool tiles sit in a bordered box")
        studioShell.classic(persist=False)
        self._pump()
        self.assertEqual(frame.frameShape, qt.QFrame.StyledPanel, "the classic presentation lost the frame of the tool grid")

    def test_tile_label_text_wraps_at_a_space(self):
        """``SegmentEditor.tileLabelText()`` keeps a label that fits, breaks one that does not at a space (the shortest wider
        line) and shortens at its end a word that is too wide, never in the middle of a word ("Aut...ine").
        """
        import SegmentEditor

        font = qt.QFont()
        font.setPixelSize(11)
        metrics = qt.QFontMetrics(font)
        wide = metrics.horizontalAdvance("Auto outline") + 4
        self.assertEqual(SegmentEditor.tileLabelText("Auto outline", metrics, wide), "Auto outline")
        narrow = metrics.horizontalAdvance("Auto outline") - 10
        self.assertEqual(SegmentEditor.tileLabelText("Auto outline", metrics, narrow), "Auto\noutline")
        self.assertEqual(SegmentEditor.tileLabelText("Mask  volume", metrics, narrow), "Mask\nvolume")
        # The break that makes the wider line shortest
        width = max(metrics.horizontalAdvance("Go go go"), metrics.horizontalAdvance("gogogogogo")) + SegmentEditor.TILE_TEXT_MARGIN
        self.assertEqual(SegmentEditor.tileLabelText("Go go go gogogogogo", metrics, width), "Go go go\ngogogogogo")
        # One long word is shortened at its end, a short one stays whole
        self.assertEqual(SegmentEditor.tileLabelText("Brush", metrics, 100), "Brush")
        cut = SegmentEditor.tileLabelText("Extraordinarily", metrics, 40)
        self.assertTrue(cut.endswith("\u2026") and cut.startswith("Extra") and metrics.horizontalAdvance(cut) <= 40 - SegmentEditor.TILE_TEXT_MARGIN, cut)

    def test_draw_more_tools_tile_labels_not_elided_1366(self):
        """The tiles of Draw > More tools show their names whole: "Auto outline", "Grow seeds" and "Mask volume" are wrapped at the
        space, "Threshold" fits a 56 px tile; the style used to elide them in the middle of the word ("Aut...ine", "Thr...old",
        "Gro...eds", "Ma...me"). The tool grid has equal columns and the tiles keep their 60 px height.
        """
        import SegmentEditor
        from StudioLib import draw, importer

        studioShell = self._applyStudio()
        self.mainWindow.showNormal()
        self.mainWindow.resize(1366, 768)
        self._wait(lambda: self.mainWindow.width <= 1366, 2000)
        importer.startOnTemplate(shell=studioShell)
        self._restores.append(importer._closeTemplateBanner)
        self._pump(10)
        widget = slicer.modules.SegmentEditorWidget
        self.assertEqual(widget._appliedPresentation, "studio")
        widget.setMoreToolsShown(True)
        self._pump(6)
        tiles = [button for button in widget._effectButtons() if button.isVisibleTo(widget.editor)]
        self.assertGreaterEqual(len(tiles), 10, "More tools shows every tool")
        widths = set()
        for tile in tiles:
            effect = widget._buttonEffect(tile)
            title = draw.EFFECT_APPEARANCE.get(effect.name, (effect.title,))[0]
            text = str(tile.text)
            self.assertNotIn("\u2026", text, f"{title}: shortened")
            self.assertEqual(" ".join(text.split()), title, f"{title}: not the tool name, or broken inside a word")
            metrics = qt.QFontMetrics(tile.font)
            for line in text.split("\n"):
                self.assertLessEqual(metrics.horizontalAdvance(line) + SegmentEditor.TILE_TEXT_MARGIN, tile.width, f"{title}: {line!r} is wider than its {tile.width} px tile")
            self.assertEqual(tile.height, SegmentEditor.TILE_MINIMUM_SIZE[1], f"{title}: the tile is not 60 px tall")
            self.assertEqual(str(tile.accessibleName), title)
            widths.add(tile.width)
        self.assertLessEqual(max(widths) - min(widths), 1, f"the tile columns differ: {sorted(widths)}")
        self.assertEqual(next(str(tile.text) for tile in tiles if widget._buttonEffect(tile).name == "Level tracing"), "Auto\noutline")
        # Back to the classic presentation: plain titles, no column stretch
        studioShell.classic(persist=False)
        self._pump(4)
        classicTexts = [str(button.text) for button in widget._effectButtons(allButtons=True) if widget._buttonEffect(button) is not None]
        self.assertNotIn("Auto\noutline", classicTexts)
        self.assertTrue(all("\n" not in text for text in classicTexts), classicTexts)

    def test_draw_inspector_padding_with_qt6_layout_margins(self):
        """Qt 6: QLayout declares the ``contentsMargins`` property, so PythonQt gives a QMargins value and not the getter.

        The tile grid inset called ``layout.contentsMargins()``, which raised "'QMargins' object is not callable" in the
        middle of the studio presentation: Draw showed the classic Segment Editor (no Lesion card, no Save lesion, two
        tile columns). Here the module layout behaves as with Qt 6 and Draw still gets the Studio presentation.
        """
        import SegmentEditor
        from StudioLib import draw, importer

        class Qt6Layout:
            """A QLayout as PythonQt wraps it with Qt 6: ``contentsMargins`` is the property value."""

            def __init__(self, layout):
                object.__setattr__(self, "_layout", layout)

            def __getattr__(self, name):
                if name == "contentsMargins":
                    return self._layout.contentsMargins()
                return getattr(self._layout, name)

        class Qt6LayoutOwner:
            """A widget whose ``layout()`` is a ``Qt6Layout``."""

            def __init__(self, widget):
                object.__setattr__(self, "_widget", widget)

            def __getattr__(self, name):
                if name == "layout":
                    return lambda: Qt6Layout(self._widget.layout())
                return getattr(self._widget, name)

        widget = slicer.modules.segmenteditor.widgetRepresentation().self()
        realLayout = widget.layout
        widget.layout = Qt6Layout(realLayout)

        def restoreLayout():
            widget.layout = realLayout

        self._restores.append(restoreLayout)
        studioShell = self._applyStudio()
        self.mainWindow.showNormal()
        self.mainWindow.resize(1366, 768)
        self._wait(lambda: self.mainWindow.width <= 1366, 2000)
        importer.startOnTemplate(shell=studioShell)
        self._restores.append(importer._closeTemplateBanner)
        self._pump(10)
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        self.assertEqual(widget._appliedPresentation, "studio", "Draw shows the classic Segment Editor")
        readout = self.mainWindow.findChild("QLabel", "StudioLesionReadout")
        self.assertTrue(readout is not None and readout.isVisible(), "the Lesion card is not shown")
        saveButtons = [button for button in self.mainWindow.findChildren("QPushButton") if "Save lesion" in str(button.text) and button.isVisible()]
        self.assertTrue(saveButtons, "no Save lesion button")
        self.assertEqual(widget.editor.effectColumnCount, widget.studioColumnCount(widget.parent.width))

        # Every layout of the inset (module, editor, tile grid) with the Qt 6 behaviour
        editor = widget.editor
        frame = editor.findChild("QFrame", "EffectsGroupBox")
        grid = frame.layout()
        grid.setContentsMargins(0, 0, 0, 0)
        realChild = widget._child
        widget.editor = Qt6LayoutOwner(editor)
        widget._child = lambda className, name: Qt6LayoutOwner(frame) if name == "EffectsGroupBox" else realChild(className, name)
        try:
            widget._updateTileGridInset()
        finally:
            widget.editor = editor
            del widget._child
        outer = realLayout.contentsMargins()
        inner = editor.layout().contentsMargins()
        margins = grid.contentsMargins()
        padding = SegmentEditor.STUDIO_INSPECTOR_PADDING
        expected = (max(0, padding - outer.left() - inner.left()), 0, max(0, padding - outer.right() - inner.right()), 0)
        self.assertEqual((margins.left(), margins.top(), margins.right(), margins.bottom()), expected)
        self.assertIsNotNone(draw.lesionCard())

        # The helper reads the margins with Qt 5 (method) and Qt 6 (property value)
        layout = qt.QVBoxLayout()
        layout.setContentsMargins(3, 4, 5, 6)
        for candidate in (layout, Qt6Layout(layout)):
            margins = SegmentEditor._layoutContentsMargins(candidate)
            self.assertEqual((margins.left(), margins.top(), margins.right(), margins.bottom()), (3, 4, 5, 6))

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

    def test_map_legend_threshold_text_never_clips(self):
        """A long threshold text in the 220 px map legend is elided with an ellipsis (rule name first, the value stays) and
        the label's tooltip holds the whole text; a text that fits the label is shown whole, with no tooltip, even when it
        fits by a pixel (the default FWE rule); the text is fitted again when the label's width changes.
        """
        from StudioLib import mappage

        studioShell = self._applyStudio()
        studioShell.switchTo("map")
        self._pump(10)
        overlay = mappage.overlay()
        if overlay is None:
            self.skipTest("the Map overlay is not available")
        state = {"statistic": "T", "window": 4.0, "level": 0.0, "threshold": None, "thresholdText": "", "sign": "both", "opacity": 0.8, "outline": False}
        label = overlay.thresholdLabel

        def show(statistic, text):
            state.update({"statistic": statistic, "thresholdText": text})
            overlay.statisticLabel.setText(statistic)
            overlay._showThreshold(text)
            overlay.legend.layout().activate()
            self._pump(3)
            return str(label.text)

        def fitsLabel(text):
            return mappage._textWidth(label.font, text) <= label.width

        overlay._state = state
        overlay.place()
        self.assertTrue(self._wait(overlay.legend.isVisible, 2000))
        short = "FWE 5% · T ≥ 4.20"
        self.assertEqual(show("T", short), short)
        self.assertEqual(str(label.toolTip), "")
        # The default rule's text fits the label by a pixel or so: it must not be elided (it was, with 12 px of slack)
        fwe = "FWE (Bonferroni) · |T| ≥ 4.67"
        seen = {}
        for statistic, text, value in (("T", fwe, "|T| ≥ 4.67"),
                                       ("T", "Top x% (N-independent) · T ≥ 2.28", "T ≥ 2.28"),
                                       ("T", "|T| ≥ 7 convention (N≈1000) · |T| ≥ 7.00", "|T| ≥ 7.00"),
                                       ("Fz", "Top x% (N-independent) · |Fz| ≥ 0.0304", "|Fz| ≥ 0.0304")):
            shown = show(statistic, text)
            seen[text] = fits = fitsLabel(text)
            self.assertTrue(shown.endswith(value), shown)
            self.assertTrue(fitsLabel(shown), (shown, label.width))
            self.assertLessEqual(label.x + label.width, overlay.legend.width)
            if fits:
                # Shown whole, no ellipsis, no tooltip: a text that fits is never shortened
                self.assertEqual(shown, text)
                self.assertEqual(str(label.toolTip), "")
            else:
                self.assertIn("…", shown)
                self.assertIn(text, str(label.toolTip))
        # The long texts never fit a 220 px legend, so the elision branch was exercised
        self.assertFalse(all(seen.values()), seen)
        # In any font: a text that fits the label by less than one narrow glyph is shown whole (no slack, and the card's
        # 1 px border counted)
        show("T", short)
        tight = fwe
        while True:
            candidate = tight.replace(")", "i)", 1)
            if not fitsLabel(candidate):
                break
            tight = candidate
        self.assertEqual(show("T", tight), tight)
        self.assertEqual(str(label.toolTip), "")
        # A wider statistic shrinks the label: the text is fitted again to the new width without a new threshold text
        text = "|T| ≥ 7 convention (N≈1000) · |T| ≥ 7.00"
        shown = show("T", text)
        widthBefore = label.width
        overlay.statisticLabel.setText("|Fz|")
        overlay.legend.layout().activate()
        self._pump(3)
        self.assertLess(label.width, widthBefore)
        refitted = str(label.text)
        self.assertNotEqual(refitted, shown)
        self.assertTrue(refitted.endswith("|T| ≥ 7.00"), refitted)
        self.assertTrue(fitsLabel(refitted), (refitted, label.width))
        self.assertIn(text, str(label.toolTip))

    def test_closed_combo_text_is_elided_with_an_ellipsis(self):
        """The current text of a closed combo box that does not fit is drawn elided ("…"), not clipped in the middle of a word
        (VISUAL.md section 6): the combo looks the same as one that holds a shorter text ending in an "…" that is visible. The
        base style clips, so for it no such text looks the same.
        """
        long = "Quick look: lesion network overlap (Boes 2015) · adapted from a long study title"
        width = 170
        holder = qt.QWidget()
        holder.resize(300, 60)
        holder.show()

        def look(text, style=None):
            combo = qt.QComboBox(holder)
            combo.addItem(text)
            if style is not None:
                combo.setStyle(style)
            combo.setGeometry(4, 4, width, combo.sizeHint.height())
            combo.show()
            self._pump(3)
            image = combo.grab().toImage()
            combo.hide()
            combo.setParent(None)
            return image

        try:
            metrics = qt.QFontMetrics(qt.QComboBox().font)
            self.assertGreater(metrics.horizontalAdvance(long), width, "the text must not fit the combo")
            # Texts whose "…" is inside the field (24 px are the arrow and the padding)
            visible = sorted({metrics.elidedText(long, qt.Qt.ElideRight, room) for room in range(40, width - 24)})
            self.assertTrue(all(form.endswith("…") for form in visible))
            shown = look(long)
            self.assertTrue(any(shown == look(form) for form in visible), "a combo text that does not fit is drawn elided with an ellipsis")
            clipped = look(long, qt.QStyleFactory.create("Fusion"))
            self.assertFalse(any(clipped == look(form, qt.QStyleFactory.create("Fusion")) for form in visible),
                             "the base style clips the text (the comparison sees the difference)")
        finally:
            holder.deleteLater()

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

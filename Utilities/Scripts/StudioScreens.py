r"""Slicer Studio screenshot harness: the visual before/after evidence of every work package.

Run inside Slicer (arguments after ``--``)::

  xvfb-run -a -s "-screen 0 1920x1080x24" ./Slicer --no-splash --python-script Utilities/Scripts/StudioScreens.py -- \
    --out DIR [--sizes 1920x1080,1366x768] [--themes dark,light] [--mrhead PATH]

For each window size and theme it writes ``DIR/<W>x<H>-<theme>/``:

- ``home.png``: the main window as it is after launch (taken before any data is loaded);
- ``segmenteditor.png``: MRHead loaded, Segment Editor selected, a segment added and Paint active;
- ``studio_library.png``, ``studio_draw.png``, ``studio_map.png``: the Studio workspaces, when the
  Studio shell is active (``StudioLib.shell.instance()``) and the workspace is available;
- ``gallery.png``: a dialog with the common widgets (buttons, checked tool buttons, collapsible
  buttons, sliders, spin boxes, combo box, focused line edit, table, scroll bar, progress bar, tabs,
  check box and radio buttons).

Themes are applied at runtime through the "Dark Slicer" / "Light Slicer" styles; no setting is
written. Use a throwaway settings directory (``HOME=$(mktemp -d)``) for first-run screenshots.
All paths are printed and listed in ``DIR/manifest.json``. Exits with 1 if any screenshot failed.

``--set studio`` takes the Slicer Studio surfaces instead (V-01), one size and one theme per process, with a
throwaway settings directory (it answers the first-start folder question and writes projects under ``$HOME``)::

  HOME=$(mktemp -d) xvfb-run -a -s "-screen 0 1366x768x24" ./Slicer --no-splash --python-script \
    Utilities/Scripts/StudioScreens.py -- --set studio --out DIR --sizes 1366x768 --themes light

It writes ``DIR/<W>x<H>-<theme>/<NN>-<name>.png`` (screen grabs, so popups and overlays are included): the
first-start folder chooser, Library (empty, with a project, with projects, with a study), the tour card, Draw (empty,
painting with the lesion card, in a study, a case of a study), a toast, Map (cards, a result, a cohort), Search, the cheat sheet, the settings
panels (Saving, Appearance, Updates, Templates) and the add-template dialog; V-02 adds All projects as a list, the project menu of
the top bar and Library with a study listed in Studies. The theme is applied with the Studio
theme setting (``Appearance/Theme``) of that throwaway settings directory.
"""

import argparse
import json
import os
import sys
import traceback

import ctk
import qt

import slicer

DEFAULT_MRHEAD_PATH = "/tmp/claude-0/understand/data/MRHead.nrrd"
STYLE_NAMES = {"dark": "Dark Slicer", "light": "Light Slicer"}


def pump(count=5):
    for _ in range(count):
        slicer.app.processEvents()


def idleWait(msec):
    """Run the event loop for msec (lets deferred work and scheduled renders happen)."""
    loop = qt.QEventLoop()
    qt.QTimer.singleShot(msec, loop.quit)
    loop.exec_()


def applyTheme(theme):
    """Switch the application style and palette at runtime, without writing any setting."""
    application = slicer.app
    style = qt.QStyleFactory.create(STYLE_NAMES[theme])
    if style is None:
        raise RuntimeError(f"style '{STYLE_NAMES[theme]}' is not available")
    application.removeEventFilter(application.style())
    application.setStyle(style)
    application.installEventFilter(application.style())
    application.setPalette(style.standardPalette())
    pump()


def setWindowSize(mainWindow, width, height):
    mainWindow.showNormal()
    mainWindow.resize(width, height)
    pump()
    idleWait(300)


def studioShell():
    try:
        from StudioLib import shell
    except ImportError:
        return None
    try:
        return shell.instance()
    except Exception:
        return None


def buildGallery(parent):
    """A dialog with the common widgets, in a known state."""
    dialog = qt.QDialog(parent)
    dialog.setWindowTitle("Widget gallery")
    dialog.resize(900, 720)
    columns = qt.QHBoxLayout(dialog)
    left = qt.QVBoxLayout()
    right = qt.QVBoxLayout()
    columns.addLayout(left, 1)
    columns.addLayout(right, 1)
    style = dialog.style()

    buttons = qt.QHBoxLayout()
    buttons.addWidget(qt.QPushButton("Button"))
    primary = qt.QPushButton("Primary")
    primary.setProperty("slicerRole", "primary")
    buttons.addWidget(primary)
    disabled = qt.QPushButton("Disabled")
    disabled.enabled = False
    buttons.addWidget(disabled)
    left.addLayout(buttons)

    toolButtons = qt.QHBoxLayout()
    for index, (text, icon) in enumerate((("Paint", qt.QStyle.SP_DialogApplyButton), ("Draw", qt.QStyle.SP_FileIcon), ("Erase", qt.QStyle.SP_TrashIcon))):
        toolButton = qt.QToolButton()
        toolButton.setText(text)
        toolButton.setIcon(style.standardIcon(icon))
        toolButton.setToolButtonStyle(qt.Qt.ToolButtonTextUnderIcon)
        toolButton.setCheckable(True)
        toolButton.setChecked(index == 0)
        toolButton.setAutoRaise(True)
        toolButtons.addWidget(toolButton)
    toolButtons.addStretch(1)
    left.addLayout(toolButtons)

    for title, collapsed in (("Expanded section", False), ("Collapsed section", True)):
        collapsible = ctk.ctkCollapsibleButton()
        collapsible.text = title
        sectionLayout = qt.QFormLayout(collapsible)
        sectionLayout.addRow("Option:", qt.QCheckBox("Enabled"))
        sectionLayout.addRow("Mode:", qt.QComboBox())
        collapsible.collapsed = collapsed
        left.addWidget(collapsible)

    form = qt.QFormLayout()
    slider = qt.QSlider(qt.Qt.Horizontal)
    slider.setRange(0, 100)
    slider.setValue(40)
    form.addRow("QSlider:", slider)
    sliderWidget = ctk.ctkSliderWidget()
    sliderWidget.minimum = 0
    sliderWidget.maximum = 100
    sliderWidget.value = 40
    form.addRow("ctkSliderWidget:", sliderWidget)
    rangeWidget = ctk.ctkRangeWidget()
    rangeWidget.minimum = 0
    rangeWidget.maximum = 100
    rangeWidget.minimumValue = 20
    rangeWidget.maximumValue = 70
    form.addRow("ctkRangeWidget:", rangeWidget)
    spinBox = qt.QSpinBox()
    spinBox.setValue(12)
    form.addRow("QSpinBox:", spinBox)
    doubleSpinBox = ctk.ctkDoubleSpinBox()
    doubleSpinBox.value = 3.5
    form.addRow("ctkDoubleSpinBox:", doubleSpinBox)
    comboBox = qt.QComboBox()
    comboBox.addItems(["Axial", "Sagittal", "Coronal"])
    form.addRow("QComboBox:", comboBox)
    lineEdit = qt.QLineEdit("Focused line edit")
    form.addRow("QLineEdit:", lineEdit)
    progressBar = qt.QProgressBar()
    progressBar.setValue(40)
    form.addRow("QProgressBar:", progressBar)
    checkBox = qt.QCheckBox("Check box")
    checkBox.setChecked(True)
    form.addRow("", checkBox)
    radioButtons = qt.QHBoxLayout()
    for text, checked in (("Radio on", True), ("Radio off", False)):
        radioButton = qt.QRadioButton(text)
        radioButton.setChecked(checked)
        radioButtons.addWidget(radioButton)
    form.addRow("", radioButtons)
    left.addLayout(form)
    left.addStretch(1)

    table = qt.QTableWidget(5, 3)
    table.setHorizontalHeaderLabels(["Name", "Value", "Unit"])
    for row in range(5):
        for column, text in enumerate((f"Segment_{row + 1}", f"{(row + 1) * 1.25:.2f}", "mL")):
            table.setItem(row, column, qt.QTableWidgetItem(text))
    table.selectRow(1)
    right.addWidget(table)

    scrollArea = qt.QScrollArea()
    scrollArea.setWidgetResizable(True)
    scrollArea.setWidget(qt.QLabel("\n".join(f"Scrolled line {line + 1}" for line in range(30))))
    scrollArea.setFixedHeight(120)
    right.addWidget(scrollArea)

    tabWidget = qt.QTabWidget()
    for title in ("Display", "Geometry", "Export"):
        page = qt.QWidget()
        pageLayout = qt.QVBoxLayout(page)
        pageLayout.addWidget(qt.QLabel(f"{title} page"))
        tabWidget.addTab(page, title)
    right.addWidget(tabWidget)
    right.addStretch(1)
    return dialog, lineEdit


class Screens:
    def __init__(self, args):
        self.args = args
        self.paths = []
        self.errors = {}
        self.mainWindow = slicer.util.mainWindow()
        self.sizes = [tuple(int(value) for value in size.lower().split("x")) for size in args.sizes.split(",") if size]
        self.themes = [theme.strip().lower() for theme in args.themes.split(",") if theme.strip()]
        for theme in self.themes:
            if theme not in STYLE_NAMES:
                raise ValueError(f"unknown theme '{theme}' (expected one of {', '.join(STYLE_NAMES)})")
        self.mrHeadNode = None

    def directory(self, size, theme):
        path = os.path.join(self.args.out, f"{size[0]}x{size[1]}-{theme}")
        os.makedirs(path, exist_ok=True)
        return path

    def save(self, widget, size, theme, name):
        path = os.path.join(self.directory(size, theme), name + ".png")
        if not widget.grab().save(path):
            raise RuntimeError(f"failed to write {path}")
        self.paths.append(path)
        print(f"SCREEN {path}", flush=True)

    def shot(self, size, theme, name, function):
        try:
            function()
        except Exception:
            key = f"{size[0]}x{size[1]}-{theme}/{name}"
            self.errors[key] = traceback.format_exc()[-3000:]
            print(f"SCREEN {key} FAILED:\n{self.errors[key]}", flush=True)

    def loadMRHead(self):
        if self.args.mrhead and os.path.exists(self.args.mrhead):
            self.mrHeadNode = slicer.util.loadVolume(self.args.mrhead, {"name": "MRHead"})
        else:
            import SampleData
            self.mrHeadNode = SampleData.SampleDataLogic().downloadSample("MRHead")
        slicer.util.setSliceViewerLayers(background=self.mrHeadNode, fit=True)
        pump()

    def segmentEditor(self, size, theme):
        slicer.util.selectModule("SegmentEditor")
        pump()
        editor = slicer.modules.SegmentEditorWidget.editor
        segmentationNode = editor.segmentationNode()
        if segmentationNode is None:
            segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode")
            segmentationNode.CreateDefaultDisplayNodes()
            editor.setSegmentationNode(segmentationNode)
        segmentationNode.SetReferenceImageGeometryParameterFromVolumeNode(self.mrHeadNode)
        editor.setSourceVolumeNode(self.mrHeadNode)
        segmentation = segmentationNode.GetSegmentation()
        if segmentation.GetNumberOfSegments() == 0:
            segmentation.AddEmptySegment()
        editor.setCurrentSegmentID(segmentation.GetNthSegmentID(0))
        editor.setActiveEffectByName("Paint")
        pump()
        idleWait(500)
        try:
            self.save(self.mainWindow, size, theme, "segmenteditor")
        finally:
            editor.setActiveEffectByName("")
            pump()

    def studioWorkspaces(self, size, theme):
        shell = studioShell()
        if shell is None:
            return
        for key in ("library", "draw", "map"):
            def workspace(key=key):
                shell.switchTo(key)
                pump()
                idleWait(500)
                if shell.currentWorkspace() != key:
                    print(f"SCREEN studio_{key} skipped: workspace not available", flush=True)
                    return
                self.save(self.mainWindow, size, theme, f"studio_{key}")
            self.shot(size, theme, f"studio_{key}", workspace)

    def gallery(self, size, theme):
        dialog, lineEdit = buildGallery(self.mainWindow)
        try:
            dialog.show()
            dialog.activateWindow()
            dialog.raise_()
            lineEdit.setFocus()
            pump()
            idleWait(300)
            self.save(dialog, size, theme, "gallery")
        finally:
            dialog.close()
            dialog.deleteLater()
            pump()

    def run(self):
        pump(10)
        idleWait(1000)
        # The state after launch, before any data is loaded or any module is selected
        for size in self.sizes:
            for theme in self.themes:
                def home(size=size, theme=theme):
                    setWindowSize(self.mainWindow, *size)
                    applyTheme(theme)
                    idleWait(500)
                    self.save(self.mainWindow, size, theme, "home")
                self.shot(size, theme, "home", home)
        self.loadMRHead()
        for size in self.sizes:
            for theme in self.themes:
                try:
                    setWindowSize(self.mainWindow, *size)
                    applyTheme(theme)
                except Exception:
                    self.errors[f"{size[0]}x{size[1]}-{theme}"] = traceback.format_exc()[-3000:]
                    continue
                self.shot(size, theme, "segmenteditor", lambda size=size, theme=theme: self.segmentEditor(size, theme))
                self.studioWorkspaces(size, theme)
                self.shot(size, theme, "gallery", lambda size=size, theme=theme: self.gallery(size, theme))
        manifest = {"screenshots": self.paths, "errors": self.errors, "studio": studioShell() is not None,
                    "sizes": [f"{width}x{height}" for width, height in self.sizes], "themes": self.themes}
        with open(os.path.join(self.args.out, "manifest.json"), "w") as f:
            json.dump(manifest, f, indent=1)
        print(f"{len(self.paths)} screenshots in {self.args.out}, {len(self.errors)} errors", flush=True)
        for path in self.paths:
            print(path)
        return 1 if self.errors else 0


def waitFor(condition, timeoutMs=30000, stepMs=50):
    """Run the event loop until ``condition()`` is true (exceptions count as false). Return the last value."""
    import time

    end = time.monotonic() + timeoutMs / 1000.0
    while True:
        try:
            value = condition()
        except Exception:
            value = None
        if value or time.monotonic() > end:
            return value
        idleWait(stepMs)


def visibleTopLevel(objectName):
    for widget in slicer.app.topLevelWidgets():
        if widget.objectName == objectName and widget.isVisible():
            return widget
    return None


def syntheticBlobVolume(path, reference, centers, sign=None, scale=1.0):
    """Write a NIfTI file on the grid of ``reference`` (a volume node): gaussian blobs at RAS ``centers`` [(x, y, z, sigma, peak)]."""
    import numpy as np

    array = slicer.util.arrayFromVolume(reference)
    ijkToRas = vtkMatrix(reference)
    k, j, i = np.indices(array.shape)
    ras = [ijkToRas[row][0] * i + ijkToRas[row][1] * j + ijkToRas[row][2] * k + ijkToRas[row][3] for row in range(3)]
    values = np.zeros(array.shape, np.float32)
    for x, y, z, sigma, peak in centers:
        values += peak * np.exp(-((ras[0] - x) ** 2 + (ras[1] - y) ** 2 + (ras[2] - z) ** 2) / (2.0 * sigma * sigma))
    node = slicer.util.addVolumeFromArray(values * scale, name="StudioScreensBlob")
    node.CopyOrientation(reference)
    slicer.util.saveNode(node, path)
    slicer.mrmlScene.RemoveNode(node)
    return path


def vtkMatrix(volumeNode):
    import vtk

    matrix = vtk.vtkMatrix4x4()
    volumeNode.GetIJKToRASMatrix(matrix)
    return [[matrix.GetElement(row, column) for column in range(4)] for row in range(4)]


class StudioScreens:
    """``--set studio``: the Slicer Studio surfaces of the visual design pass (V-01), one size and theme per process."""

    def __init__(self, args):
        self.args = args
        self.size = tuple(int(value) for value in args.sizes.split(",")[0].lower().split("x"))
        self.theme = args.themes.split(",")[0].strip().lower()
        self.folder = os.path.join(args.out, f"{self.size[0]}x{self.size[1]}-{self.theme}")
        os.makedirs(self.folder, exist_ok=True)
        self.paths = []
        self.errors = {}
        self.count = 0
        self.mainWindow = slicer.util.mainWindow()
        self.home = os.path.expanduser("~")
        self.data = os.path.join(self.home, "StudioScreensData")
        os.makedirs(self.data, exist_ok=True)

    def shell(self):
        return studioShell()

    def grab(self, name, settle=500):
        pump()
        idleWait(settle)
        from StudioLib import ui

        self.count += 1
        path = os.path.join(self.folder, f"{self.count:02d}-{name}.png")
        screen = ui.screenOf(self.mainWindow) or ui.primaryScreen()
        if not screen.grabWindow(0).copy(0, 0, *self.size).save(path):
            raise RuntimeError(f"failed to write {path}")
        self.paths.append(path)
        print(f"SCREEN {path}", flush=True)

    def step(self, name, function):
        try:
            function()
        except Exception:
            self.errors[name] = traceback.format_exc()[-3000:]
            print(f"SCREEN {name} FAILED:\n{self.errors[name]}", flush=True)

    def setTheme(self):
        from StudioLib import appmenu

        if not appmenu.setTheme(self.theme.capitalize()):
            applyTheme(self.theme)
        pump(20)

    def placeWindow(self):
        self.mainWindow.showNormal()
        self.mainWindow.setGeometry(0, 0, *self.size)
        pump()
        idleWait(500)

    def firstRun(self):
        chooser = waitFor(lambda: visibleTopLevel("StudioProjectsFolderDialog"), 40000)
        self.setTheme()
        self.placeWindow()
        if chooser is None:
            print("SCREEN firstrun skipped: no folder chooser", flush=True)
            return
        chooser = visibleTopLevel("StudioProjectsFolderDialog")
        self.grab("firstrun-chooser", 800)
        chooser.setPath(os.path.join(self.home, "Documents", "Slicer Studio"))
        chooser.continueButton.click()
        pump(20)

    def libraryEmpty(self):
        self.shell().switchTo("library")
        self.grab("library-empty", 800)

    def tourCard(self):
        from StudioLib import tour

        if tour.startTour("studio"):
            idleWait(800)
            self.grab("tour-card")
            live = tour.plugin()
            if live is not None:
                live.stopTour()
            pump(10)

    def drawEmpty(self):
        self.shell().switchTo("draw")
        self.grab("draw-empty", 800)

    def paintLesion(self, center, radius):
        import numpy as np

        from StudioLib import draw

        segmentation, source = draw.lesionNodes()
        segmentId = segmentation.GetSegmentation().GetNthSegmentID(0)
        array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentation, segmentId, source)
        import vtk

        matrix = vtk.vtkMatrix4x4()
        source.GetRASToIJKMatrix(matrix)
        ijk = [sum(matrix.GetElement(row, column) * (list(center) + [1.0])[column] for column in range(4)) for row in range(3)]
        k, j, i = np.indices(array.shape)
        spacing = source.GetSpacing()
        distance = ((i - ijk[0]) * spacing[0]) ** 2 + ((j - ijk[1]) * spacing[1]) ** 2 + ((k - ijk[2]) * spacing[2]) ** 2
        array[distance <= radius * radius] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentation, segmentId, source)
        return segmentation, source

    def drawPainting(self):
        from StudioLib import importer

        shell = self.shell()
        volume = slicer.util.loadVolume(self.args.mrhead, {"name": "sub-012 T1w"}) if os.path.exists(self.args.mrhead) else None
        if volume is not None:
            slicer.mrmlScene.RemoveNode(volume)
            importer.loadFiles([self.args.mrhead], shell)
        else:
            importer.startOnTemplate(shell=shell)
        waitFor(lambda: shell.currentWorkspace() == "draw", 15000)
        pump(20)
        idleWait(1500)
        self.paintLesion((-20.0, 20.0, 30.0), 12.0)
        editor = getattr(slicer.modules, "SegmentEditorWidget", None)
        if editor is not None:
            editor.editor.setActiveEffectByName("Paint")
        idleWait(1500)
        self.grab("draw-painting")
        shell.toast("Lesion saved to Lesions \u203a lesion.nii.gz", ms=8000, level="success")
        self.grab("toast", 300)

    def waitSaved(self):
        try:
            from StudioLib import autosave
        except ImportError:
            return
        service = autosave.instance()
        if service is None:
            return
        service.saveNow("screens", progress=False)
        waitFor(lambda: service.project is not None and not service.dirty and service.plan is None, 30000)
        idleWait(1500)

    def libraryProject(self, name):
        self.waitSaved()
        self.shell().switchTo("library")
        self.grab(name, 1500)

    def secondProject(self):
        from StudioLib import importer, projects_ui

        projects_ui.closeScene()
        pump(20)
        idleWait(1000)
        shell = self.shell()
        importer.startOnTemplate(shell=shell)
        waitFor(lambda: shell.currentWorkspace() == "draw", 15000)
        idleWait(1500)
        self.paintLesion((-30.0, -10.0, 20.0), 10.0)
        idleWait(1000)
        self.grab("draw-template-lesion")

    def libraryList(self):
        """All projects as a list (V-02), then back to the grid."""
        from StudioLib import library

        self.shell().switchTo("library")
        section = library.canvas().projectsSection
        section.setViewMode("list")
        self.grab("library-projects-list", 1000)
        section.setViewMode("grid")
        pump(10)

    def projectMenu(self):
        """The project menu of the top bar (V-02): Rename…, Show in Files, New project, Recent projects, Close project."""
        topBar = self.shell().topBar
        button = topBar.projectMenuButton
        if button.isHidden():
            print("SCREEN project-menu skipped: no project menu button", flush=True)
            return
        topBar._fillProjectMenu()
        topBar.projectMenu.popup(button.mapToGlobal(qt.QPoint(0, button.height)))
        self.grab("project-menu", 600)
        topBar.projectMenu.close()
        pump(10)

    def mapScreens(self):
        shell = self.shell()
        shell.switchTo("map")
        waitFor(lambda: shell.currentWorkspace() == "map", 15000)
        self.grab("map-cards", 1500)
        from StudioLib import mappage

        template = next((node for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode") if "MNI" in node.GetName()), None)
        if template is None:
            return
        path = os.path.join(self.data, "sub-05_desc-T_funcmap.nii.gz")
        if not os.path.exists(path):
            syntheticBlobVolume(path, template, [(-30, 20, 30, 10, 11.8), (40, -20, 20, 12, -7.4), (-40, -60, 30, 9, 9.6), (20, -70, -30, 8, 8.1)])
        mappage.loadMap(path, shell)
        idleWait(2000)
        self.grab("map-result")

    def palette(self):
        shell = self.shell()
        shell.switchTo("draw")
        idleWait(500)
        shell.openPalette()
        idleWait(500)
        from StudioLib import palette

        live = palette.instance()
        edit = getattr(live, "field", None)
        if edit is None and live is not None:
            edit = next(iter(live.widget.findChildren("QLineEdit")), None)
        if edit is not None:
            edit.setText("segment editor")
        self.grab("palette", 800)
        palette.close()
        pump(10)

    def cheatSheet(self):
        from StudioLib import cheatsheet

        cheatsheet.show(self.shell())
        self.grab("cheatsheet", 800)
        cheatsheet.hide()
        pump(10)

    def settings(self):
        dialog = slicer.app.settingsDialog()
        width, height = min(1100, self.size[0] - 80), min(760, self.size[1] - 80)
        for panel in ("Saving", "Appearance", "Updates", "Templates"):
            def show(panel=panel):
                dialog.setCurrentPanel(panel)
                dialog.show()
                dialog.resize(width, height)
                dialog.move((self.size[0] - width) // 2, (self.size[1] - height) // 2)
                self.grab(f"settings-{panel.lower()}", 1500)
            self.step(f"settings-{panel.lower()}", show)
        dialog.hide()
        pump(10)

    def addTemplate(self):
        from StudioLib import templates, templates_ui

        dialog = templates_ui.AddTemplateDialog(self.mainWindow)
        builtin = templates.builtinTemplates()[0]
        dialog.setImage(builtin.imagePath)
        dialog.show()
        dialog.move(max(0, (self.size[0] - dialog.width) // 2), max(0, (self.size[1] - dialog.height) // 2))
        idleWait(2500)
        self.grab("add-template")
        dialog.hide()
        dialog.deleteLater()
        pump(10)

    def drawStudyCase(self):
        """Draw with the first case of the open study (the case strip above the lesion card)."""
        from StudioLib import study_ui

        studySession = study_ui.session()
        if studySession is None or not studySession.study.subject_ids:
            print("SCREEN draw-study-case skipped: no open study", flush=True)
            return
        shell = self.shell()
        studySession.open(studySession.study.subject_ids[0])
        waitFor(lambda: shell.currentWorkspace() == "draw", 15000)
        self.grab("draw-study-case", 2000)

    def mapCohort(self):
        """Map with Seed = Cohort in the open study (the cohort cards instead of a single result)."""
        shell = self.shell()
        shell.switchTo("map")
        waitFor(lambda: shell.currentWorkspace() == "map", 15000)
        widget = getattr(slicer.modules, "LesionNetworkMappingWidget", None)
        modes = list(getattr(widget, "seedModes", []) or [])
        if widget is None or "cohort" not in modes:
            print("SCREEN map-cohort skipped: no cohort seed mode", flush=True)
            return
        widget.onSeedModeClicked(modes.index("cohort"))
        self.grab("map-cohort", 1500)
        widget.onSeedModeClicked(modes.index("lesion"))
        pump(10)

    def study(self):
        import shutil

        from StudioLib import projects_ui, study_ui, templates

        projects_ui.closeScene()
        pump(20)
        folder = os.path.join(self.home, "Documents", "Stroke aphasia LNM")
        os.makedirs(folder, exist_ok=True)
        builtin = templates.builtinTemplates()[0]
        files = []
        for index in range(1, 4):
            path = os.path.join(folder, f"sub-{index:02d}_T1w.nii.gz")
            if not os.path.exists(path):
                shutil.copyfile(builtin.imagePath, path)
            files.append(path)
        study_ui.createStudy(files, shell=self.shell(), folder=folder, name="Stroke aphasia LNM")
        self.shell().switchTo("library")
        self.grab("library-study", 2000)
        self.step("draw-study-case", self.drawStudyCase)
        self.step("map-cohort", self.mapCohort)
        # The study listed in Studies (V-02) once it is closed
        study_ui.closeStudy()
        pump(20)
        self.shell().switchTo("library")
        self.grab("library-studies", 2000)

    def run(self):
        self.step("firstrun", self.firstRun)
        if self.shell() is None:
            self.errors["studio"] = "the Studio shell is not active"
        else:
            for name, function in (("library-empty", self.libraryEmpty), ("tour-card", self.tourCard), ("draw-empty", self.drawEmpty),
                                   ("draw-painting", self.drawPainting), ("library-project", lambda: self.libraryProject("library-project")),
                                   ("second-project", self.secondProject), ("library-projects", lambda: self.libraryProject("library-projects")),
                                   ("library-list", self.libraryList), ("project-menu", self.projectMenu),
                                   ("map", self.mapScreens), ("palette", self.palette), ("cheatsheet", self.cheatSheet),
                                   ("settings", self.settings), ("add-template", self.addTemplate), ("study", self.study)):
                if self.args.only and name not in self.args.only.split(",") and name not in ("draw-painting", "second-project"):
                    continue
                self.step(name, function)
        manifest = {"screenshots": self.paths, "errors": self.errors, "size": f"{self.size[0]}x{self.size[1]}", "theme": self.theme}
        with open(os.path.join(self.folder, "manifest.json"), "w") as f:
            json.dump(manifest, f, indent=1)
        print(f"{len(self.paths)} screenshots in {self.folder}, {len(self.errors)} errors", flush=True)
        return 1 if self.errors else 0


def main(argv):
    parser = argparse.ArgumentParser(prog="StudioScreens.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--set", default="classic", choices=("classic", "studio"), help="classic: workspaces and gallery; studio: the Studio surfaces (V-01)")
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--only", default="", help="--set studio: comma-separated steps to take (data steps always run)")
    parser.add_argument("--sizes", default="1920x1080,1366x768", help="comma-separated window sizes (default: 1920x1080,1366x768)")
    parser.add_argument("--themes", default="dark,light", help="comma-separated themes: dark, light (default: dark,light)")
    parser.add_argument("--mrhead", default=DEFAULT_MRHEAD_PATH, help="MRHead volume file; SampleData MRHead is used if it does not exist")
    args = parser.parse_args(argv)
    if slicer.util.mainWindow() is None:
        raise RuntimeError("StudioScreens needs the main window (do not use --no-main-window)")
    os.makedirs(args.out, exist_ok=True)
    if args.set == "studio":
        return StudioScreens(args).run()
    return Screens(args).run()


if __name__ == "__main__":
    exitCode = 2
    try:
        arguments = sys.argv[1:]
        if arguments and arguments[0] == "--":
            arguments = arguments[1:]
        exitCode = main(arguments)
    except SystemExit as exception:
        exitCode = exception.code if isinstance(exception.code, int) else 2
    except Exception:
        traceback.print_exc()
        exitCode = 2
    finally:
        sys.stdout.flush()
        slicer.app.exit(exitCode)

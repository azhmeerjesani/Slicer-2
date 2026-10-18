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


def main(argv):
    parser = argparse.ArgumentParser(prog="StudioScreens.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--sizes", default="1920x1080,1366x768", help="comma-separated window sizes (default: 1920x1080,1366x768)")
    parser.add_argument("--themes", default="dark,light", help="comma-separated themes: dark, light (default: dark,light)")
    parser.add_argument("--mrhead", default=DEFAULT_MRHEAD_PATH, help="MRHead volume file; SampleData MRHead is used if it does not exist")
    args = parser.parse_args(argv)
    if slicer.util.mainWindow() is None:
        raise RuntimeError("StudioScreens needs the main window (do not use --no-main-window)")
    os.makedirs(args.out, exist_ok=True)
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

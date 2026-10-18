import qt

import slicer
from slicer.i18n import tr as _
from slicer.ScriptedLoadableModule import *


#
# Studio
#


def _onStartupCompleted():
    import time

    # The apply time of Studio includes importing the shell
    startTime = time.perf_counter()
    # Imported lazily, so that the module costs nothing at startup when Studio is not used
    from StudioLib import shell

    shell.onStartupCompleted(startTime)


class Studio(ScriptedLoadableModule):
    """The Library workspace of Slicer Studio, and the module that applies the Studio interface
    when the application has started (see StudioLib/shell.py).
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("Library")
        self.parent.categories = [""]
        self.parent.dependencies = []
        self.parent.hidden = False
        self.parent.contributors = ["Slicer Studio contributors"]
        self.parent.helpText = _("""
<p>Slicer Studio is the streamlined default interface of this application.
Its three workspaces follow the path of a lesion network mapping study:
<b>Library</b> (open data and choose a subject), <b>Draw</b> (trace the lesion) and <b>Map</b>
(compute and read a network map).</p>
<p>Keyboard: Ctrl+Alt+1/2/3 switch workspaces, Ctrl+K searches tools and actions,
Alt+1..4 change the view layout, Tab (in a view) shows or hides the inspector,
F11 toggles the focus mode and Ctrl+Alt+0 switches to the Classic interface and back
(also View &gt; Studio interface).</p>
<p>The Classic interface is never removed. Studio is not used when testing,
without main window, with the SLICER_CLASSIC_UI=1 environment variable,
or when the Studio/Enabled setting is false.</p>
""")
        self.parent.acknowledgementText = _("""
<p>Icons: <a href="https://lucide.dev">Lucide</a> 0.468.0, ISC License,
copyright (c) Lucide Contributors 2022 and Cole Bemis 2013-2022 (Feather, MIT).
The license text is in Resources/Icons/Lucide/LICENSE.</p>
<p>Font: <a href="https://rsms.me/inter/">Inter</a>, SIL Open Font License 1.1,
copyright (c) The Inter Project Authors.</p>
""")
        if not slicer.app.testingEnabled():
            slicer.app.connect("startupCompleted()", _onStartupCompleted)


#
# StudioWidget
#


class StudioWidget(ScriptedLoadableModuleWidget):
    """Inspector content of the Library workspace. It also works in Classic and without main window."""

    def __init__(self, parent=None):
        # Set before the base class constructor: without parent, it calls setup()
        self.libraryPanel = None
        ScriptedLoadableModuleWidget.__init__(self, parent)

    def setup(self):
        ScriptedLoadableModuleWidget.setup(self)
        # The module panel removes the margins of the module layout: use an inner container
        contents = qt.QWidget()
        contents.setObjectName("StudioLibraryContents")
        contentsLayout = qt.QVBoxLayout(contents)
        contentsLayout.setContentsMargins(12, 12, 12, 12)
        contentsLayout.setSpacing(8)
        panel = self._createLibraryPanel()
        if panel is not None:
            self.libraryPanel = panel
            contentsLayout.addWidget(panel)
        else:
            contentsLayout.addWidget(self._createPlaceholder())
        self.layout.addWidget(contents)
        self.layout.addStretch(1)

    @staticmethod
    def _createLibraryPanel():
        """The Library page (StudioLib.library.LibraryPanel), if this build has it."""
        library = StudioWidget._importStudioModule("StudioLib.library")
        if library is None:
            return None
        panelClass = getattr(library, "LibraryPanel", None)
        if panelClass is None:
            return None
        panel = panelClass()
        return panel if isinstance(panel, qt.QWidget) else getattr(panel, "widget", None)

    def _createPlaceholder(self):
        from StudioLib import ui

        frame, layout = ui.card(_("Get started"))
        frame.setObjectName("StudioLibraryPlaceholder")
        text = qt.QLabel(_("Open a scan, or start drawing on the MNI template."))
        text.setWordWrap(True)
        layout.addWidget(text)
        self.openButton = qt.QPushButton(_("Open…"))
        self.openButton.setObjectName("StudioOpenButton")
        self.openButton.setMinimumHeight(36)
        ui.setRole(self.openButton, "secondary")
        self.openButton.connect("clicked()", self.onOpen)
        layout.addWidget(self.openButton)
        self.templateButton = qt.QPushButton(_("Start on MNI template"))
        self.templateButton.setObjectName("StudioStartOnTemplateButton")
        self.templateButton.setMinimumHeight(36)
        ui.setRole(self.templateButton, "secondary")
        self.templateButton.connect("clicked()", self.onStartOnTemplate)
        layout.addWidget(self.templateButton)
        return frame

    @staticmethod
    def _shell():
        from StudioLib import shell

        return shell.instance()

    @staticmethod
    def _importStudioModule(name):
        """Import a Studio page or plug-in that may not exist in this build (None if it does not)."""
        import importlib

        try:
            return importlib.import_module(name)
        except ModuleNotFoundError as e:
            if e.name != name:
                raise
        return None

    @staticmethod
    def _importer():
        return StudioWidget._importStudioModule("StudioLib.importer")

    def onOpen(self):
        shell = self._shell()
        if shell is not None:
            shell.openFiles()
            return
        importer = self._importer()
        if importer is not None and hasattr(importer, "openFilesDialog"):
            importer.openFilesDialog(None)
        else:
            slicer.util.openAddDataDialog()

    def onStartOnTemplate(self):
        importer = self._importer()
        if importer is not None and hasattr(importer, "startOnTemplate"):
            importer.startOnTemplate()
            return
        # Without the Library importer: load the bundled template and open the Segment Editor
        import SampleData

        with slicer.util.tryWithErrorDisplay(_("Failed to load the MNI template."), waitCursor=True):
            volumeNode = SampleData.SampleDataLogic().downloadSample("MNI152NLin2009cAsym 2mm brain")
            slicer.util.setSliceViewerLayers(background=volumeNode, fit=True)
            shell = self._shell()
            if shell is not None:
                shell.switchTo("draw")
            else:
                slicer.util.selectModule("SegmentEditor")

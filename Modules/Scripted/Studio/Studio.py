import os

import qt

import slicer
from slicer.i18n import tr as _
from slicer.ScriptedLoadableModule import *


#
# Studio
#


#: Files, folders and URLs given on the command line that the Studio module took over from the core loader
#: (``takeOverCommandLineArguments()``), opened once Studio has been applied; None when there are none
_commandLineArguments = None


def takeOverCommandLineArguments(willApply=None, options=None):
    """Take the files given on the command line from 3D Slicer's core loader when Studio applies at startup.

    The core loader (``qSlicerCoreApplication::handleURIArguments()``, run when the event loop starts) reads every image as a
    plain volume. When Studio will be applied, its handling is turned off here, before the event loop starts, and the
    arguments are opened by ``StudioLib.importer.openCommandLineArguments()`` once Studio is applied, as a drop would be.
    The same arguments as the core loader are taken: none when a Python script was given (they are its arguments).
    ``willApply`` (default ``_studioWillApplyAtStartup()``) and ``options`` (default ``slicer.app.commandOptions()``) are for
    tests. Return the arguments taken over (a list).
    """
    global _commandLineArguments
    app = slicer.app
    if not hasattr(app, "setURIArgumentHandlingEnabled") or not app.isURIArgumentHandlingEnabled():
        # A build without the setter, or the application does not open them: the core keeps its own handling
        return []
    options = app.commandOptions() if options is None else options
    arguments = [str(argument) for argument in (options.unparsedArguments or [])]
    if not arguments or options.pythonScript or options.extraPythonScript:
        return []
    if not (_studioWillApplyAtStartup() if willApply is None else willApply):
        return []
    app.setURIArgumentHandlingEnabled(False)
    _commandLineArguments = arguments
    return list(arguments)


def _openCommandLineArguments():
    """Open the arguments taken over by ``takeOverCommandLineArguments()`` (with the core loader in Classic)."""
    global _commandLineArguments
    arguments, _commandLineArguments = _commandLineArguments, None
    if not arguments:
        return
    try:
        from StudioLib import importer

        importer.openCommandLineArguments(arguments)
    except Exception:
        import logging
        import traceback

        logging.error("The files given on the command line could not be opened:\n" + traceback.format_exc())


#: Files that macOS hands to the running application as QFileOpenEvent (Finder "Open With", a double click, a drop on the
#: Dock icon; at a cold start they come as an event instead of command line arguments). They are kept until startup is
#: completed (Studio is applied by then) and then opened with the routes of the command line files.
_pendingFileOpens = []
_startupCompleted = False
_fileOpenFilter = None


class _FileOpenFilter(qt.QObject):
    """Event filter of the application: QEvent::FileOpen opens the file like Open... does (nothing else handles it)."""

    def eventFilter(self, obj, event):
        if event.type() != qt.QEvent.FileOpen:
            return False
        try:
            path = str(event.file())
            if not path:
                url = event.url()
                path = str(url.toString()) if url is not None else ""
        except Exception:
            return False
        if path:
            openFileFromSystem(path)
        return True


def installFileOpenFilter():
    """Install the QFileOpenEvent handling on the application (once). Return the filter.

    The filter is removed when the application quits: a Python event filter must not stay on the application while the
    Python interpreter is finalized (the widgets owned by Python objects are deleted then, their destruction sends
    events through the application event filters, and the half-finalized filter crashes the process on every quit).
    """
    global _fileOpenFilter
    if _fileOpenFilter is None:
        _fileOpenFilter = _FileOpenFilter(slicer.app)
        slicer.app.installEventFilter(_fileOpenFilter)
        slicer.app.connect("aboutToQuit()", removeFileOpenFilter)
    return _fileOpenFilter


def removeFileOpenFilter():
    """Remove the QFileOpenEvent handling from the application (done when the application quits)."""
    global _fileOpenFilter
    fileOpenFilter = _fileOpenFilter
    if fileOpenFilter is None:
        return
    _fileOpenFilter = None
    try:
        slicer.app.disconnect("aboutToQuit()", removeFileOpenFilter)
    except Exception:
        pass
    try:
        slicer.app.removeEventFilter(fileOpenFilter)
    except Exception:
        pass


def openFileFromSystem(path):
    """Open a file that the system gave to the running application (QFileOpenEvent): at once, or after startup."""
    if not _startupCompleted:
        _pendingFileOpens.append(path)
        return
    _openFiles([path])


def _openFiles(paths):
    try:
        from StudioLib import importer

        importer.openCommandLineArguments(paths)
    except Exception:
        import logging
        import traceback

        logging.error("The file given by the system could not be opened:\n" + traceback.format_exc())


def _openPendingFileOpens():
    paths = list(_pendingFileOpens)
    del _pendingFileOpens[:]
    if paths:
        _openFiles(paths)


def _onStartupCompleted():
    global _startupCompleted
    _startupCompleted = True
    if _pendingFileOpens:
        qt.QTimer.singleShot(0, _openPendingFileOpens)
    import time

    # The apply time of Studio includes importing the shell
    startTime = time.perf_counter()
    if _commandLineArguments:
        # After this slot (Studio is applied by then), when the core loader would have read them
        qt.QTimer.singleShot(0, _openCommandLineArguments)
    # Imported lazily, so that the module costs nothing at startup when Studio is not used
    from StudioLib import shell

    if slicer.app.property("SlicerDisclaimerHandledByApplication"):
        # Receive the research-use disclaimer that the main window does not show (W3-03): it may come before Studio is
        # applied. Studio shows it as a Library footer line, Classic as the official dialog.
        try:
            from StudioLib import firstrun

            firstrun.connectDisclaimer()
        except Exception:
            import logging
            import traceback

            slicer.app.setProperty("SlicerDisclaimerHandledByApplication", False)
            logging.error("Studio: cannot receive the disclaimer:\n" + traceback.format_exc())
    shell.onStartupCompleted(startTime)
    # In-app updates (A-08), in Classic and in Studio. A required update is shown before the recovery offer, so
    # autosave (and its recovery offer) starts when the startup check is done: at once without an automatic check,
    # after at most 5 s with one, and not at all when the application quits to install a required update.
    started = []

    def startAutosave():
        if not started:
            started.append(True)
            _startAutosave()

    try:
        from StudioLib import updates

        updates.onStartupCompleted(startAutosave)
    except Exception:
        import logging
        import traceback

        logging.error("Updates could not be started:\n" + traceback.format_exc())
        startAutosave()
    # Settings > Templates is also there in Classic: the Normalize and Map messages of the Lesion Network Mapping module
    # (which works in Classic) point to it. In Studio the templates plug-in installs it.
    try:
        from StudioLib import templates_ui

        templates_ui.installSettingsPanelDeferred()
    except Exception:
        import logging
        import traceback

        logging.error("Settings > Templates could not be scheduled:\n" + traceback.format_exc())


def _startAutosave():
    """Scene autosave, projects and crash recovery, in Classic and in Studio (after the Studio apply time measurement).

    At the first start of Studio, the Slicer Studio folder chooser comes first (A-09); the autosave service, whose
    recovery offers follow, starts when it was answered.
    """

    def start():
        try:
            from StudioLib import autosave

            autosave.onStartupCompleted()
        except Exception:
            import logging
            import traceback

            logging.error("Autosave could not be started:\n" + traceback.format_exc())

    try:
        from StudioLib import projects_ui

        projects_ui.onStartupCompleted(start)
    except Exception:
        import logging
        import traceback

        logging.error("The Slicer Studio folder chooser could not be shown:\n" + traceback.format_exc())
        start()


def _studioWillApplyAtStartup():
    """True if Studio will be applied when the application has started.

    The same rules as ``StudioLib.shell.willApplyAtStartup()``, without importing the shell (startup time) and before
    the main window exists (the module is created first): not testing, a main window, ``SLICER_CLASSIC_UI`` is not
    "1", ``Studio/Enabled`` and ``Studio/Mode`` is not "classic".
    """
    if slicer.app.testingEnabled() or slicer.app.commandOptions().noMainWindow:
        return False
    if os.environ.get("SLICER_CLASSIC_UI") == "1":
        return False
    if not slicer.util.settingsValue("Studio/Enabled", True, converter=slicer.util.toBool):
        return False
    return str(slicer.util.settingsValue("Studio/Mode", "studio")).lower() != "classic"


class Studio(ScriptedLoadableModule):
    """The Library workspace of Slicer Studio, and the module that applies the Studio interface
    when the application has started (see StudioLib/shell.py).
    """

    @staticmethod
    def _formatHelpKeys(text):
        """``text`` with its ``{key:Ctrl+K}`` markers as this platform shows the keys (⌘K on macOS), as
        ``StudioLib.migration.formatKeys`` does (not imported: the module is created at start-up).
        """
        import re

        def native(match):
            sequence = qt.QKeySequence(match.group(1))
            return (sequence.toString(qt.QKeySequence.NativeText) if sequence.count() == 1 else "") or match.group(1)

        return re.sub(r"\{key:([^}]+)\}", native, text)

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("Library")
        self.parent.categories = [""]
        self.parent.dependencies = []
        self.parent.hidden = False
        self.parent.contributors = ["Slicer Studio contributors"]
        self.parent.helpText = self._formatHelpKeys(_("""
<p>Slicer Studio is the streamlined default interface of this application.
Its three workspaces follow the path of a lesion network mapping study:
<b>Library</b> (open data and choose a subject), <b>Draw</b> (trace the lesion) and <b>Map</b>
(compute and read a network map).</p>
<p>Keyboard: {key:Ctrl+Alt+1}/2/3 switch workspaces, {key:Ctrl+K} searches tools and actions,
{key:Alt+1}..5 change the view layout, {key:Ctrl+\\} (or {key:Ctrl+5}) shows or hides the inspector,
F6 and Shift+F6 move the keyboard focus between the top bar, the inspector and the views,
F11 (or {key:Ctrl+6}) hides or shows the panels, Esc backs out of the place and window/level modes, a maximized view
and hidden panels, and {key:Ctrl+Alt+0} switches to the Classic interface and back
(also View &gt; Studio interface). On Windows, where AltGr and Ctrl+Alt cannot be told apart, text fields
keep the characters typed with AltGr (for example @) and in a view AltGr+digit acts as Ctrl+Alt+digit.</p>
<p>The Classic interface is never removed. Studio is not used when testing,
without main window, with the SLICER_CLASSIC_UI=1 environment variable,
or when the Studio/Enabled setting is false.</p>
<p>Projects (Studio): at the first start you choose the Slicer Studio folder (Documents/Slicer Studio by
default). From then on your work is saved there automatically as you work, one folder per project
(Projects/&lt;name&gt;, a 3D Slicer scene with Images, Segmentations, Lesions, Maps and other folders), and
studies go to Studies. {key:Ctrl+S} saves at once, {key:Ctrl+N} starts a new project, and File &gt; Save a copy as… writes
a .mrb file anywhere. The folder can be changed in the Saving panel of the application settings.</p>
<p>Recovery (Classic, and scenes that are not a project): while you work, the scene is autosaved every few
minutes to a private folder of your user account (the StudioAutosave folder in the application data folder),
never to your own folders. If Slicer closes unexpectedly, or you close without saving, the work can be
recovered at the next start or with File &gt; Recover Autosaved Work.</p>
<p>Updates: Slicer Studio checks for a new version when it starts (one anonymous request to github.com) and
offers it in the Updates panel of the application settings (also ≡ &gt; Check for updates…, or Help &gt; Check for
Updates… in Classic). An update is downloaded, checked against its published SHA-256 checksum and installed when
Slicer Studio restarts. Automatic checks can be turned off in the Updates panel.</p>
<p>Your own MNI templates: Library &gt; Start on template ▾ &gt; Add your own template… (or Search: Add MNI template)
adds an MNI-space brain image, with an optional brain mask and 3D surfaces. It is copied into the Templates folder of
the Slicer Studio folder and can be drawn on, mapped from and normalized to; Settings &gt; Templates sets the default.</p>
"""))
        self.parent.acknowledgementText = _("""
<p>Icons: <a href="https://lucide.dev">Lucide</a> 0.468.0, ISC License,
copyright (c) Lucide Contributors 2022 and Cole Bemis 2013-2022 (Feather, MIT).
The license text is in Resources/Icons/Lucide/LICENSE.</p>
<p>Font: <a href="https://rsms.me/inter/">Inter</a>, SIL Open Font License 1.1,
copyright (c) The Inter Project Authors.</p>
""")
        if not slicer.app.testingEnabled():
            slicer.app.connect("startupCompleted()", _onStartupCompleted)
            installFileOpenFilter()
            if _studioWillApplyAtStartup():
                # Set before the main window is first shown: it emits disclaimerRequested() instead of opening its
                # disclaimer dialog, and Studio shows a non-modal research-use line (StudioLib/firstrun.py)
                slicer.app.setProperty("SlicerDisclaimerHandledByApplication", True)
                # Files given on the command line take Studio's import routes, not the core loader (U-07)
                takeOverCommandLineArguments(willApply=True)


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
        # isWidgetType(), not isinstance(panel, qt.QWidget): PythonQt wrappers of subclasses differ in Qt 5 and Qt 6
        isWidget = callable(getattr(panel, "isWidgetType", None)) and panel.isWidgetType()
        panelWidget = panel if isWidget else getattr(panel, "widget", None)
        if panelWidget is not None:
            # The panel object is kept (it may be a Python object that owns its widget)
            self.libraryPanel = panel
            contentsLayout.addWidget(panelWidget)
        else:
            contentsLayout.addWidget(self._createPlaceholder())
        self.layout.addWidget(contents)
        self.layout.addStretch(1)

    @staticmethod
    def _createLibraryPanel():
        """The Library page (StudioLib.library.LibraryPanel: a QWidget, or an object with a ``widget``),
        if this build has it.
        """
        library = StudioWidget._importStudioModule("StudioLib.library")
        if library is None:
            return None
        panelClass = getattr(library, "LibraryPanel", None)
        if panelClass is None:
            return None
        return panelClass()

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


#
# StudioTest
#


class StudioTest(ScriptedLoadableModuleTest):
    """Self-test of the Studio module (``slicer.selfTests["Studio"]``): a quick smoke check of the shell.

    The full tests are the ``Studio*Test.py`` suites of ``Testing/Python``. Studio never applies itself under ``--testing``:
    the smoke test applies it with ``Shell(force=True)``, visits each workspace and restores the Classic interface.
    """

    def setUp(self):
        slicer.mrmlScene.Clear(0)

    def runTest(self):
        self.setUp()
        self.test_StudioSmoke()

    def _pump(self, count=4):
        for _index in range(count):
            slicer.app.processEvents()

    def test_StudioSmoke(self):
        self.delayDisplay("Starting the Studio smoke test")
        from StudioLib import shell, ui, workspaces

        self.assertTrue(ui.token("text").isValid())
        mainWindow = slicer.util.mainWindow()
        if mainWindow is None:
            # Without the main window there is no shell: the library imports, nothing is applied
            self.assertIsNone(shell.instance())
            self.delayDisplay("Studio smoke test passed (no main window)")
            return
        if slicer.app.testingEnabled():
            self.assertIsNone(shell.instance(), "Studio must not apply itself under --testing")
        studioShell = shell.instance() or shell.Shell(force=True)
        createdHere = shell.instance() is None
        try:
            if createdHere:
                studioShell.apply()
                self._pump()
            self.assertIs(shell.instance(), studioShell)
            self.assertIsNotNone(mainWindow.findChild("QToolBar", "StudioTopBar"))
            for workspace in workspaces.WORKSPACES:
                studioShell.switchTo(workspace.key)
                self._pump()
                self.assertEqual(studioShell.currentWorkspace(), workspace.key)
            studioShell.switchTo("library")
            self._pump()
        finally:
            if createdHere:
                studioShell.dispose()
                self._pump()
                slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
        if createdHere:
            self.assertIsNone(shell.instance())
            self.assertIsNone(mainWindow.findChild("QToolBar", "StudioTopBar"))
        self.delayDisplay("Studio smoke test passed")

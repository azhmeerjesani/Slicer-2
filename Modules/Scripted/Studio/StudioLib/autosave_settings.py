"""The "Saving" panel of the application settings (A-01, SPEC 9.9; A-09).

- **Slicer Studio folder** (A-09): where projects and studies are saved, with Change… (which offers to move the projects
  and studies there) and Show. Key ``Studio/Projects/Root`` (``projects``).
- **Cadence**: projects are saved, and other scenes autosaved, at most every N minutes, after a pause of N seconds.
- **Recovery** (scenes that are not projects: Classic, studies): the private recovery folder and its retention.

It writes only fork-only keys ``Studio/Autosave/*`` and ``Studio/Projects/*`` (SPEC 9.7 rule 5: official Slicer does not
read them). Their defaults live in code (``autosave_io.DEFAULT_SETTINGS``), not in SlicerDefaultSettings.ini. The panel
is not added under ``--testing``.
"""

import os

import ctk
import qt
import slicer
from slicer.i18n import tr as _

from . import autosave_io as io
from . import ui

PANEL_OBJECT_NAME = "StudioAutosaveSettingsPanel"
#: Name of the panel in the settings dialog (A-09: it was "Autosave")
PANEL_NAME = "Saving"


def _key(name):
    return f"{io.SETTINGS_GROUP}/{name}"


class _ShowEventFilter(qt.QObject):
    def __init__(self, callback):
        qt.QObject.__init__(self)
        self.callback = callback

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.Show:
            self.callback()
        return False


class AutosaveSettingsPanel(ctk.ctkSettingsPanel):
    """Autosave settings: cadence, location, retention and the recovery actions."""

    def __init__(self, service=None, *args, **kwargs):
        ctk.ctkSettingsPanel.__init__(self, *args, **kwargs)
        self.setObjectName(PANEL_OBJECT_NAME)
        self.service = service
        self._updatingDirectory = False
        # A Studio settings page (VISUAL 4.7, V-02): the title and one line, section labels, the folder card, 44 px rows with
        # hairlines, a switch and 88 px numeric fields. The page scrolls in a short window (1366 x 768).
        outer = qt.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scrollArea = qt.QScrollArea()
        scrollArea.setObjectName("StudioSavingScrollArea")
        scrollArea.setWidgetResizable(True)
        scrollArea.setFrameShape(qt.QFrame.NoFrame)
        scrollArea.setHorizontalScrollBarPolicy(qt.Qt.ScrollBarAlwaysOff)
        outer.addWidget(scrollArea)
        page = qt.QWidget()
        page.setObjectName("StudioSavingPageContent")
        scrollArea.setWidget(page)
        scrollArea.viewport().setAutoFillBackground(False)
        layout = qt.QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(6)
        ui.settingsPageHeader(layout, _("Saving"), _("Your work is saved as you go. There is no Save to remember."), "StudioSavingPage")

        # The Slicer Studio folder (A-09)
        layout.addWidget(ui.sectionLabel(_("Slicer Studio folder")))
        layout.addSpacing(4)
        folderCard, self.rootLabel, self.rootNote, folderButtons = ui.folderCard("folder", "StudioProjectsFolderCard")
        self.rootLabel.setObjectName("StudioProjectsRootLabel")
        self.rootNote.setObjectName("StudioProjectsRootNote")
        self.showRootButton = qt.QPushButton(_("Show in {manager}").format(manager=self._fileManagerName()))
        self.showRootButton.setObjectName("StudioProjectsShowRootButton")
        self.showRootButton.connect("clicked()", self.onShowRoot)
        self.changeRootButton = qt.QPushButton(_("Change…"))
        self.changeRootButton.setObjectName("StudioProjectsChangeRootButton")
        self.changeRootButton.connect("clicked()", self.onChangeRoot)
        for button in (self.showRootButton, self.changeRootButton):
            ui.setRole(button, "secondary")
            folderButtons.addWidget(button)
        layout.addWidget(folderCard)
        layout.addSpacing(16)

        # When to save
        layout.addWidget(ui.sectionLabel(_("When to save")))
        self.enabledCheckBox = ui.switch("", "StudioAutosaveEnabledSwitch")
        self.enabledCheckBox.setSizePolicy(qt.QSizePolicy.Fixed, qt.QSizePolicy.Fixed)
        self.enabledCheckBox.setAccessibleName(_("Save automatically"))
        self.enabledCheckBox.checked = io.DEFAULT_SETTINGS["Enabled"]
        self.enabledCheckBox.toolTip = _(
            "Projects are saved in the Slicer Studio folder as you work. Other scenes (Classic, studies) are copied to a "
            "private recovery folder, so that unsaved work can be recovered after a crash; there, use File › Save to keep your work.")  # noqa: RUF001
        layout.addWidget(ui.settingsRow(_("Save automatically"), self.enabledCheckBox, _("Turning this off keeps crash recovery only")))
        self.registerProperty(_key("Enabled"), self.enabledCheckBox, "checked", str(qt.SIGNAL("toggled(bool)")))
        self.idleSpinBox = self._spinBox(3, 300, "IdleSeconds", _(" s"))
        layout.addWidget(ui.settingsRow(_("After a pause of"), self.idleSpinBox))
        self.intervalSpinBox = self._spinBox(1, 60, "IntervalMinutes", _(" min"))
        layout.addWidget(ui.settingsRow(_("And at least every"), self.intervalSpinBox, last=True))
        layout.addWidget(ui.label(_("Large scans (over 100 MB to write) are saved every 10 minutes, and always when you switch projects, "
                                    "press Ctrl+S or quit."), "caption", "text3", "StudioSavingLargeNote", wrap=True))
        layout.addSpacing(16)

        # Recovery of the scenes that are not projects
        layout.addWidget(ui.sectionLabel(_("Recovery")))
        layout.addWidget(ui.label(_("Scenes that are not projects (Classic, studies) are autosaved to a private folder, so that your work "
                                    "can be recovered after a crash."), "caption", "text3", "StudioAutosaveRecoveryTitle", wrap=True))
        # Location: not registered with registerProperty, so that only an explicit choice writes the key
        self.directoryButton = ctk.ctkDirectoryButton()
        self.directoryButton.setMinimumWidth(260)
        self.directoryButton.setMaximumWidth(420)
        self.directoryButton.toolTip = _("Folder of new autosaves. It should be a private folder, not a synchronized or shared one.")
        self.directoryButton.connect("directoryChanged(QString)", self.onDirectoryChanged)
        # The Lucide folder line icon (not the platform's coloured bitmap), re-tinted with the palette
        self._refreshFolderIcon()
        ui.addPaletteListener(self._refreshFolderIcon)
        self.resetDirectoryButton = qt.QPushButton(_("Reset"))
        self.resetDirectoryButton.toolTip = _("Use the default folder (in the application data folder)")
        self.resetDirectoryButton.connect("clicked()", self.onResetDirectory)
        self.openDirectoryButton = qt.QPushButton(_("Open"))
        self.openDirectoryButton.connect("clicked()", self.onOpenDirectory)
        for button in (self.resetDirectoryButton, self.openDirectoryButton):
            ui.setRole(button, "ghost")
        location = qt.QWidget()
        locationLayout = qt.QHBoxLayout(location)
        locationLayout.setContentsMargins(0, 0, 0, 0)
        locationLayout.setSpacing(6)
        locationLayout.addWidget(self.directoryButton, 1)
        locationLayout.addWidget(self.resetDirectoryButton)
        locationLayout.addWidget(self.openDirectoryButton)
        locationRow = ui.settingsRow(_("Location"), location)
        self.locationNote = ui.label(_("A new location applies to new autosaves."), "caption", "text3", "StudioAutosaveLocationNote", wrap=True)
        layout.addWidget(locationRow)
        layout.addWidget(self.locationNote)
        self.keepSpinBox = self._spinBox(1, 50, "KeepSessions", "")
        layout.addWidget(ui.settingsRow(_("Sessions to keep"), self.keepSpinBox))
        self.maxDiskSpinBox = self._spinBox(256, 102400, "MaxDiskMB", _(" MB"))
        layout.addWidget(ui.settingsRow(_("At most"), self.maxDiskSpinBox))
        self.maxAgeSpinBox = self._spinBox(1, 365, "MaxAgeDays", _(" days"))
        layout.addWidget(ui.settingsRow(_("Delete after"), self.maxAgeSpinBox))
        self.maxNodeSpinBox = self._spinBox(16, 102400, "MaxNodeMB", _(" MB"))
        self.maxNodeSpinBox.toolTip = _("Data larger than this is not autosaved (it is listed as not autosaved when you recover)")
        layout.addWidget(ui.settingsRow(_("Skip data larger than"), self.maxNodeSpinBox, last=True))

        buttons = qt.QHBoxLayout()
        buttons.setSpacing(8)
        self.recoverButton = qt.QPushButton(_("Recover older work…"))
        self.recoverButton.connect("clicked()", self.onRecover)
        ui.setRole(self.recoverButton, "secondary")
        buttons.addWidget(self.recoverButton)
        self.deleteAllButton = qt.QPushButton(_("Delete all autosaves…"))
        self.deleteAllButton.connect("clicked()", self.onDeleteAll)
        self.autosaveNowButton = qt.QPushButton(_("Autosave now"))
        self.autosaveNowButton.connect("clicked()", self.onAutosaveNow)
        for button in (self.deleteAllButton, self.autosaveNowButton):
            ui.setRole(button, "ghost")
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addSpacing(8)
        layout.addLayout(buttons)
        self.statusLabel = ui.label("", "caption", "text3", "StudioAutosaveStatusLabel", wrap=True)
        layout.addWidget(self.statusLabel)
        layout.addStretch(1)
        ui.bindStyleSheet(self, lambda: ui.settingsStyleSheet() + "QScrollArea#StudioSavingScrollArea, QWidget#StudioSavingPageContent { background: transparent; }")

        self.connect("settingChanged(QString,QVariant)", self.onSettingChanged)
        self.updateDirectory()
        self.updateRoot()
        self.updateStatus()
        # Refresh when the panel is shown, and on autosave status changes while it is visible
        self._showFilter = _ShowEventFilter(self._onShown)
        self.installEventFilter(self._showFilter)
        if service is not None:
            service.addStatusListener(lambda status: self.updateStatus() if self.isVisible() else None)

    @staticmethod
    def _fileManagerName():
        from . import projects

        return projects.fileManagerName()

    def _onShown(self):
        self.updateDirectory()
        self.updateRoot()
        self.updateStatus()

    def updateRoot(self):
        from . import projects

        root = projects.root()
        # The home folder is written "~" (the full path is the tooltip)
        ui.setElidedText(self.rootLabel, projects.shortPath(root), root)
        fromEnvironment = projects.isRootFromEnvironment()
        self.changeRootButton.enabled = not fromEnvironment and projects.isEnabled()
        if fromEnvironment:
            note = _("Set by the {variable} environment variable.").format(variable=projects.ROOT_ENVIRONMENT)
        elif not projects.isEnabled():
            note = _("Projects are turned off ({variable}=0).").format(variable=projects.ENABLED_ENVIRONMENT)
        else:
            projectCount, studyCount = projects.folderSummary(root) if os.path.isdir(root) else (0, 0)
            note = " · ".join([_("1 project") if projectCount == 1 else _("{n} projects").format(n=projectCount),
                               _("1 study") if studyCount == 1 else _("{n} studies").format(n=studyCount)])
            synced = projects.syncedService(root)
            if synced:
                note += " · " + _("synced to {service}: scans can contain patient data").format(service=synced)
        self.rootNote.setText(note)
        self.rootNote.setToolTip(_("Every project is a 3D Slicer scene (Projects/<name>), saved as you work."))

    def onChangeRoot(self):
        from . import projects_ui

        projects_ui.changeRootDialog(self)
        self.updateRoot()

    def onShowRoot(self):
        from . import projects_ui

        projects_ui.showRootFolder()

    def _spinBox(self, minimum, maximum, name, suffix):
        spinBox = ui.numberField(minimum, maximum, io.DEFAULT_SETTINGS[name], suffix)
        self.registerProperty(_key(name), spinBox, "value", str(qt.SIGNAL("valueChanged(int)")))
        return spinBox

    def onSettingChanged(self, key, value):
        if self.service is not None:
            self.service.reloadSettings()

    def updateDirectory(self):
        self._updatingDirectory = True
        try:
            self.directoryButton.directory = io.defaultRoot()
        finally:
            self._updatingDirectory = False
        overridden = bool(os.environ.get("SLICER_STUDIO_AUTOSAVE_DIR"))
        self.directoryButton.enabled = not overridden
        self.resetDirectoryButton.enabled = not overridden and bool(io.settingsValue("Directory"))
        if overridden:
            self.locationNote.text = _("Set by the SLICER_STUDIO_AUTOSAVE_DIR environment variable.")
        else:
            self.locationNote.text = _("A new location applies to new autosaves.")

    def _refreshFolderIcon(self):
        button = getattr(self, "directoryButton", None)
        if button is None:
            return
        try:
            button.icon = ui.icon("folder", 16)
        except RuntimeError:
            # The settings dialog (and the button with it) was deleted
            ui.removePaletteListener(self._refreshFolderIcon)

    def onDirectoryChanged(self, directory):
        if self._updatingDirectory:
            return
        qt.QSettings().setValue(_key("Directory"), "" if os.path.normpath(directory) == io.builtinRoot() else directory)
        self.onSettingChanged(_key("Directory"), directory)
        self.updateDirectory()

    def onResetDirectory(self):
        qt.QSettings().setValue(_key("Directory"), "")
        self.onSettingChanged(_key("Directory"), "")
        self.updateDirectory()

    def onOpenDirectory(self):
        path = io.defaultRoot()
        if os.path.isdir(path):
            from . import projects

            projects.showInFileManager(path)

    def onRecover(self):
        from . import recovery

        recovery.showRecoveryDialog(None, startup=False)

    def onDeleteAll(self):
        from . import recovery

        answer = qt.QMessageBox.question(self, _("Delete all autosaves"),
                                         _("Delete the autosaved work of every closed session? This cannot be undone."),
                                         qt.QMessageBox.Yes | qt.QMessageBox.Cancel, qt.QMessageBox.Cancel)
        if answer != qt.QMessageBox.Yes:
            return
        service = self.service
        recovery.deleteAll(io.defaultRoot() if service is None else service.rootDirectory(), service.sessionId if service is not None else None)
        qt.QTimer.singleShot(500, self.updateStatus)

    def onAutosaveNow(self):
        if self.service is not None:
            self.service.autosaveNow("settings")
        qt.QTimer.singleShot(1000, self.updateStatus)

    def updateStatus(self):
        from . import recovery

        service = self.service
        root = service.rootDirectory() if service is not None else io.defaultRoot()
        sessions = recovery.scan(root, service.sessionId if service is not None else None) if os.path.isdir(root) else []
        total = sum(session.bytes() for session in sessions)
        parts = []
        status = service.status() if service is not None else None
        if status is not None and status.lastSaveTime:
            import time

            parts.append(_("Last autosave {time}").format(time=time.strftime("%H:%M", time.localtime(status.lastSaveTime))))
        parts.append(_("{size} in {count} sessions").format(size=recovery.formatBytes(total), count=len(sessions)))
        if status is not None and status.state in ("paused", "error") and status.message:
            parts.append(status.message)
        self.statusLabel.text = " · ".join(parts)


_panel = None


def install(service=None):
    """Add the Autosave panel to the application settings (idempotent; never under --testing)."""
    global _panel
    if slicer.app.testingEnabled() and not getattr(service, "force", False):
        return None
    dialog = slicer.app.settingsDialog()
    if dialog is None:
        return None
    existing = dialog.findChild(qt.QWidget, PANEL_OBJECT_NAME)
    if existing is not None:
        return existing
    _panel = AutosaveSettingsPanel(service)
    dialog.addPanel(_(PANEL_NAME), _panel)
    return _panel


def panel():
    return _panel

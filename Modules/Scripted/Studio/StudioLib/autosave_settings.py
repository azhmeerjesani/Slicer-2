"""The "Autosave" panel of the application settings (A-01, SPEC 9.9).

It writes only the fork-only keys ``Studio/Autosave/*`` (SPEC 9.7 rule 5: official Slicer does not read them).
Their defaults live in code (``autosave_io.DEFAULT_SETTINGS``), not in SlicerDefaultSettings.ini. The panel is not
added under ``--testing``.
"""

import os

import ctk
import qt
import slicer
from slicer.i18n import tr as _

from . import autosave_io as io

PANEL_OBJECT_NAME = "StudioAutosaveSettingsPanel"


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
        layout = qt.QVBoxLayout(self)

        form = qt.QFormLayout()
        form.setFieldGrowthPolicy(qt.QFormLayout.FieldsStayAtSizeHint)
        layout.addLayout(form)
        self.enabledCheckBox = qt.QCheckBox(_("Autosave the scene"))
        self.enabledCheckBox.checked = io.DEFAULT_SETTINGS["Enabled"]
        self.enabledCheckBox.toolTip = _(
            "Keep a copy of the scene in a private folder while you work, so that unsaved work can be recovered "
            "after a crash or after closing without saving. Autosave is not Save: use File › Save to keep your work.")  # noqa: RUF001
        form.addRow(self.enabledCheckBox)
        self.registerProperty(_key("Enabled"), self.enabledCheckBox, "checked", str(qt.SIGNAL("toggled(bool)")))

        self.intervalSpinBox = self._spinBox(1, 60, "IntervalMinutes", _(" min"))
        self.idleSpinBox = self._spinBox(3, 300, "IdleSeconds", _(" s"))
        cadence = qt.QHBoxLayout()
        cadence.addWidget(self.intervalSpinBox)
        cadence.addWidget(qt.QLabel(_("or after a pause of")))
        cadence.addWidget(self.idleSpinBox)
        cadence.addStretch(1)
        form.addRow(_("At most every:"), cadence)

        # Location: not registered with registerProperty, so that only an explicit choice writes the key
        self.directoryButton = ctk.ctkDirectoryButton()
        self.directoryButton.setMinimumWidth(360)
        self.directoryButton.toolTip = _("Folder of new autosaves. It should be a private folder, not a synchronized or shared one.")
        self.directoryButton.connect("directoryChanged(QString)", self.onDirectoryChanged)
        self.resetDirectoryButton = qt.QPushButton(_("Reset"))
        self.resetDirectoryButton.toolTip = _("Use the default folder (in the application data folder)")
        self.resetDirectoryButton.connect("clicked()", self.onResetDirectory)
        self.openDirectoryButton = qt.QPushButton(_("Open"))
        self.openDirectoryButton.connect("clicked()", self.onOpenDirectory)
        location = qt.QHBoxLayout()
        location.addWidget(self.directoryButton, 1)
        location.addWidget(self.resetDirectoryButton)
        location.addWidget(self.openDirectoryButton)
        form.addRow(_("Location:"), location)
        self.locationNote = qt.QLabel(_("A new location applies to new autosaves."))
        form.addRow("", self.locationNote)

        self.keepSpinBox = self._spinBox(1, 50, "KeepSessions", _(" sessions"))
        form.addRow(_("Keep:"), self.keepSpinBox)
        self.maxDiskSpinBox = self._spinBox(256, 102400, "MaxDiskMB", _(" MB"))
        form.addRow(_("Max size:"), self.maxDiskSpinBox)
        self.maxAgeSpinBox = self._spinBox(1, 365, "MaxAgeDays", _(" days"))
        form.addRow(_("Delete after:"), self.maxAgeSpinBox)
        self.maxNodeSpinBox = self._spinBox(16, 102400, "MaxNodeMB", _(" MB"))
        self.maxNodeSpinBox.toolTip = _("Data larger than this is not autosaved (it is listed as not autosaved when you recover)")
        form.addRow(_("Skip data larger than:"), self.maxNodeSpinBox)

        buttons = qt.QHBoxLayout()
        self.recoverButton = qt.QPushButton(_("Recover autosaved work…"))
        self.recoverButton.connect("clicked()", self.onRecover)
        buttons.addWidget(self.recoverButton)
        self.deleteAllButton = qt.QPushButton(_("Delete all autosaves…"))
        self.deleteAllButton.connect("clicked()", self.onDeleteAll)
        buttons.addWidget(self.deleteAllButton)
        self.autosaveNowButton = qt.QPushButton(_("Autosave now"))
        self.autosaveNowButton.connect("clicked()", self.onAutosaveNow)
        buttons.addWidget(self.autosaveNowButton)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.statusLabel = qt.QLabel()
        self.statusLabel.wordWrap = True
        layout.addWidget(self.statusLabel)
        layout.addStretch(1)

        self.connect("settingChanged(QString,QVariant)", self.onSettingChanged)
        self.updateDirectory()
        self.updateStatus()
        # Refresh when the panel is shown, and on autosave status changes while it is visible
        self._showFilter = _ShowEventFilter(self._onShown)
        self.installEventFilter(self._showFilter)
        if service is not None:
            service.addStatusListener(lambda status: self.updateStatus() if self.isVisible() else None)

    def _onShown(self):
        self.updateDirectory()
        self.updateStatus()

    def _spinBox(self, minimum, maximum, name, suffix):
        spinBox = qt.QSpinBox()
        spinBox.minimum = minimum
        spinBox.maximum = maximum
        spinBox.value = io.DEFAULT_SETTINGS[name]
        spinBox.suffix = suffix
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
            qt.QDesktopServices.openUrl(qt.QUrl.fromLocalFile(path))

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
    dialog.addPanel(_("Autosave"), _panel)
    return _panel


def panel():
    return _panel

"""The "Updates" panel of the application settings (A-08), in Studio and in Classic.

It shows this version, the latest release and its notes, and offers Check now, Update now and the automatic check
setting. Only the fork-only key ``Studio/Updates/AutoCheck`` is written by the panel (SPEC 9.7 rule 5); its default
lives in code (``updates.DEFAULT_SETTINGS``). The panel is not added under ``--testing``.
"""

import time

import ctk
import qt
import slicer
from slicer.i18n import tr as _

from . import updates

PANEL_OBJECT_NAME = "StudioUpdatesSettingsPanel"


class _ShowEventFilter(qt.QObject):
    def __init__(self, callback):
        qt.QObject.__init__(self)
        self.callback = callback

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.Show:
            self.callback()
        return False


class UpdatesSettingsPanel(ctk.ctkSettingsPanel):
    """Updates settings: version, latest release, Check now, Update now, automatic checks."""

    def __init__(self, service=None, *args, **kwargs):
        ctk.ctkSettingsPanel.__init__(self, *args, **kwargs)
        self.setObjectName(PANEL_OBJECT_NAME)
        self.service = service
        layout = qt.QVBoxLayout(self)

        form = qt.QFormLayout()
        form.setFieldGrowthPolicy(qt.QFormLayout.ExpandingFieldsGrow)
        layout.addLayout(form)
        self.versionLabel = qt.QLabel()
        self.versionLabel.setObjectName("StudioUpdatesVersionLabel")
        self.versionLabel.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
        form.addRow(_("This version:"), self.versionLabel)
        self.latestLabel = qt.QLabel()
        self.latestLabel.setObjectName("StudioUpdatesLatestLabel")
        self.latestLabel.setOpenExternalLinks(True)
        self.latestLabel.setTextFormat(qt.Qt.RichText)
        form.addRow(_("Latest release:"), self.latestLabel)
        # Word-wrapped text outside the form layout: a form row does not grow for a wrapped label
        self.statusLabel = qt.QLabel()
        self.statusLabel.setObjectName("StudioUpdatesStatusLabel")
        self.statusLabel.setWordWrap(True)
        self.statusLabel.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
        layout.addWidget(self.statusLabel)

        self.notesBrowser = qt.QTextBrowser()
        self.notesBrowser.setObjectName("StudioUpdatesNotes")
        self.notesBrowser.setOpenExternalLinks(True)
        self.notesBrowser.setMaximumHeight(160)
        layout.addWidget(self.notesBrowser)

        buttons = qt.QHBoxLayout()
        self.checkButton = qt.QPushButton(_("Check now"))
        self.checkButton.setObjectName("StudioUpdatesCheckButton")
        self.checkButton.connect("clicked()", self.onCheckNow)
        buttons.addWidget(self.checkButton)
        self.updateButton = qt.QPushButton(_("Update now"))
        self.updateButton.setObjectName("StudioUpdatesUpdateButton")
        self.updateButton.connect("clicked()", self.onUpdateNow)
        buttons.addWidget(self.updateButton)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.autoCheckBox = qt.QCheckBox(_("Check for updates automatically when Slicer Studio starts"))
        self.autoCheckBox.setObjectName("StudioUpdatesAutoCheck")
        self.autoCheckBox.checked = updates.DEFAULT_SETTINGS["AutoCheck"]
        self.autoCheckBox.toolTip = _("One anonymous request to github.com at startup (and once a day while Slicer Studio runs). "
                                      "Required updates are found only by a check.")
        layout.addWidget(self.autoCheckBox)
        self.registerProperty(f"{updates.SETTINGS_GROUP}/AutoCheck", self.autoCheckBox, "checked", str(qt.SIGNAL("toggled(bool)")))
        self.privacyLabel = qt.QLabel(_("A check downloads the small file studio-update.json of the latest public release "
                                        "from github.com. Nothing about you, this computer or your data is sent."))
        self.privacyLabel.setObjectName("StudioUpdatesPrivacyLabel")
        self.privacyLabel.setWordWrap(True)
        layout.addWidget(self.privacyLabel)
        layout.addStretch(1)

        self._showFilter = _ShowEventFilter(self.refresh)
        self.installEventFilter(self._showFilter)
        if service is not None:
            service.addListener(self._onServiceChanged)
        self.refresh()

    def _onServiceChanged(self, service):
        if self.isVisible():
            self.refresh()

    def refresh(self):
        service = self.service
        current = updates.currentVersion()
        channel = updates.channel()
        channelText = _("development build") if channel != "release" else None
        self.versionLabel.text = _("Slicer Studio {version}").format(version=current) + (f" ({channelText})" if channelText else "")
        status = service.status if service is not None else None
        manifest = status.manifest if status is not None else None
        if manifest is None:
            self.latestLabel.text = _("Not checked yet")
            self.notesBrowser.setPlainText("")
            self.notesBrowser.setVisible(False)
        else:
            parts = [f"<b>{manifest.version}</b>"]
            if manifest.published:
                parts.append(_("published {date}").format(date=manifest.published[:10]))
            if status.kind == "required":
                parts.append(_("required"))
            elif status.skipped:
                parts.append(_("skipped"))
            link = f'<a href="{manifest.releasePageUrl()}">{_("Release notes")}</a>'
            self.latestLabel.text = " · ".join(parts) + " · " + link
            if manifest.notes and hasattr(self.notesBrowser, "setMarkdown"):
                self.notesBrowser.setMarkdown(manifest.notes)
            else:
                self.notesBrowser.setPlainText(manifest.notes)
            self.notesBrowser.setVisible(bool(manifest.notes) and status.available)
        self.statusLabel.text = self.statusText()
        checking = service is not None and service.checking
        self.checkButton.enabled = service is not None and not checking and updates.feedUrl() is not None
        self.checkButton.text = _("Checking…") if checking else _("Check now")
        available = status is not None and status.available
        self.updateButton.enabled = available and service is not None and service.unavailableReason() is None
        self.updateButton.toolTip = (service.unavailableReason() or "") if service is not None and available else ""
        self.updateButton.text = _("Update to {version}").format(version=status.version) if available else _("Update now")
        if updates.feedOverride() is not None:
            self.autoCheckBox.toolTip = _("The update address is set by {variable}.").format(variable=updates.FEED_ENVIRONMENT_VARIABLE)

    def statusText(self):
        service = self.service
        if updates.feedUrl() is None:
            return _("Update checks are turned off ({variable}=off).").format(variable=updates.FEED_ENVIRONMENT_VARIABLE)
        if service is not None and service.checking:
            return _("Checking…")
        parts = []
        status = service.status if service is not None else None
        if status is not None:
            if status.kind == "required":
                parts.append(_("Slicer Studio {version} is required.").format(version=status.version))
            elif status.kind == "optional":
                parts.append(_("Slicer Studio {version} is available.").format(version=status.version))
            else:
                parts.append(_("Slicer Studio is up to date."))
            if status.available and service is not None and service.unavailableReason():
                parts.append(service.unavailableReason())
        if service is not None and service.lastError is not None:
            parts.append(_("The last check failed: {error}").format(error=service.lastError))
        last = updates.settingsValue("LastCheck")
        if last:
            try:
                parts.append(_("Last checked {time}.").format(time=time.strftime("%Y-%m-%d %H:%M", time.strptime(last, "%Y-%m-%dT%H:%M:%S"))))
            except ValueError:
                pass
        elif updates.channel() != "release" and updates.feedOverride() is None:
            parts.append(_("A development build does not check by itself: use Check now."))
        return " ".join(parts)

    def onCheckNow(self):
        if self.service is not None:
            self.service.checkNow()
        self.refresh()

    def onUpdateNow(self):
        if self.service is not None:
            self.service.updateNow()
        self.refresh()


_panel = None


def install(service=None):
    """Add the Updates panel to the application settings (idempotent; never under --testing)."""
    global _panel
    if slicer.app.testingEnabled() and not getattr(service, "force", False):
        return None
    dialog = slicer.app.settingsDialog()
    if dialog is None:
        return None
    existing = dialog.findChild(qt.QWidget, PANEL_OBJECT_NAME)
    if existing is not None:
        return existing
    _panel = UpdatesSettingsPanel(service)
    dialog.addPanel(_("Updates"), _panel)
    return _panel


def panel():
    return _panel

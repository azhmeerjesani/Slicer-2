"""The "Updates" panel of the application settings (A-08), in Studio and in Classic.

It shows this version, the latest release and its notes, and offers Check now, Update now and the automatic check
setting. The section "Code from main" shows the code updates from main between releases (``updates_commit``): the
installed build, the code that runs, the last check, Restart now, Go back and the switch. Only the fork-only keys
``Studio/Updates/AutoCheck`` and ``Studio/Updates/CommitUpdates`` are written by the panel (SPEC 9.7 rule 5); their
defaults live in code (``updates.DEFAULT_SETTINGS``). The panel is not added under ``--testing``.
"""

import time

import ctk
import qt
import slicer
from slicer.i18n import tr as _

from . import projects, ui, updates

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
        # A Studio settings page (VISUAL 4.7): the title and one line, section labels, 44 px rows with hairlines, a switch
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(6)
        title = ui.label(_("Updates"), "title", "text", "StudioUpdatesTitle")
        layout.addWidget(title)
        layout.addWidget(ui.label(_("New versions of Slicer Studio, from its public releases."), "body", "text2", "StudioUpdatesDescription"))
        layout.addSpacing(14)
        layout.addWidget(ui.sectionLabel(_("Version")))

        def row(text, valueWidget, last=False):
            frame = qt.QFrame()
            frame.setObjectName("StudioSettingsRow" if not last else "StudioSettingsRowLast")
            frame.setMinimumHeight(ui.SETTINGS_ROW_HEIGHT)
            rowLayout = qt.QHBoxLayout(frame)
            rowLayout.setContentsMargins(0, 0, 0, 0)
            rowLayout.setSpacing(12)
            rowLayout.addWidget(ui.label(text, "body", "text"))
            rowLayout.addStretch(1)
            rowLayout.addWidget(valueWidget)
            layout.addWidget(frame)
            return frame

        self.versionLabel = qt.QLabel()
        self.versionLabel.setObjectName("StudioUpdatesVersionLabel")
        self.versionLabel.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
        row(_("This version"), self.versionLabel)
        self.latestLabel = qt.QLabel()
        self.latestLabel.setObjectName("StudioUpdatesLatestLabel")
        projects.routeLinks(self.latestLabel)
        self.latestLabel.setTextFormat(qt.Qt.RichText)
        row(_("Latest release"), self.latestLabel, last=True)
        # Word-wrapped text outside the rows: a row does not grow for a wrapped label
        self.statusLabel = qt.QLabel()
        self.statusLabel.setObjectName("StudioUpdatesStatusLabel")
        self.statusLabel.setWordWrap(True)
        self.statusLabel.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
        ui.setText(self.statusLabel, "caption")
        layout.addWidget(self.statusLabel)

        self.notesBrowser = qt.QTextBrowser()
        self.notesBrowser.setObjectName("StudioUpdatesNotes")
        projects.routeLinks(self.notesBrowser)
        self.notesBrowser.setMaximumHeight(160)
        layout.addWidget(self.notesBrowser)

        buttons = qt.QHBoxLayout()
        buttons.setSpacing(8)
        self.checkButton = qt.QPushButton(_("Check now"))
        self.checkButton.setObjectName("StudioUpdatesCheckButton")
        self.checkButton.connect("clicked()", self.onCheckNow)
        ui.setRole(self.checkButton, "secondary")
        buttons.addWidget(self.checkButton)
        self.updateButton = qt.QPushButton(_("Update now"))
        self.updateButton.setObjectName("StudioUpdatesUpdateButton")
        self.updateButton.connect("clicked()", self.onUpdateNow)
        ui.setRole(self.updateButton, "primary")
        buttons.addWidget(self.updateButton)
        buttons.addStretch(1)
        layout.addSpacing(6)
        layout.addLayout(buttons)
        layout.addSpacing(14)
        layout.addWidget(ui.sectionLabel(_("When to check")))

        self.autoCheckBox = ui.switch(_("Check for updates automatically when Slicer Studio starts"), "StudioUpdatesAutoCheck")
        self.autoCheckBox.setMinimumHeight(ui.SETTINGS_ROW_HEIGHT)
        self.autoCheckBox.checked = updates.DEFAULT_SETTINGS["AutoCheck"]
        self.autoCheckBox.toolTip = _("One anonymous request to github.com at startup (and once a day while Slicer Studio runs). "
                                      "Required updates are found only by a check.")
        layout.addWidget(self.autoCheckBox)
        self.registerProperty(f"{updates.SETTINGS_GROUP}/AutoCheck", self.autoCheckBox, "checked", str(qt.SIGNAL("toggled(bool)")))
        self.privacyLabel = qt.QLabel(_("A check downloads the small file studio-update.json of the latest public release "
                                        "from github.com. Nothing about you, this computer or your data is sent."))
        self.privacyLabel.setObjectName("StudioUpdatesPrivacyLabel")
        self.privacyLabel.setWordWrap(True)
        ui.setText(self.privacyLabel, "caption")
        layout.addWidget(self.privacyLabel)

        # Code updates from main between releases
        layout.addSpacing(14)
        layout.addWidget(ui.sectionLabel(_("Code from main")))
        self.buildLabel = qt.QLabel()
        self.buildLabel.setObjectName("StudioCommitBuildLabel")
        self.buildLabel.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
        row(_("Installed build"), self.buildLabel)
        self.runningLabel = qt.QLabel()
        self.runningLabel.setObjectName("StudioCommitRunningLabel")
        self.runningLabel.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
        row(_("Running code"), self.runningLabel, last=True)
        self.commitStatusLabel = qt.QLabel()
        self.commitStatusLabel.setObjectName("StudioCommitStatusLabel")
        self.commitStatusLabel.setWordWrap(True)
        self.commitStatusLabel.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
        ui.setText(self.commitStatusLabel, "caption")
        layout.addWidget(self.commitStatusLabel)
        commitButtons = qt.QHBoxLayout()
        commitButtons.setSpacing(8)
        self.commitCheckButton = qt.QPushButton(_("Check main now"))
        self.commitCheckButton.setObjectName("StudioCommitCheckButton")
        self.commitCheckButton.connect("clicked()", self.onCheckCommits)
        ui.setRole(self.commitCheckButton, "secondary")
        commitButtons.addWidget(self.commitCheckButton)
        self.commitRestartButton = qt.QPushButton(_("Restart now"))
        self.commitRestartButton.setObjectName("StudioCommitRestartButton")
        self.commitRestartButton.connect("clicked()", self.onCommitRestart)
        ui.setRole(self.commitRestartButton, "primary")
        commitButtons.addWidget(self.commitRestartButton)
        self.commitBackButton = qt.QPushButton(_("Go back"))
        self.commitBackButton.setObjectName("StudioCommitBackButton")
        self.commitBackButton.connect("clicked()", self.onCommitGoBack)
        ui.setRole(self.commitBackButton, "secondary")
        commitButtons.addWidget(self.commitBackButton)
        commitButtons.addStretch(1)
        layout.addLayout(commitButtons)
        self.commitSwitch = ui.switch(_("Also get code updates from main between releases"), "StudioCommitUpdatesSwitch")
        self.commitSwitch.setMinimumHeight(ui.SETTINGS_ROW_HEIGHT)
        self.commitSwitch.toolTip = _("Python and resource files of new commits on main, checked at start-up. Anything compiled "
                                      "into Slicer Studio waits for the next release.")
        self.commitSwitch.checked = bool(updates.settingsValue("CommitUpdates"))
        self.commitSwitch.connect("toggled(bool)", self.onCommitSwitch)
        layout.addWidget(self.commitSwitch)
        self.commitPrivacyLabel = qt.QLabel(_("A check asks github.com which commit is the newest on main and reads its list of files; "
                                              "changed files come from raw.githubusercontent.com and are checked against that commit. "
                                              "Nothing about you, this computer or your data is sent. Anyone who can change main can "
                                              "change the code that Slicer Studio runs."))
        self.commitPrivacyLabel.setObjectName("StudioCommitPrivacyLabel")
        self.commitPrivacyLabel.setWordWrap(True)
        ui.setText(self.commitPrivacyLabel, "caption")
        layout.addWidget(self.commitPrivacyLabel)
        layout.addStretch(1)
        ui.bindStyleSheet(self, lambda: (
            f"QFrame#StudioSettingsRow {{ border: none; border-bottom: 1px solid {ui.token('hairline').name()}; background: transparent; }}"
            "QFrame#StudioSettingsRowLast { border: none; background: transparent; }"
            f"QLabel#StudioUpdatesStatusLabel, QLabel#StudioUpdatesPrivacyLabel, QLabel#StudioCommitStatusLabel, "
            f"QLabel#StudioCommitPrivacyLabel {{ color: {ui.token('text3').name()}; }}"
            f"QLabel#StudioUpdatesVersionLabel, QLabel#StudioUpdatesLatestLabel, QLabel#StudioCommitBuildLabel, "
            f"QLabel#StudioCommitRunningLabel {{ color: {ui.token('text2').name()}; }}"
            f"QTextBrowser#StudioUpdatesNotes {{ background-color: {ui.token('canvas').name()}; border: 1px solid {ui.token('hairline').name()}; border-radius: 8px; }}"))

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
                updates._flattenHeadings(self.notesBrowser)
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
        self.refreshCommits()

    def refreshCommits(self):
        """The section "Code from main"."""
        service = self.service
        try:
            description = updates.updates_commit.describe(str(slicer.app.slicerHome), updates.commitStateDirectory())
        except Exception:
            description = {"build": None, "running": updates.studio_overlay_status(), "state": None}
        build = description.get("build") or {}
        running = description.get("running") or {}
        state = description.get("state") or {}
        manifests = description.get("manifests") or {}
        commit = build.get("commit")
        if commit:
            self.buildLabel.text = "{version} · {commit}{date}".format(
                version=build.get("studioVersion") or updates.currentVersion(), commit=commit[:7], date=updates._commitDate(build.get("committed")))
        else:
            self.buildLabel.text = _("Slicer Studio {version} (commit not known)").format(version=updates.currentVersion())
        active = running.get("active")
        if active:
            manifest = manifests.get(active) or {}
            self.runningLabel.text = _("main @ {commit}{date}").format(commit=active[:7], date=updates._commitDate(manifest.get("committed")))
            self.runningLabel.toolTip = manifest.get("subject") or ""
        else:
            self.runningLabel.text = _("The installed build")
            self.runningLabel.toolTip = ""
        pending = state.get("pending")
        enabled = bool(updates.settingsValue("CommitUpdates"))
        if bool(self.commitSwitch.checked) != enabled:
            self.commitSwitch.blockSignals(True)
            self.commitSwitch.checked = enabled
            self.commitSwitch.blockSignals(False)
        self.commitStatusLabel.text = self.commitStatusText(description)
        checking = service is not None and service.commitChecking
        self.commitCheckButton.enabled = service is not None and not checking and bool(commit) and updates.updates_commit.override() != "off"
        self.commitCheckButton.text = _("Checking…") if checking else _("Check main now")
        self.commitRestartButton.visible = bool(pending) and not state.get("disabled")
        previous = state.get("previous") if active and state.get("active") == active else None
        self.commitBackButton.visible = bool(active)
        self.commitBackButton.text = (_("Go back to main @ {commit}").format(commit=previous[:7]) if previous
                                      else _("Go back to the installed build"))

    def commitStatusText(self, description):
        build = description.get("build") or {}
        running = description.get("running") or {}
        state = description.get("state") or {}
        parts = []
        if updates.updates_commit.override() == "off":
            return _("Code updates are turned off ({variable}=off).").format(variable=updates.updates_commit.ENVIRONMENT_VARIABLE)
        if updates.channel() != "release":
            parts.append(_("A development build never takes code updates."))
        elif not build.get("commit"):
            parts.append(_("This build does not know the commit it was made from: it takes no code updates."))
        elif build.get("dirty"):
            parts.append(_("This build was made from changed files: it takes no code updates."))
        if state.get("disabled") and state.get("disabledReason") == "setting":
            parts.append(_("Turned off: the installed build runs."))
        pending = state.get("pending")
        if pending == "installed":
            parts.append(_("The installed build runs again from the next start."))
        elif pending:
            parts.append(_("main @ {commit} is ready and applies at the next start.").format(commit=pending[:7]))
        rolledBack = state.get("rolledBack")
        if isinstance(rolledBack, dict) and rolledBack.get("from"):
            parts.append(_("main @ {commit} did not start and was undone.").format(commit=str(rolledBack["from"])[:7]))
        if running.get("error"):
            parts.append(_("The code update was not applied: {error}.").format(error=running["error"]))
        result = self.service.commitResult if self.service is not None else None
        if isinstance(result, dict):
            kind = result.get("result")
            head = str(result.get("head") or "")[:7]
            if kind == "upToDate":
                parts.append(_("main has no newer code for this build."))
            elif kind == "ready" and result.get("already") == "active":
                parts.append(_("Up to date with main @ {commit}.").format(commit=head))
            elif kind == "ready" and result.get("staged") is False:
                parts.append(_("main @ {commit} has code updates; they are not taken while a newer release is available.").format(commit=head))
            elif kind == "needsBuild":
                parts.append(updates.commitNeedsBuildText(result))
            elif kind == "tooLarge":
                parts.append(_("main @ {commit} changes too many files for a code update: it will come with the next release.").format(commit=head))
            elif kind == "skipped":
                parts.append(_("main @ {commit} was undone on this computer and is not taken again.").format(commit=head))
            elif kind in ("failed", "unavailable") and result.get("error"):
                parts.append(_("The last check of main failed: {error}.").format(error=result["error"]))
            if kind in ("ready", "needsBuild") and result.get("paths"):
                parts.append(_("Files: {paths}").format(paths=", ".join(result["paths"][:5]) + ("…" if len(result["paths"]) > 5 else "")))
            last = updates.parseTime(result.get("checked")) if result.get("checked") else None
            if last is not None:
                parts.append(_("Checked {time}.").format(time=time.strftime("%Y-%m-%d %H:%M", time.localtime(last))))
        return " ".join(parts)

    def onCheckCommits(self):
        if self.service is not None:
            self.service.checkCommits(force=True)
        self.refresh()

    def onCommitRestart(self):
        if self.service is not None:
            self.service.restartForCommitUpdate()

    def onCommitGoBack(self):
        if self.service is not None:
            self.service.goBackFromCommitUpdate()
        self.refresh()

    def onCommitSwitch(self, checked):
        if self.service is not None:
            self.service.setCommitUpdatesEnabled(bool(checked))
        else:
            updates.setSettingsValue("CommitUpdates", bool(checked))
        self.refresh()

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
            parts.append(_("The last check failed. {error}").format(error=updates.sentence(service.lastError)))
        last = updates.settingsValue("LastCheck")
        if last:
            lastTime = updates.parseTime(last)
            if lastTime is not None:
                parts.append(_("Last checked {time}.").format(time=time.strftime("%Y-%m-%d %H:%M", time.localtime(lastTime))))
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

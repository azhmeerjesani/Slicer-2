"""Studio surfaces of the scene autosave (A-01b, SPEC 9.9; USABILITY 6.4): visible but quiet, as in Lightroom.

- The top-bar status ``StudioSaveStatus`` (``Shell.setSaveStatus()``): "Autosaving…", "Autosaved · 2 min ago" or
  "Autosave paused", hidden while nothing was autosaved in this scene. Its tooltip says that autosave is not Save;
  its menu ``StudioSaveStatusMenu`` offers Autosave now, Recover autosaved work…, Open autosave folder and
  Autosave settings…. While the scene is a project (A-09) the status is the save state instead ("Saved", "Saving…",
  "Not saved · Retry", ``projects_ui.projectStatus()``) and the menu offers the project actions (Save now, Rename
  project…, Show project folder, Save a copy as…, New project, Open project…).
- The recovery card ``StudioRecoveryCard`` at the top of the Library canvas (``library.showNotice()``) replaces
  A-01's dialog at startup. In another workspace a one-line inspector notice (key ``autosave-recovery``) points to
  it. Nothing is ever shown over a view.
- Every autosave records the workspace and the open study and subject (a context provider), so that recovery
  returns the user to where they were.

A Studio plug-in (``shell.OPTIONAL_PLUGINS``): ``install(shell)`` runs when Studio is applied, ``uninstall(shell)``
in Classic, where A-01's dialog and status-bar messages take over again. Every string says "Autosaved", never
"Saved". Without an autosave service (disabled, testing) the status stays hidden.
"""

import logging
import os
import time
import traceback

import qt
import slicer
from slicer.i18n import tr as _

from . import autosave, library, ui

#: Key of the Library notice and of the inspector notice that points to it
NOTICE_KEY = "autosave-recovery"
#: The relative time of the status ("2 min ago") is refreshed this often while the status is shown
STATUS_REFRESH_MS = 30000
MAX_CARD_ROWS = 3
CARD_ROW_HEIGHT = 64
THUMBNAIL_SIZE = 64
HEADER_ICON_SIZE = 18
#: Workspaces that Recover returns to (Library is where the card is)
RETURN_WORKSPACES = ("draw", "map")

_ui = None
#: Sessions dismissed with the card's ✕ ("Later"): not offered again in this run
_dismissed = set()


def instance():
    """The installed ``AutosaveUi``, or None (Classic, or Studio not applied)."""
    return _ui


def install(shell):
    """Studio plug-in: the status, its menu, the recovery presenter and the context provider (at most 3 ms)."""
    global _ui
    if _ui is not None:
        _ui.dispose()
    _ui = AutosaveUi(shell)
    if not _ui.attach():
        # The autosave service starts right after the Studio apply (Studio.py): attach when the event loop runs
        qt.QTimer.singleShot(0, _ui.attach)


def uninstall(shell):
    """Classic: remove the status, the card and the notice; A-01's dialog is the presenter again."""
    global _ui
    if _ui is not None:
        try:
            _ui.dispose()
        finally:
            _ui = None


def _keyText(key):
    return qt.QKeySequence(key).toString(qt.QKeySequence.NativeText)


def _clock(seconds):
    return time.strftime("%H:%M", time.localtime(seconds))


def _studyContext(shell):
    """{"path", "name", "subject"} of the open study (duck-typed ``shell.studySession``, W3-07), or None."""
    session = getattr(shell, "studySession", None)
    if session is None:
        return None
    study = getattr(session, "study", None)
    path = getattr(study, "path", None) or getattr(study, "root", None) or getattr(session, "path", None)
    if not path:
        return None
    name = getattr(study, "name", None) or getattr(session, "name", None) or os.path.basename(str(path))
    current = getattr(session, "current", None)
    subject = current() if callable(current) else None
    return {"path": str(path), "name": name, "subject": subject}


class AutosaveUi:
    """The Studio surfaces of one autosave service (``install()``)."""

    def __init__(self, shell):
        self.shell = shell
        self.service = None
        self.card = None
        self.notice = None
        self.menu = None
        self._disposed = False
        self._timer = qt.QTimer()
        self._timer.setInterval(STATUS_REFRESH_MS)
        self._timer.connect("timeout()", self.refreshStatus)

    #
    # Life cycle
    #

    def attach(self, service=None):
        """Connect to the autosave service (``autosave.instance()`` by default). Return True if one is connected."""
        if self._disposed:
            return False
        service = service or autosave.instance()
        if service is None:
            self.shell.setSaveStatus(None)
            return False
        if service is self.service:
            return True
        if self.service is not None:
            self._detach()
        self.service = service
        service.addStatusListener(self.onStatus)
        service.setRecoveryPresenter(self.presentRecovery)
        service.registerContextProvider(self.context)
        self.menu = self._buildMenu()
        self.shell.setSaveStatusMenu(self.menu)
        self.shell.registerEscapeOverlay(self.menu, self.menu.close)
        self.onStatus(service.status())
        # The startup scan already ran (Classic -> Studio): offer what is still recoverable
        pending = [session for session in service.pendingRecoveries() if session.sessionId not in _dismissed]
        if pending:
            pending = [session for session in pending if self._stillRecoverable(session)]
        if pending:
            self.presentRecovery(pending, startup=True)
        return True

    def _detach(self):
        service = self.service
        if service is None:
            return
        service.removeStatusListener(self.onStatus)
        if getattr(service, "_presenter", None) == self.presentRecovery:
            service.setRecoveryPresenter(None)
        service.unregisterContextProvider(self.context)
        self.service = None

    def dispose(self):
        self._disposed = True
        self._timer.stop()
        self._detach()
        # Kept for the next Studio apply in this run (not dismissed)
        self.closeCard()
        self.shell.setSaveStatus(None)
        self.shell.setSaveStatusMenu(None)
        if self.menu is not None:
            self.shell.unregisterEscapeOverlay(self.menu)
            self.menu.setObjectName("")
            self.menu.deleteLater()
            self.menu = None

    #
    # Context of each autosave (where the user was)
    #

    def context(self):
        context = {}
        try:
            workspace = self.shell.currentWorkspace()
            if workspace is None and hasattr(self.shell, "transientOrigin"):
                # A module opened from Search: the workspace it returns to
                workspace = self.shell.transientOrigin()
            if workspace is not None:
                context["workspace"] = workspace
            study = _studyContext(self.shell)
            if study is not None:
                context["study"] = study
        except Exception:
            logging.error("Autosave: the Studio context failed:\n" + traceback.format_exc())
        return context

    #
    # Status of the top bar
    #

    def _sceneAutosave(self, status):
        """(time, bytes) of the last autosave of the current scene, or None if it was not autosaved yet."""
        service = self.service
        if service is None or status.lastSaveTime is None:
            return None
        slot = service.slots.get(service.slotName()) if getattr(service, "slots", None) is not None else None
        if not slot or not slot.get("generation") or slot.get("state", "current") != "current":
            return None
        return status.lastSaveTime, slot.get("bytes", 0) or 0

    def _studyLine(self):
        """("Study <name>: Journal saved 14:31", timestamp) of the open study (W3-07), or (None, None)."""
        session = getattr(self.shell, "studySession", None)
        saveStatus = getattr(session, "saveStatus", None)
        if not callable(saveStatus):
            return None, None
        try:
            text, timestamp = saveStatus()
        except Exception:
            return None, None
        name = getattr(session, "name", None) or _("study")
        line = _("Study {name}: {status}").format(name=name, status=text)
        if timestamp:
            line += f" {_clock(timestamp)}"
        return line, timestamp

    def toolTip(self, autosaved=None):
        if autosaved is not None:
            when, size = autosaved
            last = _("last {time} · {size}").format(time=_clock(when), size=autosave._formatBytes(size))
            text = _("Your work is copied to a private recovery folder ({last}).").format(last=last)
        else:
            text = _("Your work is copied to a private recovery folder.")
        text += " " + _("This is not saving: use File › Save ({key}) to save your files.").format(key=_keyText("Ctrl+S"))  # noqa: RUF001
        return text

    def onStatus(self, status):
        """Status listener of the autosave service (``AutosaveStatus``)."""
        if self._disposed:
            return
        try:
            self._showStatus(status)
        except Exception:
            logging.error("Autosave: the status of the top bar failed:\n" + traceback.format_exc())

    def _showStatus(self, status):
        if self.menu is not None:
            self._updateMenu(status)
        if getattr(status, "projectName", None):
            from . import projects_ui

            text, level, toolTip, iconName = projects_ui.projectStatus(status)
            self.shell.setSaveStatus(text, level, toolTip, iconName, title=_("Saving"))
            if not self._timer.isActive():
                self._timer.start()
            return
        autosaved = self._sceneAutosave(status)
        toolTip = self.toolTip(autosaved)
        text = level = None
        if status.state == "saving":
            text, level = _("Autosaving…"), "busy"
        elif status.state in ("paused", "error"):
            text, level = _("Autosave paused"), "warn"
            if status.message:
                toolTip = status.message + "\n" + toolTip
        elif status.state != "disabled" and autosaved is not None:
            studyLine, studyTime = self._studyLine()
            when = max(autosaved[0], studyTime or 0)
            text, level = _("Autosaved · {when}").format(when=library.relativeTime(when)), "neutral"
            if studyLine:
                toolTip += "\n" + studyLine
        if text is None:
            self.shell.setSaveStatus(None)
            self._timer.stop()
            return
        self.shell.setSaveStatus(text, level, toolTip)
        if not self._timer.isActive():
            self._timer.start()

    def refreshStatus(self):
        if self.service is not None:
            self.onStatus(self.service.status())

    #
    # Menu of the status
    #

    def _buildMenu(self):
        menu = qt.QMenu()
        menu.setObjectName("StudioSaveStatusMenu")
        self.headerAction = menu.addAction("")
        self.headerAction.setObjectName("StudioSaveStatusMenuHeader")
        self.headerAction.setEnabled(False)
        self.nowAction = menu.addAction(_("Autosave now"))
        self.nowAction.setObjectName("StudioSaveStatusAutosaveNowAction")
        self.nowAction.connect("triggered()", self.autosaveNow)
        self.recoverAction = menu.addAction(_("Recover autosaved work…"))
        self.recoverAction.setObjectName("StudioSaveStatusRecoverAction")
        self.recoverAction.connect("triggered()", showRecoveryDialog)
        self.folderAction = menu.addAction(_("Open autosave folder"))
        self.folderAction.setObjectName("StudioSaveStatusOpenFolderAction")
        self.folderAction.connect("triggered()", self.openFolder)
        menu.addSeparator()
        from . import projects_ui

        self.projectSeparator = menu.addSeparator()
        self.projectActions = projects_ui.addStatusMenuActions(menu)
        menu.addSeparator()
        self.settingsAction = menu.addAction(_("Saving settings…"))
        self.settingsAction.setObjectName("StudioSaveStatusSettingsAction")
        self.settingsAction.connect("triggered()", autosave.showAutosaveSettings)
        menu.connect("aboutToShow()", self._updateMenu)
        self._updateMenu()
        return menu

    def _updateMenu(self, status=None):
        service = self.service
        if status is None and service is not None:
            status = service.status()
        project = getattr(status, "projectName", None) if status is not None else None
        # In a project the menu is the project's; the recovery items are for scenes that are not projects
        for action in (self.nowAction, self.recoverAction, self.folderAction):
            action.setVisible(not project)
        self.projectSeparator.setVisible(not project)
        for name, action in getattr(self, "projectActions", {}).items():
            action.setVisible(bool(project) or name == "open")
        if project:
            when = time.strftime("%H:%M", time.localtime(status.lastSaveTime)) if status.lastSaveTime else ""
            header = _("Projects/{name}").format(name=project)
            self.headerAction.setText(_("{project} · saved {time}").format(project=header, time=when) if when else header)
            self.projectActions["save"].setEnabled(status.state != "saving")
            return
        autosaved = self._sceneAutosave(status) if status is not None else None
        if autosaved is not None:
            when, size = autosaved
            self.headerAction.setText(_("Last autosave {time} · {size}").format(time=_clock(when), size=autosave._formatBytes(size)))
        else:
            self.headerAction.setText(_("Nothing autosaved yet"))
        self.nowAction.setEnabled(service is not None and service.enabled())

    def autosaveNow(self):
        if self.service is not None:
            self.service.autosaveNow("menu")

    def folder(self):
        service = self.service
        if service is None:
            return None
        return service.sessionDirectory() or service.rootDirectory()

    def openFolder(self):
        path = self.folder()
        if path and os.path.isdir(path):
            qt.QDesktopServices.openUrl(qt.QUrl.fromLocalFile(path))

    #
    # Recovery
    #

    @staticmethod
    def _stillRecoverable(session):
        """False once a session was recovered or discarded (also from A-01's dialog, which scans again)."""
        if not session.presentable() or not os.path.isdir(session.path):
            return False
        from . import autosave_io

        data = autosave_io.readJson(os.path.join(session.path, autosave_io.SESSION_FILE)) or {}
        return data.get("state") not in ("recovered", "discarded")

    def presentRecovery(self, sessions, startup=True):
        """Presenter of the autosave service (``setRecoveryPresenter()``): the card in Library, a notice elsewhere."""
        sessions = [session for session in sessions if session.sessionId not in _dismissed and session.presentable()]
        if not sessions or self._disposed:
            return None
        self.closeCard()
        self.card = RecoveryCard(sessions, self)
        library.showNotice(NOTICE_KEY, self.card.widget)
        if self.shell.currentWorkspace() != "library":
            self.notice = ui.noticeLine(_("Unsaved work from a previous session can be recovered"), [(_("Show"), self.showCard)])
            self.notice.setObjectName("StudioRecoveryNotice")
            self.shell.setInspectorNotice(self.notice, key=NOTICE_KEY, workspace=None)
        return self.card

    def showCard(self):
        """'Show' of the inspector notice: Library, where the card is (entering Library clears the notice)."""
        self.shell.switchTo("library")
        self.shell.clearInspectorNotice(NOTICE_KEY)

    def closeCard(self):
        """Remove the card and the inspector notice that points to it."""
        self.shell.clearInspectorNotice(NOTICE_KEY)
        if self.notice is not None:
            if library._isAlive(self.notice):
                self.notice.deleteLater()
            self.notice = None
        card, self.card = self.card, None
        if card is not None:
            library.closeNotice(NOTICE_KEY)
            card.dispose()

    def later(self):
        """✕ of the card: not offered again in this run (File > Recover Autosaved Work… still lists them)."""
        if self.card is not None:
            _dismissed.update(session.sessionId for session in self.card.sessions)
        self.closeCard()

    def recover(self, slot):
        """Recover ``slot`` (or reopen its study at its subject), close the card and go back to its workspace."""
        if not self._stillRecoverable(slot.session):
            # Recovered or discarded meanwhile (for example from "Recover autosaved work…"): never load it twice
            self._refreshCard()
            return False
        handler = recoveryHandler(slot)
        try:
            if handler is not None:
                handler(slot)
                self.closeCard()
                return True
            report = _recovery().recover(slot, parent=slicer.util.mainWindow())
        except Exception:
            logging.error("Autosave: recovery failed:\n" + traceback.format_exc())
            return False
        if report is None:
            return False
        self.closeCard()
        if slot.workspace in RETURN_WORKSPACES and self.shell.currentWorkspace() != slot.workspace:
            self.shell.switchTo(slot.workspace)
        return True

    def _refreshCard(self):
        """Show the card again with the sessions that are still recoverable, or close it."""
        remaining = [session for session in self.card.sessions if self._stillRecoverable(session)] if self.card is not None else []
        if remaining:
            self.presentRecovery(remaining)
        else:
            self.closeCard()

    def discard(self, slot):
        """Discard the session of ``slot`` (A-01's confirmation); the card then shows the other sessions, or closes."""
        if not _recovery().discard(slot.session, parent=slicer.util.mainWindow()):
            return False
        remaining = [session for session in self.card.sessions if session is not slot.session] if self.card is not None else []
        if remaining:
            self.presentRecovery(remaining)
        else:
            self.closeCard()
        return True


def _recovery():
    from . import recovery

    return recovery


def showRecoveryDialog():
    """A-01's dialog with every autosaved session ('All autosaves…', 'Recover autosaved work…')."""
    return _recovery().showRecoveryDialog()


def recoveryHandler(slot):
    """The ``handle(slot)`` of the first recovery handler that can handle ``slot`` (W3-07: reopen the study), or None."""
    service = autosave.instance()
    if _ui is not None and _ui.service is not None:
        service = _ui.service
    if service is None:
        return None
    for canHandle, handle in service.recoveryHandlers():
        try:
            if canHandle(slot):
                return handle
        except Exception:
            logging.error("Autosave: recovery handler failed:\n" + traceback.format_exc())
    return None


def recoverText(slot):
    """'Recover', or 'Reopen study at sub-012' when a handler reopens the study of ``slot``."""
    if recoveryHandler(slot) is None:
        return _("Recover")
    study = slot.study if isinstance(slot.study, dict) else {}
    if study.get("subject"):
        return _("Reopen study at {subject}").format(subject=study["subject"])
    if study.get("name"):
        return _("Reopen study {name}").format(name=study["name"])
    return _("Recover")


def _label(text, objectName, pixelSize, color=None, weight=None):
    label = qt.QLabel(text)
    label.setObjectName(objectName)
    label.setTextFormat(qt.Qt.PlainText)
    label.setWordWrap(True)
    ui._font(label, ui.fontPx(pixelSize), weight)
    if color is not None:
        ui.bindStyleSheet(label, lambda: f"QLabel#{objectName} {{ color: {ui.token(color).name()}; background: transparent; }}")
    return label


def _button(text, objectName, callback, role):
    button = qt.QPushButton(text)
    button.setObjectName(objectName)
    button.setMinimumHeight(32 if role == "primary" else 28)
    ui.setRole(button, role)
    ui.installButtonKeys(button)
    button.connect("clicked()", callback)
    return button


class RecoveryCard:
    """The recovery offer at the top of the Library canvas (``StudioRecoveryCard``), styled like the download card.

    Up to ``MAX_CARD_ROWS`` autosaved scenes, newest first: thumbnail, "Today 14:32 · closed unexpectedly", the
    contents and size, and the study and subject when there was one. The buttons act on the newest scene: Recover
    (or the study handler's "Reopen study at <sid>"), Discard, and "All autosaves…" (A-01's dialog) when there are
    more. ✕ is "Later".
    """

    def __init__(self, sessions, owner):
        recovery = _recovery()
        self.owner = owner
        self.sessions = list(sessions)
        slots = [slot for session in self.sessions for slot in session.recoverableSlots()]
        slots.sort(key=lambda slot: recovery._parseTime(slot.updated), reverse=True)
        self.slots = slots
        self._tinted = []  # (label, icon name, size, colour token): re-tinted when the palette changes
        frame, layout = ui.card()
        frame.setObjectName("StudioRecoveryCard")
        frame.setFixedWidth(library.COLUMN_WIDTH)
        layout.setContentsMargins(12, 10, 8, 12)
        layout.setSpacing(8)
        self.widget = frame

        header = qt.QHBoxLayout()
        header.setSpacing(8)
        icon = qt.QLabel()
        icon.setObjectName("StudioRecoveryCardIcon")
        icon.setFixedSize(HEADER_ICON_SIZE, HEADER_ICON_SIZE)
        self._tint(icon, "archive-restore", HEADER_ICON_SIZE, "accent-fg")
        header.addWidget(icon, 0, qt.Qt.AlignVCenter)
        title = _label(_("Recover unsaved work"), "StudioRecoveryCardTitle", 13, weight=qt.QFont.DemiBold)
        header.addWidget(title, 1)
        self.laterButton = qt.QToolButton()
        self.laterButton.setObjectName("StudioRecoveryCardLaterButton")
        self.laterButton.setFixedSize(24, 24)
        self.laterButton.setIconSize(qt.QSize(14, 14))
        self.laterButton.setIcon(ui.icon("x", 14))
        self.laterButton.setAutoRaise(True)
        self.laterButton.setToolTip(_("Later: not now (File › Recover Autosaved Work… keeps them)"))  # noqa: RUF001
        ui.setRole(self.laterButton, "ghost")
        ui.installButtonKeys(self.laterButton)
        self.laterButton.connect("clicked()", owner.later)
        header.addWidget(self.laterButton, 0, qt.Qt.AlignTop)
        layout.addLayout(header)

        for slot in slots[:MAX_CARD_ROWS]:
            layout.addWidget(self._row(slot, recovery))

        buttons = qt.QHBoxLayout()
        buttons.setSpacing(8)
        newest = slots[0] if slots else None
        self.recoverButton = _button(recoverText(newest) if newest is not None else _("Recover"), "StudioRecoveryCardRecoverButton",
                                     lambda: owner.recover(newest) if newest is not None else None, "primary")
        self.recoverButton.setEnabled(newest is not None)
        buttons.addWidget(self.recoverButton)
        self.discardButton = _button(_("Discard"), "StudioRecoveryCardDiscardButton",
                                     lambda: owner.discard(newest) if newest is not None else None, "secondary")
        self.discardButton.setEnabled(newest is not None and newest.session.status not in ("running", "possibly-crashed"))
        if newest is not None and not self.discardButton.enabled:
            self.discardButton.setToolTip(_("This session may still be open in another Slicer"))
        buttons.addWidget(self.discardButton)
        buttons.addStretch(1)
        self.allButton = _button(_("All autosaves…"), "StudioRecoveryCardAllButton", showRecoveryDialog, "ghost")
        self.allButton.setVisible(len(slots) > 1)
        buttons.addWidget(self.allButton)
        layout.addLayout(buttons)
        ui.addPaletteListener(self._retint)

    def _tint(self, label, iconName, size, colorToken):
        label.setPixmap(ui.pixmap(iconName, size, colorToken))
        self._tinted.append((label, iconName, size, colorToken))

    def _retint(self):
        for label, iconName, size, colorToken in self._tinted:
            if library._isAlive(label):
                label.setPixmap(ui.pixmap(iconName, size, colorToken))

    def _row(self, slot, recovery):
        row = qt.QWidget()
        row.setObjectName("StudioRecoveryCardRow")
        row.setMinimumHeight(CARD_ROW_HEIGHT)
        rowLayout = qt.QHBoxLayout(row)
        rowLayout.setContentsMargins(0, 0, 0, 0)
        rowLayout.setSpacing(10)
        thumbnail = qt.QLabel()
        thumbnail.setObjectName("StudioRecoveryCardThumbnail")
        thumbnail.setFixedSize(THUMBNAIL_SIZE, THUMBNAIL_SIZE)
        thumbnail.setAlignment(qt.Qt.AlignCenter)
        pixmap = qt.QPixmap(slot.thumbnail) if slot.thumbnail else None
        if pixmap is not None and not pixmap.isNull():
            thumbnail.setPixmap(pixmap.scaled(THUMBNAIL_SIZE, THUMBNAIL_SIZE, qt.Qt.KeepAspectRatio, qt.Qt.SmoothTransformation))
        else:
            # A-01 never writes a thumbnail that shows a network map
            self._tint(thumbnail, "brain", 32, "text3")
        rowLayout.addWidget(thumbnail, 0, qt.Qt.AlignTop)
        lines = qt.QVBoxLayout()
        lines.setSpacing(2)
        first = _("{time} · {status}").format(time=recovery._shortTime(slot.updated), status=recovery._statusText(slot.status()))
        lines.addWidget(_label(first, "StudioRecoveryCardWhen", 13))
        second = f"{slot.contentsText()} · {recovery.formatBytes(slot.bytes)}"
        lines.addWidget(_label(second, "StudioRecoveryCardContents", 12, "text2"))
        study = slot.study if isinstance(slot.study, dict) else None
        if study and (study.get("name") or study.get("path")):
            name = study.get("name") or os.path.basename(str(study.get("path")))
            third = _("Study {name} · {subject}").format(name=name, subject=study["subject"]) if study.get("subject") else _("Study {name}").format(name=name)
            lines.addWidget(_label(third, "StudioRecoveryCardStudy", 12, "text2"))
        lines.addStretch(1)
        rowLayout.addLayout(lines, 1)
        return row

    def dispose(self):
        ui.removePaletteListener(self._retint)
        self._tinted = []
        widget = self.widget
        if not library._isAlive(widget):
            # Deleted with its parent (the Library canvas): ui drops the bindings of deleted labels itself
            return
        for label in widget.findChildren(qt.QLabel):
            ui.bindStyleSheet(label, None)
        widget.hide()
        widget.setObjectName("")
        widget.deleteLater()

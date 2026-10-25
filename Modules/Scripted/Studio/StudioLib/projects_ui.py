r"""Projects in the user interface of Slicer Studio (A-09, D-A16): the folder chooser, the Library's Projects section,
the save status and its menu, the crash offer, File menu and Search actions, and the Ctrl+S / Ctrl+N keys.

- **Folder chooser** (``StudioProjectsFolderDialog``): at the first start of Studio, before the tour and the other
  first-run cards (and after A-08's required-update dialog), "Where should Slicer Studio keep your work?" proposes
  ``Documents/Slicer Studio`` with "Choose folder…" and a warning for a cloud-synced folder. Escape or closing the dialog
  accepts the proposal: it never blocks the application. Never shown under ``--testing``, with
  ``SLICER_STUDIO_PROJECTS_ROOT`` or in the web build.
- **Library** (``ProjectsSection``, object name ``studioProjectsSection``): the projects of the folder, the most recently
  saved first, as cards (``studioProjectCard``: thumbnail, name, time, "1 scan · 1 lesion"). A click opens a project; the
  context menu has Open, Rename…, Duplicate, Show in Finder/Files and Move to Trash. The visual design is V-01's: the
  section is built with the ``ui`` builders and stable object names.
- **Save status** (top bar, through ``autosave_ui``): "Saved", "Saving…" or "Not saved · Retry"; its menu has Save now,
  Rename project…, Show project folder, New project, Open project… and Saving settings….
- **Crash offer** (``StudioProjectCrashCard`` in Library, a notice elsewhere, a non-modal message box in Classic):
  "Slicer Studio closed unexpectedly. Your project <name> was saved at <time>. [Open project]".
- **Keys (Studio)**: Ctrl+S saves the project now (without a project: the official Save dialog), Ctrl+N starts a new
  project. File menu (Classic and Studio, while a project is open): the project name, Save now, Save a copy as… (a .mrb
  anywhere), Show project folder, New project.

A Studio plug-in (``shell.OPTIONAL_PLUGINS``): ``install(shell)`` when Studio is applied, ``uninstall(shell)`` in Classic.
``attachService(service)`` is called by the autosave service in both modes.
"""

import logging
import os
import sys
import threading
import time
import traceback

import qt
import slicer
from slicer.i18n import tr as _

from . import projects, ui

#: Key of the crash offer in the Library notice slot and in the inspector notice slot
CRASH_NOTICE_KEY = "project-crash"
#: Projects listed in Library before "Show all"
MAX_PROJECT_ROWS = 6
PROJECT_ROW_HEIGHT = 56
THUMBNAIL_SIZE = 44
#: Refreshes of the Projects section after a save are grouped
REFRESH_DELAY_MS = 400

_ui = None
_fileActions = None
_startupDone = False
_chooser = None


def instance():
    """The installed Studio plug-in (``ProjectsUi``), or None (Classic)."""
    return _ui


def _service():
    from . import autosave

    return autosave.instance()


#: Going to Library saves the open project this long after the switch (the switch itself stays fast)
WORKSPACE_SAVE_DELAY_MS = 300
#: The workspaces whose entry saves the open project now: Library, where work is reviewed, opened and closed. Not Draw or
#: Map: the user starts editing there at once, and a save then would compete with the first strokes.
SAVE_ON_ENTER_WORKSPACES = ("library",)


def onWorkspaceEntered(key=None):
    """Going to Library saves the open project now (not after IdleSeconds), shortly after the switch (shell.py)."""
    if key is not None and key not in SAVE_ON_ENTER_WORKSPACES:
        return
    service = _service()
    if service is None or service.currentProject() is None:
        return

    def save():
        if service.currentProject() is not None:
            service.autosaveNow("workspace")

    qt.QTimer.singleShot(WORKSPACE_SAVE_DELAY_MS, save)


def _shell():

    module = sys.modules.get("StudioLib.shell")
    shell = module.instance() if module is not None else None
    return shell if shell is not None and shell.isActive() else None


def _isAlive(qobject):
    try:
        qobject.objectName
    except Exception:
        return False
    return qobject is not None


def _keyText(key):
    return qt.QKeySequence(key).toString(qt.QKeySequence.NativeText)


def _clock(seconds):
    return time.strftime("%H:%M", time.localtime(seconds)) if seconds else ""


def _relativeTime(seconds):
    try:
        from . import library

        return library.relativeTime(seconds)
    except Exception:
        return _clock(seconds)


def message(text, level="info", action=None, actionText=None, ms=3000, toolTip=None):
    """A toast in Studio, a status bar message in Classic."""
    shell = _shell()
    if shell is not None:
        shell.toast(text, ms=ms, action=action, actionText=actionText, level=level, toolTip=toolTip)
        return
    mainWindow = slicer.util.mainWindow()
    statusBar = mainWindow.statusBar() if mainWindow is not None else None
    if statusBar is not None:
        statusBar.showMessage(str(text), max(ms, 4000))
    if level in ("warning", "error"):
        logging.warning(str(text))


def _question(parent, title, text, buttons, default=0):
    """A message box with the button texts ``buttons``; return the index of the clicked one (-1: closed)."""
    box = qt.QMessageBox(parent or slicer.util.mainWindow())
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(qt.QMessageBox.Question)
    created = []
    for index, buttonText in enumerate(buttons):
        role = qt.QMessageBox.RejectRole if index == len(buttons) - 1 else qt.QMessageBox.AcceptRole
        created.append(box.addButton(buttonText, role))
    box.setDefaultButton(created[default])
    box.setEscapeButton(created[-1])
    run = getattr(box, "exec", None) or box.exec_
    run()
    clicked = box.clickedButton()
    for index, button in enumerate(created):
        if clicked == button:
            return index
    return -1


#
# Folder chooser (first start)
#


class FolderChooserDialog(qt.QDialog):
    """"Where should Slicer Studio keep your work?" (``StudioProjectsFolderDialog``). Closing it accepts the proposal."""

    def __init__(self, parent=None, proposal=None):
        qt.QDialog.__init__(self, parent or slicer.util.mainWindow())
        self.setObjectName("StudioProjectsFolderDialog")
        self.setWindowTitle(_("Slicer Studio folder"))
        self.setModal(True)
        self.path = os.path.normpath(proposal or projects.root())
        self.setMinimumWidth(560)
        layout = qt.QVBoxLayout(self)
        # Dialog chrome (VISUAL 4.7): 24 px padding, the icon tile beside the title and the body text
        layout.setContentsMargins(24, 24, 24, 22)
        layout.setSpacing(16)
        header = qt.QHBoxLayout()
        header.setSpacing(14)
        header.addWidget(ui.iconTile("folder", 40, 20, "StudioProjectsFolderIcon"), 0, qt.Qt.AlignTop)
        headerText = qt.QVBoxLayout()
        headerText.setSpacing(6)
        title = qt.QLabel(_("Where should Slicer Studio keep your work?"))
        title.setObjectName("StudioProjectsFolderTitle")
        title.setWordWrap(True)
        ui.setText(title, "title")
        headerText.addWidget(title)
        body = qt.QLabel(_("Everything you open, draw and compute is saved here automatically, so there is no Save to remember. "
                           "You can change this later in Settings › Saving."))  # noqa: RUF001
        body.setObjectName("StudioProjectsFolderBody")
        body.setWordWrap(True)
        headerText.addWidget(body)
        header.addLayout(headerText, 1)
        layout.addLayout(header)
        pathRow = qt.QHBoxLayout()
        pathRow.setSpacing(8)
        pathField = qt.QFrame()
        pathField.setObjectName("StudioProjectsFolderField")
        pathField.setMinimumHeight(36)
        fieldLayout = qt.QHBoxLayout(pathField)
        fieldLayout.setContentsMargins(10, 0, 10, 0)
        fieldLayout.setSpacing(8)
        self.pathIcon = qt.QLabel()
        self.pathIcon.setObjectName("StudioProjectsFolderPathIcon")
        self.pathIcon.setFixedSize(16, 16)
        fieldLayout.addWidget(self.pathIcon)
        self.pathLabel = qt.QLabel()
        self.pathLabel.setObjectName("StudioProjectsFolderPath")
        self.pathLabel.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
        self.pathLabel.setMinimumWidth(320)
        self.pathLabel.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        fieldLayout.addWidget(self.pathLabel, 1)
        pathRow.addWidget(pathField, 1)
        self.chooseButton = qt.QPushButton(_("Choose…"))
        self.chooseButton.setObjectName("StudioProjectsFolderChooseButton")
        self.chooseButton.setToolTip(_("Choose another folder"))
        self.chooseButton.setMinimumHeight(36)
        ui.setRole(self.chooseButton, "secondary")
        self.chooseButton.connect("clicked()", self.choose)
        pathRow.addWidget(self.chooseButton, 0, qt.Qt.AlignVCenter)
        layout.addLayout(pathRow)
        # A synced folder: a warning notice (one sentence)
        self.syncedNotice = ui.notice("", "warning", objectName="StudioProjectsFolderSyncedNotice")
        self.syncedLabel = self.syncedNotice.findChild("QLabel", "StudioNoticeBoxText")
        self.syncedLabel.setObjectName("StudioProjectsFolderSynced")
        layout.addWidget(self.syncedNotice)
        # What the folder holds
        inside = qt.QFrame()
        inside.setObjectName("StudioProjectsFolderInsideCard")
        insideLayout = qt.QVBoxLayout(inside)
        insideLayout.setContentsMargins(16, 12, 16, 12)
        insideLayout.setSpacing(8)
        insideLayout.addWidget(ui.overline(_("Inside this folder"), "StudioProjectsFolderInsideTitle"))
        self._insideIcons = []
        for iconName, name, what in (
                ("folder", _("Projects"), _("one folder per project: scans, lesions, maps, exports")),
                ("folder", _("Studies"), _("lesion network mapping cohorts")),
                ("folder", _("LNM"), _("analysis outputs not tied to a study"))):
            row = qt.QHBoxLayout()
            row.setSpacing(10)
            icon = qt.QLabel()
            icon.setFixedSize(16, 16)
            icon.setProperty("studioIconName", iconName)
            self._insideIcons.append(icon)
            row.addWidget(icon)
            nameLabel = ui.label(name, "name", "text")
            nameLabel.setFixedWidth(110)
            row.addWidget(nameLabel)
            row.addWidget(ui.label(what, "caption", "text3"), 1)
            insideLayout.addLayout(row)
        layout.addWidget(inside)
        self.insideLabel = qt.QLabel(_("Each project is a 3D Slicer scene that official 3D Slicer opens as well."))
        self.insideLabel.setObjectName("StudioProjectsFolderInside")
        self.insideLabel.setWordWrap(True)
        ui.setText(self.insideLabel, "caption")
        layout.addWidget(self.insideLabel)
        buttons = qt.QHBoxLayout()
        buttons.addStretch(1)
        self.continueButton = qt.QPushButton(_("Continue"))
        self.continueButton.setObjectName("StudioProjectsFolderContinueButton")
        self.continueButton.setDefault(True)
        self.continueButton.setAutoDefault(True)
        self.continueButton.setMinimumHeight(36)
        self.continueButton.setMinimumWidth(96)
        ui.setRole(self.continueButton, "primary")
        self.continueButton.connect("clicked()", self.accept)
        buttons.addWidget(self.continueButton)
        layout.addLayout(buttons)
        ui.bindStyleSheet(self, self._styleSheet)
        self._update()

    def _styleSheet(self):
        for icon in getattr(self, "_insideIcons", []):
            icon.setPixmap(ui.pixmap(str(icon.property("studioIconName")), 16, "accent-fg"))
        if hasattr(self, "pathIcon"):
            self.pathIcon.setPixmap(ui.pixmap("folder", 16, "text3"))
        return (f"QDialog#StudioProjectsFolderDialog {{ background-color: {ui.token('card').name()}; }}"
                f"QLabel#StudioProjectsFolderBody {{ color: {ui.token('text2').name()}; }}"
                f"QLabel#StudioProjectsFolderInside {{ color: {ui.token('text3').name()}; }}"
                f"QFrame#StudioProjectsFolderField {{ background-color: {ui.token('canvas').name()}; border: 1px solid {ui.token('border').name()};"
                " border-radius: 8px; }"
                "QFrame#StudioProjectsFolderField QLabel { background: transparent; border: none; }"
                f"QFrame#StudioProjectsFolderInsideCard {{ background-color: {ui.token('card-low').name()}; border: 1px solid {ui.token('hairline').name()};"
                " border-radius: 10px; }"
                "QFrame#StudioProjectsFolderInsideCard QLabel { background: transparent; border: none; }")

    def _update(self):
        self.pathLabel.setText(self.path)
        self.pathLabel.setToolTip(self.path)
        service = projects.syncedService(self.path)
        if service:
            self.syncedLabel.setText(_("This folder is synced to {service}. Scans can contain patient data: if they do, choose a folder that "
                                       "is not synced.").format(service=service))
            self.syncedLabel.show()
            self.syncedNotice.show()
        else:
            self.syncedLabel.setText("")
            self.syncedLabel.hide()
            self.syncedNotice.hide()

    def setPath(self, path):
        self.path = os.path.normpath(str(path))
        self._update()

    def choose(self):
        start = self.path if os.path.isdir(self.path) else os.path.dirname(self.path)
        folder = qt.QFileDialog.getExistingDirectory(self, _("Choose the Slicer Studio folder"), start)
        if folder:
            # A new, empty folder or an existing Slicer Studio folder is used as is; inside another folder, a
            # "Slicer Studio" folder is proposed so that the user's own files stay apart
            folder = os.path.normpath(folder)
            if os.path.basename(folder) != projects.ROOT_FOLDER_NAME and not os.path.isdir(os.path.join(folder, projects.PROJECTS_DIR)):
                try:
                    empty = not os.listdir(folder)
                except OSError:
                    empty = False
                if not empty:
                    folder = os.path.join(folder, projects.ROOT_FOLDER_NAME)
            self.setPath(folder)

    def reject(self):
        # Escape or closing the window accepts the proposal (the application is never blocked)
        self.accept()


def chooserNeeded():
    """True if the chooser must be shown now (first start of Studio)."""
    if slicer.app.testingEnabled() or slicer.util.mainWindow() is None:
        return False
    return projects.needsChooser()


def showChooser(then=None, parent=None):
    """Show the folder chooser (window-modal, not blocking the caller); ``then()`` runs once it was answered."""
    global _chooser
    if _chooser is not None and _isAlive(_chooser):
        return _chooser
    dialog = FolderChooserDialog(parent)
    _chooser = dialog

    def finished(result=None):
        global _chooser
        _chooser = None
        path = dialog.path
        try:
            projects.setRoot(path, chosen=True)
            projects.ensureRoot(path)
        except OSError as error:
            logging.error(f"Slicer Studio: the folder {path} cannot be created: {error}")
            message(_("The folder {path} cannot be created: {error}").format(path=path, error=error), level="warning", ms=8000)
        dialog.deleteLater()
        if then is not None:
            try:
                then()
            except Exception:
                logging.error("Slicer Studio: after the folder chooser:\n" + traceback.format_exc())

    dialog.connect("finished(int)", finished)
    dialog.setWindowModality(qt.Qt.WindowModal)
    dialog.open()
    return dialog


def onStartupCompleted(then=None):
    """Called by the Studio module when the startup may go on (after the update check): the chooser first when Studio
    starts for the first time, then ``then()`` (the autosave service, whose recovery offers come after the chooser).
    """
    global _startupDone
    _startupDone = True
    if chooserNeeded() and _shell() is not None:
        showChooser(then)
        return True
    if then is not None:
        then()
    return False


#
# Actions
#


def saveNow(shell=None):
    """Save now (Ctrl+S): the open project, else the official Save dialog. Return True if saved."""
    service = _service()
    project = service.currentProject() if service is not None else None
    if project is None:
        mainWindow = slicer.util.mainWindow()
        action = mainWindow.findChild(qt.QAction, "FileSaveSceneAction") if mainWindow is not None else None
        if action is not None:
            action.trigger()
        return False
    ok = service.saveNow("Ctrl+S", progress=True)
    if ok:
        message(_("Saved · {project}").format(project=project.relativeName()), level="info", action=showProjectFolder,
                actionText=_("Show"), toolTip=project.path)
    else:
        reason = service._projectPausedReason or _("see the error log")
        message(_("Not saved: {reason}").format(reason=reason), level="warning", action=saveNow, actionText=_("Retry"), ms=8000)
    return ok


def saveCopyAs(path=None):
    """Save a copy as… (Export): the whole scene as one .mrb file anywhere. The project stays where it is."""
    service = _service()
    mainWindow = slicer.util.mainWindow()
    project = service.currentProject() if service is not None else None
    if path is None:
        start = os.path.join(projects.exportsFolder(create=False) or os.path.expanduser("~"),
                             (project.name if project is not None else "Scene") + ".mrb")
        path = qt.QFileDialog.getSaveFileName(mainWindow, _("Save a copy as"), start, _("Slicer data bundle") + " (*.mrb)")
        if not path:
            return None
    if not path.lower().endswith(".mrb"):
        path += ".mrb"
    if project is not None:
        service.saveNow("copy", progress=True)
    scene = slicer.mrmlScene
    url, rootDirectory = scene.GetURL(), scene.GetRootDirectory()
    try:
        with slicer.util.WaitCursor():
            slicer.util.saveScene(path)
    finally:
        # Writing a .mrb points the scene at the bundle: the scene stays in its project
        scene.SetURL(url)
        scene.SetRootDirectory(rootDirectory)
    if os.path.isfile(path):
        message(_("Saved a copy as {name}").format(name=os.path.basename(path)), action=lambda: projects.showInFileManager(os.path.dirname(path)),
                actionText=_("Show"), toolTip=path)
        return path
    message(_("The copy could not be saved (see the error log)"), level="warning")
    return None


def showProjectFolder():
    """Show the open project's folder in Finder, Explorer or Files (else the Slicer Studio folder)."""
    project = projects.currentProject()
    path = project.path if project is not None else projects.root()
    if not os.path.isdir(path):
        projects.ensureRoot()
    return projects.showInFileManager(path)


def showRootFolder():
    root = projects.root()
    if not os.path.isdir(root):
        projects.ensureRoot(root)
    return projects.showInFileManager(root)


def newProject():
    """New project (Ctrl+N): the open project is saved and closed; the next data opened or drawn starts the next one."""
    service = _service()
    if service is None:
        return False
    service.newProject()
    return True


def closeScene():
    """Close scene in Studio (Ctrl+W, File > Close Scene, Search): with a project open, it is saved first and closed (as New
    project), so 3D Slicer does not ask "Save before closing scene?" about saved work; else the official action.
    """
    service = _service()
    if service is not None and service.currentProject() is not None:
        service.newProject()
        return True
    mainWindow = slicer.util.mainWindow()
    action = mainWindow.findChild(qt.QAction, "FileCloseSceneAction") if mainWindow is not None else None
    if action is None:
        return False
    action.trigger()
    return True


class _CloseSceneMenuFilter(qt.QObject):
    """File > Close Scene in Studio while a project is open: runs ``closeScene()`` instead of the official handler."""

    def eventFilter(self, menu, event):
        eventType = event.type()
        if eventType == qt.QEvent.MouseButtonRelease and event.button() == qt.Qt.LeftButton:
            action = menu.actionAt(event.pos())
        elif eventType == qt.QEvent.KeyPress and event.key() in (qt.Qt.Key_Return, qt.Qt.Key_Enter):
            action = menu.activeAction()
        else:
            return False
        if action is None or action.property("objectName") != "FileCloseSceneAction" or not action.isEnabled():
            return False
        service = _service()
        if service is None or service.currentProject() is None:
            return False
        for _attempt in range(5):
            popup = qt.QApplication.activePopupWidget()
            if popup is None:
                break
            popup.close()
        qt.QTimer.singleShot(0, closeScene)
        return True


def openProject(path, shell=None):
    """Open a project folder: the open project is saved first; in Studio the project is shown in Library (Continue in Draw
    goes on), and opening the open project again goes to Draw.
    """
    service = _service()
    if service is None:
        return False
    shell = shell or _shell()
    if service.currentProject() is not None and os.path.normcase(os.path.realpath(service.currentProject().path)) == os.path.normcase(os.path.realpath(path)):
        if shell is not None:
            shell.switchTo("draw")
        return True
    with slicer.util.WaitCursor():
        opened = service.openProject(path)
    if opened and shell is not None and shell.currentWorkspace() != "library":
        # The project is shown in Library (its Current work card has "Continue in Draw")
        shell.switchTo("library")
    return opened


def openProjectDialog():
    """Open project…: choose a project folder (or its .mrml) in Projects/."""
    folder = projects.projectsFolder()
    start = folder if os.path.isdir(folder) else projects.root()
    path = qt.QFileDialog.getExistingDirectory(slicer.util.mainWindow(), _("Open project"), start)
    if not path:
        return False
    if projects.sceneFile(path) is None:
        slicer.util.errorDisplay(_("{folder} is not a project: it has no scene file (.mrml).").format(folder=path))
        return False
    return openProject(path)


def renameDialog(path=None, parent=None):
    """Rename…: the open project (default) or the project folder ``path``. Return the new folder, or None."""
    project = projects.currentProject()
    if path is None:
        if project is None:
            return None
        path = project.path
    current = project is not None and os.path.normcase(os.path.realpath(project.path)) == os.path.normcase(os.path.realpath(path))
    name = os.path.basename(os.path.normpath(path))
    newName = qt.QInputDialog.getText(parent or slicer.util.mainWindow(), _("Rename project"), _("Name:"), qt.QLineEdit.Normal, name)
    if isinstance(newName, tuple):
        newName = newName[0]
    newName = projects.sanitizeName(newName) if newName and str(newName).strip() else ""
    if not newName or newName == name:
        return None
    try:
        with slicer.util.WaitCursor():
            if current:
                newPath = _service().renameProject(newName)
            else:
                newPath = projects.renameFolder(path, newName)
    except (projects.ProjectError, OSError) as error:
        slicer.util.errorDisplay(str(error))
        return None
    projects.notifyChanged()
    message(_("Renamed to {name}").format(name=os.path.basename(newPath)))
    return newPath


def duplicate(path):
    def done(newPath):
        projects.notifyChanged()
        message(_("Duplicated as {name}").format(name=os.path.basename(newPath)))

    def failed(error):
        message(_("The project could not be duplicated: {error}").format(error=error), level="warning", ms=8000)

    projects.duplicateFolder(path, onDone=done, onError=failed)


def moveToTrash(path, parent=None):
    name = os.path.basename(os.path.normpath(path))
    if _question(parent, _("Move to Trash"), _("Move the project {name} to the Trash?").format(name=name), [_("Move to Trash"), _("Cancel")]) != 0:
        return False

    def confirmDelete():
        return _question(parent, _("Delete project"), _("This system has no Trash. Delete the project {name} permanently?").format(name=name),
                         [_("Delete"), _("Cancel")], default=1) == 0

    try:
        gone = projects.trashFolder(path, confirmDelete)
    except (projects.ProjectError, OSError) as error:
        slicer.util.errorDisplay(str(error))
        return False
    if gone:
        projects.notifyChanged()
        message(_("Moved {name} to the Trash").format(name=name))
    return gone


def showSavingSettings():
    from . import autosave

    autosave.showAutosaveSettings()


def _runWithProgress(function, text, parent=None):
    """Run ``function()`` in a thread while a progress dialog is shown (the user interface stays painted). Return its result."""
    result = {}

    def run():
        try:
            result["value"] = function()
        except BaseException as error:
            result["error"] = error

    thread = threading.Thread(target=run, name="StudioProjectsMove", daemon=True)
    dialog = qt.QProgressDialog(text, "", 0, 0, parent or slicer.util.mainWindow())
    dialog.setObjectName("StudioProjectsMoveProgress")
    dialog.setCancelButton(None)
    dialog.setMinimumDuration(500)
    dialog.setWindowModality(qt.Qt.ApplicationModal)
    thread.start()
    try:
        while thread.is_alive():
            slicer.app.processEvents(qt.QEventLoop.ExcludeUserInputEvents)
            thread.join(0.02)
    finally:
        dialog.close()
        dialog.deleteLater()
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _studyOpen():
    module = sys.modules.get("StudioLib.study_ui")
    try:
        return module is not None and module.session() is not None
    except Exception:
        return False


def changeRootDialog(parent=None):
    """Change… of Settings › Saving: choose another folder; offer to move the projects and studies there."""  # noqa: RUF002
    if projects.isRootFromEnvironment():
        slicer.util.errorDisplay(_("The folder is set by the {variable} environment variable.").format(variable=projects.ROOT_ENVIRONMENT))
        return None
    oldRoot = projects.root()
    dialog = FolderChooserDialog(parent, proposal=oldRoot)
    dialog.setWindowTitle(_("Change the Slicer Studio folder"))
    dialog.continueButton.setText(_("Use this folder"))
    dialog.choose()
    run = getattr(dialog, "exec", None) or dialog.exec_
    run()
    newRoot = dialog.path
    dialog.deleteLater()
    if os.path.normcase(os.path.realpath(newRoot)) == os.path.normcase(os.path.realpath(oldRoot)):
        return None
    move = False
    if projects.hasContent(oldRoot):
        if _studyOpen():
            slicer.util.infoDisplay(_("Close the open study first: its files are in the Slicer Studio folder."),
                                    windowTitle=_("Change the Slicer Studio folder"))
            return None
        answer = _question(parent, _("Change the Slicer Studio folder"),
                           _("Move your projects and studies to the new folder?\n\n{old}\n→ {new}").format(old=oldRoot, new=newRoot),
                           [_("Move"), _("Keep them where they are"), _("Cancel")])
        if answer in (-1, 2):
            return None
        move = answer == 0
    service = _service()
    try:
        if service is not None:
            moved = service.moveRoot(newRoot, moveContent=move,
                                     run=lambda function: _runWithProgress(function, _("Moving your projects and studies…"), parent))
        else:
            moved = _runWithProgress(lambda: projects.moveContent(oldRoot, newRoot), _("Moving your projects and studies…"), parent) if move else []
            projects.setRoot(newRoot)
        projects.ensureRoot(newRoot)
    except OSError as error:
        slicer.util.errorDisplay(_("Your projects could not all be moved: {error}\nWhat was moved is in the new folder; the folder setting is "
                                   "unchanged.").format(error=error))
        projects.notifyChanged()
        return None
    projects.notifyChanged()
    if moved:
        message(_("Moved to {folder}").format(folder=projects.shortPath(newRoot)), toolTip=newRoot)
    return newRoot


#
# Library: the Projects section
#


class _ClickFilter(qt.QObject):
    """Left click opens a project card; the context menu shows its actions. A resized card elides its texts, and a
    resized section lays its cards out again (the number of columns follows the width).
    """

    def __init__(self, section):
        qt.QObject.__init__(self)
        self.section = section

    def eventFilter(self, obj, event):
        eventType = event.type()
        if eventType == qt.QEvent.Resize:
            if obj == self.section.widget:
                self.section.updateColumns()
            else:
                self.section.elideCard(obj)
            return False
        if eventType == qt.QEvent.MouseButtonRelease and event.button() == qt.Qt.LeftButton:
            path = obj.property("studioProjectPath")
            if path:
                qt.QTimer.singleShot(0, lambda path=path: self.section.open(path))
                return True
        if eventType == qt.QEvent.KeyPress and event.key() in (qt.Qt.Key_Return, qt.Qt.Key_Enter, qt.Qt.Key_Space):
            path = obj.property("studioProjectPath")
            if path:
                qt.QTimer.singleShot(0, lambda path=path: self.section.open(path))
                return True
        return False


#: Project cards (VISUAL 4.2): a 128 px black thumbnail and a 52 px body; 4 columns from 1000 px, else 3 (2 when narrow)
CARD_THUMBNAIL_HEIGHT = 128
CARD_BODY_HEIGHT = 54
CARD_SPACING = 16
CARD_MIN_WIDTH = 180
#: Rows of cards shown before "Show all"
CARD_ROWS = 2


def projectColumns(width):
    """Columns of the project grid for a section ``width`` px wide."""
    if width >= 1000:
        return 4
    if width >= 3 * CARD_MIN_WIDTH + 2 * CARD_SPACING:
        return 3
    return 2


class ProjectsSection(qt.QObject):
    """The projects of the Slicer Studio folder in the Library page (``studioProjectsSection``): "All projects" and its
    count, then a grid of cards (``studioProjectCard``) with the thumbnail, the name and "2 h ago · 1 scan · 1 lesion";
    the project that is open is marked (``studioCurrent``) and has an "Open" badge.
    """

    def __init__(self):
        qt.QObject.__init__(self)
        self.widget = qt.QFrame()
        self.widget.setObjectName("studioProjectsSection")
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 0, 0, 28)
        layout.setSpacing(10)
        header = qt.QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        self.title = qt.QLabel(_("All projects"))
        self.title.setObjectName("StudioLibrarySectionTitle")
        ui.setText(self.title, "section")
        header.addWidget(self.title)
        self.countLabel = ui.label("", "caption", "text3", "studioProjectsCount")
        header.addWidget(self.countLabel, 0, qt.Qt.AlignBaseline if hasattr(qt.Qt, "AlignBaseline") else qt.Qt.AlignVCenter)
        header.addStretch(1)
        self.showAllButton = self._link(_("Show all"), "studioProjectsShowAll", self.toggleAll)
        header.addWidget(self.showAllButton)
        layout.addLayout(header)
        # Where the projects are is said by the page header (``library.LibraryCanvas``); these stay for other callers
        self.caption = qt.QLabel()
        self.caption.setObjectName("studioProjectsCaption")
        self.caption.setTextFormat(qt.Qt.PlainText)
        self.caption.hide()
        self.showFolderButton = self._link(_("Show in {manager}").format(manager=projects.fileManagerName()), "studioProjectsShowFolder", showRootFolder)
        self.showFolderButton.hide()
        self.grid = qt.QGridLayout()
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(CARD_SPACING)
        self.grid.setVerticalSpacing(CARD_SPACING)
        layout.addLayout(self.grid)
        self.listLayout = self.grid
        self.emptyLabel = qt.QLabel(_("Open a scan or start on the template: your work is saved here as a project."))
        self.emptyLabel.setObjectName("studioProjectsEmpty")
        self.emptyLabel.setWordWrap(True)
        ui.setText(self.emptyLabel, "caption")
        layout.addWidget(self.emptyLabel)
        self._showAll = False
        self._signature = None
        self._cards = []
        self._columns = 0
        self._infos = []
        self._clickFilter = _ClickFilter(self)
        self.widget.installEventFilter(self._clickFilter)
        # The Python event filter must be gone before the interpreter is finalized (the section may outlive the shell, a
        # Library canvas made without it): the widgets deleted then would send it events
        slicer.app.connect("aboutToQuit()", self._removeEventFilters)
        self._pixmaps = {}
        self._timer = qt.QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(REFRESH_DELAY_MS)
        self._timer.connect("timeout()", self.refresh)
        projects.addListener(self.scheduleRefresh)
        ui.bindStyleSheet(self.widget, self._styleSheet)
        self.refresh()

    @staticmethod
    def _link(text, objectName, callback):
        button = qt.QPushButton(text)
        button.setObjectName(objectName)
        button.setProperty("studioLibraryLink", True)
        button.setFlat(True)
        button.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
        button.connect("clicked()", callback)
        ui.installButtonKeys(button)
        return button

    @staticmethod
    def _styleSheet():
        t = {name: ui.token(name).name() for name in ("text", "text2", "text3", "accent", "accent-fg", "card-low", "hairline")}
        return (
            "QFrame#studioProjectsSection { background: transparent; border: none; }"
            f"QLabel#StudioLibrarySectionTitle {{ color: {t['text']}; }}"
            f"QLabel#studioProjectsCaption, QLabel#studioProjectsEmpty {{ color: {t['text3']}; }}"
            f"QFrame#studioProjectCard {{ background-color: {t['card-low']}; border: 1px solid {t['hairline']}; border-radius: 12px; }}"
            f"QFrame#studioProjectCard:hover {{ border-color: {t['text3']}; }}"
            f"QFrame#studioProjectCard:focus {{ border: 2px solid {t['accent-fg']}; }}"
            f"QFrame#studioProjectCard[studioCurrent=\"true\"] {{ border: 2px solid {t['accent']}; }}"
            f"QLabel#studioProjectCardName {{ color: {t['text']}; background: transparent; border: none; }}"
            f"QLabel#studioProjectCardMeta {{ color: {t['text3']}; background: transparent; border: none; }}"
            "QLabel#studioProjectCardThumbnail { background-color: #000000; border: none; border-top-left-radius: 11px; border-top-right-radius: 11px; }"
            "QLabel#studioProjectCardBadge { background-color: rgba(22, 22, 24, 199); color: #ececef; border: none; border-radius: 5px; padding: 0px; }"
            f"QPushButton[studioLibraryLink=\"true\"] {{ background: transparent; border: none; color: {t['accent-fg']}; padding: 2px 4px; }}")

    def scheduleRefresh(self):
        if _isAlive(self.widget):
            self._timer.start()

    def toggleAll(self):
        self._showAll = not self._showAll
        self._signature = None
        self.refresh()

    def captionText(self):
        root = projects.root()
        return _("Saved automatically in {folder}").format(folder=projects.shortPath(root))

    def currentInfo(self):
        """The ``projects.ProjectInfo`` of the open project among the listed ones, or None."""
        return next((info for info in self._infos if info.isCurrent()), None)

    def _shownCount(self, total):
        columns = max(self._columns, projectColumns(self.widget.width if self.widget.width > 0 else 1000))
        return total if self._showAll else min(total, CARD_ROWS * columns)

    def refresh(self):
        """List the projects again (only what changed is rebuilt)."""
        if not _isAlive(self.widget):
            return
        enabled = projects.isEnabled()
        self.widget.setVisible(enabled)
        if not enabled:
            return
        self.caption.setText(self.captionText())
        self.caption.setToolTip(projects.root())
        infos = projects.listProjects()
        self._infos = infos
        shown = infos[:self._shownCount(len(infos))]
        signature = tuple((info.path, info.modified, info.isCurrent(), info.thumbnail, tuple(sorted(info.counts.items()))) for info in shown)
        self.countLabel.setText(str(len(infos)) if infos else "")
        self.showAllButton.setVisible(len(infos) > len(shown) or self._showAll)
        self.showAllButton.setText(_("Show fewer") if self._showAll else _("Show all ({count})").format(count=len(infos)))
        self.emptyLabel.setVisible(not infos)
        if signature == self._signature:
            return
        self._signature = signature
        for card in self._cards:
            self.grid.removeWidget(card)
            card.hide()
            card.setObjectName("")
            card.deleteLater()
        self._cards = []
        for info in shown:
            self._cards.append(self._card(info))
        self._placeCards(force=True)

    def updateColumns(self):
        """Lay the cards out again when the width calls for another number of columns."""
        if projectColumns(self.widget.width) != self._columns:
            self._signature = None
            self.refresh()

    def _placeCards(self, force=False):
        columns = projectColumns(self.widget.width if self.widget.width > 0 else 1000)
        if columns == self._columns and not force:
            return
        self._columns = columns
        for card in self._cards:
            self.grid.removeWidget(card)
        for index, card in enumerate(self._cards):
            self.grid.addWidget(card, index // columns, index % columns)
        for column in range(4):
            self.grid.setColumnStretch(column, 1 if column < columns else 0)

    def _thumbnail(self, info):
        if not info.thumbnail:
            return None
        try:
            key = (info.thumbnail, os.path.getmtime(info.thumbnail))
        except OSError:
            return None
        pixmap = self._pixmaps.get(key)
        if pixmap is None:
            pixmap = qt.QPixmap(info.thumbnail)
            if pixmap.isNull():
                return None
            size = CARD_THUMBNAIL_HEIGHT - 8
            pixmap = pixmap.scaled(size, size, qt.Qt.KeepAspectRatio, qt.Qt.SmoothTransformation)
            self._pixmaps[key] = pixmap
        return pixmap

    def _card(self, info):
        card = qt.QFrame()
        card.setObjectName("studioProjectCard")
        card.setProperty("studioProjectPath", info.path)
        card.setProperty("studioCurrent", info.isCurrent())
        card.setFocusPolicy(qt.Qt.TabFocus)
        card.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
        card.setFixedHeight(CARD_THUMBNAIL_HEIGHT + CARD_BODY_HEIGHT)
        card.setMinimumWidth(CARD_MIN_WIDTH)
        card.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Fixed)
        card.setContextMenuPolicy(qt.Qt.CustomContextMenu)
        card.connect("customContextMenuRequested(QPoint)", lambda point, card=card, path=info.path: self.showMenu(card, path, point))
        card.installEventFilter(self._clickFilter)
        layout = qt.QVBoxLayout(card)
        # Inside the 1 px border (2 px for the open project)
        inset = 2 if info.isCurrent() else 1
        layout.setContentsMargins(inset, inset, inset, inset)
        layout.setSpacing(0)
        thumbnail = qt.QLabel()
        thumbnail.setObjectName("studioProjectCardThumbnail")
        thumbnail.setFixedHeight(CARD_THUMBNAIL_HEIGHT - inset)
        thumbnail.setAlignment(qt.Qt.AlignCenter)
        pixmap = self._thumbnail(info)
        thumbnail.setPixmap(pixmap if pixmap is not None else ui.pixmap("brain", 32, "#55555c"))
        layout.addWidget(thumbnail)
        badgeText = _("Open") if info.isCurrent() else (_("Map ready") if info.counts.get("maps") else "")
        if badgeText:
            badge = qt.QLabel(badgeText, thumbnail)
            badge.setObjectName("studioProjectCardBadge")
            badge.setAlignment(qt.Qt.AlignCenter)
            ui.setText(badge, "small")
            # The size of the text and the stylesheet padding (6 px on each side)
            badge.setFixedSize(qt.QFontMetrics(badge.font).horizontalAdvance(badgeText) + 14, qt.QFontMetrics(badge.font).height() + 4)
            badge.move(8, 8)
            badge.setAttribute(qt.Qt.WA_TransparentForMouseEvents)
        body = qt.QWidget()
        body.setObjectName("studioProjectCardBody")
        bodyLayout = qt.QVBoxLayout(body)
        bodyLayout.setContentsMargins(12, 8, 12, 9)
        bodyLayout.setSpacing(2)
        name = qt.QLabel(info.name)
        name.setObjectName("studioProjectCardName")
        name.setTextFormat(qt.Qt.PlainText)
        name.setProperty("fullText", info.name)
        name.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        ui.setText(name, "name")
        bodyLayout.addWidget(name)
        when = _relativeTime(info.modified) if info.modified else ""
        meta = " · ".join(part for part in (when, info.contentLine()) if part)
        metaLabel = qt.QLabel(meta)
        metaLabel.setObjectName("studioProjectCardMeta")
        metaLabel.setTextFormat(qt.Qt.PlainText)
        metaLabel.setProperty("fullText", meta)
        metaLabel.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        ui.setText(metaLabel, "caption")
        bodyLayout.addWidget(metaLabel)
        layout.addWidget(body)
        for widget in (thumbnail, body, name, metaLabel):
            widget.setAttribute(qt.Qt.WA_TransparentForMouseEvents)
        card.setToolTip(info.path)
        card.setAccessibleName(_("Project {name}").format(name=info.name))
        return card

    def elideCard(self, card):
        """Elide the name and the meta line of a card to its width (the full texts are the ``fullText`` properties)."""
        for label in card.findChildren("QLabel"):
            text = label.property("fullText")
            if text is None:
                continue
            width = max(0, card.width - 26)
            elided = qt.QFontMetrics(label.font).elidedText(str(text), qt.Qt.ElideRight, width)
            if label.text != elided:
                label.setText(elided)

    def cards(self):
        return [card for card in self._cards if _isAlive(card)]

    def open(self, path):
        openProject(path)
        self._signature = None
        self.scheduleRefresh()

    def menuActions(self, path):
        """``[(text, callback)]`` of a project card's context menu."""
        return [
            (_("Open"), lambda: self.open(path)),
            (_("Rename…"), lambda: renameDialog(path)),
            (_("Duplicate"), lambda: duplicate(path)),
            (_("Show in {manager}").format(manager=projects.fileManagerName()), lambda: projects.showInFileManager(path)),
            (_("Move to Trash"), lambda: moveToTrash(path)),
        ]

    def showMenu(self, card, path, point):
        menu = qt.QMenu(card)
        menu.setObjectName("studioProjectCardMenu")
        for text, callback in self.menuActions(path):
            action = menu.addAction(text)
            action.connect("triggered()", callback)
        run = getattr(menu, "exec", None) or menu.exec_
        run(card.mapToGlobal(point))
        menu.deleteLater()

    def focusFirst(self):
        cards = self.cards()
        if cards:
            cards[0].setFocus(qt.Qt.OtherFocusReason)
            return True
        return False

    def _removeEventFilters(self):
        for watched in [self.widget, *self._cards]:
            if _isAlive(watched):
                watched.removeEventFilter(self._clickFilter)

    def dispose(self):
        projects.removeListener(self.scheduleRefresh)
        self._timer.stop()
        try:
            slicer.app.disconnect("aboutToQuit()", self._removeEventFilters)
        except Exception:
            pass
        self._removeEventFilters()
        if _isAlive(self.widget):
            ui.bindStyleSheet(self.widget, None)


#
# Crash offer
#


class CrashCard:
    """"Slicer Studio closed unexpectedly. Your project <name> was saved at <time>. [Open project]" (Library notice)."""

    def __init__(self, infos, owner):
        self.infos = list(infos)
        self.owner = owner
        frame, layout = ui.card()
        frame.setObjectName("StudioProjectCrashCard")
        layout.setContentsMargins(12, 10, 8, 12)
        layout.setSpacing(8)
        self.widget = frame
        header = qt.QHBoxLayout()
        title = qt.QLabel(_("Slicer Studio closed unexpectedly"))
        title.setObjectName("StudioProjectCrashCardTitle")
        title.setWordWrap(True)
        ui._font(title, ui.fontPx(13), qt.QFont.DemiBold)
        header.addWidget(title, 1)
        self.laterButton = qt.QToolButton()
        self.laterButton.setObjectName("StudioProjectCrashCardLaterButton")
        self.laterButton.setIcon(ui.icon("x", 14))
        self.laterButton.setAutoRaise(True)
        self.laterButton.setToolTip(_("Later (the project is in Library › Projects)"))  # noqa: RUF001
        ui.setRole(self.laterButton, "ghost")
        self.laterButton.connect("clicked()", owner.closeCrashOffer)
        header.addWidget(self.laterButton, 0, qt.Qt.AlignTop)
        layout.addLayout(header)
        self.openButtons = []
        for info in self.infos[:3]:
            row = qt.QHBoxLayout()
            saved = projects.parseTime(info.data.get("savedAt")) or info.modified
            text = qt.QLabel(_("Your project {name} was saved at {time}.").format(name=info.name, time=_clock(saved) or _("an earlier time")))
            text.setObjectName("StudioProjectCrashCardText")
            text.setWordWrap(True)
            row.addWidget(text, 1)
            button = qt.QPushButton(_("Open project"))
            button.setObjectName("StudioProjectCrashCardOpenButton")
            ui.setRole(button, "primary" if not self.openButtons else "secondary")
            ui.installButtonKeys(button)
            button.connect("clicked()", lambda path=info.path: owner.openCrashed(path))
            row.addWidget(button, 0, qt.Qt.AlignVCenter)
            self.openButtons.append(button)
            layout.addLayout(row)

    def dispose(self):
        if _isAlive(self.widget):
            self.widget.hide()
            self.widget.setObjectName("")
            self.widget.deleteLater()


_crashBox = None


def showCrashOfferDialog(infos):
    """The crash offer outside Studio: a non-modal message box with "Open project" (Classic)."""
    global _crashBox
    if not infos or slicer.util.mainWindow() is None:
        return None
    info = infos[0]
    saved = projects.parseTime(info.data.get("savedAt")) or info.modified
    box = qt.QMessageBox(slicer.util.mainWindow())
    box.setObjectName("StudioProjectCrashDialog")
    box.setWindowTitle(_("Slicer Studio closed unexpectedly"))
    box.setIcon(qt.QMessageBox.Information)
    box.setText(_("Slicer Studio closed unexpectedly. Your project {name} was saved at {time}.").format(name=info.name, time=_clock(saved)))
    openButton = box.addButton(_("Open project"), qt.QMessageBox.AcceptRole)
    box.addButton(_("Close"), qt.QMessageBox.RejectRole)
    box.setWindowModality(qt.Qt.NonModal)
    box.setAttribute(qt.Qt.WA_DeleteOnClose, True)

    def clicked(button):
        if button == openButton:
            openProject(info.path)

    box.connect("buttonClicked(QAbstractButton*)", clicked)
    box.show()
    _crashBox = box
    return box


#
# Save status (top bar, through autosave_ui)
#


def projectStatus(status):
    """``(text, level, toolTip, iconName)`` of the top-bar status of the open project, or None without a project."""
    if not getattr(status, "projectName", None):
        return None
    name = f"{projects.PROJECTS_DIR}/{status.projectName}"
    folder = projects.shortPath(projects.root())
    if status.state in ("error", "paused"):
        reason = status.message or _("see the error log")
        return _("Not saved · Retry"), "warn", _("{reason}\nClick Save now in this menu, or press {key}.").format(reason=reason, key=_keyText("Ctrl+S")), None
    if status.state == "saving" or not status.lastSaveTime:
        return _("Saving…"), "busy", _("Saving {project} in {folder}").format(project=name, folder=folder), None
    when = _clock(status.lastSaveTime) if status.lastSaveTime else ""
    if status.dirty and status.lastSaveTime:
        tip = _("{project} in {folder}. Saved {time}; newer changes are saved in a moment ({key} saves now).")
    elif status.lastSaveTime:
        tip = _("{project} in {folder}. Saved {time}. Everything is saved automatically ({key} saves now).")
    else:
        tip = _("{project} in {folder}. Everything is saved automatically ({key} saves now).")
    return _("Saved"), "ok", tip.format(project=name, folder=folder, time=when, key=_keyText("Ctrl+S")), None


def addStatusMenuActions(menu, owner=None):
    """Add the project actions to the save status menu; return {name: action} (hidden without a project)."""
    actions = {}
    for name, text, callback, objectName in (
        ("save", _("Save now") + "\t" + _keyText("Ctrl+S"), saveNow, "StudioSaveStatusSaveNowAction"),
        ("rename", _("Rename project…"), renameDialog, "StudioSaveStatusRenameAction"),
        ("folder", _("Show project folder"), showProjectFolder, "StudioSaveStatusShowProjectAction"),
        ("copy", _("Save a copy as…"), saveCopyAs, "StudioSaveStatusSaveCopyAction"),
        ("new", _("New project") + "\t" + _keyText("Ctrl+N"), newProject, "StudioSaveStatusNewProjectAction"),
        ("open", _("Open project…"), openProjectDialog, "StudioSaveStatusOpenProjectAction"),
    ):
        action = menu.addAction(text)
        action.setObjectName(objectName)
        action.connect("triggered()", lambda callback=callback: callback())
        actions[name] = action
    return actions


#
# Search (palette) entries
#


def paletteItems(shell=None):
    project = projects.currentProject()
    items = [
        {"id": "projects.new", "title": _("New project"), "category": "File", "callback": newProject, "key": "Ctrl+N",
         "synonyms": ["new", "project", "new scene", "start over", "close project"]},
        {"id": "projects.open", "title": _("Open project…"), "category": "File", "callback": openProjectDialog,
         "synonyms": ["open", "project", "projects", "reopen"]},
        {"id": "projects.showFolder", "title": _("Show project folder"), "category": "File", "callback": showProjectFolder,
         "synonyms": ["where are my files", "finder", "explorer", "files", "folder", "reveal", "project folder"]},
        {"id": "projects.changeFolder", "title": _("Change Slicer Studio folder…"), "category": "File", "callback": showSavingSettings,
         "synonyms": ["folder", "location", "where", "root", "documents", "move projects", "saving"]},
        {"id": "projects.saveCopy", "title": _("Save a copy as…"), "category": "File", "callback": saveCopyAs,
         "synonyms": ["export", "mrb", "save as", "copy", "bundle", "share"]},
    ]
    if project is not None:
        items.insert(1, {"id": "projects.rename", "title": _("Rename project"), "category": "File", "callback": renameDialog,
                         "synonyms": ["rename", "project name", "name"]})
    return items


def registerPaletteProvider():
    try:
        from . import palette
    except Exception:
        return False
    if not hasattr(palette, "registerProvider"):
        return False
    palette.registerProvider(paletteItems)
    return True


#
# File menu (Classic and Studio)
#


class FileMenuActions:
    """Project items at the top of the File menu while a project is open: its name, Save now, Save a copy as…, Show
    project folder and New project (the official items keep their place and keys).
    """

    def __init__(self, mainWindow):
        self.menu = mainWindow.findChild(qt.QMenu, "FileMenu")
        self.actions = []
        if self.menu is None:
            return
        self.headerAction = qt.QAction(mainWindow)
        self.headerAction.setObjectName("StudioProjectFileHeaderAction")
        self.headerAction.setEnabled(False)
        self.saveAction = qt.QAction(mainWindow)
        self.saveAction.setObjectName("StudioSaveNowAction")
        self.saveAction.connect("triggered()", saveNow)
        self.copyAction = qt.QAction(_("Save a copy as…"), mainWindow)
        self.copyAction.setObjectName("StudioSaveCopyAsAction")
        self.copyAction.connect("triggered()", saveCopyAs)
        self.folderAction = qt.QAction(_("Show project folder"), mainWindow)
        self.folderAction.setObjectName("StudioShowProjectFolderAction")
        self.folderAction.connect("triggered()", showProjectFolder)
        self.newAction = qt.QAction(mainWindow)
        self.newAction.setObjectName("StudioNewProjectAction")
        self.newAction.connect("triggered()", newProject)
        self.separator = qt.QAction(mainWindow)
        self.separator.setSeparator(True)
        self.actions = [self.headerAction, self.saveAction, self.copyAction, self.folderAction, self.newAction, self.separator]
        existing = self.menu.actions()
        before = existing[0] if existing else None
        for action in self.actions:
            if before is not None:
                self.menu.insertAction(before, action)
            else:
                self.menu.addAction(action)
        projects.addListener(self.update)
        self.update()

    def update(self):
        project = projects.currentProject()
        studio = _shell() is not None
        visible = project is not None
        for action in self.actions:
            action.setVisible(visible)
        if project is None:
            return
        self.headerAction.setText(_("Project: {name} (saved automatically)").format(name=project.name))
        # The keys are shown only: Studio handles Ctrl+S and Ctrl+N; Classic keeps the official Ctrl+S (Save Data)
        self.saveAction.setText(_("Save now") + (f"\t{_keyText('Ctrl+S')}" if studio else ""))
        self.newAction.setText(_("New project") + (f"\t{_keyText('Ctrl+N')}" if studio else ""))

    def dispose(self):
        projects.removeListener(self.update)
        if self.menu is not None:
            for action in self.actions:
                self.menu.removeAction(action)
                action.setObjectName("")
                action.deleteLater()
        self.actions = []


def attachService(service):
    """Called by the autosave service once it is installed (Classic and Studio): File menu items, Search entries and the
    messages of the project engine. The Studio plug-in replaces the crash presenter with the Library card.
    """
    global _fileActions
    mainWindow = slicer.util.mainWindow()
    if mainWindow is not None and _fileActions is None:
        _fileActions = FileMenuActions(mainWindow)
    service.setProjectAnnouncer(announce)
    registerPaletteProvider()


def announce(event, project, **info):
    """Messages of the project engine (``AutosaveService.setProjectAnnouncer``)."""
    name = project.relativeName() if project is not None else ""
    if event == "created":
        message(_("Saving to {project}").format(project=name), action=lambda *_args: renameDialog(), actionText=_("Rename"), ms=6000,
                toolTip=project.path if project is not None else None)
    elif event == "copy":
        message(_("Opened a copy in {project}. The original is not changed.").format(project=name), level="consequence",
                action=lambda *_args: renameDialog(), actionText=_("Rename"), toolTip=project.path if project is not None else None)
    elif event == "opened":
        message(_("Continuing {project}").format(project=name), ms=2500)
    elif event == "locked":
        message(_("{name} is open in another Slicer Studio: changes made here are not saved to it.").format(name=project.name if project else ""),
                level="warning", ms=8000)
    elif event == "conflict":
        conflicts = info.get("conflicts") or []
        for theirs, ours in conflicts[:3]:
            message(_("Another program changed {theirs}: Slicer Studio saved its version as {ours}.").format(theirs=theirs, ours=ours),
                    level="consequence", action=showProjectFolder, actionText=_("Show"))
    elif event == "failed":
        message(_("Not saved: {reason}").format(reason=info.get("reason") or ""), level="warning", action=saveNow, actionText=_("Retry"), ms=8000)


#
# Studio plug-in
#


class ProjectsUi:
    """The Studio side of projects: Ctrl+S / Ctrl+N, the crash card in Library, the chooser when Studio is applied later."""

    def __init__(self, shell):
        self.shell = shell
        self.crashCard = None
        self.crashNotice = None
        self._shortcuts = []
        self._saveShortcuts = None
        self._closeShortcuts = None
        self._closeMenuFilter = None
        self._service = None

    def install(self):
        mainWindow = self.shell.mainWindow
        saveAction = mainWindow.findChild(qt.QAction, "FileSaveSceneAction")
        if saveAction is not None:
            # A second Ctrl+S shortcut would be ambiguous: Qt would trigger neither
            self._saveShortcuts = list(saveAction.shortcuts())
            saveAction.setShortcuts([])
        closeAction = mainWindow.findChild(qt.QAction, "FileCloseSceneAction")
        if closeAction is not None:
            self._closeShortcuts = list(closeAction.shortcuts())
            closeAction.setShortcuts([])
        fileMenu = mainWindow.findChild(qt.QMenu, "FileMenu")
        if fileMenu is not None:
            self._closeMenuFilter = _CloseSceneMenuFilter()
            fileMenu.installEventFilter(self._closeMenuFilter)
        self._shortcuts = [
            self.shell._createShortcut("Ctrl+S", lambda: saveNow(self.shell), _("Save now (the project; the Save dialog without one)")),
            self.shell._createShortcut("Ctrl+N", newProject, _("New project")),
            self.shell._createShortcut("Ctrl+W", closeScene, _("Close scene (the project is saved first)")),
        ]
        self.attach()
        if _fileActions is not None:
            _fileActions.update()
        if _startupDone and chooserNeeded():
            qt.QTimer.singleShot(0, lambda: showChooser() if chooserNeeded() and _ui is self else None)

    def attach(self):
        service = _service()
        if service is None:
            return False
        self._service = service
        service.setProjectPresenter(self.presentCrash)
        pending = [info for info in service.pendingProjectRecoveries() if not info.isCurrent()]
        if pending and self.crashCard is None:
            self.presentCrash(pending)
        return True

    def uninstall(self):
        mainWindow = self.shell.mainWindow
        for shortcut in self._shortcuts:
            self.shell._deleteShortcut(shortcut)
        self._shortcuts = []
        saveAction = mainWindow.findChild(qt.QAction, "FileSaveSceneAction")
        if saveAction is not None and self._saveShortcuts is not None:
            saveAction.setShortcuts(self._saveShortcuts)
        self._saveShortcuts = None
        closeAction = mainWindow.findChild(qt.QAction, "FileCloseSceneAction")
        if closeAction is not None and self._closeShortcuts is not None:
            closeAction.setShortcuts(self._closeShortcuts)
        self._closeShortcuts = None
        fileMenu = mainWindow.findChild(qt.QMenu, "FileMenu")
        if fileMenu is not None and self._closeMenuFilter is not None:
            fileMenu.removeEventFilter(self._closeMenuFilter)
        self._closeMenuFilter = None
        if self._service is not None and getattr(self._service, "_projectPresenter", None) == self.presentCrash:
            self._service.setProjectPresenter(None)
        self.closeCrashOffer()
        if _fileActions is not None:
            qt.QTimer.singleShot(0, _fileActions.update)

    def presentCrash(self, infos):
        """The crash offer: the card in Library, a notice line elsewhere."""
        if not infos:
            return None
        from . import library

        self.closeCrashOffer()
        self.crashCard = CrashCard(infos, self)
        library.showNotice(CRASH_NOTICE_KEY, self.crashCard.widget)
        if self.shell.currentWorkspace() != "library":
            self.crashNotice = ui.noticeLine(_("Slicer Studio closed unexpectedly: your project {name} was saved").format(name=infos[0].name),
                                             [(_("Open project"), lambda: self.openCrashed(infos[0].path)), (_("Show"), self.showCard)])
            self.crashNotice.setObjectName("StudioProjectCrashNotice")
            self.shell.setInspectorNotice(self.crashNotice, key=CRASH_NOTICE_KEY, workspace=None)
        return self.crashCard

    def showCard(self):
        self.shell.switchTo("library")
        self.shell.clearInspectorNotice(CRASH_NOTICE_KEY)

    def openCrashed(self, path):
        self.closeCrashOffer()
        openProject(path, self.shell)

    def closeCrashOffer(self):
        from . import library

        self.shell.clearInspectorNotice(CRASH_NOTICE_KEY)
        if self.crashNotice is not None:
            if _isAlive(self.crashNotice):
                self.crashNotice.deleteLater()
            self.crashNotice = None
        card, self.crashCard = self.crashCard, None
        if card is not None:
            library.closeNotice(CRASH_NOTICE_KEY)
            card.dispose()


def install(shell):
    """Studio plug-in entry point (``shell.OPTIONAL_PLUGINS``)."""
    global _ui
    if _ui is not None:
        uninstall(_ui.shell)
    _ui = ProjectsUi(shell)
    _ui.install()
    if _ui._service is None:
        # The autosave service starts after the Studio apply (Studio.py): attach when the event loop runs
        qt.QTimer.singleShot(0, _ui.attach)


def uninstall(shell):
    global _ui
    if _ui is None:
        return
    try:
        _ui.uninstall()
    finally:
        _ui = None

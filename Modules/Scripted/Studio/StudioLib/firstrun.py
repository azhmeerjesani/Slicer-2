"""First run and closing in Slicer Studio (USABILITY 8 rule 10 and 12, U-03): the Python side of the main window hooks
of W3-03.

A Studio plug-in (``shell.OPTIONAL_PLUGINS``): ``install(shell)`` runs when Studio is applied and ``uninstall(shell)``
in Classic, which then behaves exactly as official Slicer.

- **Research-use line.** In Studio the research-use disclaimer is a line in the Library footer, "Research use only,
  not for clinical use · Don't show again", instead of a modal dialog. The Studio module sets the application property
  ``SlicerDisclaimerHandledByApplication`` before the main window is shown (``Studio.py``), so the main window emits
  ``disclaimerRequested(QString)`` instead of opening its dialog (``connectDisclaimer()`` stores the message, also
  when it comes before this plug-in is installed). "Don't show again" writes the official key
  ``MainWindow/DontShowDisclaimerMessage`` with the value the official dialog writes, and only on that click. Classic
  keeps the modal: ``uninstall()`` clears the property, and a message that arrives while Studio is not shown opens
  the official dialog.
- **Close prompt.** When the application is closed, the main window emits ``closeSummaryRequested()`` and reads the
  properties set by ``computeCloseSummary()``: "lesionSaved" (the only unsaved items are saved lesions and the
  template: one "Quit?" question with Quit as the default), "unsavedLesion" (the lesion of Draw has unsaved edits:
  "Save lesion…" is the default; a cancelled file dialog keeps the application open) or nothing (the classic
  prompt). The autosave line (``autosave.unsavedSummary()``) is appended when the autosave service runs. While the
  scene is a project (A-09), the project is saved first and, when that succeeded, the question is only "All changes are
  saved in Projects/<name>. Quit?" (Quit is the default); when saving failed, the Studio question names the reason and
  offers "Save a copy…", "Quit without saving" and "Keep working" (the default), never the official Save Data dialog
  (it would write over the project's files in place). While a study is open (a study has no Save: its current subject is written when the application
  quits), the subject is written first and, when that succeeded, the question is only "All changes are saved in study
  <name>. Quit?"; it never offers a "Don't save" that the study could not honour.
"""

import logging
import os
import traceback

import qt
import slicer
import vtk
from slicer.i18n import tr as _

#: Official settings key of the disclaimer's "Don't show this message again" (written only on an explicit click)
DONT_SHOW_DISCLAIMER_KEY = "MainWindow/DontShowDisclaimerMessage"
#: Application property read by the main window before it shows the disclaimer (W3-03)
DISCLAIMER_HANDLED_PROPERTY = "SlicerDisclaimerHandledByApplication"
#: Key of the research-use line in the Library footer (``library.setFooterLine``)
DISCLAIMER_FOOTER_KEY = "disclaimer"
#: Main window properties of the close prompt (W3-03)
CLOSE_MODE_PROPERTY = "SlicerStudioCloseMode"
CLOSE_SUMMARY_PROPERTY = "SlicerStudioCloseSummary"
CLOSE_ANSWER_PROPERTY = "SlicerStudioCloseAnswer"
SAVE_RESULT_PROPERTY = "SlicerStudioSaveResult"
CLOSE_MODE_UNSAVED_LESION = "unsavedLesion"
CLOSE_MODE_LESION_SAVED = "lesionSaved"

#: The disclaimer message of this session (None until the main window asked for it)
_disclaimerMessage = None
_disclaimerConnected = False
_instance = None


def instance():
    """The installed plug-in (``FirstRun``), or None."""
    return _instance


def disclaimerMessage():
    """The disclaimer message that the main window asked to show in this session, or None."""
    return _disclaimerMessage


#
# Research-use line
#


def connectDisclaimer():
    """Receive the disclaimer of the main window (``disclaimerRequested``, W3-03). Called by the Studio module when the
    application has started, before Studio is applied, so that an early message is not lost. Return True if connected.
    """
    global _disclaimerConnected
    if _disclaimerConnected:
        return True
    mainWindow = slicer.util.mainWindow()
    if mainWindow is None or not hasattr(mainWindow, "disclaimerRequested"):
        return False
    mainWindow.connect("disclaimerRequested(QString)", onDisclaimerRequested)
    _disclaimerConnected = True
    return True


def onDisclaimerRequested(message):
    """The main window did not show its disclaimer dialog because the application shows it: a line in Studio, else the
    official dialog.
    """
    global _disclaimerMessage
    _disclaimerMessage = str(message)
    if _instance is not None and _instance.shell.isActive():
        _instance.showDisclaimerLine()
    else:
        # Studio is not shown (it was not applied, or the user switched to Classic first): the official modal
        showDisclaimerDialog(_disclaimerMessage)


def showDisclaimerDialog(message):
    """The disclaimer dialog of official Slicer (``qSlicerMainWindow::disclaimer``), with its "Don't show again" key."""
    import ctk

    box = ctk.ctkMessageBox(slicer.util.mainWindow())
    box.setAttribute(qt.Qt.WA_DeleteOnClose, True)
    box.setText(message)
    box.setIcon(qt.QMessageBox.Information)
    box.setDontShowAgainSettingsKey(DONT_SHOW_DISCLAIMER_KEY)
    run = getattr(box, "exec", None) or box.exec_
    return run()


def dontShowDisclaimerAgain():
    """The explicit "Don't show again" of the research-use line: write the official key as the official dialog does
    (the standard button that closed it, Ok), and remove the line.
    """
    settings = qt.QSettings()
    settings.setValue(DONT_SHOW_DISCLAIMER_KEY, int(qt.QMessageBox.Ok))
    settings.sync()
    _clearDisclaimerLine()


def _library():
    try:
        from . import library
    except ImportError:
        return None
    return library if hasattr(library, "setFooterLine") else None


def _clearDisclaimerLine():
    library = _library()
    if library is not None and hasattr(library, "clearFooterLine"):
        library.clearFooterLine(DISCLAIMER_FOOTER_KEY)


#
# Close prompt
#


def _lesionSegmentations():
    """The segmentations of the scene that the user may have drawn (not hidden from the editors)."""
    nodes = []
    for node in slicer.util.getNodesByClass("vtkMRMLSegmentationNode"):
        if node.GetHideFromEditors():
            continue
        nodes.append(node)
    return nodes


def _isTemplate(node):
    from . import space

    return node.GetAttribute(space.ROLE_ATTRIBUTE) == space.ROLE_TEMPLATE


def _lesionName(node, path):
    """The name of a lesion in the close prompt: its file name without extension once saved ("sub-01_lesion"), else
    the segmentation name.
    """
    from . import draw

    if path:
        name = os.path.basename(str(path))
        for extension in sorted(getattr(draw, "NIFTI_EXTENSIONS", (".nii.gz", ".nii")), key=len, reverse=True):
            if name.lower().endswith(extension):
                return name[: -len(extension)]
        return name
    return node.GetName() or _("lesion")


def _modifiedStorableNodes():
    """The storable nodes of the scene that are modified since they were read (or that were never saved)."""
    collection = vtk.vtkCollection()
    slicer.mrmlScene.GetStorableNodesModifiedSinceRead(collection)
    nodes = (collection.GetItemAsObject(index) for index in range(collection.GetNumberOfItems()))
    return [node for node in nodes if node is not None]


def closeSummary():
    """``(mode, summary)`` of the close prompt (USABILITY 8 rule 10).

    - ("lesionSaved", "Your lesion is saved as sub-01_lesion.nii.gz. The session (scene) is not saved. Quit?") when
      the only unsaved items are lesions saved as NIfTI and the template;
    - ("unsavedLesion", "sub-01_lesion has changes since it was saved") when the only unsaved items are the lesion of
      Draw (which "Save lesion…" saves), saved lesions and the template;
    - ("", "") otherwise: the classic prompt, which lists the modified data and saves the scene.
    """
    from . import draw

    modified = _modifiedStorableNodes()
    modifiedIds = {node.GetID() for node in modified}
    savedFiles = []
    unsavedLesions = []  # [(node, text)]
    lesionIds = set()
    for node in _lesionSegmentations():
        state, path, _savedTime = draw.lesionSaveState(node)
        name = _lesionName(node, path)
        if state == "saved":
            lesionIds.add(node.GetID())
            savedFiles.append(os.path.basename(path) if path else name)
        elif state == "unsaved":
            unsavedLesions.append((node, _("{name} has changes since it was saved").format(name=name)))
        elif state in ("never", "previous"):
            if draw.isLesionEmpty(node):
                # Nothing to lose; any unsaved node it has is listed with the others
                continue
            if state == "previous" and node.GetID() not in modifiedIds:
                # Saved with the scene and not changed since
                continue
            text = _("{name} has not been saved") if state == "never" else _("{name} has changes since it was saved")
            unsavedLesions.append((node, text.format(name=name)))
    unsavedLesionIds = {node.GetID() for node, _text in unsavedLesions}
    others = [node for node in modified
              if node.GetID() not in lesionIds and node.GetID() not in unsavedLesionIds and not _isTemplate(node)]
    if others:
        return "", ""
    if unsavedLesions:
        editorLesion, _source = draw.lesionNodes()
        if len(unsavedLesions) != 1 or editorLesion is None or unsavedLesions[0][0].GetID() != editorLesion.GetID():
            # "Save lesion…" saves the lesion of Draw only: the classic prompt saves everything
            return "", ""
        return CLOSE_MODE_UNSAVED_LESION, unsavedLesions[0][1]
    if savedFiles:
        return CLOSE_MODE_LESION_SAVED, _("Your lesion is saved as {file}. The session (scene) is not saved. Quit?").format(file=", ".join(savedFiles))
    return "", ""


def _autosaveLine():
    try:
        from . import autosave
    except ImportError:
        return ""
    if not hasattr(autosave, "instance") or autosave.instance() is None or not hasattr(autosave, "unsavedSummary"):
        return ""
    try:
        return autosave.unsavedSummary() or ""
    except Exception:
        logging.error(f"Studio: autosave summary failed:\n{traceback.format_exc()}")
        return ""


#: Jobs that quitting stops (job name -> what the user knows it as); Ctrl+N / Ctrl+W stop only the scene-bound ones.
#: Quitting stops every running job: any other heavy job is named by its title (the status chip's text).
_RUN_LABELS = {"engine": "Map network", "normalize": "Normalize to MNI", "normalize-batch": "Normalize to MNI",
               "glm": "Contrast", "nulls": "Null models", "validate": "Validation", "validate-frozen": "Frozen circuit",
               "pipeline": "Run everything", "reference": "Reference maps", "fastindex": "Fast index", "degree": "Degree map"}
_SCENE_BOUND_RUNS = ("engine", "normalize")
#: Jobs that keep their completed work when stopped: the next run continues from it (pipeline outputs, reference build)
_RESUMABLE_RUNS = ("pipeline", "reference")


def _runLabel(job):
    """What the user knows a running job as ("Map network", "Contrast", "Null models"...)."""
    name = getattr(job.spec, "name", None)
    labels = {"engine": _("Map network"), "normalize": _("Normalize to MNI"), "normalize-batch": _("Normalize to MNI"),
              "glm": _("Contrast"), "nulls": _("Null models"), "validate": _("Validation"), "validate-frozen": _("Frozen circuit"),
              "pipeline": _("Run everything"), "reference": _("Reference maps"), "fastindex": _("Fast index"), "degree": _("Degree map")}
    if name in labels:
        return labels[name]
    return str(getattr(job.spec, "title", "") or name or _("A job"))


def runningJobNotice(sceneOnly=False):
    """The sentence that tells that a long job (Map network, Normalize, Contrast, Null models, Validation, Run
    everything, Reference maps...) is running and what leaving does to it, "" if none runs.

    ``sceneOnly``: only the runs that closing the scene (Ctrl+N, Ctrl+W) stops; quitting stops every running job
    (``JobRunner._onAboutToQuit``), and every heavy job or job named in ``_RUN_LABELS`` is named.
    """
    import sys

    module = sys.modules.get("LesionNetworkMappingUI.jobrunner")
    if module is None:
        return ""
    try:
        jobs = module.instance().runningJobs()
    except Exception:
        return ""
    sentences = []
    for job in jobs:
        try:
            if job.isFinished():
                continue
            name = getattr(job.spec, "name", None)
            if sceneOnly:
                if name not in _SCENE_BOUND_RUNS:
                    continue
            elif name not in _RUN_LABELS and not getattr(job, "heavy", getattr(job.spec, "heavy", False)):
                continue
            label = _runLabel(job)
            progress = getattr(job, "progress", None)
        except Exception:
            continue
        running = _("{what} is running ({pct}%).").format(what=label, pct=round(100 * progress)) if progress is not None else _("{what} is running.").format(what=label)
        if sceneOnly:
            sentences.append(_("{running} Closing the scene stops it and its result will be lost.").format(running=running))
        elif name in _RESUMABLE_RUNS:
            sentences.append(_("{running} Quitting stops it; the work done so far is kept and the next run continues from it.").format(running=running))
        else:
            sentences.append(_("{running} Quitting stops it and its result will be lost.").format(running=running))
    return "\n".join(sentences)


#: (project, reason) while the close prompt is about a project that could not be saved (``projectCloseSummary``), else None
_unsavedProjectQuit = None


def projectCloseSummary():
    """``(mode, summary)`` of the close prompt while the scene is a project (A-09): the project is saved now; "All changes
    are saved…", or when saving failed the Studio question about the unsaved project (``askCloseQuestion``).
    """
    try:
        from . import autosave
    except ImportError:
        return None
    service = autosave.instance() if hasattr(autosave, "instance") else None
    project = service.currentProject() if service is not None and hasattr(service, "currentProject") else None
    if project is None:
        return None
    global _unsavedProjectQuit
    ok = service.flushForClose()
    if not ok:
        reason = getattr(service, "_projectPausedReason", "") or _("see the error log")
        # The project could not be saved (a full disk, a folder that cannot be written). The classic "Save before exit?"
        # would offer the official Save Data dialog, which writes over the project's own files in place: on a full disk
        # that truncates the lesion saved before. The Studio question (``askCloseQuestion``) asks instead: "Save a copy…"
        # elsewhere, quit without saving, or keep working.
        _unsavedProjectQuit = (project, reason)
        return CLOSE_MODE_LESION_SAVED, _("{project} could not be saved: {reason}. Quitting now discards its changes since the last "
                                          "save. Quit?").format(project=project.relativeName(), reason=reason)
    return CLOSE_MODE_LESION_SAVED, _("All changes are saved in {project}. Quit?").format(project=project.relativeName())


def studyCloseSummary():
    """``(mode, summary)`` of the close prompt while a study is open (SPEC D7: a study has no Save, its current subject
    is written when the application quits): the subject's edits are written now and, when that succeeded and nothing
    outside the study is unsaved, the question is only "All changes are saved in study <name>. Quit?" (the answer
    cannot discard them, so the prompt never offers "Don't save" or a "Save lesion…" outside the study). When only
    participants.tsv could not be saved (another program made it unreadable), the prompt names the subjects whose study
    changes are kept for the next session (Cancel to repair the table first). ``("", "")`` (the classic prompt) when
    writing failed otherwise (a toast gives the reason) or other data is unsaved; None without a study.
    """
    try:
        from . import study_ui
    except ImportError:
        return None
    studySession = study_ui.session() if hasattr(study_ui, "session") else None
    if studySession is None:
        return None
    written = studySession.writeCurrent()
    saved = studySession.flush()
    # participants.tsv could not be saved (another program made it unreadable, or keeps it locked): the study changes
    # are kept for the next session when the study closes (StudySession.close), and the prompt says so
    unsaved = studySession.unsavedSubjects() if written and not saved and studySession.folderAvailable() else []
    # A read-only study: the current subject's tracing cannot be written, and the prompt names it (nothing else is lost)
    unwritten = [studySession.current()] if (not written and getattr(studySession, "readOnly", False) and studySession.folderAvailable()
                                             and studySession.current() is not None and studySession.isDirty()) else []
    if (not written and not unwritten) or (not unwritten and not saved and not unsaved):
        return "", ""
    # The study's nodes (the subject's scan and lesion, the shared template) are in the study now
    others = [node for node in _modifiedStorableNodes()
              if not node.GetAttribute(study_ui.SUBJECT_ATTRIBUTE) and not _isTemplate(node)]
    if others:
        return "", ""
    if unwritten:
        return CLOSE_MODE_LESION_SAVED, _("The tracing of {sid} cannot be saved: study {study} is read-only. Quitting discards it "
                                          "(Cancel to keep working). Quit?").format(sid=unwritten[0], study=studySession.name)
    if unsaved:
        return CLOSE_MODE_LESION_SAVED, _("The study changes of {sids} could not be saved in participants.tsv of study {study}: "
                                          "{error}\n\nThey are kept and added when you open the study again after the table is "
                                          "repaired. Quit?").format(
            sids=study_ui._sidList(unsaved), study=studySession.name, error=studySession.saveErrorText())
    return CLOSE_MODE_LESION_SAVED, _("All changes are saved in study {study}. Quit?").format(study=studySession.name)


def _withRunNotice(summary):
    """The close question with the running Map network / Normalize run put first (its result is lost when quitting)."""
    notice = runningJobNotice()
    return f"{notice}\n\n{summary}" if notice else summary


def _sceneModified():
    """True when the classic prompt would ask "Save before exit?" (modified data or scene, as
    ``qSlicerMainWindowPrivate::isSceneContentModifiedSinceRead``).
    """
    if _modifiedStorableNodes():
        return True
    collection = vtk.vtkCollection()
    return bool(slicer.mrmlScene.GetModifiedSinceRead(collection))


def _runNoticeQuestion(mainWindow):
    """No Studio close question applies (nothing open, or the classic prompt would ask): when a job runs, ask the Studio
    question anyway so that it names the job that quitting stops ("Save session…" saves unsaved data first). Without
    a running job the classic prompt runs.
    """
    try:
        notice = runningJobNotice()
        if not notice:
            return
        if _sceneModified():
            question = _("The scene has changes that are not saved: Save session… saves them before quitting. Quit?")
        else:
            question = _("Quit?")
    except Exception:
        logging.error(f"Studio: cannot describe the running job:\n{traceback.format_exc()}")
        return
    mainWindow.setProperty(CLOSE_SUMMARY_PROPERTY, f"{notice}\n\n{question}")
    mainWindow.setProperty(CLOSE_MODE_PROPERTY, CLOSE_MODE_LESION_SAVED)


def computeCloseSummary():
    """Set the close prompt properties of the main window (``closeSummaryRequested()``). Never raises: on an error the
    classic prompt runs, unless a job runs (then the question names it, see ``_runNoticeQuestion``).
    """
    global _unsavedProjectQuit
    _unsavedProjectQuit = None
    mainWindow = slicer.util.mainWindow()
    if mainWindow is None:
        return
    try:
        studySummary = studyCloseSummary()
    except Exception:
        logging.error(f"Studio: writing the study before quitting failed:\n{traceback.format_exc()}")
        studySummary = ("", "")
    if studySummary is not None:
        mode, summary = studySummary
        if mode:
            summary = _withRunNotice(summary)
            mainWindow.setProperty(CLOSE_SUMMARY_PROPERTY, summary)
            mainWindow.setProperty(CLOSE_MODE_PROPERTY, mode)
        else:
            _runNoticeQuestion(mainWindow)
        return
    try:
        projectSummary = projectCloseSummary()
    except Exception:
        logging.error(f"Studio: saving the project before quitting failed:\n{traceback.format_exc()}")
        projectSummary = ("", "")
    if projectSummary is not None:
        mode, summary = projectSummary
        if mode:
            summary = _withRunNotice(summary)
            mainWindow.setProperty(CLOSE_SUMMARY_PROPERTY, summary)
            mainWindow.setProperty(CLOSE_MODE_PROPERTY, mode)
        else:
            _runNoticeQuestion(mainWindow)
        return
    try:
        mode, summary = closeSummary()
    except Exception:
        logging.error(f"Studio: cannot describe the unsaved data:\n{traceback.format_exc()}")
        mode, summary = "", ""
    if not mode:
        _runNoticeQuestion(mainWindow)
        return
    autosaveLine = _autosaveLine()
    if autosaveLine:
        summary = f"{summary}\n\n{autosaveLine}"
    summary = _withRunNotice(summary)
    mainWindow.setProperty(CLOSE_SUMMARY_PROPERTY, summary)
    mainWindow.setProperty(CLOSE_MODE_PROPERTY, mode)


class CloseQuestion:
    """Holder of the dialog and its buttons (PythonQt widgets take no attributes)."""

    answer = "cancel"


def closeQuestionDialog(summary, parent=None):
    """The "lesionSaved" exit question in the Studio dialog chrome (VISUAL 4.7): the icon tile and the title beside the
    summary (text2), "Save session…" as a link on the left and "Quit" as the primary button. Return a ``CloseQuestion``:
    its ``dialog`` is the QDialog and its ``answer`` is "quit", "save" or "cancel" (also Esc and closing the window).
    """
    from . import ui

    dialog = qt.QDialog(parent if parent is not None else slicer.util.mainWindow())
    dialog.setObjectName("StudioCloseQuestionDialog")
    dialog.setWindowTitle(_("Quit?"))
    dialog.setMinimumWidth(460)
    question = CloseQuestion()
    question.dialog = dialog
    layout = qt.QVBoxLayout(dialog)
    layout.setContentsMargins(24, 24, 24, 22)
    layout.setSpacing(14)
    header, _title, _body = ui.dialogHeader("circle-help", _("Quit?"), summary, "StudioCloseQuestion")
    layout.addLayout(header)
    buttons = qt.QHBoxLayout()
    buttons.setSpacing(10)

    def choose(answer):
        question.answer = answer
        dialog.accept() if answer != "cancel" else dialog.reject()

    question.saveButton = ui.linkButtonWidget(_("Save session…"), lambda: choose("save"), "StudioCloseQuestionSaveButton")
    buttons.addWidget(question.saveButton)
    buttons.addStretch(1)
    question.cancelButton = qt.QPushButton(_("Cancel"))
    question.cancelButton.setObjectName("StudioCloseQuestionCancelButton")
    question.cancelButton.setMinimumHeight(36)
    ui.setRole(question.cancelButton, "secondary")
    question.cancelButton.connect("clicked()", lambda: choose("cancel"))
    buttons.addWidget(question.cancelButton)
    question.quitButton = qt.QPushButton(_("Quit"))
    question.quitButton.setObjectName("StudioCloseQuestionQuitButton")
    question.quitButton.setDefault(True)
    question.quitButton.setMinimumHeight(36)
    question.quitButton.setMinimumWidth(96)
    ui.setRole(question.quitButton, "primary")
    question.quitButton.connect("clicked()", lambda: choose("quit"))
    buttons.addWidget(question.quitButton)
    layout.addLayout(buttons)
    ui.dialogChrome(dialog, styleSheet=lambda: (
        f"QLabel#StudioCloseQuestionBody {{ color: {ui.token('text2').name()}; }}"))
    return question


def askCloseQuestion():
    """``closeQuestionRequested()``: ask the "lesionSaved" exit question in the Studio dialog and set
    ``SlicerStudioCloseAnswer``. Never raises: on an error the main window shows its standard message box.
    """
    global _unsavedProjectQuit
    mainWindow = slicer.util.mainWindow()
    if mainWindow is None:
        return
    unsaved, _unsavedProjectQuit = _unsavedProjectQuit, None
    if unsaved is not None:
        # The project could not be saved: never the official Save Data dialog ("Save session…" writes over its files)
        try:
            from . import projects_ui

            project, reason = unsaved
            answer = projects_ui.askReplaceUnsavedProject(_("Quit"), project, reason, verb="quit", notice=runningJobNotice())
            mainWindow.setProperty(CLOSE_ANSWER_PROPERTY, "quit" if answer in ("save", "discard") else "cancel")
        except Exception:
            logging.error(f"Studio: cannot ask about the project that could not be saved:\n{traceback.format_exc()}")
            mainWindow.setProperty(CLOSE_ANSWER_PROPERTY, "cancel")
        return
    try:
        question = closeQuestionDialog(str(mainWindow.property(CLOSE_SUMMARY_PROPERTY) or ""), mainWindow)
        dialog = question.dialog
        run = getattr(dialog, "exec", None) or dialog.exec_
        run()
        mainWindow.setProperty(CLOSE_ANSWER_PROPERTY, question.answer)
        dialog.deleteLater()
    except Exception:
        logging.error(f"Studio: cannot show the quit question:\n{traceback.format_exc()}")


def saveLesionForClose(shell=None):
    """"Save lesion…" of the close prompt (``saveRequested()``): the Save lesion dialog. The main window quits only if
    ``SlicerStudioSaveResult`` is "saved"; a cancelled dialog or an error keeps the application open.
    """
    mainWindow = slicer.util.mainWindow()
    result = "cancelled"
    try:
        from . import draw

        if draw.saveLesionDialog(shell):
            result = "saved"
    except Exception:
        logging.error(f"Studio: cannot save the lesion:\n{traceback.format_exc()}")
    if mainWindow is not None:
        mainWindow.setProperty(SAVE_RESULT_PROPERTY, result)
    return result


#
# Plug-in
#


class FirstRun:
    """The first-run and close plug-in of a Studio shell."""

    def __init__(self, shell):
        self.shell = shell
        self._closeConnected = False

    def install(self):
        slicer.app.setProperty(DISCLAIMER_HANDLED_PROPERTY, True)
        connectDisclaimer()
        mainWindow = self.shell.mainWindow
        if hasattr(mainWindow, "closeSummaryRequested") and hasattr(mainWindow, "saveRequested"):
            mainWindow.connect("closeSummaryRequested()", computeCloseSummary)
            mainWindow.connect("saveRequested()", self._onSaveRequested)
            if hasattr(mainWindow, "closeQuestionRequested"):
                mainWindow.connect("closeQuestionRequested()", askCloseQuestion)
            self._closeConnected = True
        if _disclaimerMessage is not None and not _dontShowDisclaimer():
            self.showDisclaimerLine()

    def uninstall(self):
        # Classic keeps the official modal
        slicer.app.setProperty(DISCLAIMER_HANDLED_PROPERTY, False)
        if self._closeConnected:
            mainWindow = self.shell.mainWindow
            mainWindow.disconnect("closeSummaryRequested()", computeCloseSummary)
            mainWindow.disconnect("saveRequested()", self._onSaveRequested)
            if hasattr(mainWindow, "closeQuestionRequested"):
                mainWindow.disconnect("closeQuestionRequested()", askCloseQuestion)
            self._closeConnected = False
        _clearDisclaimerLine()

    def _onSaveRequested(self):
        saveLesionForClose(self.shell)

    def showDisclaimerLine(self):
        """The research-use line in the Library footer (with "Don't show again")."""
        library = _library()
        if library is None:
            showDisclaimerDialog(_disclaimerMessage or _("Research use only, not for clinical use"))
            return
        library.setFooterLine(DISCLAIMER_FOOTER_KEY, _("Research use only, not for clinical use"), [(_("Don't show again"), dontShowDisclaimerAgain)])


def _dontShowDisclaimer():
    try:
        value = int(qt.QSettings().value(DONT_SHOW_DISCLAIMER_KEY, int(qt.QMessageBox.InvalidRole)))
    except (TypeError, ValueError):
        return False
    return value != int(qt.QMessageBox.InvalidRole)


def install(shell):
    """Studio plug-in entry point (``shell.OPTIONAL_PLUGINS``)."""
    global _instance
    if _instance is not None:
        uninstall(_instance.shell)
    _instance = FirstRun(shell)
    _instance.install()


def uninstall(shell):
    global _instance
    if _instance is None:
        return
    try:
        _instance.uninstall()
    finally:
        _instance = None

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
  prompt). The autosave line (``autosave.unsavedSummary()``) is appended when the autosave service runs.
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


def computeCloseSummary():
    """Set the close prompt properties of the main window (``closeSummaryRequested()``). Never raises: on an error the
    classic prompt runs.
    """
    mainWindow = slicer.util.mainWindow()
    if mainWindow is None:
        return
    try:
        mode, summary = closeSummary()
    except Exception:
        logging.error(f"Studio: cannot describe the unsaved data:\n{traceback.format_exc()}")
        mode, summary = "", ""
    if not mode:
        return
    autosaveLine = _autosaveLine()
    if autosaveLine:
        summary = f"{summary}\n\n{autosaveLine}"
    mainWindow.setProperty(CLOSE_SUMMARY_PROPERTY, summary)
    mainWindow.setProperty(CLOSE_MODE_PROPERTY, mode)


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

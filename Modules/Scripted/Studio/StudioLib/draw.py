"""Draw workspace of Slicer Studio (SPEC 2.6): hooks ``enter(shell)`` and ``leave(shell)``.

Draw shows the Segment Editor in the inspector. These hooks (imported lazily by ``StudioLib.workspaces``)
make it a place that keeps its state and has a visible way out:

- The tool survives navigation. The Segment Editor deactivates its effect whenever its module is left
  (``SegmentEditorWidget.exit()``), so every trip to Library, to a transient module or to Classic used to drop
  the brush. ``leave()`` records the active effect, the segmentation, the segment and the source volume, and
  ``enter()`` restores the effect (and the segment, if the segmentation is still the same).
- Something is armed on arrival: on the first entry, the Paint effect is activated as soon as a segmentation
  with a selected segment and a source volume exist (for example, right after a scan is opened). The effect is
  armed on the event loop iteration after the entry, once the views shown by the switch are in place.
  A tool deselected with Esc stays deselected on that lesion: nothing is re-armed until the user picks a tool
  again, or another lesion is edited (a new case is a new landing, so Paint is armed on it).
  A tool that the Segment Editor deactivated by itself (another segmentation was selected, the scene was
  closed) is armed again on the new segmentation.
- "Save lesion…": a primary button in the inspector footer (also ``Ctrl+Shift+S`` and ``Ctrl+Enter``, the
  primary action of the inspector) writes the lesion as a NIfTI mask on the grid of the source volume. The
  dialog (``saveLesionDialog()``) and the writer (``saveLesion()``) are separate functions. When the Draw
  call-to-action of SPEC W2-05 lands, it can take over ``saveLesionDialog()`` and drop ``_SaveFooter``.
- ``Ctrl+Y`` redoes in the Segment Editor (it only binds ``Ctrl+Shift+Z`` and ``Y`` on Linux and macOS, and the
  classic Edit > Redo action is inactive while the menubar is hidden). Removed when Draw is left.

Everything is undone by ``leave()``, which the shell also calls before it restores the Classic interface.
"""

import logging
import os
import re
import tempfile
import traceback
import weakref

import qt
import slicer
import vtk
from slicer.i18n import tr as _
from slicer.util import VTKObservationMixin

from . import ui

#: Effect armed on the first entry into Draw
DEFAULT_EFFECT = "Paint"
#: Draw shortcuts (SPEC 9.6: not used by the views or the Segment Editor)
SAVE_LESION_SHORTCUT = "Ctrl+Shift+S"
REDO_SHORTCUT = "Ctrl+Y"
#: File dialog filter of "Save lesion…"
NIFTI_FILTER = "NIfTI (*.nii.gz *.nii)"
NIFTI_EXTENSIONS = (".nii.gz", ".nii")
#: Folder of the last saved lesion (a Studio key: official Slicer keys are never written)
LAST_SAVE_FOLDER_KEY = "Studio/LastSaveFolder"

#: BIDS subject entity in a file or node name ("sub-01_T1w.nii.gz" -> "sub-01")
_BIDS_SUBJECT_PATTERN = re.compile(r"(?:^|[_\-/\s])(sub-[A-Za-z0-9]+)")
#: "Lesions (<scan name>)", the segmentation that the importer creates for a scan
_SCAN_LESIONS_PATTERN = re.compile(r"^Lesions? \((.+)\)$")
#: Names that say nothing about the case: the file is named "lesion.nii.gz"
_GENERIC_NAMES = ("lesion", "lesions", "segmentation", "mask", "")

#: Not recorded yet (first entry)
_UNSET = object()


#
# Segment Editor access
#


def _moduleWidget():
    """The Python Segment Editor module widget, or None if it was not created."""
    return getattr(slicer.modules, "SegmentEditorWidget", None)


def segmentEditor():
    """The ``qMRMLSegmentEditorWidget`` of the Segment Editor module, or None if it was not created."""
    widget = _moduleWidget()
    return getattr(widget, "editor", None) if widget is not None else None


def _isModuleEntered():
    widget = _moduleWidget()
    if widget is None:
        return False
    try:
        return bool(widget.parent.isEntered)
    except Exception:
        return False


def _canEdit(editor):
    """True if an effect can be used: a segmentation with a selected segment and a source volume."""
    segmentationNode = editor.segmentationNode()
    segmentId = editor.currentSegmentID()
    if segmentationNode is None or not segmentId or editor.sourceVolumeNode() is None:
        return False
    return segmentationNode.GetSegmentation().GetSegment(segmentId) is not None


def _effectName(editor):
    effect = editor.activeEffect()
    return effect.name if effect is not None else None


#
# State kept across workspace switches
#


class DrawState:
    """What ``leave()`` records and ``enter()`` restores.

    ``effect`` is the name of the active effect, None if the user deselected it, ``_UNSET`` before the first
    entry. The node and segment IDs identify what was edited. The segmentation node is also kept as a weak
    reference: node IDs are reused after the scene is closed, so the ID alone does not tell a new case apart.
    """

    def __init__(self):
        self.effect = _UNSET
        self.segmentationNodeId = None
        self.segmentId = None
        self.sourceVolumeNodeId = None
        self._segmentationNodeRef = vtk.vtkWeakReference()

    def isRecorded(self):
        return self.effect is not _UNSET

    def segmentationNode(self):
        """The recorded segmentation node, None if there is none or it was deleted."""
        return self._segmentationNodeRef.Get()

    def setSegmentationNode(self, segmentationNode):
        self._segmentationNodeRef.Set(segmentationNode)
        self.segmentationNodeId = segmentationNode.GetID() if segmentationNode is not None else None

    def isDeselectedOn(self, segmentationNode):
        """True if the user deselected the tool (Esc) while editing ``segmentationNode``."""
        return self.effect is None and segmentationNode is not None and self.segmentationNode() is segmentationNode

    def __repr__(self):
        effect = "<unset>" if self.effect is _UNSET else self.effect
        return (f"DrawState(effect={effect!r}, segmentation={self.segmentationNodeId!r}, segment={self.segmentId!r}, "
                f"source={self.sourceVolumeNodeId!r})")


class _Snapshot:
    def __init__(self, editor):
        self.effect = _effectName(editor)
        self.segmentationNodeId = editor.segmentationNodeID() or None
        self.segmentId = editor.currentSegmentID() or None
        self.sourceVolumeNodeId = editor.sourceVolumeNodeID() or None
        self.canEdit = _canEdit(editor)
        # Node IDs are reused after the scene is closed: the node itself tells a new case apart
        self._segmentationNodeRef = vtk.vtkWeakReference()
        self._segmentationNodeRef.Set(editor.segmentationNode())

    def sameTarget(self, other):
        return (self._segmentationNodeRef.Get() is other._segmentationNodeRef.Get()
                and (self.segmentationNodeId, self.segmentId, self.sourceVolumeNodeId) == (
                    other.segmentationNodeId, other.segmentId, other.sourceVolumeNodeId))


class _DrawSession(VTKObservationMixin):
    """Draw state of one shell: the recorded state, the footer and the shortcuts while Draw is shown."""

    def __init__(self, shell):
        VTKObservationMixin.__init__(self)
        self.shellRef = weakref.ref(shell)
        self.state = DrawState()
        #: Effect to activate as soon as the editor can edit (None: nothing to arm)
        self.pendingEffect = None
        self.active = False
        self.footer = None
        self.redoShortcut = None
        self.saveShortcutRegistered = False
        self._snapshot = None
        self._observedNode = None
        # Parameter node changes are coalesced: one evaluation per event loop iteration
        self._timer = qt.QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.connect("timeout()", self._evaluate)

    @property
    def shell(self):
        return self.shellRef()

    #
    # enter / leave
    #

    def enter(self):
        shell = self.shell
        self.active = True
        self._installFooter(shell)
        self._installShortcuts(shell)
        editor = segmentEditor()
        if editor is None:
            return
        self._restore(editor)
        self._observe(editor)
        self._snapshot = _Snapshot(editor)
        # The effect is armed on the next event loop iteration (_evaluate()), after the views that the switch shows
        # (Library page hidden, Draw layout) got their Enter events. Armed now, Paint would get the Enter event of
        # the view under the mouse pointer and draw its brush before the brush has a shape (VTK error
        # "vtkTransformPolyDataFilter has 0 connections"), until the first mouse move.
        self._timer.start()
        self._updateFooter(editor)

    def leave(self):
        shell = self.shell
        self._timer.stop()
        editor = segmentEditor()
        if editor is not None:
            self._record(editor)
        self.removeObservers()
        self._observedNode = None
        self._snapshot = None
        self.pendingEffect = None
        self._removeShortcuts()
        if self.footer is not None:
            if shell is not None and shell.inspectorFooter() == self.footer.widget:
                shell.setInspectorFooter(None)
            self.footer.dispose()
            self.footer = None
        self.active = False

    def _record(self, editor):
        state = self.state
        if self._timer.isActive():
            # A change of the last event loop iteration that was not followed yet
            self._timer.stop()
            self._evaluate()
        if _isModuleEntered():
            # Left before the module (workspace switch, Classic): the editor still has its effect. Without one,
            # the state already holds the user's choice: None after Esc, or the tool that is waiting to be armed.
            effect = _effectName(editor)
            if effect is not None:
                state.effect = effect
        # Else the module was left first (transient module): its exit() deactivated the effect, and the state
        # holds the effect that was active before (see _evaluate()).
        segmentationNode = editor.segmentationNode()
        if state.effect is None and segmentationNode is not None and not state.isDeselectedOn(segmentationNode):
            # Deselected on another lesion than the one now edited: the next entry is a new landing
            state.effect = _UNSET
        state.setSegmentationNode(segmentationNode)
        state.segmentId = editor.currentSegmentID() or None
        state.sourceVolumeNodeId = editor.sourceVolumeNodeID() or None

    def _restore(self, editor):
        """Restore the recorded segment and source volume, and choose the effect to arm."""
        state = self.state
        scene = slicer.mrmlScene
        segmentationNode = editor.segmentationNode()
        if (state.segmentId and segmentationNode is not None and state.segmentationNode() is segmentationNode
                and segmentationNode.GetSegmentation().GetSegment(state.segmentId) is not None
                and editor.currentSegmentID() != state.segmentId):
            editor.setCurrentSegmentID(state.segmentId)
        if editor.sourceVolumeNode() is None and state.sourceVolumeNodeId:
            sourceVolumeNode = scene.GetNodeByID(state.sourceVolumeNodeId)
            if sourceVolumeNode is not None and sourceVolumeNode.IsA("vtkMRMLScalarVolumeNode"):
                editor.setSourceVolumeNode(sourceVolumeNode)
        if state.effect is None and segmentationNode is not None and not state.isDeselectedOn(segmentationNode):
            # Deselected with Esc on another lesion (another case was opened meanwhile): a new landing
            state.effect = _UNSET
        self.pendingEffect = DEFAULT_EFFECT if not state.isRecorded() else state.effect

    #
    # Tracking of the active effect while Draw is shown
    #

    def _observe(self, editor):
        node = editor.mrmlSegmentEditorNode()
        if node is self._observedNode:
            return
        self.removeObservers()
        self._observedNode = node
        if node is not None:
            self.addObserver(node, vtk.vtkCommand.ModifiedEvent, self._onParameterNodeModified)

    def _onParameterNodeModified(self, caller, event):
        self._timer.start()

    def _evaluate(self):
        """Follow the effect chosen by the user, and arm the pending effect when the editor can edit."""
        if not self.active:
            return
        editor = segmentEditor()
        if editor is None or not _isModuleEntered():
            # The module is being left (its exit() deactivates the effect): not a choice of the user
            return
        self._observe(editor)
        snapshot = _Snapshot(editor)
        previous = self._snapshot
        self._snapshot = snapshot
        if previous is not None:
            if snapshot.effect is not None and snapshot.effect != previous.effect:
                # The user picked a tool
                self.state.effect = snapshot.effect
                self.pendingEffect = None
            elif snapshot.effect is None and previous.effect is not None:
                if snapshot.sameTarget(previous) and snapshot.canEdit:
                    # Deselected with Esc, Space or the effect button: do not fight it (on this lesion)
                    self.state.effect = None
                    self.state.setSegmentationNode(editor.segmentationNode())
                    self.pendingEffect = None
                else:
                    # Deactivated by the editor (another segmentation or segment, scene closed): arm it again
                    self.pendingEffect = previous.effect
        segmentationNode = editor.segmentationNode()
        if (snapshot.effect is None and self.state.effect is None and segmentationNode is not None
                and not self.state.isDeselectedOn(segmentationNode)):
            # Deselected with Esc on another lesion, and now a new case or lesion is edited: a new landing
            self.state.effect = _UNSET
            self.pendingEffect = DEFAULT_EFFECT
        self._tryArm(editor)
        self._updateFooter(editor)

    def _tryArm(self, editor):
        effectName = self.pendingEffect
        if not effectName:
            return
        if editor.activeEffect() is not None:
            self.pendingEffect = None
            return
        if not _isModuleEntered() or not _canEdit(editor):
            return
        if editor.effectByName(effectName) is None:
            self.pendingEffect = None
            return
        self.pendingEffect = None
        editor.setActiveEffectByName(effectName)

    #
    # Footer and shortcuts
    #

    def _installFooter(self, shell):
        if self.footer is None:
            # Created on every entry and disposed by leave(): nothing is kept across Classic
            self.footer = _SaveFooter(lambda: saveLesionDialog(self.shell))
        shell.setInspectorFooter(self.footer.widget)
        if not shell.primaryActionLabel():
            shell.setPrimaryAction(lambda: saveLesionDialog(self.shell), _("Save lesion…"))

    def _updateFooter(self, editor):
        if self.footer is not None:
            self.footer.setEnabled(editor.segmentationNode() is not None)

    def _installShortcuts(self, shell):
        if not self.saveShortcutRegistered:
            # Installed now (Draw is the current workspace) and every time Draw is entered again
            shell.addWorkspaceShortcut("draw", SAVE_LESION_SHORTCUT, lambda: saveLesionDialog(self.shell), _("Save lesion"))
            self.saveShortcutRegistered = True
        self._removeShortcuts()
        if _keyIsFree(shell.mainWindow, REDO_SHORTCUT):
            # Not a Studio shortcut: it is the Segment Editor's redo, under the key that other applications use
            shortcut = qt.QShortcut(qt.QKeySequence(REDO_SHORTCUT), shell.mainWindow)
            shortcut.setObjectName("StudioDrawRedoShortcut")
            shortcut.setContext(qt.Qt.WindowShortcut)
            shortcut.connect("activated()", _redo)
            self.redoShortcut = shortcut

    def _removeShortcuts(self):
        if self.redoShortcut is None:
            return
        try:
            self.redoShortcut.setEnabled(False)
            self.redoShortcut.setObjectName("")
            self.redoShortcut.deleteLater()
        except Exception:
            # Deleted with the main window
            pass
        self.redoShortcut = None


def _keyIsFree(mainWindow, keySequence):
    """True if no enabled shortcut or action of the main window already uses ``keySequence``.

    A second shortcut would make the key ambiguous, and Qt would trigger neither (on Windows the Segment Editor
    already binds Ctrl+Y as the standard redo key).
    """
    portable = qt.QKeySequence(keySequence).toString(qt.QKeySequence.PortableText)
    for shortcut in mainWindow.findChildren("QShortcut"):
        if shortcut.enabled and shortcut.key.toString(qt.QKeySequence.PortableText) == portable:
            return False
    for action in mainWindow.actions():
        if action.enabled and any(key.toString(qt.QKeySequence.PortableText) == portable for key in action.shortcuts()):
            return False
    return True


#: Widgets that take typed text: Ctrl+Y there must not change the lesion (the user is typing, e.g. a segment name)
_TEXT_INPUT_CLASSES = ("QLineEdit", "QAbstractSpinBox", "QTextEdit", "QPlainTextEdit", "ctkConsole")


def _redo():
    focusWidget = slicer.app.focusWidget()
    if focusWidget is not None and any(focusWidget.inherits(className) for className in _TEXT_INPUT_CLASSES):
        return
    editor = segmentEditor()
    if editor is not None and _isModuleEntered():
        editor.redo()


class _SaveFooter:
    """Sticky inspector footer of Draw with the primary "Save lesion…" button."""

    def __init__(self, callback):
        self.widget = qt.QWidget()
        self.widget.setObjectName("StudioDrawFooter")
        ui.bindStyleSheet(self.widget, lambda: (
            f"QWidget#StudioDrawFooter {{ background-color: {ui.token('window').name()}; "
            f"border-top: 1px solid {ui.token('hairline').name()}; }}"))
        self.widget.setAttribute(qt.Qt.WA_StyledBackground, True)
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(0)
        self.saveButton = ui.primaryButton(_("Save lesion…"))
        self.saveButton.setObjectName("StudioSaveLesionButton")
        self.saveButton.setToolTip(_("Save the lesion as a NIfTI mask on the grid of the scan ({shortcut})").format(
            shortcut=qt.QKeySequence(SAVE_LESION_SHORTCUT).toString(qt.QKeySequence.NativeText)))
        self.saveButton.connect("clicked()", callback)
        layout.addWidget(self.saveButton)

    def setEnabled(self, enabled):
        if self.saveButton.enabled != enabled:
            self.saveButton.setEnabled(enabled)

    def dispose(self):
        """Unbind the stylesheet and delete the widget (it holds the callback, and so the Draw session)."""
        ui.bindStyleSheet(self.widget, None)
        try:
            self.saveButton.disconnect("clicked()")
        except Exception:
            pass
        self.widget.hide()
        self.widget.setParent(None)
        self.widget.deleteLater()
        self.widget = None
        self.saveButton = None


#: Draw sessions of the shells (one shell per main window)
_sessions = weakref.WeakKeyDictionary()


def session(shell):
    """The Draw session of ``shell`` (created on first use)."""
    drawSession = _sessions.get(shell)
    if drawSession is None:
        drawSession = _DrawSession(shell)
        _sessions[shell] = drawSession
    return drawSession


def drawState(shell):
    """What Draw recorded when it was last left (``DrawState``)."""
    return session(shell).state


#
# Hooks
#


def enter(shell):
    """Draw becomes the current workspace: restore the tool, show "Save lesion…" and the Draw shortcuts."""
    session(shell).enter()


def leave(shell):
    """Draw is left (another workspace, a transient module or Classic): record the tool, remove the footer."""
    drawSession = _sessions.get(shell)
    if drawSession is not None and drawSession.active:
        drawSession.leave()


#
# Save lesion
#


def _sanitizeFileName(name):
    """A file name made of letters, digits, ".", "_" and "-" only."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name)).strip("._-")
    return re.sub(r"_+", "_", name)


def _storageFileName(node):
    storageNode = node.GetStorageNode() if node is not None and hasattr(node, "GetStorageNode") else None
    fileName = storageNode.GetFileName() if storageNode is not None else None
    return str(fileName) if fileName else None


def _isInside(path, directory):
    if not directory:
        return False
    try:
        path = os.path.normcase(os.path.realpath(path))
        directory = os.path.normcase(os.path.realpath(directory))
        return os.path.commonpath([path, directory]) == directory
    except ValueError:
        # Different drives
        return False


def _applicationDirectories():
    """Folders that never hold the user's files: the application, the temporary and the cache folders."""
    directories = [slicer.app.slicerHome, slicer.app.applicationDirPath(), slicer.app.temporaryPath, tempfile.gettempdir()]
    for attribute in ("cachePath", "defaultCachePath"):
        value = getattr(slicer.app, attribute, None)
        if isinstance(value, str):
            directories.append(value)
    try:
        directories.append(slicer.mrmlScene.GetCacheManager().GetRemoteCacheDirectory())
    except Exception:
        pass
    try:
        from . import downloads

        directories.append(downloads.cacheDirectory())
    except Exception:
        pass
    return [directory for directory in directories if directory]


def isUserFile(path):
    """True if ``path`` is a file of the user: not in the application, temporary or cache folders."""
    if not path or not os.path.isfile(path):
        return False
    return not any(_isInside(path, directory) for directory in _applicationDirectories())


def defaultSaveDirectory(sourceVolumeNode=None):
    """Folder proposed by "Save lesion…": the last one used, else the folder of the scan (if it is a file of
    the user), else Documents, else the home folder. Never the application folder.
    """
    lastFolder = str(slicer.util.settingsValue(LAST_SAVE_FOLDER_KEY, "") or "")
    if lastFolder and os.path.isdir(lastFolder):
        return lastFolder
    sourceFile = _storageFileName(sourceVolumeNode)
    if sourceFile and isUserFile(sourceFile):
        return os.path.dirname(os.path.abspath(sourceFile))
    home = qt.QDir.homePath()
    documents = os.path.join(home, "Documents")
    return documents if os.path.isdir(documents) else home


def defaultLesionFileName(segmentationNode, sourceVolumeNode=None):
    """File name proposed by "Save lesion…", for example "sub-01_lesion.nii.gz" or "patient_T1_lesion.nii.gz".

    The BIDS subject of the scan wins; else the scan of a "Lesions (<scan>)" segmentation, else the
    segmentation name. A name that already ends with "lesion" is not repeated.
    """
    sourceFile = _storageFileName(sourceVolumeNode)
    candidates = [os.path.basename(sourceFile) if sourceFile else None,
                  sourceVolumeNode.GetName() if sourceVolumeNode is not None else None,
                  segmentationNode.GetName() if segmentationNode is not None else None]
    for candidate in candidates:
        match = _BIDS_SUBJECT_PATTERN.search(candidate or "")
        if match:
            return f"{match.group(1)}_lesion.nii.gz"
    base = segmentationNode.GetName() if segmentationNode is not None else ""
    match = _SCAN_LESIONS_PATTERN.match(base)
    if match:
        base = match.group(1)
    base = _sanitizeFileName(base)
    if base.lower() in _GENERIC_NAMES:
        return "lesion.nii.gz"
    if base.lower().endswith("lesion"):
        return f"{base}.nii.gz"
    return f"{base}_lesion.nii.gz"


def _withNiftiExtension(path):
    if not path.lower().endswith(NIFTI_EXTENSIONS):
        path += ".nii.gz"
    return path


def lesionNodes(editor=None):
    """``(segmentationNode, sourceVolumeNode)`` edited in Draw (the Segment Editor), Nones if there is none."""
    editor = editor or segmentEditor()
    if editor is None:
        return None, None
    return editor.segmentationNode(), editor.sourceVolumeNode()


def _castToUnsignedChar(labelmapNode):
    """Store a label map with at most 255 labels as uint8 (the usual type of lesion masks), else keep its type."""
    imageData = labelmapNode.GetImageData()
    if imageData is None or imageData.GetScalarType() == vtk.VTK_UNSIGNED_CHAR:
        return
    low, high = imageData.GetScalarRange()
    if low < 0 or high > 255:
        return
    cast = vtk.vtkImageCast()
    cast.SetInputData(imageData)
    cast.SetOutputScalarTypeToUnsignedChar()
    cast.Update()
    labelmapNode.SetAndObserveImageData(cast.GetOutput())


def saveLesion(path, segmentationNode, referenceVolumeNode=None, segmentIds=None):
    """Write the segments of ``segmentationNode`` as a NIfTI label map (official Slicer writer) and return the path.

    :param path: ``.nii.gz`` or ``.nii`` file (``.nii.gz`` is appended to other names).
    :param referenceVolumeNode: the file has the grid of this volume (the scan or the template), else the
      reference geometry of the segmentation.
    :param segmentIds: segments to write (default: all). Segments get the values 1, 2... in this order, so a
      single lesion segment is a 0/1 mask (uint8 up to 255 segments). A label map holds one value per voxel:
      where segments overlap, the voxel gets the value of the later segment, so an earlier segment can have
      fewer voxels in the file than in the editor (``saveLesionDialog()`` says so when it saves several segments).
    Raise ``RuntimeError`` on failure. No node is left in the scene.
    """
    if segmentationNode is None:
        raise RuntimeError(_("There is no lesion to save"))
    path = _withNiftiExtension(os.path.abspath(str(path)))
    segmentation = segmentationNode.GetSegmentation()
    if segmentIds is None:
        segmentIds = [segmentation.GetNthSegmentID(index) for index in range(segmentation.GetNumberOfSegments())]
    if not segmentIds:
        raise RuntimeError(_("The segmentation {name} has no segment").format(name=segmentationNode.GetName()))
    idArray = vtk.vtkStringArray()
    for segmentId in segmentIds:
        idArray.InsertNextValue(segmentId)

    scene = slicer.mrmlScene
    nodeIdsBefore = {scene.GetNthNode(index).GetID() for index in range(scene.GetNumberOfNodes())}
    labelmapNode = scene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode", scene.GenerateUniqueName("StudioLesionExport"))
    labelmapNode.SetHideFromEditors(True)
    try:
        exported = slicer.modules.segmentations.logic().ExportSegmentsToLabelmapNode(
            segmentationNode, idArray, labelmapNode, referenceVolumeNode, slicer.vtkSegmentation.EXTENT_REFERENCE_GEOMETRY)
        if not exported:
            raise RuntimeError(_("Failed to export the lesion {name}").format(name=segmentationNode.GetName()))
        _castToUnsignedChar(labelmapNode)
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        if not slicer.util.saveNode(labelmapNode, path):
            raise RuntimeError(_("Failed to write {path}").format(path=path))
    finally:
        # Remove the temporary label map and what the export added (its color table, display and storage nodes)
        for index in reversed(range(scene.GetNumberOfNodes())):
            node = scene.GetNthNode(index)
            if node is not None and node.GetID() not in nodeIdsBefore:
                scene.RemoveNode(node)
    return path


def saveLesionDialog(shell=None, path=None):
    """"Save lesion…": choose a NIfTI file (unless ``path`` is given) and save the lesion edited in Draw.

    Return the written path, or None if cancelled or failed (errors are shown).
    """
    segmentationNode, sourceVolumeNode = lesionNodes()
    if segmentationNode is None:
        slicer.util.errorDisplay(_("There is no lesion to save. Open a scan or start on the MNI template, then draw the lesion."))
        return None
    if path is None:
        defaultPath = os.path.join(defaultSaveDirectory(sourceVolumeNode), defaultLesionFileName(segmentationNode, sourceVolumeNode))
        path = qt.QFileDialog.getSaveFileName(slicer.util.mainWindow(), _("Save lesion"), defaultPath, NIFTI_FILTER)
        if not path:
            return None
    # The file keeps the grid of the lesion (for example 91x109x91 for a mask on the FSL MNI152 2 mm grid, as
    # connectomes expect), not the grid of the scan or template it is drawn on. The scan's grid is only used for
    # a segmentation without a reference geometry.
    referenceGeometry = segmentationNode.GetSegmentation().GetConversionParameter(
        slicer.vtkSegmentationConverter.GetReferenceImageGeometryParameterName())
    try:
        with slicer.util.WaitCursor():
            path = saveLesion(path, segmentationNode, None if referenceGeometry else sourceVolumeNode)
    except Exception as e:
        logging.error(f"Studio: cannot save the lesion:\n{traceback.format_exc()}")
        slicer.util.errorDisplay(_("The lesion could not be saved."), detailedText=str(e))
        return None
    folder = os.path.dirname(path)
    if folder != slicer.util.settingsValue(LAST_SAVE_FOLDER_KEY, ""):
        qt.QSettings().setValue(LAST_SAVE_FOLDER_KEY, folder)
    if shell is not None and shell.isActive():
        segmentCount = segmentationNode.GetSegmentation().GetNumberOfSegments()
        if segmentCount > 1:
            message = _("Saved {count} segments to {name} (values 1 to {count}; where they overlap, the later one wins)").format(
                count=segmentCount, name=os.path.basename(path))
        else:
            message = _("Saved lesion to {name}").format(name=os.path.basename(path))
        shell.toast(message, 4000 if segmentCount > 1 else 3000)
    return path

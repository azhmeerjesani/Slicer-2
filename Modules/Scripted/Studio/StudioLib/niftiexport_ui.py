"""Export as NIfTI: the dialog and its entry points (A-11, decision D-N1; VISUAL 4.7 dialog chrome).

- ``ExportDialog`` (``StudioNiftiExportDialog``): what to export (every image and segmentation that is open, the ones the
  user picked checked, so several are exported at once), the destination folder (``<project>/Exports/nifti`` in a
  project, else the last folder used), the format (.nii.gz or .nii), for segmentations "One label map" or "One mask per
  segment" and an optional reference image (nearest neighbour), and a summary of the files to be written. Existing files
  are replaced only after a question. The images are written by the background writer with a progress bar; a toast
  then says what was written, with "Show in Finder/Explorer/Files".
- Entry points: ``exportNodesDialog`` (Library's Open now ⋯ and Search), ``exportProjectDialog`` (the project menu and
  Search), ``convertDicomDialog`` (Search), ``exportLesionMasksDialog`` (Draw's lesion ⋯) and ``exportMapDialog`` (Map).

The engine is ``niftiexport``. Only fork-only keys are written (``Studio/NiftiExport/*``).
"""

import logging
import os
import sys
import traceback

import qt
import slicer
from slicer.i18n import tr as _

from . import niftiexport, ui

DIALOG_OBJECT_NAME = "StudioNiftiExportDialog"
DIALOG_WIDTH = 600
#: Rows of the list of images shown without scrolling
LIST_ROWS = 6
#: Rows the image list keeps when the dialog is squeezed (it scrolls)
LIST_MIN_ROWS = 2
LIST_ROW_HEIGHT = 30
#: Names shown in the summary before "and N more"
SUMMARY_NAMES = 4

_dialog = None


def _shell():
    module = sys.modules.get("StudioLib.shell")
    current = module.instance() if module is not None and hasattr(module, "instance") else None
    return current if current is not None and current.isActive() else None


def message(text, level="info", action=None, actionText=None, ms=4000, toolTip=None):
    shell = _shell()
    if shell is not None:
        shell.toast(text, ms=ms, action=action, actionText=actionText, level=level, toolTip=toolTip)
        return
    # Classic (the Map module's Export card): the status bar
    logging.info(f"Slicer Studio: {text}")
    if slicer.util.mainWindow() is not None:
        slicer.util.showStatusMessage(text, max(ms, 5000))


def _projects():
    try:
        from . import projects

        return projects
    except Exception:
        return None


def fileManagerName():
    projects = _projects()
    return projects.fileManagerName() if projects is not None else _("Files")


def showInFileManager(path):
    projects = _projects()
    if projects is not None:
        return projects.showInFileManager(path)
    return qt.QDesktopServices.openUrl(qt.QUrl.fromLocalFile(path))


def shortPath(path):
    projects = _projects()
    return projects.shortPath(path) if projects is not None else path


def _setting(key, default=""):
    value = qt.QSettings().value(key)
    return str(value) if value not in (None, "") else default


def defaultFolder():
    """``<project>/Exports/nifti`` of the open project, else the last export folder, else (web build) the folder its file
    dialogs start in (/data, which the host sees; not the container HOME), else Documents (or the home folder).
    """
    projectFolder = niftiexport.projectExportFolder(create=False)
    if projectFolder:
        return projectFolder
    last = _setting(niftiexport.LAST_FOLDER_KEY)
    if last and os.path.isdir(last):
        return last
    try:
        from .importer import webStartFolder

        webFolder = webStartFolder()
    except Exception:
        webFolder = ""
    if webFolder:
        return webFolder
    home = qt.QDir.homePath()
    documents = os.path.join(home, "Documents")
    return documents if os.path.isdir(documents) else home


def _isAlive(widget):
    try:
        widget.objectName
        return True
    except Exception:
        return False


def _connectIdClicked(group, slot):
    """Connect a QButtonGroup's id click: buttonClicked(int) is gone in Qt 6; idClicked(int) exists since Qt 5.15."""
    group.connect("idClicked(int)" if hasattr(group, "idClicked") else "buttonClicked(int)", slot)


#
# Running an export (main thread prepares, the background writer writes)
#


def plainError(error):
    """An I/O error in plain words ("Permission denied"), without the error number and the internal temporary file name."""
    from .autosave import plainOSError

    return plainOSError(error) if isinstance(error, OSError) else str(error)


class ExportRunner:
    """Run export jobs one image at a time: the next image of a job is read from the scene on the main thread
    (``Job.iterTasks``) only after the previous one was written by the background writer (``autosave_io.backgroundWriter()``,
    or ``executor``). The event loop runs between two images, so the dialog repaints and Esc works even for one segmentation
    with hundreds of segments written as masks, and only one full-grid image is held in memory at a time.

    ``onProgress(done, total, title)`` (images written of the images planned) and ``onFinished(result)`` are called on the
    main thread. ``cancel()`` stops after the current image. ``remove``: files of an earlier project export that this one
    does not write again (``niftiexport.earlierExportFiles``), removed when it starts.
    """

    #: Images handed to the writer and not written yet: the next image is prepared once this many are pending
    MAX_PENDING_WRITES = 1

    def __init__(self, jobs, executor=None, readme=None, onProgress=None, onFinished=None, remove=None):
        self.jobs = list(jobs)
        self.executor = executor
        self.readme = readme
        self.remove = list(remove or [])
        self.onProgress = onProgress
        self.onFinished = onFinished
        self.result = niftiexport.Result(os.path.dirname(self.jobs[0].path) if self.jobs else None)
        #: Jobs started (the job in progress is ``jobs[index - 1]``)
        self.index = 0
        #: Images written (or failed), and the images planned
        self.done = 0
        self.total = sum(len(job.images()) for job in self.jobs)
        self.cancelled = False
        self.finished = False
        self._waiting = 0
        self._job = None
        self._tasks = None
        self._scheduled = False

    def _executor(self):
        if self.executor is None:
            from . import autosave_io

            self.executor = autosave_io.backgroundWriter()
        return self.executor

    def start(self):
        if self.remove:
            niftiexport.removeFiles(self.remove, self.result)
        self._step()

    def cancel(self):
        self.cancelled = True

    def _schedule(self):
        """Run the next step from the event loop (never nested in a callback), once."""
        if not self._scheduled and not self.finished:
            self._scheduled = True
            qt.QTimer.singleShot(0, self._step)

    def _progress(self):
        if self.onProgress is not None:
            self.onProgress(min(self.done, self.total), max(self.total, self.done), self._job.title if self._job is not None else "")

    def _step(self):
        """Prepare and hand over the next image (or start the next job, or finish)."""
        self._scheduled = False
        if self.finished:
            return
        if self.cancelled:
            self._endJob()
            if self._waiting <= 0:
                self._finish()
            return
        if self._waiting >= self.MAX_PENDING_WRITES:
            # _taskDone schedules the next step
            return
        if self._tasks is None:
            if self.index >= len(self.jobs):
                if self._waiting <= 0:
                    self._finish()
                return
            self._job = self.jobs[self.index]
            self.index += 1
            self._tasks = self._job.iterTasks()
            self._progress()
        job = self._job
        try:
            task = next(self._tasks)
        except StopIteration:
            self._endJob()
            self._schedule()
            return
        except Exception as e:
            self._failure(job.nodeName, e, traceback.format_exc())
            self._endJob()
            self._schedule()
            return
        if task.data is None:
            try:
                self.result.add(niftiexport._writeWithSlicer(task))
            except Exception as e:
                self._failure(job.nodeName, e, traceback.format_exc())
            self.done += 1
            self._progress()
            self._schedule()
            return
        self._waiting += 1
        self._executor().submit(self._write, task, onDone=self._onWritten, onError=lambda error, name=job.nodeName: self._onFailed(name, error))
        if self._waiting < self.MAX_PENDING_WRITES:
            self._schedule()

    @staticmethod
    def _write(task):
        """Write one image (writer thread), then release its array: only the images not written yet stay in memory."""
        try:
            return task.write()
        finally:
            task.data = None

    def _endJob(self):
        """Close the job in progress (its generator, so that nothing it prepared is kept) and keep its notes."""
        if self._job is None:
            return
        tasks, self._tasks = self._tasks, None
        if tasks is not None:
            try:
                tasks.close()
            except Exception:
                logging.debug("Slicer Studio: closing a NIfTI export job failed", exc_info=True)
        self.result.notes += self._job.notes
        self._job = None

    def _onWritten(self, files):
        self.result.add(files)
        self._taskDone()

    def _onFailed(self, name, error):
        self._failure(name, error)
        self._taskDone()

    def _failure(self, name, error, details=None):
        """Record a failed job. A file that cannot be written (read-only share, full disk, unplugged drive) is expected: it is
        reported in plain words and logged as a warning, so that it does not raise the error badge. Anything else is a bug.
        """
        if isinstance(error, OSError):
            reason = plainError(error)
            logging.warning(f"Slicer Studio: NIfTI export of {name} could not be written: {error}")
            if self.result.writeFailure is None:
                self.result.writeFailure = reason
            self.result.errors.append(f"{name}: {reason}")
            self.result.failures.append((name, reason))
            return
        logging.error(f"Slicer Studio: NIfTI export of {name} failed:\n{details or error}")
        self.result.errors.append(f"{name}: {error}")
        self.result.failures.append((name, str(error)))

    def _taskDone(self):
        self._waiting -= 1
        self.done += 1
        self._progress()
        self._schedule()

    def _finish(self):
        self.finished = True
        if self.readme and self.result.images:
            try:
                niftiexport.writeReadme(self.readme[0], self.jobs[:self.index], self.result, self.readme[1])
                self.result.files.append(self.readme[0])
            except OSError as e:
                logging.warning(f"Slicer Studio: the NIfTI export README could not be written: {e}")
                self.result.errors.append(f"{os.path.basename(self.readme[0])}: {plainError(e)}")
        if self.onProgress is not None:
            total = max(self.total, self.done)
            self.onProgress(total, total, "")
        if self.onFinished is not None:
            self.onFinished(self.result)

    def wait(self, timeoutS=600):
        """Process events until the export is finished (tests and scripts)."""
        import time

        deadline = time.time() + timeoutS
        while not self.finished and time.time() < deadline:
            slicer.app.processEvents()
            time.sleep(0.005)
        return self.finished


def resultText(result):
    """"Exported 3 NIfTI images to nifti" (with the errors counted)."""
    count = len(result.images)
    folder = os.path.basename(result.folder or "") or (result.folder or "")
    text = (_("Exported 1 NIfTI image to {folder}") if count == 1 else _("Exported {count} NIfTI images to {folder}")).format(count=count, folder=folder)
    if result.errors:
        text += " · " + (_("1 failed") if len(result.errors) == 1 else _("{count} failed").format(count=len(result.errors)))
        failures = getattr(result, "failures", None)
        if failures:
            # Which image and why (the first one; the others are in the log): the images mode writes no README
            name, reason = failures[0]
            name = name if len(name) <= 40 else name[:39].rstrip() + "…"
            reason = reason if len(reason) <= 80 else reason[:79].rstrip() + "…"
            text += f" ({name}: {reason})"
    return text


NOTES_IN_MESSAGE = 2


def notesText(notes):
    """The notes of an export ("Lesions: empty segments were skipped: Edema") for the message: the first
    ``NOTES_IN_MESSAGE`` of them, and how many more are in the README and the log.
    """
    unique = list(dict.fromkeys(str(note) for note in notes if note))
    text = " · ".join(unique[:NOTES_IN_MESSAGE])
    more = len(unique) - NOTES_IN_MESSAGE
    if more > 0:
        text += " · " + (_("1 more note") if more == 1 else _("{count} more notes").format(count=more))
    return text


def announce(result):
    """The toast after an export: what was written, the notes (empty segments that were skipped, an empty threshold
    mask, a transform applied to the copy) and "Show in Finder/Explorer/Files".
    """
    if not result.images and result.errors and getattr(result, "writeFailure", None):
        message(_("The files could not be written to {folder}: {reason}").format(folder=shortPath(result.folder), reason=result.writeFailure),
                level="warning", ms=8000, toolTip=result.folder)
        return
    if not result.images and result.errors:
        message(_("Nothing was exported: {error}").format(error=result.errors[0]), level="warning", ms=8000)
        return
    folder = result.folder

    def show():
        showInFileManager(folder)

    text = resultText(result)
    removed = len(getattr(result, "removed", None) or [])
    if removed:
        text += " · " + (_("removed 1 file of an earlier export") if removed == 1 else
                         _("removed {count} files of an earlier export").format(count=removed))
    if result.notes:
        text += " · " + notesText(result.notes)
    message(text, level="warning" if result.errors else "info", action=show,
            actionText=_("Show in {manager}").format(manager=fileManagerName()), ms=10000 if result.notes else 6000, toolTip=shortPath(folder))
    for note in result.notes:
        logging.info(f"Slicer Studio NIfTI export: {note}")


#
# The dialog
#


def _extraFilesText(sidecars, tables):
    """"1 sidecar", "14 sidecars" or "1 sidecar and 1 label table": label tables are named only when a label map writes them."""
    sidecarText = (_("1 sidecar") if sidecars == 1 else _("{count} sidecars").format(count=sidecars)) if sidecars else ""
    tableText = (_("1 label table") if tables == 1 else _("{count} label tables").format(count=tables)) if tables else ""
    if sidecarText and tableText:
        return _("{sidecars} and {tables}").format(sidecars=sidecarText, tables=tableText)
    return sidecarText or tableText


class ExportDialog(qt.QDialog):
    """"Export as NIfTI" (module docstring). ``mode``: "images", "project" or "dicom" (title, names and README differ)."""

    def __init__(self, nodes=None, checked=None, mode="images", folder=None, segmentationMode=None, mapOptions=None, parent=None, executor=None,
                 title=None, body=None):
        qt.QDialog.__init__(self, parent or slicer.util.mainWindow())
        self.setObjectName(DIALOG_OBJECT_NAME)
        self.setWindowTitle(_("Export as NIfTI"))
        self.setModal(True)
        self.mode = mode
        self.executor = executor
        self.mapOptions = dict(mapOptions or {})
        self._existingShown = False
        self._existing = []
        self._stale = []
        self._summaryHeight = None
        self.runner = None
        self.lastResult = None
        if nodes is None:
            nodes = niftiexport.projectNodes() if mode == "project" else niftiexport.dicomNodes() if mode == "dicom" else niftiexport.exportableNodes()
        self.nodeIds = [node.GetID() for node in nodes]
        if checked is not None:
            checkedIds = {node.GetID() if hasattr(node, "GetID") else str(node) for node in checked}
        else:
            # Everything but the templates (public files, known by name)
            checkedIds = {node.GetID() for node in nodes if niftiexport.kindOf(node) != niftiexport.KIND_TEMPLATE}
        self.setMinimumWidth(DIALOG_WIDTH)
        self.setMaximumWidth(DIALOG_WIDTH + 120)
        layout = qt.QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 22)
        layout.setSpacing(14)
        if title is None:
            title = {"project": _("Export project as NIfTI"), "dicom": _("Convert DICOM to NIfTI")}.get(mode, _("Export as NIfTI"))
        if body is None:
            body = _("Standard NIfTI files that FSL, SPM, AFNI, nibabel and official 3D Slicer read, with left and right exactly as shown here. "
                     "Each image gets a .json sidecar.")
        header, self.titleLabel, self.bodyLabel = ui.dialogHeader("download", title, body, "StudioNiftiExport")
        layout.addLayout(header)

        # What to export: every image that is open, the chosen ones checked
        listHeader = qt.QHBoxLayout()
        listHeader.addWidget(ui.sectionLabel(_("Images"), "StudioNiftiExportListTitle"))
        listHeader.addStretch(1)
        self.selectAllLink = ui.linkButtonWidget(_("Select all"), self.selectAll, "StudioNiftiExportSelectAll")
        listHeader.addWidget(self.selectAllLink)
        layout.addLayout(listHeader)
        self.list = qt.QListWidget()
        self.list.setObjectName("StudioNiftiExportList")
        self.list.setSelectionMode(qt.QAbstractItemView.NoSelection)
        self.list.setUniformItemSizes(True)
        self.list.setHorizontalScrollBarPolicy(qt.Qt.ScrollBarAlwaysOff)
        for node in nodes:
            kind = niftiexport.kindOf(node)
            kindText = niftiexport.kindText(kind)
            # A 4D image is written whole (every time point): the row says how many
            count = niftiexport.timePoints(node)
            if count > 1:
                kindText = _("{kind}, {count} time points").format(kind=kindText, count=count)
            item = qt.QListWidgetItem(f"{node.GetName()}  ·  {kindText}")
            item.setFlags(qt.Qt.ItemIsEnabled | qt.Qt.ItemIsUserCheckable)
            item.setCheckState(qt.Qt.Checked if node.GetID() in checkedIds else qt.Qt.Unchecked)
            item.setData(qt.Qt.UserRole, node.GetID())
            item.setSizeHint(qt.QSize(0, LIST_ROW_HEIGHT))
            item.setToolTip(_("{name} ({kind})").format(name=node.GetName(), kind=kindText))
            self.list.addItem(item)
        # Up to LIST_ROWS rows when there is room. The height is flexible (not fixed): on a short screen or at a large app font the
        # dialog is capped to the screen, and a fixed-height list would be drawn under the folder card; this one shrinks and scrolls
        rows = max(1, min(LIST_ROWS, len(nodes)))
        self.list.setMaximumHeight(rows * LIST_ROW_HEIGHT + 6)
        self.list.setMinimumHeight(min(rows, LIST_MIN_ROWS) * LIST_ROW_HEIGHT + 6)
        self.list.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Preferred)
        self.list.connect("itemChanged(QListWidgetItem*)", lambda item: self.updateSummary())
        layout.addWidget(self.list)
        self.emptyLabel = ui.label(_("Nothing to export: open a scan, draw a lesion or open a map first."), "body", "text3", "StudioNiftiExportEmpty", wrap=True)
        layout.addWidget(self.emptyLabel)
        self.emptyLabel.setVisible(not nodes)
        self.list.setVisible(bool(nodes))
        self.selectAllLink.setVisible(len(nodes) > 1)

        # Destination
        self.folder = os.path.normpath(folder or (niftiexport.projectExportFolder(create=False) if mode == "project" else None) or defaultFolder())
        folderCard, self.folderLabel, self.folderMeta, buttons = ui.folderCard("folder", "StudioNiftiExportFolderCard")
        self.changeButton = qt.QPushButton(_("Change…"))
        self.changeButton.setObjectName("StudioNiftiExportChangeFolder")
        ui.setRole(self.changeButton, "secondary")
        self.changeButton.connect("clicked()", self.chooseFolder)
        buttons.addWidget(self.changeButton)
        layout.addWidget(folderCard)

        # Options: the format, and for segmentations the mode and the reference image
        options = qt.QGridLayout()
        options.setHorizontalSpacing(12)
        options.setVerticalSpacing(10)
        options.setColumnStretch(1, 1)
        options.addWidget(ui.label(_("Format"), "body", "text2", "StudioNiftiExportFormatLabel"), 0, 0)
        formatFrame, self.formatGroup = ui.segmented([_(".nii.gz (compressed)"), _(".nii")])
        formatFrame.setObjectName("StudioNiftiExportFormat")
        formatIndex = 1 if _setting(niftiexport.FORMAT_KEY) == ".nii" else 0
        self.formatGroup.button(formatIndex).setChecked(True)
        _connectIdClicked(self.formatGroup, lambda index: self.updateSummary())
        options.addWidget(formatFrame, 0, 1, qt.Qt.AlignLeft)
        self.segmentationLabel = ui.label(_("Segmentations"), "body", "text2", "StudioNiftiExportSegmentationLabel")
        options.addWidget(self.segmentationLabel, 1, 0)
        modeFrame, self.segmentationGroup = ui.segmented([_("One label map"), _("One mask per segment")])
        modeFrame.setObjectName("StudioNiftiExportSegmentationMode")
        self.segmentationFrame = modeFrame
        segmentationMode = segmentationMode or (niftiexport.SEGMENTATION_MASKS if mode == "project" else _setting(niftiexport.SEGMENTATION_MODE_KEY,
                                                                                                                  niftiexport.SEGMENTATION_LABELMAP))
        self.segmentationGroup.button(1 if segmentationMode == niftiexport.SEGMENTATION_MASKS else 0).setChecked(True)
        _connectIdClicked(self.segmentationGroup, lambda index: self.updateSummary())
        options.addWidget(modeFrame, 1, 1, qt.Qt.AlignLeft)
        self.referenceLabel = ui.label(_("Grid"), "body", "text2", "StudioNiftiExportReferenceLabel")
        options.addWidget(self.referenceLabel, 2, 0)
        self.referenceCombo = qt.QComboBox()
        self.referenceCombo.setObjectName("StudioNiftiExportReference")
        self.referenceCombo.addItem(_("The segmentation's own grid"), "")
        for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
            if (node.GetHideFromEditors() or niftiexport.isFigure(node) or node.IsA("vtkMRMLLabelMapVolumeNode")
                    or niftiexport.kindOf(node) == niftiexport.KIND_MAP):
                continue
            self.referenceCombo.addItem(_("Resampled onto {name} (nearest neighbour)").format(name=node.GetName()), node.GetID())
        self.referenceCombo.setToolTip(_("Masks keep the grid they were drawn on, unless you choose an image here"))
        self.referenceCombo.setSizeAdjustPolicy(qt.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.referenceCombo.setMinimumContentsLength(24)
        self.referenceCombo.connect("currentIndexChanged(int)", lambda index: self.updateSummary())
        options.addWidget(self.referenceCombo, 2, 1)
        layout.addLayout(options)

        # Summary of the files to be written, and a warning when some already exist
        self.summaryLabel = ui.label("", "caption", "text2", "StudioNiftiExportSummary", wrap=True)
        self.summaryLabel.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
        layout.addWidget(self.summaryLabel)
        self.existingNotice = ui.notice("", "warning", objectName="StudioNiftiExportExisting")
        layout.addWidget(self.existingNotice)
        self.progress = qt.QProgressBar()
        self.progress.setObjectName("StudioNiftiExportProgress")
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.hide()
        layout.addWidget(self.progress)

        # Footer (VISUAL 4.7): a line on the left, Cancel and the single primary button on the right
        footer = qt.QHBoxLayout()
        footer.setSpacing(10)
        self.footerLabel = ui.label(_("Left stays left: the file's affine is 3D Slicer's IJK to RAS matrix."), "caption", "text3",
                                    "StudioNiftiExportFooter", wrap=True)
        footer.addWidget(self.footerLabel, 1)
        self.cancelButton = qt.QPushButton(_("Cancel"))
        self.cancelButton.setObjectName("StudioNiftiExportCancel")
        self.cancelButton.setMinimumHeight(36)
        ui.setRole(self.cancelButton, "secondary")
        self.cancelButton.connect("clicked()", self.reject)
        footer.addWidget(self.cancelButton)
        self.exportButton = qt.QPushButton(_("Export"))
        self.exportButton.setObjectName("StudioNiftiExportButton")
        self.exportButton.setDefault(True)
        self.exportButton.setAutoDefault(True)
        self.exportButton.setMinimumHeight(36)
        self.exportButton.setMinimumWidth(96)
        ui.setRole(self.exportButton, "primary")
        self.exportButton.connect("clicked()", self.export)
        footer.addWidget(self.exportButton)
        layout.addLayout(footer)
        ui.dialogChrome(self, styleSheet=self._styleSheet)
        self.updateSummary()
        self._scrollToFirstChecked()

    @staticmethod
    def _styleSheet():
        return (ui.settingsStyleSheet()
                + f"QListWidget#StudioNiftiExportList {{ background-color: {ui.token('card-low').name()}; border: 1px solid {ui.token('hairline').name()};"
                " border-radius: 10px; padding: 2px; }")

    #
    # State
    #

    def checkedNodes(self):
        nodes = []
        for row in range(self.list.count):
            item = self.list.item(row)
            if item.checkState() == qt.Qt.Checked:
                node = slicer.mrmlScene.GetNodeByID(item.data(qt.Qt.UserRole))
                if node is not None:
                    nodes.append(node)
        return nodes

    def _scrollToFirstChecked(self):
        """Bring the first checked image into view (the list shows a few rows; the chosen image may be further down)."""
        for row in range(self.list.count):
            item = self.list.item(row)
            if item.checkState() == qt.Qt.Checked:
                self.list.scrollToItem(item, qt.QAbstractItemView.EnsureVisible)
                return

    def showEvent(self, event):
        qt.QDialog.showEvent(self, event)
        # Before the dialog is shown the list's viewport has no final height yet
        self._scrollToFirstChecked()
        # The first show sizes the dialog with QDialog's adjustSize, which Qt caps at 2/3 of the screen height (512 px at 1366x768):
        # the wrapped texts need more, so the squeezed layout would draw the list under the folder card and clip the controls.
        # A resize has no such cap.
        self._fitHeight()

    def setChecked(self, nodeIds):
        nodeIds = set(nodeIds)
        for row in range(self.list.count):
            item = self.list.item(row)
            item.setCheckState(qt.Qt.Checked if item.data(qt.Qt.UserRole) in nodeIds else qt.Qt.Unchecked)
        self.updateSummary()

    def selectAll(self):
        self.setChecked(self.nodeIds)

    def extension(self):
        return ".nii" if self.formatGroup.checkedId() == 1 else ".nii.gz"

    def segmentationMode(self):
        return niftiexport.SEGMENTATION_MASKS if self.segmentationGroup.checkedId() == 1 else niftiexport.SEGMENTATION_LABELMAP

    def setSegmentationMode(self, mode):
        self.segmentationGroup.button(1 if mode == niftiexport.SEGMENTATION_MASKS else 0).setChecked(True)
        self.updateSummary()

    def setFormat(self, extension):
        self.formatGroup.button(1 if extension == ".nii" else 0).setChecked(True)
        self.updateSummary()

    def reference(self):
        value = self.referenceCombo.currentData
        value = value() if callable(value) else value
        return str(value) if value else None

    def setFolder(self, folder):
        self.folder = os.path.normpath(str(folder))
        self.updateSummary()

    def chooseFolder(self):
        start = self.folder if os.path.isdir(self.folder) else os.path.dirname(self.folder)
        folder = qt.QFileDialog.getExistingDirectory(self, _("Export the NIfTI files into…"), start)
        if folder:
            self.setFolder(folder)

    def readme(self):
        if self.mode != "project":
            return None
        projects = _projects()
        project = projects.currentProject() if projects is not None else None
        title = _("NIfTI export of {name}").format(name=project.name) if project is not None else _("NIfTI export")
        return os.path.join(self.folder, niftiexport.README_NAME), title

    def plan(self):
        nodes = self.checkedNodes()
        return niftiexport.planExport(nodes, self.folder, self.extension(), self.segmentationMode(), self.reference(), mapOptions=self.mapOptions,
                                      bids=self.mode == "project")

    def updateSummary(self):
        if not _isAlive(self.summaryLabel):
            return
        nodes = self.checkedNodes()
        hasSegmentation = any(niftiexport.kindOf(node) == niftiexport.KIND_SEGMENTATION for node in nodes)
        resize = self.segmentationFrame.isVisibleTo(self) != hasSegmentation
        for widget in (self.segmentationLabel, self.segmentationFrame, self.referenceLabel, self.referenceCombo):
            widget.setVisible(hasSegmentation)
        ui.setElidedText(self.folderLabel, shortPath(self.folder), self.folder)
        try:
            jobs = self.plan()
        except Exception as e:
            jobs = []
            logging.error(f"Slicer Studio: the NIfTI export could not be planned: {e}")
        images = [path for job in jobs for path in job.images()]
        files = [path for job in jobs for path in job.files()]
        others = len(files) - len(images)
        # Each label table is written twice (.tsv for BIDS, .txt for ITK-SNAP) but is one table to the user
        extras = _extraFilesText(sum(1 for path in files if path.endswith(".json")), sum(1 for path in files if path.endswith(".tsv")))
        if images:
            names = ", ".join(os.path.basename(path) for path in images[:SUMMARY_NAMES])
            if len(images) > SUMMARY_NAMES:
                names += " " + _("and {count} more").format(count=len(images) - SUMMARY_NAMES)
            text = (_("1 image: {names}") if len(images) == 1 else _("{count} images: {names}")).format(count=len(images), names=names)
            if extras:
                text += " · " + _("plus {things}").format(things=extras)
            if self.mode == "project":
                text += " · README.txt"
        else:
            text = _("Check at least one image to export.")
        self.summaryLabel.setText(text)
        readme = self.readme()
        # The project README.txt is written too (export() only when there are images), so it is counted as well
        written = len(images) + others + (1 if readme else 0)
        self.folderMeta.setText(_("{count} files will be written here").format(count=written) if images else "")
        existing = niftiexport.existingFiles(jobs, [readme[0]] if readme else [])
        # A project export also removes what an earlier one wrote and this one does not write again (renamed, deleted or
        # erased segments, closed images): Exports/nifti then matches the project as it is now
        stale = niftiexport.earlierExportFiles(jobs, readme[0]) if readme and jobs else []
        if existing and stale:
            replaced = _("1 file already exists and will be replaced") if len(existing) == 1 else \
                _("{count} files already exist and will be replaced").format(count=len(existing))
            removed = _("1 file of an earlier export that is not part of this one will be removed") if len(stale) == 1 else \
                _("{stale} files of an earlier export that are not part of this one will be removed").format(stale=len(stale))
            ui.setNotice(self.existingNotice, _("{replaced}, and {removed}, after you confirm").format(replaced=replaced, removed=removed))
        elif stale:
            ui.setNotice(self.existingNotice, (_("1 file of an earlier export is not part of this one and will be removed after you confirm: {name}")
                                               if len(stale) == 1 else
                                               _("{count} files of an earlier export are not part of this one and will be removed after you confirm")
                                               ).format(count=len(stale), name=os.path.basename(stale[0])))
        elif existing:
            ui.setNotice(self.existingNotice, (_("1 file already exists and will be replaced after you confirm: {name}") if len(existing) == 1 else
                                               _("{count} files already exist and will be replaced after you confirm")).format(
                                                   count=len(existing), name=os.path.basename(existing[0])))
        self.existingNotice.setVisible(bool(existing or stale))
        self.exportButton.setEnabled(bool(images) and self.runner is None)
        self._jobs = jobs
        self._existing = existing
        self._stale = stale
        # The summary wraps onto more or fewer lines when the names change: the dialog must follow, or the squeezed layout
        # draws the fixed-height image list under the folder card
        summaryHeight = self.summaryLabel.heightForWidth(max(self.summaryLabel.width, 1)) if self.summaryLabel.hasHeightForWidth() else 0
        wrapped = summaryHeight != self._summaryHeight and self._summaryHeight is not None
        self._summaryHeight = summaryHeight
        if resize or wrapped or bool(existing or stale) != self._existingShown:
            # Rows appeared or went away: the dialog takes its new height (a frameless dialog is not resized by its layout)
            self._existingShown = bool(existing or stale)
            qt.QTimer.singleShot(0, self._fitHeight)

    def _fitHeight(self):
        layout = self.layout() if _isAlive(self) else None
        if layout is None:
            return
        layout.activate()
        # The height the wrapped texts need at the dialog's width (sizeHint() would assume a narrower dialog)
        height = layout.totalHeightForWidth(self.width) if layout.hasHeightForWidth() else layout.totalSizeHint().height()
        height = max(height, layout.totalMinimumSize().height())
        screen = ui.screenOf(self)
        if screen is not None:
            # Never taller than the screen's usable area (the window frame takes a few pixels)
            height = min(height, max(screen.availableGeometry.height() - 40, layout.totalMinimumSize().height()))
        self.resize(self.width, height)
        if screen is not None and self.isVisible():
            # The dialog was centred at its first, shorter height: when it grew, keep its frame on the screen
            decoration = max(0, self.frameGeometry.height() - self.geometry.height())
            available = screen.availableGeometry
            top = available.y() if hasattr(available, "y") else 0
            overflow = self.y + height + decoration - (top + available.height())
            if overflow > 0:
                self.move(self.x, max(top, self.y - overflow))

    #
    # Export
    #

    def confirmOverwrite(self, existing, stale=()):
        """Ask before replacing files (``existing``) and removing the files of an earlier project export that this one
        does not write again (``stale``). Return True to go ahead.
        """
        folder = os.path.basename(self.folder)

        def names(paths):
            text = "\n".join(os.path.basename(path) for path in paths[:8])
            if len(paths) > 8:
                text += "\n" + _("and {count} more").format(count=len(paths) - 8)
            return text

        parts = []
        if existing:
            parts.append((_("1 file already exists in {folder}:") if len(existing) == 1 else _("{count} files already exist in {folder}:")).format(
                count=len(existing), folder=folder) + "\n\n" + names(list(existing)))
        if stale:
            parts.append((_("1 file of an earlier export in {folder} is not part of this one and will be removed:") if len(stale) == 1 else
                          _("{count} files of an earlier export in {folder} are not part of this one and will be removed:")).format(
                              count=len(stale), folder=folder) + "\n\n" + names(list(stale)))
        box = qt.QMessageBox(qt.QMessageBox.Warning, _("Replace existing files?"), "\n\n".join(parts), qt.QMessageBox.NoButton, self)
        box.setObjectName("StudioNiftiExportReplaceQuestion")
        replace = box.addButton(_("Replace"), qt.QMessageBox.DestructiveRole)
        # The safe choice is the default (Enter) and the Escape button: Enter must never overwrite files
        keep = box.addButton(_("Cancel"), qt.QMessageBox.RejectRole)
        box.setDefaultButton(keep)
        box.setEscapeButton(keep)
        run = getattr(box, "exec", None) or box.exec_
        run()
        return box.clickedButton() == replace

    def export(self, confirm=None):
        """Plan, ask before replacing files (``confirm(files)`` replaces the question in tests: it gets every file that would be
        replaced, then those of an earlier project export that would be removed), then write in the background. Return the
        runner, or None when nothing is written.
        """
        if self.runner is not None:
            return self.runner
        self.updateSummary()
        jobs = self._jobs
        if not jobs:
            return None
        if self._existing or self._stale:
            if not (confirm(self._existing + self._stale) if confirm is not None else self.confirmOverwrite(self._existing, self._stale)):
                return None
        try:
            os.makedirs(self.folder, exist_ok=True)
        except OSError as e:
            slicer.util.errorDisplay(_("The folder {folder} cannot be created.").format(folder=self.folder), detailedText=str(e))
            return None
        self._remember()
        self.list.setEnabled(False)
        self.changeButton.setEnabled(False)
        self.exportButton.setEnabled(False)
        self.runner = ExportRunner(jobs, executor=self.executor, readme=self.readme(), onProgress=self._onProgress, onFinished=self._onFinished,
                                   remove=self._stale)
        # The bar counts images, not jobs: one segmentation written as 100 masks moves it 100 times
        self.progress.setRange(0, max(1, self.runner.total))
        self.progress.setValue(0)
        self.progress.show()
        self.runner.start()
        return self.runner

    def _remember(self):
        settings = qt.QSettings()
        settings.setValue(niftiexport.FORMAT_KEY, self.extension())
        if self.mode != "project":
            settings.setValue(niftiexport.SEGMENTATION_MODE_KEY, self.segmentationMode())
        projectFolder = niftiexport.projectExportFolder(create=False)
        projects = _projects()
        inProject = projectFolder and projects is not None and projects.isInside(self.folder, os.path.dirname(os.path.dirname(projectFolder)))
        if not inProject:
            settings.setValue(niftiexport.LAST_FOLDER_KEY, self.folder)

    def _onProgress(self, done, total, title):
        if not _isAlive(self.progress):
            return
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(done)
        if title:
            self.summaryLabel.setText(_("Writing {name}… ({done} of {total})").format(name=title, done=min(done + 1, total), total=total))

    def _onFinished(self, result):
        self.lastResult = result
        announce(result)
        if not _isAlive(self.progress):
            return
        if result.errors and not result.images:
            # Nothing was written (an unwritable folder, for example): stay open, so that another folder can be chosen
            # without picking the images and options again
            self.runner = None
            self.progress.hide()
            self.list.setEnabled(True)
            self.changeButton.setEnabled(True)
            self.cancelButton.setEnabled(True)
            self.updateSummary()
            return
        self.accept()

    def reject(self):
        # Esc or Cancel while writing stops after the current image
        if self.runner is not None and not self.runner.finished:
            self.runner.cancel()
            self.cancelButton.setEnabled(False)
            self.summaryLabel.setText(_("Stopping after the current image…"))
            return
        qt.QDialog.reject(self)


def showDialog(dialog):
    """Show the dialog (one at a time; a previous one is closed). Return it."""
    global _dialog
    if _dialog is not None and _isAlive(_dialog):
        _dialog.close()
    _dialog = dialog
    dialog.setAttribute(qt.Qt.WA_DeleteOnClose, True)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog


def currentDialog():
    return _dialog if _dialog is not None and _isAlive(_dialog) else None


#
# Entry points
#


def exportNodesDialog(nodes=None, shell=None, show=True, **kwargs):
    """"Export as NIfTI…": every exportable image listed, ``nodes`` (default: all of them) checked."""
    available = niftiexport.exportableNodes()
    if not available:
        message(_("Nothing to export as NIfTI: open a scan, draw a lesion or open a map first."), level="warning")
        return None
    checked = [node for node in (nodes or []) if node is not None]
    if checked:
        ids = {node.GetID() for node in available}
        available += [node for node in checked if node.GetID() not in ids and not niftiexport.unavailableReason(node)]
    dialog = ExportDialog(available, checked or None, **kwargs)
    return showDialog(dialog) if show else dialog


def exportProjectDialog(shell=None, show=True, **kwargs):
    """"Export project as NIfTI…": every image, lesion (one mask per segment) and map into ``<project>/Exports/nifti``."""
    nodes = niftiexport.projectNodes()
    if not nodes:
        message(_("This project has no image, lesion or map to export yet."), level="warning")
        return None
    mapOptions = kwargs.pop("mapOptions", None)
    if mapOptions is None:
        mapOptions = niftiexport.mapOptionsFromMapModule()
    dialog = ExportDialog(nodes, None, mode="project", segmentationMode=niftiexport.SEGMENTATION_MASKS, mapOptions=mapOptions, **kwargs)
    return showDialog(dialog) if show else dialog


def convertDicomDialog(shell=None, show=True, **kwargs):
    """"Convert DICOM to NIfTI": every loaded DICOM series, named "<series number>_<description>"."""
    nodes = niftiexport.dicomNodes()
    if not nodes:
        shell = shell or _shell()

        def openFolder():
            try:
                from . import importer

                importer.openFolderDialog(shell)
            except Exception:
                logging.error(f"Slicer Studio: the folder dialog could not be opened:\n{traceback.format_exc()}")

        message(_("No DICOM series is open. Open folder… reads a DICOM folder; then convert it."), level="warning", action=openFolder,
                actionText=_("Open folder…"), ms=8000)
        return None
    dialog = ExportDialog(nodes, None, mode="dicom",
                          body=_("Each loaded DICOM series becomes a NIfTI image named after its series number and description, with its full "
                                 "orientation (nothing is resampled). Sidecars hold no patient name, identifier or date."), **kwargs)
    return showDialog(dialog) if show else dialog


def exportLesionMasksDialog(shell=None, show=True, **kwargs):
    """Draw's lesion ⋯ "Export each segment as a NIfTI mask…": the lesion of Draw, one binary mask per segment."""
    try:
        from . import draw

        segmentationNode, _sourceVolume = draw.lesionNodes()
    except Exception:
        segmentationNode = None
    if segmentationNode is None:
        message(_("There is no lesion to export. Open a scan or start on the MNI template, then draw the lesion."), level="warning")
        return None
    reason = niftiexport.unavailableReason(segmentationNode)
    if reason:
        message(_("The lesion is empty: nothing was exported") if not niftiexport.segmentIds(segmentationNode, nonEmpty=True) else reason, level="warning")
        return None
    dialog = exportNodesDialog([segmentationNode], shell, show=False, segmentationMode=niftiexport.SEGMENTATION_MASKS,
                               title=_("Export each segment as a NIfTI mask"), **kwargs)
    if dialog is None:
        return None
    dialog.setChecked([segmentationNode.GetID()])
    dialog.setSegmentationMode(niftiexport.SEGMENTATION_MASKS)
    return showDialog(dialog) if show else dialog


def currentMapNode():
    """The map shown by Map (its result volume), or None."""
    widget = getattr(slicer.modules, "LesionNetworkMappingWidget", None)
    try:
        node = widget._parameterNode.resultVolume if widget is not None and widget._parameterNode is not None else None
    except Exception:
        node = None
    return node if node is not None and node.GetScene() is not None else None


def exportMapDialog(shell=None, show=True, node=None, **kwargs):
    """"Export map as NIfTI…": the map Map shows (float32) with its thresholded binary mask when a threshold is applied."""
    node = node or currentMapNode()
    if node is None:
        maps = [candidate for candidate in niftiexport.exportableNodes() if niftiexport.kindOf(candidate) == niftiexport.KIND_MAP]
        node = maps[0] if maps else None
    if node is None:
        message(_("There is no map to export yet: compute a map in Map, or open one."), level="warning")
        return None
    mapOptions = kwargs.pop("mapOptions", None)
    if mapOptions is None:
        mapOptions = niftiexport.mapOptionsFromMapModule()
    body = _("The map as float32 NIfTI with a .json sidecar (statistic, threshold, space)")
    if node.GetID() in mapOptions:
        body += _(", and a binary mask of the voxels that pass the threshold ({rule}).").format(rule=mapOptions[node.GetID()].get("rule") or
                                                                                              mapOptions[node.GetID()]["threshold"])
    else:
        body += "."
    dialog = exportNodesDialog([node], shell, show=False, mapOptions=mapOptions, title=_("Export map as NIfTI"), body=body, **kwargs)
    if dialog is None:
        return None
    dialog.setChecked([node.GetID()])
    return showDialog(dialog) if show else dialog


#
# Search (palette) entries: built into StudioLib.palette (niftiexport.*)
#


def paletteEntries():
    """``[(id, title, callback, synonyms, enabled, description)]`` of the NIfTI export entries of Search."""
    return [
        ("niftiexport.export", _("Export as NIfTI…"), exportNodesDialog,
         ("export nifti", "export as nifti", "nifti export", "save as nifti", "export image", "export scan", "export volume", "export map",
          "export labelmap", "export label map", "fsl", "spm", "afni", "nibabel", "nii export", "export nii"),
         niftiexport.hasExportableNodes,
         _("Scans, label maps, segmentations and maps as NIfTI files with sidecars (left stays left)")),
        ("niftiexport.project", _("Export project as NIfTI…"), exportProjectDialog,
         ("export project", "export project nifti", "project nifti", "export all nifti", "export everything", "bids export", "export all images"),
         lambda: niftiexport.projectExportFolder() is not None and niftiexport.hasExportableNodes(),
         _("Every image, lesion (one mask per segment) and map of the project into Exports/nifti, with a README")),
        ("niftiexport.dicom", _("Convert DICOM to NIfTI"), convertDicomDialog,
         ("dicom to nifti", "convert dicom", "dcm2nii", "dcm2niix", "dicom nifti", "dicom export", "export dicom"),
         True, _("Every loaded DICOM series as a NIfTI image named after its series")),
    ]

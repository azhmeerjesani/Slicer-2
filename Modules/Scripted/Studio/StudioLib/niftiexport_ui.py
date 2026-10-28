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
    """``<project>/Exports/nifti`` of the open project, else the last export folder, else Documents (or the home folder)."""
    projectFolder = niftiexport.projectExportFolder(create=False)
    if projectFolder:
        return projectFolder
    last = _setting(niftiexport.LAST_FOLDER_KEY)
    if last and os.path.isdir(last):
        return last
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


class ExportRunner:
    """Run export jobs: each job reads the scene on the main thread (``Job.prepare``), then its images are written by the
    background writer (``autosave_io.backgroundWriter()``, or ``executor``). ``onProgress(done, total, title)`` and
    ``onFinished(result)`` are called on the main thread. ``cancel()`` stops before the next job.
    """

    def __init__(self, jobs, executor=None, readme=None, onProgress=None, onFinished=None):
        self.jobs = list(jobs)
        self.executor = executor
        self.readme = readme
        self.onProgress = onProgress
        self.onFinished = onFinished
        self.result = niftiexport.Result(os.path.dirname(self.jobs[0].path) if self.jobs else None)
        self.index = 0
        self.cancelled = False
        self.finished = False
        self._waiting = 0

    def _executor(self):
        if self.executor is None:
            from . import autosave_io

            self.executor = autosave_io.backgroundWriter()
        return self.executor

    def start(self):
        self._next()

    def cancel(self):
        self.cancelled = True

    def _next(self):
        if self.finished:
            return
        if self.cancelled or self.index >= len(self.jobs):
            self._finish()
            return
        job = self.jobs[self.index]
        if self.onProgress is not None:
            self.onProgress(self.index, len(self.jobs), job.title)
        try:
            tasks = job.prepare()
        except Exception as e:
            logging.error(f"Slicer Studio: NIfTI export of {job.nodeName} failed:\n{traceback.format_exc()}")
            self.result.errors.append(f"{job.nodeName}: {e}")
            tasks = []
        self.result.notes += job.notes
        self.index += 1
        background = [task for task in tasks if task.data is not None]
        for task in tasks:
            if task.data is None:
                try:
                    self.result.add(niftiexport._writeWithSlicer(task))
                except Exception as e:
                    self.result.errors.append(f"{job.nodeName}: {e}")
        if not background:
            qt.QTimer.singleShot(0, self._next)
            return
        self._waiting = len(background)
        for task in background:
            self._executor().submit(task.write, onDone=self._onWritten, onError=lambda error, name=job.nodeName: self._onFailed(name, error))

    def _onWritten(self, files):
        self.result.add(files)
        self._taskDone()

    def _onFailed(self, name, error):
        logging.error(f"Slicer Studio: NIfTI export of {name} failed: {error}")
        self.result.errors.append(f"{name}: {error}")
        self._taskDone()

    def _taskDone(self):
        self._waiting -= 1
        if self._waiting <= 0:
            qt.QTimer.singleShot(0, self._next)

    def _finish(self):
        self.finished = True
        if self.readme and self.result.images:
            try:
                niftiexport.writeReadme(self.readme[0], self.jobs[:self.index], self.result, self.readme[1])
                self.result.files.append(self.readme[0])
            except OSError as e:
                self.result.errors.append(f"{os.path.basename(self.readme[0])}: {e}")
        if self.onProgress is not None:
            self.onProgress(len(self.jobs), len(self.jobs), "")
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
    if not result.images and result.errors:
        message(_("Nothing was exported: {error}").format(error=result.errors[0]), level="warning", ms=8000)
        return
    folder = result.folder

    def show():
        showInFileManager(folder)

    text = resultText(result)
    if result.notes:
        text += " · " + notesText(result.notes)
    message(text, level="warning" if result.errors else "info", action=show,
            actionText=_("Show in {manager}").format(manager=fileManagerName()), ms=10000 if result.notes else 6000, toolTip=shortPath(folder))
    for note in result.notes:
        logging.info(f"Slicer Studio NIfTI export: {note}")


#
# The dialog
#


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
        listHeader.addWidget(ui.overline(_("Images"), "StudioNiftiExportListTitle"))
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
            item = qt.QListWidgetItem(f"{node.GetName()}  ·  {niftiexport.kindText(kind)}")
            item.setFlags(qt.Qt.ItemIsEnabled | qt.Qt.ItemIsUserCheckable)
            item.setCheckState(qt.Qt.Checked if node.GetID() in checkedIds else qt.Qt.Unchecked)
            item.setData(qt.Qt.UserRole, node.GetID())
            item.setSizeHint(qt.QSize(0, LIST_ROW_HEIGHT))
            item.setToolTip(_("{name} ({kind})").format(name=node.GetName(), kind=niftiexport.kindText(kind)))
            self.list.addItem(item)
        rows = max(1, min(LIST_ROWS, len(nodes)))
        self.list.setFixedHeight(rows * LIST_ROW_HEIGHT + 6)
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
        self.folderLabel.setText(shortPath(self.folder))
        self.folderLabel.setToolTip(self.folder)
        try:
            jobs = self.plan()
        except Exception as e:
            jobs = []
            logging.error(f"Slicer Studio: the NIfTI export could not be planned: {e}")
        images = [path for job in jobs for path in job.images()]
        others = sum(len(job.files()) for job in jobs) - len(images)
        if images:
            names = ", ".join(os.path.basename(path) for path in images[:SUMMARY_NAMES])
            if len(images) > SUMMARY_NAMES:
                names += " " + _("and {count} more").format(count=len(images) - SUMMARY_NAMES)
            text = (_("1 image: {names}") if len(images) == 1 else _("{count} images: {names}")).format(count=len(images), names=names)
            text += " · " + _("plus {count} sidecar and label files").format(count=others)
            if self.mode == "project":
                text += " · README.txt"
        else:
            text = _("Check at least one image to export.")
        self.summaryLabel.setText(text)
        self.folderMeta.setText(_("{count} files will be written here").format(count=len(images) + others) if images else "")
        readme = self.readme()
        existing = niftiexport.existingFiles(jobs, [readme[0]] if readme else [])
        if existing:
            ui.setNotice(self.existingNotice, (_("1 file already exists and will be replaced after you confirm: {name}") if len(existing) == 1 else
                                               _("{count} files already exist and will be replaced after you confirm")).format(
                                                   count=len(existing), name=os.path.basename(existing[0])))
        self.existingNotice.setVisible(bool(existing))
        self.exportButton.setEnabled(bool(images) and self.runner is None)
        self._jobs = jobs
        self._existing = existing
        # The summary wraps onto more or fewer lines when the names change: the dialog must follow, or the squeezed layout
        # draws the fixed-height image list under the folder card
        summaryHeight = self.summaryLabel.heightForWidth(max(self.summaryLabel.width, 1)) if self.summaryLabel.hasHeightForWidth() else 0
        wrapped = summaryHeight != self._summaryHeight and self._summaryHeight is not None
        self._summaryHeight = summaryHeight
        if resize or wrapped or bool(existing) != self._existingShown:
            # Rows appeared or went away: the dialog takes its new height (a frameless dialog is not resized by its layout)
            self._existingShown = bool(existing)
            qt.QTimer.singleShot(0, self._fitHeight)

    def _fitHeight(self):
        layout = self.layout() if _isAlive(self) else None
        if layout is None:
            return
        layout.activate()
        # The height the wrapped texts need at the dialog's width (sizeHint() would assume a narrower dialog)
        height = layout.totalHeightForWidth(self.width) if layout.hasHeightForWidth() else layout.totalSizeHint().height()
        self.resize(self.width, max(height, layout.totalMinimumSize().height()))

    #
    # Export
    #

    def confirmOverwrite(self, existing):
        """Ask before replacing files. Return True to replace them."""
        names = "\n".join(os.path.basename(path) for path in existing[:8])
        if len(existing) > 8:
            names += "\n" + _("and {count} more").format(count=len(existing) - 8)
        box = qt.QMessageBox(qt.QMessageBox.Warning, _("Replace existing files?"),
                             (_("1 file already exists in {folder}:") if len(existing) == 1 else _("{count} files already exist in {folder}:")).format(
                                 count=len(existing), folder=os.path.basename(self.folder)) + "\n\n" + names, qt.QMessageBox.NoButton, self)
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
        """Plan, ask before replacing files (``confirm(existing)`` replaces the question in tests), then write in the background.
        Return the runner, or None when nothing is written.
        """
        if self.runner is not None:
            return self.runner
        self.updateSummary()
        jobs = self._jobs
        if not jobs:
            return None
        if self._existing:
            ask = confirm or self.confirmOverwrite
            if not ask(self._existing):
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
        self.progress.setRange(0, len(jobs))
        self.progress.setValue(0)
        self.progress.show()
        self.runner = ExportRunner(jobs, executor=self.executor, readme=self.readme(), onProgress=self._onProgress, onFinished=self._onFinished)
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
        self.progress.setValue(done)
        if title:
            self.summaryLabel.setText(_("Writing {name}… ({done} of {total})").format(name=title, done=done + 1, total=total))

    def _onFinished(self, result):
        self.lastResult = result
        announce(result)
        if _isAlive(self.progress):
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

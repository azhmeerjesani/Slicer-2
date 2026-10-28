"""Library extension: "Tutorial cohorts…" (L-53, LNM addendum 4.4 and F40): practice studies built from reusable public data.

A new lab member can learn the whole loop on real data that may be reused: overlap, lesion network mapping, a symptom
score and the lesion volume covariate. The IMPORT card has one row, **Tutorial cohorts…** (also in Search, which works
without an open study). It opens a dialog with the cohorts of ``LesionNetworkMappingLib.tutorials`` (recipes in
``catalog/tutorials.json``; CC0 and Apache sources only):

- the list shows the title, the licence and the size; the details show the summary, the licence badge, whether the masks
  are in MNI space or native (normalize first, A-03), what the cohort teaches, the guidance on the lesion volume
  covariate where there is one, and the citation to use;
- options: all subjects or the first 25 (a quick start), the scans of native cohorts (large, off by default) and the
  folder of the new study (a new folder: nothing is overwritten);
- **Download and build** runs ``python -m LesionNetworkMappingLib.tutorials build`` as a light job (the job runner, Stop
  works, progress shows files and megabytes). Nothing is downloaded before this click and nothing is bundled. A cohort
  that is only linked (LESYMAP) shows its link and asks for the folder that you downloaded;
- when it is done, **Open study** opens the new study; figures made from it carry "TUTORIAL DATA".

Esc and the close button hide the dialog; they never stop a download (Stop does). A running download keeps going in the
job runner and the dialog shows its progress again when it is opened. Downloads are cached and verified; the dialog says
how much space they use and can delete them (studies built from them keep their own copies). Works in Classic (the Studio
module panel hosts the same cards).
"""

import logging
import os
import traceback

import qt
import slicer
from slicer.i18n import tr as _

from . import library, ui

#: Order of the row among the IMPORT cards (after "Add control set…")
CARD_ORDER = 60
#: The "quick start" option: the first subjects (those with a score first)
QUICK_START_SUBJECTS = 25
#: Name of the folder of new tutorial studies below the user's documents folder
FOLDER_NAME = "Slicer Studio tutorials"
#: Test hook: a tutorials catalog file used instead of the shipped one (also passed to the job)
catalogPath = None


def _lnm(name):
    import importlib

    return importlib.import_module("LesionNetworkMappingLib." + name)


def _tutorials():
    return _lnm("tutorials")


def _toast(text, ms=3000, **kwargs):
    shell = library._shell()
    if shell is not None:
        shell.toast(text, ms, **kwargs)
    else:
        logging.info(f"Studio tutorials: {text}")


def cacheFolder():
    """The LNM cache folder (``<application cache>/LNM``, as the other cards use it)."""
    return os.path.join(slicer.app.cachePath, "LNM")


def defaultFolder(entry):
    """A new folder for the study: "<documents>/Slicer Studio tutorials/<id>", "<id>-2"... (never an existing study or file)."""
    documents = ""
    try:
        documents = str(qt.QStandardPaths.writableLocation(qt.QStandardPaths.DocumentsLocation))
    except Exception:
        documents = ""
    root = os.path.join(documents or os.path.expanduser("~"), FOLDER_NAME)
    candidate, number = os.path.join(root, entry["id"]), 2
    while os.path.exists(candidate):
        candidate, number = os.path.join(root, f"{entry['id']}-{number}"), number + 1
    return candidate


def buildArguments(entry, dest, limit=None, scans=False, sourceDir=None, cache=None, catalog=None):
    """Arguments of ``python -m LesionNetworkMappingLib.tutorials build`` for the job runner."""
    args = ["build", "--id", entry["id"], "--dest", dest, "--cache", cache or cacheFolder()]
    if catalog:
        args += ["--catalog", catalog]
    if limit:
        args += ["--limit", str(int(limit))]
    if scans:
        args.append("--scans")
    if sourceDir:
        args += ["--source-dir", sourceDir]
    return args


def sizeText(entry, limit=None, scans=False):
    """"About 116 MB" (the download of the choices made), or "Not downloaded by the app" for a cohort that is only linked."""
    tutorials = _tutorials()
    if entry["source"]["type"] == "folder":
        return _("Not downloaded by the app: you choose the folder you downloaded")
    guess = tutorials.estimate(entry, limit, scans)
    if guess["total_bytes"] is None:
        return _("Download size unknown")
    text = _("About {size}").format(size=tutorials.human_size(guess["total_bytes"]))
    if guess["scan_bytes"]:
        text += " " + _("({scans} of it scans)").format(scans=tutorials.human_size(guess["scan_bytes"]))
    return text


#
# The dialog
#


class _Dialog(qt.QDialog):
    """Esc and the close button hide the dialog; they never stop a download (Stop does, SPEC D8)."""


class TutorialsDialog:
    """Choose a cohort, set the options, download and build it as a job, open the study."""

    def __init__(self, parent=None):
        self.tutorials = _tutorials()
        self.entries = self.tutorials.entries(catalogPath)
        self.job = None
        self.resultPath = None
        self._resultOptions = None
        self.dialog = _Dialog(parent or slicer.util.mainWindow())
        self.dialog.setObjectName("StudioTutorialsDialog")
        self.dialog.setWindowTitle(_("Tutorial cohorts"))
        self.dialog.setMinimumWidth(ui.fontPx(720))
        outer = qt.QVBoxLayout(self.dialog)
        intro = library._cardLabel("StudioTutorialsIntro", 12, "text2")
        intro.setText(_("Practice on real data that you may reuse. A cohort downloads only when you click Download, straight from its public source; "
                        "the app does not bundle any of it. Every figure made from a tutorial study is marked TUTORIAL DATA."))
        outer.addWidget(intro)

        body = qt.QHBoxLayout()
        body.setSpacing(12)
        outer.addLayout(body, 1)
        self.listWidget = qt.QListWidget()
        self.listWidget.setObjectName("StudioTutorialsList")
        self.listWidget.setAccessibleName(_("Tutorial cohorts"))
        self.listWidget.setFixedWidth(ui.fontPx(210))
        self.listWidget.setWordWrap(True)
        for entry in self.entries:
            item = qt.QListWidgetItem(f"{entry['title']}\n{entry['licence']} · {self._sizeLine(entry)}")
            item.setData(qt.Qt.UserRole, entry["id"])
            self.listWidget.addItem(item)
        body.addWidget(self.listWidget)

        scroll = qt.QScrollArea()
        scroll.setObjectName("StudioTutorialsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(qt.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(qt.Qt.ScrollBarAlwaysOff)
        detail = qt.QWidget()
        layout = qt.QVBoxLayout(detail)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(8)
        scroll.setWidget(detail)
        body.addWidget(scroll, 1)
        self._scroll, self._detail = scroll, detail

        self.titleLabel = library._cardLabel("StudioTutorialsTitle", 15, "text")
        library._setFont(self.titleLabel, 15, qt.QFont.DemiBold)
        layout.addWidget(self.titleLabel)
        chips = qt.QHBoxLayout()
        chips.setSpacing(6)
        self.licenceChip = ui.chip("", "ok")
        self.licenceChip.setObjectName("StudioTutorialsLicence")
        self.spaceChip = ui.chip("", "neutral")
        self.spaceChip.setObjectName("StudioTutorialsSpace")
        chips.addWidget(self.licenceChip)
        chips.addWidget(self.spaceChip)
        chips.addStretch(1)
        layout.addLayout(chips)
        self.summaryLabel = library._cardLabel("StudioTutorialsSummary", 13, "text")
        layout.addWidget(self.summaryLabel)
        self.nativeLabel = library._cardLabel("StudioTutorialsNative", 12, "warning")
        layout.addWidget(self.nativeLabel)
        layout.addWidget(self._header(_("What you will learn")))
        self.stepsLabel = library._cardLabel("StudioTutorialsSteps", 12, "text2")
        layout.addWidget(self.stepsLabel)
        self.guidanceHeader = self._header(_("The lesion volume covariate"))
        layout.addWidget(self.guidanceHeader)
        self.guidanceLabel = library._cardLabel("StudioTutorialsGuidance", 12, "text")
        layout.addWidget(self.guidanceLabel)
        layout.addWidget(self._header(_("Source and citation")))
        self.citationLabel = library._cardLabel("StudioTutorialsCitation", 12, "text2")
        self.citationLabel.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
        layout.addWidget(self.citationLabel)
        self.linkRow = qt.QWidget()
        linkLayout = qt.QHBoxLayout(self.linkRow)
        linkLayout.setContentsMargins(0, 0, 0, 0)
        self.linkLabel = library._cardLabel("StudioTutorialsLink", 12, "text2")
        self.linkLabel.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
        linkLayout.addWidget(self.linkLabel, 1)
        self.linkButton = qt.QPushButton(_("Open page"))
        self.linkButton.setObjectName("StudioTutorialsOpenPage")
        ui.setRole(self.linkButton, "secondary")
        self.linkButton.connect("clicked()", self.openPage)
        linkLayout.addWidget(self.linkButton)
        layout.addWidget(self.linkRow)

        layout.addWidget(self._header(_("Options")))
        form = qt.QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setFieldGrowthPolicy(qt.QFormLayout.AllNonFixedFieldsGrow)
        self.limitCombo = qt.QComboBox()
        self.limitCombo.setObjectName("StudioTutorialsLimit")
        self.limitCombo.setAccessibleName(_("Subjects"))
        self.limitCombo.setSizeAdjustPolicy(qt.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.limitCombo.setMinimumContentsLength(12)
        self.limitRow = form
        form.addRow(_("Subjects"), self.limitCombo)
        self.scansCheck = qt.QCheckBox(_("Also download the scans"))
        self.scansCheck.setObjectName("StudioTutorialsScans")
        form.addRow("", self.scansCheck)
        self.scansNote = library._cardLabel("StudioTutorialsScansNote", 12, "text3")
        form.addRow("", self.scansNote)
        self.sourceRow = qt.QWidget()
        sourceLayout = qt.QHBoxLayout(self.sourceRow)
        sourceLayout.setContentsMargins(0, 0, 0, 0)
        self.sourceEdit = qt.QLineEdit()
        self.sourceEdit.setObjectName("StudioTutorialsSourceFolder")
        self.sourceEdit.setPlaceholderText(_("The folder you downloaded"))
        sourceLayout.addWidget(self.sourceEdit, 1)
        self.sourceBrowse = qt.QPushButton(_("Choose…"))
        self.sourceBrowse.setObjectName("StudioTutorialsSourceBrowse")
        self.sourceBrowse.connect("clicked()", self.browseSource)
        sourceLayout.addWidget(self.sourceBrowse)
        form.addRow(_("Downloaded folder"), self.sourceRow)
        self.folderRow = qt.QWidget()
        folderLayout = qt.QHBoxLayout(self.folderRow)
        folderLayout.setContentsMargins(0, 0, 0, 0)
        self.folderEdit = qt.QLineEdit()
        self.folderEdit.setObjectName("StudioTutorialsFolder")
        self.folderEdit.setAccessibleName(_("Folder of the new study"))
        folderLayout.addWidget(self.folderEdit, 1)
        self.folderBrowse = qt.QPushButton(_("Choose…"))
        self.folderBrowse.setObjectName("StudioTutorialsBrowse")
        self.folderBrowse.connect("clicked()", self.browse)
        folderLayout.addWidget(self.folderBrowse)
        form.addRow(_("New study in"), self.folderRow)
        layout.addLayout(form)
        self.sizeLabel = library._cardLabel("StudioTutorialsSize", 12, "text2")
        layout.addWidget(self.sizeLabel)
        self.cacheRow = qt.QWidget()
        cacheLayout = qt.QHBoxLayout(self.cacheRow)
        cacheLayout.setContentsMargins(0, 0, 0, 0)
        self.cacheLabel = library._cardLabel("StudioTutorialsCache", 12, "text3")
        cacheLayout.addWidget(self.cacheLabel, 1)
        self.cacheButton = qt.QPushButton(_("Delete downloads"))
        self.cacheButton.setObjectName("StudioTutorialsFreeCache")
        self.cacheButton.setToolTip(_("Studies that were built from them keep their own copies"))
        self.cacheButton.connect("clicked()", self.freeCache)
        cacheLayout.addWidget(self.cacheButton)
        layout.addWidget(self.cacheRow)
        layout.addStretch(1)

        self.progressBar = qt.QProgressBar()
        self.progressBar.setObjectName("StudioTutorialsProgress")
        self.progressBar.setTextVisible(False)
        self.progressBar.setFixedHeight(6)
        self.progressBar.setRange(0, 1000)
        self.progressBar.hide()
        outer.addWidget(self.progressBar)
        self.statusLabel = library._cardLabel("StudioTutorialsStatus", 12, "text2")
        self.statusLabel.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
        outer.addWidget(self.statusLabel)
        buttons = qt.QHBoxLayout()
        buttons.addStretch(1)
        self.closeButton = qt.QPushButton(_("Close"))
        self.closeButton.setObjectName("StudioTutorialsClose")
        self.closeButton.connect("clicked()", self.dialog.hide)
        buttons.addWidget(self.closeButton)
        self.stopButton = qt.QPushButton(_("Stop"))
        self.stopButton.setObjectName("StudioTutorialsStop")
        self.stopButton.connect("clicked()", self.stop)
        buttons.addWidget(self.stopButton)
        self.openButton = qt.QPushButton(_("Open study"))
        self.openButton.setObjectName("StudioTutorialsOpen")
        ui.setRole(self.openButton, "primary")
        self.openButton.connect("clicked()", self.openStudy)
        buttons.addWidget(self.openButton)
        self.downloadButton = qt.QPushButton(_("Download and build"))
        self.downloadButton.setObjectName("StudioTutorialsDownload")
        ui.setRole(self.downloadButton, "primary")
        self.downloadButton.setDefault(True)
        self.downloadButton.connect("clicked()", self.start)
        buttons.addWidget(self.downloadButton)
        outer.addLayout(buttons)

        self.listWidget.connect("currentRowChanged(int)", self._onSelect)
        self.limitCombo.connect("currentIndexChanged(int)", lambda _index: self._onOptionChanged())
        self.scansCheck.connect("toggled(bool)", lambda _checked: self._onOptionChanged())
        self.sourceEdit.connect("textChanged(QString)", lambda _text: self._updateButtons())
        self.folderEdit.connect("textChanged(QString)", lambda _text: self._updateButtons())
        self._folders = {}  # tutorial id: the folder text the user last had for it
        self.listWidget.setCurrentRow(0)
        self._fitToScreen()

    # Layout helpers

    @staticmethod
    def _header(text):
        label = qt.QLabel(text)
        label.setProperty("slicerRole", "sectionHeader")
        library._setFont(label, 12, qt.QFont.DemiBold)
        return label

    def _sizeLine(self, entry):
        if entry["source"]["type"] == "folder":
            return _("you download it")
        return self.tutorials.human_size(entry.get("size_bytes"))

    def _fitToScreen(self, fraction=0.9):
        """Open tall enough to show the details without scrolling, but never taller than most of the screen."""

        def hint(widget):
            size = widget.sizeHint
            return size() if callable(size) else size

        chrome = hint(self.dialog).height() - hint(self._scroll).height()
        width = max(self.dialog.minimumWidth, hint(self.dialog).width())
        tallest, shown = 0, self.listWidget.currentRow
        for row in range(len(self.entries)):  # tall enough for the longest description
            self.listWidget.setCurrentRow(row)
            tallest = max(tallest, hint(self._detail).height())
        self.listWidget.setCurrentRow(shown)
        height = chrome + tallest + 8
        screen = ui.screenOf(slicer.util.mainWindow() or self.dialog)
        if screen is not None:
            available = screen.availableGeometry
            available = available() if callable(available) else available
            width = min(width, int(available.width() * fraction))
            height = min(height, int(available.height() * fraction))
        self.dialog.resize(width, height)

    # State

    def entry(self):
        row = self.listWidget.currentRow
        return self.entries[row] if 0 <= row < len(self.entries) else None

    def isBusy(self):
        return self.job is not None and not self.job.isFinished()

    def select(self, tutorialId):
        for row, entry in enumerate(self.entries):
            if entry["id"] == tutorialId:
                self.listWidget.setCurrentRow(row)
                return True
        return False

    def _remember(self):
        previous = getattr(self, "_shown", None)
        if previous is not None:
            self._folders[previous] = str(self.folderEdit.text)

    def _onSelect(self, _row):
        self._remember()
        entry = self.entry()
        if entry is None:
            return
        self._shown = entry["id"]
        tutorials = self.tutorials
        self.titleLabel.setText(entry["title"])
        self.licenceChip.setText(entry["licence"])
        ui.setChipLevel(self.licenceChip, {"green": "ok", "amber": "warn"}.get(entry.get("badge"), "neutral"))
        native = bool(entry.get("native"))
        self.spaceChip.setText(_("Native space") if native else _("MNI space") if entry["space"] != "MNIColin27" else _("Colin27 grid"))
        ui.setChipLevel(self.spaceChip, "warn" if native else "neutral")
        self.summaryLabel.setText(entry["summary"])
        self.nativeLabel.setVisible(native)
        self.nativeLabel.setText(_("The masks are in scan (native) space: normalize first (A-03). The scans are an option below; without them the study opens but cannot be normalized in the app."))
        self.stepsLabel.setText("\n".join(f"{number}. {step}" for number, step in enumerate(entry["steps"], 1)))
        guidance = entry.get("volume_covariate")
        self.guidanceHeader.setVisible(bool(guidance))
        self.guidanceLabel.setVisible(bool(guidance))
        self.guidanceLabel.setText(guidance or "")
        self.citationLabel.setText(_("Licence {licence}. Please cite: {citation}").format(licence=entry["licence"], citation=entry["citation"]))
        link = entry["source"].get("link")
        self.linkRow.setVisible(entry["source"]["type"] == "folder")
        self.linkLabel.setText(link or "")
        isFolder = entry["source"]["type"] == "folder"
        self.limitCombo.blockSignals(True)
        self.limitCombo.clear()
        total = entry.get("n_subjects")
        self.limitCombo.addItem(_("All {n} subjects").format(n=total) if total else _("All subjects"), None)
        if total is None or total > QUICK_START_SUBJECTS:
            self.limitCombo.addItem(_("The first {n} (quick start)").format(n=QUICK_START_SUBJECTS), QUICK_START_SUBJECTS)
        self.limitCombo.blockSignals(False)
        self.limitCombo.setCurrentIndex(0)
        self.scansCheck.setVisible(native and not isFolder)
        self.scansCheck.setChecked(False)
        scanBytes = entry.get("scan_bytes_each")
        self.scansNote.setVisible(native and not isFolder)
        self.scansNote.setText(_("The scans are needed to normalize in the app; about {size} each.").format(size=tutorials.human_size(scanBytes)) if scanBytes else "")
        self.sourceRow.setVisible(isFolder)
        self.limitRow.labelForField(self.sourceRow).setVisible(isFolder)
        self.folderEdit.setText(self._folders.get(entry["id"]) or defaultFolder(entry))
        self._refreshSize()
        self._refreshCache()
        self._updateButtons()

    def limit(self):
        data = self.limitCombo.itemData(self.limitCombo.currentIndex)
        return int(data) if data else None

    def scans(self):
        return bool(self.scansCheck.isVisibleTo(self.dialog) and self.scansCheck.checked)

    def _refreshSize(self):
        entry = self.entry()
        if entry is not None:
            self.sizeLabel.setText(sizeText(entry, self.limit(), self.scans()))

    def _refreshCache(self):
        entry = self.entry()
        if entry is None:
            return
        used = self.tutorials.cache_bytes(entry["id"], cacheFolder())
        self.cacheRow.setVisible(used > 0)
        self.cacheLabel.setText(_("Downloaded files kept for next time: {size}").format(size=self.tutorials.human_size(used)))

    def folder(self):
        return str(self.folderEdit.text).strip()

    def _options(self):
        """What a build is made from: the study folder, the subject limit and the scans option."""
        folder = self.folder()
        return (os.path.normpath(folder) if folder else "", self.limit(), self.scans())

    def _onOptionChanged(self):
        self._refreshSize()
        self._updateButtons()

    def _updateButtons(self):
        entry = self.entry()
        busy = self.isBusy()
        # "Built" only while the dialog still describes that build: another folder, size or scans option offers a new build.
        built = self.resultPath is not None and os.path.isdir(self.resultPath) and getattr(self, "_resultId", None) == (entry or {}).get("id") and self._resultOptions == self._options()
        folder = self.folder()
        needsSource = entry is not None and entry["source"]["type"] == "folder"
        reason = ""
        if not folder:
            reason = _("Choose a folder for the new study")
        elif os.path.isfile(folder) or (os.path.isdir(folder) and os.listdir(folder)):
            reason = _("This folder is not empty: choose a new one")
        elif needsSource and not os.path.isdir(str(self.sourceEdit.text).strip()):
            reason = _("Choose the folder you downloaded")
        self.downloadButton.setText(_("Build study") if needsSource else _("Download and build"))
        self.downloadButton.setEnabled(not busy and not reason)
        self.downloadButton.setToolTip(reason)
        self.downloadButton.setVisible(not built)
        self.openButton.setVisible(built)
        self.stopButton.setVisible(busy)
        self.closeButton.setText(_("Hide") if busy else _("Close"))
        for widget in (self.listWidget, self.limitCombo, self.scansCheck, self.folderEdit, self.folderBrowse, self.sourceEdit, self.sourceBrowse):
            widget.setEnabled(not busy)

    # Actions

    def openPage(self):
        entry = self.entry()
        link = entry["source"].get("link") if entry else None
        if link:
            from . import projects

            projects.openLink(link)

    def browse(self):
        start = os.path.dirname(self.folder()) or os.path.expanduser("~")
        chosen = qt.QFileDialog.getExistingDirectory(self.dialog, _("Folder of the new study"), start)
        if chosen:
            entry = self.entry()
            self.folderEdit.setText(os.path.join(str(chosen), entry["id"]) if os.path.isdir(str(chosen)) and os.listdir(str(chosen)) else str(chosen))

    def browseSource(self):
        chosen = qt.QFileDialog.getExistingDirectory(self.dialog, _("The folder you downloaded"), str(self.sourceEdit.text) or os.path.expanduser("~"))
        if chosen:
            self.sourceEdit.setText(str(chosen))

    def freeCache(self):
        entry = self.entry()
        if entry is not None and not self.isBusy():
            self.tutorials.clear_cache(entry["id"], cacheFolder())
            self._refreshCache()
            self.statusLabel.setText(_("Downloaded files deleted. The studies you built keep their own copies."))

    def start(self):
        """Run the build job for the selected cohort. Return True when the job was submitted."""
        entry = self.entry()
        if entry is None or self.isBusy() or not self.downloadButton.enabled:
            return False
        try:
            from LesionNetworkMappingUI import jobrunner
        except ImportError:
            self.statusLabel.setText(_("The job runner is not available in this build."))
            return False
        dest = self.folder()
        self._startOptions = self._options()
        sourceDir = str(self.sourceEdit.text).strip() or None
        args = buildArguments(entry, dest, self.limit(), self.scans(), sourceDir, catalog=catalogPath)
        spec = jobrunner.JobSpec("LesionNetworkMappingLib.tutorials", args, title=_("Tutorial cohort"), heavy=False, kind="tutorial", runRoot=self.tutorials.cache_root(cacheFolder()),
                                 onEvent=self._onEvent, onDone=self._onDone, name="tutorial")
        self.resultPath = None
        self._resultId = None
        self._resultOptions = None
        self._jobId = entry["id"]
        self._warnings = 0
        self.progressBar.setRange(0, 0)
        self.progressBar.show()
        self.statusLabel.setText(_("Listing the files…"))
        self.job = jobrunner.instance().submit(spec)
        self._updateButtons()
        return True

    def stop(self):
        if self.isBusy():
            self.job.stop()
            self.statusLabel.setText(_("Stopping… files that were downloaded are kept for next time."))

    def _onEvent(self, event):
        kind = event.get("event")
        if kind == "stage":
            labels = {"plan": _("Listing the files…"), "download": _("Downloading…"), "build": _("Building the study…")}
            self.statusLabel.setText(labels.get(event.get("name"), str(event.get("label") or "")))
        elif kind == "warning":
            self._warnings += 1
        elif kind == "progress":
            stage, done, total = event.get("stage"), event.get("done") or 0, event.get("total") or 0
            if stage == "download":
                have, want = event.get("bytes_done") or 0, event.get("bytes_total") or 0
                fraction = have / want if want else (done / total if total else 0)
                text = _("Downloading… {done} of {total} files").format(done=done, total=total)
                if want:
                    text += " · " + _("{have} of {want}").format(have=self.tutorials.human_size(have), want=self.tutorials.human_size(want))
                self.statusLabel.setText(text)
            elif stage == "build":
                fraction = done / total if total else 0
                self.statusLabel.setText(_("Building the study… {done} of {total}").format(done=done, total=total))
            else:
                return
            self.progressBar.setRange(0, 1000)
            self.progressBar.setValue(int(max(0.0, min(1.0, fraction)) * 1000))

    def _onDone(self, job):
        self.progressBar.hide()
        entry = next((item for item in self.entries if item["id"] == getattr(self, "_jobId", None)), None)
        if job.succeeded():
            path = next(iter(job.outputs or []), None) or next((item.get("path") for item in job.results if item.get("kind") == "study"), None)
            result = next((item for item in job.results if item.get("kind") == "study"), {})
            self.resultPath = path
            self._resultId = entry["id"] if entry else None
            self._resultOptions = self._startOptions
            subjects = int(result.get("subjects") or 0)
            text = (_("Built {n} subject") if subjects == 1 else _("Built {n} subjects")).format(n=subjects)
            if result.get("native"):
                text += " · " + _("normalize first (A-03)")
            if self._warnings:
                text += " · " + (_("{n} note, see the job log") if self._warnings == 1 else _("{n} notes, see the job log")).format(n=self._warnings)
            self.statusLabel.setText(text)
            _toast(_("Tutorial study ready · {n} subjects").format(n=subjects), 6000, action=self.openStudy, actionText=_("Open"))
        elif job.state == "cancelled":
            self.statusLabel.setText(_("Stopped. Downloaded files are kept; nothing was built."))
        else:
            reason = str(job.failureReason or _("The build failed"))
            if "No network" in reason or "offline" in reason.lower():
                reason += " " + _("Connect to the internet and try again: files that were downloaded are kept.")
            self.statusLabel.setText(_("Not built: {reason}").format(reason=reason))
        self._refreshCache()
        self._updateButtons()

    def openStudy(self):
        """Open the study that was built (Library shows its subject list)."""
        path = self.resultPath
        if not path or not os.path.isdir(path):
            return None
        studyUi = library._studyUi()
        if studyUi is None:
            self.statusLabel.setText(_("Studies are not available in this build. The study is in {path}").format(path=path))
            return None
        self.dialog.hide()
        return library._runAction(lambda: studyUi.openStudy(path, library._shell()), _("Failed to open the study."))

    def show(self):
        self._refreshCache()
        self._updateButtons()
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()


_dialog = None


def dialog():
    """The tutorials dialog (created on first use; kept, so a running download shows its progress again)."""
    global _dialog
    if _dialog is None or not library._isAlive(_dialog.dialog):
        _dialog = TutorialsDialog()
    return _dialog


def openDialog(tutorialId=None):
    """Open the dialog (optionally on one cohort) and return it. Works without an open study."""
    try:
        shown = dialog()
        if tutorialId:
            shown.select(tutorialId)
        shown.show()
        return shown
    except Exception as exc:
        logging.error(f"Studio tutorials: {traceback.format_exc()}")
        if not slicer.app.testingEnabled():
            slicer.util.errorDisplay(_("Tutorial cohorts could not be opened.\n\n{error}").format(error=exc))
        return None


#
# Library extension points
#


def _createRow(session):
    row, button = library._rowButton(_("Tutorial cohorts…"), "StudioImportTutorialCohortsButton", openDialog, "secondary")
    button.setToolTip(ui.tooltip(_("Tutorial cohorts…"), None, _("Practice studies from public CC0 and Apache data: download only when you click, with licence and citation shown")))
    return row


CARD = {"section": "import", "order": CARD_ORDER, "create": _createRow}


def tutorialAction():
    """Search: Tutorial cohorts…."""
    return openDialog() is not None


#: Search entries (dict form of the provider contract of StudioLib.palette); no study needs to be open
PALETTE_ACTIONS = [
    {"id": "study.tutorialCohorts", "title": "Tutorial cohorts…", "category": "Library",
     "synonyms": ["tutorial", "practice data", "example data", "sample cohort", "learn", "training data", "open data", "cc0", "joutsa", "soop", "aphasia", "arc", "nihss", "lesymap", "download cohort"],
     "callback": tutorialAction, "enabled": True,
     "description": "Practice studies built from reusable public data (CC0 and Apache); a cohort downloads only when you click, with its licence and citation shown"},
]

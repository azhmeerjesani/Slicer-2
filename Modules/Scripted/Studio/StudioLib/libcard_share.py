"""Library extension: "Export study for sharing…" (L-46, LNM addendum 4.3 and 7.2): a share-safe BIDS-derivatives copy.

The STUDY card "Share" says when the study was last exported for sharing (from the private records in
``<study>/.studio/share/``) and has one button, **Export study for sharing…** (also in Search). It opens a dialog:

- **Participants**: pseudonymise ids (on by default; the key stays in the study's ``.studio`` folder), and one check box
  per column. Free text, tracer and reviewer names and dates are off by default and marked; paths are never offered.
- **Lesion masks** (opt-in, template space only) with the identifiability warning; own-patient masks need an explicit
  choice (share them or leave them out), which is recorded; optional fMRIPrep and lacuna aliases.
- **Maps** (opt-in): the study's unthresholded group maps, and a NeuroVault-ready folder (nothing is uploaded).
- **Licence**: an explicit choice. CC0 is pre-selected for the user's own masks only; with GSP-derived maps there is no
  default, the GSP data use terms note is shown and must be acknowledged.
- **Output**: a folder (or a .zip file) outside the study's application files.
- **Checklist**: ids, columns, headers, paths, own-patient decision and licence, updated as the options change. Export is
  enabled when no item is blocking.

The export (``LesionNetworkMappingLib.share.export_dataset``) runs as a light job in a worker thread (Stop works) on a
freshly opened copy of the saved study, and ends with a toast whose action shows the folder. Nothing is uploaded and no
setting is written. Works in Classic (the Studio module panel hosts the same cards).
"""

import html
import logging
import os
import traceback

import qt
import slicer
from slicer.i18n import tr as _

from . import library, ui

#: Order of the card among the STUDY cards (the last one: sharing comes after the analysis)
CARD_ORDER = 90
#: Debounce of the checklist preview while options change
PREVIEW_DELAY_MS = 150
#: Rows of the column list before it scrolls
COLUMN_ROWS = 6
OWN_PATIENT_CHOICES = ((None, "Choose…"), ("share", "Share them"), ("exclude", "Leave them out"))
#: Checklist items whose explanation the dialog already shows above the checklist (the identifiability warning, the GSP note)
SHOWN_ABOVE = ("identifiable", "gsp_terms")
STATUS_MARKS = {"ok": ("✓", "success"), "warn": ("!", "warning"), "blocker": ("✕", "danger"), "fail": ("✕", "danger")}


def _lnm(name):
    import importlib

    return importlib.import_module("LesionNetworkMappingLib." + name)


def _share():
    return _lnm("share")


def _session():
    studyUi = library._studyUi()
    return studyUi.session() if studyUi is not None else None


def _toast(text, ms=3000, **kwargs):
    shell = library._shell()
    if shell is not None:
        shell.toast(text, ms, **kwargs)
    else:
        logging.info(f"Studio share: {text}")


def _studioVersion():
    try:
        from .version import STUDIO_VERSION

        return STUDIO_VERSION
    except ImportError:
        return None


def lastExport(studyPath):
    """The most recent private export record of a study (``.studio/share/*.json``), or None."""
    import json

    folder = os.path.join(studyPath, ".studio", _share().SHARE_DIR)
    try:
        names = sorted(name for name in os.listdir(folder) if name.endswith(".json"))
    except OSError:
        return None
    for name in reversed(names):
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def defaultOutput(study):
    """A free folder next to the study: "<study folder>-share", "-share-2"..."""
    base = os.path.normpath(study.path) + "-share"
    candidate, number = base, 2
    while os.path.exists(candidate) or os.path.exists(candidate + ".zip"):
        candidate, number = f"{base}-{number}", number + 1
    return candidate


def exportWork(progress, cancel, studyPath, out, options, stages):
    """Worker thread: ``share.export_dataset`` on a fresh copy of the saved study (``stages`` maps stage keys to texts)."""
    share = _share()
    study = _lnm("study").Study.open(studyPath)
    return share.export_dataset(study, out, options, progress=lambda stage, done=None, total=None: progress(stages.get(stage, stage), done, total),
                                cancel=cancel)


def _importForWorker():
    for name in ("share", "export", "nifti", "study", "schema", "jobs"):
        _lnm(name)


#
# The dialog
#


class _Dialog(qt.QDialog):
    """Esc and the close button go through the owner's ``cancel`` (Stop while exporting)."""

    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self._owner = owner

    def reject(self):
        owner = self._owner
        if owner is not None and owner.worker.isBusy():
            owner.cancel()
            return
        qt.QDialog.reject(self)


class ShareDialog:
    """Options, licence, output and the live checklist of a share-safe export; the export runs in a worker thread."""

    def __init__(self, session, parent=None):
        self.session = session
        self.study = session.study
        self.result = None
        self.error = None
        self.plan = None
        share = _share()
        self._mapped = share.map_plan(self.study.path)
        self._gspMaps = any(entry["gsp"] for entry in self._mapped)
        self._unknownTermsMaps = any(entry.get("terms_unknown") for entry in self._mapped)
        self.dialog = _Dialog(self, parent or slicer.util.mainWindow())
        self.dialog.setObjectName("StudioShareDialog")
        self.dialog.setWindowTitle(_("Export study for sharing"))
        self.dialog.setMinimumWidth(ui.fontPx(540))
        outer = qt.QVBoxLayout(self.dialog)
        intro = qt.QLabel(_("Writes a copy of the study for sharing (BIDS derivatives). Figures, PDFs, scans, native-space data and "
                            "application files are never included. Nothing is uploaded."))
        intro.setWordWrap(True)
        outer.addWidget(intro)
        scroll = qt.QScrollArea()
        scroll.setObjectName("StudioShareScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(qt.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(qt.Qt.ScrollBarAlwaysOff)
        body = qt.QWidget()
        layout = qt.QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 4, 0)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        self._scroll, self._body = scroll, body

        form = qt.QFormLayout()
        self.nameEdit = qt.QLineEdit(self.study.name)
        self.nameEdit.setObjectName("StudioShareName")
        form.addRow(_("Dataset name"), self.nameEdit)
        self.authorsEdit = qt.QLineEdit()
        self.authorsEdit.setObjectName("StudioShareAuthors")
        self.authorsEdit.setPlaceholderText(_("Authors, separated by semicolons"))
        form.addRow(_("Authors"), self.authorsEdit)
        layout.addLayout(form)

        layout.addWidget(self._header(_("Participants")))
        self.pseudonymCheck = qt.QCheckBox(_("Pseudonymise ids (sub-P001…; the key stays in this study)"))
        self.pseudonymCheck.setObjectName("StudioSharePseudonymise")
        self.pseudonymCheck.setChecked(True)
        layout.addWidget(self.pseudonymCheck)
        self.includeExcludedCheck = qt.QCheckBox(_("Also rows with include = 0"))
        self.includeExcludedCheck.setObjectName("StudioShareIncludeExcluded")
        layout.addWidget(self.includeExcludedCheck)
        columnsLabel = qt.QLabel(_("Columns of participants.tsv (paths are never exported):"))
        layout.addWidget(columnsLabel)
        self.columnArea = qt.QScrollArea()
        self.columnArea.setObjectName("StudioShareColumns")
        self.columnArea.setWidgetResizable(True)
        columnWidget = qt.QWidget()
        columnLayout = qt.QVBoxLayout(columnWidget)
        columnLayout.setContentsMargins(6, 4, 6, 4)
        columnLayout.setSpacing(2)
        self.columnChecks = {}
        for choice in share.column_choices(self.study):
            text = choice["name"]
            if choice["sensitive"]:
                text += " — " + _(choice["sensitive"])
            check = qt.QCheckBox(text)
            check.setObjectName(f"StudioShareColumn_{choice['name']}")
            check.setChecked(choice["default"])
            if choice["description"]:
                check.setToolTip(choice["description"])
            check.connect("toggled(bool)", lambda _checked: self.schedulePreview())
            columnLayout.addWidget(check)
            self.columnChecks[choice["name"]] = check
        columnLayout.addStretch(1)
        self.columnArea.setWidget(columnWidget)
        self.columnArea.setFixedHeight(ui.fontPx(22) * COLUMN_ROWS + 8)
        layout.addWidget(self.columnArea)

        layout.addWidget(self._header(_("Lesion masks")))
        self.lesionsCheck = qt.QCheckBox(_("Include lesion masks (template space only)"))
        self.lesionsCheck.setObjectName("StudioShareLesions")
        layout.addWidget(self.lesionsCheck)
        self.identifiableLabel = library._cardLabel("StudioShareIdentifiable", 12, "warning")
        self.identifiableLabel.setText(_(share.IDENTIFIABILITY_WARNING))
        layout.addWidget(self.identifiableLabel)
        ownRow = qt.QHBoxLayout()
        self.ownLabel = qt.QLabel()
        self.ownLabel.setObjectName("StudioShareOwnPatientLabel")
        ownRow.addWidget(self.ownLabel, 1)
        self.ownCombo = qt.QComboBox()
        self.ownCombo.setObjectName("StudioShareOwnPatient")
        self.ownCombo.setAccessibleName(_("Own-patient masks"))
        for _value, text in OWN_PATIENT_CHOICES:
            self.ownCombo.addItem(_(text))
        ownRow.addWidget(self.ownCombo)
        self.ownWidget = qt.QWidget()
        self.ownWidget.setLayout(ownRow)
        ownRow.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.ownWidget)
        aliasRow = qt.QHBoxLayout()
        self.fmriprepCheck = qt.QCheckBox(_("fMRIPrep alias (label-lesion_roi)"))
        self.fmriprepCheck.setObjectName("StudioShareAliasFmriprep")
        self.lacunaCheck = qt.QCheckBox(_("lacuna alias"))
        self.lacunaCheck.setObjectName("StudioShareAliasLacuna")
        aliasRow.addWidget(self.fmriprepCheck)
        aliasRow.addWidget(self.lacunaCheck)
        aliasRow.addStretch(1)
        layout.addLayout(aliasRow)

        layout.addWidget(self._header(_("Maps")))
        count = len(self._mapped)
        self.mapsCheck = qt.QCheckBox((_("Include {n} unthresholded group map") if count == 1 else _("Include {n} unthresholded group maps")).format(n=count))
        self.mapsCheck.setObjectName("StudioShareMaps")
        self.mapsCheck.setEnabled(count > 0)
        layout.addWidget(self.mapsCheck)
        self.neurovaultCheck = qt.QCheckBox(_("Also write a NeuroVault-ready folder (nothing is uploaded)"))
        self.neurovaultCheck.setObjectName("StudioShareNeuroVault")
        self.neurovaultCheck.setEnabled(count > 0)
        layout.addWidget(self.neurovaultCheck)

        layout.addWidget(self._header(_("Licence")))
        self.licenceCombo = qt.QComboBox()
        self.licenceCombo.setObjectName("StudioShareLicence")
        self.licenceCombo.setAccessibleName(_("Licence"))
        layout.addWidget(self.licenceCombo)
        self.licenceEdit = qt.QPlainTextEdit()
        self.licenceEdit.setObjectName("StudioShareLicenceText")
        self.licenceEdit.setPlaceholderText(_("Licence text"))
        self.licenceEdit.setFixedHeight(ui.fontPx(60))
        layout.addWidget(self.licenceEdit)
        self.gspLabel = library._cardLabel("StudioShareGspNote", 12, "warning")
        self.gspLabel.setText(_(share.GSP_TERMS_NOTE))
        layout.addWidget(self.gspLabel)
        self.gspCheck = qt.QCheckBox(_("I have read the GSP data use terms note"))
        self.gspCheck.setObjectName("StudioShareGspAcknowledged")
        layout.addWidget(self.gspCheck)
        self._licenceKeys = []
        self._licenceGsp = None

        layout.addWidget(self._header(_("Output")))
        outRow = qt.QHBoxLayout()
        self.outputEdit = qt.QLineEdit(defaultOutput(self.study))
        self.outputEdit.setObjectName("StudioShareOutput")
        outRow.addWidget(self.outputEdit, 1)
        self.browseButton = qt.QPushButton(_("Choose…"))
        self.browseButton.setObjectName("StudioShareBrowse")
        self.browseButton.connect("clicked()", self.browse)
        outRow.addWidget(self.browseButton)
        layout.addLayout(outRow)
        self.zipCheck = qt.QCheckBox(_("As a .zip file"))
        self.zipCheck.setObjectName("StudioShareZip")
        layout.addWidget(self.zipCheck)

        layout.addStretch(1)

        outer.addWidget(self._header(_("Checklist")))
        self.checklistLabel = qt.QLabel()
        self.checklistLabel.setObjectName("StudioShareChecklist")
        self.checklistLabel.setWordWrap(True)
        self.checklistLabel.setTextFormat(qt.Qt.RichText)
        self.checklistLabel.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
        self.checklistLabel.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        outer.addWidget(self.checklistLabel)

        self.progressLabel = qt.QLabel()
        self.progressLabel.setObjectName("StudioShareProgress")
        self.progressLabel.setWordWrap(True)
        self.progressLabel.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
        outer.addWidget(self.progressLabel)
        buttons = qt.QHBoxLayout()
        buttons.addStretch(1)
        self.cancelButton = qt.QPushButton(_("Cancel"))
        self.cancelButton.setObjectName("StudioShareCancel")
        self.cancelButton.connect("clicked()", self.cancel)
        buttons.addWidget(self.cancelButton)
        self.exportButton = qt.QPushButton(_("Export"))
        self.exportButton.setObjectName("StudioShareExport")
        self.exportButton.setDefault(True)
        self.exportButton.connect("clicked()", self.export)
        buttons.addWidget(self.exportButton)
        outer.addLayout(buttons)

        self._timer = qt.QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(PREVIEW_DELAY_MS)
        self._timer.connect("timeout()", self.preview)
        for check in (self.pseudonymCheck, self.includeExcludedCheck, self.lesionsCheck, self.fmriprepCheck, self.lacunaCheck, self.mapsCheck,
                      self.neurovaultCheck, self.gspCheck, self.zipCheck):
            check.connect("toggled(bool)", lambda _checked: self.schedulePreview())
        self.ownCombo.connect("currentIndexChanged(int)", lambda _index: self.schedulePreview())
        self.licenceCombo.connect("currentIndexChanged(int)", lambda _index: self.schedulePreview())
        self.licenceCombo.connect("activated(int)", self._onLicenceActivated)
        self.licenceEdit.connect("textChanged()", self.schedulePreview)
        self.outputEdit.connect("textChanged(QString)", lambda _text: self.schedulePreview())
        from .libcard_cohort import _Worker

        self.worker = _Worker(self._onDone, self._onProgress)
        self.preview()
        self._fitToScreen()

    def _fitToScreen(self, fraction=0.9):
        """Open tall enough to show every option without scrolling, but never taller than most of the screen."""

        def hint(widget):
            size = widget.sizeHint
            return size() if callable(size) else size

        chrome = hint(self.dialog).height() - hint(self._scroll).height()
        width = max(self.dialog.minimumWidth, hint(self.dialog).width())
        height = chrome + hint(self._body).height() + 8
        screen = ui.screenOf(slicer.util.mainWindow() or self.dialog)
        if screen is not None:
            available = screen.availableGeometry
            available = available() if callable(available) else available
            width = min(width, int(available.width() * fraction))
            height = min(height, int(available.height() * fraction))
        self.dialog.resize(width, height)

    @staticmethod
    def _header(text):
        label = qt.QLabel(text.upper())
        label.setProperty("slicerRole", "sectionHeader")
        library._setFont(label, 11, qt.QFont.DemiBold, 0.6)
        return label

    # Options

    def gspIncluded(self):
        """Whether GSP-derived maps are part of the export as the options stand."""
        return self._gspMaps and (self.mapsCheck.checked or self.neurovaultCheck.checked)

    def _fillLicences(self, gsp):
        """The licence choices: no default with GSP-derived maps, CC0 pre-selected (suggested) for the user's own masks."""
        if gsp == self._licenceGsp:
            return
        share = _share()
        current = self.licenceKey()
        self._licenceGsp = gsp
        self.licenceCombo.blockSignals(True)
        self.licenceCombo.clear()
        self._licenceKeys = []
        if gsp:
            self.licenceCombo.addItem(_("Choose a licence…"))
            self._licenceKeys.append(None)
        for key, (title, _url) in share.LICENCES.items():
            text = _(title)
            if not gsp and key == share.suggested_licence(False):
                text += " — " + _("suggested for your own masks")
            self.licenceCombo.addItem(text)
            self._licenceKeys.append(key)
        if gsp and not self._licenceChosen:
            keep = None  # no default with GSP-derived maps: the suggestion made for own masks is withdrawn
        else:
            keep = current
        default = share.suggested_licence(gsp)
        target = keep if keep in self._licenceKeys else default if default in self._licenceKeys else self._licenceKeys[0]
        self.licenceCombo.setCurrentIndex(self._licenceKeys.index(target))
        self.licenceCombo.blockSignals(False)

    _licenceChosen = False  # True once the user picked a licence in the combo (activated, not set by the dialog)

    def licenceKey(self):
        index = int(self.licenceCombo.currentIndex)
        return self._licenceKeys[index] if 0 <= index < len(self._licenceKeys) else None

    def options(self):
        """The share options as the dialog shows them (``share.DEFAULT_OPTIONS`` keys)."""
        own = OWN_PATIENT_CHOICES[max(0, int(self.ownCombo.currentIndex))][0]
        aliases = tuple(alias for alias, check in (("fmriprep", self.fmriprepCheck), ("lacuna", self.lacunaCheck)) if check.checked)
        return {
            "name": str(self.nameEdit.text).strip() or None,
            "authors": [author.strip() for author in str(self.authorsEdit.text).split(";") if author.strip()],
            "columns": [name for name, check in self.columnChecks.items() if check.checked],
            "pseudonymise": bool(self.pseudonymCheck.checked),
            "include_excluded": bool(self.includeExcludedCheck.checked),
            "lesions": bool(self.lesionsCheck.checked),
            "own_patient": own,
            "aliases": aliases,
            "maps": bool(self.mapsCheck.checked),
            "neurovault": bool(self.neurovaultCheck.checked),
            "licence": self.licenceKey(),
            "licence_text": str(self.licenceEdit.toPlainText()) or None,
            "terms_acknowledged": bool(self.gspCheck.checked),
            "zip": bool(self.zipCheck.checked),
            "software": {"version": _studioVersion(), "code_url": _share().CODE_URL},
        }

    def output(self):
        text = str(self.outputEdit.text).strip()
        if not text:
            return ""
        text = os.path.abspath(os.path.expanduser(text))
        if self.zipCheck.checked and not text.lower().endswith(".zip"):
            text += ".zip"
        return text

    def browse(self):
        start = os.path.dirname(self.output()) or os.path.dirname(self.study.path)
        if self.zipCheck.checked:
            path = qt.QFileDialog.getSaveFileName(self.dialog, _("Export study for sharing"), os.path.join(start, os.path.basename(defaultOutput(self.study)) + ".zip"),
                                                  _("Zip archives (*.zip)"))
        else:
            path = qt.QFileDialog.getExistingDirectory(self.dialog, _("Empty folder for the export"), start)
        if path:
            self.outputEdit.setText(str(path))

    # Preview

    def schedulePreview(self):
        self._timer.start()

    def _onLicenceActivated(self, index):
        self._licenceChosen = self._licenceKeys[index] is not None if 0 <= index < len(self._licenceKeys) else False
        self.schedulePreview()

    def preview(self):
        """Recompute the plan (no file is written) and show the checklist; Export is enabled when nothing blocks."""
        share = _share()
        # No default licence with GSP-derived maps or with maps whose data use terms are not recorded
        self._fillLicences(self.gspIncluded() or (self._unknownTermsMaps and bool(self.mapsCheck.checked or self.neurovaultCheck.checked)))
        lesions = bool(self.lesionsCheck.checked)
        self.identifiableLabel.setVisible(lesions)
        self.fmriprepCheck.setEnabled(lesions)
        self.lacunaCheck.setEnabled(lesions)
        self.licenceEdit.setVisible(self.licenceKey() == "other")
        gsp = self.gspIncluded()
        self.gspLabel.setVisible(gsp)
        self.gspCheck.setVisible(gsp)
        options = self.options()
        try:
            self.plan = share.plan(self.study, options, mapped=self._mapped)
            items = list(self.plan["checklist"])
        except Exception as e:
            logging.debug(f"Studio share: the plan failed:\n{traceback.format_exc()}")
            self.plan = None
            items = [{"id": "plan", "label": _("The export cannot be planned"), "status": "blocker", "detail": str(e)}]
        ownCount = 0
        if self.plan is not None:
            shareable = {sid for sid, _path, _space in share.lesion_plan(self.study, self.plan["rows"], {**share._options(options), "own_patient": "share"})[0]}
            ownCount = len([sid for sid in self.plan["own_patient"] if sid in shareable])
        self.ownWidget.setVisible(lesions and ownCount > 0)
        self.ownLabel.setText((_("{n} mask comes from the study's own patients:") if ownCount == 1 else _("{n} masks come from the study's own patients:")).format(n=ownCount))
        output = self.output()
        if not output:
            items.append({"id": "output", "label": _("Output chosen"), "status": "blocker", "detail": _("Choose a folder or a .zip file.")})
        elif os.path.exists(output) and (not os.path.isdir(output) or os.listdir(output)):
            items.append({"id": "output", "label": _("Output chosen"), "status": "blocker", "detail": _("It already exists; choose a new name or an empty folder.")})
        self.items = items
        self.checklistLabel.setText(self.checklistHtml(items))
        self._updateButtons()

    @staticmethod
    def checklistHtml(items):
        """The items that need attention one per line (with their explanation), then the passed checks on one line."""
        lines = []
        for item in items:
            if item["status"] == "ok":
                continue
            mark, token = STATUS_MARKS.get(item["status"], ("·", "text2"))
            detail = f"<br><span style='color:{ui.token('text2').name()}'>{html.escape(_(item['detail']))}</span>" if item.get("detail") and item["id"] not in SHOWN_ABOVE else ""
            lines.append(f"<span style='color:{ui.token(token).name()}'><b>{mark}</b></span> {html.escape(_(item['label']))}{detail}")
        passed = [html.escape(_(item["label"])) for item in items if item["status"] == "ok"]
        if passed:
            lines.append(f"<span style='color:{ui.token('success').name()}'><b>✓</b></span> <span style='color:{ui.token('text2').name()}'>{' · '.join(passed)}</span>")
        return "<br>".join(lines)

    def blocking(self):
        return [item for item in getattr(self, "items", []) if item["status"] in ("blocker", "fail")]

    def _updateButtons(self):
        busy = self.worker.isBusy()
        self.exportButton.setEnabled(not busy and not self.blocking())
        self.exportButton.setToolTip("" if not self.blocking() else _("Resolve the checklist items marked ✕ first"))
        self.cancelButton.setText(_("Stop") if busy else _("Close") if self.result else _("Cancel"))

    # Export

    def export(self):
        self._timer.stop()
        self.preview()
        if self.blocking() or self.worker.isBusy():
            return False
        if hasattr(self.session, "flush"):
            self.session.flush()  # the worker reads the saved study
        _importForWorker()
        stages = {"participants": _("Participants…"), "lesions": _("Lesion masks"), "maps": _("Maps"), "neurovault": _("NeuroVault folder"),
                  "verify": _("Checking the files…")}
        self.result, self.error = None, None
        self.progressLabel.setText(_("Exporting…"))
        started = self.worker.start(exportWork, studyPath=self.study.path, out=self.output(), options=self.options(), stages=stages)
        self._updateButtons()
        return started

    def cancel(self):
        if self.worker.isBusy():
            self.worker.cancel()
            self.progressLabel.setText(_("Stopping…"))
            return
        self.dialog.reject()

    def _onProgress(self, progress):
        if not progress:
            return
        stage, done, total = progress
        text = str(stage)
        if total:
            text += f" {int(done or 0) + 1}/{int(total)}"
        self.progressLabel.setText(text)

    def _onDone(self, result, error):
        self.result, self.error = result, error
        if error == "cancelled":
            self.progressLabel.setText(_("Stopped. Nothing was written."))
        elif error:
            error = str(error)
            if error.startswith("ShareError: "):
                error = error[len("ShareError: "):]  # the reason is enough; the type name is noise
            self.progressLabel.setText(_("Not exported: {error}").format(error=error))
        else:
            path = result["path"]
            self.progressLabel.setText(_("Exported {n} files to {name}").format(n=len(result["files"]), name=os.path.basename(path)))
            _toast(_("Study exported for sharing · {n} files").format(n=len(result["files"])), 6000, action=lambda: showPath(path), actionText=_("Show"))
            refreshCards()
        self._updateButtons()
        self.preview()

    def show(self):
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()


def showPath(path):
    """Show an exported folder (or the folder of a zip file) in the file manager."""
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    qt.QDesktopServices.openUrl(qt.QUrl.fromLocalFile(folder))


_dialogs = []  # open dialogs (kept alive)


def exportForSharing(session=None, modal=False):
    """Open the "Export study for sharing" dialog for the open study. With ``modal=False`` the dialog is shown and returned."""
    session = session or _session()
    if session is None:
        _toast(_("Open a study first"))
        return None
    for dialog in list(_dialogs):
        if library._isAlive(dialog.dialog) and dialog.dialog.isVisible() and dialog.study is session.study:
            dialog.show()
            return dialog
    dialog = ShareDialog(session)
    _dialogs.append(dialog)
    dialog.dialog.connect("finished(int)", lambda _result: _dialogs.remove(dialog) if dialog in _dialogs else None)
    if modal:
        dialog.dialog.exec_() if hasattr(dialog.dialog, "exec_") else dialog.dialog.exec()
        return dialog
    dialog.show()
    return dialog


#
# The card (STUDY)
#


class ShareCard:
    """The 'Share' card of the STUDY section: the last export (from the private records) and Export study for sharing…."""

    def __init__(self, session):
        self.session = session
        self.widget = qt.QWidget()
        self.widget.setObjectName("StudioShareCard")
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(6)
        title = qt.QLabel(_("Share").upper())
        title.setObjectName("StudioShareTitle")
        title.setProperty("slicerRole", "sectionHeader")
        library._setFont(title, 11, qt.QFont.DemiBold, 0.6)
        layout.addWidget(title)
        self.statusLabel = library._cardLabel("StudioShareStatus", 12, "text2")
        layout.addWidget(self.statusLabel)
        row, self.exportButton = library._rowButton(_("Export study for sharing…"), "StudioShareExportButton", self.open, "secondary")
        self.exportButton.setToolTip(ui.tooltip(_("Export study for sharing…"), None,
                                                _("A share-safe BIDS-derivatives copy: chosen columns, pseudonymised ids, opt-in masks and maps, an explicit licence")))
        layout.addWidget(row)
        _cards.append(self)

    def dispose(self):
        if self in _cards:
            _cards.remove(self)

    def open(self):
        return exportForSharing(_session() or self.session)

    def refresh(self, selectedIds=None):
        session = _session() or self.session
        if session is None or getattr(session, "study", None) is None:
            return
        self.session = session
        record = lastExport(session.study.path)
        if record is None:
            self.statusLabel.setText(_("Not exported for sharing yet. Figures, scans and app files are never part of an export."))
            return
        when = str(record.get("created") or "")[:10]
        lesions = int(record.get("lesions") or 0)
        text = _("Last export {date} · {n} files").format(date=when, n=int(record.get("files") or 0))
        if lesions:
            text += " · " + (_("{n} lesion mask") if lesions == 1 else _("{n} lesion masks")).format(n=lesions)
        if record.get("pseudonymised"):
            text += " · " + _("pseudonymised")
        self.statusLabel.setText(text)


_cards = []  # live cards
_widgets = {}  # widget: ShareCard


def refreshCards():
    for card in list(_cards):
        if library._isAlive(card.widget):
            card.refresh()


def _createCard(session):
    card = ShareCard(session)
    _widgets[card.widget] = card
    card.widget.connect("destroyed()", lambda: (card.dispose(), _widgets.pop(card.widget, None)))
    card.refresh()
    return card.widget


def _refreshCard(widget, session, selectedIds):
    card = _widgets.get(widget)
    if card is not None and library._isAlive(widget):
        card.refresh(selectedIds)


def _paletteEnabled():
    return _session() is not None


def exportAction():
    """Search: Export study for sharing…."""
    return exportForSharing() is not None


CARD = {"section": "study", "order": CARD_ORDER, "create": _createCard, "refresh": _refreshCard}

#: Search entries (dict form of the provider contract of StudioLib.palette)
PALETTE_ACTIONS = [
    {"id": "study.exportForSharing", "title": "Export study for sharing…", "category": "Library",
     "synonyms": ["share", "sharing", "bids", "bids derivatives", "neurovault", "publish", "data sharing", "pseudonymise", "pseudonymize", "export lesions"],
     "callback": exportAction, "enabled": _paletteEnabled,
     "description": "A share-safe copy of the study: chosen columns, pseudonymised ids, opt-in masks and maps, an explicit licence; nothing is uploaded"},
]

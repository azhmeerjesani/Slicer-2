"""Lead-DBS actions of the connectome manager (Manage… menu and the manager dialog; addendum F19).

- **Import Lead-DBS maps…**: copies existing Lead-DBS outputs (``*_conn-*_desc-{AvgR,AvgR_Fz,T}_funcmap``) into the
  open study's ``derivatives/lnm/conn-<label>/`` (or a chosen folder) with sidecars, a mask and a ``maps.tsv`` that
  the Cohort card's statistics read (``leaddbs import``, a light job).
- **Verify against Lead-DBS…**: maps the 24 pinned reference lesions with the selected connectome and reports the
  measured agreement with the published Lead-DBS maps (``leaddbs parity``, a heavy job with progress and Stop). The
  live GSP data use terms text is fetched and shown first (``leaddbs terms``, a light job); nothing is downloaded
  before "I accept these data use terms" is ticked. The acceptance is stored per terms-text sha256
  (``Studio/LNM/AcceptedTermsText/<sha>``, SPEC 7.1 rule 6).

Both dialogs are non-modal; a job keeps running when its dialog is closed (Esc). Works in Classic and Studio.
"""

import math
import os

import qt
import slicer
from slicer.i18n import tr as _

from . import jobrunner, widgets

#: Acceptance of a live terms text, per sha256 (SPEC 7.1 rule 6; fork-only key)
ACCEPTED_TERMS_TEXT_SETTING = "Studio/LNM/AcceptedTermsText"
#: sha256 of the last terms text accepted for the parity references (used when the live text cannot be fetched again)
PARITY_TERMS_SETTING = "Studio/LNM/LeadDbsParityTermsSha256"
#: Last folder chosen in the import dialog (a local path: settings only, never a shared file)
IMPORT_FOLDER_SETTING = "Studio/LNM/LeadDbsImportFolder"
JOB_MODULE = "LesionNetworkMappingLib.leaddbs"
PARITY_COLUMNS = (_("Lesion"), _("Set"), _("r (T)"), _("r (AvgR)"), _("Partial r"), _("max |ΔT|"), _("Flips |T|≥7"), _("Top-10% Dice"))
IMPORT_COLUMNS = (_("Participant"), _("Lead-DBS lesion"), _("Maps"))

_dialogs = {}


def _settingTrue(key):
    return str(qt.QSettings().value(key)).lower() in ("true", "1")


def _formatNumber(value, digits=3):
    if value is None:
        return "—"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return str(value)
    if math.isnan(value):
        return "—"
    return f"{value:,.0f}" if digits == 0 else f"{value:.{digits}f}"


def _label(text, objectName, secondary=False, selectable=False):
    label = qt.QLabel(text)
    label.setObjectName(objectName)
    label.setWordWrap(True)
    if secondary:
        label.setProperty("slicerRole", "secondaryText")
    if selectable:
        label.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
    return label


def _button(text, objectName, primary=False):
    button = qt.QPushButton(text)
    button.setObjectName(objectName)
    button.setProperty("slicerRole", "primary" if primary else "secondary")
    return button


def _studyPath(ctx):
    study = getattr(ctx, "study", None) if ctx is not None else None
    return getattr(study, "path", None) if study is not None else None


#
# Import
#


class ImportDialog(qt.QDialog):
    """Import Lead-DBS maps: folder, connectome label, destination (the open study or a folder), progress and results."""

    def __init__(self, ctx, parent=None):
        qt.QDialog.__init__(self, parent or slicer.util.mainWindow())
        self.ctx = ctx
        self.job = None
        self.found = {}
        self.setObjectName("LNMLeadDbsImportDialog")
        self.setWindowTitle(_("Import Lead-DBS maps"))
        self.setMinimumWidth(560)
        layout = qt.QVBoxLayout(self)
        layout.addWidget(_label(_("Copy network maps computed by Lead-DBS (…_conn-<connectome>_desc-AvgR/AvgR_Fz/T_funcmap.nii) for cohort statistics and "
                                  "figures. The files are copied unchanged; their subject list is unknown, so they are pooled only with Lead-DBS maps of the "
                                  "same connectome label, never with maps computed here."), "LNMLeadDbsImportIntro"))
        form = qt.QFormLayout()
        folderRow = qt.QHBoxLayout()
        self.folderEdit = qt.QLineEdit(str(qt.QSettings().value(IMPORT_FOLDER_SETTING) or ""))
        self.folderEdit.setObjectName("LNMLeadDbsImportFolder")
        self.folderEdit.connect("editingFinished()", self.scan)
        folderRow.addWidget(self.folderEdit, 1)
        browse = _button(_("Browse…"), "LNMLeadDbsImportBrowse")
        browse.connect("clicked()", self.browse)
        folderRow.addWidget(browse)
        form.addRow(_("Lead-DBS outputs:"), folderRow)
        self.labelCombo = qt.QComboBox()
        self.labelCombo.setObjectName("LNMLeadDbsImportLabel")
        form.addRow(_("Connectome:"), self.labelCombo)
        studyPath = _studyPath(ctx)
        self.outEdit = qt.QLineEdit("")
        self.outEdit.setObjectName("LNMLeadDbsImportOut")
        if studyPath:
            self.destinationLabel = _label(_("the open study ({name}): derivatives/lnm/conn-<label>/").format(name=os.path.basename(studyPath)), "LNMLeadDbsImportDestination", secondary=True)
            form.addRow(_("Destination:"), self.destinationLabel)
        else:
            self.destinationLabel = None
            self.outEdit.setPlaceholderText(_("Output folder (no study is open)"))
            form.addRow(_("Destination:"), self.outEdit)
        layout.addLayout(form)
        self.termsLabel = _label(_("Maps from a GSP-derived connectome carry the GSP data use terms (GSP-DUT-2014) in their sidecars."), "LNMLeadDbsImportTerms", secondary=True)
        layout.addWidget(self.termsLabel)
        self.progress = widgets.progressRow(objectNamePrefix="LNMLeadDbsImport")
        self.progress.widget.hide()
        layout.addWidget(self.progress.widget)
        self.statusLabel = _label("", "LNMLeadDbsImportStatus", selectable=True)
        layout.addWidget(self.statusLabel)
        self.table = widgets.tableView(list(IMPORT_COLUMNS))
        self.table.setObjectName("LNMLeadDbsImportTable")
        layout.addWidget(self.table, 1)
        buttons = qt.QHBoxLayout()
        buttons.addStretch(1)
        self.importButton = _button(_("Import"), "LNMLeadDbsImportButton", primary=True)
        self.importButton.connect("clicked()", self.startImport)
        buttons.addWidget(self.importButton)
        close = _button(_("Close"), "LNMLeadDbsImportClose")
        close.connect("clicked()", self.close)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.scan()

    def browse(self):
        folder = qt.QFileDialog.getExistingDirectory(self, _("Folder of Lead-DBS outputs"), self.folderEdit.text or os.path.expanduser("~"))
        if folder:
            self.folderEdit.setText(folder)
            self.scan()

    def scan(self):
        """List the connectome labels of the folder's Lead-DBS maps (with their counts) in the combo box."""
        from LesionNetworkMappingLib import leaddbs

        folder = self.folderEdit.text.strip()
        self.labelCombo.clear()
        self.found = {}
        if not folder or not os.path.isdir(folder):
            self.statusLabel.setText(_("Choose the folder that holds the Lead-DBS outputs.") if not folder else _("Folder not found: {folder}").format(folder=folder))
            self.updateButtons()
            return
        try:
            self.found = leaddbs.scan_folder(folder)
        except OSError as exc:
            self.statusLabel.setText(str(exc))
        counts = {}
        for _lesion, conn in self.found:
            label = leaddbs._alnum(conn)
            counts[label] = counts.get(label, 0) + 1
        for label in sorted(counts):
            text = _("{label} ({n} lesion)") if counts[label] == 1 else _("{label} ({n} lesions)")
            self.labelCombo.addItem(text.format(label=label, n=counts[label]), label)
        if not counts:
            self.statusLabel.setText(_("No Lead-DBS maps in this folder (…_conn-…_desc-T_funcmap.nii[.gz])."))
        else:
            self.statusLabel.setText("")
        self.updateButtons()

    def updateButtons(self):
        running = self.job is not None and not self.job.isFinished()
        destination = bool(_studyPath(self.ctx)) or bool(self.outEdit.text.strip())
        self.importButton.setEnabled(not running and self.labelCombo.count > 0 and (destination or self.destinationLabel is None))

    def arguments(self):
        args = ["import", "--folder", self.folderEdit.text.strip(), "--label", self.labelCombo.currentData]
        studyPath = _studyPath(self.ctx)
        if studyPath:
            args += ["--study", studyPath]
        else:
            out = self.outEdit.text.strip()
            if not out:
                return None
            args += ["--out", out]
        return args

    def startImport(self):
        args = self.arguments()
        if args is None:
            self.statusLabel.setText(_("Choose an output folder (or open a study first)."))
            return None
        qt.QSettings().setValue(IMPORT_FOLDER_SETTING, self.folderEdit.text.strip())
        self.statusLabel.setText(_("Importing…"))
        spec = jobrunner.JobSpec(JOB_MODULE, args, title=_("Importing Lead-DBS maps"), heavy=False, kind="leaddbs-import", name="leaddbs-import",
                                 runRoot=jobrunner.defaultRunRoot(), onEvent=lambda event: self.progress.update(), onDone=self.onDone)
        self.job = jobrunner.instance().submit(spec)
        self.progress.setJob(self.job)
        self.progress.widget.show()
        self.updateButtons()
        return self.job

    def onDone(self, job):
        self.progress.update()
        self.updateButtons()
        if not job.succeeded():
            self.statusLabel.setText(_("The import failed: {reason}").format(reason=job.failureReason) if job.state != "cancelled" else _("Stopped."))
            return
        record = next((event for event in job.results if event.get("kind") == "import"), None)
        mapsTsv = next((event.get("path") for event in job.results if event.get("kind") == "maps"), None)
        self.fillTable(mapsTsv, record.get("path") if record else None)
        warnings = [event.get("message") for event in job.warnings if event.get("message")]
        text = _("Imported {n} lesion maps into {folder}. Use them in the Cohort card (its maps.tsv); the folder's mask.nii.gz is the mask of the statistics.").format(
            n=(job.doneEvent or {}).get("n", "?"), folder=os.path.dirname(mapsTsv) if mapsTsv else "?")
        self.statusLabel.setText(" ".join([text, *warnings]))

    def fillTable(self, mapsTsv, recordPath):
        from LesionNetworkMappingLib import cohortmaps, leaddbs

        if not mapsTsv or not os.path.isfile(mapsTsv):
            return
        rows = cohortmaps.read_maps_tsv(mapsTsv, resolve_paths=False)
        record = leaddbs._read_json(recordPath) if recordPath else None
        lesions = (record or {}).get("participants") or {}
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            present = ", ".join(key for key in leaddbs.MAP_KEYS if row.get(key) not in (None, "", "n/a"))
            for column, value in enumerate((row["participant_id"], lesions.get(row["participant_id"], ""), present)):
                self.table.setItem(index, column, qt.QTableWidgetItem(str(value)))


#
# Parity
#


class ParityDialog(qt.QDialog):
    """Verify against Lead-DBS: the live GSP terms, then the parity job, its per-lesion table and the methods sentence.

    ``catalogPath`` and ``cache`` may be set before Verify (tests and custom reference sets); the defaults are the
    shipped catalog and ``<Slicer cache>/LNM``.
    """

    def __init__(self, ctx, bundlePath, parent=None):
        qt.QDialog.__init__(self, parent or slicer.util.mainWindow())
        self.ctx = ctx
        self.bundlePath = bundlePath
        self.job = None
        self.termsJob = None
        self.termsSha = None
        self.catalogPath = None
        self.cache = os.path.join(slicer.app.cachePath, "LNM")
        self.parityResult = None
        self.setObjectName("LNMLeadDbsParityDialog")
        self.setWindowTitle(_("Verify against Lead-DBS"))
        self.setMinimumSize(760, 720)
        layout = qt.QVBoxLayout(self)
        layout.addWidget(_label(_("Maps 24 reference lesions (aphasia, addiction, epilepsy and synthetic sets of van den Heuvel et al. 2026) with this connectome and "
                                  "compares them with their published Lead-DBS GSP1000v2 Full Set maps. The numbers are measured, not promised: parity is "
                                  "like-for-like only on a connectome converted from the Lead-DBS release."), "LNMLeadDbsParityIntro"))
        self.bundleLabel = _label("", "LNMLeadDbsParityBundle", secondary=True, selectable=True)
        layout.addWidget(self.bundleLabel)
        self.releaseBanner = widgets.banner("", level="warn")
        self.releaseBanner.setObjectName("LNMLeadDbsParityRelease")
        self.releaseBanner.hide()
        layout.addWidget(self.releaseBanner)
        termsTitle = _label(_("Data use terms (shown before anything is downloaded)"), "LNMLeadDbsParityTermsTitle")
        layout.addWidget(termsTitle)
        self.sourceLabel = _label(
            _("Downloads, on request only: 24 lesion masks (licence: none stated in the van den Heuvel repository), their Lead-DBS AvgR and T maps and a degree "
              "map from raw.githubusercontent.com (one pinned commit; each file is checked against it). The Lead-DBS maps are derived from GSP1000: the GSP Open Access Data Use "
              "Terms (GSP-DUT-2014) apply to them and to the results."), "LNMLeadDbsParitySource", secondary=True)
        layout.addWidget(self.sourceLabel)
        self.termsView = qt.QTextBrowser()
        self.termsView.setObjectName("LNMLeadDbsParityTermsText")
        self.termsView.setOpenExternalLinks(True)
        self.termsView.setMinimumHeight(80)
        self.termsView.setMaximumHeight(150)
        layout.addWidget(self.termsView)
        self.acceptCheck = qt.QCheckBox(_("I accept these data use terms"))
        self.acceptCheck.setObjectName("LNMLeadDbsParityAccept")
        self.acceptCheck.setEnabled(False)
        self.acceptCheck.connect("toggled(bool)", self.onAcceptToggled)
        layout.addWidget(self.acceptCheck)
        options = qt.QHBoxLayout()
        options.addWidget(qt.QLabel(_("Subjects:")))
        self.subsetCombo = qt.QComboBox()
        self.subsetCombo.setObjectName("LNMLeadDbsParitySubset")
        options.addWidget(self.subsetCombo)
        self.saveMapsCheck = qt.QCheckBox(_("Also save our maps (Lead-DBS names)"))
        self.saveMapsCheck.setObjectName("LNMLeadDbsParitySaveMaps")
        options.addWidget(self.saveMapsCheck)
        options.addStretch(1)
        layout.addLayout(options)
        self.progress = widgets.progressRow(objectNamePrefix="LNMLeadDbsParity")
        self.progress.widget.hide()
        layout.addWidget(self.progress.widget)
        self.table = widgets.tableView(list(PARITY_COLUMNS))
        self.table.setObjectName("LNMLeadDbsParityTable")
        self.table.setMinimumHeight(150)
        layout.addWidget(self.table, 1)
        self.methodsLabel = qt.QTextBrowser()  # the methods sentence, selectable and scrollable (it is long)
        self.methodsLabel.setObjectName("LNMLeadDbsParityMethods")
        self.methodsLabel.setMaximumHeight(110)
        self.methodsLabel.hide()
        layout.addWidget(self.methodsLabel)
        self.statusLabel = _label("", "LNMLeadDbsParityStatus", secondary=True, selectable=True)
        layout.addWidget(self.statusLabel)
        buttons = qt.QHBoxLayout()
        self.openButton = _button(_("Open folder"), "LNMLeadDbsParityOpenFolder")
        self.openButton.setEnabled(False)
        self.openButton.connect("clicked()", self.openFolder)
        buttons.addWidget(self.openButton)
        buttons.addStretch(1)
        self.verifyButton = _button(_("Verify"), "LNMLeadDbsParityVerify", primary=True)
        self.verifyButton.setEnabled(False)
        self.verifyButton.connect("clicked()", self.startParity)
        buttons.addWidget(self.verifyButton)
        close = _button(_("Close"), "LNMLeadDbsParityClose")
        close.connect("clicked()", self.close)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.setBundle(bundlePath)

    # Bundle

    def setBundle(self, bundlePath):
        from LesionNetworkMappingLib import leaddbs
        from LesionNetworkMappingLib.connectome import ConnectomeBundle

        self.bundlePath = bundlePath
        self.subsetCombo.clear()
        self.subsetCombo.addItem(_("All subjects"), "")
        if not bundlePath:
            self.bundleLabel.setText(_("Select a connectome first."))
            self.updateButtons()
            return
        try:
            bundle = ConnectomeBundle.open(bundlePath)
        except Exception as exc:
            self.bundleLabel.setText(_("The connectome cannot be opened: {error}").format(error=exc))
            self.bundlePath = None
            self.updateButtons()
            return
        try:
            subsets = bundle.subsets
        except ValueError:
            subsets = {}
        for name in subsets:
            self.subsetCombo.addItem(name, name)
        self.bundleLabel.setText(_("Connectome: {name} · N={n:,} · Lead-DBS label {label}").format(name=bundle.name, n=bundle.n_subjects, label=leaddbs.leaddbs_label(bundle)))
        if leaddbs.is_leaddbs_release(bundle):
            self.releaseBanner.hide()
        else:
            widgets.setBannerText(self.releaseBanner, _("Release differences included: {caveat}.").format(caveat=leaddbs.RELEASE_CAVEAT))
            self.releaseBanner.show()
        self.updateButtons()

    # Terms

    def loadTerms(self):
        """Fetch the live terms text in a light job (never on the main thread)."""
        self.termsView.setPlainText(_("Fetching the GSP data use terms from Harvard Dataverse…"))
        spec = jobrunner.JobSpec(JOB_MODULE, ["terms"], title=_("Data use terms"), heavy=False, kind="leaddbs-terms", name="leaddbs-terms",
                                 runRoot=jobrunner.defaultRunRoot(), onDone=self.onTermsDone)
        self.termsJob = jobrunner.instance().submit(spec)
        return self.termsJob

    def onTermsDone(self, job):
        event = next((event for event in job.results if event.get("kind") == "terms"), None)
        if job.succeeded() and event is not None:
            self.setTermsText(event.get("text") or "", event.get("url"))
            return
        self.termsSha = None
        cached = self.allCached()
        previous = qt.QSettings().value(PARITY_TERMS_SETTING)
        if cached and previous and _settingTrue(f"{ACCEPTED_TERMS_TEXT_SETTING}/{previous}"):
            self.termsView.setPlainText(_("The terms could not be fetched now ({reason}); you accepted them before and every reference file is already on this computer, "
                                          "so nothing is downloaded.").format(reason=job.failureReason))
            self.termsSha = str(previous)
            self.acceptCheck.setEnabled(True)
            self.acceptCheck.setChecked(True)
        else:
            self.termsView.setPlainText(_("The data use terms could not be fetched ({reason}). They must be shown before anything is downloaded: check the connection and "
                                          "open this dialog again, or read them at https://doi.org/10.7910/DVN/KKTJQC.").format(reason=job.failureReason))
            self.acceptCheck.setEnabled(False)
            self.acceptCheck.setChecked(False)
        self.updateButtons()

    def setTermsText(self, text, url=None):
        """Show the live terms text; tick the box when this exact text was accepted before."""
        from LesionNetworkMappingLib import catalog

        self.termsSha = catalog.terms_sha256(text)
        self.termsView.setPlainText(text + (f"\n\n{url}" if url else ""))
        self.acceptCheck.setEnabled(True)
        self.acceptCheck.setChecked(_settingTrue(f"{ACCEPTED_TERMS_TEXT_SETTING}/{self.termsSha}"))
        self.updateButtons()

    def onAcceptToggled(self, checked):
        if self.termsSha:
            settings = qt.QSettings()
            key = f"{ACCEPTED_TERMS_TEXT_SETTING}/{self.termsSha}"
            if checked:
                settings.setValue(key, "true")
                settings.setValue(PARITY_TERMS_SETTING, self.termsSha)
            else:
                settings.remove(key)
        self.updateButtons()

    def allCached(self):
        from LesionNetworkMappingLib import leaddbs

        try:
            return not leaddbs.missing_files(leaddbs.load_parity_catalog(self.catalogPath), cache=self.cache)
        except Exception:
            return False

    # Run

    def updateButtons(self):
        running = self.job is not None and not self.job.isFinished()
        self.verifyButton.setEnabled(bool(self.bundlePath) and bool(self.termsSha) and self.acceptCheck.checked and not running)

    def outputFolder(self):
        from LesionNetworkMappingLib import leaddbs

        label = leaddbs.leaddbs_label(self.bundleName(), self.subsetCombo.currentData or None)
        studyPath = _studyPath(self.ctx)
        root = os.path.join(studyPath, "derivatives", "lnm") if studyPath else jobrunner.defaultRunRoot()
        return os.path.join(root, f"parity-leaddbs_conn-{label}")

    def bundleName(self):
        from LesionNetworkMappingLib.connectome import ConnectomeBundle

        try:
            return ConnectomeBundle.open(self.bundlePath)
        except Exception:
            return os.path.basename(str(self.bundlePath).rstrip("/\\"))

    def arguments(self):
        from LesionNetworkMappingLib import leaddbs

        out = self.outputFolder()
        args = ["parity", "--bundle", self.bundlePath, "--out", out, "--accept-terms", leaddbs.GSP_TERMS, "--cache", self.cache]
        studyPath = _studyPath(self.ctx)
        if studyPath:
            args += ["--run-root", studyPath]
        if self.subsetCombo.currentData:
            args += ["--subject-list", self.subsetCombo.currentData]
        if self.saveMapsCheck.checked:
            args.append("--save-maps")
        if self.catalogPath:
            args += ["--catalog", self.catalogPath]
        return args

    def startParity(self):
        if not (self.bundlePath and self.termsSha and self.acceptCheck.checked):
            return None
        self.table.setRowCount(0)
        self.methodsLabel.setPlainText("")
        self.methodsLabel.hide()
        self.statusLabel.setText(_("Verifying against Lead-DBS…"))
        spec = jobrunner.JobSpec(JOB_MODULE, self.arguments(), title=_("Verify against Lead-DBS"), heavy=True, kind="parity", name="parity",
                                 runRoot=jobrunner.defaultRunRoot(), onEvent=self.onEvent, onDone=self.onDone, context=self.ctx)
        self.job = jobrunner.instance().submit(spec)
        self.progress.setJob(self.job)
        self.progress.widget.show()
        self.updateButtons()
        return self.job

    def onEvent(self, event):
        if event.get("event") == "result" and event.get("kind") == "parity_lesion":
            self.addRow(event)
        self.progress.update()

    def addRow(self, values, setName=""):
        row = self.table.rowCount
        self.table.insertRow(row)
        cells = (values.get("id"), values.get("set", setName), _formatNumber(values.get("r_T"), 4), _formatNumber(values.get("r_AvgR"), 4),
                 _formatNumber(values.get("partial_r_T")), _formatNumber(values.get("max_abs_dT"), 2), _formatNumber(values.get("n_flip"), 0), _formatNumber(values.get("dice_top")))
        for column, value in enumerate(cells):
            self.table.setItem(row, column, qt.QTableWidgetItem(str(value or "")))

    def onDone(self, job):
        import json

        self.progress.update()
        self.updateButtons()
        if not job.succeeded():
            self.statusLabel.setText(_("Stopped.") if job.state == "cancelled" else _("The verification failed: {reason}").format(reason=job.failureReason))
            return
        path = next((event.get("path") for event in job.results if event.get("kind") == "parity"), None)
        try:
            with open(path, encoding="utf-8") as f:
                self.parityResult = json.load(f)
        except (OSError, ValueError, TypeError) as exc:
            self.statusLabel.setText(_("The report cannot be read: {error}").format(error=exc))
            return
        self.table.setRowCount(0)
        for row in self.parityResult.get("lesions") or []:
            self.addRow({**row["metrics"], "id": row["id"], "set": row["set"]})
        self.methodsLabel.setPlainText(self.parityResult.get("methods") or "")
        self.methodsLabel.show()
        self.statusLabel.setText(_("Report: {path} (terms {terms}).").format(path=path, terms=self.parityResult.get("terms")))
        self.openButton.setEnabled(True)

    def openFolder(self):
        if self.parityResult is None:
            return
        widgets.showInFileManager(self.outputFolder())


#
# Actions
#


def _show(key, dialog):
    _dialogs[key] = dialog
    dialog.show()
    dialog.raise_()
    return dialog


def openImport(ctx, bundlePath=None):
    """Show the import dialog (one per application; reused)."""
    dialog = _dialogs.get("import")
    if dialog is None:
        dialog = ImportDialog(ctx)
    else:
        dialog.ctx = ctx
    return _show("import", dialog)


def openParity(ctx, bundlePath):
    """Show the parity dialog for ``bundlePath`` and fetch the live terms text (the job starts only after acceptance)."""
    dialog = _dialogs.get("parity")
    if dialog is None or (dialog.job is None or dialog.job.isFinished()):
        if dialog is not None:
            dialog.close()
        dialog = ParityDialog(ctx, bundlePath)
        _show("parity", dialog)
        dialog.loadTerms()
        return dialog
    return _show("parity", dialog)  # a verification is running: show it rather than starting another


ACTIONS = [
    {"id": "leaddbsImport", "label": _("Import Lead-DBS maps…"), "order": 60, "factory": openImport},
    {"id": "leaddbsParity", "label": _("Verify against Lead-DBS…"), "order": 61, "factory": openParity},
]

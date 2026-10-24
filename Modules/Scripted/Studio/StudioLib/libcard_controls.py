"""Library extension: control-lesion sets (L-42, LNM addendum 7.2): "Add control set…" in IMPORT and "Control sets" in STUDY.

Specificity tests and empirical nulls compare a study's lesions with hundreds of control lesions. A control set is registered
once in ``<Studio/LNM/DataDirectory>/ControlSets/<name>`` (``LesionNetworkMappingLib.controlsets``), mapped once per connectome
into the set's own cache, and linked to any number of studies without copying a file:

- **Add control set…** (IMPORT) asks for a folder, scans its headers with the study recognisers and the template grid
  registry, asks for the template when the grid is shared by several templates (the TemplateFlow 2009 grids hold 2009c and
  2009a images), and takes a name, the source and the licence or data-use note. The lesions are then hashed and measured in
  a worker thread (Stop works), and the set is linked to the open study when "Link to this study" is ticked.
- **Control sets** (STUDY) lists the registered sets with their licence notes. For the selected set: Link to this study /
  Unlink (rows with group = control and source_type = control_db; ``controlset:`` paths, no copies), the mismatch with the
  study's cases (standardized mean differences of volume, 2D share and hemisphere, amber when |SMD| > 0.25, and the template
  family, amber when it differs; case values the table lacks are read from the lesion files in a worker thread, the card
  shows the table's values meanwhile), and "Map controls with <connectome>", which runs the cohortmaps job into the set's cache
  (lesions already mapped with that connectome and subject list are not computed again, whichever study asks).

The registry folder follows ``Studio/LNM/DataDirectory`` (only read; fork-only, D-S1): it is set on
``Study.controlset_root`` and in the environment variable ``SLICER_STUDIO_CONTROLSETS`` (inherited by jobs) whenever the
card refreshes. Nothing else is written to the settings. Works in Classic (the Studio module panel hosts the same cards).
"""

import logging
import os
import sys
import traceback
import types

import qt
import slicer
from slicer.i18n import tr as _

from . import library, ui

#: Order of the card among the STUDY cards (after cohort diagnostics, before the cohort analysis)
CARD_ORDER = 55
#: Order of "Add control set…" among the IMPORT extensions
IMPORT_ORDER = 50
DATA_DIRECTORY_SETTING = "Studio/LNM/DataDirectory"
CONNECTOMES_SETTING = "Studio/LNM/Connectomes"
#: |SMD| above this is amber (controlsets.SMD_FLAG)
SMD_FLAG = 0.25
LIST_ROWS = 4
#: Case values missing from the table (2D share, hemisphere) are read from the lesion files, in a worker thread, for studies up
#: to this many cases
COMPUTE_MAX_CASES = 500
_statsCache = {}  # (path, mtime, size): lesion values read from files, shared by the cards


def _lnm(name):
    import importlib

    return importlib.import_module("LesionNetworkMappingLib." + name)


def _session():
    studyUi = library._studyUi()
    return studyUi.session() if studyUi is not None else None


def _notify(session):
    studyUi = library._studyUi()
    notify = getattr(studyUi, "_notify", None) if studyUi is not None else None
    if callable(notify):
        notify(session)


def _toast(text, ms=3000, **kwargs):
    shell = library._shell()
    if shell is not None:
        shell.toast(text, ms, **kwargs)
    else:
        logging.info(f"Studio control sets: {text}")


#
# The registry folder (Studio/LNM/DataDirectory, only read)
#


def dataDirectory():
    """``Studio/LNM/DataDirectory`` as the normalization card reads it (one default for both)."""
    try:
        from LesionNetworkMappingUI import normalize_controller

        return normalize_controller.dataDirectory()
    except ImportError:
        pass
    value = str(qt.QSettings().value(DATA_DIRECTORY_SETTING) or "").strip()
    return value or _lnm("controlsets").default_data_directory()


def registryRoot():
    """<DataDirectory>/ControlSets, also set on ``Study.controlset_root`` and in the environment of jobs."""
    controlsets = _lnm("controlsets")
    root = controlsets.root_for_data_directory(dataDirectory())
    _lnm("study").Study.controlset_root = root
    os.environ[controlsets.ENV_ROOT] = root
    return root


def registeredSets():
    """The registered control sets (registry entries), or [] when the library is not available."""
    try:
        return _lnm("controlsets").list_sets(registryRoot())
    except Exception:
        logging.debug(f"Studio control sets: the registry could not be read:\n{traceback.format_exc()}")
        return []


def connectomes():
    """[(name, path)] of the registered connectome bundles (``Studio/LNM/Connectomes``, only read) that open."""
    paths = []
    try:
        import LesionNetworkMapping

        paths = LesionNetworkMapping.LesionNetworkMappingLogic.registeredConnectomes()
    except Exception:
        value = qt.QSettings().value(CONNECTOMES_SETTING)
        paths = [value] if isinstance(value, str) else list(value or [])
    out = []
    for path in paths:
        try:
            bundle = _lnm("connectome").ConnectomeBundle.open(path)
        except Exception:
            continue
        try:
            if bundle.kind == "timeseries":
                out.append((bundle.name, bundle.path))
        finally:
            bundle.close()
    return out


#
# Texts (pure functions of a registry entry or a mismatch report)
#


def setSummary(entry):
    """"490 lesions · MNI152NLin6Asym · 12 2D"."""
    parts = [(_("{n} lesion") if entry["n"] == 1 else _("{n} lesions")).format(n=entry["n"]), str(entry.get("space") or "")]
    twoD = sum(count for key, count in (entry.get("dimensionality") or {}).items() if key in ("2D", "multi2D"))
    if twoD:
        parts.append(_("{n} 2D").format(n=twoD))
    return " · ".join(part for part in parts if part)


def licenceText(entry):
    note = str(entry.get("licence_note") or "").strip()
    return note if note else _("No licence or data-use note recorded")


def itemText(entry):
    """The list row of a set: name and summary, then its licence note."""
    return f"{entry['name']} · {setSummary(entry)}\n{licenceText(entry)}"


def mismatchLines(report, flag=SMD_FLAG):
    """``[(text, level)]`` of a mismatch report: one line per covariate and the template family; amber above ``flag``."""
    lines = []
    for name, item in report["covariates"].items():
        value = item.get("smd")
        if value is None:
            lines.append((_("{label}: not known (Check the lesion set fills it in)").format(label=_(item["label"])), "text2"))
            continue
        text = _("{label}: SMD {smd}").format(label=_(item["label"]), smd=f"{value:+.2f}")
        lines.append((text, "warning" if abs(value) > flag else "text2"))
    family = report["family"]
    if family.get("differ"):
        lines.append((_("Template family differs: set {setSpace}, study {studySpace}").format(setSpace=family["set_space"], studySpace=family["study_space"]), "warning"))
    elif family.get("study_space"):
        lines.append((_("Same template family ({family})").format(family=family["set"]), "text2"))
    return lines


#
# Add control set… (IMPORT)
#


def registerWork(progress, cancel, folder, name, source, licenceNote, space, root, scan, stage):
    """Worker thread: ``controlsets.register_folder`` (hashing and measuring every lesion). ``stage`` is the progress text,
    translated on the main thread.
    """
    controlsets = _lnm("controlsets")
    return controlsets.register_folder(folder, name, source=source, licence_note=licenceNote, space=space, root=root, scan=scan,
                                       progress=lambda done, total: progress(stage, done, total), cancel=cancel)


def caseStatsWork(progress, cancel, paths, stage):
    """Worker thread: the values of case lesion files the table lacks (``controlsets.read_case_stats``)."""
    return _lnm("controlsets").read_case_stats(paths, progress=lambda done, total: progress(stage, done, total), cancel=cancel)


def _importForWorker():
    for name in ("controlsets", "lesionops", "nifti", "geometry", "export", "jobs", "study"):
        _lnm(name)


class _Dialog(qt.QDialog):
    """The dialog of :class:`AddControlSetDialog`: Esc and the close button go through its ``cancel`` (Stop while registering)."""

    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self._owner = owner

    def reject(self):
        owner = self._owner
        if owner is not None and owner.worker.isBusy():
            owner.cancel()  # stops the registration; the dialog stays open and shows "Stopped."
            return
        qt.QDialog.reject(self)


class AddControlSetDialog:
    """Folder, name, source, licence note and (for a shared grid) the template; then registration in a worker thread."""

    def __init__(self, parent=None, folder=None):
        self.entry = None
        self.scan = None
        self.questions = []
        self.dialog = _Dialog(self, parent or slicer.util.mainWindow())
        self.dialog.setObjectName("StudioControlSetDialog")
        self.dialog.setWindowTitle(_("Add control set"))
        self.dialog.setMinimumWidth(ui.fontPx(460))
        layout = qt.QVBoxLayout(self.dialog)
        intro = qt.QLabel(_("Register a folder of control lesions once and link it to any study. The files stay where they are; "
                            "their maps are cached per connectome."))
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = qt.QFormLayout()
        folderRow = qt.QHBoxLayout()
        self.folderEdit = qt.QLineEdit()
        self.folderEdit.setObjectName("StudioControlSetFolder")
        self.folderEdit.setReadOnly(True)
        folderRow.addWidget(self.folderEdit, 1)
        self.browseButton = qt.QPushButton(_("Choose…"))
        self.browseButton.setObjectName("StudioControlSetBrowse")
        self.browseButton.connect("clicked()", self.browse)
        folderRow.addWidget(self.browseButton)
        form.addRow(_("Folder"), folderRow)
        self.nameEdit = qt.QLineEdit()
        self.nameEdit.setObjectName("StudioControlSetName")
        self.nameEdit.setPlaceholderText(_("e.g. Ferguson490"))
        self.nameEdit.connect("textChanged(QString)", lambda _text: self._updateButtons())
        form.addRow(_("Name"), self.nameEdit)
        self.sourceEdit = qt.QLineEdit()
        self.sourceEdit.setObjectName("StudioControlSetSource")
        self.sourceEdit.setPlaceholderText(_("Citation, DOI or dataset"))
        form.addRow(_("Source"), self.sourceEdit)
        self.licenceEdit = qt.QPlainTextEdit()
        self.licenceEdit.setObjectName("StudioControlSetLicence")
        self.licenceEdit.setPlaceholderText(_("Licence or data-use agreement: who may use these lesions, and how"))
        self.licenceEdit.setFixedHeight(ui.fontPx(60))
        form.addRow(_("Licence / DUA"), self.licenceEdit)
        layout.addLayout(form)
        self.scanLabel = qt.QLabel()
        self.scanLabel.setObjectName("StudioControlSetScan")
        self.scanLabel.setWordWrap(True)
        layout.addWidget(self.scanLabel)
        self.questionsLayout = qt.QFormLayout()
        layout.addLayout(self.questionsLayout)
        self.spaceCombos = []
        self.linkCheck = qt.QCheckBox(_("Link to this study (rows are added; no file is copied)"))
        self.linkCheck.setObjectName("StudioControlSetLinkNow")
        self.linkCheck.setChecked(_session() is not None)
        self.linkCheck.setVisible(_session() is not None)
        layout.addWidget(self.linkCheck)
        self.progressLabel = qt.QLabel()
        self.progressLabel.setObjectName("StudioControlSetProgress")
        layout.addWidget(self.progressLabel)
        buttons = qt.QHBoxLayout()
        buttons.addStretch(1)
        self.cancelButton = qt.QPushButton(_("Cancel"))
        self.cancelButton.setObjectName("StudioControlSetCancel")
        self.cancelButton.connect("clicked()", self.cancel)
        buttons.addWidget(self.cancelButton)
        self.registerButton = qt.QPushButton(_("Register"))
        self.registerButton.setObjectName("StudioControlSetRegister")
        self.registerButton.setDefault(True)
        self.registerButton.connect("clicked()", self.register)
        buttons.addWidget(self.registerButton)
        layout.addLayout(buttons)
        from .libcard_cohort import _Worker

        self.worker = _Worker(self._onDone, self._onProgress)
        if folder:
            self.setFolder(folder)
        self._updateButtons()

    def browse(self):
        folder = qt.QFileDialog.getExistingDirectory(self.dialog, _("Folder of control lesions"), str(self.folderEdit.text) or os.path.expanduser("~"))
        if folder:
            self.setFolder(str(folder))

    def setFolder(self, folder):
        """Scan a folder (headers only) and show what it holds and the template questions."""
        controlsets = _lnm("controlsets")
        self.folderEdit.setText(folder)
        if not str(self.nameEdit.text).strip():
            self.nameEdit.setText("".join(c for c in os.path.basename(os.path.normpath(folder)) if c.isalnum() or c in "_.-")[:64])
        qt.QApplication.setOverrideCursor(qt.QCursor(qt.Qt.WaitCursor))
        try:
            self.scan = controlsets.scan_folder(folder)
        except Exception as e:
            self.scan = None
            self.scanLabel.setText(_("The folder could not be read: {error}").format(error=e))
            self._updateButtons()
            return
        finally:
            qt.QApplication.restoreOverrideCursor()
        lesions = self.scan["lesions"]
        grids = ", ".join(f"{gridId} ({grid['n']})" for gridId, grid in self.scan["grids"].items())
        text = (_("{n} lesion found") if len(lesions) == 1 else _("{n} lesions found")).format(n=len(lesions))
        if grids:
            text += " · " + _("grids: {grids}").format(grids=grids)
        if self.scan["skipped"]:
            text += " · " + _("{n} files skipped").format(n=len(self.scan["skipped"]))
        if controlsets.NO_GRID in self.scan["grids"]:
            text += "\n" + _("Lesions that are not on a standard template grid are left out.")
        self.questions = controlsets.questions(self.scan)
        if self.questions:
            text += "\n" + _("Several templates share this grid: choose the one the lesions were drawn on.")
        self.scanLabel.setText(text)
        while self.questionsLayout.rowCount():
            self.questionsLayout.removeRow(0)
        self.spaceCombos = []
        for question in self.questions:
            combo = qt.QComboBox()
            combo.setObjectName("StudioControlSetSpace")
            combo.addItem(_("Choose the template…"))
            for candidate in question["candidates"]:
                combo.addItem(candidate)
            combo.setToolTip(_("{n} lesions are on the {grid} grid, which several templates share. Which template were they drawn on?").format(
                n=question["n"], grid=question["grid_id"]))
            combo.connect("currentIndexChanged(int)", lambda _index: self._updateButtons())
            self.questionsLayout.addRow(_("Template of {grid}").format(grid=question["grid_id"]), combo)
            self.spaceCombos.append(combo)
        self._updateButtons()

    def answers(self):
        """{grid id: space} chosen in the dialog (unanswered grids are left out)."""
        out = {}
        for question, combo in zip(self.questions, self.spaceCombos, strict=True):
            if combo.currentIndex > 0:
                out[question["grid_id"]] = question["candidates"][combo.currentIndex - 1]
        return out

    def _updateButtons(self):
        ready = bool(self.scan and self.scan["lesions"]) and bool(str(self.nameEdit.text).strip()) and len(self.answers()) == len(self.questions)
        self.registerButton.setEnabled(ready and not self.worker.isBusy())

    def register(self):
        """Register in the worker thread. Return True when it started."""
        controlsets = _lnm("controlsets")
        if self.scan is None or self.worker.isBusy():
            return False
        try:
            name = controlsets.validate_name(str(self.nameEdit.text))
        except ValueError as e:
            self.progressLabel.setText(str(e))
            return False
        root = registryRoot()
        if os.path.exists(os.path.join(root, name, controlsets.CONTROLSET_JSON)):
            self.progressLabel.setText(_("A control set named {name} is already registered.").format(name=name))
            return False
        _importForWorker()
        self.registerButton.setEnabled(False)
        self.cancelButton.setText(_("Stop"))
        return self.worker.start(registerWork, folder=self.scan["folder"], name=name, source=str(self.sourceEdit.text).strip(),
                                 licenceNote=str(self.licenceEdit.toPlainText()).strip(), space=self.answers(), root=root, scan=self.scan,
                                 stage=_("Reading lesions"))

    def _onProgress(self, progress):
        if progress is None:
            return
        stage, done, total = progress
        self.progressLabel.setText(f"{stage} {done}/{total}" if total else stage)

    def _onDone(self, entry, error):
        self.cancelButton.setText(_("Cancel"))
        if error:
            self.progressLabel.setText(_("Stopped.") if error == "cancelled" else _("Not registered: {error}").format(error=error))
            self._updateButtons()
            return
        self.entry = entry
        session = _session()
        if self.linkCheck.isChecked() and session is not None:
            linkSet(session, entry)
        _toast(_("Control set {name} registered: {summary}").format(name=entry["name"], summary=setSummary(entry)), 4000)
        refreshCards(entry["name"])
        self.dialog.accept()

    def cancel(self):
        if self.worker.isBusy():
            self.worker.cancel()
            return
        self.worker.stop()
        qt.QDialog.reject(self.dialog)

    def exec(self):
        return library._exec(self.dialog)


_dialogs = []  # open dialogs (tests)


def addControlSet(folder=None, modal=True):
    """IMPORT: the "Add control set…" dialog. With ``modal=False`` the dialog is shown and returned (tests)."""
    dialog = AddControlSetDialog(folder=folder)
    _dialogs.append(dialog)
    if not modal:
        dialog.dialog.show()
        return dialog
    try:
        dialog.exec()
    finally:
        dialog.worker.stop()
        if dialog in _dialogs:
            _dialogs.remove(dialog)
    return dialog.entry


def _createImportRow(session):
    row, button = library._rowButton(_("Add control set…"), "StudioImportAddControlSetButton", addControlSet, "secondary")
    button.setToolTip(ui.tooltip(_("Add control set…"), None, _("Register a folder of control lesions once; link it to studies without copying")))
    return row


#
# Study actions
#


def linkSet(session, entry, ids=None):
    """Link a set to the open study (rows added or updated, no copy); flush and notify. Return the link report."""
    report = session.study.link_control_set(entry["name"], ids=ids, root=registryRoot())
    session.flush()
    _notify(session)
    added, updated, removed = len(report["added"]), len(report["updated"]), len(report.get("removed") or [])
    text = _("{n} control lesions linked (no files copied)").format(n=added) if added else _("{n} control rows updated").format(n=updated)
    if removed:
        text += " · " + (_("{n} row no longer in the set removed") if removed == 1 else _("{n} rows no longer in the set removed")).format(n=removed)
    if report["skipped"]:
        text += " · " + _("{n} skipped").format(n=len(report["skipped"]))
    _toast(text, 4000)
    return report


def unlinkSet(session, name):
    removed = session.study.unlink_control_set(name)
    session.flush()
    _notify(session)
    _toast(_("{n} control rows removed from the study (the control set is kept)").format(n=len(removed)), 4000)
    return removed


#
# The card (STUDY)
#


class ControlSetsCard:
    """The 'Control sets' card of the STUDY section (CARD, see the module docstring)."""

    def __init__(self, session):
        self.session = session
        self.entries = []
        self.report = None
        self.mapJob = None
        self._connectomes = None
        self.readingCases = 0  # case lesion files being read in the worker (0: none)
        from .libcard_cohort import _Worker

        self.statsWorker = _Worker(self._onCaseStats, self._onCaseStatsProgress)
        self.widget = qt.QWidget()
        self.widget.setObjectName("StudioControlSetsCard")
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(6)
        title = qt.QLabel(_("Control sets").upper())
        title.setObjectName("StudioControlSetsTitle")
        title.setProperty("slicerRole", "sectionHeader")
        library._setFont(title, 11, qt.QFont.DemiBold, 0.6)
        layout.addWidget(title)
        self.statusLabel = library._cardLabel("StudioControlSetsStatus", 12, "text2")
        layout.addWidget(self.statusLabel)
        self.list = qt.QListWidget()
        self.list.setObjectName("StudioControlSetsList")
        self.list.setAccessibleName(_("Control sets"))
        self.list.setWordWrap(False)  # two elided lines per set (the tool tip has the whole text): a stable row height
        self.list.setTextElideMode(qt.Qt.ElideRight)
        self.list.setVerticalScrollMode(qt.QAbstractItemView.ScrollPerPixel)
        self.list.connect("currentRowChanged(int)", lambda _row: self._renderSelection())
        layout.addWidget(self.list)
        self.details = qt.QWidget()
        self.details.setObjectName("StudioControlSetsDetails")
        detailLayout = qt.QVBoxLayout(self.details)
        detailLayout.setContentsMargins(0, 0, 0, 0)
        detailLayout.setSpacing(4)
        self.sourceLabel = library._cardLabel("StudioControlSetsSource", 12, "text")
        self.sourceLabel.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
        detailLayout.addWidget(self.sourceLabel)
        self.licenceLabel = library._cardLabel("StudioControlSetsLicence", 12, "text")
        self.licenceLabel.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
        detailLayout.addWidget(self.licenceLabel)
        self.linkLabel = library._cardLabel("StudioControlSetsLinkState", 12, "text2")
        detailLayout.addWidget(self.linkLabel)
        self.linkRow, self.linkButton = library._rowButton(_("Link to this study"), "StudioControlSetsLinkButton", self.link, "secondary")
        self.linkButton.setToolTip(_("Adds one control row per lesion (group = control); the files are not copied"))
        self.unlinkButton = library._roleButton(_("Unlink"), "StudioControlSetsUnlinkButton", self.unlink, "secondary")
        self.unlinkButton.setToolTip(_("Removes the control rows of this set from the study; the set and its files are kept"))
        self.linkRow.layout().addWidget(self.unlinkButton)
        detailLayout.addWidget(self.linkRow)
        self.mismatchTitle = library._cardLabel("StudioControlSetsMismatchTitle", 11, "text2")
        self.mismatchTitle.setText(_("Compared with the study's cases (amber: |SMD| above 0.25 or another template family)"))
        detailLayout.addWidget(self.mismatchTitle)
        self.mismatchLabels = []
        self.mismatchLayout = qt.QVBoxLayout()
        self.mismatchLayout.setSpacing(2)
        detailLayout.addLayout(self.mismatchLayout)
        mapRow = library._buttonRow()
        self.connectomeCombo = qt.QComboBox()
        self.connectomeCombo.setObjectName("StudioControlSetsConnectome")
        self.connectomeCombo.setAccessibleName(_("Connectome"))
        self.connectomeCombo.connect("currentIndexChanged(int)", lambda _index: self._renderMap())
        mapRow.layout().addWidget(self.connectomeCombo, 1)
        self.mapButton = library._roleButton(_("Map controls"), "StudioControlSetsMapButton", self.mapControls, "secondary")
        mapRow.layout().addWidget(self.mapButton, 1)
        detailLayout.addWidget(mapRow)
        self.mapLabel = library._cardLabel("StudioControlSetsMapState", 12, "text2")
        detailLayout.addWidget(self.mapLabel)
        layout.addWidget(self.details)
        _cards.append(self)

    def dispose(self):
        self.statsWorker.stop()
        if self in _cards:
            _cards.remove(self)

    # State

    def refresh(self, selectedIds=None, select=None):
        session = _session()
        if session is None:
            return
        self.session = session
        current = select or self.selectedName()
        self.entries = registeredSets()
        self.list.blockSignals(True)
        self.list.clear()
        for entry in self.entries:
            item = qt.QListWidgetItem(itemText(entry))
            item.setToolTip(f"{entry['name']}\n{entry.get('source') or ''}\n{licenceText(entry)}".strip())
            self.list.addItem(item)
        names = [entry["name"] for entry in self.entries]
        self.list.setCurrentRow(names.index(current) if current in names else (0 if names else -1))
        self.list.blockSignals(False)
        rowsHeight = sum(max(self.list.sizeHintForRow(row), ui.fontPx(36)) for row in range(min(len(names), LIST_ROWS)))
        self.list.setFixedHeight(rowsHeight + 2 * self.list.frameWidth + 2)
        linked = {item["name"] for item in session.study.control_sets}
        if not self.entries:
            self.statusLabel.setText(_("No control sets yet. Add one in Import (Add control set…)."))
        else:
            self.statusLabel.setText(_("{n} registered · {k} linked to this study").format(n=len(self.entries), k=len(linked & set(names))))
        self.list.setVisible(bool(self.entries))
        self._connectomes = None
        self._renderSelection()

    def selectedEntry(self):
        row = self.list.currentRow
        return self.entries[row] if 0 <= row < len(self.entries) else None

    def selectedName(self):
        entry = self.selectedEntry()
        return entry["name"] if entry else None

    def _renderSelection(self):
        entry = self.selectedEntry()
        self.details.setVisible(entry is not None)
        if entry is None or self.session is None:
            return
        controlsets = _lnm("controlsets")
        study = self.session.study
        self.sourceLabel.setText(_("Source: {source}").format(source=entry.get("source") or _("not recorded")))
        self.licenceLabel.setText(_("Licence / DUA: {note}").format(note=licenceText(entry)))
        rows = controlsets.linked_rows(study, entry["name"])
        record = next((item for item in study.control_sets if item["name"] == entry["name"]), None)
        if rows:
            text = _("Linked: {n} control rows").format(n=len(rows))
            if record and record.get("sha256") and record["sha256"] != entry.get("content_sha256"):
                text += " · " + _("the set changed since it was linked: link again")
            self.linkLabel.setText(text)
        else:
            self.linkLabel.setText(_("Not linked to this study"))
        self.linkButton.setText(_("Link again") if rows else _("Link to this study"))
        self.unlinkButton.setVisible(bool(rows))
        try:
            # Values the table lacks are read from the case lesion files in a worker thread, never here: until they are
            # in _statsCache the report uses the table's values only, and the card shows "Reading case lesions…"
            pending = controlsets.missing_case_files(study, _statsCache) if len(controlsets.case_rows(study)) <= COMPUTE_MAX_CASES else []
            if pending:
                self._readCaseFiles(pending)
            compute = not pending and len(controlsets.case_rows(study)) <= COMPUTE_MAX_CASES  # every file is in the cache
            self.report = controlsets.mismatch_report(entry, study, flag=SMD_FLAG, compute_missing=compute, root=registryRoot(), cache=_statsCache)
        except Exception:
            logging.debug(f"Studio control sets: no mismatch report:\n{traceback.format_exc()}")
            self.report = None
        self._renderMismatch()
        self._renderConnectomes()

    def _readCaseFiles(self, paths):
        """Read the case lesion files whose values the table lacks in the worker thread (one run at a time)."""
        if self.statsWorker.isBusy():
            return
        _lnm("controlsets")
        _lnm("lesionops")  # imported here: the worker does not import modules
        _lnm("nifti")
        self.readingCases = len(paths)
        self.statsWorker.start(caseStatsWork, paths=paths, stage=_("Reading case lesions"))

    def _onCaseStatsProgress(self, progress):
        if progress is not None and self.mismatchLabels and self.readingCases:
            _stage, done, total = progress
            self.mismatchLabels[-1].setText(_("Reading case lesions… {done}/{total}").format(done=done, total=total))

    def _onCaseStats(self, stats, error):
        self.readingCases = 0
        if error or not isinstance(stats, dict):
            if error != "cancelled":
                logging.warning(f"Studio control sets: the case lesions could not be read: {error}")
            if library._isAlive(self.widget):
                self._renderMismatch()  # the table's values stay; no retry until the card renders again
            return
        _statsCache.update(stats)  # main thread: the cache is only changed here
        if library._isAlive(self.widget) and self.session is not None:
            self._renderSelection()

    def _renderMismatch(self):
        for label in self.mismatchLabels:
            label.setParent(None)
        self.mismatchLabels = []
        lines = mismatchLines(self.report) if self.report and self.report["n_cases"] else [(_("No cases in the study to compare with"), "text2")]
        if self.readingCases and self.report and self.report["n_cases"]:
            lines = [*lines, (_("Reading case lesions…"), "text2")]
        for index, (text, level) in enumerate(lines):
            label = library._cardLabel(f"StudioControlSetsMismatch{index}", 12, level)
            label.setText(text)
            self.mismatchLayout.addWidget(label)
            self.mismatchLabels.append(label)
        self.mismatchTitle.setVisible(bool(self.report and self.report["n_cases"]))

    def _renderConnectomes(self):
        if self._connectomes is None:
            self._connectomes = connectomes()
            current = str(self.connectomeCombo.currentText)
            self.connectomeCombo.blockSignals(True)
            self.connectomeCombo.clear()
            for name, _path in self._connectomes:
                self.connectomeCombo.addItem(name)
            names = [name for name, _path in self._connectomes]
            if current in names:
                self.connectomeCombo.setCurrentIndex(names.index(current))
            self.connectomeCombo.blockSignals(False)
        self._renderMap()

    def selectedConnectome(self):
        index = self.connectomeCombo.currentIndex
        return self._connectomes[index] if self._connectomes and 0 <= index < len(self._connectomes) else None

    def _renderMap(self):
        connectome = self.selectedConnectome()
        entry = self.selectedEntry()
        busy = self.mapJob is not None and not self.mapJob.isFinished()
        self.connectomeCombo.setVisible(bool(self._connectomes))
        if connectome is None:
            self.mapButton.setText(_("Map controls"))
            self.mapButton.setEnabled(False)
            self.mapLabel.setText(_("Add a connectome in Map to map the controls."))
            return
        self.mapButton.setText(_("Map controls with {connectome}").format(connectome=connectome[0]))
        self.mapButton.setEnabled(entry is not None and not busy)
        self.mapLabel.setText(_("Mapping…") if busy else self._mapState(entry, connectome))

    def _mapState(self, entry, connectome):
        """"Mapped with GSP1000: 490 lesions" from the last cohortmaps record of the set (cheap reads), else "Not mapped yet"."""
        if entry is None:
            return ""
        try:
            controlsets, cohortmaps = _lnm("controlsets"), _lnm("cohortmaps")
            record = cohortmaps.read_record(os.path.join(controlsets.maps_out_dir(entry, connectome[1]), cohortmaps.COHORTMAPS_JSON))
        except Exception:
            record = None
        if not isinstance(record, dict):
            return _("Not mapped with {connectome} yet (maps are computed once and reused by every study)").format(connectome=connectome[0])
        lesions = record.get("lesions")
        mapped = int(lesions.get("mapped") or 0) if isinstance(lesions, dict) else 0
        finished, created = str(record.get("end") or record.get("start") or ""), str(entry.get("created") or "")
        if finished and created and finished < created:  # (ISO 8601 UTC times) mapped before the set was registered again
            return _("Mapped with {connectome} before the set changed: map again (maps already computed are reused)").format(connectome=connectome[0])
        return _("Mapped with {connectome}: {n} of {total} lesions, cached for every study").format(connectome=connectome[0], n=mapped, total=entry["n"])

    # Actions

    def link(self):
        entry, session = self.selectedEntry(), _session()
        if entry is None or session is None:
            return None
        report = library._runAction(lambda: linkSet(session, entry), _("The control set could not be linked."))
        self.refresh(select=entry["name"])
        return report

    def unlink(self):
        entry, session = self.selectedEntry(), _session()
        if entry is None or session is None:
            return None
        removed = library._runAction(lambda: unlinkSet(session, entry["name"]), _("The control set could not be unlinked."))
        self.refresh(select=entry["name"])
        return removed

    def mapControls(self):
        """Run the cohortmaps job of the selected set into its cache. Return True when it was submitted."""
        entry, connectome = self.selectedEntry(), self.selectedConnectome()
        if entry is None or connectome is None or (self.mapJob is not None and not self.mapJob.isFinished()):
            return False
        try:
            from LesionNetworkMappingUI import jobrunner
        except ImportError:
            _toast(_("The job runner is not available"))
            return False
        args = _lnm("controlsets").map_job_args(entry, connectome[1])
        spec = jobrunner.JobSpec("LesionNetworkMappingLib.cohortmaps", args, title=_("Mapping controls"), heavy=True, kind="cohortmaps",
                                 runRoot=entry["dir"], onDone=self._onMapDone, name="controlset")
        self.mapJob = jobrunner.instance().submit(spec)
        self._renderMap()
        return True

    def _onMapDone(self, job):
        if job.succeeded():
            _toast(_("Controls mapped"), 3000)
        self._renderMap()


_cards = []  # live cards
_widgets = {}  # widget: ControlSetsCard


def liveCard():
    for card in list(_cards):
        if library._isAlive(card.widget):
            return card
    return None


def refreshCards(select=None):
    for card in list(_cards):
        if library._isAlive(card.widget):
            card.refresh(select=select)


def _createCard(session):
    _ensureImportCard()
    card = ControlSetsCard(session)
    _widgets[card.widget] = card
    card.widget.connect("destroyed()", lambda: (card.dispose(), _widgets.pop(card.widget, None)))
    card.refresh()
    return card.widget


def _refreshCard(widget, session, selectedIds):
    card = _widgets.get(widget)
    if card is not None and library._isAlive(widget):
        card.refresh(selectedIds)


#
# Library extension points
#

CARD = {"section": "study", "order": CARD_ORDER, "create": _createCard, "refresh": _refreshCard}

#: The IMPORT entry lives in a companion module: the libcard contract has one CARD per module
IMPORT_MODULE = "StudioLib.libcard_controls_import"


def _ensureImportCard():
    """Register the companion module whose CARD is "Add control set…" in IMPORT (library.libcardModules lists it)."""
    module = sys.modules.get(IMPORT_MODULE)
    if module is None:
        module = types.ModuleType(IMPORT_MODULE, "Library extension: 'Add control set…' in IMPORT (see StudioLib.libcard_controls).")
        module.CARD = {"section": "import", "order": IMPORT_ORDER, "create": _createImportRow}
        sys.modules[IMPORT_MODULE] = module
    modules = getattr(library, "_libcards", None)
    if isinstance(modules, list) and module not in modules:
        modules.append(module)  # the registry was built before this module was imported


def _paletteEnabled():
    return _session() is not None


def addControlSetAction():
    """Search: Add control set…."""
    if _session() is None:
        _toast(_("Open a study first"))
        return False
    addControlSet()
    return True


def showControlSetsAction():
    """Search: Control sets (Library is shown with the study's cards)."""
    if _session() is None:
        _toast(_("Open a study first"))
        return False
    shell = library._shell()
    if shell is not None:
        shell.switchTo("library")
        slicer.app.processEvents()
    card = liveCard()
    if card is not None:
        card.widget.setFocus()
    return card is not None


#: Search entries (dict form of the provider contract of StudioLib.palette)
PALETTE_ACTIONS = [
    {"id": "study.addControlSet", "title": "Add control set…", "category": "Library", "synonyms": ["control lesions", "controls", "specificity", "null", "register"],
     "callback": addControlSetAction, "enabled": _paletteEnabled, "description": "Register a folder of control lesions once and link it to studies without copying"},
    {"id": "study.controlSets", "title": "Control sets", "category": "Library", "synonyms": ["control lesions", "mismatch", "smd", "link controls", "map controls"],
     "callback": showControlSetsAction, "enabled": _paletteEnabled, "description": "Link control sets, check their mismatch with the cases and map them"},
]

_ensureImportCard()
try:
    registryRoot()
except Exception:
    logging.debug(f"Studio control sets: the registry folder is not set:\n{traceback.format_exc()}")

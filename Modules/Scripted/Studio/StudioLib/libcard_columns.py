"""Library extension: the "Columns…" roles editor of the STUDY card and the "Case" section of the SUBJECT card (L-31, LNM addendum 3.4, 7.2).

**Columns…** (STUDY card). An analysis uses the roles a study declares, never "every numeric column": ``fit_rms_mm`` and
``n_slices`` are bookkeeping, not symptoms. The card says how many columns still have no confirmed role and which numeric
columns the score pickers offer only as unconfirmed guesses; "Columns…" opens a table of every column with its inferred type, a
role combo (id, group, score, covariate, block, metadata, bookkeeping, path), units, levels and description. Confirm writes the
changed rows with ``Study.set_column_role`` (the roles live in ``participants.json``, a BIDS data dictionary that official
tools ignore the fork key of) and saves through the StudySession on the main thread. A bookkeeping or metadata column can only
become a score after a confirmation that says why this is a score leak; standard columns keep their role.

**Case** (SUBJECT card). The registry fields of the open subject, grouped as Source, Imaging, Case and Workflow and edited
in place: strict enumerations are combos, the other levels are editable combos, the rest line edits with the units shown, and
every write goes through ``StudySession.setValues`` (validated by the registry, saved with the study). A DOI has an "Open"
link (``https://doi.org/<doi>`` through ``QDesktopServices``). Derived and recorded fields (hemisphere, review status,
tracing method, flags) are shown read-only. The interim note and source reference that Draw keeps next to the lesion
(``Studio.LesionNotes`` and ``Studio.LesionSourceReference`` of the segmentation, ``Notes`` and ``SourceReference`` of the
sidecar, W2-05) are offered once as "Use": they move into the ``notes``, ``doi``, ``figure`` and ``citation`` columns, and
the Draw attributes and the sidecar keys are removed (cross-team request CRT-04).

Nothing here writes a setting, an absolute path or participants.tsv outside the StudySession. Search: "Columns…". Works in
Classic (the Studio module panel hosts the same cards) and without a shell.
"""

import json
import logging
import re
import traceback
from urllib.parse import quote

import qt
import slicer
from slicer.i18n import tr as _

from . import library, ui

#: Order of the card among the STUDY cards (after the study card, before the normalization card) and of the Case section
CARD_ORDER = 30
SECTION_ORDER = 30
#: Columns of the table
COL_NAME, COL_TYPE, COL_ROLE, COL_UNITS, COL_LEVELS, COL_DESCRIPTION = range(6)
#: Combo data of a column whose role nobody confirmed
NOT_SET = "unknown"
#: Groups of the Case section: (key, title, editable columns)
CASE_GROUPS = (
    ("source", "Source", ("source_type", "source_dataset", "source_id", "doi", "pmid", "citation", "figure", "panel", "case_label")),
    ("imaging", "Imaging", ("image_modality", "fig_convention", "planes", "dimensionality")),
    ("case", "Patient and lesion", ("dataset", "age", "sex", "handedness", "etiology", "onset_to_imaging_days", "symptom", "domain", "reported_side", "reported_location")),
    ("workflow", "Workflow", ("tracer", "reviewer", "image_quality", "multifocal")),
)
#: Shown read-only under the groups (derived or recorded by the application): (column, label)
CASE_FACTS = (("hemisphere", "Hemisphere"), ("review_status", "Review"), ("trace_method", "Traced by"), ("qc_flags", "Flags"), ("duplicate_of", "Duplicate of"))
#: Short labels of the Case fields (the registry's descriptions are the tooltips)
FIELD_LABELS = {
    "source_type": "Source type", "source_dataset": "Source dataset", "source_id": "Source ID", "doi": "DOI", "pmid": "PMID", "citation": "Citation",
    "figure": "Figure", "panel": "Panel", "case_label": "Case label", "image_modality": "Modality", "fig_convention": "Figure convention",
    "planes": "Planes", "dimensionality": "Dimensionality", "dataset": "Dataset", "age": "Age", "sex": "Sex", "handedness": "Handedness",
    "etiology": "Etiology", "onset_to_imaging_days": "Onset to imaging", "symptom": "Symptom", "domain": "Domain", "reported_side": "Reported side",
    "reported_location": "Reported location", "tracer": "Tracer", "reviewer": "Reviewer", "image_quality": "Image quality", "multifocal": "Multifocal",
}
#: Expanded groups of the Case section (kept while the application runs: the section is rebuilt with the card)
_expanded = {"source": False, "imaging": False, "case": True, "workflow": False}
DOI_PATTERN = re.compile(r"10\.\d{4,9}/\S+", re.IGNORECASE)
DOI_WITH_PREFIX_PATTERN = re.compile(r"(?:(?:https?://(?:dx\.)?doi\.org/|doi\s*:?)\s*)?10\.\d{4,9}/\S+", re.IGNORECASE)
FIGURE_PATTERN = re.compile(r"\b(?:fig(?:ure)?\.?)\s*([0-9]+[A-Za-z]?)", re.IGNORECASE)


#
# Lazy access to the library
#


def _lnm(name):
    import importlib

    return importlib.import_module("LesionNetworkMappingLib." + name)


def _session():
    studyUi = library._studyUi()
    return studyUi.session() if studyUi is not None else None


def _toast(text, ms=3000, **kwargs):
    shell = library._shell()
    if shell is not None:
        shell.toast(text, ms, **kwargs)
    else:
        logging.info(f"Studio columns: {text}")


def _notify(session):
    """Tell the study listeners (the lists, the pickers) that the study changed; the session has no public method for a role change."""
    studyUi = library._studyUi()
    notify = getattr(studyUi, "_notify", None) if studyUi is not None else None
    if notify is not None:
        notify(session)
    else:
        library.refreshStudy()


def _commit(session):
    """Save participants.tsv, participants.json and study.json now (main thread) and update the views of the study."""
    session.flush()
    _notify(session)


#
# Roles
#


def roleLabel(role):
    """The words of a role in the combo and the table."""
    return {
        "id": _("ID"),
        "group": _("Group"),
        "score": _("Score (symptom)"),
        "covariate": _("Covariate"),
        "block": _("Block (dataset)"),
        "metadata": _("Metadata"),
        "bookkeeping": _("Bookkeeping"),
        "path": _("File path"),
        NOT_SET: _("Not set"),
    }.get(role, str(role))


def roleHelp(role):
    """One sentence on what a role does in the analyses."""
    return {
        "id": _("Identifies the subject."),
        "group": _("A grouping of the lesions, such as case and control; analyses compare its levels."),
        "score": _("A symptom or outcome that the network maps are related to. Only columns marked score are offered in the score pickers."),
        "covariate": _("Adjusted for in the statistics (age, volume, 2D or 3D tracing)."),
        "block": _("Lesions of one block are exchangeable only among themselves (for example one dataset)."),
        "metadata": _("Describes the case; no analysis uses it."),
        "bookkeeping": _("Written by the application while it works (status, quality control, fit residuals); never a score."),
        "path": _("A file path relative to the study."),
        NOT_SET: _("Nobody confirmed this column's role. A numeric column is offered as an unconfirmed score until you choose one."),
    }.get(role, "")


def columnInfo(study, name):
    """What the table shows of a column: ``{"name", "spec", "role", "standard", "registered", "confirmed"}``."""
    schema = _lnm("schema")
    spec = study.column_spec(name)
    entry = study.dictionary.get(name)
    fork = schema.fork_entry(entry)
    return {
        "name": name,
        "spec": spec,
        "role": spec.role,
        "standard": name in schema.STANDARD_ROLES,
        "registered": schema.registered(name) is not None,
        "confirmed": schema.is_confirmed(entry) and fork.get("by") != "registry",
    }


def columnsSummary(study):
    """Numbers of the card: ``{"columns", "unset" (names without a confirmed or registered role), "guessed" (numeric columns offered as unconfirmed scores)}``."""
    schema = _lnm("schema")
    unset = [name for name in study.columns if study.column_spec(name).role == schema.UNKNOWN_ROLE]
    guessed = [name for name, confirmed in study.score_columns() if not confirmed]
    return {"columns": len(study.columns), "unset": unset, "guessed": guessed}


def scoreLeakReason(study, name, role):
    """Why ``name`` must not simply become a score (a sentence), or None when marking it as ``role`` is fine.

    Bookkeeping and metadata columns record how the lesions were processed or described; a score picker that offered them
    would let an analysis find the pipeline (fit residuals, slice counts, tracing time) instead of the lesion network.
    """
    if role != "score":
        return None
    current = study.column_spec(name).role
    registered = _lnm("schema").registered(name)
    if registered is not None and registered.role in ("bookkeeping", "metadata"):
        current = registered.role  # the registry says it must not be a score, whatever role was confirmed in between
    if current not in ("bookkeeping", "metadata"):
        return None
    if current == "bookkeeping":
        return _("{name} is a bookkeeping column: the application writes it while it works (quality control, fit residuals, slice counts, tracing time). "
                 "As a score it would make an analysis find how the lesions were traced instead of a lesion network.").format(name=name)
    return _("{name} is a metadata column: it only describes the case. As a score it would be related to the network maps although nothing about it "
             "was measured as a symptom.").format(name=name)


def confirmScore(names, reasons, parent=None):
    """Ask whether bookkeeping or metadata columns really become scores. Return the names the user accepts (tests replace this function)."""
    accepted = []
    for name, reason in zip(names, reasons, strict=True):
        box = qt.QMessageBox(parent or slicer.util.mainWindow())
        box.setObjectName("StudioColumnsScoreConfirm")
        box.setIcon(qt.QMessageBox.Warning)
        box.setWindowTitle(_("Use {name} as a score?").format(name=name))
        box.setText(_("Use {name} as a score?").format(name=name))
        box.setInformativeText(reason)
        use = box.addButton(_("Use as score"), qt.QMessageBox.AcceptRole)
        keep = box.addButton(_("Keep as is"), qt.QMessageBox.RejectRole)
        box.setDefaultButton(keep)
        box.setEscapeButton(keep)
        library._exec(box)
        if box.clickedButton() == use:
            accepted.append(name)
    return accepted


def _readOnly(item):
    """Make a table item not editable (the flags are taken from the item: Qt 5 takes an int, Qt 6 the flags)."""
    flags = item.flags()
    try:
        item.setFlags(flags & ~qt.Qt.ItemIsEditable)
    except (TypeError, ValueError):
        item.setFlags(int(flags) & ~int(qt.Qt.ItemIsEditable))


def parseLevels(text):
    """``{level: description or None}`` of a levels text ("case; control", "1 = Poor; 2 = Adequate"), or None for an empty text.

    Levels are separated by ";" (or by "," when there is no ";"), and a description follows "=".
    """
    text = str(text or "").strip()
    if not text:
        return None
    parts = [part.strip() for part in (text.split(";") if ";" in text else text.split(","))]
    levels = {}
    for part in parts:
        if not part:
            continue
        level, _sep, description = part.partition("=")
        level = level.strip()
        if level:
            levels[level] = description.strip() or None
    return levels or None


def levelsText(levels):
    """The text of :func:`parseLevels`' input for a levels dict."""
    if not levels:
        return ""
    return "; ".join(f"{level} = {description}" if description else str(level) for level, description in levels.items())


def applyChanges(session, changes, confirm=None):
    """Confirm the roles (and types, units, levels, descriptions) of columns of the open study.

    :param changes: ``{column: {"role", and optionally "type", "units", "levels" (dict), "description"}}``; an empty text of
      units or description removes it.
    :param confirm: ``confirm(names, reasons) -> accepted names`` for the columns that would become scores from bookkeeping
      or metadata (default :func:`confirmScore`); a column that is not accepted keeps its role and is listed in ``declined``.
    :return: ``{"applied": [names], "declined": [names], "errors": {name: message}}``. The study is saved when something was applied.
    """
    schema = _lnm("schema")
    study = session.study
    confirm = confirm or confirmScore
    guarded = [(name, scoreLeakReason(study, name, change.get("role"))) for name, change in changes.items()]
    guarded = [(name, reason) for name, reason in guarded if reason]
    accepted = set(confirm([name for name, _reason in guarded], [reason for _name, reason in guarded])) if guarded else set()
    result = {"applied": [], "declined": [], "errors": {}}
    for name, change in changes.items():
        if any(name == guardedName for guardedName, _reason in guarded) and name not in accepted:
            result["declined"].append(name)
            continue
        clear = [key for key in ("units", "description") if key in change and change[key] == ""]
        try:
            study.set_column_role(name, change["role"], type=change.get("type"), levels=change.get("levels"),
                                  units=change.get("units") or None, description=change.get("description") or None)
        except (KeyError, ValueError) as e:
            result["errors"][name] = str(e)
            continue
        entry = study.dictionary.get(name) or {}
        for key in clear:  # the data dictionary has no way to say "no units": drop the key
            entry.pop({"units": "Units", "description": "Description"}[key], None)
        if "levels" in change and change["levels"] is None:
            entry.pop("Levels", None)
        study.dictionary[name] = entry
        result["applied"].append(name)
    if result["applied"]:
        _commit(session)
    return result


#
# The dialog
#


class ColumnsDialog:
    """The table of the columns of the study (objectName StudioColumnsDialog): name, type, role, units, levels and description."""

    def __init__(self, session, parent=None):
        self.session = session
        self.original = {}
        dialog = qt.QDialog(parent or slicer.util.mainWindow())
        dialog.setObjectName("StudioColumnsDialog")
        dialog.setWindowTitle(_("Columns of {study}").format(study=session.name))
        dialog.resize(900, 520)
        dialog.setMinimumSize(640, 320)
        layout = qt.QVBoxLayout(dialog)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        intro = library._cardLabel("StudioColumnsIntro", 12, "text2")
        intro.setText(_("Say what each column is. Analyses use the roles you confirm here, never \"every numeric column\": a column is a score only when you say so, "
                        "and columns the application writes while it works (bookkeeping) are never offered as scores."))
        layout.addWidget(intro)
        filterRow = qt.QHBoxLayout()
        self.onlyUnset = qt.QCheckBox(_("Only columns without a role"))
        self.onlyUnset.setObjectName("StudioColumnsOnlyUnset")
        self.onlyUnset.connect("toggled(bool)", lambda checked: self._applyFilter())
        filterRow.addWidget(self.onlyUnset)
        filterRow.addStretch(1)
        self.countLabel = qt.QLabel()
        self.countLabel.setObjectName("StudioColumnsCount")
        library._setFont(self.countLabel, 12)
        ui.bindStyleSheet(self.countLabel, lambda: f"QLabel#StudioColumnsCount {{ color: {ui.token('text2').name()}; }}")
        filterRow.addWidget(self.countLabel)
        layout.addLayout(filterRow)
        self.table = qt.QTableWidget(0, 6)
        self.table.setObjectName("StudioColumnsTable")
        self.table.setHorizontalHeaderLabels([_("Column"), _("Type"), _("Role"), _("Units"), _("Levels"), _("Description")])
        self.table.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(qt.QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(qt.QHeaderView.Interactive)
        header.setStretchLastSection(True)
        self.table.setAccessibleName(_("Columns of the study and their roles"))
        layout.addWidget(self.table, 1)
        self.noteLabel = library._cardLabel("StudioColumnsNote", 12, "warning")
        layout.addWidget(self.noteLabel)
        self.helpLabel = library._cardLabel("StudioColumnsRoleHelp", 12, "text2")
        self.helpLabel.hide()
        layout.addWidget(self.helpLabel)
        buttons = qt.QHBoxLayout()
        buttons.addStretch(1)
        self.cancelButton = qt.QPushButton(_("Cancel"))
        self.cancelButton.setObjectName("StudioColumnsCancelButton")
        ui.setRole(self.cancelButton, "secondary")
        self.cancelButton.connect("clicked()", dialog.reject)
        buttons.addWidget(self.cancelButton)
        self.confirmButton = qt.QPushButton(_("Confirm"))
        self.confirmButton.setObjectName("StudioColumnsConfirmButton")
        ui.setRole(self.confirmButton, "primary")
        self.confirmButton.setAutoDefault(False)
        self.confirmButton.setToolTip(_("Save the roles you changed in participants.json (the study folder), next to the table"))
        self.confirmButton.connect("clicked()", self.confirm)
        buttons.addWidget(self.confirmButton)
        layout.addLayout(buttons)
        self.widget = dialog
        self.table.connect("currentCellChanged(int,int,int,int)", lambda row, column, previousRow, previousColumn: self._showHelp(row))
        self.fill()

    # Content

    def _combo(self, objectName, items, current):
        combo = qt.QComboBox()
        combo.setObjectName(objectName)
        for text, data in items:
            combo.addItem(text, data)
        index = combo.findData(current)
        combo.setCurrentIndex(max(0, index))
        return combo

    def fill(self):
        """(Re)build the rows from the open study."""
        schema = _lnm("schema")
        study = self.session.study
        self.original = {}
        self.table.setRowCount(0)
        for name in study.columns:
            info = columnInfo(study, name)
            spec = info["spec"]
            row = self.table.rowCount
            self.table.insertRow(row)
            nameItem = qt.QTableWidgetItem(name)
            _readOnly(nameItem)
            tip = _("A standard column of every study: its role is fixed") if info["standard"] else (
                _("A registered column: its role is the registry's until you confirm another") if info["registered"] and not info["confirmed"] else "")
            nameItem.setToolTip(tip)
            self.table.setItem(row, COL_NAME, nameItem)
            typeCombo = self._combo(f"StudioColumnsType_{name}", [(typeName, typeName) for typeName in schema.TYPES], spec.type)
            typeCombo.setEnabled(not info["standard"])
            typeCombo.setAccessibleName(_("Type of {name}").format(name=name))
            self.table.setCellWidget(row, COL_TYPE, typeCombo)
            roles = [(roleLabel(NOT_SET), NOT_SET)] if spec.role == schema.UNKNOWN_ROLE else []
            roles += [(roleLabel(role), role) for role in schema.ROLES]
            roleCombo = self._combo(f"StudioColumnsRole_{name}", roles, spec.role)
            roleCombo.setEnabled(not info["standard"])
            roleCombo.setToolTip(tip or roleHelp(spec.role))
            roleCombo.setAccessibleName(_("Role of {name}").format(name=name))
            roleCombo.connect("currentIndexChanged(int)", lambda index, row=row: self._showHelp(row))
            self.table.setCellWidget(row, COL_ROLE, roleCombo)
            units = spec.units or ""
            levels = levelsText(spec.levels)
            description = spec.description or ""
            for column, text in ((COL_UNITS, units), (COL_LEVELS, levels), (COL_DESCRIPTION, description)):
                item = qt.QTableWidgetItem(text)
                if info["standard"] and column != COL_DESCRIPTION:
                    _readOnly(item)
                elif column == COL_UNITS and info["registered"] and schema.registered(name).units:
                    _readOnly(item)  # the registry's units show through: they cannot be removed
                    item.setToolTip(_("The units of a registered column are the registry's"))
                self.table.setItem(row, column, item)
            self.original[name] = {"role": spec.role, "type": spec.type, "units": units, "levels": levels, "description": description, "standard": info["standard"]}
        self.table.resizeColumnToContents(COL_NAME)
        for column, width in ((COL_TYPE, 110), (COL_ROLE, 150), (COL_UNITS, 70), (COL_LEVELS, 200)):
            self.table.setColumnWidth(column, width)
        self.table.setColumnWidth(COL_NAME, max(140, self.table.columnWidth(COL_NAME) + 12))
        self._applyFilter()
        self._setNote("")

    def rowOf(self, name):
        for row in range(self.table.rowCount):
            if self.table.item(row, COL_NAME).text() == name:
                return row
        return -1

    def roleCombo(self, name):
        return self.table.cellWidget(self.rowOf(name), COL_ROLE)

    def typeCombo(self, name):
        return self.table.cellWidget(self.rowOf(name), COL_TYPE)

    def setRole(self, name, role):
        combo = self.roleCombo(name)
        index = combo.findData(role)
        if index < 0:
            raise ValueError(f"No role {role!r}")
        combo.setCurrentIndex(index)

    def setText(self, name, column, text):
        self.table.item(self.rowOf(name), column).setText(text)

    def _applyFilter(self):
        only = self.onlyUnset.isChecked()
        shown = 0
        for row in range(self.table.rowCount):
            name = self.table.item(row, COL_NAME).text()
            hide = only and self.original[name]["role"] != NOT_SET
            self.table.setRowHidden(row, hide)
            shown += 0 if hide else 1
        unset = sum(1 for values in self.original.values() if values["role"] == NOT_SET)
        self.countLabel.setText((_("1 column · {unset} without a role") if len(self.original) == 1 else _("{n} columns · {unset} without a role")).format(n=len(self.original), unset=unset))

    def _setNote(self, text):
        self.noteLabel.setText(text)
        self.noteLabel.setVisible(bool(text))

    def _showHelp(self, row):
        text = ""
        if 0 <= row < self.table.rowCount:
            name = self.table.item(row, COL_NAME).text()
            combo = self.table.cellWidget(row, COL_ROLE)
            role = combo.itemData(combo.currentIndex)
            text = f"{name}: {roleLabel(role)}. {roleHelp(role)}"
        self.helpLabel.setText(text)
        self.helpLabel.setVisible(bool(text))

    # Changes

    def changes(self):
        """``{column: change}`` of the rows the user edited (a row needs a role to be confirmed), and ``{column: message}`` of the rows that cannot be."""
        found, problems = {}, {}
        for row in range(self.table.rowCount):
            name = self.table.item(row, COL_NAME).text()
            before = self.original[name]
            roleCombo = self.table.cellWidget(row, COL_ROLE)
            typeCombo = self.table.cellWidget(row, COL_TYPE)
            role = roleCombo.itemData(roleCombo.currentIndex)
            typeName = typeCombo.itemData(typeCombo.currentIndex)
            units = self.table.item(row, COL_UNITS).text().strip()
            levels = self.table.item(row, COL_LEVELS).text().strip()
            description = self.table.item(row, COL_DESCRIPTION).text().strip()
            edited = (role != before["role"] or typeName != before["type"] or units != before["units"]
                      or levels != before["levels"] or description != before["description"])
            if not edited:
                continue
            if role == NOT_SET:
                problems[name] = _("Choose a role for {name} to keep its other changes.").format(name=name)
                continue
            change = {"role": role}
            if typeName != before["type"] and not before["standard"]:
                change["type"] = typeName
            if units != before["units"] and not before["standard"]:
                change["units"] = units
            if levels != before["levels"] and not before["standard"]:
                change["levels"] = parseLevels(levels)
            if description != before["description"]:
                change["description"] = description
            found[name] = change
        return found, problems

    def confirm(self):
        """Confirm: write the changed rows (a bookkeeping or metadata column becomes a score only after the question). Return the result of :func:`applyChanges`."""
        changes, problems = self.changes()
        if not changes:
            self._setNote(" ".join(problems.values()) or _("Nothing changed."))
            return {"applied": [], "declined": [], "errors": problems}
        result = applyChanges(self.session, changes, confirm=lambda names, reasons: confirmScore(names, reasons, self.widget))
        notes = list(problems.values()) + [f"{name}: {message}" for name, message in result["errors"].items()]
        for name in result["declined"]:
            self.setRole(name, self.original[name]["role"])  # the answer was "Keep as is"
        if result["applied"]:
            _toast(_("Roles saved for {n} columns").format(n=len(result["applied"])) if len(result["applied"]) != 1 else _("Role saved for {name}").format(name=result["applied"][0]))
            self.fill()
        self._setNote(" ".join(notes))
        result["errors"].update(problems)
        if result["applied"] and not notes and not result["declined"]:
            self.widget.accept()
        return result

    def show(self):
        self.widget.show()
        self.widget.raise_()
        self.widget.activateWindow()
        return self.widget


_dialogs = []  # open dialogs (kept alive)


def showColumns(session=None):
    """Open the columns table of the open study (objectName StudioColumnsDialog). Return its :class:`ColumnsDialog`, or None without a study."""
    session = session or _session()
    if session is None:
        _toast(_("Open a study first"))
        return None
    for old in list(_dialogs):
        _dialogs.remove(old)
        if library._isAlive(old.widget):
            old.widget.close()
            library._discard(old.widget)
    dialog = ColumnsDialog(session)
    _dialogs.append(dialog)
    dialog.show()
    return dialog


#
# The card
#


class ColumnsCard:
    """The 'Columns' card of the STUDY section."""

    def __init__(self, session):
        self.session = session
        self.widget = qt.QWidget()
        self.widget.setObjectName("StudioColumnsCard")
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(4)
        top = qt.QHBoxLayout()
        top.setSpacing(8)
        title = qt.QLabel(_("Columns"))
        title.setObjectName("StudioColumnsTitle")
        title.setProperty("slicerRole", "sectionHeader")
        library._setFont(title, 12, qt.QFont.DemiBold)
        top.addWidget(title)
        top.addStretch(1)
        self.openButton = library._linkButton(_("Columns…"), "StudioColumnsButton", self.open)
        self.openButton.setToolTip(ui.tooltip(_("Columns"), None, _("Say which columns are scores, covariates, groups or bookkeeping")))
        top.addWidget(self.openButton)
        layout.addLayout(top)
        self.line = library._cardLabel("StudioColumnsLine", 12, "text")
        layout.addWidget(self.line)
        self.warning = library._cardLabel("StudioColumnsWarning", 12, "warning")
        layout.addWidget(self.warning)

    def refresh(self, selectedIds=None):
        session = _session()
        if session is None:
            return
        self.session = session
        numbers = columnsSummary(session.study)
        unset = numbers["unset"]
        self.line.setText((_("1 column · {unset} without a confirmed role") if numbers["columns"] == 1 else _("{n} columns · {unset} without a confirmed role")).format(n=numbers["columns"], unset=len(unset)) if unset else
                          (_("1 column · its role is set") if numbers["columns"] == 1 else _("{n} columns · every role is set")).format(n=numbers["columns"]))
        guessed = numbers["guessed"]
        if guessed:
            shown = ", ".join(guessed[:3]) + (f" +{len(guessed) - 3}" if len(guessed) > 3 else "")
            self.warning.setText(_("Offered as an unconfirmed score: {names}. Confirm what they are in Columns…").format(names=shown))
        self.warning.setVisible(bool(guessed))

    def open(self):
        return showColumns(self.session)


_cards = {}  # widget: ColumnsCard


def _createCard(session):
    card = ColumnsCard(session)
    _cards[card.widget] = card
    card.widget.connect("destroyed()", lambda: _cards.pop(card.widget, None))
    card.refresh()
    return card.widget


def _refreshCard(widget, session, selectedIds):
    card = _cards.get(widget)
    if card is not None and library._isAlive(widget):
        card.refresh(selectedIds)


#
# The Case section
#


def doiOf(text):
    """The bare DOI ("10.1000/xyz") in a text such as "doi:10.1000/xyz" or "https://doi.org/10.1000/xyz", or None."""
    match = DOI_PATTERN.search(str(text or "").strip())
    return match.group(0).rstrip(".,;)") if match else None


def doiUrl(text):
    """``https://doi.org/<doi>`` of a DOI text, or None when it holds no DOI."""
    doi = doiOf(text)
    return "https://doi.org/" + quote(doi, safe="/:()-._~;<>[]@!$&'*+,=") if doi else None


def openExternalUrl(url):
    """Open a web address in the browser, or show the link to copy in the web build (tests replace this function)."""
    from . import projects

    return projects.openLink(url)


def parseSourceReference(text):
    """Columns for the interim source reference of Draw ("doi:10.1000/xyz, Fig. 2"): ``{"doi", "figure"}``.

    When anything else is left once the DOI, its "doi:" or web address prefix and the figure are taken out ("Smith et al. 2020, Fig. 3"),
    the full original text is also kept in ``"citation"``, so that nothing the user typed is lost when the interim field is removed.
    """
    text = str(text or "").strip()
    if not text:
        return {}
    values = {}
    rest = text
    doi = doiOf(text)
    if doi:
        values["doi"] = doi
        rest = DOI_WITH_PREFIX_PATTERN.sub(" ", rest, count=1)
    figure = FIGURE_PATTERN.search(text)
    if figure:
        values["figure"] = figure.group(1)
        rest = FIGURE_PATTERN.sub(" ", rest, count=1)
    if not values or re.search(r"[^\W_]", rest):
        values["citation"] = text
    return values


def _missing(value):
    return value is None or value == "" or value == "n/a"


def levelLabel(level, description):
    """The text of a level in a combo: the description ("Male"), with the level first for numbers ("2 · Adequate")."""
    if not description or description == level:
        return str(level)
    return f"{level} · {description}" if str(level).isdigit() else str(description)


class CaseSection:
    """The 'Case' section of the SUBJECT card (SUBJECT_SECTION): the registry fields of the open subject, edited in place."""

    def __init__(self, session):
        self.session = session
        self.sid = None
        self._loading = False
        self.editors = {}  # column: widget
        self.rows = {}  # column: (label, field widget) of the form, hidden for a blinded column
        self.groups = {}  # key: (button, container, columns)
        self.widget = qt.QWidget()
        self.widget.setObjectName("StudioCaseSection")
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(4)
        top = qt.QHBoxLayout()
        title = qt.QLabel(_("Case"))
        title.setObjectName("StudioCaseTitle")
        title.setProperty("slicerRole", "sectionHeader")
        library._setFont(title, 12, qt.QFont.DemiBold)
        top.addWidget(title)
        top.addStretch(1)
        self.numberLabel = library._cardLabel("StudioCaseNumber", 11, "text2")
        self.numberLabel.setSizePolicy(qt.QSizePolicy.Preferred, qt.QSizePolicy.Preferred)
        top.addWidget(self.numberLabel)
        layout.addLayout(top)
        self.interimFrame = qt.QFrame()
        self.interimFrame.setObjectName("StudioCaseInterim")
        interimLayout = qt.QVBoxLayout(self.interimFrame)
        interimLayout.setContentsMargins(0, 0, 0, 0)
        interimLayout.setSpacing(2)
        self.interimLabel = library._cardLabel("StudioCaseInterimLabel", 12, "text")
        interimLayout.addWidget(self.interimLabel)
        self.interimButton = library._linkButton(_("Use as notes and source"), "StudioCaseInterimUse", self.useInterim)
        interimLayout.addWidget(self.interimButton, 0, qt.Qt.AlignLeft)
        layout.addWidget(self.interimFrame)
        for key, title, columns in CASE_GROUPS:
            self._addGroup(layout, key, title, columns)
        self.warningLabel = library._cardLabel("StudioCaseWarning", 12, "warning")
        layout.addWidget(self.warningLabel)
        self.factsLabel = library._cardLabel("StudioCaseFacts", 12, "text2")
        layout.addWidget(self.factsLabel)

    # Building

    def _addGroup(self, layout, key, title, columns):
        schema = _lnm("schema")
        button = qt.QToolButton()
        button.setObjectName(f"StudioCaseGroup_{key}")
        button.setCheckable(True)
        button.setChecked(_expanded.get(key, False))
        button.setToolButtonStyle(qt.Qt.ToolButtonTextBesideIcon)
        button.setArrowType(qt.Qt.DownArrow if button.isChecked() else qt.Qt.RightArrow)
        button.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Fixed)
        objectName = button.objectName
        ui.bindStyleSheet(button, lambda: f"QToolButton#{objectName} {{ color: {ui.token('text').name()}; text-align: left; border: none; padding: 2px 0px; }}")
        button.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
        library._setFont(button, 12, qt.QFont.DemiBold)
        layout.addWidget(button)
        container = qt.QWidget()
        container.setObjectName(f"StudioCaseFields_{key}")
        form = qt.QFormLayout(container)
        form.setContentsMargins(16, 0, 0, 4)
        form.setSpacing(4)
        form.setFieldGrowthPolicy(qt.QFormLayout.AllNonFixedFieldsGrow)
        form.setLabelAlignment(qt.Qt.AlignLeft)
        for column in columns:
            spec = schema.registered(column)
            if spec is None:
                continue
            label = qt.QLabel(_(FIELD_LABELS.get(column, column)))
            label.setObjectName(f"StudioCaseLabel_{column}")
            library._setFont(label, 12)
            label.setFixedWidth(ui.fontPx(112))
            tooltip = (spec.description or "") + (f" ({spec.units})" if spec.units else "")
            label.setToolTip(tooltip)
            editor = self._editorFor(column, spec)
            editor.setToolTip(tooltip)
            self.editors[column] = editor
            if column == "doi":
                row = qt.QWidget()
                rowLayout = qt.QHBoxLayout(row)
                rowLayout.setContentsMargins(0, 0, 0, 0)
                rowLayout.setSpacing(4)
                rowLayout.addWidget(editor, 1)
                self.doiButton = library._linkButton(_("Open"), "StudioCaseDoiOpen", self.openDoi)
                self.doiButton.setToolTip(_("Open https://doi.org/<doi> in the browser"))
                rowLayout.addWidget(self.doiButton)
                form.addRow(label, row)
                self.rows[column] = (label, row)
            else:
                form.addRow(label, editor)
                self.rows[column] = (label, editor)
        container.setVisible(button.isChecked())
        layout.addWidget(container)
        self.groups[key] = (button, container, columns)
        button.connect("toggled(bool)", lambda checked, key=key: self._toggle(key, checked))

    def _editorFor(self, column, spec):
        if spec.levels:
            combo = qt.QComboBox()
            combo.setObjectName(f"StudioCaseEdit_{column}")
            combo.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
            combo.addItem("—", None)
            for level, description in spec.levels.items():
                combo.addItem(levelLabel(level, description), level)
            combo.setProperty("baseCount", combo.count)
            if not spec.strict:
                combo.setEditable(True)
                combo.lineEdit().setPlaceholderText("—")
                combo.lineEdit().connect("editingFinished()", lambda column=column, combo=combo: self._commitCombo(column, combo, typed=True))
            combo.connect("activated(int)", lambda index, column=column, combo=combo: self._commitCombo(column, combo))
            return combo
        edit = qt.QLineEdit()
        edit.setObjectName(f"StudioCaseEdit_{column}")
        edit.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        edit.setPlaceholderText(f"— ({spec.units})" if spec.units else "—")
        edit.connect("editingFinished()", lambda column=column, edit=edit: self._commitLine(column, edit))
        if column == "doi":
            edit.connect("textChanged(QString)", lambda text: self._updateDoiButton())
        return edit

    def _toggle(self, key, checked):
        button, container, _columns = self.groups[key]
        _expanded[key] = bool(checked)
        container.setVisible(bool(checked))
        button.setArrowType(qt.Qt.DownArrow if checked else qt.Qt.RightArrow)

    # Content

    def refresh(self, sid):
        session = _session()
        self.sid = sid
        if session is None or sid is None or sid not in session.study:
            self.widget.hide()
            return
        self.session = session
        self.widget.show()
        study = session.study
        row = study.subject(sid)
        # Blind tracing (L-43): the fields of blinded columns (symptom, domain) are not shown
        hidden = library._hiddenColumns(study)
        self._loading = True
        try:
            warnings = []
            for column, editor in self.editors.items():
                label, field = self.rows.get(column, (None, editor))
                for widget in (label, field):
                    if widget is not None and widget.isHidden() != (column in hidden):
                        widget.setHidden(column in hidden)
                if column in hidden:
                    self._show(column, editor, None)  # no value of the subject stays in a hidden editor
                    continue
                value = row.get(column)
                self._show(column, editor, value)
                warning = self._warningOf(column, value)
                if warning:
                    warnings.append(warning)
            for key, (button, _container, columns) in self.groups.items():
                title = next(title for groupKey, title, _columns in CASE_GROUPS if groupKey == key)
                shown = [column for column in columns if column in self.editors and column not in hidden]
                filled = sum(1 for column in shown if not _missing(row.get(column)))
                button.setText(f"{_(title)}  ·  {filled}/{len(shown)}")
        finally:
            self._loading = False
        self.warningLabel.setText("  ".join(warnings))
        self.warningLabel.setVisible(bool(warnings))
        facts = []
        for column, label in CASE_FACTS:
            value = row.get(column)
            if not _missing(value) and value != "none":
                facts.append(f"{_(label)}: {value}")
        self.factsLabel.setText(" · ".join(facts))
        self.factsLabel.setVisible(bool(facts))
        number = self._caseNumber(study, sid)
        self.numberLabel.setText(_("Case {n} in the figures").format(n=number) if number else "")
        self.numberLabel.setVisible(bool(number))
        self._updateInterim(study, sid, row)
        self._updateDoiButton()

    def _show(self, column, editor, value):
        text = "" if _missing(value) else str(value)
        if hasattr(editor, "itemData"):
            if editor.hasFocus() or (editor.isEditable() and editor.lineEdit().hasFocus()):
                return
            base = editor.property("baseCount") or editor.count
            while editor.count > base:
                editor.removeItem(editor.count - 1)
            index = editor.findData(text) if text else 0
            if index < 0:
                if editor.isEditable():
                    editor.setCurrentIndex(0)
                    editor.setEditText(text)
                    return
                editor.addItem(_("{value} (not a valid level)").format(value=text), text)
                index = editor.count - 1
            editor.setCurrentIndex(index)
            return
        if editor.hasFocus():
            return
        if str(editor.text) != text:
            editor.setText(text)

    def _warningOf(self, column, value):
        if _missing(value):
            return ""
        schema = _lnm("schema")
        spec = schema.registered(column)
        if spec is None or spec.strict:
            return ""
        try:
            _text, warning = schema.normalize_with_spec(spec, str(value))
        except ValueError as e:
            return str(e)
        return warning or ""

    def _caseNumber(self, study, sid):
        order = recordedGallery(study)
        return order.index(sid) + 1 if sid in order else None

    # Interim notes and source reference of Draw

    def _interim(self, study, sid, row):
        """``{"notes", "reference"}`` of the sidecar that no column holds yet."""
        try:
            sidecar = study.read_lesion_sidecar(sid)
        except Exception:
            return {}
        if not sidecar:
            return {}
        found = {}
        notes = str(sidecar.get("Notes") or "").strip()
        if notes and _missing(row.get("notes")):
            found["notes"] = notes
        reference = str(sidecar.get("SourceReference") or "").strip()
        if reference and all(_missing(row.get(column)) for column in ("doi", "figure", "citation")):
            found["reference"] = reference
        return found

    def _updateInterim(self, study, sid, row):
        found = self._interim(study, sid, row)
        self.interim = found
        if not found:
            self.interimFrame.hide()
            return
        parts = []
        if "notes" in found:
            parts.append(_("Notes: {text}").format(text=found["notes"]))
        if "reference" in found:
            parts.append(_("Source: {text}").format(text=found["reference"]))
        self.interimLabel.setText(_("Typed in Draw:") + " " + " · ".join(parts))
        self.interimFrame.show()

    def useInterim(self):
        """Move the interim notes and source reference into the columns and remove them from the sidecar and the open segmentation."""
        session = _session()
        if session is None or self.sid is None:
            return {}
        study, sid = session.study, self.sid
        found = self._interim(study, sid, study.subject(sid))
        values = {}
        if "notes" in found:
            values["notes"] = found["notes"]
        if "reference" in found:
            values.update(parseSourceReference(found["reference"]))
        if not values:
            return {}
        session.setValues([sid], values)
        self._clearInterim(session, sid, found)
        return values

    def _clearInterim(self, session, sid, found):
        study = session.study
        try:
            path = study.lesion_sidecar_path(sid)
            sidecar = study.read_lesion_sidecar(sid)
            if sidecar is not None:
                for key, kind in (("Notes", "notes"), ("SourceReference", "reference")):
                    if kind in found:
                        sidecar.pop(key, None)
                _lnm("schema").write_text_atomic(path, json.dumps(sidecar, indent=2, ensure_ascii=False) + "\n")
        except Exception:
            logging.warning(f"Studio Library: the interim notes of {sid} could not be removed from its sidecar:\n{traceback.format_exc()}")
        try:
            if session.current() == sid:
                draw = library._draw()
                node = session.segmentationNode()
                if node is not None and draw is not None:
                    if "notes" in found:
                        node.SetAttribute(getattr(draw, "NOTES_ATTRIBUTE", "Studio.LesionNotes"), "")
                    if "reference" in found:
                        node.SetAttribute(getattr(draw, "SOURCE_REFERENCE_ATTRIBUTE", "Studio.LesionSourceReference"), "")
        except Exception:
            logging.warning(f"Studio Library: the interim notes of {sid} could not be cleared in Draw:\n{traceback.format_exc()}")

    # Editing

    def _write(self, column, text):
        session = _session()
        if session is None or self.sid is None or self.sid not in session.study or self._loading:
            return False
        if column in library._hiddenColumns(session.study):
            return False  # a blinded field is not shown, so it is not written either
        current = session.study.subject(self.sid).get(column)
        text = str(text).strip()
        if column == "doi" and doiOf(text):
            text = doiOf(text)  # "doi:10.1000/x" and the doi.org address are stored as the bare DOI
        if text == ("" if _missing(current) else str(current)):
            return False
        try:
            session.setValues([self.sid], {column: text or None})
        except ValueError as e:
            _toast(str(e), 5000, level="warning")
            self.refresh(self.sid)
            return False
        return True

    def _commitLine(self, column, edit):
        self._write(column, str(edit.text))

    def _commitCombo(self, column, combo, typed=False):
        if self._loading:
            return
        index = combo.currentIndex
        if typed and combo.isEditable():
            text = str(combo.currentText)
            match = combo.findText(text)
            data = combo.itemData(match) if match >= 0 else text
        else:
            data = combo.itemData(index)
        self._write(column, "" if data is None else str(data))

    def setField(self, column, text):
        """Set a field as the user would (tests and Search): the editor shows ``text`` and the value is written."""
        editor = self.editors[column]
        if hasattr(editor, "itemData"):
            index = editor.findData(text) if text else 0
            if index >= 0:
                editor.setCurrentIndex(index)
            elif editor.isEditable():
                editor.setEditText(text)
            return self._write(column, text)
        editor.setText(text)
        return self._write(column, text)

    def _updateDoiButton(self):
        editor = self.editors.get("doi")
        if editor is None or not hasattr(self, "doiButton"):
            return
        self.doiButton.setEnabled(doiUrl(str(editor.text)) is not None)

    def openDoi(self):
        """Open the DOI of the subject in the browser. Return the address, or None."""
        editor = self.editors.get("doi")
        url = doiUrl(str(editor.text)) if editor is not None else None
        if url is None:
            return None
        openExternalUrl(url)
        return url


def recordedGallery(study):
    """Participant ids of the gallery recorded in ``derivatives/lnm/lesionset.json`` by the Lesion overview ([] without one)."""
    import os

    path = os.path.join(study.path, "derivatives", "lnm", "lesionset.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    gallery = data.get("gallery") if isinstance(data, dict) else None
    order = gallery.get("sids") if isinstance(gallery, dict) else gallery
    return [sid for sid in order if isinstance(sid, str)] if isinstance(order, list) else []


_sections = {}  # widget: CaseSection


def _createSection(session):
    section = CaseSection(session)
    _sections[section.widget] = section
    section.widget.connect("destroyed()", lambda: _sections.pop(section.widget, None))
    return section.widget


def _refreshSection(widget, session, sid):
    section = _sections.get(widget)
    if section is not None and library._isAlive(widget):
        section.refresh(sid)


#
# Library extension points
#

CARD = {"section": "study", "order": CARD_ORDER, "create": _createCard, "refresh": _refreshCard}
SUBJECT_SECTION = {"order": SECTION_ORDER, "create": _createSection, "refresh": _refreshSection}


def _paletteEnabled():
    return _session() is not None


#: Search entries (dict form of the provider contract of StudioLib.palette)
PALETTE_ACTIONS = [
    {"id": "study.columns", "title": "Columns…", "category": "Library", "synonyms": ["roles", "score column", "covariate", "variables", "data dictionary", "participants.json", "bookkeeping"],
     "callback": showColumns, "enabled": _paletteEnabled, "description": "Say which columns of the study are scores, covariates, groups or bookkeeping"},
]

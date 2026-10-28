"""Library extension: literature "Sources" and PRISMA counts (L-51, LNM addendum 7.2 and 7.4, RW-12).

The STUDY card **Sources** keeps the records a case-report search screened (``sources/records.tsv``,
``LesionNetworkMappingLib.sources``), so that the PRISMA counts and flow diagram come for free and every case is tied to its
paper:

- **Table** of the records (record, title, year, stage, cases). Keys act on the table only (no window-wide shortcut, so
  they never reach the views): **P** include / advance one stage (identified, screened, full text, included), **X** exclude
  with a reason (a small popover; Enter confirms, Esc closes it), **U** undo the last stage change, **Enter** open the
  record's attachments folder (``sources/<record_id>/``, never exported), **/** filter the table (Esc clears it).
- **Import…** (inline form): a RIS, PubMed (nbib / MEDLINE), BibTeX or CSV file, or pasted DOIs and PMIDs, with the database
  and the query for the search log (``study.json`` ``search``). Exact DOI / PMID duplicates are merged; records with
  near-identical titles are offered for merging (Merge / Keep both).
- **Fetch metadata** (a click): title, authors, year and journal of the selected record from Crossref (DOI) or PubMed (PMID);
  only the identifier is sent, and nothing is replaced. Nothing is fetched without this click.
- **Add cases…**: ``n`` new subjects ``sub-<record_id><nn>`` that carry the record's id, DOI, PMID and citation.
- **Open folder** and **PRISMA export** (``derivatives/prisma/prisma_counts.json`` and ``prisma_flow.svg``).

Works in Classic (the Studio module panel hosts the same cards).
"""

import contextlib
import logging
import os
import traceback

import qt
from slicer.i18n import tr as _

from . import library, ui

CARD_ORDER = 45
#: Rows of the table before it scrolls
TABLE_ROWS = 6
STAGE_LABELS = (("identified", "Identified"), ("screened", "Screened"), ("fulltext", "Full text"), ("included", "Included"), ("excluded", "Excluded"))
STAGE_LEVELS = {"identified": "text2", "screened": "text", "fulltext": "text", "included": "success", "excluded": "danger"}
COMMON_REASONS = ("Not a case report", "No usable lesion imaging", "Lesion cannot be localised", "Not the studied symptom", "Duplicate patient report", "No full text")
#: Keys of the table (scoped to it): (key, description), for the shortcut list
TABLE_KEYS = (
    ("P", "Sources table: include or advance one stage"),
    ("X", "Sources table: exclude with a reason"),
    ("U", "Sources table: undo the last stage change"),
    ("Return", "Sources table: open the record's attachments folder"),
    ("/", "Sources table: filter"),
)
FILE_FILTER = "Reference files (*.ris *.nbib *.bib *.bibtex *.txt *.csv *.tsv *.medline);;All files (*)"


def _projects():
    from . import projects

    return projects


def _sources():
    import importlib

    return importlib.import_module("LesionNetworkMappingLib.sources")


def _session():
    studyUi = library._studyUi()
    return studyUi.session() if studyUi is not None else None


def _toast(text, ms=3000, **kwargs):
    shell = library._shell()
    if shell is not None:
        shell.toast(text, ms, **kwargs)
    else:
        logging.info(f"Studio sources: {text}")


def stageText(stage):
    return _(dict(STAGE_LABELS).get(stage, stage))


def summaryText(records, counts):
    """"12 records · 3 included · 2 excluded · 7 cases" for the card's summary line."""
    stages = records.stage_counts()
    parts = [(_("{n} record") if len(records) == 1 else _("{n} records")).format(n=len(records))]
    if records.rows:
        parts.append(_("{n} to screen").format(n=stages["identified"]))
    if stages["included"]:
        parts.append(_("{n} included").format(n=stages["included"]))
    if stages["excluded"]:
        parts.append(_("{n} excluded").format(n=stages["excluded"]))
    if counts["cases"]:
        parts.append((_("{n} case") if counts["cases"] == 1 else _("{n} cases")).format(n=counts["cases"]))
    return " · ".join(parts)


class _ReasonPopover:
    """The reason of an exclusion: a small popover under the table (Enter confirms, Esc closes it, nothing is modal)."""

    def __init__(self, card):
        self.card = card
        self.recordId = None
        popover = qt.QFrame(card.widget, qt.Qt.Popup)
        popover.setObjectName("StudioSourcesReasonPopover")
        popover.setFrameShape(qt.QFrame.StyledPanel)
        popover.setMinimumWidth(300)
        layout = qt.QVBoxLayout(popover)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        self.title = qt.QLabel()
        self.title.setObjectName("StudioSourcesReasonTitle")
        self.title.setWordWrap(True)
        layout.addWidget(self.title)
        self.reason = qt.QComboBox()
        self.reason.setObjectName("StudioSourcesReason")
        self.reason.setEditable(True)
        self.reason.setInsertPolicy(qt.QComboBox.NoInsert)
        self.reason.lineEdit().setPlaceholderText(_("Reason for excluding"))
        self.reason.lineEdit().connect("returnPressed()", self.confirm)
        layout.addWidget(self.reason)
        row = qt.QHBoxLayout()
        row.setSpacing(6)
        row.addStretch(1)
        cancel = qt.QPushButton(_("Cancel"))
        cancel.setObjectName("StudioSourcesReasonCancel")
        cancel.setAutoDefault(False)
        cancel.connect("clicked()", self.widget_close)
        row.addWidget(cancel)
        self.okButton = qt.QPushButton(_("Exclude"))
        self.okButton.setObjectName("StudioSourcesReasonOk")
        self.okButton.setAutoDefault(False)
        ui.setRole(self.okButton, "primary")
        self.okButton.connect("clicked()", self.confirm)
        row.addWidget(self.okButton)
        layout.addLayout(row)
        self.widget = popover

    def widget_close(self):
        self.widget.hide()

    def open(self, recordId, reasons):
        self.recordId = recordId
        self.title.setText(_("Why is {record} excluded?").format(record=recordId))
        self.reason.clear()
        for reason in reasons:
            self.reason.addItem(reason)
        self.reason.setCurrentIndex(-1)
        self.reason.setEditText("")
        popover = self.widget
        popover.adjustSize()
        table = self.card.table
        popover.move(table.mapToGlobal(qt.QPoint(0, table.height)))
        popover.show()
        self.reason.setFocus()

    def confirm(self):
        text = str(self.reason.currentText).strip()
        if not text:
            return
        recordId = self.recordId
        self.widget.hide()
        self.card.exclude(recordId, text)


class SourcesCard:
    """The 'Sources' part of the STUDY card."""

    def __init__(self, session):
        self.session = session
        self.records = None
        self._studyPath = None
        self._signature = None
        self._offers = []
        self._offersAll = []
        self._offersKey = None
        self._dismissed = set()
        self._rows = []
        self.shortcuts = []
        self.widget = qt.QWidget()
        self.widget.setObjectName("StudioSourcesLibraryCard")
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(8)
        title = qt.QLabel(_("Sources"))
        title.setObjectName("StudioSourcesLibraryTitle")
        title.setProperty("slicerRole", "sectionHeader")
        library._setFont(title, 12, qt.QFont.DemiBold)
        layout.addWidget(title)
        self.summary = library._cardLabel("StudioSourcesSummary", 12, "text")
        layout.addWidget(self.summary)
        self.hint = library._cardLabel("StudioSourcesHint", 12, "text2")
        layout.addWidget(self.hint)

        self.filter = qt.QLineEdit()
        self.filter.setObjectName("StudioSourcesFilter")
        self.filter.setPlaceholderText(_("Filter the records (/)"))
        ui.setClearButton(self.filter)
        self.filter.setToolTip(_("Filter by record, title, author, DOI, PMID or stage. Esc clears the filter."))
        self.filter.connect("textChanged(QString)", self._applyFilter)
        library._installEventFilter(self.filter, self._escapeFilter())
        layout.addWidget(self.filter)

        self.table = qt.QTableWidget(0, 3)
        self.table.setObjectName("StudioSourcesTable")
        self.table.setHorizontalHeaderLabels([_("Record"), _("Title"), _("Stage")])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(qt.QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(qt.QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(False)
        self.table.setTextElideMode(qt.Qt.ElideRight)
        self.table.horizontalHeader().setStretchLastSection(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, qt.QHeaderView.Interactive)
        header.setSectionResizeMode(1, qt.QHeaderView.Stretch)
        header.setSectionResizeMode(2, qt.QHeaderView.ResizeToContents)
        header.setMinimumSectionSize(40)
        self.table.setColumnWidth(0, 104)
        # The title is what is being judged: it keeps room, and nothing scrolls sideways
        self.table.setHorizontalScrollBarPolicy(qt.Qt.ScrollBarAlwaysOff)
        self.table.setFocusPolicy(qt.Qt.StrongFocus)
        self.table.setMinimumHeight(TABLE_ROWS * 26 + 28)
        self.table.setToolTip(
            _("P: include or advance · X: exclude with a reason · U: undo · Enter: open the attachments folder · /: filter"),
        )
        self.table.connect("itemSelectionChanged()", self._updateButtons)
        self.table.connect("cellDoubleClicked(int,int)", lambda *args: self.openFolder())
        layout.addWidget(self.table)
        ui.bindStyleSheet(self.table, self._tableStyleSheet)
        self._addShortcut("P", self.advanceSelected, "SourcesAdvance", TABLE_KEYS[0][1])
        self._addShortcut("X", self.askExclude, "SourcesExclude", TABLE_KEYS[1][1])
        self._addShortcut("U", self.undoSelected, "SourcesUndo", TABLE_KEYS[2][1])
        self._addShortcut("Return", self.openFolder, "SourcesOpenFolder", TABLE_KEYS[3][1])
        self._addShortcut("Enter", self.openFolder, "SourcesOpenFolderEnter", TABLE_KEYS[3][1])
        self._addShortcut("/", self.focusFilter, "SourcesFilter", TABLE_KEYS[4][1])
        self.reasonPopover = _ReasonPopover(self)

        self.offerRow = qt.QWidget()
        self.offerRow.setObjectName("StudioSourcesOfferRow")
        offerLayout = qt.QVBoxLayout(self.offerRow)
        offerLayout.setContentsMargins(0, 0, 0, 0)
        offerLayout.setSpacing(4)
        self.offerLabel = library._cardLabel("StudioSourcesOffer", 12, "warning")
        offerLayout.addWidget(self.offerLabel)
        buttons = qt.QHBoxLayout()
        buttons.setSpacing(8)
        self.mergeButton = library._roleButton(_("Merge"), "StudioSourcesMergeButton", self.mergeOffer, "secondary")
        self.keepButton = library._roleButton(_("Keep both"), "StudioSourcesKeepButton", self.keepOffer, "secondary")
        for button in (self.mergeButton, self.keepButton):
            button.setMinimumHeight(28)
            buttons.addWidget(button)
        offerLayout.addLayout(buttons)
        layout.addWidget(self.offerRow)

        self.importRow, self.importButton = library._rowButton(_("Import…"), "StudioSourcesImportButton", self.showImport)
        self.importButton.setToolTip(_("Add records from a RIS, PubMed, BibTeX or CSV file, or from pasted DOIs and PMIDs"))
        layout.addWidget(self.importRow)
        self.importForm = self._buildImportForm()
        layout.addWidget(self.importForm)

        actions = library._buttonRow()
        self.fetchButton = library._roleButton(_("Fetch metadata"), "StudioSourcesFetchButton", self.fetchMetadata, "secondary")
        self.fetchButton.setToolTip(
            _("Look up title, authors, year and journal of the selected record online (Crossref or PubMed). Only its DOI or PMID is sent."),
        )
        self.addCasesButton = library._roleButton(_("Add cases…"), "StudioSourcesAddCasesButton", self.showAddCases, "secondary")
        self.addCasesButton.setToolTip(_("Add subjects that carry the selected record's id, DOI, PMID and citation"))
        actions.layout().addWidget(self.fetchButton)
        actions.layout().addWidget(self.addCasesButton)
        layout.addWidget(actions)
        self.addCasesForm = self._buildAddCasesForm()
        layout.addWidget(self.addCasesForm)
        more = library._buttonRow()
        self.folderButton = library._roleButton(_("Open folder"), "StudioSourcesFolderButton", self.openFolder, "secondary")
        self.folderButton.setToolTip(_("The selected record's attachments (figures, PDFs; never exported)"))
        self.prismaButton = library._roleButton(_("PRISMA export"), "StudioSourcesPrismaButton", self.exportPrisma, "secondary")
        self.prismaButton.setToolTip(_("Write derivatives/prisma/prisma_counts.json and prisma_flow.svg from the records"))
        more.layout().addWidget(self.folderButton)
        more.layout().addWidget(self.prismaButton)
        layout.addWidget(more)
        self.errorLabel = library._cardLabel("StudioSourcesError", 12, "danger")
        self.errorLabel.hide()
        layout.addWidget(self.errorLabel)
        self.offerRow.hide()

    # Construction helpers

    def _escapeFilter(self):
        card = self

        class _Escape(qt.QObject):
            def eventFilter(self, watched, event):
                if event.type() == qt.QEvent.KeyPress and event.key() == qt.Qt.Key_Escape:
                    if str(card.filter.text):
                        card.filter.clear()
                    card.table.setFocus()
                    return True
                return False

        self._escape = _Escape(self.filter)
        return self._escape

    def _addShortcut(self, key, callback, name, description):
        shortcut = qt.QShortcut(qt.QKeySequence(key), self.table)
        shortcut.setObjectName("Studio" + name + "Shortcut")
        # Scoped to the table: a window-wide key would reach the views
        shortcut.setContext(qt.Qt.WidgetWithChildrenShortcut)
        shortcut.setProperty("studioDescription", description)
        shortcut.connect("activated()", callback)
        self.shortcuts.append(shortcut)
        return shortcut

    def _buildImportForm(self):
        form = qt.QFrame()
        form.setObjectName("StudioSourcesImportForm")
        layout = qt.QFormLayout(form)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setSpacing(4)
        self.fileButton = qt.QPushButton(_("Choose a file…"))
        self.fileButton.setObjectName("StudioSourcesFileButton")
        self.fileButton.setAutoDefault(False)
        self.fileButton.connect("clicked()", self.chooseFile)
        layout.addRow(_("File"), self.fileButton)
        self.paste = qt.QPlainTextEdit()
        self.paste.setObjectName("StudioSourcesPaste")
        self.paste.setPlaceholderText(_("or paste DOIs and PMIDs, one per line"))
        self.paste.setMaximumHeight(64)
        layout.addRow(_("Paste"), self.paste)
        self.database = qt.QLineEdit()
        self.database.setObjectName("StudioSourcesDatabase")
        self.database.setPlaceholderText(_("PubMed, Scopus…"))
        layout.addRow(_("Database"), self.database)
        self.query = qt.QLineEdit()
        self.query.setObjectName("StudioSourcesQuery")
        self.query.setPlaceholderText(_("The search string (for the search log)"))
        layout.addRow(_("Query"), self.query)
        self.origin = qt.QComboBox()
        self.origin.setObjectName("StudioSourcesOrigin")
        for key, text in (("database", "Database search"), ("citation", "Citation searching"), ("other", "Other")):
            self.origin.addItem(_(text), key)
        layout.addRow(_("Found by"), self.origin)
        row = qt.QHBoxLayout()
        row.setSpacing(6)
        self.importGo = qt.QPushButton(_("Import"))
        self.importGo.setObjectName("StudioSourcesImportGo")
        ui.setRole(self.importGo, "primary")
        self.importGo.setAutoDefault(False)
        self.importGo.connect("clicked()", self.importNow)
        self.importCancel = qt.QPushButton(_("Cancel"))
        self.importCancel.setObjectName("StudioSourcesImportCancel")
        self.importCancel.setAutoDefault(False)
        self.importCancel.connect("clicked()", self.hideImport)
        row.addWidget(self.importGo)
        row.addWidget(self.importCancel)
        row.addStretch(1)
        layout.addRow(row)
        self._importPath = None
        form.hide()
        return form

    def _buildAddCasesForm(self):
        form = qt.QFrame()
        form.setObjectName("StudioSourcesAddCasesForm")
        layout = qt.QHBoxLayout(form)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setSpacing(6)
        self.caseCount = qt.QSpinBox()
        self.caseCount.setObjectName("StudioSourcesCaseCount")
        self.caseCount.setRange(1, 999)
        self.caseCount.setValue(1)
        self.caseCount.setSuffix(" " + _("cases"))
        layout.addWidget(self.caseCount)
        self.addCasesGo = qt.QPushButton(_("Add"))
        self.addCasesGo.setObjectName("StudioSourcesAddCasesGo")
        ui.setRole(self.addCasesGo, "primary")
        self.addCasesGo.setAutoDefault(False)
        self.addCasesGo.connect("clicked()", self.addCasesNow)
        self.addCasesCancel = qt.QPushButton(_("Cancel"))
        self.addCasesCancel.setObjectName("StudioSourcesAddCasesCancel")
        self.addCasesCancel.setAutoDefault(False)
        self.addCasesCancel.connect("clicked()", form.hide)
        layout.addWidget(self.addCasesGo)
        layout.addWidget(self.addCasesCancel)
        layout.addStretch(1)
        form.hide()
        return form

    @staticmethod
    def _tableStyleSheet():
        t = {name: ui.token(name).name() for name in ("card", "text", "text2", "hairline", "accent-subtle", "accent-fg", "border")}
        return (
            f"QTableWidget#StudioSourcesTable {{ background-color: {t['card']}; border: 1px solid {t['hairline']}; border-radius: 8px; "
            f"color: {t['text']}; selection-background-color: {t['accent-subtle']}; selection-color: {t['text']}; outline: none; }}"
            f"QTableWidget#StudioSourcesTable::item {{ padding: 0px 6px; border-bottom: 1px solid {t['hairline']}; }}"
            f"QTableWidget#StudioSourcesTable:focus {{ border: 2px solid {t['accent-fg']}; }}"
            f"QTableWidget#StudioSourcesTable QHeaderView::section {{ background-color: {t['card']}; color: {t['text2']}; border: none; "
            f"border-bottom: 1px solid {t['border']}; padding: 3px 6px; }}"
        )

    # Data

    def _study(self):
        session = _session()
        if session is None:
            return None
        self.session = session
        return session.study

    def load(self, force=False):
        """The records of the open study, read again only when the file changed (or ``force``)."""
        studyObject = self._study()
        if studyObject is None:
            self.records = None
            return None
        sources = _sources()
        path = os.path.join(studyObject.path, *sources.RECORDS_TSV)
        try:
            stat = os.stat(path)
            signature = (studyObject.path, stat.st_mtime_ns, stat.st_size)
        except OSError:
            signature = (studyObject.path, None, None)
        if force or self.records is None or signature != self._signature:
            try:
                self.records = sources.Records.open(studyObject)
            except sources.SourcesError as e:
                self.records = sources.Records(studyObject.path)
                self._showError(str(e))
            self._signature = signature
        return self.records

    def _save(self):
        """Write the records and the study (the search log, new cases)."""
        sources = _sources()
        self.records.save()
        studyObject = self._study()
        try:
            stat = os.stat(os.path.join(studyObject.path, *sources.RECORDS_TSV))
            self._signature = (studyObject.path, stat.st_mtime_ns, stat.st_size)
        except OSError:
            self._signature = None
        session = _session()
        if session is not None:
            session.setValues([], {})
            session.flush()

    def selectedId(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self._rows) or self.table.isRowHidden(row):
            return None
        return self._rows[row]

    def _showError(self, text):
        self.errorLabel.setText(text)
        self.errorLabel.setVisible(bool(text))

    # Refresh

    def refresh(self, selectedIds=None, select=None):
        if not library._isAlive(self.widget):
            return
        records = self.load()
        if records is None:
            return
        sources = _sources()
        keep = select or self.selectedId()
        scroll = self.table.verticalScrollBar().value
        counts = sources.prisma_counts(self._study(), records)
        self.summary.setText(summaryText(records, counts))
        self.hint.setVisible(not records.rows)
        self.hint.setText(_("No records yet. Import a search export, or paste DOIs and PMIDs. Counts and the PRISMA diagram follow from the stages."))
        blocked = self.table.blockSignals(True)
        try:
            self.table.setRowCount(len(records.rows))
            self._rows = [row["record_id"] for row in records.rows]
            for index, row in enumerate(records.rows):
                cells = (row["record_id"], row["title"] or row["doi"] or row["pmid"] or "—", stageText(row["stage"]))
                details = [row["year"], row["journal"], _("{n} cases").format(n=row["n_cases"]) if row["n_cases"] else ""]
                for column, text in enumerate(cells):
                    item = self.table.item(index, column)
                    if item is None:
                        item = qt.QTableWidgetItem()
                        self.table.setItem(index, column, item)
                    item.setText(text)
                    tip = row["title"] if column == 1 else " · ".join(part for part in details if part) if column == 0 else ""
                    if column == 2 and row["stage"] == "excluded" and row["exclusion_reason"]:
                        tip = _("Excluded: {reason}").format(reason=row["exclusion_reason"])
                    item.setToolTip(tip)
                    item.setForeground(qt.QBrush(ui.token(STAGE_LEVELS[row["stage"]] if column == 2 else "text")))
        finally:
            self.table.blockSignals(blocked)
        if keep in self._rows:
            self.table.selectRow(self._rows.index(keep))
        elif self._rows and self.table.currentRow() < 0:
            self.table.selectRow(0)
        self.table.verticalScrollBar().setValue(scroll)
        self._applyFilter()
        self._refreshOffers()
        self._updateButtons()

    def _applyFilter(self, *_args):
        text = str(self.filter.text).strip().lower()
        records = self.records
        if records is None:
            return
        for index, row in enumerate(records.rows):
            haystack = " ".join((row["record_id"], row["title"], row["authors"], row["doi"], row["pmid"], row["stage"], row["journal"])).lower()
            self.table.setRowHidden(index, bool(text) and text not in haystack)

    def _updateButtons(self, *_args):
        selected = self.selectedId()
        has = selected is not None
        for button in (self.fetchButton, self.addCasesButton, self.folderButton):
            button.setEnabled(has)
        record = None
        if has and self.records is not None:
            with contextlib.suppress(KeyError):
                record = self.records.get(selected)
        self.fetchButton.setEnabled(record is not None and bool(record["doi"] or record["pmid"]))
        if record is not None and not (record["doi"] or record["pmid"]):
            self.fetchButton.setToolTip(_("The record has no DOI or PMID to look up"))
        self.prismaButton.setEnabled(self.records is not None and len(self.records) > 0)

    def _refreshOffers(self):
        records = self.records
        if records is None:
            offers = []
        else:
            # Offers depend on the ids, titles and years only: P, X and U do not recompute them
            key = hash(tuple((row["record_id"], row["title"], row["year"]) for row in records.rows))
            if key != self._offersKey:
                self._offersAll = records.merge_offers()
                self._offersKey = key
            offers = self._offersAll
        self._offers = [offer for offer in offers if offer[:2] not in self._dismissed]
        self.offerRow.setVisible(bool(self._offers))
        if self._offers:
            keep, drop, ratio = self._offers[0]
            self.offerLabel.setText(_("Possible duplicate: {drop} looks like {keep} ({percent}% the same title)").format(drop=drop, keep=keep, percent=round(100 * ratio)))
            self.mergeButton.setToolTip(_("Merge {drop} into {keep}; it then counts as a removed duplicate").format(drop=drop, keep=keep))

    # Table actions

    def focusFilter(self):
        self.filter.setFocus()
        self.filter.selectAll()

    def _run(self, function):
        self._showError("")
        try:
            return function()
        except (ValueError, KeyError) as e:
            self._showError(str(e).strip("'\""))
        except Exception as e:
            logging.error(f"Studio sources: {traceback.format_exc()}")
            self._showError(str(e))
        return None

    def advanceSelected(self):
        recordId = self.selectedId()
        records = self.load()
        if recordId is None or records is None:
            return None

        def work():
            stage = records.advance(recordId)
            self._save()
            self.refresh(select=recordId)
            self._moveNext(recordId)
            return stage

        return self._run(work)

    def _moveNext(self, recordId):
        """Select the next visible record, so that P, P, P goes down the list."""
        if recordId not in self._rows:
            return
        for index in range(self._rows.index(recordId) + 1, len(self._rows)):
            if not self.table.isRowHidden(index):
                self.table.selectRow(index)
                return

    def askExclude(self):
        recordId = self.selectedId()
        records = self.load()
        if recordId is None or records is None:
            return
        used = [row["exclusion_reason"] for row in records.rows if row["exclusion_reason"]]
        reasons = list(dict.fromkeys([*used, *(_(reason) for reason in COMMON_REASONS)]))
        self.reasonPopover.open(recordId, reasons)

    def exclude(self, recordId, reason):
        records = self.load()
        if records is None:
            return None

        def work():
            records.exclude(recordId, reason)
            self._save()
            self.refresh(select=recordId)
            self._moveNext(recordId)
            self.table.setFocus()
            return True

        return self._run(work)

    def undoSelected(self):
        recordId = self.selectedId()
        records = self.load()
        if recordId is None or records is None:
            return None

        def work():
            stage = records.undo(recordId)
            if stage is None:
                _toast(_("Nothing to undo for {record}").format(record=recordId))
                return None
            self._save()
            self.refresh(select=recordId)
            return stage

        return self._run(work)

    def attachmentsFolder(self, recordId, create=True):
        records = self.load()
        folder = records.attachments_folder(recordId)
        if create:
            os.makedirs(folder, exist_ok=True)
        return folder

    def openFolder(self):
        recordId = self.selectedId()
        if recordId is None:
            studyObject = self._study()
            folder = os.path.join(studyObject.path, "sources") if studyObject is not None else None
            if folder is not None:
                os.makedirs(folder, exist_ok=True)
        else:
            folder = self._run(lambda: self.attachmentsFolder(recordId))
        if folder:
            _projects().showInFileManager(folder)
        return folder

    # Duplicates

    def mergeOffer(self):
        if not self._offers:
            return
        keep, drop, _ratio = self._offers[0]

        def work():
            studyObject = self._study()
            self.records.merge(keep, drop)
            _sources().relink_cases(studyObject, keep, drop)
            self._save()
            self.refresh(select=keep)
            _toast(_("Merged {drop} into {keep}").format(drop=drop, keep=keep))

        self._run(work)

    def keepOffer(self):
        if self._offers:
            self._dismissed.add(self._offers[0][:2])
            self._refreshOffers()

    # Import

    def showImport(self):
        self.importRow.hide()
        self.importForm.show()
        self.paste.setFocus()

    def hideImport(self):
        self.importForm.hide()
        self.importRow.show()
        self._importPath = None
        self.fileButton.setText(_("Choose a file…"))
        self.paste.clear()

    def chooseFile(self):
        path = qt.QFileDialog.getOpenFileName(self.widget, _("Import records"), "", FILE_FILTER)
        if path:
            self._importPath = str(path)
            self.fileButton.setText(os.path.basename(self._importPath))
            if not str(self.database.text).strip():
                self.database.setText(os.path.splitext(os.path.basename(self._importPath))[0])

    def importNow(self):
        return self.importRecords(path=self._importPath, text=str(self.paste.plainText), database=str(self.database.text).strip() or None,
                                  query=str(self.query.text).strip(), origin=self.origin.currentData)

    def importRecords(self, path=None, text="", database=None, query="", origin="database"):
        """Add the records of a file or of pasted text; log the search; offer near duplicates. Return the report or None."""
        sources = _sources()
        records = self.load()
        studyObject = self._study()
        if records is None or studyObject is None:
            return None
        report = None
        self._showError("")
        try:
            if path:
                with open(path, encoding="utf-8-sig", errors="replace") as f:
                    text = f.read()
            elif not text.strip():
                raise sources.SourcesError(_("Choose a file or paste DOIs or PMIDs"))
            report = sources.import_text(studyObject, records, text, path or "", origin=origin, database=database, query=query)
        except (OSError, sources.SourcesError) as e:
            self._showError(str(e))
            return None
        self._save()
        self.hideImport()
        self.refresh()
        message = _("{added} added").format(added=len(report["added"]))
        if report["merged"]:
            message += " · " + _("{n} duplicates merged").format(n=len(report["merged"]))
        _toast(message)
        return report

    # Metadata and cases

    def fetchMetadata(self):
        recordId = self.selectedId()
        records = self.load()
        if recordId is None or records is None:
            return None
        sources = _sources()
        self._showError("")
        qt.QApplication.setOverrideCursor(qt.Qt.WaitCursor)
        try:
            filled = sources.fetch_metadata(records, recordId)
        except sources.SourcesError as e:
            self._showError(str(e))
            return None
        finally:
            qt.QApplication.restoreOverrideCursor()
        self._save()
        self.refresh(select=recordId)
        _toast(_("Filled {fields}").format(fields=", ".join(filled)) if filled else _("Nothing new to fill"))
        return filled

    def showAddCases(self):
        self.addCasesForm.show()
        self.caseCount.setFocus()

    def addCasesNow(self):
        return self.addCases(self.selectedId(), int(self.caseCount.value))

    def addCases(self, recordId, n):
        """Add ``n`` cases of the record to the study. Return the new subject ids."""
        records = self.load()
        studyObject = self._study()
        if recordId is None or records is None or studyObject is None:
            return None

        def work():
            created = _sources().add_cases(studyObject, records, recordId, n)
            self._save()
            self.addCasesForm.hide()
            self.refresh(select=recordId)
            _toast((_("{n} case added to {record}") if len(created) == 1 else _("{n} cases added to {record}")).format(n=len(created), record=recordId))
            return created

        return self._run(work)

    # PRISMA

    def exportPrisma(self):
        records = self.load()
        studyObject = self._study()
        if records is None or studyObject is None:
            return None

        def work():
            _counts, paths = _sources().export_prisma(studyObject, records)
            _toast(_("PRISMA counts and flow diagram written to derivatives/prisma"), ms=4000)
            _projects().showInFileManager(os.path.dirname(paths[0]))
            return paths

        return self._run(work)


_cards = {}


def _createCard(session):
    card = SourcesCard(session)
    _cards[card.widget] = card
    card.widget.connect("destroyed()", lambda: _cards.pop(card.widget, None))
    return card.widget


def _refreshCard(widget, session, selectedIds):
    card = _cards.get(widget)
    if card is not None and library._isAlive(widget):
        card.refresh(selectedIds)


def liveCard():
    for widget, card in list(_cards.items()):
        if library._isAlive(widget) and widget.isVisible():
            return card
    return None


def _exportPrisma():
    card = liveCard()
    if card is not None:
        return card.exportPrisma()
    session = _session()
    if session is None:
        _toast(_("Open a study first"))
        return None
    sources = _sources()
    _counts, paths = sources.export_prisma(session.study)
    _toast(_("PRISMA counts and flow diagram written to derivatives/prisma"), ms=4000)
    return paths


def _paletteActions(shell=None):
    return [
        {"id": "lnm.sources-prisma", "title": "Export PRISMA flow diagram", "category": "Library",
         "synonyms": ["prisma", "flow diagram", "literature search", "records", "systematic review", "screening counts"],
         "callback": _exportPrisma, "enabled": lambda: _session() is not None,
         "description": "Write the PRISMA counts and flow diagram of the study's literature records to derivatives/prisma"},
    ]


CARD = {"section": "study", "order": CARD_ORDER, "create": _createCard, "refresh": _refreshCard}
PALETTE_ACTIONS = _paletteActions

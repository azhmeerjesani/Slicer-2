"""Library extension: blind double tracing, mask-level agreement and adjudication (L-44; SPEC_LNM_ADDENDUM 7.2, RW-11).

- **Column** "2nd tracing": "● To do" (assigned), "● In progress" (work kept in passing, still in the queue), "● 0.82" (the
  Dice of the pair; red below the adjudication threshold),
  "● Consensus" (adjudicated); "—" without a second tracing.
- **Filters and queues** "Second tracing" (``second_tracing``: assigned, not traced yet) and "Adjudicate" (``adjudicate``:
  pairs whose Dice is below the study's threshold): chips of the subject list and ``StudySession.next(queue=…)``; the
  case strip's queues of the same names hand their subjects over to the states of ``StudioLib.adjudicate``.
- **STUDY card** "Agreement": the counts, the medians of the last computed agreement (Dice, centroid distance, HD95, volume
  ratio, on the tracing grid and on the 2 mm connectome grid) against the between-lesion baseline, **Second tracing N →**
  and **Adjudicate N →** (Draw states, Studio only), **Compute agreement** (writes ``derivatives/qc/interrater.tsv``, read
  by the methods text), the Dice threshold of adjudication (audited) and **Assign second tracing…** (a seeded random
  fraction; inline form).
- **BATCH card** "Assign second tracing…" for the selected subjects (the same inline form; a companion module, as the
  libcard contract has one CARD per module).

Agreement is computed on masks, never shown as network similarity (``agreement.NETWORK_LABEL``). Importing this module
installs the study hooks of ``StudioLib.adjudicate``. Works in Classic (the Studio module panel hosts the cards; the Draw
states need Studio).
"""

import contextlib
import logging
import sys
import traceback
import types

import qt
from slicer.i18n import tr as _

from . import adjudicate, library, review, ui

CARD_ORDER = 47
BATCH_ORDER = 47
#: The BATCH entry lives in a companion module: the libcard contract has one CARD per module
BATCH_MODULE = "StudioLib.libcard_agreement_batch"
DOT = "●"
#: Lines reserved for the metrics of the Agreement card
METRICS_LINES = 4
MODE_ITEMS = (("inter", "Inter-rater (another rater)"), ("intra", "Intra-rater (same rater, ≥ 7 days later)"))
LEVEL_ITEMS = (("trace_only", "Trace only (reuse the figure fit)"), ("full", "Full (own figure crop and fit)"))


def _rules():
    return adjudicate._rules()


def _session():
    return review._session()


def _missing(value):
    return value is None or str(value).strip() in ("", "n/a")


def _safe(function, *args):
    try:
        return function(*args)
    except Exception:
        logging.error(f"Studio Library: agreement: {traceback.format_exc()}")
        return None


#
# Column, filters and queues
#


def agreementCell(row):
    """``(text, level)`` of the "2nd tracing" column."""
    rules = _rules()
    status = rules.status_of(row)
    if status is None:
        return "—", "muted"
    if status == rules.ADJUDICATED:
        return f"{DOT} " + _("Consensus"), "ok"
    if status == rules.IN_PROGRESS:
        return f"{DOT} " + _("In progress"), "warn"
    if status == rules.DONE:
        dice = rules._float(row.get("rater2_dice"))
        if dice is None:
            return f"{DOT} " + _("Traced"), "neutral"
        return f"{DOT} {dice:.2f}", "danger" if dice < adjudicate.threshold() else "ok"
    return f"{DOT} " + _("To do"), "warn"


def counts(studyObject):
    rules = _rules()
    rows = studyObject.subjects
    threshold = rules.dice_threshold(studyObject)
    return {
        "todo": sum(1 for row in rows if rules.second_tracing_todo(row)),
        "pairs": sum(1 for row in rows if rules.status_of(row) in rules.PAIR_STATUSES),
        "adjudicate": sum(1 for row in rows if rules.needs_adjudication(row, threshold)),
        "consensus": sum(1 for row in rows if rules.status_of(row) == rules.ADJUDICATED),
    }


#
# Assign form (STUDY: a random fraction; BATCH: the selected subjects)
#


class AssignForm:
    """Inline form of "Assign second tracing…" (nothing modal). ``selected()`` gives the BATCH subjects (None: a fraction)."""

    def __init__(self, prefix, selected=None):
        self.selected = selected
        self.widget = qt.QFrame()
        self.widget.setObjectName(f"{prefix}AssignForm")
        layout = qt.QFormLayout(self.widget)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setSpacing(4)
        self.fraction = qt.QSpinBox()
        self.fraction.setObjectName(f"{prefix}AssignFraction")
        self.fraction.setRange(1, 100)
        self.fraction.setSuffix(" %")
        self.fraction.setValue(round(100 * _rules().DEFAULT_FRACTION))
        self.fraction.setToolTip(_("Share of the traced, included subjects drawn at random (the seed is recorded)"))
        if selected is None:
            layout.addRow(_("Subjects"), self.fraction)
        else:
            self.fraction.hide()
        self.rater = qt.QLineEdit()
        self.rater.setObjectName(f"{prefix}AssignRater")
        self.rater.setPlaceholderText(_("Any rater"))
        self.rater.setToolTip(_("The second rater's name or initials (required for intra-rater agreement)"))
        layout.addRow(_("Rater"), self.rater)
        self.mode = qt.QComboBox()
        self.mode.setObjectName(f"{prefix}AssignMode")
        for key, text in MODE_ITEMS:
            self.mode.addItem(_(text), key)
        layout.addRow(_("Mode"), self.mode)
        self.level = qt.QComboBox()
        self.level.setObjectName(f"{prefix}AssignLevel")
        for key, text in LEVEL_ITEMS:
            self.level.addItem(_(text), key)
        self.level.setToolTip(_("For figure tracings: whether the second rater reuses the first tracing's figure fit"))
        layout.addRow(_("Blind level"), self.level)
        self.seed = qt.QLineEdit()
        self.seed.setObjectName(f"{prefix}AssignSeed")
        self.seed.setPlaceholderText(_("New random seed"))
        self.seed.setValidator(qt.QIntValidator(0, 2147483647, self.seed))
        if selected is None:
            layout.addRow(_("Seed"), self.seed)
        else:
            self.seed.hide()
        self.errorLabel = qt.QLabel()
        self.errorLabel.setObjectName(f"{prefix}AssignError")
        self.errorLabel.setWordWrap(True)
        ui.bindStyleSheet(self.errorLabel, lambda: f"QLabel {{ color: {ui.token('danger').name()}; }}")
        self.errorLabel.hide()
        layout.addRow(self.errorLabel)
        buttons = qt.QHBoxLayout()
        buttons.setSpacing(6)
        self.assignButton = qt.QPushButton(_("Assign"))
        self.assignButton.setObjectName(f"{prefix}AssignButton")
        ui.setRole(self.assignButton, "secondary")
        self.assignButton.connect("clicked()", self.assign)
        self.cancelButton = qt.QPushButton(_("Cancel"))
        self.cancelButton.setObjectName(f"{prefix}AssignCancelButton")
        ui.setRole(self.cancelButton, "ghost")
        self.cancelButton.connect("clicked()", self.hide)
        for button in (self.assignButton, self.cancelButton):
            button.setMinimumHeight(28)
            ui.installButtonKeys(button)
        buttons.addWidget(self.assignButton, 1)
        buttons.addWidget(self.cancelButton)
        layout.addRow(buttons)
        for edit in (self.rater, self.seed):
            edit.connect("returnPressed()", self.assign)
        self.widget.hide()

    def show(self):
        self.errorLabel.hide()
        self.widget.show()
        shell = library._shell()
        if shell is not None and hasattr(shell, "registerEscapeOverlay"):
            shell.registerEscapeOverlay(self.widget, self.hide)
        self.rater.setFocus()

    def hide(self):
        self.widget.hide()
        shell = library._shell()
        if shell is not None and hasattr(shell, "unregisterEscapeOverlay"):
            with contextlib.suppress(Exception):
                shell.unregisterEscapeOverlay(self.widget)

    def isOpen(self):
        return not self.widget.isHidden()

    def assign(self):
        session = _session()
        if session is None:
            return None
        rules = _rules()
        sids = self.selected() if self.selected is not None else None
        rater = str(self.rater.text).strip() or None
        seedText = str(self.seed.text).strip()
        try:
            record = rules.assign(session.study, fraction=self.fraction.value / 100.0, rater=rater, seed=int(seedText) if seedText else None, sids=sids,
                                  mode=self.mode.itemData(self.mode.currentIndex), level=self.level.itemData(self.level.currentIndex),
                                  who=review.raterName(ask=False))
        except ValueError as e:
            self.errorLabel.setText(str(e))
            self.errorLabel.show()
            return None
        session.setValues([], {})
        session.flush()
        self.hide()
        n = len(record["sids"])
        text = _("{n} second tracing(s) assigned").format(n=n)
        if record.get("seed") is not None:
            text += " · " + _("seed {seed}").format(seed=record["seed"])
        if record.get("skipped"):
            text += " · " + _("{n} not eligible").format(n=len(record["skipped"]))
        review._toast(text, ms=4000)
        return record


#
# STUDY card
#


def _metricsText(data):
    """Medians of the last computed agreement against the between-lesion baseline (three short lines), or ""."""
    if not data or not data.get("n_pairs"):
        return ""
    median = data.get("median") or {}
    coarse = data.get("median_2mm") or {}
    baseline = (data.get("baseline") or {}).get("median_mm")

    def number(value, digits=2, unit=""):
        return "—" if value is None else f"{value:.{digits}f}{unit}"

    lines = [
        _("Median Dice {dice} · HD95 {hd} · volume ratio {ratio}").format(dice=number(median.get("dice")), hd=number(median.get("hd95_mm"), 1, " mm"),
                                                                          ratio=number(median.get("volume_ratio"))),
        _("Centroids {centroid} apart vs {baseline} between lesions").format(centroid=number(median.get("centroid_mm"), 1, " mm"), baseline=number(baseline, 1, " mm")),
        _("2 mm grid: Dice {dice} · {n} pairs").format(dice=number(coarse.get("dice")), n=data["n_pairs"]),
    ]
    return "\n".join(lines)


class AgreementCard:
    """The 'Agreement' part of the STUDY card."""

    def __init__(self, session):
        self.session = session
        self._setting = False
        self.widget = qt.QWidget()
        self.widget.setObjectName("StudioAgreementLibraryCard")
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(8)
        title = qt.QLabel(_("Agreement"))
        title.setObjectName("StudioAgreementLibraryTitle")
        title.setProperty("slicerRole", "sectionHeader")
        library._setFont(title, 12, qt.QFont.DemiBold)
        layout.addWidget(title)
        self.summary = library._cardLabel("StudioAgreementSummary", 12, "text")
        layout.addWidget(self.summary)
        self.metrics = library._cardLabel("StudioAgreementMetrics", 12, "text2")
        self.metrics.setToolTip(_("Tracing reliability measured on the masks (Cotovio et al. 2020: 8 mm between raters vs 51 mm between lesions). "
                                  "Network similarity overstates tracing agreement (van den Heuvel 2026), so it is not used here."))
        # Room for the metric lines from the start (stable layout): the module panel's scroll area keeps a card at its
        # earlier height when a label grows after it was laid out, and the lines would overlap
        self.metrics.setMinimumHeight(METRICS_LINES * qt.QFontMetrics(self.metrics.font).lineSpacing())
        layout.addWidget(self.metrics)
        self.secondRow, self.secondButton = library._rowButton(_("Second tracing →"), "StudioAgreementSecondButton", self.startSecond, "primary")
        self.secondButton.setToolTip(_("Trace the assigned subjects again, blind: the first tracing is not loaded"))
        layout.addWidget(self.secondRow)
        self.adjudicateRow, self.adjudicateButton = library._rowButton(_("Adjudicate →"), "StudioAgreementAdjudicateButton", self.startAdjudicate)
        self.adjudicateButton.setToolTip(_("Both tracings as outlines: choose A, B, Union, Intersection or Edit for the consensus lesion"))
        layout.addWidget(self.adjudicateRow)
        self.computeRow, self.computeButton = library._rowButton(_("Compute agreement"), "StudioAgreementComputeButton", self.compute)
        self.computeButton.setToolTip(_("Dice, centroid distance, volume ratio and HD95 of every pair, on the tracing grid and on the 2 mm grid; "
                                        "writes derivatives/qc/interrater.tsv for the methods text"))
        layout.addWidget(self.computeRow)
        thresholdRow = qt.QHBoxLayout()
        thresholdRow.setSpacing(6)
        thresholdLabel = qt.QLabel(_("Adjudicate below Dice"))
        thresholdLabel.setObjectName("StudioAgreementThresholdLabel")
        thresholdRow.addWidget(thresholdLabel, 1)
        self.threshold = qt.QDoubleSpinBox()
        self.threshold.setObjectName("StudioAgreementThreshold")
        self.threshold.setRange(0.05, 1.0)
        self.threshold.setSingleStep(0.05)
        self.threshold.setDecimals(2)
        self.threshold.setToolTip(_("Pairs with a Dice below this go to the Adjudicate queue (changes are recorded)"))
        self.threshold.connect("editingFinished()", self._setThreshold)
        thresholdRow.addWidget(self.threshold)
        layout.addLayout(thresholdRow)
        self.assignRow, self.assignButton = library._rowButton(_("Assign second tracing…"), "StudioAgreementAssignButton", self._showForm)
        self.assignButton.setToolTip(_("A seeded random share of the traced subjects is traced a second time, blind (the seed is recorded)"))
        layout.addWidget(self.assignRow)
        self.form = AssignForm("StudioAgreement")
        layout.addWidget(self.form.widget)

    def refresh(self, selectedIds=None):
        session = _session()
        if session is None:
            return
        self.session = session
        studyObject = session.study
        rules = _rules()
        numbers = counts(studyObject)
        parts = [_("{n} to trace twice").format(n=numbers["todo"]), _("{n} pairs").format(n=numbers["pairs"])]
        if numbers["adjudicate"]:
            parts.append(_("{n} to adjudicate").format(n=numbers["adjudicate"]))
        if numbers["consensus"]:
            parts.append(_("{n} consensus").format(n=numbers["consensus"]))
        self.summary.setText(" · ".join(parts))
        data = rules.read_summary(studyObject)
        text = _metricsText(data)
        # Pairs that could not be computed (a missing file) are listed as errors, not counted in n_pairs
        computed = (data.get("n_pairs") or 0) + len(data.get("errors") or []) if data else None
        if numbers["pairs"] and (not data or computed != numbers["pairs"] or _stale(studyObject, data)):
            text = (text + "\n" if text else "") + _("Not up to date: Compute agreement")
        elif not numbers["pairs"]:
            text = _("No lesion is traced twice yet: assign second tracings to a share of the subjects")
        if str(self.metrics.text) != text:
            self.metrics.setText(text)
        shell = library._shell()
        inStudio = shell is not None
        idle = adjudicate.state() is None
        rows = (self.secondRow, self.adjudicateRow, self.computeRow, self.assignRow)
        before = [not row.isHidden() for row in rows]
        # A state in Draw disables the rows rather than hiding them: the card keeps its height (stable layout)
        self.secondRow.setVisible(inStudio and numbers["todo"] > 0)
        self.secondButton.setEnabled(idle)
        self.secondButton.setText(_("Second tracing {n} →").format(n=numbers["todo"]))
        self.adjudicateRow.setVisible(inStudio and numbers["adjudicate"] > 0)
        self.adjudicateButton.setEnabled(idle)
        self.adjudicateButton.setText(_("Adjudicate {n} →").format(n=numbers["adjudicate"]))
        self.computeRow.setVisible(numbers["pairs"] > 0)
        self.assignRow.setVisible(not self.form.isOpen())
        self._setting = True
        try:
            self.threshold.setValue(rules.dice_threshold(studyObject))
        finally:
            self._setting = False
        if [not row.isHidden() for row in rows] != before:
            self.relayout()
            qt.QTimer.singleShot(0, self.relayout)

    def relayout(self):
        """Fit the card to rows shown or hidden after it was laid out.

        The module panel's scroll area keeps a card at its earlier height when its contents change after layout, and the
        rows would overlap: the layouts up to the panel are redone and the card's minimum height is pinned to the height
        its contents need at its width (word-wrapped labels), which also lets it shrink again.
        """
        widget = self.widget
        if not library._isAlive(widget):
            return
        layout = widget.layout()
        layout.invalidate()
        layout.activate()
        width = widget.width
        needed = widget.heightForWidth(width) if width > 0 and widget.hasHeightForWidth() else -1
        if needed <= 0:
            needed = layout.sizeHint().height()
        if widget.minimumHeight != needed:
            widget.setMinimumHeight(needed)
        parent = widget
        for _level in range(6):
            parent.updateGeometry()
            if parent.layout() is not None:
                parent.layout().invalidate()
            parent = parent.parentWidget()
            if parent is None:
                break

    def _showForm(self):
        self.assignRow.hide()
        self.form.show()
        self.relayout()
        qt.QTimer.singleShot(0, self.relayout)

    def _setThreshold(self):
        if self._setting:
            return
        session = _session()
        if session is None:
            return
        try:
            record = _rules().set_dice_threshold(session.study, float(self.threshold.value), who=review.raterName(ask=False))
        except ValueError as e:
            review._toast(str(e), level="warning")
            return
        if record is not None:
            session.setValues([], {})
            session.flush()

    def compute(self):
        return computeAgreement()

    def startSecond(self):
        adjudicate.startSecondTracing()

    def startAdjudicate(self):
        adjudicate.startAdjudication()


def _stale(studyObject, data):
    """True when a second tracing or a consensus is newer than the stored summary."""
    created = _rules()._parse_time(data.get("created"))
    if created is None:
        return True
    for row in studyObject.subjects:
        for key in ("rater2_date", "modified"):
            if _rules().status_of(row) is None:
                continue
            when = _rules()._parse_time(row.get(key))
            if when is not None and when > created:
                return True
    return False


def computeAgreement():
    """Compute the agreement of every pair and write interrater.tsv / .json (Search, the card). Return the summary."""
    session = _session()
    if session is None:
        review._toast(_("Open a study first"))
        return None
    qt.QApplication.setOverrideCursor(qt.Qt.WaitCursor)
    try:
        data = _rules().summary(session.study)
    except Exception as e:
        logging.error(f"Studio Library: the agreement could not be computed:\n{traceback.format_exc()}")
        review._toast(str(e), ms=5000, level="warning")
        return None
    finally:
        qt.QApplication.restoreOverrideCursor()
    session.setValues([], {})
    session.flush()
    if data["n_pairs"]:
        review._toast(_("Agreement of {n} pairs written to derivatives/qc/interrater.tsv").format(n=data["n_pairs"]), ms=3500)
    else:
        review._toast(_("No lesion is traced twice yet"), ms=3000)
    return data


_cards = {}


def _createCard(session):
    card = AgreementCard(session)
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


#
# BATCH card
#


class BatchSection:
    """"Assign second tracing…" for the subjects selected in the list (the BATCH card)."""

    def __init__(self, session):
        self.sids = []
        self.widget = qt.QWidget()
        self.widget.setObjectName("StudioAgreementBatch")
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(6)
        self.row, self.button = library._rowButton(_("Assign second tracing…"), "StudioAgreementBatchAssignButton", self._showForm)
        self.button.setToolTip(_("The selected subjects are traced a second time, blind"))
        layout.addWidget(self.row)
        self.form = AssignForm("StudioAgreementBatch", selected=lambda: list(self.sids))
        layout.addWidget(self.form.widget)

    def refresh(self, sids):
        self.sids = list(sids or [])
        self.button.setText(_("Assign second tracing to {n}…").format(n=len(self.sids)))
        self.row.setVisible(not self.form.isOpen())
        self.form.assignButton.setText(_("Assign {n}").format(n=len(self.sids)))

    def _showForm(self):
        self.row.hide()
        self.form.show()


_batches = {}


def _createBatch(session):
    section = BatchSection(session)
    _batches[section.widget] = section
    section.widget.connect("destroyed()", lambda: _batches.pop(section.widget, None))
    return section.widget


def _refreshBatch(widget, session, selectedIds):
    section = _batches.get(widget)
    if section is not None and library._isAlive(widget):
        section.refresh(selectedIds)


def _ensureBatchCard():
    """Register the companion module whose CARD is the BATCH entry (library.libcardModules lists it)."""
    module = sys.modules.get(BATCH_MODULE)
    if module is None:
        module = types.ModuleType(BATCH_MODULE, "Library extension: 'Assign second tracing…' in BATCH (see StudioLib.libcard_agreement).")
        module.CARD = {"section": "batch", "order": BATCH_ORDER, "create": _createBatch, "refresh": _refreshBatch}
        sys.modules[BATCH_MODULE] = module
    modules = getattr(library, "_libcards", None)
    if isinstance(modules, list) and module not in modules:
        modules.append(module)  # the registry was built before this module was imported


#
# Search
#


def _waiting(key):
    session = _session()
    return counts(session.study)[key] if session is not None else 0


def _startSecond():
    return adjudicate.startSecondTracing()


def _startAdjudication():
    return adjudicate.startAdjudication()


def _paletteActions(shell=None):
    return [
        {"id": "lnm.second-tracing-start", "title": "Second tracing (blind)", "category": "Library",
         "synonyms": ["double tracing", "inter-rater", "intra-rater", "reliability", "rater 2", "retrace"],
         "callback": _startSecond, "enabled": lambda: adjudicate.state() is None and _waiting("todo") > 0,
         "description": "Trace the assigned subjects again in Draw without seeing the first tracing"},
        {"id": "lnm.adjudicate-start", "title": "Adjudicate tracings", "category": "Library", "synonyms": ["consensus", "disagreement", "dice", "union", "intersection"],
         "callback": _startAdjudication, "enabled": lambda: adjudicate.state() is None and _waiting("adjudicate") > 0,
         "description": "Both tracings as outlines: choose A, B, Union, Intersection or Edit"},
        {"id": "lnm.agreement-compute", "title": "Compute tracing agreement", "category": "Library",
         "synonyms": ["dice", "hd95", "hausdorff", "inter-rater agreement", "interrater.tsv", "reliability"],
         "callback": computeAgreement, "enabled": lambda: _waiting("pairs") > 0,
         "description": "Mask-level agreement of the lesions traced twice, against the between-lesion baseline"},
        {"id": "lnm.agreement-exit", "title": "Exit second tracing or adjudication", "category": "Draw", "synonyms": ["stop second tracing", "end adjudication"],
         "callback": adjudicate.exitState, "enabled": lambda: adjudicate.state() is not None, "description": "Back to the Library (what was saved is kept)"},
    ]


#
# Library extension points
#

CARD = {"section": "study", "order": CARD_ORDER, "create": _createCard, "refresh": _refreshCard}
COLUMNS = [{"key": "rater2_status", "header": "2nd tracing", "width": 116, "valueFn": agreementCell}]
FILTERS = [
    {"key": adjudicate.QUEUE_SECOND, "label": "Second tracing", "predicate": adjudicate.secondTodo},
    {"key": adjudicate.QUEUE_ADJUDICATE, "label": "Adjudicate", "predicate": adjudicate.needsAdjudication},
]
QUEUES = [
    {"key": adjudicate.QUEUE_SECOND, "label": "Second tracing", "predicate": adjudicate.secondTodo},
    {"key": adjudicate.QUEUE_ADJUDICATE, "label": "Adjudicate", "predicate": adjudicate.needsAdjudication},
]
PALETTE_ACTIONS = _paletteActions

_safe(_ensureBatchCard)
try:
    adjudicate.installStudyHooks()
except Exception:
    logging.error(f"Studio Library: the agreement hooks could not be installed:\n{traceback.format_exc()}")

# The registry of LesionNetworkMappingLib.agreement registers its columns on import (participants.json roles)
with contextlib.suppress(Exception):
    _rules()

"""Library extension: review of lesions (L-43; SPEC_LNM_ADDENDUM 7.2).

- **Column** "Review": a status dot and the state (Pending, Approved, Changes, Rejected; "—" when not reviewed), with the
  reviewer and the comment in the tooltip text of the state.
- **Filters and queues** "Needs review" (pending), "Changes requested" and "Auto-unreviewed" (automatic segmentations no
  human approved): chips of the subject list, and ``StudySession.next(queue=…)`` / ``StudioLib.review.startReview(queue)``.
- **STUDY card** "Review": the counts, **Review N →** (the review state of Draw, Studio only), **Resume review**, and
  the study settings *Require review before analyses* (``review_required``) and *Blind tracing* (``blind_tracing``),
  each change audited in ``.studio/audit.jsonl``. With blind tracing, the group column of the subject list is hidden
  (the grid cards already hide the blinded columns).
- **SUBJECT section** "Review" (Studio Library and the Classic Studio module panel): the state, who traced and who
  reviewed, the comment, the chips, **Review in Draw** (Studio), and the same three decisions as Draw's review footer:
  Approve, Request changes… and Reject… (inline forms); **Reopen review** for a decided lesion.

Importing this module installs the study hooks of ``StudioLib.review`` (the Done hook that records assisted provenance
and submits lesions for review, the open hook that keeps the review state and the Lesion card chips up to date).
"""

import contextlib
import logging
import traceback

import qt
from slicer.i18n import tr as _

from . import library, review

CARD_ORDER = 45
SECTION_ORDER = 45
#: The dot of the Review column
DOT = "●"


def _rules():
    return review._rules()


def _session():
    return review._session()


def _missing(value):
    return value is None or str(value).strip() in ("", "n/a")


#
# Column, filters and queues
#


def reviewCell(row):
    """``(text, level)`` of the Review column: "● Pending" (amber), "● Approved" (green), "● Rejected" (red)..."""
    rules = _rules()
    status = rules.status_of(row)
    if status == "none":
        if rules.is_auto(row):
            return f"{DOT} " + _("Auto"), "danger"
        return "—", "muted"
    text = {"pending": _("Pending"), "approved": _("Approved"), "changes": _("Changes"), "rejected": _("Rejected")}.get(status, status)
    level = {"pending": "warn", "approved": "ok", "changes": "warn", "rejected": "danger"}.get(status, "neutral")
    if rules.is_auto_unreviewed(row):
        level = "danger"
    return f"{DOT} {text}", level


def needsReview(row):
    return _rules().needs_review(row)


def changesRequested(row):
    return _rules().changes_requested(row)


def autoUnreviewed(row):
    return _rules().is_auto_unreviewed(row)


def counts(studyObject):
    """{queue key: n} of a study."""
    rows = studyObject.subjects
    return {review.QUEUE_NEEDS_REVIEW: sum(1 for row in rows if needsReview(row)), review.QUEUE_CHANGES: sum(1 for row in rows if changesRequested(row)),
            review.QUEUE_AUTO: sum(1 for row in rows if autoUnreviewed(row))}


#
# Blinding of the subject list
#


def _studyTables():
    import slicer

    mainWindow = slicer.util.mainWindow()
    if mainWindow is None:
        return []
    return [table for table in mainWindow.findChildren("QTableView") if table.objectName == "StudioStudyTable"]


#: Filter chips and the sort of the subject list that show the group (hidden by blind tracing)
GROUP_FILTERS = ("cases", "controls")
GROUP_SORT = "group"
#: Labels of the subject list that count cases and controls (``library.studyCounts``)
COUNT_LABELS = ("StudioStudyPageCounts", "StudioStudyCardCounts")


def applyBlinding(session=None):
    """Hide what shows the group, scores and symptoms in the subject lists while ``blind_tracing`` is set (the blinded
    columns of ``review.blinded_columns``, the Cases / Controls chips, the Group sort and the case / control counts);
    show them again without blinding.
    """
    session = session or _session()
    hidden = set(_rules().blinded_columns(session.study)) if session is not None else set()
    for table in _studyTables():
        model = table.model()
        columns = getattr(model, "columns", None)
        if not columns:
            continue
        for index, entry in enumerate(columns):
            hide = entry.get("key") in hidden
            if table.isColumnHidden(index) != hide:
                table.setColumnHidden(index, hide)
    blind = "group" in hidden
    for view in list(getattr(library, "_studyViews", ())):
        with contextlib.suppress(Exception):
            if library._isAlive(view.widget):
                _blindList(view, blind)
    if session is not None:
        _blindCounts(session.study, blind)


def _blindList(view, blind):
    """The group chips and the group sort of one subject list (``library.StudyList``)."""
    chips = getattr(view, "chips", None)
    if chips is None:
        return
    if blind and hasattr(view, "filterKey") and view.filterKey() in GROUP_FILTERS:
        view.setFilter("all")
    for key in GROUP_FILTERS:
        button = chips.get(key)
        if button is not None and button.isHidden() != blind:
            button.setVisible(not blind)
    combo = getattr(view, "sortCombo", None)
    if combo is None:
        return
    index = combo.findData(GROUP_SORT)
    if blind and index >= 0:
        if view.sortKey() == GROUP_SORT:
            view.setSort("table")
        index = combo.findData(GROUP_SORT)
        _sortLabels[GROUP_SORT] = str(combo.itemText(index))  # the library's translated label, put back without blinding
        combo.removeItem(index)
    elif not blind and index < 0:
        keys = [key for key, _label in library.STUDY_SORTS]
        position = sum(1 for key in keys[:keys.index(GROUP_SORT)] if combo.findData(key) >= 0)
        combo.insertItem(position, _sortLabels.get(GROUP_SORT) or dict(library.STUDY_SORTS)[GROUP_SORT], GROUP_SORT)


_sortLabels = {}


class _BlindCounts:
    """A study as ``library.studyCounts`` sees it without the group column."""

    def __init__(self, studyObject):
        self.subjects = [_rules().blinded_row(studyObject, row) for row in studyObject.subjects]


def _blindCounts(studyObject, blind):
    import slicer

    mainWindow = slicer.util.mainWindow()
    if mainWindow is None:
        return
    text = library.studyCounts(_BlindCounts(studyObject) if blind else studyObject)
    for name in COUNT_LABELS:
        for label in mainWindow.findChildren("QLabel", name):
            if str(label.text) != text:
                label.setText(text)


def _scheduleBlinding(session=None):
    # After the subject lists refreshed (their models may have been reset by the same change)
    qt.QTimer.singleShot(0, lambda: _safe(applyBlinding))


def _safe(function, *args):
    try:
        function(*args)
    except Exception:
        logging.error(f"Studio Library: review: {traceback.format_exc()}")


#
# STUDY card
#


class ReviewCard:
    """The 'Review' part of the STUDY card."""

    def __init__(self, session):
        self.session = session
        self._setting = False
        self.widget = qt.QWidget()
        self.widget.setObjectName("StudioReviewLibraryCard")
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(8)
        title = qt.QLabel(_("Review"))
        title.setObjectName("StudioReviewLibraryTitle")
        title.setProperty("slicerRole", "sectionHeader")
        library._setFont(title, 12, qt.QFont.DemiBold)
        layout.addWidget(title)
        self.summary = library._cardLabel("StudioReviewSummary", 12, "text")
        layout.addWidget(self.summary)
        self.reviewRow, self.reviewButton = library._rowButton(_("Review →"), "StudioReviewStartButton", self.start, "primary")
        self.reviewButton.setToolTip(_("Open the lesions waiting for review in Draw, one by one: Approve → next, Request changes or Reject"))
        layout.addWidget(self.reviewRow)
        self.resumeRow, self.resumeButton = library._rowButton(_("Resume review"), "StudioReviewResumeButton", self.resume)
        layout.addWidget(self.resumeRow)
        self.autoRow, self.autoButton = library._rowButton(_("Review automatic segmentations →"), "StudioReviewAutoButton", self.startAuto)
        layout.addWidget(self.autoRow)
        self.requiredCheck = qt.QCheckBox(_("Require review before analyses"))
        self.requiredCheck.setObjectName("StudioReviewRequiredCheck")
        self.requiredCheck.setToolTip(_("Map and cohort analyses use approved lesions only (on by default when the study has case reports). "
                                        "Automatic segmentations always need approval. Changes are recorded."))
        self.requiredCheck.connect("toggled(bool)", lambda checked: self._setSetting("review_required", checked))
        layout.addWidget(self.requiredCheck)
        self.blindCheck = qt.QCheckBox(_("Blind tracing: hide group, scores and symptoms"))
        self.blindCheck.setObjectName("StudioReviewBlindCheck")
        self.blindCheck.setToolTip(_("Tracers and reviewers do not see the group, score and symptom columns, and network maps are never shown "
                                     "while tracing or reviewing. Recorded in the provenance."))
        self.blindCheck.connect("toggled(bool)", lambda checked: self._setSetting("blind_tracing", checked))
        # A long text: it wraps (a check box shows it on one line and would widen the inspector)
        layout.addWidget(library.ui.wrapCheckBox(self.blindCheck))
        self.gateLabel = library._cardLabel("StudioReviewGateLabel", 12, "text2")
        layout.addWidget(self.gateLabel)

    def refresh(self, selectedIds=None):
        session = _session()
        if session is None:
            return
        self.session = session
        studyObject = session.study
        numbers = counts(studyObject)
        rules = _rules()
        summary = rules.gate_summary(studyObject)
        parts = [_("{n} to review").format(n=numbers[review.QUEUE_NEEDS_REVIEW]), _("{n} changes requested").format(n=numbers[review.QUEUE_CHANGES])]
        if numbers[review.QUEUE_AUTO]:
            parts.append(_("{n} automatic not approved").format(n=numbers[review.QUEUE_AUTO]))
        parts.append(_("{n} approved").format(n=summary["approved"]))
        self.summary.setText(" · ".join(parts))
        shell = library._shell()
        inStudio = shell is not None
        current = review.state()
        self.reviewRow.setVisible(inStudio and numbers[review.QUEUE_NEEDS_REVIEW] > 0 and current is None)
        self.reviewButton.setText(_("Review {n} →").format(n=numbers[review.QUEUE_NEEDS_REVIEW]))
        paused = review.pausedQueue()
        self.resumeRow.setVisible(inStudio and paused is not None and current is None)
        if paused is not None:
            self.resumeButton.setText(_("Resume review ({n} left)").format(n=len(paused[1])))
        autoWaiting = [row for row in studyObject.subjects if autoUnreviewed(row) and not needsReview(row)]
        self.autoRow.setVisible(inStudio and bool(autoWaiting) and current is None)
        self._setting = True
        try:
            self.requiredCheck.setChecked(bool(studyObject.review_required))
            self.blindCheck.setChecked(bool(studyObject.blind_tracing))
        finally:
            self._setting = False
        gated = sum(len(sids) for sids in summary["excluded"].values())
        if gated:
            self.gateLabel.setText(_("Analyses leave out {n} lesion(s) that review has not approved (preflight lists them; Include anyway is recorded).").format(n=gated))
        else:
            self.gateLabel.setText(_("Analyses use every included lesion.") if not summary["required"] else _("Analyses use approved lesions only."))
        _scheduleBlinding(session)

    def _setSetting(self, key, value):
        if self._setting:
            return
        session = _session()
        if session is None:
            return
        who = review.raterName(ask=False)
        try:
            record = _rules().set_study_setting(session.study, key, bool(value), who)
        except Exception as e:
            logging.error(f"Studio Library: the review setting {key} could not be changed:\n{traceback.format_exc()}")
            review._toast(str(e), level="warning")
            return
        if record is not None:
            session.setValues([], {})
            session.flush()
            text = {("review_required", True): _("Analyses now use approved lesions only"), ("review_required", False): _("Analyses no longer require review"),
                    ("blind_tracing", True): _("Blind tracing on: group, scores and symptoms are hidden"),
                    ("blind_tracing", False): _("Blind tracing off: group, scores and symptoms are shown")}[(key, bool(value))]
            review._toast(text, ms=3000)
        _scheduleBlinding(session)

    def start(self):
        review.startReview(review.QUEUE_NEEDS_REVIEW)

    def startAuto(self):
        review.startReview(review.QUEUE_AUTO)

    def resume(self):
        review.resumeReview()


_cards = {}


def _createCard(session):
    card = ReviewCard(session)
    _cards[card.widget] = card
    card.widget.connect("destroyed()", lambda: _cards.pop(card.widget, None))
    return card.widget


def _refreshCard(widget, session, selectedIds):
    card = _cards.get(widget)
    if card is not None and library._isAlive(widget):
        card.refresh(selectedIds)


#
# SUBJECT section
#


class SubjectSection:
    """The 'Review' section of the SUBJECT card (Studio Library and the Classic Studio module panel)."""

    def __init__(self, session):
        self.session = session
        self.sid = None
        self.widget = qt.QWidget()
        self.widget.setObjectName("StudioReviewSubjectSection")
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(6)
        title = qt.QLabel(_("Review"))
        title.setObjectName("StudioReviewSubjectTitle")
        title.setProperty("slicerRole", "sectionHeader")
        library._setFont(title, 12, qt.QFont.DemiBold)
        layout.addWidget(title)
        self.chipLayout = qt.QHBoxLayout()
        self.chipLayout.setSpacing(4)
        layout.addLayout(self.chipLayout)
        self.status = library._cardLabel("StudioReviewSubjectStatus", 12, "text")
        layout.addWidget(self.status)
        self.drawRow, self.drawButton = library._rowButton(_("Review in Draw"), "StudioReviewInDrawButton", self._reviewInDraw)
        layout.addWidget(self.drawRow)
        self.actions = review.ReviewActions(self._decide, "StudioReviewSubject")
        layout.addWidget(self.actions.widget)
        self.reopenRow, self.reopenButton = library._rowButton(_("Reopen review"), "StudioReviewReopenButton", self._reopen)
        self.reopenButton.setToolTip(_("Send the lesion back to pending review (recorded); a rejected lesion stays excluded until it is approved"))
        layout.addWidget(self.reopenRow)

    def refresh(self, sid):
        session = _session()
        if session is not None and session is not self.session:
            self.session = session
        if session is None or sid is None or sid not in session.study:
            self.widget.hide()
            return
        if sid != self.sid:
            self.actions.hideForm()
        self.sid = sid
        rules = _rules()
        row = session.study.subject(sid)
        status = rules.status_of(row)
        self.widget.show()
        review.fillChips(self.chipLayout, row)
        lines = [_(rules.STATE_LABELS.get(status, status))]
        tracer = next((row.get(key) for key in ("tracer", "rater") if not _missing(row.get(key))), None)
        if tracer:
            lines.append(_("Traced by {who}").format(who=tracer))
        if not _missing(row.get("trace_method")):
            lines.append(_("Method: {method}").format(method=row["trace_method"]))
        if not _missing(row.get("reviewer")):
            lines.append(_("Reviewer: {who}").format(who=row["reviewer"]))
        if not _missing(row.get("review_comment")):
            lines.append(_("Comment: {comment}").format(comment=row["review_comment"]))
        self.status.setText("\n".join(lines))
        shell = library._shell()
        self.drawRow.setVisible(shell is not None and status not in ("approved", "rejected") and review.state() is None and not _missing(row.get("lesion")))
        decided = status in ("approved", "rejected")
        self.actions.widget.setVisible(not decided)
        self.actions.setState(status, enabled=not _missing(row.get("lesion")))
        self.reopenRow.setVisible(decided)

    def _decide(self, to, comment, reason):
        if self.sid is None:
            return False
        return review.transition(self.sid, to, comment, reason, session=_session(), via="library") is not None

    def _reopen(self):
        if self.sid is not None:
            review.transition(self.sid, "pending", session=_session(), via="library-reopen")

    def _reviewInDraw(self):
        session = _session()
        if session is None or self.sid is None:
            return
        row = session.study.subject(self.sid)
        queue = review.QUEUE_AUTO if autoUnreviewed(row) and not needsReview(row) else review.QUEUE_NEEDS_REVIEW
        review.startReview(queue, sid=self.sid)

    def dispose(self):
        with contextlib.suppress(Exception):
            self.actions.dispose()


_sections = {}


def _createSection(session):
    section = SubjectSection(session)
    _sections[section.widget] = section
    section.widget.connect("destroyed()", lambda: (section.dispose(), _sections.pop(section.widget, None)))
    return section.widget


def _refreshSection(widget, session, sid):
    section = _sections.get(widget)
    if section is not None and library._isAlive(widget):
        section.refresh(sid)


#
# Search
#


def _waiting():
    session = _session()
    return counts(session.study)[review.QUEUE_NEEDS_REVIEW] if session is not None else 0


def _paletteActions(shell=None):
    # "enabled" is evaluated when Search shows the entry (the provider's entries may be kept)
    return [
        {"id": "lnm.review-start", "title": "Review lesions", "category": "Library", "synonyms": ["approve", "reject", "request changes", "review queue", "qc"],
         "callback": lambda: review.startReview(review.QUEUE_NEEDS_REVIEW), "enabled": lambda: review.state() is None and _waiting() > 0,
         "description": "Open the lesions waiting for review in Draw, one by one"},
        {"id": "lnm.review-exit", "title": "Exit review", "category": "Draw", "synonyms": ["stop review", "end review"],
         "callback": review.exitReview, "enabled": lambda: review.state() is not None, "description": "Back to tracing in Draw (decisions are kept)"},
    ]


#
# Library extension points
#

CARD = {"section": "study", "order": CARD_ORDER, "create": _createCard, "refresh": _refreshCard}
SUBJECT_SECTION = {"order": SECTION_ORDER, "create": _createSection, "refresh": _refreshSection}
COLUMNS = [{"key": "review_status", "header": "Review", "width": 104, "valueFn": reviewCell}]
FILTERS = [
    {"key": review.QUEUE_NEEDS_REVIEW, "label": "Needs review", "predicate": needsReview},
    {"key": review.QUEUE_CHANGES, "label": "Changes requested", "predicate": changesRequested},
    {"key": review.QUEUE_AUTO, "label": "Auto-unreviewed", "predicate": autoUnreviewed},
]
QUEUES = [
    {"key": review.QUEUE_NEEDS_REVIEW, "label": "Needs review", "predicate": needsReview},
    {"key": review.QUEUE_CHANGES, "label": "Changes requested", "predicate": changesRequested},
    {"key": review.QUEUE_AUTO, "label": "Auto-unreviewed", "predicate": autoUnreviewed},
]
PALETTE_ACTIONS = _paletteActions


def _onStudy(session):
    _scheduleBlinding(session)


try:
    review.installStudyHooks()
    studyUi = review._studyUi()
    if studyUi is not None:
        studyUi.removeListener(_onStudy)
        studyUi.addListener(_onStudy)
except Exception:
    logging.error(f"Studio Library: the review hooks could not be installed:\n{traceback.format_exc()}")

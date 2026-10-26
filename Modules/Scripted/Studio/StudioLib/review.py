"""The review state of Draw (L-43; SPEC_LNM_ADDENDUM 3.10, 7.2-7.3; USABILITY 2, 3, 5).

Review is a *state* of Draw, not a workspace. ``startReview()`` (Library's Review card "Review N →", the SUBJECT card's
"Review in Draw", Search "Review lesions") opens the lesions of a queue one by one in Draw:

- **Mask** as an outline only (the segmentation's 2D fill is turned off and restored afterwards), on the **source**: the
  figure overlay rebuilt from ``sources/<record>/fig-<k>_fit.json`` (``figure.showSource``, L-29) for a figure tracing,
  else the scan the subject was traced on.
- **Notice** (the inspector notice slot, never over a view): "Review · sub-03 · 2 of 5", who traced it and how, the
  review chips, the reviewer's earlier comment and the subject's notes, and **Exit review**.
- **Footer** in place of "Done → next": **[Approve → next]** (primary, ``Ctrl+Enter``), **[Request changes…]** (an
  inline comment) and **[Reject…]** (an inline reason and comment). Nothing is modal. A decision writes the row and the
  audit record (``LesionNetworkMappingLib.review.apply_transition``) and opens the next lesion of the queue; the end of
  the queue ends the review.
- **Reviewer edits** are allowed: the edited lesion is written before the decision, the tracer's file is kept as
  ``desc-prereview`` (and ``desc-traced`` as ``desc-prereviewtraced``) and ``trace_method`` gets ``review-edit``.
- Leaving Draw, opening a subject that is not waiting for review or Exit review ends the state; the Library's Review
  card offers "Resume review". Network maps are never shown in Draw (W2-05), so neither while reviewing.

The tracer side (study hooks, ``installStudyHooks()``):

- **Done hook**: the assisted counts of Draw (``draw.assistCounts``, fed by Smart fill through ``draw.notifyAssist``)
  become the sidecar's ``SlicerStudio.assist`` {smartfill_voxels_pct, tools}; an automatic tool (``auto:<tool>``) adds
  its token to ``trace_method``. After the lesion is written, the lesion becomes pending when the study requires review,
  when it was under review before, or always for an automatic segmentation (audited, ``via`` "done").
- **Lesion card chips** (``LesionCard.addChipRow("review")``): the review state, "auto-unreviewed" and a neutral
  "Assisted 64%".

``transition()`` is the one entry point of the decisions (the footer, the SUBJECT card in Studio and in Classic, the
tests). The reviewer's name is the fork-only setting ``Studio/RaterName``, asked once (never under ``--testing``).
"""

import contextlib
import logging
import os
import shutil
import time
import traceback
import weakref

import qt
import slicer
from slicer.i18n import tr as _

from . import ui

#: Fork-only setting: the name recorded with reviews (and double tracing, L-44)
RATER_SETTING = "Studio/RaterName"
#: Key of the inspector notice of the review state
NOTICE_KEY = "studio-review"
#: Key of the Lesion card chip row
CHIP_ROW_KEY = "review"
#: The Done hook runs after the figure (20), intake QC and normalization (90) hooks
DONE_HOOK_ORDER = 95
#: Queues of the review (``libcard_review.QUEUES``)
QUEUE_NEEDS_REVIEW = "needs_review"
QUEUE_CHANGES = "changes_requested"
QUEUE_AUTO = "auto_unreviewed"
#: Name recorded when no rater name is set and none can be asked (--testing, a cancelled question for the tracer)
UNNAMED = "unnamed"


def _lnm(name):
    import importlib

    return importlib.import_module("LesionNetworkMappingLib." + name)


def _rules():
    return _lnm("review")


def _studyUi():
    import importlib

    try:
        module = importlib.import_module("StudioLib.study_ui")
    except ImportError:
        return None
    return module if module.isAvailable() else None


def _session():
    studyUi = _studyUi()
    return studyUi.session() if studyUi is not None else None


def _shell():
    from . import shell

    return shell.instance()


def _draw():
    import importlib

    try:
        return importlib.import_module("StudioLib.draw")
    except ImportError:
        return None


def _figure():
    import importlib

    try:
        return importlib.import_module("StudioLib.figure")
    except ImportError:
        return None


def _toast(text, ms=3000, **kwargs):
    shell = _shell()
    if shell is not None:
        shell.toast(text, ms, **kwargs)
    else:
        logging.info(f"Studio review: {text}")


def _missing(value):
    return value is None or str(value).strip() in ("", "n/a")


def _isTesting():
    return bool(slicer.app.testingEnabled())


def _isAlive(qobject):
    try:
        qobject.objectName
        return True
    except Exception:
        return False


#
# Rater name
#


def storedRaterName():
    """``Studio/RaterName``, or "" when not set."""
    value = qt.QSettings().value(RATER_SETTING, "")
    return str(value or "").strip()


def setRaterName(name):
    """Store the rater name (fork-only setting)."""
    qt.QSettings().setValue(RATER_SETTING, str(name or "").strip())


def raterName(ask=True, parent=None):
    """The name recorded with a review: ``Studio/RaterName``, asked once when it is not set.

    Never asked under ``--testing`` or with ``ask=False`` (then :data:`UNNAMED`). Return None when the question was
    cancelled.
    """
    name = storedRaterName()
    if name:
        return name
    if not ask or _isTesting() or slicer.util.mainWindow() is None:
        return UNNAMED
    text = qt.QInputDialog.getText(parent or slicer.util.mainWindow(), _("Your name"),
                                   _("Name or initials recorded with each review (kept in this computer's settings):"))
    text = str(text or "").strip()
    if not text:
        return None
    setRaterName(text)
    return text


#
# Decisions (one entry point for Draw, Library and Classic)
#


def _descPath(path, desc):
    try:
        from . import lesionqc

        return lesionqc.descPath(path, desc)
    except Exception:
        folder, name = os.path.split(path)
        return os.path.join(folder, name.replace("desc-lesion_mask", f"desc-{desc}_mask"))


def _keepOriginal(studyObject, source, desc):
    """Copy ``source`` to its ``desc-<desc>`` sibling under lesions/ unless one exists (the first original is kept)."""
    if not source or not os.path.isfile(source):
        return None
    target = _descPath(source, desc)
    if target == source or os.path.exists(target):
        return target if os.path.exists(target) else None
    lesions = os.path.join(os.path.abspath(studyObject.path), _lnm("study").LESIONS_DIR)
    if os.path.commonpath([os.path.abspath(target), lesions]) != lesions:
        return None  # never write next to a file outside the study's lesions/
    temporary = target + f".{os.getpid()}.tmp"
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        with contextlib.suppress(OSError):
            os.remove(temporary)
    return target


def writeReviewEdit(session, sid, who):
    """Write the reviewer's edits of the open subject (Draw): keep the tracer's files, write the lesion, mark review-edit.

    Return True when the lesion was written (or there was nothing to write), False when it could not be written.
    """
    if session.current() != sid or not session.isDirty(sid):
        return True
    rules = _rules()
    studyObject = session.study
    row = studyObject.subject(sid)
    lesion = studyObject.abspath(row.get("lesion")) if not _missing(row.get("lesion")) else None
    kept = []
    try:
        for source, desc in ((lesion, "prereview"), (_descPath(lesion, "traced") if lesion else None, "prereviewtraced")):
            path = _keepOriginal(studyObject, source, desc)
            if path:
                kept.append(studyObject.relpath(path))
    except OSError as e:
        _toast(_("The tracer's lesion of {sid} could not be kept: {error}").format(sid=sid, error=e), ms=6000, level="warning")
        return False
    write = getattr(session, "_write", None)
    if write is None:
        return False
    studyUi = _studyUi()
    try:
        write(status=None, runHooks=False)
    except Exception as e:
        blocking = getattr(studyUi, "Blocking", ()) if studyUi is not None else ()
        if not (blocking and isinstance(e, blocking)):
            logging.error(f"Studio review: the edited lesion of {sid} could not be written:\n{traceback.format_exc()}")
        _toast(str(e), ms=6000, level="warning")
        return False
    studyObject.set(sid, trace_method=rules.add_tokens(studyObject.subject(sid).get("trace_method"), rules.REVIEW_EDIT_TOKEN))
    row = studyObject.subject(sid)
    if row.get("dimensionality") in ("2D", "multi2D"):
        # The 2D analysis files (extrusion) follow the edited tracing, as on Done
        with contextlib.suppress(Exception):
            from . import lesionqc

            lesionqc.regenerate(studyObject, sid, session, source="lesion")
    rules.append_audit(studyObject, {"event": "review_edit", "sid": sid, "who": who, "when": rules._now(), "kept": kept})
    return True


def transition(sid, to, comment=None, reason=None, session=None, who=None, via="review"):
    """Apply a review decision to a subject of the open study: write a reviewer's edits first (the open subject), then the
    row and the audit record, and save. A decision on a lesion that is not pending (never submitted, or changes requested)
    first submits it (→ pending, audited). Return the audit record, or None (the reason is shown in a toast).
    """
    session = session or _session()
    if session is None or sid is None or sid not in session.study:
        return None
    rules = _rules()
    who = who or raterName(ask=True)
    if not who:
        _toast(_("A reviewer name is needed to record the review"), ms=4000, level="warning")
        return None
    studyObject = session.study
    row = studyObject.subject(sid)
    submit = to in rules.DECISIONS and rules.status_of(row) in ("none", "changes")
    try:
        rules.validate_transition({**row, "review_status": "pending"} if submit else row, to, who, comment, reason)
    except rules.TransitionError as e:
        _toast(str(e), ms=5000, level="warning")
        return None
    edited = session.current() == sid and session.isDirty(sid)
    extra = {"via": via}
    if to == "rejected" and edited and _isErased(session):
        # A reviewer who erased the lesion rejects it: nothing is written (the tracer's file is kept, as when an erased
        # lesion is left), and the rejection is recorded
        edited = False
        extra["erased"] = True
    if to in rules.DECISIONS and edited and not writeReviewEdit(session, sid, who):
        return None
    try:
        if submit and rules.status_of(studyObject.subject(sid)) in ("none", "changes"):
            rules.apply_transition(studyObject, sid, "pending", who, extra={"via": via})
        record = rules.apply_transition(studyObject, sid, to, who, comment, reason, extra={**extra, "edited": bool(edited)})
    except rules.TransitionError as e:
        _toast(str(e), ms=5000, level="warning")
        return None
    _writeSidecarReview(studyObject, sid)
    session.setValues([], {})  # save soon and tell the views
    session.flush()
    return record


def _isErased(session):
    """True when the open subject's lesion has no voxels left in Draw."""
    studyUi = _studyUi()
    node = session.segmentationNode() if hasattr(session, "segmentationNode") else None
    if studyUi is None or node is None or not hasattr(studyUi, "lesionMask"):
        return False
    try:
        return studyUi.lesionMask(node) is None
    except Exception:
        return False


def _writeSidecarReview(studyObject, sid):
    """The review state in the lesion sidecar (``SlicerStudio.review``), so that a shared lesion says it was reviewed."""
    row = studyObject.subject(sid)
    if _missing(row.get("lesion")) or not os.path.isfile(studyObject.abspath(row.get("lesion")) or ""):
        return
    try:
        studyObject.write_lesion_sidecar(sid, studio={"review": _rules().sidecar_values(studyObject, row)["review"]})
    except (OSError, ValueError) as e:
        logging.warning(f"Studio review: the sidecar of {sid} keeps its earlier review state: {e}")


#
# Shared action widget: [Approve] [Request changes…] [Reject…] with an inline comment / reason form
#


class ReviewActions:
    """The three review actions with an inline form (nothing modal). ``decide(to, comment, reason) -> bool`` applies one.

    ``primaryText`` labels Approve ("Approve → next · Ctrl+Enter" in Draw, "Approve" in Library). While the Request changes
    or Reject form is open, Approve is disabled and :meth:`primary` (Ctrl+Enter in Draw) sends the form; Esc closes the
    form (an Esc overlay of the Studio shell; a comment being typed keeps Esc, USABILITY 3 rung 0).
    ``onModeChanged()`` is called when the form opens or closes (Draw relabels its primary action).
    """

    def __init__(self, decide, prefix="StudioReview", primaryText=None, onModeChanged=None):
        self._decide = decide
        self._mode = None
        self._open = True
        self._onModeChanged = onModeChanged
        self.widget = qt.QWidget()
        self.widget.setObjectName(f"{prefix}Actions")
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.approveButton = ui.primaryButton(primaryText or _("Approve"))
        self.approveButton.setObjectName(f"{prefix}ApproveButton")
        self.approveButton.connect("clicked()", self.approve)
        ui.installButtonKeys(self.approveButton)
        layout.addWidget(self.approveButton)
        row = qt.QHBoxLayout()
        row.setSpacing(6)
        self.changesButton = qt.QPushButton(_("Request changes…"))
        self.changesButton.setObjectName(f"{prefix}ChangesButton")
        self.changesButton.setToolTip(_("Send the lesion back to its tracer with a comment"))
        self.rejectButton = qt.QPushButton(_("Reject…"))
        self.rejectButton.setObjectName(f"{prefix}RejectButton")
        self.rejectButton.setToolTip(_("Exclude the lesion from analyses (include = 0), with a reason"))
        for button, mode in ((self.changesButton, "changes"), (self.rejectButton, "rejected")):
            ui.setRole(button, "secondary")
            button.setMinimumHeight(28)
            button.connect("clicked()", lambda mode=mode: self.showForm(mode))
            ui.installButtonKeys(button)
            row.addWidget(button, 1)
        layout.addLayout(row)
        # Inline form
        self.form = qt.QFrame()
        self.form.setObjectName(f"{prefix}Form")
        formLayout = qt.QVBoxLayout(self.form)
        formLayout.setContentsMargins(0, 2, 0, 0)
        formLayout.setSpacing(4)
        self.reasonCombo = qt.QComboBox()
        self.reasonCombo.setObjectName(f"{prefix}ReasonCombo")
        self.reasonCombo.addItem(_("Choose a reason…"), "")
        for reason in _rules().REJECT_REASONS:
            self.reasonCombo.addItem(_(reason.capitalize()), reason)
        formLayout.addWidget(self.reasonCombo)
        self.commentEdit = qt.QLineEdit()
        self.commentEdit.setObjectName(f"{prefix}Comment")
        self.commentEdit.setClearButtonEnabled(True)
        self.commentEdit.connect("returnPressed()", self.send)
        self.commentEdit.connect("textChanged(QString)", lambda _text: self.errorLabel.hide())
        self.reasonCombo.connect("currentIndexChanged(int)", lambda _index: self.errorLabel.hide())
        formLayout.addWidget(self.commentEdit)
        self.errorLabel = qt.QLabel()
        self.errorLabel.setObjectName(f"{prefix}FormError")
        self.errorLabel.setWordWrap(True)
        ui.bindStyleSheet(self.errorLabel, lambda: f"QLabel {{ color: {ui.token('danger').name()}; }}")
        formLayout.addWidget(self.errorLabel)
        buttons = qt.QHBoxLayout()
        buttons.setSpacing(6)
        self.sendButton = qt.QPushButton(_("Send"))
        self.sendButton.setObjectName(f"{prefix}SendButton")
        ui.setRole(self.sendButton, "secondary")
        self.sendButton.connect("clicked()", self.send)
        self.cancelButton = qt.QPushButton(_("Cancel"))
        self.cancelButton.setObjectName(f"{prefix}CancelButton")
        ui.setRole(self.cancelButton, "ghost")
        self.cancelButton.connect("clicked()", self.hideForm)
        for button in (self.sendButton, self.cancelButton):
            button.setMinimumHeight(28)
            ui.installButtonKeys(button)
        buttons.addWidget(self.sendButton, 1)
        buttons.addWidget(self.cancelButton)
        formLayout.addLayout(buttons)
        layout.addWidget(self.form)
        self.hideForm()

    def setState(self, status, enabled=True):
        """Enable the actions for a lesion in ``status`` (a decided lesion has none)."""
        self._open = bool(enabled and status not in ("approved", "rejected"))
        if not self._open:
            self.hideForm()
        self._updateButtons()

    def _updateButtons(self):
        self.approveButton.setEnabled(self._open and self._mode is None)
        for button in (self.changesButton, self.rejectButton):
            button.setEnabled(self._open)

    def setSendText(self, text):
        if str(self.sendButton.text) != text:
            self.sendButton.setText(text)
            self.sendButton.setAccessibleName(text)

    def sendText(self):
        """The label of the form's send button: "Send request" or "Reject" ("" without a form)."""
        return {"changes": _("Send request"), "rejected": _("Reject")}.get(self._mode, "")

    def setPrimaryText(self, text):
        if str(self.approveButton.text) != text:
            self.approveButton.setText(text)
            self.approveButton.setAccessibleName(text)

    def mode(self):
        return self._mode

    def showForm(self, mode):
        changed = mode != self._mode
        self._mode = mode
        self.reasonCombo.setVisible(mode == "rejected")
        self.reasonCombo.setCurrentIndex(0)
        self.commentEdit.clear()
        self.commentEdit.setPlaceholderText(_("What should change? (required)") if mode == "changes" else _("Comment (required for Other)"))
        self.setSendText(self.sendText())
        self.errorLabel.setText("")
        self.errorLabel.hide()
        self.form.show()
        self._updateButtons()
        shell = _shell()
        if shell is not None and hasattr(shell, "registerEscapeOverlay"):
            shell.registerEscapeOverlay(self.form, self.hideForm)
        (self.reasonCombo if mode == "rejected" else self.commentEdit).setFocus()
        if changed:
            self._modeChanged()

    def hideForm(self):
        changed = self._mode is not None
        focus = slicer.app.focusWidget()
        hadFocus = changed and focus is not None and self.form.isAncestorOf(focus)
        self._mode = None
        self.form.hide()
        self.errorLabel.hide()
        shell = _shell()
        if shell is not None and hasattr(shell, "unregisterEscapeOverlay"):
            shell.unregisterEscapeOverlay(self.form)
        self._updateButtons()
        if hadFocus and self.approveButton.isEnabled():
            self.approveButton.setFocus()
        if changed:
            self._modeChanged()

    def _modeChanged(self):
        if self._onModeChanged is not None:
            try:
                self._onModeChanged()
            except Exception:
                logging.error(f"Studio review: the review actions could not be updated:\n{traceback.format_exc()}")

    def primary(self):
        """The primary action (Ctrl+Enter): send the open form, else approve."""
        if self._mode is not None:
            self.send()
        else:
            self.approve()

    def approve(self):
        if self._mode is not None or not self._open:
            return  # the open form is sent with primary(); a decided lesion has no actions
        if self._decide("approved", None, None):
            self.hideForm()

    def send(self):
        if self._mode is None:
            return
        comment = str(self.commentEdit.text).strip()
        reason = self.reasonCombo.itemData(self.reasonCombo.currentIndex) if self._mode == "rejected" else None
        error = None
        if self._mode == "changes" and not comment:
            error = _("Write what should change: the tracer sees this comment")
        elif self._mode == "rejected" and not reason:
            error = _("Choose a reason")
        elif self._mode == "rejected" and reason == "other" and not comment:
            error = _("Say why in the comment")
        if error:
            self.errorLabel.setText(error)
            self.errorLabel.show()
            return
        if self._decide(self._mode, comment or None, reason or None):
            self.hideForm()

    def dispose(self):
        shell = _shell()
        if shell is not None and hasattr(shell, "unregisterEscapeOverlay"):
            with contextlib.suppress(Exception):
                shell.unregisterEscapeOverlay(self.form)
        for button in (self.approveButton, self.changesButton, self.rejectButton, self.sendButton, self.cancelButton):
            with contextlib.suppress(Exception):
                button.disconnect("clicked()")
        self._decide = None
        self._onModeChanged = None


#
# Chips (Lesion card, review notice, Library SUBJECT card)
#


def chipSpecs(row):
    """``[(text, level)]`` of the review chips of a row: state, auto-unreviewed and "Assisted 64%"."""
    rules = _rules()
    status = rules.status_of(row)
    chips = []
    level = {"pending": "warn", "approved": "ok", "changes": "warn", "rejected": "danger"}.get(status)
    if level is not None:
        chips.append((_(rules.STATE_LABELS[status]), level))
    if rules.is_auto_unreviewed(row):
        chips.append((_("auto-unreviewed"), "danger"))
    assist = row.get("assist_pct")
    # assist_pct counts every assisted tool: an automatic segmentation says so with its own chip, not as "Assisted"
    if not _missing(assist) and not rules.is_auto(row):
        with contextlib.suppress(TypeError, ValueError):
            if float(assist) > 0:
                chips.append((_("Assisted {pct}%").format(pct=f"{float(assist):.0f}"), "neutral"))
    return chips


def fillChips(layout, row):
    """Replace the chips in a horizontal layout."""
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.hide()
            widget.deleteLater()
    for text, level in chipSpecs(row):
        chip = ui.chip(text, level)
        chip.setObjectName("StudioReviewChip")
        layout.addWidget(chip)
    layout.addStretch(1)


class _ChipRow:
    """The review chip row of the Lesion card (Draw) for the open study subject."""

    def __init__(self):
        self.widget = qt.QWidget()
        self.widget.setObjectName("StudioReviewChipRow")
        self.layout = qt.QHBoxLayout(self.widget)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(4)
        self._card = None

    def refresh(self, session):
        draw = _draw()
        card = draw.lesionCard() if draw is not None and hasattr(draw, "lesionCard") else None
        sid = session.current() if session is not None else None
        row = session.study.subject(sid) if sid is not None and sid in session.study else None
        chips = chipSpecs(row) if row is not None else []
        if card is None:
            return
        if not chips:
            if self._card is not None:
                with contextlib.suppress(Exception):
                    self._card.removeChipRow(CHIP_ROW_KEY)
                self._card = None
            return
        fillChips(self.layout, row)
        if self._card is not card or self.widget.parent() is None:
            card.addChipRow(CHIP_ROW_KEY, self.widget)
            self._card = card
        # The Draw header has a Maximum vertical policy: it grows with the new row only when its geometry is updated
        parent = self.widget
        for _level in range(4):
            parent.updateGeometry()
            parent = parent.parentWidget()
            if parent is None:
                break


_chipRow = None


def _refreshChipRow(session=None):
    global _chipRow
    try:
        if _chipRow is None or not _isAlive(_chipRow.widget):
            _chipRow = _ChipRow()
        _chipRow.refresh(session or _session())
        if _state is not None and _state.active:
            _state._pinHeader()
    except Exception:
        logging.error(f"Studio review: the Lesion card chips failed:\n{traceback.format_exc()}")


#
# Study hooks (tracer side)
#

#: {sid: {"before": (modified, lesion_mtime), "target", "summary", "edited", "who"}} between a Done hook and the write
_pendingDone = {}


def doneHook(sid, labelmap, ijkToRas, ctx):
    """Done hook: assisted provenance, auto:<tool> tokens and the review state after Done (applied once written)."""
    import numpy as np

    rules = _rules()
    session = ctx.get("session") or _session()
    if session is None:
        return {}
    studyObject = session.study
    row = studyObject.subject(sid)
    draw = _draw()
    counts = {}
    if draw is not None and hasattr(draw, "assistCounts"):
        with contextlib.suppress(Exception):
            counts = draw.assistCounts(sid) or {}
    total = int(np.count_nonzero(labelmap)) if labelmap is not None else None
    summary = rules.assisted_summary(counts, total)
    extras = {}
    method = rules.method_after_assist(row.get("trace_method"), summary)
    if summary["auto"]:
        # Session._write computes the other tokens; an automatic tool's token is added here (the extras replace them)
        target = ctx.get("target")
        derivatives = os.path.join(studyObject.path, _lnm("study").DERIVATIVES_DIR)
        current = studyObject.abspath(row.get("lesion")) if not _missing(row.get("lesion")) else None
        if current and os.path.commonpath([os.path.abspath(current), os.path.abspath(derivatives)]) == os.path.abspath(derivatives) and target != current:
            method = rules.add_tokens(method, rules.REVIEW_EDIT_TOKEN)
        extras["trace_method"] = method
    edited = bool(session.isDirty(sid)) if hasattr(session, "isDirty") else True
    target = rules.status_after_done(studyObject, row, method, edited)
    _pendingDone[sid] = {"before": (row.get("modified"), row.get("lesion_mtime")), "target": target, "summary": summary, "edited": edited,
                         "time": time.time()}
    qt.QTimer.singleShot(0, lambda: _afterDone(sid))
    return extras


def _afterDone(sid):
    """After Done wrote the lesion: the review transition (audited) and the sidecar provenance."""
    pending = _pendingDone.pop(sid, None)
    session = _session()
    if pending is None or session is None or sid not in session.study:
        return
    rules = _rules()
    studyObject = session.study
    row = studyObject.subject(sid)
    if (row.get("modified"), row.get("lesion_mtime")) == pending["before"]:
        return  # Done was blocked: nothing was written
    try:
        if pending["target"] and rules.can_transition(rules.status_of(row), pending["target"]):
            rules.apply_transition(studyObject, sid, pending["target"], storedRaterName() or UNNAMED, extra={"via": "done", "edited": pending["edited"]})
            row = studyObject.subject(sid)
        values = rules.sidecar_values(studyObject, row, pending["summary"])
        with contextlib.suppress(ValueError):
            studyObject.write_lesion_sidecar(sid, studio=values)
    except Exception:
        logging.error(f"Studio review: the review state of {sid} could not be recorded:\n{traceback.format_exc()}")
    session.setValues([], {})
    session.flush()


def openHook(sid, ctx):
    """A subject was opened: the review state follows (or ends), and the Lesion card chips show its review."""
    state = _state
    if state is not None and state.active:
        state.onSubjectOpened(sid)
    qt.QTimer.singleShot(0, _refreshChipRow)


def _onStudyChanged(session=None):
    session = session if session is not None else _session()
    if session is None:
        if _state is not None:
            _state.exit(quiet=True)
        return
    session.removeDoneHook(doneHook)
    session.addDoneHook(doneHook, order=DONE_HOOK_ORDER)
    session.removeOpenHook(openHook)
    session.addOpenHook(openHook)
    if _state is not None and _state.session is not session:
        _state.exit(quiet=True)
    elif _state is not None and _state.active:
        _state.refreshNotice()
    _refreshChipRow(session)


_installed = [False]


def installStudyHooks():
    """Follow the study session: the Done and open hooks of the open study (idempotent)."""
    studyUi = _studyUi()
    if studyUi is None:
        return False
    if not _installed[0]:
        studyUi.addListener(_onStudyChanged)
        _installed[0] = True
    if studyUi.session() is not None:
        _onStudyChanged(studyUi.session())
    return True


#
# The review state of Draw
#


def queuePredicate(queue):
    rules = _rules()
    return {QUEUE_NEEDS_REVIEW: rules.needs_review, QUEUE_CHANGES: rules.changes_requested, QUEUE_AUTO: rules.is_auto_unreviewed}.get(queue, rules.needs_review)


def queueLabel(queue):
    return {QUEUE_NEEDS_REVIEW: _("Needs review"), QUEUE_CHANGES: _("Changes requested"), QUEUE_AUTO: _("Auto-unreviewed")}.get(queue, _("Needs review"))


def figureFitFiles(studyObject, sid):
    """The figure fit files of a subject (``sources/<record_id or sid>/fig-<k>_fit.json`` naming it), sorted."""
    row = studyObject.subject(sid)
    folders = [value for value in (row.get("record_id"), sid) if not _missing(value)]
    found = []
    for folderName in dict.fromkeys(folders):
        folder = os.path.join(studyObject.path, "sources", str(folderName))
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if not (name.startswith("fig-") and name.endswith("_fit.json")):
                continue
            path = os.path.join(folder, name)
            owner = None
            with contextlib.suppress(Exception):
                import json

                with open(path, encoding="utf-8") as f:
                    owner = json.load(f).get("participant_id")
            if owner in (None, sid):
                found.append(path)
    return found


class _ReviewFooter:
    """The Draw footer while reviewing: the duck-typed footer of ``draw._DrawSession`` (``widget``, ``update()``, ``dispose()``).

    It holds Draw's own footer and gives it back when the review ends; Draw disposes it when Draw is left.
    """

    def __init__(self, state, original):
        self.stateRef = weakref.ref(state)
        self.original = original
        self.widget = qt.QWidget()
        self.widget.setObjectName("StudioReviewFooter")
        ui.bindStyleSheet(self.widget, lambda: (
            f"QWidget#StudioReviewFooter {{ background-color: {ui.token('window').name()}; "
            f"border-top: 1px solid {ui.token('hairline').name()}; }}"))
        self.widget.setAttribute(qt.Qt.WA_StyledBackground, True)
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(12, 8, 12, 10)
        layout.setSpacing(4)
        self.actions = ReviewActions(self._decide, "StudioReview", onModeChanged=self._onModeChanged)
        layout.addWidget(self.actions.widget)

    def _decide(self, to, comment, reason):
        state = self.stateRef()
        return state.decide(to, comment, reason) if state is not None else False

    def _primary(self):
        """Ctrl+Enter: approve, or send the open Request changes / Reject form (never approve over an open form)."""
        if self.actions is not None:
            self.actions.primary()

    def _onModeChanged(self):
        # The form opened or closed: Ctrl+Enter and its label follow (the shell's primary action is set from update())
        state = self.stateRef()
        drawSession = state._drawSession() if state is not None else None
        if drawSession is not None and drawSession.footer is self:
            drawSession.refreshPanel()

    def update(self):
        """Labels; return (primary label, primary callback) for Ctrl+Enter."""
        from . import draw

        if self.actions is None:
            return "", None
        key = ui.keyText(draw.PRIMARY_SHORTCUT)
        state = self.stateRef()
        last = state is not None and state.isLast()
        session = _session()
        sid = state.current if state is not None else None
        row = session.study.subject(sid) if session is not None and sid in session.study else {}
        self.actions.setState(_rules().status_of(row), enabled=bool(row))
        approveText = _("Approve") if last else _("Approve → next")
        if self.actions.mode() is not None:
            # The open form owns Ctrl+Enter: its send button shows the key, Approve does not
            label = f"{self.actions.sendText()} · {key}"
            self.actions.setPrimaryText(approveText)
            self.actions.setSendText(label)
        else:
            label = f"{approveText} · {key}"
            self.actions.setPrimaryText(label)
        self.actions.approveButton.setToolTip(ui.tooltip(_("Approve"), draw.PRIMARY_SHORTCUT, _("the lesion may be used in analyses; open the next one to review")))
        return label, self._primary

    def dispose(self):
        """Draw is left (or the review ends): Draw's footer is disposed with it, and the review ends."""
        state = self.stateRef()
        original, self.original = self.original, None
        if original is not None:
            with contextlib.suppress(Exception):
                original.dispose()
        if state is not None and state.footer is self:
            state.footer = None
            state.exit(quiet=True, fromFooter=True)
        self._discard()

    def _discard(self):
        if self.actions is not None:
            self.actions.dispose()
            self.actions = None
        if self.widget is not None:
            for target in (self.widget, *self.widget.findChildren("QWidget")):
                ui.bindStyleSheet(target, None)
            self.widget.hide()
            self.widget.deleteLater()
            self.widget = None


class _LayoutWatcher(qt.QObject):
    """Calls ``callback`` (once per event loop iteration) when the watched widget's layout asks for a new size."""

    def __init__(self, callback):
        qt.QObject.__init__(self)
        self.callback = callback
        self._scheduled = False

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.LayoutRequest and not self._scheduled:
            self._scheduled = True
            qt.QTimer.singleShot(0, self._run)
        return False

    def _run(self):
        self._scheduled = False
        self.callback()


class ReviewState:
    """One pass of review through a queue (see the module docstring)."""

    def __init__(self, session, shell, sids, queue):
        self.session = session
        self.shell = shell
        self.queue = queue
        self.sids = list(sids)
        self.reviewed = []
        self.current = None
        self.expected = None
        self.active = False
        self.footer = None
        self.notice = None
        self._display = None  # (segmentation node ID, fill 2D, outline 2D, outline opacity)
        self._figureShown = False
        self._header = None  # (Draw header widget, its minimum height before the review)

    # Navigation

    def remaining(self):
        """The subjects of the queue still waiting, in order (the current one first when it waits)."""
        predicate = queuePredicate(self.queue)
        out = []
        for sid in self.sids:
            if sid in self.reviewed or sid not in self.session.study:
                continue
            with contextlib.suppress(Exception):
                if predicate(self.session.study.subject(sid)) or sid == self.current:
                    out.append(sid)
        return out

    def position(self):
        """(1-based position of the current subject, total) for "2 of 5"."""
        total = len(self.sids)
        index = self.sids.index(self.current) + 1 if self.current in self.sids else 0
        return index, total

    def isLast(self):
        return not [sid for sid in self.remaining() if sid != self.current]

    def open(self, sid):
        """Open a subject in Draw in the review state. Return True if it is shown."""
        self.expected = sid
        self.active = True
        try:
            if not self.session.open(sid, switchToDraw=False):
                return False
        finally:
            self.expected = None
        self.current = sid
        if self.shell is not None and self.shell.currentWorkspace() != "draw":
            self.shell.switchTo("draw")
        self.install()
        return True

    def onSubjectOpened(self, sid):
        """Open hook: a subject opened by the review, or by Alt+→ / the switcher while reviewing."""
        if sid == self.expected:
            return
        row = self.session.study.subject(sid) if sid in self.session.study else None
        if row is not None and queuePredicate(self.queue)(row):
            self.current = sid
            if sid not in self.sids:
                self.sids.append(sid)
            qt.QTimer.singleShot(0, self.install)
        else:
            qt.QTimer.singleShot(0, lambda: self.exit(message=_("Review ended: {sid} is not waiting for review").format(sid=sid)))

    def decide(self, to, comment=None, reason=None):
        """A decision on the current subject, then the next one of the queue. Return True if it was recorded."""
        sid = self.current
        if sid is None:
            return False
        record = transition(sid, to, comment, reason, session=self.session)
        if record is None:
            return False
        self.reviewed.append(sid)
        label = {"approved": _("Approved {sid}"), "changes": _("Changes requested for {sid}"), "rejected": _("Rejected {sid}")}[to].format(sid=sid)
        following = [other for other in self.remaining() if other != sid]
        if not following:
            done = len(self.reviewed)
            self.exit(message=_("{label} · review queue done ({n} reviewed)").format(label=label, n=done))
            return True
        _toast(label, ms=2000)
        self.open(following[0])
        return True

    # Presentation

    def install(self):
        """Outline-only mask, the source, the footer and the notice for the current subject."""
        if not self.active or self.current is None:
            return
        self._outlineOnly()
        self._showSource()
        self._installFooter()
        self.refreshNotice()
        _refreshChipRow(self.session)
        qt.QTimer.singleShot(0, self._pinHeader)

    def _pinHeader(self):
        """Keep Draw's header (the Lesion card and its chip rows) at its full height while the notice and the review footer
        take room: its vertical policy is Maximum, so a short inspector would squeeze its rows over each other instead of
        scrolling. The minimum height is given back when the review ends.
        """
        if not self.active:
            return
        draw = _draw()
        card = draw.lesionCard() if draw is not None and hasattr(draw, "lesionCard") else None
        header = card.widget.parentWidget() if card is not None and _isAlive(card.widget) else None
        if header is None:
            return
        if self._header is None or self._header[0] is not header:
            self._unpinHeader()
            watcher = _LayoutWatcher(self._pinHeader)
            header.installEventFilter(watcher)
            self._header = (header, header.minimumHeight, watcher)
        layout = header.layout()
        if layout is not None:
            layout.activate()
        # The height the header needs at its width (word-wrapped labels), not its size hint (computed for another width)
        needed = header.heightForWidth(header.width) if header.hasHeightForWidth() and header.width > 0 else -1
        height = max(self._header[1], needed if needed > 0 else header.sizeHint.height())
        if header.minimumHeight != height:
            header.setMinimumHeight(height)

    def _unpinHeader(self):
        saved, self._header = self._header, None
        if saved is not None and _isAlive(saved[0]):
            saved[0].removeEventFilter(saved[2])
            saved[0].setMinimumHeight(saved[1])

    def _outlineOnly(self):
        node = self.session.segmentationNode()
        displayNode = node.GetDisplayNode() if node is not None else None
        if displayNode is None:
            return
        if self._display is not None and self._display[0] == node.GetID():
            return
        self._display = (node.GetID(), displayNode.GetVisibility2DFill(), displayNode.GetVisibility2DOutline(), displayNode.GetOpacity2DOutline())
        displayNode.SetVisibility2DFill(False)
        displayNode.SetVisibility2DOutline(True)
        displayNode.SetOpacity2DOutline(1.0)

    def _restoreDisplay(self):
        saved, self._display = self._display, None
        if saved is None:
            return
        node = slicer.mrmlScene.GetNodeByID(saved[0])
        displayNode = node.GetDisplayNode() if node is not None else None
        if displayNode is not None:
            displayNode.SetVisibility2DFill(saved[1])
            displayNode.SetVisibility2DOutline(saved[2])
            displayNode.SetOpacity2DOutline(saved[3])

    def _showSource(self):
        self.sourceText = None
        fits = figureFitFiles(self.session.study, self.current)
        figure = _figure()
        if fits and figure is not None:
            try:
                figure.showSource(fits[0], self.shell)
                self._figureShown = True
                self.sourceText = _("Source: {name} (figure fit)").format(name=os.path.basename(fits[0])[:-len("_fit.json")])
                return
            except Exception as e:
                logging.warning(f"Studio review: the figure of {self.current} could not be shown: {e}")
                self.sourceText = _("Source figure not shown: {error}").format(error=e)
                return
        row = self.session.study.subject(self.current)
        if not _missing(row.get("scan")):
            self.sourceText = _("Source: {name}").format(name=os.path.basename(str(row["scan"])))

    def _drawSession(self):
        draw = _draw()
        if draw is None or self.shell is None:
            return None
        drawSession = draw.session(self.shell)
        return drawSession if getattr(drawSession, "active", False) else None

    def _installFooter(self):
        drawSession = self._drawSession()
        if drawSession is None:
            return
        if not isinstance(drawSession.footer, _ReviewFooter):
            self.footer = _ReviewFooter(self, drawSession.footer)
            drawSession.footer = self.footer
            self.shell.setInspectorFooter(self.footer.widget)
        drawSession.refreshPanel()

    def _restoreFooter(self):
        footer, self.footer = self.footer, None
        drawSession = self._drawSession()
        if footer is None:
            return
        original, footer.original = footer.original, None
        if drawSession is not None and drawSession.footer is footer:
            drawSession.footer = original
            if original is not None and self.shell is not None:
                self.shell.setInspectorFooter(original.widget)
            footer.stateRef = lambda: None
            footer._discard()
            drawSession.refreshPanel()
        elif original is not None:
            with contextlib.suppress(Exception):
                original.dispose()

    def _buildNotice(self):
        notice = qt.QFrame()
        notice.setObjectName("StudioReviewNotice")
        notice.setProperty("studioNoticeLine", True)
        notice.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        ui.bindStyleSheet(notice, lambda: (
            f"QFrame#StudioReviewNotice {{ background-color: {ui.token('accent-subtle').name()}; border: none; "
            f"border-bottom: 1px solid {ui.token('hairline').name()}; }}"
            f"QFrame#StudioReviewNotice QLabel {{ background: transparent; color: {ui.token('text').name()}; }}"))
        layout = qt.QVBoxLayout(notice)
        layout.setContentsMargins(12, 6, 8, 6)
        layout.setSpacing(4)
        top = qt.QHBoxLayout()
        self.titleLabel = qt.QLabel()
        self.titleLabel.setObjectName("StudioReviewTitle")
        self.titleLabel.setWordWrap(True)
        font = qt.QFont(self.titleLabel.font)
        font.setWeight(qt.QFont.DemiBold)
        self.titleLabel.setFont(font)
        top.addWidget(self.titleLabel, 1)
        self.exitButton = qt.QToolButton()
        self.exitButton.setObjectName("StudioReviewExitButton")
        self.exitButton.setText(_("Exit review"))
        self.exitButton.setAutoRaise(True)
        self.exitButton.setToolTip(_("Back to tracing in Draw: the decisions made so far are kept"))
        self.exitButton.connect("clicked()", self.exit)
        ui.setRole(self.exitButton, "ghost")
        ui.installButtonKeys(self.exitButton)
        top.addWidget(self.exitButton, 0, qt.Qt.AlignTop)
        layout.addLayout(top)
        self.chipLayout = qt.QHBoxLayout()
        self.chipLayout.setSpacing(4)
        layout.addLayout(self.chipLayout)
        self.detailLabel = qt.QLabel()
        self.detailLabel.setObjectName("StudioReviewDetail")
        self.detailLabel.setWordWrap(True)
        self.detailLabel.setTextFormat(qt.Qt.PlainText)
        layout.addWidget(self.detailLabel)
        self.notice = notice

    def refreshNotice(self):
        if not self.active or self.current is None or self.shell is None:
            return
        if self.notice is None or not _isAlive(self.notice):
            self._buildNotice()
        rules = _rules()
        studyObject = self.session.study
        row = studyObject.subject(self.current)
        index, total = self.position()
        self.titleLabel.setText(_("Review · {sid} · {index} of {total} ({queue})").format(sid=self.current, index=index, total=total, queue=queueLabel(self.queue)))
        fillChips(self.chipLayout, row)
        lines = []
        tracer = next((row.get(key) for key in ("tracer", "rater") if not _missing(row.get(key))), None)
        method = row.get("trace_method")
        traced = _("Traced by {who}").format(who=tracer) if tracer else _("Tracer not recorded")
        lines.append(f"{traced} · " + (str(method) if not _missing(method) else _("method not recorded")))
        if getattr(self, "sourceText", None):
            lines.append(self.sourceText)
        if rules.status_of(row) in ("pending", "changes") and not _missing(row.get("review_comment")):
            lines.append(_("Earlier review ({who}): {comment}").format(who=row.get("reviewer") if not _missing(row.get("reviewer")) else "?", comment=row["review_comment"]))
        if not _missing(row.get("notes")):
            lines.append(_("Notes: {notes}").format(notes=row["notes"]))
        if studyObject.blind_tracing:
            lines.append(_("Blind review: group, scores and symptoms are hidden; network maps are not shown"))
        self.detailLabel.setText("\n".join(lines))
        if self.shell.inspectorNotice() is not self.notice:
            self.shell.setInspectorNotice(self.notice, NOTICE_KEY, "draw")
        qt.QTimer.singleShot(0, self._pinHeader)

    def exit(self, message=None, quiet=False, fromFooter=False):
        """End the review: Draw's footer, the fill of the mask and the views come back. The decisions are kept."""
        global _state, _paused
        if _state is self:
            _state = None
        wasActive = self.active
        self.active = False
        if not fromFooter:
            self._restoreFooter()
        self._unpinHeader()
        self._restoreDisplay()
        if self._figureShown:
            figure = _figure()
            if figure is not None:
                with contextlib.suppress(Exception):
                    figure.closeFigure(removeNodes=True)
            self._figureShown = False
        if self.shell is not None:
            with contextlib.suppress(Exception):
                self.shell.clearInspectorNotice(NOTICE_KEY)
        if self.notice is not None:
            for target in (self.notice, *self.notice.findChildren("QWidget")):
                ui.bindStyleSheet(target, None)
            self.notice.deleteLater()
            self.notice = None
        left = [sid for sid in self.remaining() if sid not in self.reviewed]
        _paused = (self.session, self.queue, left) if left and wasActive else None
        _refreshChipRow(self.session)
        _notifyLibrary(self.session)
        if message:
            _toast(message, ms=4000, level="consequence" if not quiet else "info")
        elif wasActive and not quiet:
            _toast(_("Review ended"), ms=2000)


_state = None
#: (session, queue, sids) of a review that ended before its queue did ("Resume review")
_paused = None


def _notifyLibrary(session):
    studyUi = _studyUi()
    if studyUi is not None and session is not None:
        with contextlib.suppress(Exception):
            session.setValues([], {})


def state():
    """The active review state, or None."""
    return _state if _state is not None and _state.active else None


def isReviewing():
    return state() is not None


def pausedQueue():
    """``(queue, [sids])`` of a review that can be resumed, or None."""
    session = _session()
    if _paused is None or _paused[0] is not session or session is None:
        return None
    predicate = queuePredicate(_paused[1])
    left = [sid for sid in _paused[2] if sid in session.study and predicate(session.study.subject(sid))]
    return (_paused[1], left) if left else None


def queueRows(session, queue):
    predicate = queuePredicate(queue)
    return [row for row in session.study.subjects if predicate(row)]


def startReview(queue=QUEUE_NEEDS_REVIEW, sid=None, sids=None, shell=None):
    """Open the review state of Draw on a queue (or from ``sid`` on). Return the ``ReviewState``, or None.

    Studio only: in Classic, the Studio module's SUBJECT card offers the same decisions.
    """
    global _state, _paused
    session = _session()
    shell = shell or _shell()
    if session is None:
        _toast(_("Open a study first"))
        return None
    if shell is None:
        logging.info("Studio review: the review state of Draw needs the Studio interface")
        return None
    order = list(sids) if sids else [row["participant_id"] for row in queueRows(session, queue)]
    if sid is not None:
        order = [sid] + [other for other in order if other != sid]
    if not order:
        _toast(_("Nothing is waiting in {queue}").format(queue=queueLabel(queue)), ms=3000)
        return None
    if _state is not None:
        _state.exit(quiet=True)
    _paused = None
    installStudyHooks()
    current = ReviewState(session, shell, order, queue)
    _state = current
    if not current.open(order[0]):
        current.exit(quiet=True)
        return None
    return current


def resumeReview(shell=None):
    paused = pausedQueue()
    if paused is None:
        return None
    return startReview(paused[0], sids=paused[1], shell=shell)


def exitReview():
    current = state()
    if current is not None:
        current.exit()
        return True
    return False

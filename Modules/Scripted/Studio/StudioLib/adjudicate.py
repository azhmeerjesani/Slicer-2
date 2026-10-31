"""Blind second tracing and adjudication in Draw (L-44; SPEC_LNM_ADDENDUM 3.10, 7.2-7.3, RW-11; USABILITY 2, 3, 5).

Both are *states* of Draw, like review (``StudioLib.review``), not workspaces. Each has an inspector notice ("where am
I", with **Exit**), a footer in place of "Done → next" (Ctrl+Enter), and ends at the end of its queue, on Exit, when
Draw is left or when a subject outside the queue is opened (Alt+←/→, the switcher). The decisions are kept.

**Second tracing** (``startSecondTracing()``; Library "Second tracing N →", Search, the case strip's "Second tracing"
queue): the subjects assigned a second tracing (``LesionNetworkMappingLib.agreement.assign``) open one by one *blind*:

- the primary lesion is never loaded. The study session first writes and closes its own subject (nothing of it stays in
  the scene); this state then loads the source and an empty lesion of its own (or the second rater's earlier tracing):
  the scan the subject was traced on, or, for a figure tracing, the study template with the figure: rebuilt from the first
  tracing's fit (``trace_only``) or the crop alone, for the second rater's own crop and fit (``full``);
- a subject the case strip opens in the "Second tracing" queue is handed over before anything is drawn: its lesion is
  hidden in the same event and its nodes removed;
- **Save second tracing → next** (Ctrl+Enter) writes ``lesions/rater-<initials>/<sub>_space-<sp>_desc-lesion_mask.nii.gz``
  and its sidecar (``agreement.save_second_tracing``: blinding and the intra-rater interval of 7 days are checked there);
  the agreement is not shown to the second rater;
- the tracing in progress is journaled 2 s after the last edit (``journal.SecondTracingJournal``,
  ``.studio/journal/rater-<initials>/<sub>.nii.gz``): after a crash, the subject reopened in this state offers it in the
  state's notice (Recover | Discard), and the autosave names the state and its subject, so that the recovery card
  reopens this second tracing (``study_ui._recover``), never the first tracing.

**Adjudication** (``startAdjudication()``; Library "Adjudicate N →", Search, the case strip's "Adjudicate" queue): the
pairs whose Dice is below the study's threshold (0.5 by default) show both tracings as outlines in two colours, A (the
first tracing, red) and B (the second, blue). **A**, **B**, **Union**, **Intersection** write the consensus at once;
**Edit…** adds a green "Consensus" segment (the union) to edit and **Save consensus → next** (Ctrl+Enter) writes it.
The consensus becomes the subject's ``desc-lesion`` with ``rater`` = consensus; the first tracing is kept as
``desc-preadjudication`` and the second stays in its rater folder; every decision is audited and has an Undo in its
toast (``agreement.adjudicate``, ``agreement.undo_adjudication``).

The rater's name is ``Studio/RaterName`` (``StudioLib.review.raterName``); the first Done of a lesion records it as the
lesion's ``tracer`` (``doneHook``), which blinding and intra-rater mode check. Work kept in passing (Skip, Exit, Alt+←/→,
leaving Draw) is a second tracing *in progress*: it stays in the queue and counts in no agreement until it is saved.
Network maps are never shown in Draw.
Studio only: Classic has the Library cards (``StudioLib.libcard_agreement``) but no Draw states.
"""

import contextlib
import logging
import os
import traceback
import weakref

import numpy as np
import qt
import slicer
import vtk
from slicer.i18n import tr as _

from . import review, ui

NOTICE_KEY = "studio-agreement"
#: Order of the Done hook that records the tracer (``StudySession.addDoneHook``)
DONE_HOOK_ORDER = 60
QUEUE_SECOND = "second_tracing"
QUEUE_ADJUDICATE = "adjudicate"
#: Node attribute of the nodes of these states (a participant id, never a path)
NODE_ATTRIBUTE = "Studio.AgreementSubject"
#: Outline colours of the two tracings and of the consensus being edited
COLOR_A = (0.949, 0.329, 0.357)  # the lesion red of Draw (#F2545B)
COLOR_B = (0.231, 0.510, 0.965)  # blue (#3B82F6)
COLOR_CONSENSUS = (0.133, 0.773, 0.369)  # green (#22C55E)
CHOICE_LABELS = {"A": "A", "B": "B", "union": "Union", "intersection": "Intersection", "edit": "Edit…"}


def _rules():
    import importlib

    return importlib.import_module("LesionNetworkMappingLib.agreement")


def _studyUi():
    return review._studyUi()


def _session():
    return review._session()


def _shell():
    return review._shell()


def _importer():
    from . import importer

    return importer


def _toast(text, ms=3000, **kwargs):
    review._toast(text, ms, **kwargs)


def _missing(value):
    return value is None or str(value).strip() in ("", "n/a")


def _isAlive(qobject):
    return review._isAlive(qobject)


def _vtkMatrix(array):
    matrix = vtk.vtkMatrix4x4()
    for row in range(4):
        for column in range(4):
            matrix.SetElement(row, column, float(array[row][column]))
    return matrix


def threshold(session=None):
    session = session or _session()
    return _rules().dice_threshold(session.study) if session is not None else _rules().DICE_THRESHOLD


def secondTodo(row):
    return _rules().second_tracing_todo(row)


def needsAdjudication(row):
    return _rules().needs_adjudication(row, threshold())


def queueRows(session, queue):
    predicate = secondTodo if queue == QUEUE_SECOND else needsAdjudication
    return [row for row in session.study.subjects if predicate(row)]


#
# Scene helpers
#


def releaseSession(session):
    """Write and close the study session's open subject, so that nothing of it stays in the scene. Return False to stay
    (its lesion could not be written; the session says why).
    """
    if session.current() is None:
        return True
    if not session._leave(write=True):
        return False
    session._removeSubjectNodes()
    session._current = None
    session._gridReference = None
    session._openStamp = None
    session._dirty = False
    session._markedDirty = False
    return True


def _hideSegmentation(node):
    displayNode = node.GetDisplayNode() if node is not None else None
    if displayNode is not None:
        displayNode.SetVisibility(False)


def _removeNodes(nodeIds, keep=()):
    for nodeId in nodeIds:
        if nodeId and nodeId not in keep:
            node = slicer.mrmlScene.GetNodeByID(nodeId)
            if node is not None:
                slicer.mrmlScene.RemoveNode(node)


def _labelmapNode(array_ijk, affine, name):
    """A hidden label map volume of an [i, j, k] mask with its file affine (for import into a segmentation)."""
    node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode", slicer.mrmlScene.GenerateUniqueName(name))
    node.SetHideFromEditors(True)
    slicer.util.updateVolumeFromArray(node, np.ascontiguousarray(np.transpose(np.asarray(array_ijk) != 0, (2, 1, 0)).astype(np.uint8)))
    node.SetIJKToRASMatrix(_vtkMatrix(affine))
    return node


def _addSegment(segmentationNode, array_ijk, affine, name, color):
    """Add a segment from a mask (its own grid). Return the segment id."""
    labelmap = _labelmapNode(array_ijk, affine, "StudioAgreementImport")
    try:
        segmentation = segmentationNode.GetSegmentation()
        before = {segmentation.GetNthSegmentID(index) for index in range(segmentation.GetNumberOfSegments())}
        if not slicer.modules.segmentations.logic().ImportLabelmapToSegmentationNode(labelmap, segmentationNode):
            raise RuntimeError(_("The tracing could not be shown"))
        added = [segmentation.GetNthSegmentID(index) for index in range(segmentation.GetNumberOfSegments())
                 if segmentation.GetNthSegmentID(index) not in before]
        if not added:
            # An empty mask: an empty segment keeps its place
            segmentId = segmentation.AddEmptySegment("", name, color)
        else:
            segmentId = added[0]
        segment = segmentation.GetSegment(segmentId)
        segment.SetName(name)
        segment.SetColor(*color)
        return segmentId
    finally:
        slicer.mrmlScene.RemoveNode(labelmap)


def _segmentMask(segmentationNode, segmentId):
    """``(mask_kji uint8, ijkToRAS)`` of one segment cropped to its voxels, or None when it is empty."""
    studyUi = _studyUi()
    data = studyUi._internalArray(segmentationNode, segmentId)
    if data is None:
        return None
    cropped = studyUi._cropped(*data)
    return None if cropped is None else (cropped[0].astype(np.uint8), cropped[1])


#
# Footer and notice (shared by both states)
#


class _StateFooter:
    """The Draw footer of a state: the duck-typed footer of ``draw._DrawSession`` (``widget``, ``update()``, ``dispose()``).

    It holds Draw's own footer and gives it back when the state ends; Draw disposes it when Draw is left.
    """

    def __init__(self, state, original):
        self.stateRef = weakref.ref(state)
        self.original = original
        self.widget = qt.QWidget()
        self.widget.setObjectName("StudioAgreementFooter")
        ui.bindStyleSheet(self.widget, lambda: (
            f"QWidget#StudioAgreementFooter {{ background-color: {ui.token('window').name()}; "
            f"border-top: 1px solid {ui.token('hairline').name()}; }}"))
        self.widget.setAttribute(qt.Qt.WA_StyledBackground, True)
        self.layout = qt.QVBoxLayout(self.widget)
        self.layout.setContentsMargins(12, 8, 12, 10)
        self.layout.setSpacing(6)
        state.buildFooter(self)

    def update(self):
        """Labels; return (primary label, primary callback) for Ctrl+Enter."""
        state = self.stateRef()
        if state is None:
            return "", None
        return state.updateFooter(self)

    def dispose(self):
        """Draw is left (or the state ends): Draw's footer is disposed with it, and the state ends."""
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
        if self.widget is not None:
            for target in (self.widget, *self.widget.findChildren("QWidget")):
                ui.bindStyleSheet(target, None)
            self.widget.hide()
            self.widget.deleteLater()
            self.widget = None


class _AgreementState:
    """A pass through a queue in Draw (second tracing or adjudication; see the module docstring)."""

    queue = None
    exitText = ""

    def __init__(self, session, shell, sids):
        self.session = session
        self.shell = shell
        self.sids = list(sids)
        self.finished = []
        self.current = None
        self.active = False
        self.footer = None
        self.notice = None
        self._nodeIds = []
        self._volumeId = None
        self._figureShown = False
        self.lines = []

    # Navigation

    def predicate(self, row):
        return secondTodo(row) if self.queue == QUEUE_SECOND else needsAdjudication(row)

    def remaining(self):
        out = []
        for sid in self.sids:
            if sid in self.finished or sid not in self.session.study:
                continue
            with contextlib.suppress(Exception):
                if self.predicate(self.session.study.subject(sid)) or sid == self.current:
                    out.append(sid)
        return out

    def position(self):
        index = self.sids.index(self.current) + 1 if self.current in self.sids else 0
        return index, len(self.sids)

    def following(self):
        return [sid for sid in self.remaining() if sid != self.current]

    def open(self, sid):
        """Open a subject in this state. Return True if it is shown."""
        if not releaseSession(self.session):
            return False
        self.active = True
        try:
            with _renderPaused():
                self._clearNodes()
                self.current = sid
                self.load(sid)
        except Exception as e:
            logging.error(f"Studio agreement: {sid} could not be opened:\n{traceback.format_exc()}")
            _toast(_("{sid} could not be opened: {error}").format(sid=sid, error=e), ms=6000, level="warning")
            self.current = None
            return False
        if self.shell is not None:
            self.shell.setBreadcrumb(self.session.study.name, f"{sid} · {self.title()}")
            if self.shell.currentWorkspace() != "draw":
                self.shell.switchTo("draw")
        self.install()
        _autosaveSoon(self.title())
        return True

    def openNext(self, message):
        following = self.following()
        if not following:
            self.exit(message=_("{message} · {queue} done").format(message=message, queue=self.title()))
            return
        _toast(message, ms=2000)
        self.open(following[0])

    def onSubjectOpened(self, sid):
        """The study session opened a subject (Alt+←/→, the switcher, the case strip) while this state is active."""
        self.saveQuietly()
        row = self.session.study.subject(sid) if sid in self.session.study else None
        if row is not None and self.predicate(row):
            _hideSegmentation(self.session.segmentationNode())
            if sid not in self.sids:
                self.sids.append(sid)
            qt.QTimer.singleShot(0, lambda: self.open(sid))
        else:
            self.exit(message=_("{title} ended: {sid} is not in its queue").format(title=self.title(), sid=sid), keepScene=True)

    # Subclass parts

    def title(self):
        return ""

    def load(self, sid):
        raise NotImplementedError

    def buildFooter(self, footer):
        raise NotImplementedError

    def updateFooter(self, footer):
        raise NotImplementedError

    def saveQuietly(self):
        """Keep work in progress before the subject goes (second tracing: written when not empty)."""

    # Scene

    def _loadSource(self, sid, figure=None):
        """The volume to trace on: the subject's scan, else the study template. Return (volume node, its NIfTI file)."""
        studyObject = self.session.study
        row = studyObject.subject(sid)
        scan = studyObject.abspath(row.get("scan")) if not _missing(row.get("scan")) else None
        importer = _importer()
        if scan and os.path.isfile(scan) and not figure:
            with importer._ownLoad():
                volume = slicer.util.loadVolume(scan, {"singleFile": True, "show": False})
            volume.SetName(importer._baseName(scan))
            volume.SetAttribute(NODE_ATTRIBUTE, sid)
            self._nodeIds.append(volume.GetID())
            return volume, scan
        if scan and not figure:
            # A scan named but not on disk (moved, renamed, a drive not connected) is never replaced by the template
            raise FileNotFoundError(_("the scan {file} is missing").format(file=row.get("scan")))
        volume = self.session.templateNode()
        studyUi = _studyUi()
        reference = self.session._templateFiles.get(volume.GetID()) or studyUi._storagePath(volume)
        return volume, reference

    def _clearNodes(self):
        keep = set()
        with contextlib.suppress(Exception):
            template = self.session.templateNode() if self.session._templateCache else None
            if template is not None:
                keep.add(template.GetID())
        widget = getattr(slicer.modules, "SegmentEditorWidget", None)
        editor = widget.editor if widget is not None else None
        if editor is not None and editor.segmentationNode() is not None and editor.segmentationNode().GetID() in self._nodeIds:
            editor.setSegmentationNode(None)
        if self._figureShown:
            figure = review._figure()
            if figure is not None:
                with contextlib.suppress(Exception):
                    figure.closeFigure(removeNodes=True)
            self._figureShown = False
        _removeNodes(self._nodeIds, keep)
        self._nodeIds = []

    # Presentation

    def _drawSession(self):
        draw = review._draw()
        if draw is None or self.shell is None:
            return None
        drawSession = draw.session(self.shell)
        return drawSession if getattr(drawSession, "active", False) else None

    def install(self):
        if not self.active or self.current is None:
            return
        drawSession = self._drawSession()
        if drawSession is not None:
            if not isinstance(drawSession.footer, _StateFooter) or drawSession.footer.stateRef() is not self:
                previous = drawSession.footer
                if isinstance(previous, _StateFooter):
                    previous.stateRef = lambda: None
                    original, previous.original = previous.original, None
                    previous._discard()
                    previous = original
                self.footer = _StateFooter(self, previous)
                drawSession.footer = self.footer
                self.shell.setInspectorFooter(self.footer.widget)
            drawSession.refreshPanel()
        self.refreshNotice()

    def refreshFooter(self):
        drawSession = self._drawSession()
        if drawSession is not None and drawSession.footer is self.footer:
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
        notice.setObjectName("StudioAgreementNotice")
        notice.setProperty("studioNoticeLine", True)
        notice.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        ui.bindStyleSheet(notice, lambda: (
            f"QFrame#StudioAgreementNotice {{ background-color: {ui.token('accent-subtle').name()}; border: none; "
            f"border-bottom: 1px solid {ui.token('hairline').name()}; }}"
            f"QFrame#StudioAgreementNotice QLabel {{ background: transparent; color: {ui.token('text').name()}; }}"))
        layout = qt.QVBoxLayout(notice)
        layout.setContentsMargins(12, 6, 8, 6)
        layout.setSpacing(4)
        top = qt.QHBoxLayout()
        self.titleLabel = qt.QLabel()
        self.titleLabel.setObjectName("StudioAgreementTitle")
        self.titleLabel.setWordWrap(True)
        font = qt.QFont(self.titleLabel.font)
        font.setWeight(qt.QFont.DemiBold)
        self.titleLabel.setFont(font)
        top.addWidget(self.titleLabel, 1)
        self.exitButton = qt.QToolButton()
        self.exitButton.setObjectName("StudioAgreementExitButton")
        self.exitButton.setText(self.exitText)
        self.exitButton.setAutoRaise(True)
        self.exitButton.setToolTip(_("Back to the Library: what was saved is kept"))
        self.exitButton.connect("clicked()", self._onExitClicked)
        ui.setRole(self.exitButton, "ghost")
        ui.installButtonKeys(self.exitButton)
        top.addWidget(self.exitButton, 0, qt.Qt.AlignTop)
        layout.addLayout(top)
        self.detailLabel = qt.QLabel()
        self.detailLabel.setObjectName("StudioAgreementDetail")
        self.detailLabel.setWordWrap(True)
        self.detailLabel.setTextFormat(qt.Qt.PlainText)
        layout.addWidget(self.detailLabel)
        self.notice = notice

    def _onExitClicked(self):
        self.exit()

    def refreshNotice(self):
        if not self.active or self.current is None or self.shell is None:
            return
        if self.notice is None or not _isAlive(self.notice):
            self._buildNotice()
        index, total = self.position()
        self.titleLabel.setText(_("{title} · {sid} · {index} of {total}").format(title=self.title(), sid=self.current, index=index, total=total))
        self.detailLabel.setText("\n".join(self.lines))
        if self.shell.inspectorNotice() is not self.notice:
            self.shell.setInspectorNotice(self.notice, NOTICE_KEY, "draw")

    def exit(self, message=None, quiet=False, fromFooter=False, keepScene=False):
        """End the state: Draw's footer comes back, this state's nodes go (``keepScene``: the session's subject stays)."""
        global _state
        if _state is self:
            _state = None
        wasActive = self.active
        if wasActive and not keepScene:
            self.saveQuietly()
        self.active = False
        if not fromFooter:
            self._restoreFooter()
        self._clearNodes()
        if self.shell is not None:
            with contextlib.suppress(Exception):
                self.shell.clearInspectorNotice(NOTICE_KEY)
            if wasActive and not keepScene:
                self.shell.setBreadcrumb(self.session.study.name, None)
        if self.notice is not None:
            for target in (self.notice, *self.notice.findChildren("QWidget")):
                ui.bindStyleSheet(target, None)
            self.notice.deleteLater()
            self.notice = None
        with contextlib.suppress(Exception):
            self.session.setValues([], {})
        if wasActive and not keepScene and not fromFooter and self.shell is not None and self.shell.currentWorkspace() == "draw":
            # Back where the queue was started: the study in the Library
            self.shell.switchTo("library")
        if message:
            _toast(message, ms=4000, level="consequence" if not quiet else "info")
        elif wasActive and not quiet:
            _toast(_("{title} ended").format(title=self.title()), ms=2000)


def _autosaveSoon(reason):
    """Autosave once the state's subject is shown, so that the newest autosave names this state and its subject (the
    study's context, ``study_ui._autosaveContext``), not the subject the study session had before (A-01).
    """
    try:
        from . import autosave
    except ImportError:
        return
    qt.QTimer.singleShot(0, lambda: autosave.autosaveNow(reason))


@contextlib.contextmanager
def _renderPaused():
    pause = getattr(slicer.app, "pauseRender", None)
    if pause is None:
        yield
        return
    pause()
    try:
        yield
    finally:
        slicer.app.resumeRender()


#
# Second tracing
#


class SecondTracingState(_AgreementState):
    """Blind second tracing: the primary lesion is never loaded (see the module docstring)."""

    queue = QUEUE_SECOND

    def __init__(self, session, shell, sids, rater):
        _AgreementState.__init__(self, session, shell, sids)
        self.exitText = _("Exit second tracing")
        self.rater = rater
        self.reference = None
        self.segmentationId = None
        self._stamp = None
        self._figureFit = None
        # Crash safety (SPEC 5.5, D-A7): the second tracing's own journal, written 2 s after the last edit
        self._journal = None
        self._edited = False
        self._placedJournalNotice = None
        studyUi = _studyUi()
        self._observer = studyUi._SegmentObserver(self._onSegmentModified) if studyUi is not None else None

    def title(self):
        return _("Second tracing")

    # Journal

    def journal(self):
        """The journal of this rater's second tracings (``journal.SecondTracingJournal``), or None."""
        if self._journal is None:
            try:
                from . import journal

                self._journal = journal.SecondTracingJournal(self.session, self.rater, onNoticeChanged=self._placeJournalNotice)
            except (ImportError, ValueError):
                logging.warning(f"Studio agreement: the second tracing cannot be journaled:\n{traceback.format_exc()}")
                self._journal = None
        return self._journal

    def _onSegmentModified(self):
        if not self.active or self.current is None or self._journal is None:
            return
        if not self._edited:
            node = self.segmentationNode()
            if node is None or _studyUi()._contentStamp(node) == self._stamp:
                return
            self._edited = True
        self._journal.schedule()

    def _placeJournalNotice(self):
        """The journal's "Recover unsaved second tracing" line goes in this state's notice (the slot holds one notice)."""
        widget = self._journal.noticeWidget() if self._journal is not None else None
        if widget is not None and not _isAlive(widget):
            widget = None
        placed = self._placedJournalNotice
        if placed is not None and placed is not widget:
            self._placedJournalNotice = None
            if _isAlive(placed):
                if self.notice is not None and _isAlive(self.notice):
                    self.notice.layout().removeWidget(placed)
                placed.hide()
        if widget is None or self.notice is None or not _isAlive(self.notice) or self._placedJournalNotice is widget:
            return
        layout = widget.layout()
        if layout is not None:
            layout.setContentsMargins(0, 2, 0, 0)
        self.notice.layout().addWidget(widget)
        widget.show()
        self._placedJournalNotice = widget

    def refreshNotice(self):
        _AgreementState.refreshNotice(self)
        self._placeJournalNotice()

    def _clearNodes(self):
        if self._observer is not None:
            self._observer.dispose()
        if self._journal is not None:
            # Saved (the journal was removed then), skipped or left: a journal on disk is offered when the subject comes back
            self._journal.detach()
        _AgreementState._clearNodes(self)

    def load(self, sid):
        rules = _rules()
        studyObject = self.session.study
        rules.check_second_tracing(studyObject, sid, self.rater)
        row = studyObject.subject(sid)
        level = row.get("rater2_level") if row.get("rater2_level") in rules.LEVELS else rules.DEFAULT_LEVEL
        mode = row.get("rater2_mode") if row.get("rater2_mode") in rules.MODES else "inter"
        fits = review.figureFitFiles(studyObject, sid)
        volume, self.reference = self._loadSource(sid, figure=bool(fits))
        importer = _importer()
        studyUi = _studyUi()
        own = studyObject.abspath(row.get("rater2_lesion")) if not _missing(row.get("rater2_lesion")) else None
        if own and os.path.isfile(own) and rules._same_rater(row.get("rater2"), self.rater):
            # The second rater's own earlier tracing (never the primary lesion)
            segmentationNode = importer.loadLesion(own)
            self.reference = own
        else:
            segmentationNode = importer.createLesionSegmentation(volume, f"{sid} (second tracing)")
        segmentationNode.SetName(f"{sid} (second tracing)")
        segmentationNode.SetAttribute(NODE_ATTRIBUTE, sid)
        self._nodeIds.append(segmentationNode.GetID())
        self.segmentationId = segmentationNode.GetID()
        importer.selectInSegmentEditor(segmentationNode, volume)
        importer.setViewLayers(background=volume, fit=True)
        self._figureFit = None
        figureText = None
        if fits:
            figureText = self._showFigure(fits[0], level)
        self._stamp = studyUi._contentStamp(segmentationNode)
        self._edited = False
        if self._observer is not None:
            self._observer.observe(segmentationNode)
        journal = self.journal()
        if journal is not None:
            # A journal newer than the rater's own tracing file (Slicer stopped before it was saved) is offered
            journal.attach(sid, segmentationNode)
        lines = [_("Blind: the first tracing is not loaded, and the agreement is shown only in the Library"),
                 _("Rater: {rater} · {mode}").format(rater=self.rater, mode=_("intra-rater (same rater, 7 days later)") if mode == "intra" else _("inter-rater"))]
        if figureText:
            lines.append(figureText)
        elif not _missing(row.get("scan")):
            lines.append(_("Source: {name}").format(name=os.path.basename(str(row["scan"]))))
        if studyObject.blind_tracing:
            lines.append(_("Blind tracing: group, scores and symptoms are hidden; network maps are not shown"))
        self.lines = lines

    def _showFigure(self, fitPath, level):
        figure = review._figure()
        if figure is None:
            return None
        try:
            if level == "trace_only":
                figure.showSource(fitPath, self.shell)
                self._figureShown = True
                self._figureFit = fitPath
                return _("Figure: {name} with the first tracing's fit (trace only)").format(name=os.path.basename(fitPath)[:-len("_fit.json")])
            png = fitPath[:-len("_fit.json")] + ".png"
            if figure.openFigure(png, self.shell) is not None:
                self._figureShown = True
                return _("Figure: {name} (full: make your own crop and fit)").format(name=os.path.basename(png))
        except Exception as e:
            logging.warning(f"Studio agreement: the figure of {self.current} could not be shown: {e}")
            return _("Figure not shown: {error}").format(error=e)
        return None

    def segmentationNode(self):
        return slicer.mrmlScene.GetNodeByID(self.segmentationId) if self.segmentationId else None

    def isDirty(self):
        node = self.segmentationNode()
        return node is not None and _studyUi()._contentStamp(node) != self._stamp

    def buildFooter(self, footer):
        footer.saveButton = ui.primaryButton(_("Save second tracing → next"))
        footer.saveButton.setObjectName("StudioSecondTracingSaveButton")
        footer.saveButton.connect("clicked()", self.saveNext)
        ui.installButtonKeys(footer.saveButton)
        footer.layout.addWidget(footer.saveButton)
        footer.skipButton = qt.QPushButton(_("Skip for now"))
        footer.skipButton.setObjectName("StudioSecondTracingSkipButton")
        footer.skipButton.setToolTip(_("Open the next second tracing; this one stays in the queue"))
        ui.setRole(footer.skipButton, "ghost")
        footer.skipButton.connect("clicked()", self.skip)
        ui.installButtonKeys(footer.skipButton)
        footer.layout.addWidget(footer.skipButton)

    def updateFooter(self, footer):
        from . import draw

        key = ui.keyText(draw.PRIMARY_SHORTCUT)
        last = not self.following()
        text = _("Save second tracing") if last else _("Save second tracing → next")
        label = f"{text} · {key}"
        if str(footer.saveButton.text) != label:
            footer.saveButton.setText(label)
            footer.saveButton.setAccessibleName(label)
        footer.saveButton.setToolTip(ui.tooltip(_("Save second tracing"), draw.PRIMARY_SHORTCUT,
                                                _("written to lesions/rater-{label}/; the first tracing is never shown").format(label=self._label())))
        footer.skipButton.setVisible(not last)
        return label, self.saveNext

    def _label(self):
        try:
            return _rules().rater_label(self.rater)
        except ValueError:
            return "?"

    def save(self, quiet=False, final=True):
        """Write the second tracing of the current subject. Return True when it was written.

        ``final`` False keeps work in progress (Skip, Exit, another subject): the subject stays in the queue and counts in
        no agreement until "Save second tracing" (``agreement.save_second_tracing``).
        """
        node = self.segmentationNode()
        sid = self.current
        if node is None or sid is None:
            return False
        draw = review._draw()
        if draw is not None and hasattr(draw, "_applyPendingSmartFill"):
            with contextlib.suppress(Exception):
                draw._applyPendingSmartFill(self.shell)
        mask = _studyUi().lesionMask(node)
        if mask is None:
            if not quiet:
                _toast(_("The second tracing of {sid} is empty: nothing was saved").format(sid=sid), ms=4000, level="warning")
            return False
        rules = _rules()
        try:
            result = rules.save_second_tracing(self.session.study, sid, mask[0], mask[1], self.reference, self.rater, extra=self._figureExtra(sid), final=final)
        except (rules.AgreementError, ValueError, OSError) as e:
            _toast(str(e), ms=6000, level="warning")
            return False
        self._stamp = _studyUi()._contentStamp(node)
        self._edited = False
        if self._journal is not None:
            # The tracing is written: its journal is out of date
            self._journal.discard(sid)
        self.session.setValues([], {})
        self.session.flush()
        if not quiet:
            _toast(_("Saved {name}").format(name=os.path.basename(result["path"])), ms=2000)
        return True

    def _figureExtra(self, sid):
        """The figure provenance of the second tracing: the first tracing's fit reused, or the second rater's own fit."""
        if self._figureFit is not None:
            return {"figure_fit": self.session.study.relpath(self._figureFit), "figure_fit_reused": True}
        figure = review._figure()
        current = figure.session() if figure is not None and self._figureShown else None
        if current is None or not getattr(current, "panels", None):
            return None
        rules = _rules()
        folder = os.path.join(self.session.study.path, os.path.dirname(rules.second_relpath(self.session.study, sid, self.rater)))
        written = []
        try:
            import importlib

            figurefit = importlib.import_module("LesionNetworkMappingLib.figurefit")
            current.sid = sid
            for index, panel in enumerate(current.panels, start=1):
                if panel.fit is None:
                    panel.refit()
                if panel.fit is None or not panel.cropPath or not os.path.isfile(panel.cropPath):
                    continue
                stem = os.path.join(folder, f"{sid}_fig-{index}")
                rules._copy(panel.cropPath, stem + ".png")
                record = current._record(panel, figurefit.image_sha256(stem + ".png"))
                record["tracer"] = self.rater
                figurefit.write_fit_json(stem + "_fit.json", record)
                written.append(self.session.study.relpath(stem + "_fit.json"))
        except Exception:
            logging.warning(f"Studio agreement: the figure fit of the second tracing of {sid} could not be saved:\n{traceback.format_exc()}")
        return {"figure_fits": written, "figure_fit_reused": False} if written else None

    def saveQuietly(self):
        if self.active and self.current is not None and self.isDirty():
            self.save(quiet=True, final=False)

    def saveNext(self):
        sid = self.current
        if sid is None or not self.save():
            return False
        self.finished.append(sid)
        self.openNext(_("Saved the second tracing of {sid}").format(sid=sid))
        return True

    def skip(self):
        following = self.following()
        if not following:
            return
        self.saveQuietly()
        # The skipped subject goes to the end of the pass
        self.sids = [sid for sid in self.sids if sid != self.current] + [self.current]
        self.open(following[0])


#
# Adjudication
#


class AdjudicationState(_AgreementState):
    """Both tracings as outlines; A | B | Union | Intersection | Edit → the consensus (see the module docstring)."""

    queue = QUEUE_ADJUDICATE

    def __init__(self, session, shell, sids, who):
        _AgreementState.__init__(self, session, shell, sids)
        self.exitText = _("Exit adjudication")
        self.who = who
        self.segmentationId = None
        self.segmentIds = {}
        self.editing = False
        self.metrics = None

    def title(self):
        return _("Adjudicate")

    def load(self, sid):
        rules = _rules()
        studyObject = self.session.study
        row = studyObject.subject(sid)
        paths = rules.pair_paths(studyObject, row)
        if paths is None:
            raise RuntimeError(_("the two tracings are not both on disk"))
        a, affineA = rules.load_mask(paths[0])
        b, affineB = rules.load_mask(paths[1])
        volume, _reference = self._loadSource(sid, figure=bool(review.figureFitFiles(studyObject, sid)))
        segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", f"{sid} (adjudicate)")
        segmentationNode.CreateDefaultDisplayNodes()
        segmentationNode.SetAttribute(NODE_ATTRIBUTE, sid)
        self._nodeIds.append(segmentationNode.GetID())
        self.segmentationId = segmentationNode.GetID()
        # The consensus is edited on the first tracing's grid (the file it replaces)
        geometry = _labelmapNode(np.zeros(a.shape, dtype=np.uint8), affineA, "StudioAgreementGeometry")
        try:
            segmentationNode.SetReferenceImageGeometryParameterFromVolumeNode(geometry)
        finally:
            slicer.mrmlScene.RemoveNode(geometry)
        first = rules.primary_rater(row, studyObject) if _missing(row.get("rater1_lesion")) else None
        labelA = rules._label_or_none(first)
        labelB = rules._label_or_none(row.get("rater2"))
        self.segmentIds = {"A": _addSegment(segmentationNode, a >= 0.5, affineA, f"A · {labelA}" if labelA else "A", COLOR_A),
                           "B": _addSegment(segmentationNode, b >= 0.5, affineB, f"B · {labelB}" if labelB else "B", COLOR_B)}
        displayNode = segmentationNode.GetDisplayNode()
        displayNode.SetVisibility2DFill(False)
        displayNode.SetVisibility2DOutline(True)
        displayNode.SetOpacity2DOutline(1.0)
        importer = _importer()
        importer.selectInSegmentEditor(segmentationNode, volume)
        importer.setViewLayers(background=volume, fit=True)
        with contextlib.suppress(Exception):
            center = rules.centroid_mm((a >= 0.5) | (rules._to_grid(b, affineB, rules.geometry.Grid(a.shape, affineA))), affineA)
            if center is not None:
                slicer.modules.markups.logic().JumpSlicesToLocation(float(center[0]), float(center[1]), float(center[2]), True)
        self.editing = False
        self.metrics = rules.study_pair(studyObject, sid, grid2mm=None)
        dice = self.metrics["dice"] if self.metrics else None
        firstText = _("first tracing by {a}").format(a=labelA) if labelA else _("first tracing (tracer not recorded)")
        secondText = _("second tracing by {b}").format(b=labelB) if labelB else _("second tracing (rater not recorded)")
        lines = [_("A (red): {first} · B (blue): {second}").format(first=firstText, second=secondText)]
        if self.metrics is not None:
            lines.append(_("Dice {dice} · centroid distance {distance} mm · volumes {va} / {vb} mL").format(
                dice=f"{dice:.2f}" if dice is not None else "—",
                distance=f"{self.metrics['centroid_mm']:.1f}" if self.metrics["centroid_mm"] is not None else "—",
                va=f"{self.metrics['volume_a_ml']:.2f}", vb=f"{self.metrics['volume_b_ml']:.2f}"))
        lines.append(_("The consensus becomes the lesion (rater = consensus); both tracings are kept, every choice is recorded"))
        self.lines = lines

    def segmentationNode(self):
        return slicer.mrmlScene.GetNodeByID(self.segmentationId) if self.segmentationId else None

    def buildFooter(self, footer):
        footer.choiceRow = qt.QWidget()
        footer.choiceRow.setObjectName("StudioAdjudicateChoices")
        grid = qt.QGridLayout(footer.choiceRow)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)
        footer.choiceButtons = {}
        tips = {"A": _("Keep the first tracing (red)"), "B": _("Keep the second tracing (blue)"), "union": _("Voxels of either tracing"),
                "intersection": _("Voxels of both tracings"), "edit": _("Start from the union and edit it in Draw")}
        for index, choice in enumerate(("A", "B", "union", "intersection", "edit")):
            button = qt.QPushButton(_(CHOICE_LABELS[choice]))
            button.setObjectName(f"StudioAdjudicate{choice.capitalize()}Button")
            button.setToolTip(tips[choice])
            ui.setRole(button, "secondary")
            button.setMinimumHeight(28)
            button.connect("clicked()", lambda choice=choice: self.choose(choice))
            ui.installButtonKeys(button)
            grid.addWidget(button, index // 3, index % 3)
            footer.choiceButtons[choice] = button
        footer.layout.addWidget(footer.choiceRow)
        footer.saveButton = ui.primaryButton(_("Save consensus → next"))
        footer.saveButton.setObjectName("StudioAdjudicateSaveButton")
        footer.saveButton.connect("clicked()", self.saveEdit)
        ui.installButtonKeys(footer.saveButton)
        footer.layout.addWidget(footer.saveButton)
        footer.cancelButton = qt.QPushButton(_("Cancel edit"))
        footer.cancelButton.setObjectName("StudioAdjudicateCancelButton")
        ui.setRole(footer.cancelButton, "ghost")
        footer.cancelButton.connect("clicked()", self.cancelEdit)
        ui.installButtonKeys(footer.cancelButton)
        footer.layout.addWidget(footer.cancelButton)
        footer.skipButton = qt.QPushButton(_("Skip for now"))
        footer.skipButton.setObjectName("StudioAdjudicateSkipButton")
        ui.setRole(footer.skipButton, "ghost")
        footer.skipButton.connect("clicked()", self.skip)
        ui.installButtonKeys(footer.skipButton)
        footer.layout.addWidget(footer.skipButton)

    def updateFooter(self, footer):
        from . import draw

        footer.choiceRow.setVisible(not self.editing)
        footer.saveButton.setVisible(self.editing)
        footer.cancelButton.setVisible(self.editing)
        footer.skipButton.setVisible(not self.editing and bool(self.following()))
        if not self.editing:
            # No default choice: the adjudicator picks one (Ctrl+Enter does nothing here)
            return "", None
        key = ui.keyText(draw.PRIMARY_SHORTCUT)
        text = _("Save consensus") if not self.following() else _("Save consensus → next")
        label = f"{text} · {key}"
        if str(footer.saveButton.text) != label:
            footer.saveButton.setText(label)
            footer.saveButton.setAccessibleName(label)
        return label, self.saveEdit

    def choose(self, choice):
        if choice == "edit":
            self.startEdit()
            return True
        return self.decide(choice)

    def decide(self, choice, mask=None):
        """Write the consensus of the current subject, then open the next pair. Return True if it was written."""
        sid = self.current
        if sid is None:
            return False
        rules = _rules()
        studyObject = self.session.study
        try:
            if mask is not None:
                record = rules.adjudicate(studyObject, sid, choice, self.who, array_kji=mask[0], ijk_to_ras=mask[1])
            else:
                record = rules.adjudicate(studyObject, sid, choice, self.who)
        except (rules.AgreementError, ValueError, OSError) as e:
            _toast(str(e), ms=6000, level="warning")
            return False
        _afterConsensus(self.session, sid)
        self.finished.append(sid)
        label = _("Consensus of {sid}: {choice}").format(sid=sid, choice=_(CHOICE_LABELS[choice]).rstrip("…"))
        if record.get("review_reopened"):
            label += " · " + _("its review is pending again")
        following = self.following()
        if not following:
            self.exit(quiet=True)
        else:
            self.open(following[0])
        shell = _shell()
        if shell is not None:
            shell.toast(label, 6000, action=lambda: undo(sid), actionText=_("Undo"), level="consequence")
        return True

    def startEdit(self):
        node = self.segmentationNode()
        if node is None:
            return
        rules = _rules()
        row = self.session.study.subject(self.current)
        paths = rules.pair_paths(self.session.study, row)
        a, affineA = rules.load_mask(paths[0])
        b, affineB = rules.load_mask(paths[1])
        union = rules.consensus_mask(a, rules._to_grid(b, affineB, rules.geometry.Grid(a.shape, affineA)), "union")
        if "consensus" not in self.segmentIds:
            self.segmentIds["consensus"] = _addSegment(node, union, affineA, _("Consensus"), COLOR_CONSENSUS)
        displayNode = node.GetDisplayNode()
        displayNode.SetSegmentVisibility2DFill(self.segmentIds["consensus"], True)
        displayNode.SetVisibility2DFill(True)
        for key in ("A", "B"):
            displayNode.SetSegmentVisibility2DFill(self.segmentIds[key], False)
        widget = getattr(slicer.modules, "SegmentEditorWidget", None)
        if widget is not None and widget.editor is not None:
            widget.editor.setCurrentSegmentID(self.segmentIds["consensus"])
        self.editing = True
        self.refreshFooter()
        if self.shell is not None and hasattr(self.shell, "registerEscapeOverlay") and self.notice is not None:
            self.shell.registerEscapeOverlay(self.notice, self.cancelEdit)

    def cancelEdit(self):
        node = self.segmentationNode()
        segmentId = self.segmentIds.pop("consensus", None)
        if node is not None and segmentId:
            node.GetSegmentation().RemoveSegment(segmentId)
            node.GetDisplayNode().SetVisibility2DFill(False)
        self.editing = False
        if self.shell is not None and hasattr(self.shell, "unregisterEscapeOverlay") and self.notice is not None:
            self.shell.unregisterEscapeOverlay(self.notice)
        self.refreshFooter()

    def saveEdit(self):
        node = self.segmentationNode()
        if not self.editing or node is None:
            return False
        draw = review._draw()
        if draw is not None and hasattr(draw, "_applyPendingSmartFill"):
            with contextlib.suppress(Exception):
                draw._applyPendingSmartFill(self.shell)
        mask = _segmentMask(node, self.segmentIds["consensus"])
        if mask is None:
            _toast(_("The consensus is empty: nothing was saved"), ms=4000, level="warning")
            return False
        if self.shell is not None and hasattr(self.shell, "unregisterEscapeOverlay") and self.notice is not None:
            self.shell.unregisterEscapeOverlay(self.notice)
        return self.decide("edit", mask)

    def skip(self):
        following = self.following()
        if following:
            self.sids = [sid for sid in self.sids if sid != self.current] + [self.current]
            self.open(following[0])

    def exit(self, message=None, quiet=False, fromFooter=False, keepScene=False):
        if self.shell is not None and hasattr(self.shell, "unregisterEscapeOverlay") and self.notice is not None:
            with contextlib.suppress(Exception):
                self.shell.unregisterEscapeOverlay(self.notice)
        _AgreementState.exit(self, message, quiet, fromFooter, keepScene)


def _afterConsensus(session, sid):
    """The row is written (soon), the 2D analysis files follow the consensus, the views are told."""
    row = session.study.subject(sid)
    if row.get("dimensionality") in ("2D", "multi2D"):
        with contextlib.suppress(Exception):
            from . import lesionqc

            lesionqc.regenerate(session.study, sid, session, source="lesion")
    session.setValues([], {})
    session.flush()


def undo(sid):
    """Undo the consensus of a subject (the toast's Undo): the first tracing is the lesion again (audited)."""
    session = _session()
    if session is None or sid not in session.study:
        return False
    rules = _rules()
    who = review.raterName(ask=False)
    try:
        rules.undo_adjudication(session.study, sid, who)
    except (rules.AgreementError, OSError) as e:
        _toast(str(e), ms=5000, level="warning")
        return False
    _afterConsensus(session, sid)
    _toast(_("Consensus of {sid} undone").format(sid=sid), ms=2500)
    return True


#
# Entry points
#

_state = None


def state():
    """The active second tracing or adjudication state, or None."""
    return _state if _state is not None and _state.active else None


def _start(queue, sid=None, sids=None, shell=None):
    global _state
    session = _session()
    shell = shell or _shell()
    if session is None:
        _toast(_("Open a study first"))
        return None
    if shell is None:
        logging.info("Studio agreement: second tracing and adjudication in Draw need the Studio interface")
        return None
    order = list(sids) if sids else [row["participant_id"] for row in queueRows(session, queue)]
    if sid is not None:
        order = [sid] + [other for other in order if other != sid]
    label = _("Second tracing") if queue == QUEUE_SECOND else _("Adjudicate")
    if not order:
        _toast(_("Nothing is waiting in {queue}").format(queue=label), ms=3000)
        return None
    who = review.raterName(ask=True)
    if not who:
        _toast(_("A rater name is needed to record the tracing"), ms=4000, level="warning")
        return None
    if queue == QUEUE_SECOND:
        # Subjects this rater may not trace (their own lesion in inter mode, the 7-day interval) stay in the queue
        rules = _rules()
        allowed, refused = [], []
        for other in order:
            try:
                rules.check_second_tracing(session.study, other, who)
                allowed.append(other)
            except rules.AgreementError as e:
                refused.append(str(e))
        if not allowed:
            _toast(refused[0] if refused else _("Nothing is waiting in {queue}").format(queue=label), ms=6000, level="warning")
            return None
        if refused:
            _toast(_("{n} second tracing(s) left out: {reason}").format(n=len(refused), reason=refused[0]), ms=5000)
        order = allowed
    if review.state() is not None:
        review.exitReview()
    if _state is not None:
        _state.exit(quiet=True)
    installStudyHooks()
    current = SecondTracingState(session, shell, order, who) if queue == QUEUE_SECOND else AdjudicationState(session, shell, order, who)
    _state = current
    if not current.open(order[0]):
        current.exit(quiet=True, keepScene=True)
        return None
    return current


def startSecondTracing(sid=None, sids=None, shell=None):
    """Open the blind second tracing state of Draw (the subjects assigned to a second tracing, or ``sids``)."""
    return _start(QUEUE_SECOND, sid, sids, shell)


def startAdjudication(sid=None, sids=None, shell=None):
    """Open the adjudication state of Draw (the pairs below the Dice threshold, or ``sids``)."""
    return _start(QUEUE_ADJUDICATE, sid, sids, shell)


def exitState():
    current = state()
    if current is not None:
        current.exit()
        return True
    return False


#
# Study hooks: the case strip's queues open subjects through the session
#


def openHook(sid, ctx):
    """A subject was opened by the study session: an active state follows (or ends); in the "Second tracing" or the
    "Adjudicate" queue of the case strip, the subject is handed over to its state before anything is drawn.
    """
    current = state()
    if current is not None:
        current.onSubjectOpened(sid)
        return
    session = ctx.get("session") or _session()
    if session is None or sid not in session.study or _shell() is None:
        return
    spec = session.queue() if hasattr(session, "queue") else None
    key = spec.get("key") if spec else None
    row = session.study.subject(sid)
    if key == QUEUE_SECOND and secondTodo(row):
        # Blinding: the primary lesion that the session loaded is hidden in this event and removed before it is shown
        _hideSegmentation(session.segmentationNode())
        qt.QTimer.singleShot(0, lambda: _handOver(QUEUE_SECOND, sid))
    elif key == QUEUE_ADJUDICATE and needsAdjudication(row):
        qt.QTimer.singleShot(0, lambda: _handOver(QUEUE_ADJUDICATE, sid))


def _handOver(queue, sid):
    if state() is not None:
        return
    started = _start(queue, sid=sid)
    session = _session()
    if started is None and session is not None and queue == QUEUE_SECOND and session.current() == sid:
        # Not allowed for this rater: the session's subject goes, so that its lesion is never shown
        releaseSession(session)


def doneHook(sid, labelmap, ijkToRas, ctx):
    """Done hook: the first Done of a lesion records who traced it (``tracer`` = the rater name, ``Studio/RaterName``),
    which inter-rater blinding and intra-rater mode check (``agreement.primary_rater``). A tracer already recorded (the
    column, the review audit) is kept, and a lesion with a second tracing keeps the tracer of its pair.
    """
    session = ctx.get("session") or _session()
    if session is None or sid not in session.study:
        return {}
    name = review.storedRaterName()
    if not name:
        return {}
    rules = _rules()
    row = session.study.subject(sid)
    if rules.status_of(row) is not None or rules.primary_rater(row, session.study) is not None:
        return {}
    return {"tracer": name}


def _onStudyChanged(session=None):
    session = session if session is not None else _session()
    if session is None:
        if _state is not None:
            _state.exit(quiet=True, keepScene=True)
        return
    session.removeDoneHook(doneHook)
    session.addDoneHook(doneHook, order=DONE_HOOK_ORDER)
    session.removeOpenHook(openHook)
    session.addOpenHook(openHook)
    if _state is not None and _state.session is not session:
        _state.exit(quiet=True, keepScene=True)


_installed = [False]


def installStudyHooks():
    """Follow the study session (idempotent)."""
    studyUi = _studyUi()
    if studyUi is None:
        return False
    if not _installed[0]:
        studyUi.addListener(_onStudyChanged)
        _installed[0] = True
    if studyUi.session() is not None:
        _onStudyChanged(studyUi.session())
    return True

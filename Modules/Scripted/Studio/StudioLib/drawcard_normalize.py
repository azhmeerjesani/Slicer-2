"""Normalize to MNI card of Draw (Studio's presentation of the Segment Editor only; A-03b).

Draw discovers this module (``drawcard_*``, ``DRAW_CARD``) and puts the card after Display. It is shown
when the source volume is a native scan (not on an MNI grid, not a normalization result):

- Idle: a 34 px list row "Normalize to MNI · Native space" with a chevron (VISUAL 4.4) that starts the normalization. With an
  empty lesion it is "Normalize scan only" (then trace on the warped scan), with the tip that a rough outline first
  makes the registration lesion-aware in its tooltip.
- Running: the progress row and Stop (the job runs in the background through the LNM JobRunner, which
  shows it in the status chip).
- Done: the QC panel, Use for Map → (the lesion becomes the Map seed) and Trace on the MNI scan (the warped
  scan becomes the source and the template-space lesion the edited segmentation). Edits of the native lesion
  offer Update MNI lesion.

With an open study (``shell.studySession``) the click goes to the study's batch normalization
(``StudioLib.libcard_normalize.normalizeSubjects``) when that module exists. The work itself is
``LesionNetworkMappingUI.normalize_controller``; without the LNM module the card stays hidden.
"""

import logging
import weakref

import qt
import slicer
import vtk
from slicer.i18n import tr as _

from . import ui

#: The card of the current Draw panel (for ``focus()``)
_active = None

REFRESH_DELAY_MS = 300


def _normalizeController():
    """``LesionNetworkMappingUI.normalize_controller``, or None when the LNM module is not available."""
    try:
        from LesionNetworkMappingUI import normalize_controller
    except Exception:
        return None
    return normalize_controller


def _lnmWidgets():
    try:
        from LesionNetworkMappingUI import widgets
    except Exception:
        return None
    return widgets


def _segmentEditor():
    widget = getattr(slicer.modules, "SegmentEditorWidget", None)
    return getattr(widget, "editor", None) if widget is not None else None


class DrawNormalizeCard:
    """Controller of the Draw card (``widget`` is the frame StudioNormalizeDrawCard)."""

    def __init__(self, shell):
        global _active
        self.shell = shell
        self.nc = _normalizeController()
        self.controller = self.nc.NormalizeController(name="draw") if self.nc is not None else None
        self.ctx = {}
        self._qcPanel = None
        self.message = None
        self._observed = []
        self._lesionEmpty = False
        self._menuAdded = False
        #: (segmentation, source volume) of the editor when the job started: a scan-only result has no native lesion to match
        self._origin = None

        self.widget = qt.QFrame()
        self.widget.setObjectName("StudioNormalizeDrawCard")
        ui.setRole(self.widget, "card")
        self.widget.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Maximum)
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        self._layout = layout

        # Idle: one list row (VISUAL 4.4); running and done: the card
        self.listRow = ui.listRow("globe", _("Normalize to MNI"), _("Native space"), self.start, "StudioNormalizeDrawRow")
        layout.addWidget(self.listRow)

        self.cardRow = qt.QWidget()
        self.cardRow.setObjectName("StudioNormalizeDrawCardRow")
        row = qt.QHBoxLayout(self.cardRow)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.label = qt.QLabel(_("Native space · Map needs MNI"))
        self.label.setObjectName("StudioNormalizeDrawLabel")
        self.label.setWordWrap(True)
        self.label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        self.label.setProperty("slicerRole", "secondaryText")
        row.addWidget(self.label, 1)
        self.button = qt.QPushButton(_("Normalize to MNI"))
        self.button.setObjectName("StudioNormalizeDrawButton")
        self.button.setMinimumHeight(28)
        ui.setRole(self.button, "secondary")
        self.button.setToolTip(ui.tooltip(_("Normalize to MNI"), None, _("register the scan to the MNI template with the lesion masked out, in the background")))
        self.button.connect("clicked()", self.start)
        row.addWidget(self.button)
        self.moreButton = qt.QToolButton()
        self.moreButton.setObjectName("StudioNormalizeDrawMore")
        self.moreButton.setText("⋯")
        self.moreButton.setAutoRaise(True)
        self.moreButton.setPopupMode(qt.QToolButton.InstantPopup)
        self.moreButton.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0px; }")
        self.moreButton.setToolTip(_("More normalization options"))
        menu = qt.QMenu(self.moreButton)
        self.scanOnlyAction = menu.addAction(_("Normalize scan only"))
        self.scanOnlyAction.connect("triggered()", lambda: self.start(scanOnly=True))
        menu.addAction(_("Options in Map…")).connect("triggered()", self.openInMap)
        self.moreButton.setMenu(menu)
        row.addWidget(self.moreButton)
        layout.addWidget(self.cardRow)

        self.tip = qt.QLabel(_("A rough lesion outline first makes the registration lesion-aware"))
        self.tip.setObjectName("StudioNormalizeDrawTip")
        self.tip.setWordWrap(True)
        self.tip.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        self.tip.setProperty("slicerRole", "secondaryText")
        self.tip.hide()
        layout.addWidget(self.tip)

        widgets = _lnmWidgets()
        self.progress = widgets.ProgressRow(objectNamePrefix="StudioNormalizeDraw") if widgets is not None else None
        if self.progress is not None:
            self.progress.stopButton.connect("clicked()", self.stop)
            self.progress.label.setWordWrap(False)
            self.progress.label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
            self.progress.widget.hide()
            layout.addWidget(self.progress.widget)

        self.messageHost = qt.QVBoxLayout()
        self.messageHost.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self.messageHost)
        self.qcHost = qt.QVBoxLayout()
        self.qcHost.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self.qcHost)

        doneRow = qt.QHBoxLayout()
        doneRow.setSpacing(6)
        self.useForMapButton = ui.primaryButton(_("Use for Map →"))
        self.useForMapButton.setObjectName("StudioNormalizeUseForMap")
        self.useForMapButton.setToolTip(ui.tooltip(_("Use for Map"), None, _("the MNI lesion becomes the seed of Map")))
        self.useForMapButton.connect("clicked()", self.useForMap)
        doneRow.addWidget(self.useForMapButton)
        self.traceButton = qt.QPushButton(_("Trace on the MNI scan"))
        self.traceButton.setObjectName("StudioNormalizeTraceOnMni")
        ui.setRole(self.traceButton, "ghost")
        self.traceButton.connect("clicked()", self.traceOnMni)
        doneRow.addWidget(self.traceButton)
        doneRow.addStretch(1)
        self.doneRow = qt.QWidget()
        self.doneRow.setLayout(doneRow)
        self.doneRow.hide()
        layout.addWidget(self.doneRow)

        self._refreshTimer = qt.QTimer()
        self._refreshTimer.setSingleShot(True)
        self._refreshTimer.setInterval(REFRESH_DELAY_MS)
        self._refreshTimer.connect("timeout()", lambda: self.refresh(None))

        if self.controller is not None:
            self.controller.stateChanged.append(self._onState)
            self.controller.progressChanged.append(self._onProgress)
            self.controller.resultReady.append(self._onResult)
            self.controller.failed.append(self._onFailed)
            self.controller.editStateChanged.append(lambda edited, reregister: self._showEditState())
        self._addMenuAction()
        self.widget.hide()
        _active = weakref.ref(self)
        # Draw shows the card when it adds it to the inspector: the first refresh decides (a template, yours or built-in, is not
        # native space, so the card hides again)
        self._refreshTimer.start()

    #
    # Draw card protocol
    #

    def _context(self, ctx=None):
        if ctx:
            self.ctx = dict(ctx)
        editor = _segmentEditor()
        if editor is not None:
            self.ctx["segmentationNode"] = editor.segmentationNode()
            self.ctx["sourceVolumeNode"] = editor.sourceVolumeNode()
            self.ctx["segmentId"] = editor.currentSegmentID()
        return self.ctx

    def _studio(self):
        if self.shell is None:
            return False
        widget = getattr(slicer.modules, "SegmentEditorWidget", None)
        presentation = getattr(widget, "presentation", None) if widget is not None else None
        try:
            return presentation() == "studio" if callable(presentation) else True
        except Exception:
            return True

    def _drawIsCurrent(self):
        try:
            return self.shell is not None and self.shell.currentWorkspace() == "draw"
        except Exception:
            return False

    def refresh(self, ctx=None, checkEmpty=True):
        """Show the card for a native source volume (or while this card's job or result is shown).

        ``checkEmpty`` reads the lesion's voxels again (after edits); state changes of the job reuse the last answer.
        """
        try:
            self.widget.objectName  # noqa: B018 (a deferred refresh can arrive after the Draw panel was deleted, at exit)
        except Exception:
            return
        ctx = self._context(ctx)
        self._observe(ctx)
        nc = self.nc
        segmentationNode, sourceVolumeNode, segmentId = ctx.get("segmentationNode"), ctx.get("sourceVolumeNode"), ctx.get("segmentId")
        visible = False
        if nc is not None and self._studio() and sourceVolumeNode is not None:
            result = self.controller.result
            if result is not None and result.nativeSegmentationNode is not None and segmentationNode is result.nativeSegmentationNode and self._drawIsCurrent():
                self.controller.showNative(result)
            ownResult = self._ownsResult(result, segmentationNode, sourceVolumeNode, segmentId)
            native = not nc.isNormalizationResult(sourceVolumeNode) and nc.spaceOf(sourceVolumeNode) is None and not nc.isNormalizationResult(segmentationNode)
            visible = native or ownResult or self.controller.isBusy()
        self.widget.setVisible(visible)
        if not visible:
            return
        if checkEmpty:
            self._lesionEmpty = nc.lesionIsEmpty(segmentationNode, segmentId) if segmentationNode is not None else True
        self._showState()

    def _ownsResult(self, result, segmentationNode, sourceVolumeNode, segmentId=None):
        """True when the editor shows what the result was made from (or the result itself).

        The native lesion is a segment: another segment of the same segmentation (``segmentId``, the editor's
        current one) has no MNI copy in this result, so the card offers Normalize to MNI for it, never this
        result's Use for Map.
        """
        if result is None:
            return False
        if segmentationNode is not None and segmentationNode is result.segmentationNode:
            return True
        if segmentationNode is not None and segmentationNode is result.nativeSegmentationNode:
            return not result.nativeSegmentId or (segmentId or None) == result.nativeSegmentId
        # Scan only: no native lesion was normalized; the editor still holds the segmentation and scan of the run
        return (result.nativeSegmentationNode is None and self._origin is not None and self._origin == (segmentationNode, sourceVolumeNode)
                and sourceVolumeNode is result.scanNode)

    @staticmethod
    def _showsMniResult(result, segmentationNode, sourceVolumeNode):
        """True when the editor shows the result's warped scan or MNI lesion (after "Trace on the MNI scan"), not the native ones."""
        if result is None:
            return False
        return ((sourceVolumeNode is not None and sourceVolumeNode is result.volumeNode)
                or (segmentationNode is not None and segmentationNode is result.segmentationNode))

    @staticmethod
    def _nativeSources(result):
        """(native scan, native lesion, segment id) the result came from, or None when the request's nodes are not in the scene."""
        request = result.request
        scan = request.scanNode if request is not None else None
        if scan is None or not slicer.mrmlScene.IsNodePresent(scan):
            return None
        lesion = request.segmentationNode
        if lesion is not None and not slicer.mrmlScene.IsNodePresent(lesion):
            return None
        return scan, lesion, request.segmentId

    def _templateName(self, templateId):
        """The name of the target template: the user's name of their own template (A-10), else its id."""
        nameOf = getattr(self.nc, "templateName", None) if self.nc is not None else None
        try:
            return nameOf(templateId) if nameOf is not None else templateId
        except Exception:
            return templateId

    @staticmethod
    def _isScanOnly(result):
        request = result.request if result is not None else None
        return request is not None and request.segmentationNode is None and not request.lesionPath

    def _showState(self):
        controller = self.controller
        busy = controller.isBusy()
        result = controller.result
        segmentationNode = self.ctx.get("segmentationNode")
        showingResult = controller.state in ("done", "cancelled") and self._ownsResult(result, segmentationNode, self.ctx.get("sourceVolumeNode"), self.ctx.get("segmentId"))
        if self._lesionEmpty and not showingResult:
            self.button.setText(_("Normalize scan only"))
        else:
            self.button.setText(_("Normalize again") if showingResult else _("Normalize to MNI"))
        self.tip.setVisible(self._lesionEmpty and not busy and not showingResult)
        self.button.setEnabled(not busy)
        self.scanOnlyAction.setEnabled(not busy)
        self.label.setText(_("Normalized · {template}").format(template=self._templateName(result.templateId)) if showingResult else _("Native space · Map needs MNI"))
        if self.progress is not None:
            self.progress.widget.setVisible(busy)
        self.doneRow.setVisible(showingResult)
        self.useForMapButton.setEnabled(showingResult and result.segmentationNode is not None and bool(result.segmentId) and not self.nc.lesionIsEmpty(result.segmentationNode, result.segmentId))
        if self._qcPanel is not None:
            self._qcPanel.setVisible(showingResult)
        # An edit notice (Update MNI lesion, Re-register) is about the result's lesion: hidden while another segment is current
        editNotice = self.message is not None and bool(self.message.property("editNotice"))
        if editNotice:
            self.message.setVisible(showingResult)
        # Nothing running, no result and no message: the list row alone
        rowMode = not busy and not showingResult and (self.message is None or editNotice)
        self._setRowMode(rowMode)
        if rowMode:
            ui.setListRowValue(self.listRow, _("Native space"))
            self.listRow.findChild("QLabel", "StudioListRowText").setText(str(self.button.text))
            self.listRow.setAccessibleName(str(self.button.text))
            tip = str(self.button.toolTip)
            if self._lesionEmpty:
                tip += "\n" + str(self.tip.text)
            self.listRow.setToolTip(tip)

    def _setRowMode(self, rowMode):
        """The list row alone (no card fill) or the card with its rows."""
        self.listRow.setVisible(rowMode)
        self.cardRow.setVisible(not rowMode)
        if rowMode:
            self.tip.hide()
        role = None if rowMode else "card"
        if self.widget.property("slicerRole") != role:
            self.widget.setProperty("slicerRole", role)
            style = self.widget.style()
            style.unpolish(self.widget)
            style.polish(self.widget)
            self.widget.update()
            if rowMode:
                self._layout.setContentsMargins(0, 0, 0, 0)
            else:
                self._layout.setContentsMargins(10, 8, 10, 8)

    def _observe(self, ctx):
        """Refresh after the editor's segmentation, segment or source changes, and after strokes (empty / not empty)."""
        targets = []
        editor = _segmentEditor()
        editorNode = editor.mrmlSegmentEditorNode() if editor is not None else None
        if editorNode is not None:
            targets.append((editorNode, vtk.vtkCommand.ModifiedEvent))
        segmentationNode = ctx.get("segmentationNode")
        if segmentationNode is not None:
            targets.append((segmentationNode, slicer.vtkSegmentation.SegmentModified))
            targets.append((segmentationNode, slicer.vtkSegmentation.SourceRepresentationModified))
        if [(node, event) for node, event, _tag in self._observed] == targets:
            return
        self._unobserve()
        for node, event in targets:
            tag = node.AddObserver(event, lambda caller, eventName: self._refreshTimer.start())
            self._observed.append((node, event, tag))

    def _unobserve(self):
        for node, _event, tag in self._observed:
            try:
                node.RemoveObserver(tag)
            except Exception:
                pass
        self._observed = []

    def dispose(self):
        global _active
        self._refreshTimer.stop()
        self._unobserve()
        self._removeMenuAction()
        if self.controller is not None:
            self.controller.dispose()
        if _active is not None and _active() is self:
            _active = None

    #
    # Lesion card menu
    #

    def _addMenuAction(self):
        try:
            from . import draw

            card = draw.lesionCard()
        except Exception:
            card = None
        if card is not None and self.nc is not None:
            card.addMenuAction(_("Normalize to MNI…"), self.focusSelf)
            self._menuAdded = True

    def _removeMenuAction(self):
        if not self._menuAdded:
            return
        try:
            from . import draw

            card = draw.lesionCard()
            if card is not None:
                card.removeMenuAction(_("Normalize to MNI…"))
        except Exception:
            pass
        self._menuAdded = False

    def focusSelf(self):
        """Scroll the card into view and focus its button (never starts a job)."""
        self.refresh(None)
        if self.widget.isHidden():
            return False
        parent = self.widget.parentWidget()
        while parent is not None:
            if hasattr(parent, "ensureWidgetVisible"):
                parent.ensureWidgetVisible(self.widget)
                break
            parent = parent.parentWidget()
        (self.listRow if not self.listRow.isHidden() else self.button).setFocus()
        return True

    #
    # Actions
    #

    def start(self, scanOnly=False):
        """Normalize the editor's lesion (or only the scan); with an open study, the study's batch normalization."""
        if self.controller is None or self.controller.isBusy():
            return False
        ctx = self._context()
        segmentationNode, segmentId, source = ctx.get("segmentationNode"), ctx.get("segmentId"), ctx.get("sourceVolumeNode")
        if source is None:
            return False
        study = getattr(self.shell, "studySession", None) if self.shell is not None else None
        if study is not None:
            try:
                from . import libcard_normalize
            except ImportError:
                libcard_normalize = None
            if libcard_normalize is not None and hasattr(libcard_normalize, "normalizeSubjects"):
                sid = study.current() if hasattr(study, "current") else None
                if sid:
                    libcard_normalize.normalizeSubjects([sid])
                    return True
        result = self.controller.result
        if self._showsMniResult(result, segmentationNode, source):
            # "Trace on the MNI scan" put the result's warped scan and MNI lesion in the editor: Normalize again redoes the
            # native scan and lesion the result came from (the engine refuses a scan that is already on the template grid)
            native = self._nativeSources(result)
            if native is None:
                self._showMessage(_("The native scan of this result is no longer open: open it to normalize again."), ())
                return False
            source, segmentationNode, segmentId = native
            scanOnly = scanOnly or segmentationNode is None or self.nc.lesionIsEmpty(segmentationNode, segmentId)
        else:
            scanOnly = scanOnly or self._lesionEmpty
        # The options remembered in the Map card (template incl. the user's own, quality, modality, margin) apply here too
        request = self.nc.NormalizeRequest(scanNode=source, segmentationNode=None if scanOnly else segmentationNode, segmentId=None if scanOnly else segmentId,
                                           **self.nc.rememberedRequestOptions())
        self._clearMessage()
        self._clearQc()
        try:
            return self.controller.start(request)
        except Exception as exc:
            logging.exception("Studio: the normalization could not start")
            self._showMessage(_("The normalization could not start: {error}").format(error=exc), ())
            return False

    def stop(self):
        if self.controller is not None:
            self.controller.stop()

    def openInMap(self):
        """Show the Map card (options: template, quality, modality, margin, rough alignment)."""
        ctx = self._context()
        if self.nc is not None:
            # preferMap: from Draw, offerNormalize() would otherwise just focus this Draw card again
            return bool(self.nc.offerNormalize(ctx.get("segmentationNode"), ctx.get("segmentId"), self.shell, preferMap=True))
        return False

    def useForMap(self):
        """Make the MNI lesion the Map seed, then switch to Map (only the MNI copy of the lesion the editor shows)."""
        result = self.controller.result if self.controller is not None else None
        if result is None or result.segmentationNode is None:
            return False
        shown = self._context()
        if not self._ownsResult(result, shown.get("segmentationNode"), shown.get("sourceVolumeNode"), shown.get("segmentId")):
            self.refresh(None, checkEmpty=False)
            return False
        widget = getattr(slicer.modules, "LesionNetworkMappingWidget", None)
        ctx = getattr(widget, "ctx", None)
        if ctx is not None:
            ctx.setSeed(result.segmentationNode, result.segmentId)
            if hasattr(widget, "updateGUIFromParameterNode"):
                widget.updateGUIFromParameterNode()
        else:
            try:
                import LesionNetworkMapping

                parameterNode = LesionNetworkMapping.LesionNetworkMappingLogic().getParameterNode()
                parameterNode.seedSegmentation = result.segmentationNode
                parameterNode.seedSegmentId = result.segmentId or ""
            except Exception:
                logging.exception("Studio: the Map seed could not be set")
                return False
        try:
            from . import mappage

            mappage.noteSeedChoice()  # an explicit choice: entering Map keeps it (no Draw adoption)
        except Exception:
            logging.exception("Studio: the Map seed choice could not be recorded")
        self.controller.showInViews(result)
        if self.shell is not None:
            self.shell.switchTo("map")
        return True

    def traceOnMni(self):
        """Edit the template-space lesion on the warped scan."""
        result = self.controller.result if self.controller is not None else None
        editor = _segmentEditor()
        if result is None or editor is None or result.volumeNode is None:
            return False
        editor.setSegmentationNode(result.segmentationNode)
        editor.setSourceVolumeNode(result.volumeNode)
        if result.segmentId:
            editor.setCurrentSegmentID(result.segmentId)
        self.controller.showInViews(result)
        self.refresh(None)
        return True

    #
    # Controller callbacks
    #

    def _onState(self, state):
        nc = self.nc
        if state in (nc.PREPARING, nc.QUEUED) or (state == nc.LOADING and self._origin is None):
            ctx = self._context()  # what the editor shows when the run starts (also for a job started without start())
            self._origin = (ctx.get("segmentationNode"), ctx.get("sourceVolumeNode"))
        if self.progress is not None:
            busy = state in nc.BUSY_STATES
            self.progress.stopButton.setEnabled(busy and state != nc.LOADING)
            if state == nc.PREPARING:
                self.nc.setRowProgress(self.progress, 0.0, _("Exporting the scan and the lesion…"))
            elif state == nc.QUEUED:
                self.nc.setRowProgress(self.progress, 0.0, _("Starting…"))
            elif state == nc.LOADING:
                self.nc.setRowProgress(self.progress, 1.0, _("Loading the result…"))
        if state == nc.CANCELLED:
            self._showMessage(_("Normalization stopped. Nothing was changed."), (), level="neutral")
            if self.controller.result is not None:  # a stopped "Normalize again" gives the previous result back, with its QC
                self._clearQc()
                self._buildQc(self.controller.result)
                if self.controller.edited:
                    self._showEditState()
        elif state == nc.IDLE:  # the scene was closed: its result is gone
            self._origin = None
            self._clearMessage()
            self._clearQc()
        self.refresh(None, checkEmpty=False)

    def _onProgress(self, fraction, text, eta):
        if self.progress is not None:
            self.nc.setRowProgress(self.progress, fraction, text)

    def _onResult(self, result):
        self._clearMessage()
        self._clearQc()
        self._buildQc(result)
        qt.QTimer.singleShot(0, self._revealDoneRow)
        if self.shell is not None:
            try:
                if self._isScanOnly(result):
                    text = _("Scan normalized ({template}) · trace the lesion on it").format(template=self._templateName(result.templateId))
                else:
                    text = _("Lesion normalized ({template}) · review the alignment").format(template=self._templateName(result.templateId))
                self.shell.toast(text, 3000)
            except Exception:
                pass
        self.refresh(None, checkEmpty=False)

    def _buildQc(self, result):
        width = self.widget.width - 24 if self.widget.width > 100 else 260
        self._qcPanel = self.controller.buildQcWidget(result, width=min(width, 360), onRedo=self._redo, maxHeight=110)
        self.qcHost.addWidget(self._qcPanel)

    def _revealDoneRow(self):
        """Scroll the inspector so that Use for Map is visible (without moving the focus)."""
        if self.doneRow.isHidden():
            return
        parent = self.widget.parentWidget()
        while parent is not None:
            if hasattr(parent, "ensureWidgetVisible"):
                parent.ensureWidgetVisible(self.doneRow)
                break
            parent = parent.parentWidget()

    def _redo(self, changes):
        result = self.controller.result
        if result is None or result.request is None or self.controller.isBusy():
            return False
        self._clearQc()
        return self.controller.start(result.request.copy(**changes))

    def _onFailed(self, info):
        text, offer = self.nc.describeFailure(info)
        actions = []
        if offer:
            actions.append((_("Options in Map…"), self.openInMap))
        self._showMessage(text, actions)
        self.refresh(None, checkEmpty=False)

    def _showEditState(self):
        controller = self.controller
        if not controller.edited:
            if self.message is not None and self.message.property("editNotice"):
                self._clearMessage()
            return
        if controller.needsReregister:
            self._showMessage(_("Lesion edited after normalization: the edit leaves the masked area, so register again."),
                              ((_("Re-register"), lambda: self._redo({})),))
        else:
            self._showMessage(_("Lesion edited after normalization."), ((_("Update MNI lesion"), self.updateMniLesion),))
        self.message.setProperty("editNotice", True)
        self._showState()

    def updateMniLesion(self):
        self._showMessage(_("Updating the MNI lesion…"), (), level="busy")
        self.message.setProperty("editNotice", True)

        def done(ok):
            if ok:
                self._clearMessage()
            else:
                self._showMessage(_("The MNI lesion could not be updated."), ())

        return self.controller.updateMniLesion(done)

    def _showMessage(self, text, actions, level="warn"):
        self._clearMessage()
        widgets = _lnmWidgets()
        if widgets is None:
            return
        self.message = widgets.banner(text, actions, level=level)
        self.message.setObjectName("StudioNormalizeDrawMessage")
        self.messageHost.addWidget(self.message)

    def _clearMessage(self):
        if self.message is not None:
            self.messageHost.removeWidget(self.message)
            self.message.hide()
            self.message.deleteLater()
            self.message = None

    def _clearQc(self):
        if self._qcPanel is not None:
            self.qcHost.removeWidget(self._qcPanel)
            self._qcPanel.hide()
            self._qcPanel.deleteLater()
            self._qcPanel = None


def active():
    """The Draw card of the current Draw panel, or None."""
    card = _active() if _active is not None else None
    return card


def focus(segmentationNode=None, segmentId=None):
    """``offerNormalize()`` in Draw: show and focus the card (the lesion must be the edited one); never starts a job."""
    card = active()
    if card is None:
        return False
    ctx = card._context()
    if segmentationNode is not None and ctx.get("segmentationNode") is not segmentationNode:
        return False
    return card.focusSelf()


DRAW_CARD = {
    "key": "normalize",
    "order": 120,  # after Display (100): Brush still fits at 1366 x 768
    "factory": DrawNormalizeCard,
}

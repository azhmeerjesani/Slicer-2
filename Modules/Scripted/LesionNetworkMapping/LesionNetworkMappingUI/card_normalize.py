"""Normalize to MNI card of the Map module (order 15, lesion mode, Studio and Classic; A-03b).

The card appears right under Seed when the seed lesion is in the scan's own space (not on an MNI grid) and
is not itself a normalization result. One click on Normalize to MNI exports the lesion and the scan, runs
the A-03 normalization in the background (progress, Stop), loads the template-space lesion as a new
segmentation, makes it the seed and shows the QC mosaic and suggestion. The native segmentation is kept.
Compute stays enabled: a review is recommended, not required, for a single lesion.

Options (a non-modal popover): template (the connectome's family when its files are present), quality,
modality with an optional anatomical T1w, the lesion margin excluded from matching, the whole-head template
(explicit download), a rough manual alignment, and "Remember these options" (the fork-only setting
``Studio/LNM/Normalize/LastOptions``, written only when ticked). Nothing is downloaded without a click.
"""

import json
import logging
import os
import time

import qt
import slicer
import vtk
from slicer.i18n import tr as _

from . import ORDER_NORMALIZE, widgets
from . import normalize_controller as nc

QUALITY_LABELS = (
    ("standard", "Standard (affine + coarse nonlinear, ~30 s)"),
    ("fine", "Fine (~70 s)"),
    ("affine", "Affine only (~15 s)"),
)
ROUGH_ALIGNMENT_NAME = "Rough alignment"
EXPLANATION = "This lesion is in the scan's own space. Lesion network mapping needs it in the connectome's MNI space."


class _ShowFilter(qt.QObject):
    """Calls ``callback`` soon after the watched widget is shown (the Map widget shows every card of the seed mode)."""

    def __init__(self, widget, callback):
        qt.QObject.__init__(self, widget)
        self.callback = callback
        widget.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.Show:
            qt.QTimer.singleShot(0, self.callback)
        return False


def _bundleSpace(bundle):
    """Template id of a connectome bundle's space family (NLin6Asym for FSL-family connectomes), or None."""
    if bundle is None:
        return None
    try:
        from LesionNetworkMappingLib import geometry

        detected = geometry.detect_space((tuple(bundle.shape), bundle.affine))
        name = detected[0] if detected else (getattr(bundle, "metadata", None) or {}).get("space")
        family = geometry.space_family(name)
    except Exception:
        return None
    if family == "fsl6":
        return nc.NLIN6_TEMPLATE
    if family in ("2009", "2009b"):
        return nc.DEFAULT_TEMPLATE
    return None


class NormalizeCard:
    """Controller of the Normalize to MNI card (``widget`` is a collapsible card named LNMNormalizeCard)."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.controller = nc.NormalizeController(context=ctx, name="map")
        self.options = self._loadOptions()
        self.roughTransform = None
        self.anatNode = None
        self._seed = (None, None)
        self._applicable = False
        self._preselected = None
        self._qcPanel = None
        self._download = None
        self._observedParameterNode = None
        self._parameterObserver = None
        self._refreshTimer = qt.QTimer()
        self._refreshTimer.setSingleShot(True)
        self._refreshTimer.setInterval(0)
        self._refreshTimer.connect("timeout()", self.refresh)

        self.widget = widgets.card(_("Normalize to MNI"), objectName="LNMNormalizeCard")
        layout = qt.QVBoxLayout(self.widget)
        layout.setSpacing(6)
        self.explanation = self._label(_(EXPLANATION), "LNMNormalizeExplanation")
        layout.addWidget(self.explanation)

        form = qt.QFormLayout()
        self.scanSelector = slicer.qMRMLNodeComboBox()
        self.scanSelector.setObjectName("LNMNormalizeScanSelector")
        self.scanSelector.nodeTypes = ["vtkMRMLScalarVolumeNode"]
        self.scanSelector.showChildNodeTypes = False
        self.scanSelector.addEnabled = False
        self.scanSelector.removeEnabled = False
        self.scanSelector.renameEnabled = False
        self.scanSelector.noneEnabled = True
        self.scanSelector.noneDisplay = _("Choose the scan")
        self.scanSelector.setMRMLScene(slicer.mrmlScene)
        self.scanSelector.setToolTip(_("The scan the lesion was traced on (it is registered to the template)"))
        self.scanSelector.connect("currentNodeChanged(vtkMRMLNode*)", lambda node: self.refreshState())
        form.addRow(_("Scan"), self.scanSelector)
        layout.addLayout(form)

        self.templateNote = self._label("", "LNMNormalizeTemplateNote")
        layout.addWidget(self.templateNote)

        row = qt.QHBoxLayout()
        row.setSpacing(6)
        self.normalizeButton = qt.QPushButton(_("Normalize to MNI"))
        self.normalizeButton.setObjectName("LNMNormalizeButton")
        self.normalizeButton.setProperty("slicerRole", "primary")
        self.normalizeButton.setToolTip(_("Register the scan to the MNI template with the lesion masked out, and warp the lesion (runs in the background)"))
        self.normalizeButton.connect("clicked()", self.start)
        row.addWidget(self.normalizeButton)
        self.optionsButton = qt.QPushButton(_("Options…"))
        self.optionsButton.setObjectName("LNMNormalizeOptionsButton")
        self.optionsButton.setProperty("slicerRole", "ghost")
        self.optionsButton.setFlat(True)
        self.optionsButton.connect("clicked()", self.showOptions)
        row.addWidget(self.optionsButton)
        row.addStretch(1)
        self.folderButton = qt.QPushButton(_("Open folder"))
        self.folderButton.setObjectName("LNMNormalizeOpenFolder")
        self.folderButton.setProperty("slicerRole", "ghost")
        self.folderButton.setFlat(True)
        self.folderButton.connect("clicked()", self.openFolder)
        self.folderButton.hide()
        row.addWidget(self.folderButton)
        layout.addLayout(row)

        self.reasonLabel = self._label("", "LNMNormalizeReason")
        layout.addWidget(self.reasonLabel)

        self.progress = widgets.ProgressRow(objectNamePrefix="LNMNormalize")
        self.progress.bar.setObjectName("LNMNormalizeProgress")
        self.progress.stopButton.setToolTip(_("Stop the normalization (the previous result, if any, is kept)"))
        self.progress.stopButton.connect("clicked()", self.stop)
        # One elided line: a word-wrapped status text would lay out the whole Map panel again on every update
        self.progress.label.setWordWrap(False)
        self.progress.label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        self.progress.widget.hide()
        layout.addWidget(self.progress.widget)

        self.messageHost = qt.QVBoxLayout()
        self.messageHost.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self.messageHost)
        self.message = None
        self.qcHost = qt.QVBoxLayout()
        self.qcHost.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self.qcHost)

        self.optionsPopover = None
        self._showFilter = _ShowFilter(self.widget, self._applyVisibility)

        self.controller.stateChanged.append(self._onState)
        self.controller.progressChanged.append(self._onProgress)
        self.controller.resultReady.append(self._onResult)
        self.controller.failed.append(self._onFailed)
        self.controller.editStateChanged.append(lambda edited, reregister: self._showEditState())
        ctx.seedChanged.append(self.refresh)
        ctx.bundleChanged.append(self.refreshState)
        self._observeParameterNode()
        self.refresh()

    @staticmethod
    def _label(text, objectName):
        label = qt.QLabel(text)
        label.setObjectName(objectName)
        label.setWordWrap(True)
        label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        label.setProperty("slicerRole", "secondaryText")
        return label

    #
    # Card protocol
    #

    def onSeedChecked(self, check):
        self.refresh()

    def onStudyChanged(self):
        self.refreshState()

    def cleanup(self):
        for name, callback in (("seedChanged", self.refresh), ("bundleChanged", self.refreshState)):
            callbacks = getattr(self.ctx, name, [])
            if callback in callbacks:
                callbacks.remove(callback)
        if self._observedParameterNode is not None and self._parameterObserver is not None:
            self._observedParameterNode.RemoveObserver(self._parameterObserver)
        self._observedParameterNode = None
        if self.optionsPopover is not None:
            self.optionsPopover.close()
        self.controller.dispose()

    def _observeParameterNode(self):
        """Seed changes made directly in the parameter node (Draw's "Use for Map") are noticed here."""
        wrapper = self.ctx.parameterNode
        node = getattr(wrapper, "parameterNode", wrapper)
        if node is self._observedParameterNode:
            return
        if self._observedParameterNode is not None and self._parameterObserver is not None:
            self._observedParameterNode.RemoveObserver(self._parameterObserver)
        self._observedParameterNode = None
        if node is not None and hasattr(node, "AddObserver"):
            self._parameterObserver = node.AddObserver(vtk.vtkCommand.ModifiedEvent, lambda caller, event: self._refreshTimer.start())
            self._observedParameterNode = node

    #
    # State
    #

    def _seedNow(self):
        try:
            return self.ctx.seed()
        except Exception:
            return None, None

    def _showsResult(self, segmentationNode):
        result = self.controller.result
        return result is not None and segmentationNode is not None and segmentationNode is result.segmentationNode

    def isApplicable(self):
        return self._applicable

    def _widgetGone(self):
        """True once the card's widget was deleted (a deferred refresh can arrive after the Map panel, at exit)."""
        try:
            self.widget.objectName  # noqa: B018 (PythonQt raises for a deleted object)
        except Exception:
            return True
        return False

    def refresh(self):
        """Re-evaluate the seed: the card is shown for a native seed (and while it shows this card's job or result)."""
        if self._widgetGone():
            return
        self._observeParameterNode()
        segmentationNode, segmentId = self._seedNow()
        self._seed = (segmentationNode, segmentId)
        if segmentationNode is None or not slicer.mrmlScene.IsNodePresent(segmentationNode):
            self._applicable = False
        elif self._showsResult(segmentationNode):
            self._applicable = True
        elif nc.isNormalizationResult(segmentationNode):
            self._applicable = False
        else:
            result = self.controller.result
            if result is not None and segmentationNode is result.nativeSegmentationNode:
                self.controller.showNative(result)
            source = nc.findSourceVolume(segmentationNode)
            self._applicable = nc.isNativeSegmentation(segmentationNode) and (source is None or nc.spaceOf(source) is None)
            if self._applicable and source is not None and self._preselected is None:
                current = self.scanSelector.currentNode()
                if current is None or nc.isNormalizationResult(current) or current is not source:
                    self.scanSelector.setCurrentNode(source)
        self._applyVisibility()
        self.refreshState()

    def preselect(self, segmentationNode, segmentId):
        """offerNormalize(): make this lesion the one to normalize and preselect its scan (no job is started)."""
        if segmentationNode is not None:
            seed = self._seedNow()
            if seed[0] is not segmentationNode or (segmentId and seed[1] != segmentId):
                self.ctx.setSeed(segmentationNode, segmentId)
            source = nc.findSourceVolume(segmentationNode)
            if source is not None:
                self.scanSelector.setCurrentNode(source)
        self.refresh()

    def _seedMode(self):
        node = self.ctx.parameterNode
        try:
            return getattr(node, "seedMode", None) or "lesion"
        except Exception:
            return "lesion"

    def _applyVisibility(self):
        """Hide the card unless it is useful; the Map widget shows every card of the seed mode, this filter corrects it."""
        if self._widgetGone() or self.widget.parentWidget() is None:
            return
        wanted = (self._applicable or self.controller.isBusy()) and self._seedMode() == "lesion"
        if not wanted and not self.widget.isHidden():
            self.widget.hide()
        elif wanted and self.widget.isHidden():
            self.widget.show()

    def templateId(self):
        """The chosen template, else the connectome's family when its files are present, else MNI152NLin2009cAsym."""
        chosen = self.options.get("templateId")
        if chosen in nc.TEMPLATE_IDS or (nc.isUserTemplate(chosen) and nc.templateAvailable(chosen)):
            return chosen
        wanted = _bundleSpace(self.ctx.bundle)
        if wanted == nc.NLIN6_TEMPLATE and nc.templateAvailable(nc.NLIN6_TEMPLATE):
            return nc.NLIN6_TEMPLATE
        return nc.DEFAULT_TEMPLATE

    def _templateNoteText(self):
        wanted = _bundleSpace(self.ctx.bundle)
        templateId = self.templateId()
        if nc.isUserTemplate(templateId):
            # Your own template (A-10): the registration quality depends on it (experimental, D-A12)
            return _("Registered to your template {name}: the registration quality depends on the template (experimental).").format(
                name=nc.templateName(templateId))
        if wanted and wanted != templateId:
            return _("Registered to {template}; the connectome uses {wanted}: a few mm difference (QA shows the coverage)").format(template=templateId, wanted=wanted)
        return ""

    def _disabledReason(self):
        segmentationNode, segmentId = self._seed
        if segmentationNode is None or not segmentId:
            return _("Choose the lesion in Seed.")
        if self.scanSelector.currentNode() is None:
            return _("Choose the scan the lesion was traced on.")
        if nc.isNormalizationResult(self.scanSelector.currentNode()):
            return _("This scan is already a normalization result.")
        templateId = self.templateId()
        if not nc.templateAvailable(templateId):
            if nc.isUserTemplate(templateId):
                return _("Your template {name} needs an image and a brain mask (Settings > Templates).").format(name=nc.templateName(templateId))  # noqa: RUF001
            return _("The {template} template is not on this computer (Options → download it).").format(template=templateId)
        available = nc.normalizationAvailable()
        if not available:
            return available[1]
        return ""

    def refreshState(self):
        busy = self.controller.isBusy()
        reason = "" if busy else self._disabledReason()
        self.normalizeButton.setEnabled(not busy and not reason)
        self.reasonLabel.setText(reason)
        self.reasonLabel.setVisible(bool(reason) and self._applicable and not self._showsResult(self._seed[0]))
        note = self._templateNoteText()
        self.templateNote.setText(note)
        self.templateNote.setVisible(bool(note))
        showingResult = self._showsResult(self._seed[0])
        self.explanation.setVisible(not showingResult)
        self.normalizeButton.setText(_("Normalize again") if showingResult else _("Normalize to MNI"))
        self.folderButton.setVisible(bool(getattr(self.controller, "outDir", None)) and os.path.isdir(getattr(self.controller, "outDir", "") or ""))

    #
    # Options
    #

    def _loadOptions(self):
        """Options of the last run when they were remembered (``Studio/LNM/Normalize/LastOptions``, read only here)."""
        return nc.rememberedOptions()

    def rememberOptions(self):
        """Write the options to ``Studio/LNM/Normalize/LastOptions`` (only when "Remember these options" is ticked; unticking forgets them)."""
        if not self.options.get("remember"):
            settings = qt.QSettings()
            if settings.contains(nc.LAST_OPTIONS_SETTING):
                settings.remove(nc.LAST_OPTIONS_SETTING)
            return False
        stored = {key: self.options.get(key) for key in ("templateId", "quality", "modality", "dilateMm")}
        qt.QSettings().setValue(nc.LAST_OPTIONS_SETTING, json.dumps(stored, sort_keys=True))
        return True

    def _buildOptions(self):
        popover = qt.QFrame(self.widget, qt.Qt.Popup)
        popover.setObjectName("LNMNormalizeOptions")
        popover.setFrameShape(qt.QFrame.StyledPanel)
        popover.setMinimumWidth(320)
        layout = qt.QVBoxLayout(popover)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)
        form = qt.QFormLayout()

        self.templateCombo = qt.QComboBox()
        self.templateCombo.setObjectName("LNMNormalizeTemplate")
        self._fillTemplateCombo()
        self.templateCombo.connect("currentIndexChanged(int)", self._onOptionsChanged)
        form.addRow(_("Template"), self.templateCombo)

        self.qualityCombo = qt.QComboBox()
        self.qualityCombo.setObjectName("LNMNormalizeQuality")
        for quality, label in QUALITY_LABELS:
            self.qualityCombo.addItem(_(label), quality)
        self.qualityCombo.connect("currentIndexChanged(int)", self._onOptionsChanged)
        form.addRow(_("Quality"), self.qualityCombo)

        self.modalityCombo = qt.QComboBox()
        self.modalityCombo.setObjectName("LNMNormalizeModality")
        self.modalityCombo.addItem(_("Automatic"), "auto")
        for modality in nc.MODALITIES:
            self.modalityCombo.addItem(modality, modality)
        self.modalityCombo.connect("currentIndexChanged(int)", self._onOptionsChanged)
        form.addRow(_("Modality"), self.modalityCombo)

        self.anatSelector = slicer.qMRMLNodeComboBox()
        self.anatSelector.setObjectName("LNMNormalizeAnatSelector")
        self.anatSelector.nodeTypes = ["vtkMRMLScalarVolumeNode"]
        self.anatSelector.showChildNodeTypes = False
        self.anatSelector.addEnabled = False
        self.anatSelector.removeEnabled = False
        self.anatSelector.noneEnabled = True
        self.anatSelector.noneDisplay = _("None")
        self.anatSelector.setMRMLScene(slicer.mrmlScene)
        self.anatSelector.setToolTip(_("Anatomical T1w of the same subject (recommended for FLAIR, DWI, CT): the scan is aligned to it first"))
        self.anatSelector.connect("currentNodeChanged(vtkMRMLNode*)", self._onOptionsChanged)
        form.addRow(_("Anatomical T1w"), self.anatSelector)

        self.marginSpinBox = qt.QDoubleSpinBox()
        self.marginSpinBox.setObjectName("LNMNormalizeMargin")
        self.marginSpinBox.setRange(1.0, 10.0)
        self.marginSpinBox.setSingleStep(1.0)
        self.marginSpinBox.setDecimals(0)
        self.marginSpinBox.setSuffix(" mm")
        self.marginSpinBox.setToolTip(_("Lesion margin excluded from matching (cost-function masking)"))
        self.marginSpinBox.connect("valueChanged(double)", self._onOptionsChanged)
        form.addRow(_("Lesion margin"), self.marginSpinBox)
        layout.addLayout(form)

        self.nlin6Button = qt.QPushButton(_("Download NLin6Asym 2 mm (1.4 MB, FSL licence terms: non-commercial)"))
        self.nlin6Button.setObjectName("LNMNormalizeDownloadNLin6")
        self.nlin6Button.setProperty("slicerRole", "secondary")
        self.nlin6Button.setToolTip(_("FSL licence terms apply (non-commercial). Downloaded from TemplateFlow into the application cache, checked by SHA-256."))
        self.nlin6Button.connect("clicked()", lambda: self.downloadTemplate(nc.NLIN6_TEMPLATE, ("fixed", "mask")))
        layout.addWidget(self.nlin6Button)

        self.headLabel = self._label("", "LNMNormalizeHeadStatus")
        layout.addWidget(self.headLabel)
        self.headButton = qt.QPushButton(_("Download head template (1.8 MB)"))
        self.headButton.setObjectName("LNMNormalizeDownloadHead")
        self.headButton.setProperty("slicerRole", "secondary")
        self.headButton.setToolTip(_("TemplateFlow's whole-head MNI152NLin2009cAsym 2 mm T1w: it lets a strongly tilted head be recovered"))
        self.headButton.connect("clicked()", lambda: self.downloadTemplate(nc.DEFAULT_TEMPLATE, ("head",)))
        layout.addWidget(self.headButton)

        self.roughButton = qt.QPushButton(_("Help it: rough manual alignment…"))
        self.roughButton.setObjectName("LNMNormalizeRoughAlignment")
        self.roughButton.setProperty("slicerRole", "ghost")
        self.roughButton.setToolTip(_("Move the scan roughly onto the template with the Transforms module; that position starts the registration"))
        self.roughButton.connect("clicked()", self.startRoughAlignment)
        layout.addWidget(self.roughButton)
        self.roughLabel = self._label("", "LNMNormalizeRoughStatus")
        layout.addWidget(self.roughLabel)

        self.rememberCheckBox = qt.QCheckBox(_("Remember these options"))
        self.rememberCheckBox.setObjectName("LNMNormalizeRemember")
        self.rememberCheckBox.connect("toggled(bool)", self._onOptionsChanged)
        layout.addWidget(self.rememberCheckBox)
        self.downloadStatus = self._label("", "LNMNormalizeDownloadStatus")
        layout.addWidget(self.downloadStatus)
        self.optionsPopover = popover
        self._showOptionValues()

    def _fillTemplateCombo(self):
        """(Re)fill the Template list: Automatic, the standard templates, then the user's templates with a brain mask (A-10).

        The user templates change while the app runs (Settings > Templates), so showOptions() calls this each time
        the popover opens; the selection is restored afterwards by _showOptionValues().
        """
        combo = self.templateCombo
        wasBlocked = combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem(_("Automatic (the connectome's family)"), "")
            for templateId in nc.TEMPLATE_IDS:
                combo.addItem(templateId, templateId)
            for templateId, name in nc.userTemplates():
                combo.addItem(_("{name} (yours)").format(name=name), templateId)
        finally:
            combo.blockSignals(wasBlocked)

    def _showOptionValues(self):
        widgetsToBlock = (self.templateCombo, self.qualityCombo, self.modalityCombo, self.marginSpinBox, self.rememberCheckBox, self.anatSelector)
        blocked = [widget.blockSignals(True) for widget in widgetsToBlock]
        try:
            self.templateCombo.setCurrentIndex(max(0, self.templateCombo.findData(self.options.get("templateId") or "")))
            self.qualityCombo.setCurrentIndex(max(0, self.qualityCombo.findData(self.options.get("quality"))))
            self.modalityCombo.setCurrentIndex(max(0, self.modalityCombo.findData(self.options.get("modality"))))
            self.marginSpinBox.setValue(float(self.options.get("dilateMm") or nc.DEFAULT_DILATE_MM))
            self.rememberCheckBox.setChecked(bool(self.options.get("remember")))
            self.anatSelector.setCurrentNode(self.anatNode if self.anatNode is not None else self._guessAnat())
        finally:
            for widget, wasBlocked in zip(widgetsToBlock, blocked, strict=True):
                widget.blockSignals(wasBlocked)
        self._refreshOptionsState()

    def _guessAnat(self):
        """A scene volume named like a T1w (T1, MPRAGE) other than the scan, when the scan is not a T1w."""
        scan = self.scanSelector.currentNode()
        if scan is None:
            return None
        modality = self.options.get("modality")
        if modality in (None, "", "auto"):
            modality = nc.guessModality(scan)[0]
        if modality == "T1w":
            return None
        for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
            if node is scan or node.IsA("vtkMRMLLabelMapVolumeNode") or nc.isNormalizationResult(node):
                continue
            if nc.guessModality(node) == ("T1w", False):
                return node
        return None

    def _refreshOptionsState(self):
        if self.optionsPopover is None:
            return
        scan = self.scanSelector.currentNode()
        if scan is not None and self.options.get("modality") in (None, "", "auto"):
            modality, guessed = nc.guessModality(scan)
            self.modalityCombo.setItemText(0, _("Automatic ({modality}{guessed})").format(modality=modality, guessed=_(", guessed") if guessed else ""))
        nlin6Missing = not nc.templateAvailable(nc.NLIN6_TEMPLATE)
        self.nlin6Button.setVisible(nlin6Missing and (self.options.get("templateId") == nc.NLIN6_TEMPLATE or _bundleSpace(self.ctx.bundle) == nc.NLIN6_TEMPLATE))
        headPresent = nc.headTemplateAvailable(nc.DEFAULT_TEMPLATE)
        self.headLabel.setText(_("Whole-head template: present (tilt recovery available)") if headPresent
                               else _("Whole-head template: not on this computer (only needed when a tilted head cannot be aligned)"))
        self.headButton.setVisible(not headPresent)
        rough = self._roughAlignmentInUse()
        self.roughLabel.setText(_("The rough alignment starts the registration.") if rough else "")
        self.roughLabel.setVisible(rough)

    def _onOptionsChanged(self, *args):
        if self.optionsPopover is None:
            return
        self.options["templateId"] = self.templateCombo.currentData or None
        self.options["quality"] = self.qualityCombo.currentData or "standard"
        self.options["modality"] = self.modalityCombo.currentData or "auto"
        self.options["dilateMm"] = float(self.marginSpinBox.value)
        self.options["remember"] = bool(self.rememberCheckBox.checked)
        self.anatNode = self.anatSelector.currentNode()
        self.rememberOptions()
        self._refreshOptionsState()
        self.refreshState()

    def showOptions(self):
        """Show the Options popover under the Options button (non-modal: a click outside closes it)."""
        if self.optionsPopover is None:
            self._buildOptions()
        else:
            self._fillTemplateCombo()
            self._showOptionValues()
        popover = self.optionsPopover
        popover.adjustSize()
        point = self.optionsButton.mapToGlobal(qt.QPoint(0, self.optionsButton.height))
        popover.move(point)
        popover.show()
        return popover

    #
    # Template downloads (explicit clicks only)
    #

    def downloadTemplate(self, templateId, roles, then=None):
        """Download template files after a click (never under --testing); ``then()`` runs once they are present."""
        if self._download is not None and getattr(self._download, "isRunning", lambda: False)():
            return False
        size = nc.templateDownloadSize(templateId, roles)
        self._setDownloadStatus(_("Downloading {template} ({size:.1f} MB)…").format(template=templateId, size=size / 1e6))

        def finished(paths):
            self._download = None
            self._setDownloadStatus(_("Downloaded {template}.").format(template=templateId))
            self._refreshOptionsState()
            self.refreshState()
            if then is not None:
                then()

        def failed(message):
            self._download = None
            self._setDownloadStatus(message)

        self._download = nc.downloadTemplateFiles(templateId, roles, finished, failed)
        return True

    def _setDownloadStatus(self, text):
        if self.optionsPopover is not None:
            self.downloadStatus.setText(text)
        if text:
            self._showMessage(text, (), level="neutral")

    #
    # Rough manual alignment
    #

    def _roughAlignmentInUse(self):
        scan = self.scanSelector.currentNode()
        transform = self.roughTransform
        return scan is not None and transform is not None and slicer.mrmlScene.IsNodePresent(transform) and scan.GetParentTransformNode() is transform

    def startRoughAlignment(self):
        """Create the linear transform "Rough alignment", put the scan under it and open Transforms with interaction handles."""
        scan = self.scanSelector.currentNode()
        if scan is None:
            return None
        transform = self.roughTransform
        if transform is None or not slicer.mrmlScene.IsNodePresent(transform):
            transform = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLinearTransformNode", ROUGH_ALIGNMENT_NAME)
            self.roughTransform = transform
        transform.CreateDefaultDisplayNodes()
        display = transform.GetDisplayNode()
        if display is not None:
            display.SetEditorVisibility(True)
            display.SetEditorSliceIntersectionVisibility(True)
        scan.SetAndObserveTransformNodeID(transform.GetID())
        if self.optionsPopover is not None:
            self.optionsPopover.close()
        if not slicer.app.testingEnabled():
            slicer.util.selectModule("Transforms")
            transformsWidget = getattr(slicer.modules, "transforms", None)
            if transformsWidget is not None:
                try:
                    transformsWidget.widgetRepresentation().setEditedNode(transform)
                except Exception:
                    pass
        self.refreshState()
        return transform

    #
    # Actions
    #

    def request(self, **changes):
        """The NormalizeRequest of the card's current seed, scan and options."""
        segmentationNode, segmentId = self._seed
        scan = self.scanSelector.currentNode()
        anat = self.anatNode if self.anatNode is not None and slicer.mrmlScene.IsNodePresent(self.anatNode) else None
        request = nc.NormalizeRequest(scanNode=scan, segmentationNode=segmentationNode, segmentId=segmentId, anatNode=anat,
                                      templateId=self.templateId(), quality=self.options.get("quality") or "standard",
                                      modality=self.options.get("modality") or "auto", dilateMm=float(self.options.get("dilateMm") or nc.DEFAULT_DILATE_MM),
                                      initialTransformNode=self.roughTransform if self._roughAlignmentInUse() else None)
        studyPath = getattr(self.ctx.study, "path", None) if self.ctx.study is not None else None
        sid = segmentationNode.GetAttribute("Studio.ParticipantId") if segmentationNode is not None else None
        if studyPath and sid:
            request.studyPath, request.sid = os.fspath(studyPath), sid
        for key, value in changes.items():
            setattr(request, key, value)
        return request

    def start(self, **changes):
        """The primary button: start the normalization of the seed (returns whether it started)."""
        if self.controller.isBusy() or self._disabledReason():
            return False
        self._clearMessage()
        self._clearQc()
        if self.options.get("remember"):
            self.rememberOptions()
        try:
            return self.controller.start(self.request(**changes))
        except Exception as exc:
            logging.exception("Lesion network mapping: the normalization could not start")
            self._showMessage(_("The normalization could not start: {error}").format(error=exc), ())
            return False

    def redo(self, changes):
        """⋯ Redo: affine only / fine / with a 6 mm margin, from the lesion that was normalized."""
        result = self.controller.result
        if result is None or result.request is None:
            return False
        self._clearMessage()
        self._clearQc()
        return self.controller.start(result.request.copy(**changes))

    def stop(self):
        self.controller.stop()

    def openFolder(self):
        outDir = getattr(self.controller, "outDir", None)
        if outDir and os.path.isdir(outDir):
            widgets.showInFileManager(outDir)

    #
    # Controller callbacks
    #

    def _onState(self, state):
        busy = state in nc.BUSY_STATES
        self.progress.widget.setVisible(busy)
        self.progress.stopButton.setEnabled(busy and state != nc.LOADING)
        if state == nc.PREPARING:
            nc.setRowProgress(self.progress, 0.0, _("Exporting the scan and the lesion…"))
        elif state == nc.QUEUED:
            nc.setRowProgress(self.progress, 0.0, _("Starting…"))
        elif state == nc.LOADING:
            nc.setRowProgress(self.progress, 1.0, _("Loading the result…"))
        elif state == nc.CANCELLED:
            self._showMessage(_("Normalization stopped. Nothing was changed."), (), level="neutral")
        elif state == nc.IDLE:  # the scene was closed: its result is gone
            self._clearMessage()
            self._clearQc()
        self._applyVisibility()
        self.refreshState()

    def _onProgress(self, fraction, text, eta):
        nc.setRowProgress(self.progress, fraction, text)

    def _onResult(self, result):
        started = time.perf_counter()
        self._clearMessage()
        self._clearQc()
        width = max(160, self.widget.width - 40) if self.widget.width > 0 else 280
        self._qcPanel = self.controller.buildQcWidget(result, width=min(width, 420), onRedo=self.redo, maxHeight=240)
        self.qcHost.addWidget(self._qcPanel)
        warnings = [item.get("message") for item in self.controller.warnings if item.get("code") in ("modality_guessed", "cross_modality", "template_family_mismatch")]
        if warnings:
            self._showMessage("; ".join(text for text in warnings if text), (), level="neutral")
        self.controller.timings["qc_widget_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
        qt.QTimer.singleShot(0, lambda: self._useResult(result))

    def _useResult(self, result):
        """The new lesion becomes the seed (the native segmentation is kept)."""
        if self.controller.result is not result:
            return
        if result.segmentationNode is not None:
            self.ctx.setSeed(result.segmentationNode, result.segmentId)
            widget = getattr(slicer.modules, "LesionNetworkMappingWidget", None)
            if widget is not None and getattr(widget, "ctx", None) is self.ctx and hasattr(widget, "updateGUIFromParameterNode"):
                widget.updateGUIFromParameterNode()  # the Seed card's selectors follow the parameter node only there
        qt.QTimer.singleShot(0, lambda: self.controller.result is result and self.controller.showInViews(result))  # its own short pause
        shell = self.ctx.shell()
        if shell is not None:
            try:
                shell.toast(_("Lesion normalized ({template}) · review the alignment").format(template=result.templateId), 3000)
            except Exception:
                pass
        self.refresh()

    def _onFailed(self, info):
        text, offer = nc.describeFailure(info)
        actions = []
        if offer == "head":
            actions.append((_("Download head template (1.8 MB, TemplateFlow) and retry"),
                            lambda: self.downloadTemplate(nc.DEFAULT_TEMPLATE, ("head",), then=lambda: self.start(**{"quality": self.controller.request.quality}))))
        elif offer and offer.startswith("template:"):
            templateId = offer.split(":", 1)[1]
            label = _("Download NLin6Asym 2 mm (1.4 MB, FSL licence terms: non-commercial) and retry") if templateId == nc.NLIN6_TEMPLATE else _("Download the template and retry")
            actions.append((label, lambda: self.downloadTemplate(templateId, ("fixed", "mask"), then=self.start)))
        job = info.get("job")
        if job is not None and getattr(job, "logPath", None):
            actions.append((_("Show log"), lambda: widgets.showInFileManager(job.logPath)))
        self._showMessage(text, actions, level="warn")
        self.refreshState()

    def _showEditState(self):
        controller = self.controller
        if not controller.edited:
            if self.message is not None and self.message.property("editNotice"):
                self._clearMessage()
            return
        if controller.needsReregister:
            text = _("Lesion edited after normalization: the edit leaves the masked area, so register again.")
            actions = ((_("Re-register"), lambda: self.redo({})),)
        else:
            text = _("Lesion edited after normalization.")
            actions = ((_("Update MNI lesion"), self.updateMniLesion),)
        self._showMessage(text, actions, level="warn")
        self.message.setProperty("editNotice", True)

    def updateMniLesion(self):
        self._showMessage(_("Updating the MNI lesion…"), (), level="busy")
        self.message.setProperty("editNotice", True)

        def done(ok):
            if ok:
                self._clearMessage()
            else:
                self._showMessage(_("The MNI lesion could not be updated."), (), level="warn")

        return self.controller.updateMniLesion(done)

    #
    # Messages and QC
    #

    def _showMessage(self, text, actions, level="warn"):
        self._clearMessage()
        self.message = widgets.banner(text, actions, level=level)
        self.message.setObjectName("LNMNormalizeMessage")
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


def _existingCard():
    widget = getattr(slicer.modules, "LesionNetworkMappingWidget", None)
    ctx = getattr(widget, "ctx", None)
    return ctx.card("normalize") if ctx is not None else None


def _paletteNormalize():
    """Search: open Map and show the Normalize card for the seed (no job starts without the card's button)."""
    import LesionNetworkMapping

    def then(widget):
        segmentationNode, segmentId = widget.ctx.seed()
        nc.offerNormalize(widget.ctx, segmentationNode, segmentId)

    LesionNetworkMapping._openMap(then)


def _paletteEnabled():
    card = _existingCard()
    if card is not None and hasattr(card, "isApplicable"):
        return card.isApplicable()
    return bool(nc.normalizationAvailable())


CARD = {
    "id": "normalize",
    "title": "Normalize to MNI",
    "order": ORDER_NORMALIZE,
    "modes": ("lesion",),
    "classic": True,
    "factory": NormalizeCard,
}

#: Search entry (dict form of the provider contract, SPEC 2.10 amendment; read by StudioLib.palette)
PALETTE_ACTIONS = [
    {
        "id": "lnm.normalize",
        "title": "Normalize lesion to MNI…",
        "category": "Map",
        "synonyms": ["register", "mni", "template", "native space", "normalization", "warp"],
        "callback": _paletteNormalize,
        "enabled": _paletteEnabled,
        "description": "Register the scan to the MNI template and warp the lesion",
    },
]

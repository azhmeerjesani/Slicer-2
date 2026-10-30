"""Smart fill: click inside a lesion and it is filled by region growing with an automatic intensity tolerance.

The computation lives in SmartFillAlgorithm.py (numpy and VTK only). This file holds the effect: options,
interaction, the time-sliced compute driver, the preview display and Apply.

- numpy, vtk.util.numpy_support and SmartFillAlgorithm are imported lazily (prefetched when the effect is
  activated), because all effects are imported at application startup and numpy is not imported then.
- The compute runs on the GUI thread (VTK must not run in worker threads) in steps of at most ~40 ms, from
  single-shot QTimer callbacks, so that the application stays responsive. A new click, a tolerance change, a
  source or geometry change or deactivation cancels the running computation (the latest request wins).
- The preview is drawn by the effect in the slice views; it never enters the scene or any file. Apply writes
  it to the selected segment as one undo step through the standard modifySelectedSegmentByLabelmap (masking and
  overwrite settings apply). Parameters are segment editor node attributes "Smart fill.<Name>"; the user's
  defaults are stored in the fork-only settings group Studio/SmartFill/ (not used under --testing).
- Smart fill is assisted tracing: it never approves a result. Callables in applyObservers are notified after
  each Apply (for provenance records).

Resources/Icons/SmartFill.png is rendered from the Lucide "wand-sparkles" icon (lucide-static 0.468.0, ISC licence)
with a gold stroke (#E5A00D) over a 1 px #3A2A00 halo.
"""

import logging
import os
import time

import ctk
import qt
import vtk

import slicer
from slicer.i18n import tr as _

from SegmentEditorEffects import *

__all__ = ["SegmentEditorSmartFillEffect"]

ADD, REMOVE = 1, 2
AMBER = "#F5A524"
SETTINGS_GROUP = "Studio/SmartFill/"
# Parameter name, built-in default, type
PARAMETERS = (
    ("Mode", "3D", str),
    ("AutoTolerance", 1, bool),
    ("TolerancePercent", 10.0, float),
    ("MaxRadiusMm", 100.0, float),
    ("LeakGuardMm", 1.5, float),
    ("FillHoles", 1, bool),
    ("StayInsideBrain", 1, bool),
    ("ApplyOnClick", 0, bool),
)
TOLERANCE_PERCENT_RANGE = (1.0, 60.0)
TOLERANCE_STEP = 1.15  # factor of one wheel step or one +/- key press
DRAG_PIXELS_PER_DOUBLING = 120.0  # dragging this many logical pixels to the right doubles the tolerance
CLICK_MAX_MOVE_PIXELS = 4.0  # logical pixels; a Shift/Ctrl press-release that moved less is a click
CLICK_MAX_SECONDS = 0.6
WHEEL_MIN_INTERVAL_SECONDS = 0.040  # at most one tolerance step per 40 ms (trackpads send many wheel events)
STEP_BUDGET_SECONDS = 0.040  # compute time per timer callback
PROVISIONAL_FINALIZE_MS = 15.0  # a tolerance update shows its flood first if finalizing the region took longer than this
BUSY_DELAY_MS = 150  # show "Computing..." only for computations that take longer
MASK_POLL_INTERVAL_MS = 50

# Lazily imported modules: numpy, numpy_support, algorithm (SmartFillAlgorithm)
_modules = {}
# Brain masks read from files: (template grid, path, mtime) -> (array [k, j, i], affine)
_brainMaskFileCache = {}


def _lazyModules():
    """Import numpy and the algorithm on first use; the first call also warms up VTK and numpy (~100 ms once)."""
    if not _modules:
        import numpy
        from vtk.util import numpy_support

        from SegmentEditorEffects import SmartFillAlgorithm

        _modules.update(numpy=numpy, numpy_support=numpy_support, algorithm=SmartFillAlgorithm)
        # The first computation in a process pays a one-time initialization (measured 79 ms vs 9-12 ms later):
        # pay it now, so that the first click is not the slow one.
        try:
            shape = (24, 24, 24)
            kk, jj, ii = numpy.ogrid[0:24, 0:24, 0:24]
            ball = ((kk - 12) ** 2 + (jj - 12) ** 2 + (ii - 12) ** 2) <= 25
            image = numpy.where(ball, 160.0, 100.0).astype(numpy.float32)
            image += numpy.random.default_rng(0).normal(0.0, 4.0, shape).astype(numpy.float32)
            SmartFillAlgorithm.smartFill(image, (12, 12, 12), (1.0, 1.0, 1.0), 200.0, maxRadiusMm=20.0)
        except Exception:
            logging.exception("Smart fill warm-up failed")
    return _modules


def addApplyObserver(observer):
    """Call observer(info) after every Smart fill Apply (see SegmentEditorSmartFillEffect.applyObservers)."""
    if observer not in SegmentEditorSmartFillEffect.applyObservers:
        SegmentEditorSmartFillEffect.applyObservers.append(observer)


def removeApplyObserver(observer):
    if observer in SegmentEditorSmartFillEffect.applyObservers:
        SegmentEditorSmartFillEffect.applyObservers.remove(observer)


def _nativeControlKeyName():
    """'Ctrl' on Windows and Linux, the command key symbol on macOS (Qt maps Cmd to the Control modifier)."""
    text = qt.QKeySequence("Ctrl+A").toString(qt.QKeySequence.NativeText)
    text = text[:-1] if text.endswith("A") else text
    return text[:-1] if text.endswith("+") else text


def _timedSteps(generator, timing):
    """Generator: run the steps of generator, adding their compute time (not the time between them) to timing[0]."""
    while True:
        start = time.perf_counter()
        try:
            next(generator)
        except StopIteration as stop:
            timing[0] += 1000.0 * (time.perf_counter() - start)
            return stop.value
        timing[0] += 1000.0 * (time.perf_counter() - start)
        yield


def _devicePixelRatio(widget):
    try:
        return float(widget.devicePixelRatioF())
    except AttributeError:
        return float(widget.devicePixelRatio())


def _settingValue(value, valueType):
    """Convert a QSettings value (a string when read from an ini file) to the parameter type."""
    if valueType is bool:
        if isinstance(value, str):
            return 1 if value.strip().lower() in ("1", "true", "yes", "on") else 0
        return 1 if value else 0
    return valueType(value)


class SegmentEditorSmartFillEffect(AbstractScriptedSegmentEditorEffect):
    """Smart fill: click inside a lesion to fill it by region growing with an automatic tolerance, with a live preview.

    Public (duck-typed) API: hasPendingPreview(), isComputing(), applyPendingPreview(), discardPendingPreview(),
    previewAtIjk(ijk, operation, tolerance, synchronous), setTolerance(value, relative), lastResult,
    addApplyObserver(observer), removeApplyObserver(observer).
    """

    # Callables called with an info dict after each Apply: segmentationNode, segmentId, addedVoxels, removedVoxels,
    # voxelVolumeMm3, params (Mode, tolerance, auto, MaxRadiusMm, LeakGuardMm). Exceptions are logged.
    applyObservers = []
    addApplyObserver = staticmethod(addApplyObserver)
    removeApplyObserver = staticmethod(removeApplyObserver)

    def __init__(self, scriptedEffect):
        scriptedEffect.name = "Smart fill"  # no tr (don't translate it because modules find effects by name)
        scriptedEffect.title = _("Smart fill")
        AbstractScriptedSegmentEditorEffect.__init__(self, scriptedEffect)

        self.lastResult = {}
        self.parts = []  # [(sign, offsetKji, bool region)] in the numpy index space of the source volume
        self.lastPart = None  # grower, tolerance, auto, flags of the last part (tolerance edits recompute it)
        self.pipelines = {}  # slice widget -> PreviewPipeline
        self.previewImage = None
        self.previewExtent = None
        self.previewLabels = None
        self.previewCounts = (0, 0)
        self.task = None
        self.applyWhenTaskDone = False
        self.press = None
        self.lastWheelTime = 0.0
        self.lastClickView = None
        self.lastClickRas = None  # the HUD is anchored to the clicked point, so it follows pan, zoom and resize
        self.hudVisible = True
        self.clickSerial = 0
        self.message = ""
        self.messageIsWarning = False
        self.active = False
        self.brainMask = None  # dict(state="loading"|"ready", grid, array, ...) when a mask matches the grid
        self.maskThread = None

        self.taskTimer = qt.QTimer()
        self.taskTimer.setSingleShot(True)
        self.taskTimer.setInterval(0)
        self.taskTimer.connect("timeout()", self.onTaskTimer)
        self.dragTimer = qt.QTimer()
        self.dragTimer.setSingleShot(True)
        self.dragTimer.setInterval(0)
        self.dragTimer.connect("timeout()", self.onDragTimer)
        self.maskTimer = qt.QTimer()
        self.maskTimer.setInterval(MASK_POLL_INTERVAL_MS)
        self.maskTimer.connect("timeout()", self.onMaskTimer)

    def cleanup(self):
        self.active = False  # a pending prefetch must not use the effect once it is destroyed
        self.cancelTask()
        self.taskTimer.disconnect("timeout()", self.onTaskTimer)
        self.dragTimer.disconnect("timeout()", self.onDragTimer)
        self.maskTimer.stop()
        self.maskTimer.disconnect("timeout()", self.onMaskTimer)

    def clone(self):
        import qSlicerSegmentationsEditorEffectsPythonQt as effects

        clonedEffect = effects.qSlicerSegmentEditorScriptedEffect(None)
        clonedEffect.setPythonSource(__file__.replace("\\", "/"))
        return clonedEffect

    def icon(self):
        iconPath = os.path.join(os.path.dirname(__file__), "Resources/Icons/SmartFill.png")
        if os.path.exists(iconPath):
            return qt.QIcon(iconPath)
        return qt.QIcon()

    def helpText(self):
        ctrl = _nativeControlKeyName()
        return "<html>" + _("Click inside a lesion to fill it automatically") + _("""<br>
        The lesion is grown from the clicked point; the intensity tolerance is chosen from the contrast of its boundary.
        A preview is shown until it is applied.
        <p><ul style="margin: 0">
        <li><b>Left-click:</b> new preview (replaces the pending one). Dragging left or right changes the tolerance.
        <li><b>Shift+click:</b> add a region to the preview.
        <li><b>{ctrl}+click:</b> remove a region.
        <li><b>Shift+wheel</b> or <b>+ / -:</b> change the tolerance of the last region.
        <li><b>Enter:</b> apply the preview (one undo step). <b>Backspace:</b> discard it.
        </ul><p>
        Limits: needs visible contrast between the lesion and its surroundings. On the bare MNI template there is no lesion
        to find: use the patient's scan (native, or registered to the template) as the source volume. T1 lesions with
        grey-matter-like intensity: use 'This slice', a lower tolerance or {ctrl}+click.
        Smart fill is assisted tracing: review the result.</p>""").format(ctrl=ctrl)

    #
    # Options
    #

    def setupOptionsFrame(self):
        self.mode2DRadioButton = qt.QRadioButton(_("This slice"))
        self.mode2DRadioButton.objectName = "SmartFillMode2DRadioButton"
        self.mode2DRadioButton.setToolTip(_("Fill only in the slice that is clicked."))
        self.mode3DRadioButton = qt.QRadioButton(_("3D"))
        self.mode3DRadioButton.objectName = "SmartFillMode3DRadioButton"
        self.mode3DRadioButton.setToolTip(_("Fill the lesion in 3D."))
        self.modeButtonGroup = qt.QButtonGroup()
        self.modeButtonGroup.addButton(self.mode2DRadioButton)
        self.modeButtonGroup.addButton(self.mode3DRadioButton)
        modeLayout = qt.QHBoxLayout()
        modeLayout.addWidget(self.mode2DRadioButton)
        modeLayout.addWidget(self.mode3DRadioButton)
        modeLayout.addStretch(1)
        self.scriptedEffect.addLabeledOptionsWidget(_("Mode:"), modeLayout)

        self.toleranceSlider = ctk.ctkSliderWidget()
        self.toleranceSlider.objectName = "SmartFillToleranceSlider"
        self.toleranceSlider.minimum = TOLERANCE_PERCENT_RANGE[0]
        self.toleranceSlider.maximum = TOLERANCE_PERCENT_RANGE[1]
        self.toleranceSlider.singleStep = 0.5
        self.toleranceSlider.pageStep = 5.0
        self.toleranceSlider.decimals = 1
        self.toleranceSlider.suffix = " %"
        self.toleranceSlider.setToolTip(_("Intensity tolerance, in percent of the local intensity scale of the clicked point."
                                          " Changing it recomputes the last region of the preview."))
        self.autoToleranceCheckBox = qt.QCheckBox(_("Auto"))
        self.autoToleranceCheckBox.objectName = "SmartFillAutoToleranceCheckBox"
        self.autoToleranceCheckBox.setToolTip(_("Choose the tolerance of each click from the contrast of the lesion boundary."))
        self.toleranceLabel = qt.QLabel("")
        self.toleranceLabel.objectName = "SmartFillToleranceLabel"
        self.toleranceLabel.setToolTip(_("Tolerance of the last region, in intensity units"))
        metrics = self.toleranceLabel.fontMetrics()
        widestText = "±0000.0"  # stable layout: the label does not resize as the tolerance changes
        self.toleranceLabel.setMinimumWidth(metrics.horizontalAdvance(widestText) if hasattr(metrics, "horizontalAdvance") else metrics.width(widestText))
        toleranceLayout = qt.QHBoxLayout()
        toleranceLayout.addWidget(self.toleranceSlider, 1)
        toleranceLayout.addWidget(self.autoToleranceCheckBox)
        toleranceLayout.addWidget(self.toleranceLabel)
        self.scriptedEffect.addLabeledOptionsWidget(_("Tolerance:"), toleranceLayout)

        self.leakageGroupBox = ctk.ctkCollapsibleGroupBox()
        self.leakageGroupBox.objectName = "SmartFillLeakageGroupBox"
        self.leakageGroupBox.title = _("Leakage control")
        self.leakageGroupBox.collapsed = True
        leakageLayout = qt.QFormLayout(self.leakageGroupBox)

        self.maxRadiusSpinBox = slicer.qMRMLSpinBox()
        self.maxRadiusSpinBox.objectName = "SmartFillMaxRadiusSpinBox"
        self.maxRadiusSpinBox.setMRMLScene(slicer.mrmlScene)
        self.maxRadiusSpinBox.quantity = "length"
        self.maxRadiusSpinBox.unitAwareProperties = slicer.qMRMLSpinBox.Prefix | slicer.qMRMLSpinBox.Suffix
        self.maxRadiusSpinBox.decimals = 0
        self.maxRadiusSpinBox.minimum = 10.0
        self.maxRadiusSpinBox.maximum = 150.0
        self.maxRadiusSpinBox.singleStep = 5.0
        self.maxRadiusSpinBox.setToolTip(_("Largest distance from the clicked point that a region can reach. A first pass uses at most"
                                           " 50 mm and is extended automatically when the lesion continues."))
        self.maxRadiusNoteLabel = qt.QLabel("")
        maxRadiusLayout = qt.QHBoxLayout()
        maxRadiusLayout.addWidget(self.maxRadiusSpinBox)
        maxRadiusLayout.addWidget(self.maxRadiusNoteLabel, 1)
        leakageLayout.addRow(_("Max radius:"), maxRadiusLayout)

        self.leakGuardSpinBox = qt.QDoubleSpinBox()
        self.leakGuardSpinBox.objectName = "SmartFillLeakGuardSpinBox"
        self.leakGuardSpinBox.minimum = 0.0
        self.leakGuardSpinBox.maximum = 5.0
        self.leakGuardSpinBox.singleStep = 0.5
        self.leakGuardSpinBox.decimals = 1
        self.leakGuardSpinBox.suffix = " mm"
        self.leakGuardSpinBox.specialValueText = _("Off")
        self.leakGuardSpinBox.setToolTip(_("Cut leaks through bridges thinner than about twice this size."
                                           " The lesion boundary is preserved."))
        leakageLayout.addRow(_("Leak guard:"), self.leakGuardSpinBox)

        self.fillHolesCheckBox = qt.QCheckBox(_("Fill holes and smooth edges"))
        self.fillHolesCheckBox.objectName = "SmartFillFillHolesCheckBox"
        self.fillHolesCheckBox.setToolTip(_("Fill enclosed holes and close small gaps of the region."))
        leakageLayout.addRow(self.fillHolesCheckBox)

        self.stayInsideBrainCheckBox = qt.QCheckBox(_("Stay inside brain mask"))
        self.stayInsideBrainCheckBox.objectName = "SmartFillStayInsideBrainCheckBox"
        self.stayInsideBrainCheckBox.setToolTip(_("Available when the segmentation uses an MNI152 template grid whose brain mask is"
                                                  " installed (2 mm is bundled). For other scans use Masking \u203a Editable area."))
        leakageLayout.addRow(self.stayInsideBrainCheckBox)
        self.scriptedEffect.addOptionsWidget(self.leakageGroupBox)

        self.applyOnClickCheckBox = qt.QCheckBox(_("Apply on click"))
        self.applyOnClickCheckBox.objectName = "SmartFillApplyOnClickCheckBox"
        self.applyOnClickCheckBox.setToolTip(_("Apply each click immediately instead of showing a preview to apply with Enter."
                                               " Shift+click and {ctrl}+click still show a preview.").format(ctrl=_nativeControlKeyName()))
        self.scriptedEffect.addOptionsWidget(self.applyOnClickCheckBox)

        self.applyButton = qt.QPushButton(_("Apply"))
        self.applyButton.objectName = "SmartFillApplyButton"
        self.applyButton.setToolTip(_("Apply the preview to the selected segment (Enter)"))
        self.discardButton = qt.QPushButton(_("Discard"))
        self.discardButton.objectName = "SmartFillDiscardButton"
        self.discardButton.setToolTip(_("Discard the preview (Backspace)"))
        buttonLayout = qt.QHBoxLayout()
        buttonLayout.addWidget(self.applyButton)
        buttonLayout.addWidget(self.discardButton)
        self.scriptedEffect.addOptionsWidget(buttonLayout)

        self.statusLabel = qt.QLabel("")
        self.statusLabel.objectName = "SmartFillStatusLabel"
        self.statusLabel.wordWrap = True
        self.statusLabel.textFormat = qt.Qt.RichText
        self.statusLabel.setAlignment(qt.Qt.AlignTop)  # (left is the default horizontal alignment)
        # stable layout: room for three lines, so that status changes do not move the widgets below
        self.statusLabel.setMinimumHeight(3 * self.statusLabel.fontMetrics().lineSpacing())
        self.scriptedEffect.addOptionsWidget(self.statusLabel)

        self.mode2DRadioButton.connect("toggled(bool)", self.onUserEdit)
        self.mode3DRadioButton.connect("toggled(bool)", self.onUserEdit)
        self.autoToleranceCheckBox.connect("toggled(bool)", self.onUserEdit)
        self.maxRadiusSpinBox.connect("valueChanged(double)", self.onUserEdit)
        self.leakGuardSpinBox.connect("valueChanged(double)", self.onUserEdit)
        self.fillHolesCheckBox.connect("toggled(bool)", self.onUserEdit)
        self.stayInsideBrainCheckBox.connect("toggled(bool)", self.onUserEdit)
        self.applyOnClickCheckBox.connect("toggled(bool)", self.onUserEdit)
        self.toleranceSlider.connect("valueChanged(double)", self.onToleranceSliderChanged)
        self.applyButton.connect("clicked()", self.applyPendingPreview)
        self.discardButton.connect("clicked()", self.discardPendingPreview)

    def setMRMLDefaults(self):
        settings = qt.QSettings()
        useSettings = not slicer.app.testingEnabled()
        for name, default, valueType in PARAMETERS:
            value = default
            if useSettings and settings.contains(SETTINGS_GROUP + name):
                try:
                    value = _settingValue(settings.value(SETTINGS_GROUP + name), valueType)
                except (TypeError, ValueError):
                    value = default
            if valueType is str:
                value = "2D" if str(value).upper() == "2D" else "3D"
            self.scriptedEffect.setParameterDefault(name, value)

    def parameterValue(self, name):
        for parameterName, default, valueType in PARAMETERS:
            if parameterName == name:
                break
        else:
            raise KeyError(name)
        if not self.scriptedEffect.parameterDefined(name):
            return default
        if valueType is str:
            return self.scriptedEffect.parameter(name)
        if valueType is bool:
            return self.scriptedEffect.integerParameter(name) != 0
        return self.scriptedEffect.doubleParameter(name)

    def updateGUIFromMRML(self):
        widgets = (self.mode2DRadioButton, self.mode3DRadioButton, self.autoToleranceCheckBox, self.maxRadiusSpinBox,
                   self.leakGuardSpinBox, self.fillHolesCheckBox, self.stayInsideBrainCheckBox, self.applyOnClickCheckBox,
                   self.toleranceSlider)
        wasBlocked = [widget.blockSignals(True) for widget in widgets]
        mode2D = self.parameterValue("Mode") == "2D"
        self.mode2DRadioButton.setChecked(mode2D)
        self.mode3DRadioButton.setChecked(not mode2D)
        self.autoToleranceCheckBox.setChecked(self.parameterValue("AutoTolerance"))
        self.maxRadiusSpinBox.value = self.parameterValue("MaxRadiusMm")
        self.leakGuardSpinBox.value = self.parameterValue("LeakGuardMm")
        self.fillHolesCheckBox.setChecked(self.parameterValue("FillHoles"))
        self.stayInsideBrainCheckBox.setChecked(self.parameterValue("StayInsideBrain"))
        self.applyOnClickCheckBox.setChecked(self.parameterValue("ApplyOnClick"))
        if self.lastPart is not None and self.parts:
            self.toleranceSlider.value = self.lastPart["tolerancePercent"]
        else:
            self.toleranceSlider.value = self.parameterValue("TolerancePercent")
        for widget, blocked in zip(widgets, wasBlocked, strict=True):
            widget.blockSignals(blocked)
        self.stayInsideBrainCheckBox.enabled = self.brainMask is not None
        self.updateMaxRadiusNote()
        self.updateStatus()

    def updateMRMLFromGUI(self):
        values = {
            "Mode": "2D" if self.mode2DRadioButton.checked else "3D",
            "AutoTolerance": 1 if self.autoToleranceCheckBox.checked else 0,
            "MaxRadiusMm": float(self.maxRadiusSpinBox.value),
            "LeakGuardMm": float(self.leakGuardSpinBox.value),
            "FillHoles": 1 if self.fillHolesCheckBox.checked else 0,
            "StayInsideBrain": 1 if self.stayInsideBrainCheckBox.checked else 0,
            "ApplyOnClick": 1 if self.applyOnClickCheckBox.checked else 0,
        }
        self.setParameters(values)

    def setParameters(self, values):
        """Set parameters and, outside of testing, store the user's choice in the fork-only settings group."""
        settings = None if slicer.app.testingEnabled() else qt.QSettings()
        for name, value in values.items():
            current = self.parameterValue(name)
            if isinstance(value, float) and isinstance(current, float) and abs(current - value) < 1e-9:
                continue
            if not isinstance(value, float) and current == value:
                continue
            self.scriptedEffect.setParameter(name, value)
            if settings is not None:
                settings.setValue(SETTINGS_GROUP + name, bool(value) if name in ("AutoTolerance", "FillHoles", "StayInsideBrain", "ApplyOnClick") else value)

    def onUserEdit(self, *args):
        self.updateMRMLFromGUI()

    def onToleranceSliderChanged(self, value):
        self.setParameters({"TolerancePercent": float(value)})
        if self.parts:
            self.setTolerance(float(value))

    def updateMaxRadiusNote(self):
        referenceImage = self.scriptedEffect.referenceGeometryImage()
        note = ""
        if referenceImage is not None:
            spacing = referenceImage.GetSpacing()
            radius = self.parameterValue("MaxRadiusMm")
            count = 1
            for s in spacing:
                count *= 2 * int(-(-radius // max(s, 1e-6))) + 1
            maxRoiVoxels = 4_000_000
            if count > maxRoiVoxels:
                effective = radius * (maxRoiVoxels / count) ** (1.0 / 3.0)
                note = _("effective {radius} mm at this resolution").format(radius=int(effective))
        self.maxRadiusNoteLabel.text = note

    #
    # Lifecycle
    #

    def activate(self):
        self.active = True
        self.message = ""
        qt.QTimer.singleShot(0, self.prefetch)
        self.updateStatus()

    def prefetch(self):
        if not self.active:
            return
        _lazyModules()
        self.refreshBrainMask()

    def deactivate(self):
        self.active = False
        self.cancelTask()
        self.applyWhenTaskDone = False
        self.press = None
        self.dragTimer.stop()
        self.maskTimer.stop()
        self.clearParts()
        self.removePipelines()

    def sourceVolumeNodeChanged(self):
        self.cancelTask()
        self.discardPendingPreview()

    def referenceGeometryChanged(self):
        self.cancelTask()
        self.discardPendingPreview()
        if self.active and _modules:
            self.refreshBrainMask()

    def layoutChanged(self):
        self.removePipelines()
        if self.parts:
            self.updatePreviewDisplay()

    #
    # Public API
    #

    def hasPendingPreview(self):
        return bool(self.parts)

    def isComputing(self):
        return self.task is not None

    def previewAtIjk(self, ijk, operation="new", tolerance=None, synchronous=True, sliceAxis=None):
        """Compute a preview for a click at a voxel of the source volume (absolute IJK of the aligned source volume).

        :param operation: 'new' (replace the preview), 'extend' (add a region) or 'remove' (remove a region).
        :param tolerance: tolerance in percent of the click's intensity scale; None uses the parameters
          (automatic tolerance, or TolerancePercent when AutoTolerance is off).
        :param synchronous: run the computation to completion before returning (the default for scripts and tests);
          otherwise it runs time-sliced from the event loop (see isComputing()).
        :param sliceAxis: numpy axis held constant in 'This slice' mode (2 - IJK axis); default: the axis of the
          Red slice view, or the K axis.
        :return: lastResult (a copy).
        """
        if self.parameterValue("Mode") == "2D" and sliceAxis is None:
            sliceAxis = self.sliceAxisOfRedView()
            if sliceAxis is None:
                sliceAxis = 0
        self.startClick(list(ijk), operation, tolerance, sliceAxis, synchronous=synchronous)
        return dict(self.lastResult)

    def setTolerance(self, value, relative=False):
        """Change the tolerance of the last region of the preview and recompute it.

        :param value: tolerance in percent of the click's intensity scale, or a factor when relative is True.
        Without a pending preview, it changes the TolerancePercent parameter when Auto is off; when Auto is on,
        a relative change only shows a hint (the automatic tolerance would ignore it).
        """
        low, high = TOLERANCE_PERCENT_RANGE
        if not self.parts or self.lastPart is None:
            if relative and self.parameterValue("AutoTolerance"):
                self.setMessage(_("The tolerance is automatic: click inside a lesion first, then adjust it"))
                return
            percent = self.parameterValue("TolerancePercent") * value if relative else value
            self.setParameters({"TolerancePercent": float(min(max(percent, low), high))})
            return
        scale = self.lastPart["scale"]
        currentPercent = self.lastPart["tolerancePercent"]
        if self.task is not None and self.task.get("tolerance") is not None and scale > 0:
            currentPercent = 100.0 * self.task["tolerance"] / scale  # repeated steps add up while an update runs
        percent = currentPercent * value if relative else value
        percent = min(max(percent, low), high)
        self.startRetolerance(percent * scale / 100.0)

    def applyPendingPreview(self):
        """Apply the pending preview to the selected segment as one undo step. Returns True if something was applied."""
        self.applyWhenTaskDone = False
        if self.task is not None:
            # finish a first pass or a tolerance update (bounded); do not wait for an automatic radius extension,
            # apply the completed preview instead (never a partial region)
            self.finishTaskSynchronously(stopAtExtension=True)
            self.cancelTask()
        if not self.parts:
            return False
        if not self.scriptedEffect.confirmCurrentSegmentVisible():
            return False
        startTime = time.perf_counter()
        modules = _lazyModules()
        numpy, numpy_support = modules["numpy"], modules["numpy_support"]
        extent, labels = self.previewExtent, self.previewLabels
        before = self.segmentMaskInExtent(extent)
        referenceImage = self.scriptedEffect.referenceGeometryImage()
        self.scriptedEffect.saveStateForUndo()
        for sign, mode in ((ADD, slicer.qSlicerSegmentEditorAbstractEffect.ModificationModeAdd),
                           (REMOVE, slicer.qSlicerSegmentEditorAbstractEffect.ModificationModeRemove)):
            mask = labels == sign
            if not mask.any():
                continue
            modifier = slicer.vtkOrientedImageData()
            modifier.SetExtent(extent)
            modifier.SetSpacing(referenceImage.GetSpacing())
            modifier.SetOrigin(referenceImage.GetOrigin())
            modifier.CopyDirections(referenceImage)
            scalars = numpy.ascontiguousarray(mask.astype(numpy.uint8)).ravel()
            modifier.GetPointData().SetScalars(numpy_support.numpy_to_vtk(scalars, deep=True, array_type=vtk.VTK_UNSIGNED_CHAR))
            self.scriptedEffect.modifySelectedSegmentByLabelmap(modifier, mode, list(extent))
        after = self.segmentMaskInExtent(extent)
        addedVoxels = int(numpy.count_nonzero(after & ~before))
        removedVoxels = int(numpy.count_nonzero(before & ~after))
        voxelVolumeMm3 = self.voxelVolumeMm3()
        lastPart = self.lastPart or {}
        info = {
            "segmentationNode": self.scriptedEffect.parameterSetNode().GetSegmentationNode(),
            "segmentId": self.scriptedEffect.parameterSetNode().GetSelectedSegmentID(),
            "addedVoxels": addedVoxels,
            "removedVoxels": removedVoxels,
            "voxelVolumeMm3": voxelVolumeMm3,
            "params": {
                "Mode": lastPart.get("mode", self.parameterValue("Mode")),
                "tolerance": lastPart.get("tolerance"),
                "auto": lastPart.get("auto"),
                "MaxRadiusMm": self.parameterValue("MaxRadiusMm"),
                "LeakGuardMm": self.parameterValue("LeakGuardMm"),
            },
        }
        self.clearParts()
        self.lastResult["applyMs"] = 1000.0 * (time.perf_counter() - startTime)
        self.setMessage(_("Applied {volume}").format(volume=self.formatVolume(addedVoxels * voxelVolumeMm3, "+")))
        for observer in list(self.applyObservers):
            try:
                observer(info)
            except Exception:
                logging.exception("Smart fill apply observer failed")
        return True

    def discardPendingPreview(self):
        self.cancelTask()
        self.applyWhenTaskDone = False
        self.clearParts()
        self.updateStatus()

    #
    # Interaction
    #

    def processInteractionEvents(self, callerInteractor, eventId, viewWidget):
        if viewWidget.className() != "qMRMLSliceWidget":
            return False
        shift = callerInteractor.GetShiftKey()
        control = callerInteractor.GetControlKey()
        alt = callerInteractor.GetAltKey()
        xy = callerInteractor.GetEventPosition()

        if eventId in (vtk.vtkCommand.LeftButtonPressEvent, vtk.vtkCommand.LeftButtonDoubleClickEvent):
            if alt or (shift and control):
                self.press = None
                return False
            if shift or control:
                # Shift+drag pans and Ctrl+drag blends: act on the release, if the mouse did not move
                self.press = {"xy": tuple(xy), "time": time.perf_counter(), "operation": "extend" if shift else "remove",
                              "view": viewWidget}
                return False
            self.press = {"xy": tuple(xy), "time": time.perf_counter(), "operation": "new", "view": viewWidget,
                          "dragging": False, "dx": 0.0, "baseTolerance": None, "serial": self.clickSerial + 1}
            self.clickInView(viewWidget, xy, "new")
            return True

        if eventId == vtk.vtkCommand.MouseMoveEvent:
            if self.press is not None and self.press["operation"] == "new" and self.press["view"] is viewWidget:
                dx = (xy[0] - self.press["xy"][0]) / _devicePixelRatio(viewWidget)
                if not self.press["dragging"] and abs(dx) < CLICK_MAX_MOVE_PIXELS:
                    return True
                self.press["dragging"] = True
                self.press["dx"] = dx
                self.dragTimer.start()
                return True
            return False

        if eventId == vtk.vtkCommand.LeftButtonReleaseEvent:
            press, self.press = self.press, None
            if press is None:
                return False
            if press["operation"] == "new":
                if self.parameterValue("ApplyOnClick"):
                    if self.task is not None:
                        self.applyWhenTaskDone = True
                    elif self.parts:
                        self.applyPendingPreview()
                return True
            dpr = _devicePixelRatio(viewWidget)
            moved = max(abs(xy[0] - press["xy"][0]), abs(xy[1] - press["xy"][1])) / dpr
            if press["view"] is viewWidget and moved < CLICK_MAX_MOVE_PIXELS and time.perf_counter() - press["time"] < CLICK_MAX_SECONDS:
                self.clickInView(viewWidget, press["xy"], press["operation"])
            return False

        if eventId in (vtk.vtkCommand.MouseWheelForwardEvent, vtk.vtkCommand.MouseWheelBackwardEvent,
                       vtk.vtkCommand.MouseWheelLeftEvent, vtk.vtkCommand.MouseWheelRightEvent):
            if not shift or control or alt:
                return False
            now = time.perf_counter()
            if now - self.lastWheelTime >= WHEEL_MIN_INTERVAL_SECONDS:
                self.lastWheelTime = now
                increase = eventId in (vtk.vtkCommand.MouseWheelForwardEvent, vtk.vtkCommand.MouseWheelLeftEvent)
                self.setTolerance(TOLERANCE_STEP if increase else 1.0 / TOLERANCE_STEP, relative=True)
            return True

        if eventId == vtk.vtkCommand.KeyPressEvent:
            if control or alt:
                return False
            key = callerInteractor.GetKeySym()
            if key in ("plus", "equal", "KP_Add"):
                self.setTolerance(TOLERANCE_STEP, relative=True)
                return True
            if key in ("minus", "underscore", "KP_Subtract"):
                self.setTolerance(1.0 / TOLERANCE_STEP, relative=True)
                return True
            if key in ("Return", "KP_Enter"):
                if self.parts or self.task is not None:
                    self.applyPendingPreview()
                    return True
                return False
            if key == "BackSpace":
                if self.parts or self.task is not None:
                    self.discardPendingPreview()
                    return True
                return False
            return False

        if eventId == vtk.vtkCommand.EnterEvent:
            self.hudVisible = True
            self.updateHud()
        elif eventId == vtk.vtkCommand.LeaveEvent:
            self.hudVisible = False
            self.updateHud()
        return False

    def processViewNodeEvents(self, callerViewNode, eventId, viewWidget):
        pipeline = self.pipelines.get(viewWidget)
        if pipeline is not None and callerViewNode and callerViewNode.IsA("vtkMRMLSliceNode"):
            pipeline.updateGeometry()
            self.updateHud()
            viewWidget.sliceView().scheduleRender()

    def onDragTimer(self):
        press = self.press
        if press is None or not press.get("dragging"):
            return
        if self.lastPart is None or self.lastPart.get("serial") != press["serial"]:
            return  # the first pass of this click is still running: setLastPart restarts this timer
        if press["baseTolerance"] is None:
            press["baseTolerance"] = self.lastPart["tolerance"]
        tolerance = press["baseTolerance"] * 2.0 ** (press["dx"] / DRAG_PIXELS_PER_DOUBLING)
        low, high = TOLERANCE_PERCENT_RANGE
        scale = self.lastPart["scale"]
        tolerance = min(max(tolerance, low * scale / 100.0), high * scale / 100.0)
        if abs(tolerance - self.lastPart["tolerance"]) > 1e-9 * max(1.0, scale):
            self.startRetolerance(tolerance)

    def clickInView(self, viewWidget, xy, operation):
        sourceImage = self.scriptedEffect.sourceVolumeImageData()
        segmentationNode = self.scriptedEffect.parameterSetNode().GetSegmentationNode()
        if sourceImage is None or segmentationNode is None:
            self.setMessage(_("Select a source volume and a segment"), warning=True)
            return
        parentTransformNode = segmentationNode.GetParentTransformNode()
        ijk = self.xyToIjk(xy, viewWidget, sourceImage, parentTransformNode)
        sliceAxis = None
        self.obliqueNote = False
        if self.parameterValue("Mode") == "2D":
            sliceAxis = self.sliceAxisOfView(viewWidget, sourceImage, parentTransformNode)
            if sliceAxis is None:
                self.obliqueNote = True
        self.lastClickView = viewWidget
        self.lastClickRas = self.xyToRas(xy, viewWidget)
        self.hudVisible = True
        self.startClick(ijk, operation, None, sliceAxis, synchronous=False)

    def sliceAxisOfView(self, viewWidget, sourceImage, parentTransformNode):
        """Numpy axis held constant in the view (2 - IJK axis), or None if the view is oblique."""
        sliceNode = self.scriptedEffect.viewNode(viewWidget)
        offset = max(sliceNode.GetDimensions())
        i0, j0, k0 = self.xyToIjk((0, 0), viewWidget, sourceImage, parentTransformNode)
        i1, j1, k1 = self.xyToIjk((offset, offset), viewWidget, sourceImage, parentTransformNode)
        if i0 == i1:
            return 2
        if j0 == j1:
            return 1
        if k0 == k1:
            return 0
        return None

    def sliceAxisOfRedView(self):
        layoutManager = slicer.app.layoutManager()
        sourceImage = self.scriptedEffect.sourceVolumeImageData()
        segmentationNode = self.scriptedEffect.parameterSetNode().GetSegmentationNode()
        if layoutManager is None or sourceImage is None or segmentationNode is None:
            return None
        sliceWidget = layoutManager.sliceWidget("Red")
        if sliceWidget is None:
            return None
        return self.sliceAxisOfView(sliceWidget, sourceImage, segmentationNode.GetParentTransformNode())

    #
    # Computation
    #

    def sourceContext(self, waitForBrainMask=True):
        """Source array view, spacing, display window, intensity range and allowed mask (or a message).

        :param waitForBrainMask: wait for a brain mask that is still being read (scripts); otherwise (clicks) return a
          message instead of blocking the GUI thread.
        """
        modules = _lazyModules()
        numpy, numpy_support = modules["numpy"], modules["numpy_support"]
        sourceImage = self.scriptedEffect.sourceVolumeImageData()
        if sourceImage is None or sourceImage.GetPointData().GetScalars() is None:
            return None, _("Select a source volume")
        extent = sourceImage.GetExtent()
        dims = (extent[5] - extent[4] + 1, extent[3] - extent[2] + 1, extent[1] - extent[0] + 1)
        array = numpy_support.vtk_to_numpy(sourceImage.GetPointData().GetScalars())
        if array.ndim > 1:
            array = array[:, 0]
        array = array.reshape(dims)
        parameterSetNode = self.scriptedEffect.parameterSetNode()
        window = 0.0
        sourceVolumeNode = parameterSetNode.GetSourceVolumeNode()
        displayNode = sourceVolumeNode.GetDisplayNode() if sourceVolumeNode else None
        if displayNode is not None and hasattr(displayNode, "GetWindow"):
            window = float(displayNode.GetWindow())
        intensityRange = None
        if parameterSetNode.GetSourceVolumeIntensityMask():
            intensityRange = tuple(parameterSetNode.GetSourceVolumeIntensityMaskRange())
        allowed = None
        if self.parameterValue("StayInsideBrain") and self.brainMask is not None:
            if self.brainMask["state"] == "loading" and not waitForBrainMask:
                return None, _("The brain mask is still loading: click again in a moment")
            self.waitForBrainMask()
            if self.brainMask is not None and self.brainMask.get("array") is not None and self.brainMask["array"].shape == dims:
                allowed = self.brainMask["array"]
        return {"array": array, "extent": extent, "spacing": sourceImage.GetSpacing(), "window": window,
                "intensityRange": intensityRange, "allowed": allowed}, None

    def startClick(self, ijk, operation, tolerancePercent, sliceAxis, synchronous):
        """Start (or run synchronously) the computation of one click."""
        context, error = self.sourceContext(waitForBrainMask=synchronous)
        seed = None
        if context is not None:
            extent = context["extent"]
            seed = (int(ijk[2]) - extent[4], int(ijk[1]) - extent[2], int(ijk[0]) - extent[0])
            if not all(0 <= s < n for s, n in zip(seed, context["array"].shape, strict=True)):
                error = _("Click inside the image")
            elif context["allowed"] is not None and not context["allowed"][seed]:
                error = _("Outside the brain mask")
        if error or operation != "new":
            # the preview is kept (or extended): a tolerance update that shows its unfinished region must end first
            self.settleProvisionalTask()
        self.cancelTask()
        if error:
            self.setMessage(error, warning=True)
            return
        self.clickSerial += 1
        generator = self.iterClick(context, seed, operation, tolerancePercent, sliceAxis, self.clickSerial)
        self.runTask(generator, "click", synchronous)

    def startRetolerance(self, tolerance):
        if self.lastPart is None or not self.parts:
            return
        self.cancelTask()
        self.runTask(self.iterRetolerance(tolerance), "tolerance", synchronous=False)

    def iterClick(self, context, seed, operation, tolerancePercent, sliceAxis, serial):
        algorithm = _lazyModules()["algorithm"]
        startTime = time.perf_counter()
        sign = REMOVE if operation == "remove" else ADD
        maxRadiusMm = self.parameterValue("MaxRadiusMm")
        auto = tolerancePercent is None and self.parameterValue("AutoTolerance")
        mode = "2D" if sliceAxis is not None else "3D"
        firstRadiusMm = min(algorithm.FIRST_PASS_RADIUS_MM, maxRadiusMm) if auto else maxRadiusMm
        arguments = (context["array"], seed, context["spacing"], context["window"])
        options = {"sliceAxis": sliceAxis, "allowed": context["allowed"], "intensityRange": context["intensityRange"]}
        self.task["phase"] = "click"

        grower = yield from algorithm.RegionGrower.iterCreate(*arguments, maxRadiusMm=firstRadiusMm, **options)
        if grower.outsideAllowed:
            self.setMessage(_("The clicked voxel is outside the editable intensity range"), warning=True)
            return
        if auto:
            tolerance, region, info = yield from grower.iterAutoTolerance()
        else:
            if tolerancePercent is None:
                tolerancePercent = self.parameterValue("TolerancePercent")
            tolerance = tolerancePercent * grower.scale / 100.0
            region, _count = grower.region(tolerance)
            yield
            info = {"limitReached": grower.limitReached(region), "noEdge": False, "mayContinue": False}
        finalizeTiming = [0.0]
        region = yield from _timedSteps(
            algorithm.iterFinalize(grower, region, self.parameterValue("LeakGuardMm"), self.parameterValue("FillHoles")), finalizeTiming)
        finalizeMs = finalizeTiming[0]
        extending = bool(auto and info.get("mayContinue") and firstRadiusMm < maxRadiusMm)
        limitReached = bool(info.get("limitReached") or (info.get("mayContinue") and not extending))
        part = {"serial": serial, "sign": sign, "operation": operation, "grower": grower, "region": region,
                "tolerance": tolerance, "auto": auto, "limitReached": limitReached, "noEdge": bool(info.get("noEdge")),
                "escalated": False, "extending": extending, "mode": mode, "finalizeMs": finalizeMs}
        pass1Ms = 1000.0 * (time.perf_counter() - startTime)
        yield  # the display update is a step of its own
        self.setLastPart(part, replaceLast=False, timings={"pass1": pass1Ms, "pass2": None, "finalize": finalizeMs})
        logging.debug(f"Smart fill click {operation} at {seed}: tolerance {tolerance:.4g} ({'auto' if auto else 'manual'}),"
                      f" {int(region.sum())} voxels, {pass1Ms:.0f} ms, extending: {extending}")
        if not extending:
            return
        # the first pass is shown (amber outline, "extending..."); Apply now applies it without waiting
        self.task["phase"] = "extend"
        self.updateStatus()
        yield
        pass2Start = time.perf_counter()
        grower2 = yield from algorithm.RegionGrower.iterCreate(*arguments, maxRadiusMm=maxRadiusMm, **options)
        tolerance2, region2, info2 = yield from grower2.iterAutoTolerance(minFraction=info.get("fraction"))
        finalizeTiming = [0.0]
        region2 = yield from _timedSteps(
            algorithm.iterFinalize(grower2, region2, self.parameterValue("LeakGuardMm"), self.parameterValue("FillHoles")), finalizeTiming)
        finalizeMs = finalizeTiming[0]
        part2 = dict(part, grower=grower2, region=region2, tolerance=tolerance2, escalated=True, extending=False, finalizeMs=finalizeMs,
                     limitReached=bool(info2.get("limitReached") or info2.get("mayContinue")), noEdge=bool(info2.get("noEdge")))
        yield  # the display update is a step of its own
        self.setLastPart(part2, replaceLast=True, timings={"pass2": 1000.0 * (time.perf_counter() - pass2Start), "finalize": finalizeMs})
        logging.debug(f"Smart fill extended to {grower2.effectiveRadiusMm:.0f} mm: tolerance {tolerance2:.4g}, {int(region2.sum())} voxels")

    def iterRetolerance(self, tolerance):
        """Generator: recompute the last part at a new tolerance, progressively.

        When finalizing the region (leak guard, closing and hole filling) took more than PROVISIONAL_FINALIZE_MS
        last time, the flood at the new tolerance is shown at once (provisional) and rendered before the region is
        finalized and shown again. A newer tolerance request cancels the finalization; anything else that keeps the
        preview (Apply, an extend or remove click) first finishes it, so that a provisional region is never applied
        or kept.
        """
        algorithm = _lazyModules()["algorithm"]
        self.task["phase"] = "tolerance"
        self.task["tolerance"] = tolerance
        startTime = time.perf_counter()
        part = self.lastPart
        grower = part["grower"]
        region, _count = grower.region(tolerance)
        limitReached = grower.limitReached(region)
        newPart = dict(part, region=region, tolerance=tolerance, auto=False, limitReached=limitReached, noEdge=False,
                       extending=False, provisional=False)
        leakGuardMm, fillHoles = self.parameterValue("LeakGuardMm"), self.parameterValue("FillHoles")
        yield
        if leakGuardMm > 0 or fillHoles:
            if part.get("finalizeMs", 0.0) > PROVISIONAL_FINALIZE_MS:
                self.task["provisional"] = True
                self.setLastPart(dict(newPart, provisional=True), replaceLast=True,
                                 timings={"toleranceFirst": 1000.0 * (time.perf_counter() - startTime)})
                self.task["pause"] = True  # back to the event loop, so that the provisional region is rendered now
                yield
            finalizeTiming = [0.0]
            newPart["region"] = yield from _timedSteps(algorithm.iterFinalize(grower, region, leakGuardMm, fillHoles), finalizeTiming)
            newPart["finalizeMs"] = finalizeTiming[0]
            yield  # the display update is a step of its own
        timings = {"tolerance": 1000.0 * (time.perf_counter() - startTime)}
        if not self.task.get("provisional"):
            timings["toleranceFirst"] = timings["tolerance"]
        self.task["provisional"] = False
        self.setLastPart(newPart, replaceLast=True, timings=timings)

    def setLastPart(self, part, replaceLast, timings):
        """Store a computed region as the last part of the preview and update the display."""
        displayStart = time.perf_counter()
        part["scale"] = part["grower"].scale
        part["tolerancePercent"] = 100.0 * part["tolerance"] / part["scale"] if part["scale"] > 0 else 0.0
        offset, region = tuple(int(x) for x in part["grower"].offsetKji), part["region"]
        if region.any():
            slices = _lazyModules()["algorithm"].boundingBoxSlices(region, pad=0)
            offset = tuple(o + s.start for o, s in zip(offset, slices, strict=True))
            region = region[slices]
        entry = (part["sign"], offset, region)
        if replaceLast and self.parts:
            self.parts[-1] = entry
        elif part["operation"] == "new":
            self.parts = [entry]
        else:
            self.parts.append(entry)
        self.lastPart = part
        self.updatePreviewDisplay()
        voxels = int(self.previewCounts[0])
        voxelVolumeMm3 = self.voxelVolumeMm3()
        timingsMs = dict(self.lastResult.get("timingsMs", {})) if replaceLast else {}
        timingsMs.update(timings)
        timingsMs["display"] = 1000.0 * (time.perf_counter() - displayStart)
        timingsMs.setdefault("maxBlock", 0.0)
        self.lastResult = {
            "voxels": voxels,
            "removeVoxels": int(self.previewCounts[1]),
            "volumeMl": voxels * voxelVolumeMm3 / 1000.0,
            "tolerance": part["tolerance"],
            "tolerancePercent": part["tolerancePercent"],
            "scale": part["scale"],
            "auto": part["auto"],
            "limitReached": part["limitReached"],
            "noEdge": part["noEdge"],
            "escalated": part["escalated"],
            "extending": part["extending"],
            "mode": part["mode"],
            "provisional": bool(part.get("provisional")),
            "timingsMs": timingsMs,
        }
        self.message = ""
        self.updateGUIFromMRML()
        if self.press is not None and self.press.get("dragging") and self.press.get("serial") == part.get("serial"):
            self.dragTimer.start()

    #
    # Time-sliced task driver
    #

    def runTask(self, generator, name, synchronous):
        self.task = {"generator": generator, "name": name, "phase": name, "maxBlockMs": 0.0, "maxStep": 0.0, "busy": False}
        if synchronous:
            self.finishTaskSynchronously()
            return
        # The first steps run right away (within the time budget), the rest from the event loop. "Computing..." is
        # shown only if the task is still running after BUSY_DELAY_MS (short tasks do not make the status flicker).
        task = self.task
        qt.QTimer.singleShot(BUSY_DELAY_MS, lambda: self.showBusy(task))
        self.onTaskTimer()

    def showBusy(self, task):
        if self.task is task:
            task["busy"] = True
            self.updateStatus()

    def settleProvisionalTask(self):
        """Finish a tolerance update whose unfinished region is shown, so that it is never kept in the preview."""
        if self.task is not None and self.task.get("provisional"):
            self.finishTaskSynchronously()

    def cancelTask(self):
        if self.task is not None:
            self.task["generator"].close()
            self.task = None
            self.taskTimer.stop()

    def finishTaskSynchronously(self, stopAtExtension=False):
        task = self.task
        if task is None:
            return
        self.taskTimer.stop()
        startTime = time.perf_counter()
        self.stepTask(task, budgetSeconds=None, stopAtExtension=stopAtExtension)
        task["maxBlockMs"] = max(task["maxBlockMs"], 1000.0 * (time.perf_counter() - startTime))

    def onTaskTimer(self):
        task = self.task
        if task is None:
            return
        startTime = time.perf_counter()
        finished = self.stepTask(task, budgetSeconds=STEP_BUDGET_SECONDS)
        blockMs = 1000.0 * (time.perf_counter() - startTime)
        task["maxBlockMs"] = max(task["maxBlockMs"], blockMs)
        if "timingsMs" in self.lastResult:
            self.lastResult["timingsMs"]["maxBlock"] = max(self.lastResult["timingsMs"].get("maxBlock", 0.0), task["maxBlockMs"])
        if not finished and self.task is task:
            self.taskTimer.start()

    def stepTask(self, task, budgetSeconds, stopAtExtension=False):
        """Run steps of the task until it ends or the time budget is used (None: no budget). Returns True when done."""
        startTime = time.perf_counter()
        try:
            while True:
                if stopAtExtension and task["phase"] == "extend":
                    return False
                stepStart = time.perf_counter()
                next(task["generator"])
                now = time.perf_counter()
                task["maxStep"] = max(task["maxStep"], now - stepStart)
                if self.task is not task:
                    return True  # cancelled from inside the step
                if task.pop("pause", False) and budgetSeconds is not None:
                    return False  # the step asked to return to the event loop (to render)
                # run another step only if a step as long as the longest one so far still fits in the budget
                if budgetSeconds is not None and (now - startTime) + task["maxStep"] > budgetSeconds:
                    return False
        except StopIteration:
            pass
        except Exception:
            logging.exception("Smart fill computation failed")
            self.setMessage(_("Smart fill failed: see the application log"), warning=True)
        if self.task is task:
            self.task = None
            if "timingsMs" in self.lastResult:
                self.lastResult["timingsMs"]["maxBlock"] = max(self.lastResult["timingsMs"].get("maxBlock", 0.0), task["maxBlockMs"])
            self.updateStatus()
            if self.applyWhenTaskDone:
                self.applyWhenTaskDone = False
                if self.parts:
                    self.applyPendingPreview()
        return True

    #
    # Brain mask
    #

    def referenceGridAffine(self):
        """(dims IJK, 4x4 numpy affine of the voxel (0, 0, 0) of the extent) of the reference geometry, or None."""
        numpy = _lazyModules()["numpy"]
        referenceImage = self.scriptedEffect.referenceGeometryImage()
        if referenceImage is None:
            return None
        extent = referenceImage.GetExtent()
        if extent[0] > extent[1] or extent[2] > extent[3] or extent[4] > extent[5]:
            return None
        matrix = vtk.vtkMatrix4x4()
        referenceImage.GetImageToWorldMatrix(matrix)
        affine = numpy.array([[matrix.GetElement(r, c) for c in range(4)] for r in range(4)])
        shift = numpy.eye(4)
        shift[:3, 3] = (extent[0], extent[2], extent[4])
        return (extent[1] - extent[0] + 1, extent[3] - extent[2] + 1, extent[5] - extent[4] + 1), affine @ shift

    def refreshBrainMask(self):
        """Find a brain mask for the template grid of the segmentation (a file or a scene volume); read it in a thread."""
        self.brainMask = None
        self.maskTimer.stop()
        modules = _lazyModules()
        numpy, algorithm = modules["numpy"], modules["algorithm"]
        grid = self.referenceGridAffine()
        templateGrid = algorithm.templateGridOf(grid[0], grid[1]) if grid else None
        if templateGrid is not None:
            dims, affine = grid
            # a scene volume with "brain_mask" in its name on the identical grid
            for volumeNode in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
                if "brain_mask" not in (volumeNode.GetName() or "") or volumeNode.GetParentTransformNode() is not None:
                    continue
                imageData = volumeNode.GetImageData()
                if imageData is None or tuple(imageData.GetDimensions()) != tuple(dims):
                    continue
                matrix = vtk.vtkMatrix4x4()
                volumeNode.GetIJKToRASMatrix(matrix)
                volumeAffine = numpy.array([[matrix.GetElement(r, c) for c in range(4)] for r in range(4)])
                if numpy.abs(volumeAffine - affine).max() <= 1e-3:
                    self.brainMask = {"state": "ready", "grid": templateGrid, "array": slicer.util.arrayFromVolume(volumeNode) != 0,
                                      "source": volumeNode.GetName()}
                    break
            if self.brainMask is None:
                path = self.brainMaskPath(templateGrid)
                if path:
                    self.startBrainMaskRead(templateGrid, path, dims, affine)
        if hasattr(self, "stayInsideBrainCheckBox"):
            self.stayInsideBrainCheckBox.enabled = self.brainMask is not None

    @staticmethod
    def brainMaskPath(templateGrid):
        fileName = _lazyModules()["algorithm"].BRAIN_MASK_FILE_NAMES.get(tuple(templateGrid))
        if not fileName:
            return None
        directories = [slicer.app.cachePath]
        if hasattr(slicer.modules, "sampledata"):
            directories.append(os.path.join(os.path.dirname(slicer.modules.sampledata.path), "Resources", "Data"))
        for directory in directories:
            path = os.path.join(directory, fileName)
            if os.path.isfile(path):
                return path
        return None

    def startBrainMaskRead(self, templateGrid, path, dims, affine):
        import threading

        algorithm = _lazyModules()["algorithm"]
        key = (tuple(templateGrid), path, os.path.getmtime(path))
        result = {}
        self.brainMask = {"state": "loading", "grid": templateGrid, "array": None, "source": path, "key": key,
                          "dims": dims, "affine": affine, "result": result}
        if key in _brainMaskFileCache:
            result["data"] = _brainMaskFileCache[key]
            self.finishBrainMaskRead()
            return

        def read():
            try:
                result["data"] = algorithm.readNifti1(path)  # files and numpy only in the thread
            except Exception as e:
                result["error"] = e

        self.maskThread = threading.Thread(target=read, name="SmartFillBrainMaskReader", daemon=True)
        self.maskThread.start()
        self.maskTimer.start()

    def onMaskTimer(self):
        if self.maskThread is not None and self.maskThread.is_alive():
            return
        self.maskTimer.stop()
        self.finishBrainMaskRead()

    def waitForBrainMask(self):
        if self.brainMask is not None and self.brainMask["state"] == "loading":
            if self.maskThread is not None:
                self.maskThread.join(2.0)
            self.maskTimer.stop()
            self.finishBrainMaskRead()

    def finishBrainMaskRead(self):
        mask = self.brainMask
        if mask is None or mask["state"] != "loading":
            return
        self.maskThread = None
        result = mask["result"]
        if "data" not in result:
            logging.warning(f"Smart fill: brain mask {mask['source']} could not be read: {result.get('error')}")
            self.brainMask = None
        else:
            _brainMaskFileCache[mask["key"]] = result["data"]
            array, maskAffine = result["data"]
            mapped = _lazyModules()["algorithm"].maskToReference(array, maskAffine, mask["dims"], mask["affine"])
            if mapped is None:
                self.brainMask = None
            else:
                self.brainMask = {"state": "ready", "grid": mask["grid"], "array": mapped, "source": mask["source"]}
        if hasattr(self, "stayInsideBrainCheckBox"):
            self.stayInsideBrainCheckBox.enabled = self.brainMask is not None

    #
    # Preview display
    #

    def clearParts(self):
        self.parts = []
        self.lastPart = None
        self.previewImage = None
        self.previewExtent = None
        self.previewLabels = None
        self.previewCounts = (0, 0)
        for sliceWidget, pipeline in self.pipelines.items():
            pipeline.setVisible(False)
            sliceWidget.sliceView().scheduleRender()

    def removePipelines(self):
        for sliceWidget, pipeline in self.pipelines.items():
            for actor in pipeline.actors():
                self.scriptedEffect.removeActor2D(sliceWidget, actor)
            sliceWidget.sliceView().scheduleRender()
        self.pipelines = {}

    def updatePreviewDisplay(self):
        modules = _lazyModules()
        numpy, numpy_support, algorithm = modules["numpy"], modules["numpy_support"], modules["algorithm"]
        offset, labels = algorithm.composeParts(self.parts)
        if labels is None:
            self.clearParts()
            return
        sourceImage = self.scriptedEffect.sourceVolumeImageData()
        sourceExtent = sourceImage.GetExtent() if sourceImage is not None else (0, 0, 0, 0, 0, 0)
        start = (int(offset[2]) + sourceExtent[0], int(offset[1]) + sourceExtent[2], int(offset[0]) + sourceExtent[4])
        extent = [start[0], start[0] + labels.shape[2] - 1, start[1], start[1] + labels.shape[1] - 1, start[2], start[2] + labels.shape[0] - 1]
        image = vtk.vtkImageData()
        image.SetExtent(extent)
        image.GetPointData().SetScalars(numpy_support.numpy_to_vtk(labels.ravel(), deep=True, array_type=vtk.VTK_UNSIGNED_CHAR))
        self.previewImage = image
        self.previewExtent = extent
        self.previewLabels = labels
        self.previewCounts = (int(numpy.count_nonzero(labels == ADD)), int(numpy.count_nonzero(labels == REMOVE)))

        segmentationNode = self.scriptedEffect.parameterSetNode().GetSegmentationNode()
        parentTransformNode = segmentationNode.GetParentTransformNode() if segmentationNode else None
        self.nonLinearTransform = parentTransformNode is not None and not parentTransformNode.IsTransformToWorldLinear()
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None or self.nonLinearTransform:
            for pipeline in self.pipelines.values():
                pipeline.setVisible(False)
            return
        color = self.segmentColor()
        lastPart = self.lastPart or {}
        warning = bool(lastPart.get("limitReached") or lastPart.get("noEdge") or lastPart.get("extending"))
        outlineColor = qt.QColor(AMBER) if warning else qt.QColor.fromRgbF(*color)
        widgetsToKeep = []
        for sliceViewName in layoutManager.sliceViewNames():
            sliceWidget = layoutManager.sliceWidget(sliceViewName)
            if not sliceWidget.isVisible() or not self.scriptedEffect.segmentationDisplayableInView(sliceWidget.mrmlSliceNode()):
                continue
            widgetsToKeep.append(sliceWidget)
            pipeline = self.pipelines.get(sliceWidget)
            if pipeline is None:
                pipeline = PreviewPipeline(self, sliceWidget)
                self.pipelines[sliceWidget] = pipeline
                for actor in pipeline.actors():
                    self.scriptedEffect.addActor2D(sliceWidget, actor)
            pipeline.setInput(image)
            pipeline.setColors(color, (outlineColor.redF(), outlineColor.greenF(), outlineColor.blueF()))
            pipeline.updateGeometry()
            pipeline.setVisible(True)
            sliceWidget.sliceView().scheduleRender()
        for sliceWidget in list(self.pipelines.keys()):
            if sliceWidget not in widgetsToKeep:
                for actor in self.pipelines[sliceWidget].actors():
                    self.scriptedEffect.removeActor2D(sliceWidget, actor)
                del self.pipelines[sliceWidget]
        self.updateHud()

    def segmentColor(self):
        parameterSetNode = self.scriptedEffect.parameterSetNode()
        segmentationNode = parameterSetNode.GetSegmentationNode()
        segmentId = parameterSetNode.GetSelectedSegmentID()
        if segmentationNode is None or not segmentId:
            return (1.0, 1.0, 0.0)
        displayNode = segmentationNode.GetDisplayNode()
        if displayNode is not None:
            color = [0.0, 0.0, 0.0]
            try:
                if displayNode.GetSegmentColor(segmentId, color):
                    return tuple(color)
            except TypeError:
                pass
        segment = segmentationNode.GetSegmentation().GetSegment(segmentId)
        return tuple(segment.GetColor()) if segment is not None else (1.0, 1.0, 0.0)

    def hudText(self):
        if not self.parts:
            return ""
        lastPart = self.lastPart or {}
        if self.task is not None and self.task.get("phase") == "extend":
            return _("extending…")
        voxelVolumeMm3 = self.voxelVolumeMm3()
        text = self.formatVolume(self.previewCounts[0] * voxelVolumeMm3, "+")
        if self.previewCounts[1]:
            text += " · " + self.formatVolume(self.previewCounts[1] * voxelVolumeMm3, "\u2212")
        if lastPart:
            text += " · ±" + self.formatTolerance(lastPart["tolerance"])
        return text

    def updateHud(self):
        text = self.hudText()
        for sliceWidget, pipeline in self.pipelines.items():
            xy = self.clickXyInView(sliceWidget) if text and self.hudVisible and sliceWidget is self.lastClickView else None
            if pipeline.setHud(text if xy is not None else "", xy):
                sliceWidget.sliceView().scheduleRender()

    def clickXyInView(self, sliceWidget):
        """XY of the clicked point in the view, or None if it is not in the displayed slice (or not visible)."""
        if self.lastClickRas is None:
            return None
        sliceNode = sliceWidget.mrmlSliceNode()
        rasToXy = vtk.vtkMatrix4x4()
        vtk.vtkMatrix4x4.Invert(sliceNode.GetXYToRAS(), rasToXy)
        x, y, z, _w = rasToXy.MultiplyPoint(list(self.lastClickRas) + [1.0])
        dimensions = sliceNode.GetDimensions()
        if abs(z) > 0.5 or not (0 <= x < dimensions[0] and 0 <= y < dimensions[1]):
            return None
        return (x, y)

    #
    # Status
    #

    def setMessage(self, text, warning=False):
        self.message = text
        self.messageIsWarning = warning
        self.updateStatus()

    def updateStatus(self):
        if not hasattr(self, "statusLabel"):
            return
        pending = bool(self.parts)
        self.applyButton.enabled = pending
        self.discardButton.enabled = pending
        lastPart = self.lastPart or {}
        if pending and lastPart:
            self.toleranceLabel.text = "±" + self.formatTolerance(lastPart["tolerance"])
        else:
            self.toleranceLabel.text = ""
        lines = []
        warnings = []
        if self.task is not None and ((self.task.get("busy") and self.task.get("phase") == "click") or self.task.get("phase") == "extend"):
            lines.append(_("extending…") if self.task.get("phase") == "extend" else _("Computing…"))
        elif pending:
            voxelVolumeMm3 = self.voxelVolumeMm3()
            text = _("Preview") + " " + self.formatVolume(self.previewCounts[0] * voxelVolumeMm3, "+")
            if self.previewCounts[1]:
                text += " · " + self.formatVolume(self.previewCounts[1] * voxelVolumeMm3, "\u2212")
            if lastPart:
                text += " · ±" + self.formatTolerance(lastPart["tolerance"])
                if lastPart.get("auto"):
                    text += " " + _("(auto)")
            lines.append(text)
        elif self.message and not self.messageIsWarning:
            lines.append(self.message)
        else:
            ctrl = _nativeControlKeyName()
            lines.append(_("Click inside the lesion · drag ↔ or Shift+wheel: tolerance · Shift+click: add"
                           " · {ctrl}+click: remove · Enter: apply").format(ctrl=ctrl))
        if pending and lastPart:
            if lastPart.get("limitReached"):
                radius = round(lastPart["grower"].effectiveRadiusMm)
                warnings.append(_("Reached the {radius} mm limit — the lesion may continue (raise Max radius) or the region"
                                  " is leaking (lower the tolerance)").format(radius=radius))
            elif lastPart.get("noEdge"):
                warnings.append(_("No clear lesion boundary here"))
        if self.message and self.messageIsWarning:
            warnings.append(self.message)
        if getattr(self, "obliqueNote", False) and pending:
            warnings.append(_("Slice not aligned with the image axes: used 3D"))
        if getattr(self, "nonLinearTransform", False) and pending:
            warnings.append(_("The preview is not shown with non-linear transforms"))
        parameterSetNode = self.scriptedEffect.parameterSetNode()
        if parameterSetNode is not None and pending and parameterSetNode.GetMaskMode() != slicer.vtkMRMLSegmentationNode.EditAllowedEverywhere:
            warnings.append(_("Masking applies on Apply"))
        html = "<br>".join(self.escapeHtml(line) for line in lines)
        for warning in warnings:
            html += f'<br><span style="color: {AMBER};">{self.escapeHtml(warning)}</span>'
        self.statusLabel.text = html
        self.updateHud()

    @staticmethod
    def escapeHtml(text):
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def voxelVolumeMm3(self):
        spacing = self.segmentSpacing()
        return float(spacing[0] * spacing[1] * spacing[2])

    @staticmethod
    def formatVolume(volumeMm3, sign):
        volumeMl = volumeMm3 / 1000.0
        text = f"{volumeMl:.2f}" if volumeMl < 1.0 else f"{volumeMl:.1f}"
        return f"{sign}{text} mL"

    @staticmethod
    def formatTolerance(tolerance):
        magnitude = abs(tolerance)
        if magnitude >= 100:
            return f"{tolerance:.0f}"
        if magnitude >= 1:
            return f"{tolerance:.1f}".rstrip("0").rstrip(".")
        return f"{tolerance:.3g}"

    #
    # Segment voxels (for the apply observers)
    #

    def segmentMaskInExtent(self, extent):
        """Bool array [k, j, i] of the selected segment inside an extent of the reference geometry."""
        modules = _lazyModules()
        numpy, numpy_support = modules["numpy"], modules["numpy_support"]
        shape = (extent[5] - extent[4] + 1, extent[3] - extent[2] + 1, extent[1] - extent[0] + 1)
        output = numpy.zeros(shape, bool)
        parameterSetNode = self.scriptedEffect.parameterSetNode()
        segmentationNode = parameterSetNode.GetSegmentationNode()
        segmentId = parameterSetNode.GetSelectedSegmentID()
        segment = segmentationNode.GetSegmentation().GetSegment(segmentId) if segmentationNode and segmentId else None
        if segment is None:
            return output
        labelmap = segment.GetRepresentation(slicer.vtkSegmentationConverter.GetSegmentationBinaryLabelmapRepresentationName())
        referenceImage = self.scriptedEffect.referenceGeometryImage()
        if labelmap is None or labelmap.GetPointData().GetScalars() is None or referenceImage is None:
            return output
        labelValue = segment.GetLabelValue()
        if not slicer.vtkOrientedImageDataResample.DoGeometriesMatch(labelmap, referenceImage):
            threshold = vtk.vtkImageThreshold()
            threshold.SetInputData(labelmap)
            threshold.ThresholdBetween(labelValue, labelValue)
            threshold.SetInValue(1)
            threshold.SetOutValue(0)
            threshold.SetOutputScalarTypeToUnsignedChar()
            threshold.Update()
            thresholded = slicer.vtkOrientedImageData()
            thresholded.ShallowCopy(threshold.GetOutput())
            thresholded.CopyDirections(labelmap)
            reference = slicer.vtkOrientedImageData()
            reference.SetExtent(extent)
            reference.SetSpacing(referenceImage.GetSpacing())
            reference.SetOrigin(referenceImage.GetOrigin())
            reference.CopyDirections(referenceImage)
            resampled = slicer.vtkOrientedImageData()
            if slicer.vtkOrientedImageDataResample.ResampleOrientedImageToReferenceOrientedImage(thresholded, reference, resampled, False):
                labelmap, labelValue = resampled, 1
            else:
                return output
        labelmapExtent = labelmap.GetExtent()
        lower = [max(extent[2 * a], labelmapExtent[2 * a]) for a in range(3)]
        upper = [min(extent[2 * a + 1], labelmapExtent[2 * a + 1]) for a in range(3)]
        if any(lower[a] > upper[a] for a in range(3)):
            return output
        labelmapShape = (labelmapExtent[5] - labelmapExtent[4] + 1, labelmapExtent[3] - labelmapExtent[2] + 1, labelmapExtent[1] - labelmapExtent[0] + 1)
        array = numpy_support.vtk_to_numpy(labelmap.GetPointData().GetScalars())
        if array.ndim > 1:
            array = array[:, 0]
        array = array.reshape(labelmapShape)
        source = array[lower[2] - labelmapExtent[4]:upper[2] - labelmapExtent[4] + 1,
                       lower[1] - labelmapExtent[2]:upper[1] - labelmapExtent[2] + 1,
                       lower[0] - labelmapExtent[0]:upper[0] - labelmapExtent[0] + 1]
        output[lower[2] - extent[4]:upper[2] - extent[4] + 1,
               lower[1] - extent[2]:upper[1] - extent[2] + 1,
               lower[0] - extent[0]:upper[0] - extent[0] + 1] = source == labelValue
        return output


class PreviewPipeline:
    """Preview of the Smart fill labels in one slice view: fill, outlines and a small text label (HUD)."""

    def __init__(self, effect, sliceWidget):
        self.effect = effect
        self.sliceWidget = sliceWidget
        self.devicePixelRatio = _devicePixelRatio(sliceWidget)

        self.reslice = vtk.vtkImageReslice()
        self.reslice.SetInterpolationModeToNearestNeighbor()
        self.reslice.SetOutputDimensionality(2)
        self.reslice.SetOutputOrigin(0, 0, 0)
        self.reslice.SetOutputSpacing(1, 1, 1)

        self.lookupTable = vtk.vtkLookupTable()
        self.lookupTable.SetNumberOfTableValues(3)
        self.lookupTable.SetTableRange(0, 2)
        self.lookupTable.SetTableValue(0, 0.0, 0.0, 0.0, 0.0)
        self.lookupTable.SetTableValue(1, 1.0, 1.0, 0.0, 0.45)
        self.lookupTable.SetTableValue(2, 1.0, 1.0, 1.0, 0.30)
        self.lookupTable.Build()
        self.colors = vtk.vtkImageMapToColors()
        self.colors.SetLookupTable(self.lookupTable)
        self.colors.SetOutputFormatToRGBA()
        self.colors.SetInputConnection(self.reslice.GetOutputPort())
        self.mapper = vtk.vtkImageMapper()
        self.mapper.SetColorWindow(255)
        self.mapper.SetColorLevel(127.5)
        self.mapper.SetInputConnection(self.colors.GetOutputPort())
        self.fillActor = vtk.vtkActor2D()
        self.fillActor.SetMapper(self.mapper)

        self.outlineActors = {}
        self.outlineFilters = []
        for label in (ADD, REMOVE):
            threshold = vtk.vtkImageThreshold()
            threshold.SetInputConnection(self.reslice.GetOutputPort())
            threshold.ThresholdBetween(label, label)
            threshold.SetInValue(1)
            threshold.SetOutValue(0)
            threshold.SetOutputScalarTypeToUnsignedChar()
            contour = vtk.vtkFlyingEdges2D()
            contour.SetInputConnection(threshold.GetOutputPort())
            contour.SetValue(0, 0.5)
            mapper = vtk.vtkPolyDataMapper2D()
            mapper.SetInputConnection(contour.GetOutputPort())
            mapper.ScalarVisibilityOff()
            actor = vtk.vtkActor2D()
            actor.SetMapper(mapper)
            actor.GetProperty().SetLineWidth(2.0 * self.devicePixelRatio)
            self.outlineActors[label] = actor
            self.outlineFilters.append((threshold, contour, mapper))
        self.outlineActors[REMOVE].GetProperty().SetColor(1.0, 1.0, 1.0)

        self.hudActor = vtk.vtkTextActor()
        textProperty = self.hudActor.GetTextProperty()
        textProperty.SetFontSize(round(12 * self.devicePixelRatio))
        textProperty.BoldOn()
        textProperty.ShadowOn()
        textProperty.SetColor(1.0, 1.0, 1.0)
        self.hudActor.SetVisibility(False)

    def actors(self):
        return [self.fillActor, self.outlineActors[ADD], self.outlineActors[REMOVE], self.hudActor]

    def setInput(self, image):
        self.reslice.SetInputData(image)

    def setColors(self, fillColor, outlineColor):
        self.lookupTable.SetTableValue(1, fillColor[0], fillColor[1], fillColor[2], 0.45)
        self.lookupTable.Modified()
        self.outlineActors[ADD].GetProperty().SetColor(*outlineColor)

    def updateGeometry(self):
        """Reslice the label image (index space) to the XY pixels of the view."""
        effect = self.effect
        sliceNode = self.sliceWidget.mrmlSliceNode()
        referenceImage = effect.scriptedEffect.referenceGeometryImage()
        if referenceImage is None:
            return
        worldToImage = vtk.vtkMatrix4x4()
        referenceImage.GetWorldToImageMatrix(worldToImage)
        xyToWorld = vtk.vtkMatrix4x4()
        xyToWorld.DeepCopy(sliceNode.GetXYToRAS())
        segmentationNode = effect.scriptedEffect.parameterSetNode().GetSegmentationNode()
        parentTransformNode = segmentationNode.GetParentTransformNode() if segmentationNode else None
        if parentTransformNode is not None:
            worldToSegmentation = vtk.vtkMatrix4x4()
            parentTransformNode.GetMatrixTransformFromWorld(worldToSegmentation)
            vtk.vtkMatrix4x4.Multiply4x4(worldToSegmentation, xyToWorld, xyToWorld)
        xyToImage = vtk.vtkMatrix4x4()
        vtk.vtkMatrix4x4.Multiply4x4(worldToImage, xyToWorld, xyToImage)
        self.reslice.SetResliceAxes(xyToImage)
        dimensions = sliceNode.GetDimensions()
        self.reslice.SetOutputExtent(0, dimensions[0] - 1, 0, dimensions[1] - 1, 0, 0)

    def setVisible(self, visible):
        self.fillActor.SetVisibility(visible)
        self.outlineActors[ADD].SetVisibility(visible)
        self.outlineActors[REMOVE].SetVisibility(visible)
        if not visible:
            self.hudActor.SetVisibility(False)
            self.hudState = None

    def setHud(self, text, xy):
        """Show the text next to the clicked point (empty text: hide). Returns True if the view needs a render."""
        state = (text, tuple(xy) if xy is not None and text else None, tuple(self.sliceWidget.mrmlSliceNode().GetDimensions()))
        if state == getattr(self, "hudState", None):
            return False
        self.hudState = state
        if not text or xy is None:
            self.hudActor.SetVisibility(False)
            return True
        self.hudActor.SetInput(text)
        dpr = self.devicePixelRatio
        x, y = xy[0] + 14 * dpr, xy[1] - 14 * dpr
        dimensions = self.sliceWidget.mrmlSliceNode().GetDimensions()
        renderer = self.effect.scriptedEffect.renderer(self.sliceWidget)
        size = [0.0, 0.0]
        if renderer is not None:
            self.hudActor.GetSize(renderer, size)
        x = min(max(x, 2 * dpr), dimensions[0] - size[0] - 2 * dpr)
        y = min(max(y, 2 * dpr), dimensions[1] - size[1] - 2 * dpr)
        self.hudActor.SetPosition(x, y)
        self.hudActor.SetVisibility(True)
        return True

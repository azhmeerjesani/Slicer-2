"""Validate card of the Map module (order 90, Seed = Cohort; SPEC_LNM_ADDENDUM §3.8, §7.1).

The card asks whether a circuit tells held-out patients apart better than the things it can be confused with, and
puts the evidence where both sides of the 2026 debate look for it: one table of baselines with confidence
intervals. It runs ``LesionNetworkMappingLib.validate`` (L-26) in the job runner, with Stop, and shows its result.

Controls:

- the outcome column: a group column with two levels (binary: AUC, odds ratio) or a score column (continuous: r,
  out-of-sample R²), and the positive class of a binary outcome;
- the circuit that is rebuilt inside every fold from the training subjects only: GLM t map, sLNM partial r map or
  the top-x% ROI of the GLM t map. Nothing is thresholded with a permutation inside a fold;
- the scheme: Auto shows the decision and its reason before anything runs ("LODO: every one of the 2 datasets holds
  both classes", or "LODO not possible: dataset X holds one class → stratified 10-fold x 5 repeats"), LODO,
  k-fold, or LOO (which shows the bias warning, "pooled LOO predictions are negatively biased for r/AUC and have
  high variance");
- the dataset column (automatic, a column, or none), covariates (lesion volume is always in the models), optional
  reference maps (files on the connectome grid, and the registry of the reference-map library when it exists), and
  under Options the score, the bootstrap and permutation counts, the folds and the seed.

Run maps the lesions first when the cohort has no current maps.tsv, then runs the validation into
``<study>/derivatives/lnm/conn-<label>[_n-N]/validation/<outcome>_<circuit>``. The job reports an estimate of the time
of the bootstrap and permutation reruns after the first cross-validation, shown under the progress bar.

Result:

- a table with the rows Circuit (and the circuit with μC and PC1 partialled out), Volume, μC, PC1, Lesion location
  and every reference map: AUC or r, R², OR per SD, 95% CI and p_perm of the primary metric, and the incremental
  row (volume + circuit against volume). Baselines are always listed; one that could not be computed says why;
- neutral verdict sentences ("the circuit does not exceed the degree baseline (ΔAUC 0.01, 95% CI minus 0.04 to 0.06)");
- the held-out ROC curves (binary) or scatter (continuous), drawn with the figure classes of ``figures.py`` and written
  to ``figures/validation`` next to the result (PNG, SVG, source data; registered as a figure run).

"Apply frozen circuit to another study…" takes the current circuit (``ctx.currentCircuit`` from the Cohort tabs, or a
file) with its sha256, asks for another study's folder (mapped with this connectome) and scores its lesions with the
circuit as it is: no fitting, a simple bootstrap CI, no permutation p. The sha256 is recorded in the result. The card
works in Classic and without the Studio shell.
"""

import html
import itertools
import logging
import math
import os
import re

import qt
import slicer
from slicer.i18n import tr as _

from . import ORDER_VALIDATE, widgets
from .cohort_contrast import DESIGN_DELAY_MS, CovariateChips, _fill, _idClickedSignal, _readJson, _stat, disclaimerCaption, duration, label, levelColor, segmented

CIRCUITS = ("glm", "slnm", "topk")
CIRCUIT_TEXTS = {"glm": "GLM t", "slnm": "sLNM r", "topk": "Top-x% ROI"}
SCHEMES = ("auto", "lodo", "kfold", "loo")
SCHEME_TEXTS = {"auto": "Auto", "lodo": "LODO", "kfold": "k-fold", "loo": "LOO"}
SCORES = (("", "Default"), ("similarity", "Spatial r (Fisher z)"), ("damage", "Damage score"), ("roi", "ROI connectivity"))
VALIDATION_FOLDER = "validation"
FIGURE_NAME = "validation"
DEFAULT_TOP_PCT = 10
#: Width of the figure in the card (the inspector never widens)
FIGURE_WIDTH_PX = 360
#: Colours of the curves: the circuit first, then the baselines (distinguishable in grey scale by their order)
CURVE_COLORS = {
    "circuit": "#D1495B",
    "circuit_partial": "#EDAE49",
    "volume": "#6B6B6B",
    "mu_r": "#00798C",
    "pc1": "#30638E",
    "location": "#7D4E9E",
}
REFERENCE_COLORS = ("#3C8D5A", "#B5651D", "#2E5EAA")
#: Short names of the table rows (the tooltip has the full label)
ROW_NAMES = {
    "circuit": "Circuit",
    "circuit_partial": "Circuit, partial",
    "volume": "Volume",
    "mu_r": "μC (degree)",
    "pc1": "PC1",
    "location": "Lesion location",
}
MINUS = "\u2212"


def _mapWidget():
    return getattr(slicer.modules, "LesionNetworkMappingWidget", None)


def _num(value, digits=2):
    """A number with a true minus sign; "—" when it is missing or not finite."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(value):
        return "—"
    return f"{value:.{digits}f}".replace("-", MINUS)


def _ratio(value):
    """An odds ratio; very large ones (a perfectly separated sample) are shown as "> 1000"."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if math.isfinite(number) and number >= 1000:
        return "> 1000"
    return _num(number)


def _interval(ci, digits=2):
    try:
        low, high = float(ci[0]), float(ci[1])
    except (TypeError, ValueError, IndexError):
        return "—"
    if not (math.isfinite(low) and math.isfinite(high)):
        return "—"
    return f"[{_num(low, digits)}, {_num(high, digits)}]"


def _pText(value, floor=None):
    """A permutation p: three significant digits; "≤ x" when it is the smallest attainable one."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(value):
        return "—"
    text = f"{value:.3g}"
    if floor is not None and value <= float(floor) * (1 + 1e-9):
        return "≤ " + text
    return text


def _safeName(text):
    return re.sub(r"[^A-Za-z0-9]+", "", str(text)) or "x"


def _setAlignment(item, alignment):
    try:
        item.setTextAlignment(alignment)
    except (TypeError, ValueError):
        item.setTextAlignment(int(alignment))  # Qt 5 takes an int, Qt 6 the flags


def _tableItem(text, tooltip=None, right=True, bold=False, muted=False):
    item = qt.QTableWidgetItem(text)
    item.setFlags(qt.Qt.ItemIsEnabled)
    if right:
        _setAlignment(item, qt.Qt.AlignRight | qt.Qt.AlignVCenter)
    if tooltip:
        item.setToolTip(tooltip)
    if bold:
        font = qt.QFont(item.font())
        font.setBold(True)
        item.setFont(font)
    if muted:
        item.setForeground(qt.QBrush(slicer.app.palette().color(qt.QPalette.Disabled, qt.QPalette.WindowText)))
    return item


def _value(attribute):
    """A Qt property that PythonQt wraps as an attribute and PyQt-style code calls: its value either way."""
    return attribute() if callable(attribute) else attribute


def _fitTable(table, rows):
    """Show every row without an inner scroll bar (the inspector scrolls)."""
    table.resizeRowsToContents()
    height = _value(table.horizontalHeader().height) + sum(table.rowHeight(row) for row in range(rows)) + 2 * _value(table.frameWidth) + 2
    table.setFixedHeight(height)


def _pixmap(image):
    """A ``qt.QPixmap`` of a Pillow image (read from PPM bytes: Qt 5 and Qt 6)."""
    import numpy as np

    array = np.ascontiguousarray(np.asarray(image.convert("RGB")))
    height, width = array.shape[:2]
    data = b"P6\n%d %d\n255\n" % (width, height) + array.tobytes()
    return qt.QPixmap.fromImage(qt.QImage.fromData(qt.QByteArray(data), "PPM"))


#
# Figure (ROC curves or scatter), drawn with the classes of figures.py
#


def _readScores(path):
    """{column: [values]} of scores.tsv (strings; "n/a" stays as is)."""
    with open(path, encoding="utf-8") as f:
        lines = [line.rstrip("\n").split("\t") for line in f if line.strip()]
    header = lines[0]
    return {name: [row[index] if index < len(row) else "n/a" for row in lines[1:]] for index, name in enumerate(header)}


def _floats(values):
    import numpy as np

    out = []
    for value in values:
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            out.append(float("nan"))
    return np.asarray(out, dtype=np.float64)


def rocCurve(prediction, outcome):
    """(fpr, tpr) of the points where the ROC curve changes (ties in the score move diagonally), from 0,0 to 1,1."""
    import numpy as np

    prediction = np.asarray(prediction, dtype=np.float64)
    outcome = np.asarray(outcome, dtype=np.float64)
    keep = np.isfinite(prediction) & np.isfinite(outcome)
    prediction, outcome = prediction[keep], outcome[keep]
    positives, negatives = float(np.sum(outcome == 1)), float(np.sum(outcome == 0))
    if positives == 0 or negatives == 0:
        return np.array([0.0, 1.0]), np.array([0.0, 1.0])
    order = np.argsort(-prediction, kind="stable")
    prediction, outcome = prediction[order], outcome[order]
    tp, fp = np.cumsum(outcome == 1), np.cumsum(outcome == 0)
    last = np.r_[np.flatnonzero(np.diff(prediction)), prediction.size - 1]  # the last of each run of equal scores
    return np.r_[0.0, fp[last] / negatives], np.r_[0.0, tp[last] / positives]


def validationFigure(record, scoresPath, dpi=150, widthMm=89.0):
    """The held-out ROC curves (binary outcome) or the held-out scatter (continuous) as a ``figures.Figure``.

    The curves are those of the out-of-fold model predictions in scores.tsv, one per computed predictor (circuit,
    then the baselines), with the AUC and its 95% CI in the legend; the scatter shows the circuit's predictions
    against the outcome. ``figures.write_figure`` writes PNG, SVG, figure.json, caption.txt and the source data.
    Returns None when the result holds no predictions.
    """
    import numpy as np
    from PIL import Image

    from LesionNetworkMappingLib import figures

    table = _readScores(scoresPath)
    outcome = _floats(table.get("outcome") or [])
    predictors = [item for item in record.get("predictors") or [] if item.get("status") == "computed" and f"{item['name']}_prediction" in table]
    if outcome.size == 0 or not predictors:
        return None
    binary = record.get("outcome_type") == "binary"
    fonts = figures.Fonts(None)
    scale = dpi / 72.0
    text, small, big = round(figures.TEXT_PT * scale), round(figures.SMALL_PT * scale), round(figures.LETTER_PT * scale)
    margin = round(6 * scale)
    width = round(widthMm / 25.4 * dpi)  # a single journal column
    legendRows = len(predictors) if binary else 2
    legendHeight = round(small * 1.9) * legendRows
    plot = width - 2 * margin - round(text * 3.2)  # square plot area
    left, top = margin + round(text * 3.2), margin + round(big * 2.6)
    bottom = top + plot
    height = bottom + round(small * 3.6) + legendHeight + margin
    base = Image.new("RGB", (width, height), (255, 255, 255))
    ink, grid = "#000000", "#C8C8C8"
    line = max(1, round(scale / 2.5))
    annotations = []
    title = _("Held-out ROC curves") if binary else _("Held-out predictions of the circuit")
    annotations.append(figures.Annotation("text", margin, margin, text=title, size=big, color=ink, anchor="lt", bold=True))
    for tick in (0.0, 0.5, 1.0):
        x = left + plot * tick
        y = bottom - plot * tick
        annotations.append(figures.Annotation("line", x, top, w=x, h=bottom, size=line, color=grid))
        annotations.append(figures.Annotation("line", left, y, w=left + plot, h=y, size=line, color=grid))
    sourcedata = []
    caption = ""
    legend = []
    if binary:
        annotations.append(figures.Annotation("line", left, bottom, w=left + plot, h=top, size=line, color="#9A9A9A", opacity=0.8))
        for tick in (0.0, 0.5, 1.0):
            annotations.append(figures.Annotation("text", left + plot * tick, bottom + round(small * 0.4), text=f"{tick:g}", size=small, color=ink, anchor="mt"))
            annotations.append(figures.Annotation("text", left - round(small * 0.5), bottom - plot * tick, text=f"{tick:g}", size=small, color=ink, anchor="rm"))
        annotations.append(figures.Annotation("text", left + plot / 2, bottom + round(small * 1.9), text=_("False positive rate"), size=small, color=ink, anchor="mt"))
        annotations.append(figures.Annotation("text", left, top - round(small * 0.5), text=_("True positive rate"), size=small, color=ink, anchor="lb"))  # above the axis: Pillow text is not rotated
        references = 0
        for item in predictors:
            name = item["name"]
            color = CURVE_COLORS.get(name)
            if color is None:
                color = REFERENCE_COLORS[references % len(REFERENCE_COLORS)]
                references += 1
            fpr, tpr = rocCurve(_floats(table[f"{name}_prediction"]), outcome)
            points = [(left + plot * float(x), bottom - plot * float(y)) for x, y in zip(fpr, tpr, strict=True)]
            heavy = name == "circuit"
            for (x0, y0), (x1, y1) in itertools.pairwise(points):
                annotations.append(figures.Annotation("line", x0, y0, w=x1, h=y1, size=line * (2 if heavy else 1), color=color))
            for x, y in zip(fpr, tpr, strict=True):
                sourcedata.append({"predictor": name, "fpr": f"{float(x):.6g}", "tpr": f"{float(y):.6g}"})
            metric = (item.get("metrics") or {}).get("auc") or {}
            legend.append((color, ROW_NAMES.get(name) or name.replace("ref:", _("Reference: ")), f"AUC {_num(metric.get('value'))} {_interval(metric.get('ci'))}", heavy))
        caption = _("Receiver operating characteristic curves of the out-of-fold model predictions (scores.tsv). Each curve belongs to a model with lesion volume and one predictor, "
                    "fitted on the training subjects only; the legend gives the AUC with its whole-cross-validation bootstrap 95% CI.")
    else:
        circuit = next(item for item in predictors if item["name"] == "circuit")
        x = _floats(table["circuit_prediction"])
        keep = np.isfinite(x) & np.isfinite(outcome)
        xs, ys = x[keep], outcome[keep]
        low, high = float(min(xs.min(), ys.min())), float(max(xs.max(), ys.max()))
        span = (high - low) or 1.0
        low, high = low - 0.04 * span, high + 0.04 * span

        def at(value, lo, hi):
            return (value - lo) / (hi - lo)

        annotations.append(figures.Annotation("line", left, bottom - plot * at(low, low, high), w=left + plot, h=bottom - plot * at(high, low, high), size=line, color="#9A9A9A", opacity=0.8))
        for tick in (0.0, 0.5, 1.0):
            value = low + (high - low) * tick
            annotations.append(figures.Annotation("text", left + plot * tick, bottom + round(small * 0.4), text=_num(value, 1), size=small, color=ink, anchor="mt"))
            annotations.append(figures.Annotation("text", left - round(small * 0.5), bottom - plot * tick, text=_num(value, 1), size=small, color=ink, anchor="rm"))
        annotations.append(figures.Annotation("text", left + plot / 2, bottom + round(small * 1.9), text=_("Out-of-fold prediction"), size=small, color=ink, anchor="mt"))
        annotations.append(figures.Annotation("text", left, top - round(small * 0.5), text=_("Outcome"), size=small, color=ink, anchor="lb"))
        half = max(2, round(scale * 1.2))
        sids = table.get("participant_id") or [""] * outcome.size
        for index in np.flatnonzero(keep):
            px, py = left + plot * at(x[index], low, high), bottom - plot * at(outcome[index], low, high)
            annotations.append(figures.Annotation("rect", px - half, py - half, w=2 * half, h=2 * half, color=CURVE_COLORS["circuit"], opacity=0.75))
            sourcedata.append({"participant_id": sids[index], "outcome": f"{outcome[index]:.6g}", "prediction": f"{x[index]:.6g}"})
        r = (circuit.get("metrics") or {}).get("r") or {}
        r2 = (circuit.get("metrics") or {}).get("r2") or {}
        legend.append((CURVE_COLORS["circuit"], _("Circuit"), f"r {_num(r.get('value'))} {_interval(r.get('ci'))}", True))
        legend.append((CURVE_COLORS["circuit"], _("Circuit"), f"R² {_num(r2.get('value'))} {_interval(r2.get('ci'))}", False))
        caption = _("Out-of-fold model predictions of the circuit (scores.tsv) against the outcome; the dashed line is equality. The out-of-sample R² is measured against the training mean and can be negative.")
    y = bottom + round(small * 3.4)
    for color, name, detail, heavy in legend:
        annotations.append(figures.Annotation("line", left, y + small * 0.5, w=left + round(text * 1.6), h=y + small * 0.5, size=line * (2 if heavy else 1), color=color))
        annotations.append(figures.Annotation("text", left + round(text * 2.0), y + small * 0.5, text=f"{name}  {detail}", size=small, color=ink, anchor="lm"))
        y += round(small * 1.9)
    meta = {
        "format": figures.FORMAT,
        "version": figures.VERSION,
        "preset": "validation-roc" if binary else "validation-scatter",
        "title": title,
        "params": {"circuit": (record.get("circuit") or {}).get("kind"), "scheme": (record.get("scheme") or {}).get("used"), "outcome": (record.get("outcome") or {}).get("column")},
        "inputs": [],
        "convention": None,
        "panels": [],
        "tiles": [],
        "colorbars": [],
        "dpi": int(dpi),
        "width_mm": float(widthMm),
        "size_px": [width, height],
        "font": "Inter" if not fonts.default else "Pillow default",
        "watermark": None,
        "caption": caption,
    }
    return figures.Figure(base, annotations, dpi, fonts, meta, caption + "\n", sourcedata)


#
# The card
#


class ValidateCard:
    """Controller of the Validate card (see the module docstring)."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.job = None
        self.mapsJob = None
        self.frozenJob = None
        self.jobFolder = None
        self.jobKind = None  # "cv" or "frozen": what ``job`` is
        self.result = None  # (folder, record) of the shown cross-validation
        self.frozenResult = None  # (folder, record) of the shown frozen replication
        self.figureFile = None
        self._figureToken = None
        self._tableCache = (None, None)
        self._scope = None
        self._cleanedUp = False
        self._circuitTouched = False
        self._refmaps = []  # chosen reference map files
        self.refmapPopover = None
        self.decision = None
        self.decisionError = None
        self.notice = ""  # a failure or stop of the last job, shown instead of the run state until the settings change
        self._jobFolder = None
        self._timer = qt.QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(DESIGN_DELAY_MS)
        self._timer.connect("timeout()", self.refresh)
        self.widget = qt.QWidget()
        self.widget.setObjectName("LNMValidate")
        layout = qt.QVBoxLayout(self.widget)
        layout.setSpacing(6)
        layout.setContentsMargins(0, 0, 0, 0)
        self.statusLabel = label("", "LNMValidateStatus", "secondaryText")
        layout.addWidget(self.statusLabel)
        self.introLabel = label(_("Does the circuit separate held-out patients better than lesion volume, the degree map (μC), PC1 and lesion location? "
                                  "These baselines are always computed in the same folds."), "LNMValidateIntro", "secondaryText")
        layout.addWidget(self.introLabel)
        self._buildControls(layout)
        self._buildRun(layout)
        self._buildResults(layout)
        self._buildFrozen(layout)
        layout.addStretch(1)
        for name in ("studyChanged", "bundleChanged", "computeEnabledChanged"):
            getattr(ctx, name).append(self.refresh)
        if not hasattr(ctx, "currentCircuitChanged"):
            ctx.currentCircuitChanged = []  # created on first use (the Symptom tab does the same): MapContext has no such list
        ctx.currentCircuitChanged.append(self.onCircuitChanged)
        ctx.runner.jobFinished.append(self._onJobFinished)
        self.refresh()

    #
    # Build
    #

    def _combo(self, objectName, toolTip, callback=None):
        combo = qt.QComboBox()
        combo.setObjectName(objectName)
        combo.setToolTip(toolTip)
        combo.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        combo.connect("currentIndexChanged(int)", lambda index: (callback or self.onSettingsChanged)())
        return combo

    def _buildControls(self, layout):
        form = qt.QFormLayout()
        form.setFieldGrowthPolicy(qt.QFormLayout.AllNonFixedFieldsGrow)
        self.outcomeCombo = self._combo("LNMValidateOutcome", _("The outcome the circuit is validated against: a group column with two levels (binary: AUC, odds ratio) or a score (continuous: r, out-of-sample R²)"),
                                        self.onOutcomeChanged)
        form.addRow(_("Outcome"), self.outcomeCombo)
        self.positiveCombo = self._combo("LNMValidatePositive", _("The class that counts as positive (AUC and odds ratio are of this class)"))
        self.positiveRow = qt.QWidget()
        positiveLayout = qt.QHBoxLayout(self.positiveRow)
        positiveLayout.setContentsMargins(0, 0, 0, 0)
        positiveLayout.addWidget(self.positiveCombo)
        form.addRow(_("Positive class"), self.positiveRow)
        self.positiveForm = form
        # Circuit
        circuitRow = qt.QHBoxLayout()
        self.circuitFrame, self.circuitGroup = segmented([_(CIRCUIT_TEXTS[key]) for key in CIRCUITS], "LNMValidateCircuit",
                                                         _("The circuit rebuilt in every fold from the training lesions only: GLM t map of the outcome, sLNM partial r map, or the top-x% ROI of the GLM t map. "
                                                           "Never a permutation-FWE threshold inside a fold."))
        self.circuitGroup.connect(_idClickedSignal(self.circuitGroup), lambda index: self.onCircuitClicked())
        circuitRow.addWidget(self.circuitFrame)
        self.topSpin = qt.QSpinBox()
        self.topSpin.setObjectName("LNMValidateTopPct")
        self.topSpin.setRange(1, 50)
        self.topSpin.setValue(DEFAULT_TOP_PCT)
        self.topSpin.setSuffix(" %")
        self.topSpin.setToolTip(_("The ROI is the top x% of |t| of the in-fold map (a fixed rank rule)"))
        self.topSpin.connect("valueChanged(int)", lambda value: self.onSettingsChanged())
        circuitRow.addWidget(self.topSpin)
        circuitRow.addStretch(1)
        form.addRow(_("Circuit"), circuitRow)
        # Scheme
        self.schemeFrame, self.schemeGroup = segmented([_(SCHEME_TEXTS[key]) for key in SCHEMES], "LNMValidateScheme",
                                                       _("Auto: leave-one-dataset-out when every held-out dataset can be scored, else stratified 10-fold × 5 repeats · LODO · k-fold · "  # noqa: RUF001
                                                         "LOO (pooled LOO predictions are negatively biased and have a high variance)"))
        self.schemeGroup.connect(_idClickedSignal(self.schemeGroup), lambda index: self.onSettingsChanged())
        form.addRow(_("Scheme"), self.schemeFrame)
        self.datasetCombo = self._combo("LNMValidateDataset", _("The datasets: folds of LODO, bootstrap strata and permutation blocks"))
        form.addRow(_("Datasets"), self.datasetCombo)
        layout.addLayout(form)
        self.schemeLabel = label("", "LNMValidateSchemeReason", "secondaryText")
        self.schemeLabel.setTextFormat(qt.Qt.RichText)
        layout.addWidget(self.schemeLabel)
        # Covariates
        self.covariatesTitle = label(_("Covariates"), "LNMValidateCovariatesTitle", "secondaryText")
        layout.addWidget(self.covariatesTitle)
        self.covariates = CovariateChips("LNMValidate", self.onSettingsChanged)
        layout.addWidget(self.covariates.widget)
        self.volumeLabel = label(_("Lesion volume is in every model: it is the first baseline."), "LNMValidateVolume", "caption")
        layout.addWidget(self.volumeLabel)
        # Reference maps
        refRow = qt.QHBoxLayout()
        self.refmapButton = qt.QPushButton(_("Reference maps…"))
        self.refmapButton.setObjectName("LNMValidateRefmapsButton")
        self.refmapButton.setProperty("slicerRole", "ghost")
        self.refmapButton.setToolTip(_("Unrelated maps (on the connectome grid) used as extra baselines: the table shows whether the circuit separates held-out patients better than a map of another condition does"))
        self.refmapButton.connect("clicked()", self.showRefmaps)
        refRow.addWidget(self.refmapButton)
        self.refmapLabel = label("", "LNMValidateRefmaps", "secondaryText")
        refRow.addWidget(self.refmapLabel, 1)
        layout.addLayout(refRow)
        # Options
        self.optionsButton = qt.QToolButton()
        self.optionsButton.setObjectName("LNMValidateOptionsButton")
        self.optionsButton.setText(_("Options"))
        self.optionsButton.setCheckable(True)
        self.optionsButton.setToolButtonStyle(qt.Qt.ToolButtonTextBesideIcon)
        self.optionsButton.setArrowType(qt.Qt.RightArrow)
        self.optionsButton.setProperty("slicerRole", "ghost")
        self.optionsButton.setToolTip(_("Score, bootstrap and permutation counts, folds, seed"))
        self.optionsButton.connect("toggled(bool)", self.onOptionsToggled)
        layout.addWidget(self.optionsButton, 0, qt.Qt.AlignLeft)
        self.options = qt.QWidget()
        self.options.setObjectName("LNMValidateOptions")
        options = qt.QFormLayout(self.options)
        options.setContentsMargins(0, 0, 0, 0)
        self.scoreCombo = self._combo("LNMValidateScore", _("The lesion score the models use: the Fisher-z spatial r of the lesion's network map with the circuit (default), the damage score (needs the lesion "
                                                            "occupancy of the cohort maps) or the ROI connectivity (top-x% circuits only)"))
        for key, text in SCORES:
            self.scoreCombo.addItem(_(text), key)
        options.addRow(_("Score"), self.scoreCombo)
        self.bootSpin = self._spin("LNMValidateBootstrap", 0, 5000, 200, _("Whole-cross-validation bootstrap resamples (confidence intervals; 200 by default, at least 20 for bias-corrected intervals)"), 50)
        options.addRow(_("Bootstrap"), self.bootSpin)
        self.permSpin = self._spin("LNMValidatePermutations", 0, 20000, 1000, _("Outcome permutations within datasets, the whole pipeline rerun (p_perm; 1000 by default)"), 100)
        options.addRow(_("Permutations"), self.permSpin)
        self.kSpin = self._spin("LNMValidateFolds", 2, 20, 10, _("Folds of the stratified k-fold scheme"), 1)
        options.addRow(_("Folds"), self.kSpin)
        self.repeatsSpin = self._spin("LNMValidateRepeats", 1, 20, 5, _("Repeats of the stratified k-fold scheme"), 1)
        options.addRow(_("Repeats"), self.repeatsSpin)
        self.seedSpin = self._spin("LNMValidateSeed", 0, 999999, 0, _("Seed of the folds, bootstrap and permutations"), 1)
        options.addRow(_("Seed"), self.seedSpin)
        self.options.hide()
        layout.addWidget(self.options)

    def _spin(self, objectName, low, high, value, toolTip, step):
        spin = qt.QSpinBox()
        spin.setObjectName(objectName)
        spin.setRange(low, high)
        spin.setSingleStep(step)
        spin.setValue(value)
        spin.setToolTip(toolTip)
        spin.connect("valueChanged(int)", lambda _value: self.onSettingsChanged())
        return spin

    def _buildRun(self, layout):
        row = qt.QHBoxLayout()
        row.addStretch(1)
        self.runButton = qt.QPushButton(_("Run validation"))
        self.runButton.setObjectName("LNMValidateRunButton")
        self.runButton.setProperty("slicerRole", "secondary")
        self.runButton.connect("clicked()", self.run)
        row.addWidget(self.runButton)
        layout.addLayout(row)
        self.reasonLabel = label("", "LNMValidateReason", "secondaryText")
        layout.addWidget(self.reasonLabel)
        self.progress = widgets.progressRow(objectNamePrefix="LNMValidate")
        self.progress.label.setWordWrap(False)
        self.progress.label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        self.progress.widget.hide()
        layout.addWidget(self.progress.widget)
        self.estimateLabel = label("", "LNMValidateEstimate", "secondaryText")
        self.estimateLabel.hide()
        layout.addWidget(self.estimateLabel)

    def _buildResults(self, layout):
        self.results = qt.QWidget()
        self.results.setObjectName("LNMValidateResults")
        box = qt.QVBoxLayout(self.results)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(6)
        self.summaryLabel = label("", "LNMValidateSummary")
        self.summaryLabel.setTextFormat(qt.Qt.RichText)
        box.addWidget(self.summaryLabel)
        self.tableWidget = widgets.tableView([_("Predictor"), "AUC", "R²", "OR", "95% CI", "p_perm"])
        self.tableWidget.setObjectName("LNMValidateTable")
        self.tableWidget.setSelectionMode(qt.QAbstractItemView.NoSelection)
        self.tableWidget.setAlternatingRowColors(False)
        self.tableWidget.setHorizontalScrollBarPolicy(qt.Qt.ScrollBarAlwaysOff)
        self.tableWidget.setVerticalScrollBarPolicy(qt.Qt.ScrollBarAlwaysOff)
        box.addWidget(self.tableWidget)
        self.tableCaption = label("", "LNMValidateTableCaption", "caption")
        box.addWidget(self.tableCaption)
        self.verdictLabel = label("", "LNMValidateVerdicts")
        self.verdictLabel.setTextFormat(qt.Qt.RichText)
        box.addWidget(self.verdictLabel)
        self.figureLabel = qt.QLabel("")
        self.figureLabel.setObjectName("LNMValidateFigure")
        self.figureLabel.setAlignment(qt.Qt.AlignCenter)
        self.figureLabel.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        self.figureLabel.setToolTip(_("Held-out ROC curves or scatter (written to the figures folder next to the result)"))
        box.addWidget(self.figureLabel)
        self.notesLabel = label("", "LNMValidateNotes")
        self.notesLabel.setTextFormat(qt.Qt.RichText)
        box.addWidget(self.notesLabel)
        buttons = qt.QHBoxLayout()
        self.folderButton = qt.QPushButton(_("Show folder"))
        self.folderButton.setObjectName("LNMValidateShowFolderButton")
        self.folderButton.setProperty("slicerRole", "ghost")
        self.folderButton.connect("clicked()", self.showFolder)
        buttons.addWidget(self.folderButton)
        buttons.addStretch(1)
        box.addLayout(buttons)
        box.addWidget(disclaimerCaption("LNMValidateDisclaimer"))
        self.results.hide()
        layout.addWidget(self.results)

    def _buildFrozen(self, layout):
        self.frozenBox = qt.QWidget()
        self.frozenBox.setObjectName("LNMValidateFrozen")
        box = qt.QVBoxLayout(self.frozenBox)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(6)
        box.addWidget(label(_("Replication with a frozen circuit"), "LNMValidateFrozenTitle"))
        self.circuitLabel = label("", "LNMValidateCircuitInfo", "secondaryText")
        box.addWidget(self.circuitLabel)
        row = qt.QHBoxLayout()
        self.frozenButton = qt.QPushButton(_("Apply frozen circuit to another study…"))
        self.frozenButton.setObjectName("LNMValidateFrozenButton")
        self.frozenButton.setProperty("slicerRole", "secondary")
        self.frozenButton.setToolTip(_("Score another study's lesions with this circuit as it is (no fitting; a simple bootstrap CI; the circuit's sha256 is recorded)"))
        self.frozenButton.connect("clicked()", self.applyFrozen)
        row.addWidget(self.frozenButton)
        row.addStretch(1)
        box.addLayout(row)
        self.frozenReason = label("", "LNMValidateFrozenReason", "secondaryText")
        box.addWidget(self.frozenReason)
        self.frozenResults = qt.QWidget()
        self.frozenResults.setObjectName("LNMValidateFrozenResults")
        results = qt.QVBoxLayout(self.frozenResults)
        results.setContentsMargins(0, 0, 0, 0)
        results.setSpacing(6)
        self.frozenSummary = label("", "LNMValidateFrozenSummary")
        self.frozenSummary.setTextFormat(qt.Qt.RichText)
        results.addWidget(self.frozenSummary)
        self.frozenTable = widgets.tableView([_("Predictor"), "AUC", "OR", "95% CI"])
        self.frozenTable.setObjectName("LNMValidateFrozenTable")
        self.frozenTable.setSelectionMode(qt.QAbstractItemView.NoSelection)
        self.frozenTable.setAlternatingRowColors(False)
        self.frozenTable.setHorizontalScrollBarPolicy(qt.Qt.ScrollBarAlwaysOff)
        self.frozenTable.setVerticalScrollBarPolicy(qt.Qt.ScrollBarAlwaysOff)
        results.addWidget(self.frozenTable)
        self.frozenVerdict = label("", "LNMValidateFrozenVerdict")
        self.frozenVerdict.setTextFormat(qt.Qt.RichText)
        results.addWidget(self.frozenVerdict)
        results.addWidget(disclaimerCaption("LNMValidateFrozenDisclaimer"))
        self.frozenResults.hide()
        box.addWidget(self.frozenResults)
        layout.addWidget(self.frozenBox)

    #
    # Places
    #

    def study(self):
        return self.ctx.study

    def card(self):
        return self.ctx.card("cohort")

    def currentMapsTsv(self):
        card = self.card()
        if card is not None and hasattr(card, "currentMapsTsv"):
            return card.currentMapsTsv()
        path = getattr(self.ctx, "cohortMaps", None)
        if path and os.path.isfile(path) and (_readJson(os.path.join(os.path.dirname(path), "cohortmaps.json")) or {}).get("status") == "done":
            return path
        return None

    def connectomeDir(self):
        """``<study>/derivatives/lnm/conn-<label>[_n-N]``: the folder of the cohort maps (and of this validation)."""
        path = self.currentMapsTsv()
        if path:
            return os.path.dirname(os.path.dirname(path))
        widget = _mapWidget()
        cohortDir = widget.cohortOutputDir() if widget is not None and hasattr(widget, "cohortOutputDir") else None
        return os.path.dirname(cohortDir) if cohortDir else None

    def participantsPath(self):
        study = self.ctx.study
        path = getattr(study, "path", None) if study is not None else None
        return os.path.join(path, "participants.tsv") if path else None

    def maskPath(self):
        bundle = self.ctx.bundle
        return os.path.join(bundle.path, "mask.nii.gz") if bundle is not None else None

    def validationRoot(self):
        connDir = self.connectomeDir()
        return os.path.join(connDir, VALIDATION_FOLDER) if connDir else None

    def mapsRunning(self):
        widget = _mapWidget()
        job = getattr(widget, "cohortJob", None) if widget is not None else None
        return job if job is not None and not job.isFinished() else None

    def running(self):
        return any(job is not None and not job.isFinished() for job in (self.job, self.mapsJob))

    #
    # Settings
    #

    def outcome(self):
        """{"column", "type" ("binary" or "continuous"), "positive"} of the outcome control, or None."""
        data = self.outcomeCombo.currentData
        if not data:
            return None
        kind, _sep, column = str(data).partition(":")
        positive = self.positiveCombo.currentData if kind == "binary" else None
        return {"column": column, "type": kind, "positive": positive or None}

    def circuit(self):
        index = self.circuitGroup.checkedId()
        return CIRCUITS[index] if 0 <= index < len(CIRCUITS) else "glm"

    def setCircuit(self, circuit):
        self._circuitTouched = True  # an explicit choice is kept when the outcome changes
        self.circuitGroup.button(CIRCUITS.index(circuit)).setChecked(True)
        self.onSettingsChanged()

    def scheme(self):
        index = self.schemeGroup.checkedId()
        return SCHEMES[index] if 0 <= index < len(SCHEMES) else "auto"

    def setScheme(self, scheme):
        self.schemeGroup.button(SCHEMES.index(scheme)).setChecked(True)
        self.onSettingsChanged()

    def datasetColumn(self):
        """"" (automatic), a column name or "none"."""
        return self.datasetCombo.currentData or ""

    def score(self):
        return self.scoreCombo.currentData or ""

    def covariateColumns(self):
        outcome = (self.outcome() or {}).get("column")
        return [name for name in self.covariates.checked() if name != outcome and name != "volume_ml"]

    def refmaps(self):
        return list(self._refmaps)

    def onSettingsChanged(self):
        self.notice = ""
        self.scheduleRefresh()

    def onCircuitClicked(self):
        self._circuitTouched = True
        self.onSettingsChanged()

    def defaultCircuit(self):
        """The in-fold circuit kind offered until the user chooses one: the kind of the current circuit, else the sLNM map for a score and the GLM t map for a group."""
        kind = (getattr(self.ctx, "currentCircuit", None) or {}).get("kind")
        if kind in ("glm", "slnm"):
            return kind
        outcome = self.outcome()
        return "slnm" if outcome is not None and outcome["type"] == "continuous" else "glm"

    def applyDefaultCircuit(self):
        if not self._circuitTouched and self.circuit() != self.defaultCircuit():
            self.circuitGroup.button(CIRCUITS.index(self.defaultCircuit())).setChecked(True)

    def onOutcomeChanged(self):
        self.applyDefaultCircuit()
        self.fillPositive()
        self.onSettingsChanged()

    def onCircuitChanged(self):
        """The current circuit changed (Cohort ▸ Symptom ▸ Use as circuit)."""
        if self._cleanedUp:
            return
        self.refresh()

    def onOptionsToggled(self, checked):
        self.options.setVisible(bool(checked))
        self.optionsButton.setArrowType(qt.Qt.DownArrow if checked else qt.Qt.RightArrow)

    def scheduleRefresh(self):
        self._timer.start()

    #
    # Study columns
    #

    def fillColumns(self):
        study = self.study()
        outcomes = []
        if study is not None:
            for column in study.group_columns():
                if column in study.columns:
                    levels = self.levelsOf(column)
                    if len(levels) == 2:
                        outcomes.append((_("{name} (binary)").format(name=column), f"binary:{column}"))
            for name, confirmed in study.score_columns():
                text = _("{name} (score)") if confirmed else _("{name} (score, role not confirmed)")
                outcomes.append((text.format(name=name), f"continuous:{name}"))
        _fill(self.outcomeCombo, outcomes)
        self.fillPositive()
        columns = study.columns if study is not None else []
        datasets = [(_("Automatic"), ""), (_("None (one cohort)"), "none")]
        from LesionNetworkMappingLib import design as designlib

        blocks = [column for column in (study.block_columns() if study is not None else []) if column in columns]
        for column in designlib.NESTING_COLUMNS:
            if column in columns and column not in blocks:
                blocks.append(column)
        _fill(self.datasetCombo, datasets + [(column, column) for column in blocks])
        covariates = [name for name in (study.covariate_columns() if study is not None else []) if name != "volume_ml"]
        self.covariates.setColumns(covariates)
        for name, button in self.covariates.buttons.items():
            button.setVisible(name != (self.outcome() or {}).get("column"))
        self.covariatesTitle.setVisible(bool(self.covariates.buttons))
        self.covariates.widget.setVisible(bool(self.covariates.buttons))

    def levelsOf(self, column):
        from LesionNetworkMappingLib.study import is_missing

        study = self.study()
        if study is None:
            return []
        return sorted({str(row.get(column)) for row in study.subjects if not is_missing(row.get(column))})

    def fillPositive(self):
        outcome = self.outcome()
        binary = outcome is not None and outcome["type"] == "binary"
        levels = self.levelsOf(outcome["column"]) if binary else []
        current = self.positiveCombo.currentData
        wanted = current if current in levels else ("case" if "case" in levels else ("1" if "1" in levels else (levels[-1] if levels else "")))
        texts = [(_("{level} (positive)").format(level=level), level) for level in levels]
        _fill(self.positiveCombo, texts, next((text for text, data in texts if data == wanted), None))
        self.positiveRow.setVisible(binary)
        label_ = self.positiveForm.labelForField(self.positiveRow)
        if label_ is not None:
            label_.setVisible(binary)

    #
    # Subjects and the scheme decision
    #

    def subjectTable(self):
        """What the job will use, as ``validate.load_data`` does: {"sids", "y", "datasets", "excluded", "dataset_column", "mapped"}.

        Restricted to the subjects with a map when the lesions are mapped, else to the lesions the cohort maps job will map.
        Raises ``ValueError`` with the reason when the settings cannot run.
        """
        from LesionNetworkMappingLib import glm, validate
        from LesionNetworkMappingLib.study import NA, is_missing

        study = self.study()
        outcome = self.outcome()
        if study is None:
            raise ValueError(_("Open a study in the Seed card (Seed: Cohort)."))
        if outcome is None:
            raise ValueError(_("The study has no group column with two levels and no score column to validate against."))
        mapsTsv = self.currentMapsTsv()
        widget = _mapWidget()
        info = getattr(widget, "cohortInfo", None) if widget is not None else None
        seeds = tuple(sorted(sid for sid, _path in (info or {}).get("lesions", []))) if not mapsTsv else ()
        participants = self.participantsPath()
        key = (outcome["column"], outcome["type"], outcome["positive"], self.datasetColumn(), tuple(self.covariateColumns()), mapsTsv, _stat(mapsTsv), seeds, _stat(participants), id(study))
        if self._tableCache[0] == key:
            value = self._tableCache[1]
            if isinstance(value, ValueError):
                raise value
            return value
        try:
            value = self._subjectTable(study, outcome, mapsTsv, seeds, glm, validate, NA, is_missing)
        except ValueError as exc:
            self._tableCache = (key, exc)
            raise
        self._tableCache = (key, value)
        return value

    def _subjectTable(self, study, outcome, mapsTsv, seeds, glm, validate, NA, is_missing):  # noqa: N803
        rows = {row["participant_id"]: row for row in study.subjects}
        mapped = glm._map_rows(mapsTsv, glm.MAP_COLUMNS["Fz"]) if mapsTsv else None
        candidates = [sid for sid in study.subject_ids if (sid in mapped if mapped is not None else (sid in seeds if seeds else True))]
        column = outcome["column"]
        codes = None
        if outcome["type"] == "binary":
            values, _positive = validate.binary_codes([rows[sid].get(column) for sid in study.subject_ids], outcome["positive"])
            codes = dict(zip(study.subject_ids, values, strict=True))
        covariates = self.covariateColumns()
        datasetColumn = self.datasetColumn()
        explicit = bool(datasetColumn)
        if datasetColumn == "none" and "none" not in study.columns:
            datasetColumn = ""
        if not explicit and "dataset" in study.columns and any(not is_missing(row.get("dataset")) for row in study.subjects):
            datasetColumn = "dataset"
        sids, y, datasets, excluded = [], [], [], {}
        for sid in candidates:
            row = rows[sid]
            value = codes[sid] if codes is not None else validate._numeric(row.get(column))
            if value is None:
                excluded.setdefault(_("missing {column}").format(column=column), []).append(sid)
                continue
            if any(validate._numeric(row.get(name)) is None for name in covariates):
                excluded.setdefault(_("missing covariate"), []).append(sid)
                continue
            label_ = None
            if datasetColumn:
                label_ = None if is_missing(row.get(datasetColumn)) else str(row.get(datasetColumn))
                if label_ is None and explicit:
                    excluded.setdefault(_("missing {column}").format(column=datasetColumn), []).append(sid)
                    continue
            sids.append(sid)
            y.append(value)
            datasets.append(label_)
        if len(sids) < 4:
            raise ValueError(_("Only {n} subject(s) have an outcome and the covariates: at least 4 are needed.").format(n=len(sids)))
        if datasetColumn and any(item is None for item in datasets):
            datasets = [NA if item is None else item for item in datasets]
        if outcome["type"] == "binary" and (sum(1 for item in y if item == 1) < 2 or sum(1 for item in y if item == 0) < 2):
            raise ValueError(_("A binary outcome needs at least 2 subjects of each class."))
        return {"sids": sids, "y": y, "datasets": datasets if datasetColumn else None, "excluded": excluded, "dataset_column": datasetColumn or None, "mapped": mapped is not None}

    def schemeDecision(self):
        """{"used", "reason", "message", "warnings", "n", "datasets"} of the chosen scheme on the subjects of the job, or None (with ``decisionError``)."""
        import numpy as np

        from LesionNetworkMappingLib import validate

        self.decisionError = None
        outcome = self.outcome()
        try:
            table = self.subjectTable()
            folds = validate.make_folds(self.scheme(), np.asarray(table["y"], dtype=float), table["datasets"], 0, outcome["type"], self.kSpin.value, self.repeatsSpin.value)
        except ValueError as exc:
            self.decisionError = str(exc)
            return None
        used = folds.describe()
        used.update({"n": len(table["sids"]), "datasets": sorted(set(table["datasets"])) if table["datasets"] else [], "excluded": table["excluded"], "dataset_column": table["dataset_column"]})
        return used

    #
    # State
    #

    def blockingSettings(self):
        reasons = []
        outcome = self.outcome()
        if outcome is None:
            reasons.append(_("The study has no group column with two levels and no score column to validate against."))
        if self.score() == "roi" and self.circuit() != "topk":
            reasons.append(_("ROI connectivity needs the top-x% ROI circuit."))
        if self.bootSpin.value and self.bootSpin.value < 20:
            reasons.append(_("Use at least 20 bootstrap resamples (or 0 for none): bias-corrected intervals need them."))
        return reasons

    def runState(self):
        """(enabled, reason) of Run."""
        if self.ctx.study is None:
            return False, _("Open a study in the Seed card (Seed: Cohort).")
        if self.ctx.bundle is None:
            return False, _("Choose a connectome in the Connectome card.")
        if self.running():
            return False, _("Mapping the lesions first…") if self.job is None or self.job.isFinished() else _("Validation running…")
        settings = self.blockingSettings()
        if settings:
            return False, settings[0]
        if self.decision is None:
            return False, self.decisionError or _("The settings cannot run.")
        if not self.ctx.computeEnabled():
            return False, " ".join(self.ctx.computeDisabledReasons()) or _("Not ready")
        if self.currentMapsTsv() is None:
            widget = _mapWidget()
            if self.mapsRunning() is not None:
                return True, _("The lesions are being mapped: the validation starts when they are done.")
            enabled, reason = widget.cohortComputeState() if widget is not None else (False, _("The Map module is not available."))
            if not enabled:
                return False, reason
            return True, _("Run maps the lesions first (cohort maps), then runs the validation.")
        if self.ctx.runner.isBusy():
            return True, _("Another computation is running: this one will wait for it.")
        return True, ""

    def refresh(self):
        """Show the state of the settings (called when a control changes, debounced, and when the study or connectome changes)."""
        if self._cleanedUp:
            return
        self._timer.stop()
        scope = self.connectomeDir()
        if scope != self._scope:  # another study or connectome: its own results
            self._scope = scope
            self.result, self.frozenResult = None, None
            self.results.hide()
            self.frozenResults.hide()
            self.onScopeChanged()
        self.fillColumns()
        self.applyDefaultCircuit()
        self.decision = self.schemeDecision()
        self.showScheme()
        self.showRefmapState()
        self.showState()
        self.showCircuitInfo()
        if self.result is None or self.frozenResult is None:
            self.showLatestResults()

    def onScopeChanged(self):
        if not self.running():
            self.progress.setJob(None)
            self.progress.widget.hide()
            self.estimateLabel.hide()
            self.reasonLabel.setText("")

    def showScheme(self):
        from LesionNetworkMappingLib import validate

        decision = self.decision
        if decision is None:
            self.schemeLabel.setText(f"<div>{html.escape(self.decisionError or '')}</div>" if self.decisionError else "")
            self.schemeLabel.setVisible(bool(self.decisionError))
            return
        warn = levelColor("warn")
        reason = decision.get("reason") or ""
        if self.scheme() == "auto":
            reason = _("Auto: {reason}").format(reason=("LODO, " + reason.removeprefix("LODO: ")) if reason.startswith("LODO: ") else reason)
        lines = [html.escape(reason)]
        if decision.get("message") and self.scheme() != "auto":  # LODO was asked for and is not possible
            lines[0] = f'<span style="color:{warn}">! {lines[0]}</span>'
        warnings = list(decision.get("warnings") or [])
        if decision["used"] == "loo" and validate.LOO_WARNING not in warnings:
            warnings.append(validate.LOO_WARNING)
        lines += [f'<span style="color:{warn}">! {html.escape(text)}</span>' for text in warnings]
        counts = _("{n} subjects").format(n=decision["n"])
        if decision.get("datasets"):
            counts += " · " + _("datasets: {names}").format(names=", ".join(decision["datasets"][:6]))
        excluded = decision.get("excluded") or {}
        if excluded:
            counts += " · " + _("excluded: {reasons}").format(reasons="; ".join(f"{len(members)} {reason}" for reason, members in excluded.items()))
        lines.append(html.escape(counts))
        self.schemeLabel.setText("".join(f"<div>{line}</div>" for line in lines))
        self.schemeLabel.show()

    def showState(self):
        enabled, reason = self.runState()
        self.runButton.setEnabled(enabled)
        reason = self.notice or reason
        self.reasonLabel.setText(reason)
        self.reasonLabel.setVisible(bool(reason))
        mapped = bool(self.currentMapsTsv())
        self.statusLabel.setText("" if mapped else _("The lesions are not mapped with this connectome yet: Run maps them first."))
        self.statusLabel.setVisible(not mapped)
        self.topSpin.setVisible(self.circuit() == "topk")
        self.refmapButton.setEnabled(True)
        n = self.bootSpin.value + self.permSpin.value + 1
        self.runButton.setToolTip(_("{n} cross-validations: the observed one, {boot} bootstrap and {perm} permutation reruns of the whole pipeline. The job estimates the time after the first one.").format(
            n=n, boot=self.bootSpin.value, perm=self.permSpin.value))
        frozenEnabled, frozenReason = self.frozenState()
        self.frozenButton.setEnabled(frozenEnabled)
        self.frozenReason.setText(frozenReason)
        self.frozenReason.setVisible(bool(frozenReason))

    def frozenState(self):
        if self.ctx.study is None or self.ctx.bundle is None:
            return False, _("Open a study and choose a connectome first.")
        if self.running():
            return False, _("Wait for the running job to finish.")
        if self.outcome() is None:
            return False, _("Choose the outcome first.")
        if getattr(self.ctx, "currentCircuit", None) is None:
            return True, _("No circuit is set: you will choose a circuit file. Cohort ▸ Symptom ▸ Use as circuit sets one.")
        return True, ""

    def showCircuitInfo(self):
        circuit = getattr(self.ctx, "currentCircuit", None)
        if not circuit:
            self.circuitLabel.setText(_("Current circuit: none"))
            return
        kind = {"slnm": _("sLNM partial r map"), "glm": _("GLM t map")}.get(circuit.get("kind"), _("map"))
        name = circuit.get("score") or circuit.get("outcome") or ""
        sha = str(circuit.get("sha256") or "")[:12]
        self.circuitLabel.setText(_("Current circuit: {kind}{name} · sha256 {sha}…").format(kind=kind, name=f" · {name}" if name else "", sha=sha))

    #
    # Reference maps
    #

    def refmapCandidates(self):
        """[(title, path, reason or None)] of the reference maps the library holds (L-52 registry, when present), with why one cannot be used."""
        from LesionNetworkMappingLib import nifti

        out = []
        try:
            from .normalize_controller import dataDirectory

            root = os.path.join(dataDirectory(), "ReferenceMaps")
        except Exception:
            return out
        registry = _readJson(os.path.join(root, "refmaps.json")) if os.path.isfile(os.path.join(root, "refmaps.json")) else None
        entries = []
        if isinstance(registry, dict):
            entries = registry.get("maps") or registry.get("entries") or ([registry] if "id" in registry else [])
        mask = self.maskPath()
        grid = None
        if mask and os.path.isfile(mask):
            try:
                header = nifti.read_nifti_header(mask)
                grid = (tuple(header["shape"][:3]), header["affine"])
            except Exception:
                grid = None
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("title") or entry.get("id") or "")
            files = [entry.get("file"), entry.get("path"), f"{entry.get('id')}.nii.gz"]
            path = next((os.path.join(root, str(item)) for item in files if item and os.path.isfile(os.path.join(root, str(item)))), None)
            if not path:
                continue
            reason = None
            if grid is not None:
                try:
                    header = nifti.read_nifti_header(path)
                    import numpy as np

                    if tuple(header["shape"][:3]) != grid[0] or not np.allclose(header["affine"], grid[1], atol=1e-4, rtol=0):
                        reason = _("not on the connectome grid (resample it first)")
                except Exception as exc:
                    reason = str(exc)
            out.append((name, path, reason))
        return out

    def showRefmaps(self):
        if self.refmapPopover is not None:
            self.refmapPopover.close()
        popover = qt.QFrame(self.widget, qt.Qt.Popup)
        popover.setObjectName("LNMValidateRefmapsPopover")
        popover.setFrameShape(qt.QFrame.StyledPanel)
        popover.setMinimumWidth(300)
        layout = qt.QVBoxLayout(popover)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.addWidget(label(_("Reference maps"), "LNMValidateRefmapsTitle"))
        layout.addWidget(label(_("Maps of other conditions, resampled to the connectome grid. Each becomes a baseline scored like the circuit, in the same folds."), "LNMValidateRefmapsHelp", "caption"))
        self.refmapList = qt.QListWidget()
        self.refmapList.setObjectName("LNMValidateRefmapsList")
        candidates = self.refmapCandidates()
        known = {os.path.abspath(path) for _title, path, _reason in candidates}
        entries = candidates + [(os.path.basename(path), path, None) for path in self._refmaps if os.path.abspath(path) not in known]
        for title, path, reason in entries:
            item = qt.QListWidgetItem(title if not reason else f"{title} · {reason}")
            item.setData(qt.Qt.UserRole, path)
            item.setToolTip(os.path.basename(path))
            flags = qt.Qt.ItemIsUserCheckable | (qt.Qt.ItemIsEnabled if not reason else qt.Qt.NoItemFlags)
            item.setFlags(flags)
            item.setCheckState(qt.Qt.Checked if path in self._refmaps else qt.Qt.Unchecked)
            self.refmapList.addItem(item)
        self.refmapList.setMaximumHeight(150)
        self.refmapList.connect("itemChanged(QListWidgetItem*)", lambda item: self._readRefmaps())
        self.refmapList.setVisible(bool(entries))
        layout.addWidget(self.refmapList)
        if not entries:
            layout.addWidget(label(_("The reference-map library holds no map yet."), "LNMValidateRefmapsEmpty", "secondaryText"))
        add = qt.QPushButton(_("Add map file…"))
        add.setObjectName("LNMValidateRefmapsAdd")
        add.setProperty("slicerRole", "secondary")
        add.connect("clicked()", self.addRefmapFile)
        layout.addWidget(add)
        self.refmapPopover = popover
        popover.adjustSize()
        popover.move(self.refmapButton.mapToGlobal(qt.QPoint(0, self.refmapButton.height)))
        popover.show()
        return popover

    def _readRefmaps(self):
        chosen = []
        for row in range(self.refmapList.count):
            item = self.refmapList.item(row)
            if item.checkState() == qt.Qt.Checked:
                chosen.append(str(item.data(qt.Qt.UserRole)))
        self.setRefmaps(chosen)

    def addRefmapFile(self):
        path = qt.QFileDialog.getOpenFileName(self.widget, _("Reference map"), "", "NIfTI (*.nii *.nii.gz)")
        if path:
            self.setRefmaps(self.refmaps() + [path] if path not in self._refmaps else self.refmaps())
            if self.refmapPopover is not None:
                self.refmapPopover.close()
            self.showRefmaps()

    def setRefmaps(self, paths):
        """Choose the reference map files (NIfTI on the connectome grid) used as extra baselines."""
        self._refmaps = [os.path.abspath(path) for path in dict.fromkeys(paths)]
        self.showRefmapState()
        self.showState()

    def showRefmapState(self):
        names = [os.path.basename(path) for path in self._refmaps]
        self.refmapLabel.setText(_("{n} reference map(s): {names}").format(n=len(names), names=", ".join(names[:3]) + (" …" if len(names) > 3 else "")) if names else _("No reference maps (volume, μC, PC1 and lesion location are always included)."))

    #
    # Run
    #

    def run(self):
        """Run the validation (after the cohort maps job when the lesions are not mapped); return the job, or None."""
        if self.running():
            return None
        self.notice = ""
        self.decision = self.schemeDecision()
        if not self.runState()[0]:
            self.showScheme()
            self.showState()
            return None
        mapsTsv = self.currentMapsTsv()
        if mapsTsv is None:
            job = self.mapsRunning()
            if job is None:
                card = self.card()
                job = card.mapLesions() if card is not None else _mapWidget().computeCohort()
            if job is None:
                self.showState()
                return None
            self.mapsJob = job
            self.progress.setJob(job)
            self.progress.widget.show()
            self.showState()
            return job
        return self._start(mapsTsv)

    def outputFolder(self):
        root = self.validationRoot()
        outcome = self.outcome()
        if root is None or outcome is None:
            return None
        return os.path.join(root, f"{_safeName(outcome['column'])}_{self.circuit()}")

    def jobSpec(self, mapsTsv):
        from . import jobrunner

        folder = self.outputFolder()
        outcome = self.outcome()
        if folder is None or outcome is None:
            raise ValueError(_("No output folder (study, connectome or outcome missing)"))
        os.makedirs(folder, exist_ok=True)
        args = ["--maps", mapsTsv, "--participants", self.participantsPath(), "--outcome", outcome["column"], "--type", outcome["type"], "--circuit", self.circuit(), "--scheme", self.scheme(),
                "--covariates", ",".join(self.covariateColumns()), "--boot", str(self.bootSpin.value), "--perm", str(self.permSpin.value), "--seed", str(self.seedSpin.value),
                "--k", str(self.kSpin.value), "--repeats", str(self.repeatsSpin.value), "--mask", self.maskPath(), "--out", folder, "--run-root", self.ctx.study.path]
        if outcome["positive"]:
            args += ["--positive", outcome["positive"]]
        if self.circuit() == "topk":
            args += ["--top-pct", str(self.topSpin.value)]
        if self.score():
            args += ["--score", self.score()]
        if self.datasetColumn():
            args += ["--dataset", self.datasetColumn()]
        if self.ctx.bundle is not None:
            args += ["--bundle", self.ctx.bundle.path]
        if self._refmaps:
            args += ["--refmaps", ",".join(self._refmaps)]
        self._jobFolder = folder
        return jobrunner.JobSpec("LesionNetworkMappingLib.validate", args, title=_("Validation"), heavy=True, kind="validation", runRoot=os.path.dirname(folder), onEvent=self.onEvent, onDone=self.onDone,
                                 name="validate", statusText=_statusText)

    def _start(self, mapsTsv):
        from . import jobrunner

        try:
            spec = self.jobSpec(mapsTsv)
        except Exception as exc:
            logging.exception("Lesion network mapping: the validation job could not be prepared")
            self.reasonLabel.setText(_("The validation cannot start: {error}").format(error=exc))
            self.reasonLabel.show()
            return None
        if not isinstance(spec, jobrunner.JobSpec):
            return None
        self.jobFolder = self._jobFolder
        self.jobKind = "cv"
        self.estimateLabel.hide()
        self.job = self.ctx.submit(spec)
        self.progress.setJob(self.job)
        self.progress.widget.show()
        self.showState()
        return self.job

    def _onJobFinished(self, job):
        if self._cleanedUp:
            return
        if job is self.mapsJob:
            self.mapsJob = None
            self._updateProgress(force=True)
            self._tableCache = (None, None)
            if job.succeeded() and self.currentMapsTsv() is not None:
                self.decision = self.schemeDecision()
                enabled, _reason = self.runState()
                if enabled:
                    self._start(self.currentMapsTsv())
                    return
            self.showState()
        elif getattr(job, "kind", None) == "cohortmaps":
            self._tableCache = (None, None)
            if self.widget.isVisible():
                self.scheduleRefresh()

    def _updateProgress(self, force=False):
        try:
            from LesionNetworkMapping import updateProgressQuietly
        except ImportError:
            self.progress.update()
            return
        updateProgressQuietly(self.progress, force)

    def onEvent(self, event):
        if event.get("event") == "stage" and event.get("estimate"):
            self.estimateLabel.setText(_("Estimate: {estimate}").format(estimate=event["estimate"]))
            self.estimateLabel.show()
        self._updateProgress(force=event.get("event") != "progress")

    def onDone(self, job):
        self._updateProgress(force=True)
        title = _("Frozen circuit") if self.jobKind == "frozen" else _("Validation")
        if job.state == "failed":
            self.notice = _("{title} failed: {reason}").format(title=title, reason=job.failureReason)
        elif job.state == "cancelled":
            self.notice = _("{title} stopped: a job that did not finish has no results.").format(title=title)
        else:
            self.notice = ""
        self.estimateLabel.hide()
        self.showState()
        if job.succeeded():
            folder = self.jobFolder
            if self.jobKind == "frozen":
                self.loadFrozen(folder)
            else:
                record = self.loadResult(folder)
                if record is not None:
                    self.recordBaselines(record)
            self.message(_("{title} done: the results are in the Validate card").format(title=title))

    def message(self, text, ms=3000):
        """A consequence message: a toast in Studio, the reason line in Classic."""
        shell = self.ctx.shell()
        if shell is not None:
            shell.toast(text, ms)
        else:
            self.reasonLabel.setText(text)
            self.reasonLabel.show()

    #
    # Results
    #

    def resultFolders(self, frozen=False):
        root = self.validationRoot()
        if not root or not os.path.isdir(root):
            return []
        folders = [os.path.join(root, name) for name in sorted(os.listdir(root)) if os.path.isfile(os.path.join(root, name, "validation.json"))]
        return [folder for folder in folders if os.path.basename(folder).startswith("frozen_") == frozen]

    def showLatestResults(self):
        """The newest finished result of this cohort's connectome folder, so that the card shows it again after a restart."""
        for frozen in (False, True):
            if (self.frozenResult if frozen else self.result) is not None:
                continue
            best = None
            for folder in self.resultFolders(frozen):
                record = _readJson(os.path.join(folder, "validation.json"))
                if record is not None and record.get("status") == "done":
                    stamp = _stat(os.path.join(folder, "validation.json"))
                    if best is None or stamp > best[0]:
                        best = (stamp, folder)
            if best is not None:
                (self.loadFrozen if frozen else self.loadResult)(best[1])

    def loadResult(self, folder):
        """Show the validation.json of ``folder``; return its record (None when it is not a finished cross-validation)."""
        record = _readJson(os.path.join(folder, "validation.json"))
        if record is None or record.get("status") != "done" or record.get("mode") != "cv":
            return None
        self.result = (folder, record)
        self.showResult(folder, record)
        self.results.show()
        return record

    def recordBaselines(self, record):
        """Add the names of the computed baselines to the run summary in the registry.

        The reporting checklist (item H11, "validation without baselines recorded") reads ``summary["baselines"]``, which the
        validate command line does not write; later registry updates win field by field, so this makes the checklist and the
        battery strip report what the table shows.
        """
        from LesionNetworkMappingLib import runs

        study, runId = self.study(), record.get("run_id")
        if study is None or not getattr(study, "path", None) or not runId:
            return
        names = [item["name"] for item in record.get("predictors") or [] if item.get("role") == "baseline" and item.get("status") == "computed"]
        try:
            known = runs.load(study.path).get(runId)
            if known is None:
                return
            summary = dict(runs.read_summary(study.path, known) or {})
            if summary.get("baselines") != names:
                summary["baselines"] = names
                runs.update(study.path, runId, summary=summary)
        except Exception:
            logging.exception("Lesion network mapping: the baselines could not be added to the validation run summary")

    def tableRows(self, record):
        """The rows of the baselines table: [(name, tooltip, metrics dict or None, reason, role)] plus the incremental row data."""
        rows = []
        for item in record.get("predictors") or []:
            name = item.get("name", "")
            short = ROW_NAMES.get(name) or (_("Reference: {name}").format(name=name[4:]) if name.startswith("ref:") else name)
            rows.append((short, item.get("label") or short, item.get("metrics") or {}, item.get("reason") if item.get("status") != "computed" else None, item.get("role"), name))
        return rows

    def showResult(self, folder, record):
        binary = record.get("outcome_type") == "binary"
        primary = record.get("primary_metric") or ("auc" if binary else "r2")
        table = self.tableWidget
        table.setHorizontalHeaderLabels(["Predictor", "AUC" if binary else "r", "R²", "OR", "95% CI", "p_perm"])
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, qt.QHeaderView.Stretch)
        for column in range(1, 6):
            header.setSectionResizeMode(column, qt.QHeaderView.ResizeToContents)
        table.setColumnHidden(2, binary)  # R² is a continuous outcome's metric, the odds ratio a binary one's
        table.setColumnHidden(3, not binary)
        floor = (record.get("permutation") or {}).get("min_attainable_p")
        rows = self.tableRows(record)
        incremental = record.get("incremental") or {}
        count = len(rows) + 1
        table.clearContents()
        table.setRowCount(count)
        firstMetric = "auc" if binary else "r"
        for index, (short, tip, metrics, reason, role, name) in enumerate(rows):
            if reason is not None:  # a baseline that could not be computed stays in the table, with the reason
                table.setItem(index, 0, _tableItem(short, f"{tip}\n{_('Not computed')}: {reason}", right=False, muted=True))
                for column in (1, 2, 3, 5):
                    table.setItem(index, column, _tableItem("—", reason, muted=True))
                table.setItem(index, 4, _tableItem(_("not computed"), reason, muted=True))
                continue
            heavy = role == "circuit"
            first, r2, odds = metrics.get(firstMetric) or {}, metrics.get("r2") or {}, metrics.get("or_per_sd") or {}
            main = metrics.get(primary) or {}
            table.setItem(index, 0, _tableItem(short, tip, right=False, bold=heavy))
            table.setItem(index, 1, _tableItem(_num(first.get("value")), self.metricTip(first, firstMetric), bold=heavy))
            table.setItem(index, 2, _tableItem(_num(r2.get("value")), self.metricTip(r2, "r2"), bold=heavy))
            table.setItem(index, 3, _tableItem(_ratio(odds.get("value")), self.metricTip(odds, "or_per_sd")))
            table.setItem(index, 4, _tableItem(_interval(main.get("ci")), self.metricTip(main, primary), bold=heavy))
            table.setItem(index, 5, _tableItem(_pText(main.get("p_perm"), floor), self.metricTip(main, primary)))
        last = len(rows)
        symbol = "ΔAUC" if primary == "auc" else "ΔR²"
        text = incremental.get("text") or ""
        delta = f"Δ {_num(incremental.get('delta'))}"
        table.setItem(last, 0, _tableItem(_("Δ vs volume alone"), f"{symbol}: " + _("volume + circuit minus volume alone") + (f"\n{text}" if text else ""), right=False, bold=True))
        table.setItem(last, 1, _tableItem(delta if primary == "auc" else "—", text, bold=True))
        table.setItem(last, 2, _tableItem(delta if primary != "auc" else "—", text, bold=True))
        table.setItem(last, 3, _tableItem("—", text))
        table.setItem(last, 4, _tableItem(_interval(incremental.get("ci")), text, bold=True))
        table.setItem(last, 5, _tableItem(_pText(incremental.get("p_perm"), floor), text, bold=True))
        table.show()
        _fitTable(table, count)
        metricName = "AUC" if primary == "auc" else "R²"
        self.tableCaption.setText(_("95% CI (whole-cross-validation bootstrap, {boot} resamples) and p_perm ({perm} permutations within datasets, one-sided) are of the {metric}. "
                                    "The last row is the difference of the {metric}: volume + circuit minus volume alone.").format(
            boot=(record.get("bootstrap") or {}).get("B", "?"), perm=(record.get("permutation") or {}).get("K", "?"), metric=metricName))
        self.showSummary(record)
        verdicts = [text for text in record.get("verdicts") or [] if text]
        self.verdictLabel.setText("".join(f"<div>· {html.escape(text[0].upper() + text[1:])}</div>" for text in verdicts))
        self.verdictLabel.setVisible(bool(verdicts))
        self.showNotes(record)
        self.showFigure(folder, record)

    @staticmethod
    def metricTip(metric, name):
        if not metric or metric.get("value") is None:
            return ""
        label_ = {"auc": "AUC", "or_per_sd": "OR per SD", "r": "r", "r2": "R²"}.get(name, name)
        tip = f"{label_} {_num(metric.get('value'), 3)} · 95% CI {_interval(metric.get('ci'), 3)}"
        if metric.get("p_perm") is not None:
            tip += f" · p_perm {_pText(metric.get('p_perm'))}"
        if metric.get("ci_method"):
            tip += f" · {metric['ci_method']} interval"
        return tip

    def showSummary(self, record):
        scheme = record.get("scheme") or {}
        outcome = record.get("outcome") or {}
        circuit = record.get("circuit") or {}
        n = outcome.get("n", "?")
        if outcome.get("type") == "binary":
            sample = _("n = {n} ({pos} {positive}, {neg} other)").format(n=n, pos=outcome.get("n_positive"), positive=outcome.get("positive"), neg=outcome.get("n_negative"))
        else:
            sample = _("n = {n}").format(n=n)
        lines = [
            "<b>" + html.escape(_("{outcome} · {circuit} · {sample}").format(outcome=outcome.get("column", ""), circuit=circuit.get("label", ""), sample=sample)) + "</b>",
            html.escape(_("Scheme: {reason}").format(reason=scheme.get("reason") or scheme.get("used") or "")),
        ]
        thresholding = circuit.get("thresholding")
        if thresholding:
            lines.append(html.escape(_("Circuit thresholding inside folds: {text}").format(text=thresholding)))
        timings = record.get("timings") or {}
        if timings.get("total_s"):
            lines.append(html.escape(_("Took {time}").format(time=duration(timings["total_s"]))))
        self.summaryLabel.setText("".join(f"<div>{line}</div>" for line in lines))

    def showNotes(self, record):
        warn = levelColor("warn")
        notes = []
        for text in record.get("warnings") or []:
            notes.append(f'<div style="color:{warn}">! {html.escape(text)}</div>')
        if record.get("model"):
            notes.append(f"<div>{html.escape(record['model'])}</div>")
        bootstrap = record.get("bootstrap") or {}
        if bootstrap.get("interval"):
            notes.append(f"<div>{html.escape(_('Intervals: {text}').format(text=str(bootstrap['interval']).split(';')[0]))}</div>")
        self.notesLabel.setText("".join(notes))
        self.notesLabel.setVisible(bool(notes))

    def showFigure(self, folder, record):
        """Draw the ROC curves or scatter and show them, in later event-loop passes (the table is painted first), then write them next to the result."""
        self.figureFile = None
        self.figureLabel.clear()
        self.figureLabel.setVisible(False)
        self._figureToken = object()
        token = self._figureToken
        qt.QTimer.singleShot(0, lambda: self._drawFigure(folder, record, token))

    def _drawFigure(self, folder, record, token):
        if self._cleanedUp or token is not self._figureToken:
            return
        scoresPath = os.path.join(folder, "scores.tsv")
        if not os.path.isfile(scoresPath):
            return
        try:
            figure = validationFigure(record, scoresPath)
        except Exception:
            logging.exception("Lesion network mapping: the validation figure could not be drawn")
            return
        if figure is None:
            return
        qt.QTimer.singleShot(0, lambda: self._showFigureImage(folder, record, figure, token))

    def _showFigureImage(self, folder, record, figure, token):
        if self._cleanedUp or token is not self._figureToken:
            return
        pixmap = _pixmap(figure.image())
        self.figureLabel.setPixmap(pixmap.scaledToWidth(FIGURE_WIDTH_PX, qt.Qt.SmoothTransformation))
        self.figureLabel.show()
        qt.QTimer.singleShot(0, lambda: self._writeFigure(folder, record, figure, token))

    def _writeFigure(self, folder, record, figure, token):
        if not self._cleanedUp and token is self._figureToken:
            self.writeFigure(folder, record, figure)

    def writeFigure(self, folder, record, figure):
        """``figures/validation`` next to the result (PNG, SVG, figure.json, caption, source data), registered as a figure run; written again only when scores.tsv changed."""
        from LesionNetworkMappingLib import figures, runs

        target = os.path.join(folder, "figures", FIGURE_NAME)
        png = os.path.join(target, FIGURE_NAME + ".png")
        scoresStamp = _stat(os.path.join(folder, "scores.tsv"))
        if os.path.isfile(png) and scoresStamp and os.stat(png).st_mtime_ns >= scoresStamp[0]:
            self.figureFile = png
            return png
        try:
            written = figures.write_figure(figure, folder, FIGURE_NAME, formats=("png", "svg"))
        except Exception:
            logging.exception("Lesion network mapping: the validation figure could not be written")
            return None
        self.figureFile = os.path.join(written["dir"], FIGURE_NAME + ".png")
        study = self.study()
        if study is not None and getattr(study, "path", None):
            try:
                runs.register(study.path, "figure", written["dir"], parents=[record.get("run_id")] if record.get("run_id") else (), params={"preset": figure.meta.get("preset"), "source": "validation"},
                              status="done", summary={"files": written["files"]})
            except Exception:
                logging.exception("Lesion network mapping: the validation figure could not be registered")
        return self.figureFile

    def showFolder(self):
        folder = self.result[0] if self.result else (self.frozenResult[0] if self.frozenResult else None)
        if folder and os.path.isdir(folder) and not slicer.app.testingEnabled():
            qt.QDesktopServices.openUrl(qt.QUrl.fromLocalFile(folder))

    #
    # Frozen circuit
    #

    def circuitSource(self, circuitPath=None):
        """{"path", "sha256", ...} of the circuit to freeze: ``circuitPath`` (its sha256 is computed), the current circuit, or a chosen file; None when cancelled."""
        from LesionNetworkMappingLib import export

        if circuitPath:
            return {"path": os.path.abspath(circuitPath), "sha256": export.file_sha256(circuitPath), "kind": "file"}
        current = getattr(self.ctx, "currentCircuit", None)
        if current and current.get("path") and os.path.isfile(current["path"]):
            return dict(current)
        path = qt.QFileDialog.getOpenFileName(self.widget, _("Circuit map to freeze"), "", "NIfTI (*.nii *.nii.gz)")
        if not path:
            return None
        return {"path": os.path.abspath(path), "sha256": export.file_sha256(path), "kind": "file"}

    def resolveOtherStudy(self, folder):
        """(participants.tsv, maps.tsv, study folder) of another study mapped with this connectome; raises ``ValueError`` with the reason."""
        from LesionNetworkMappingLib import export

        folder = os.path.abspath(folder)
        if os.path.isfile(os.path.join(folder, "maps.tsv")):  # a cohort folder
            root = folder
            for _step in range(5):
                root = os.path.dirname(root)
                if os.path.isfile(os.path.join(root, "study.json")):
                    break
            else:
                raise ValueError(_("The folder holds maps.tsv but no study around it (no study.json)."))
            return os.path.join(root, "participants.tsv"), os.path.join(folder, "maps.tsv"), root
        if not os.path.isfile(os.path.join(folder, "study.json")):
            raise ValueError(_("This folder is not a study (it has no study.json)."))
        participants = os.path.join(folder, "participants.tsv")
        if not os.path.isfile(participants):
            raise ValueError(_("The study has no participants.tsv."))
        mapsTsv = os.path.join(export.derivatives_dir(folder, self.ctx.bundle.name), "cohort", "maps.tsv")
        if not os.path.isfile(mapsTsv):
            raise ValueError(_("The study's lesions are not mapped with this connectome yet. Open it in the Seed card and map its lesions first."))
        return participants, mapsTsv, folder

    def frozenFolder(self, studyRoot, circuitSha):
        root = self.validationRoot()
        outcome = self.outcome()
        if root is None or outcome is None:
            return None
        return os.path.join(root, f"frozen_{_safeName(outcome['column'])}_{_safeName(os.path.basename(studyRoot))}_{circuitSha[:8]}")

    def applyFrozen(self, circuitPath=None, studyFolder=None):
        """Apply the current circuit (or ``circuitPath``) to another study's maps (a folder picker unless ``studyFolder`` is given); return the job or None."""
        from LesionNetworkMappingLib import validate

        from . import jobrunner

        if self.running():
            return None
        outcome = self.outcome()
        if outcome is None or self.ctx.bundle is None or self.ctx.study is None:
            return None
        self.frozenReason.setText("")
        circuit = self.circuitSource(circuitPath)
        if circuit is None:
            return None
        if not studyFolder:
            studyFolder = qt.QFileDialog.getExistingDirectory(self.widget, _("Other study folder (mapped with this connectome)"), os.path.dirname(getattr(self.ctx.study, "path", "") or ""))
        if not studyFolder:
            return None
        try:
            participants, mapsTsv, root = self.resolveOtherStudy(studyFolder)
            if os.path.abspath(root) == os.path.abspath(self.ctx.study.path):
                raise ValueError(_("That is the open study: choose another study for a replication."))
            from LesionNetworkMappingLib import glm

            other, _inStudy = glm._load_study(participants)
            if outcome["column"] not in other.columns:
                raise ValueError(_("The other study has no column {name}.").format(name=outcome["column"]))
        except ValueError as exc:
            self.frozenReason.setText(str(exc))
            self.frozenReason.show()
            return None
        folder = self.frozenFolder(root, circuit["sha256"])
        os.makedirs(folder, exist_ok=True)
        args = ["--frozen", circuit["path"], "--frozen-sha256", circuit["sha256"], "--maps", mapsTsv, "--participants", participants, "--outcome", outcome["column"], "--type", outcome["type"],
                "--circuit", self.circuit(), "--boot", str(validate.DEFAULT_FROZEN_BOOT), "--seed", str(self.seedSpin.value), "--bundle", self.ctx.bundle.path,
                "--mask", self.maskPath(), "--out", folder, "--run-root", self.ctx.study.path]
        if outcome["positive"]:
            args += ["--positive", outcome["positive"]]
        if self.circuit() == "topk":
            args += ["--top-pct", str(self.topSpin.value)]
        if self.score():
            args += ["--score", self.score()]
        spec = jobrunner.JobSpec("LesionNetworkMappingLib.validate", args, title=_("Frozen circuit"), heavy=True, kind="validation", runRoot=os.path.dirname(folder), onEvent=self.onEvent, onDone=self.onDone,
                                 name="validate-frozen", statusText=_frozenStatusText)
        self.jobFolder = folder
        self.jobKind = "frozen"
        self.estimateLabel.hide()
        self.job = self.ctx.submit(spec)
        self.progress.setJob(self.job)
        self.progress.widget.show()
        self.showState()
        return self.job

    def loadFrozen(self, folder):
        record = _readJson(os.path.join(folder, "validation.json"))
        if record is None or record.get("status") != "done" or record.get("mode") != "frozen":
            return None
        self.frozenResult = (folder, record)
        self.showFrozen(record)
        self.frozenResults.show()
        return record

    def showFrozen(self, record):
        binary = (record.get("outcome") or {}).get("type") == "binary"
        first = "auc" if binary else "r"
        metrics = record.get("metrics") or {}
        rows = [(_("Frozen circuit"), metrics, True), (_("Frozen circuit, partial"), record.get("partial_metrics"), False), (_("Volume"), record.get("volume_metrics"), False)]
        rows = [row for row in rows if row[1]]
        table = self.frozenTable
        header = table.horizontalHeader()
        table.setHorizontalHeaderLabels(["Predictor", "AUC" if binary else "r", "OR", "95% CI"])
        header.setSectionResizeMode(0, qt.QHeaderView.Stretch)
        for column in range(1, 4):
            header.setSectionResizeMode(column, qt.QHeaderView.ResizeToContents)
        table.clearContents()
        table.setRowCount(len(rows))
        for index, (name, values, heavy) in enumerate(rows):
            main, odds = values.get(first) or {}, values.get("or_per_sd") or {}
            tip = self.metricTip(main, first)
            table.setItem(index, 0, _tableItem(name, "", right=False, bold=heavy))
            table.setItem(index, 1, _tableItem(_num(main.get("value")), tip, bold=heavy))
            table.setItem(index, 2, _tableItem(_ratio(odds.get("value")) if binary else "—", self.metricTip(odds, "or_per_sd") if binary else ""))
            table.setItem(index, 3, _tableItem(_interval(main.get("ci")), tip, bold=heavy))
        _fitTable(table, len(rows))
        frozen = record.get("frozen") or {}
        outcome = record.get("outcome") or {}
        metric = "AUC" if binary else "r"
        circuit, volume = metrics.get(first) or {}, (record.get("volume_metrics") or {}).get(first) or {}
        bootstrap = record.get("bootstrap") or {}
        lines = [
            "<b>" + html.escape(_("Frozen circuit applied to {n} subjects · {outcome}").format(n=outcome.get("n", "?"), outcome=outcome.get("column", ""))) + "</b>",
            html.escape(_("The circuit file has sha256 {sha}, recorded in validation.json. Nothing was fitted on this cohort.").format(sha=str(frozen.get("sha256") or "")[:16] + "…")),
            html.escape(_("95% CI: simple bootstrap of the subjects within strata ({boot} resamples). There is no permutation p: no model was fitted here.").format(boot=bootstrap.get("B", "?"))),
        ]
        self.frozenSummary.setText("".join(f"<div>{line}</div>" for line in lines))
        overlap = None
        try:
            overlap = not (float(circuit["ci"][1]) < float(volume["ci"][0]) or float(volume["ci"][1]) < float(circuit["ci"][0]))
        except (KeyError, TypeError, ValueError, IndexError):
            overlap = None
        text = _("The frozen circuit gives {metric} {value} (95% CI {low} to {high}) in this cohort; lesion volume alone gives {volumeValue} (95% CI {volumeLow} to {volumeHigh}).").format(
            metric=metric, value=_num(circuit.get("value")), low=_num((circuit.get("ci") or [None, None])[0]), high=_num((circuit.get("ci") or [None, None])[1]),
            volumeValue=_num(volume.get("value")), volumeLow=_num((volume.get("ci") or [None, None])[0]), volumeHigh=_num((volume.get("ci") or [None, None])[1]))
        if overlap is not None:
            text += " " + (_("The two intervals overlap.") if overlap else _("The two intervals do not overlap."))
        self.frozenVerdict.setText(f"<div>· {html.escape(text)}</div>")
        self.frozenSummary.show()

    #
    # Card protocol (called by the Map widget)
    #

    def onStudyChanged(self):
        self._tableCache = (None, None)
        self.refresh()

    def onCohortChanged(self):
        self._tableCache = (None, None)
        self.refresh()

    def onCohortMapsChanged(self, path=None):
        self._tableCache = (None, None)
        self.refresh()

    def cleanup(self):
        self._cleanedUp = True
        self._timer.stop()
        for name in ("studyChanged", "bundleChanged", "computeEnabledChanged"):
            callbacks = getattr(self.ctx, name, [])
            if self.refresh in callbacks:
                callbacks.remove(self.refresh)
        circuits = getattr(self.ctx, "currentCircuitChanged", [])
        if self.onCircuitChanged in circuits:
            circuits.remove(self.onCircuitChanged)
        finished = getattr(self.ctx.runner, "jobFinished", [])
        if self._onJobFinished in finished:
            finished.remove(self._onJobFinished)
        for job in (self.job,):
            if job is not None and not job.isFinished():
                job.spec.onDone = None
                job.spec.onEvent = None
        if self.refmapPopover is not None:
            self.refmapPopover.close()


def _statusText(job):
    """Status chip text of a validation job: "Validation · bootstrap 20/200 · 3 min left"."""
    from . import jobrunner

    event = job.lastEvent or {}
    text = _("Validation")
    stage = (job.stage or {}).get("name")
    if stage:
        text += f" · {stage}"
    if event.get("event") == "progress" and event.get("total") and event.get("stage") in ("bootstrap", "permutations"):
        text += f" {event.get('done')}/{event.get('total')}"
    elif job.progress is not None:
        text += f" {int(job.progress * 100)}%"
    eta = jobrunner.formatEta(job.eta)
    return f"{text} · {eta}" if eta else text


def _frozenStatusText(job):
    from . import jobrunner

    text = _("Frozen circuit")
    if job.progress is not None:
        text += f" {int(job.progress * 100)}%"
    eta = jobrunner.formatEta(job.eta)
    return f"{text} · {eta}" if eta else text


CARD = {
    "id": "validate",
    "title": "Validate",
    "order": ORDER_VALIDATE,
    "modes": ("cohort",),
    "classic": True,
    "factory": ValidateCard,
}

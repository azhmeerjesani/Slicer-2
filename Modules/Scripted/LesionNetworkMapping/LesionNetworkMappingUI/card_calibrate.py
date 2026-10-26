"""Calibrate card of the Map module (order 135, Seed = Cohort; L-48, SPEC_LNM_ADDENDUM §3.4, §7.1 and §8).

"Calibrate this analysis" runs the current design of the Cohort card (the Contrast tab's GLM or the Symptom tab's sLNM,
with every setting as configured) through ``LesionNetworkMappingLib.calibrate`` as a heavy job in the job runner, with
Stop and an ETA that follows the measured costs (about 7 min at n = 200, 4 design columns, V = 228,483 for 100 null datasets x 500
permutations, measured on 4 cores; the addendum planned 25 min):

- **Empirical FWER**: the share of null datasets (labels or scores permuted within the design's exchangeability scheme;
  random lesion sets for a one-sample design) in which any voxel is FWE-significant, with its 95% CI. The chip is amber
  ("inflated") when the CI's lower bound exceeds alpha: this catches design and coding errors such as the one-sample design
  whose FWER is close to 1. BH-FDR is shown next to it.
- **Planted network**: a score planted on the lesions' connectivity to a target network (the network of one of the
  lesions, or a mask file) at three effect sizes; the power curve (any FWE voxel in the target, with CIs) and the
  recovery table (peak in the target, peak distance, spatial r with the truth, and the same after partialling μC_r
  and PC1 when the connectome has reference maps).
- Optionally the **sensitivity route** (classic overlap rule and its null-referenced version) on relocated lesion sets.

The run's folder is ``<connectome folder>/calibration/<contrast label>`` (calibration.json without absolute paths, the
target and truth maps); it is registered (kind "calibration"). The card shows the newest calibration again after a
restart and states its checklist evidence (item 8). Nothing is computed until the button is pressed. It works in
Classic and without the Studio shell. Search: "Calibrate this analysis".
"""

import itertools
import logging
import math
import os

import qt
import slicer
from slicer.i18n import tr as _

from . import ORDER_CALIBRATE, widgets
from .cohort_contrast import disclaimerCaption, duration, label

CALIBRATION_FOLDER = "calibration"
SOURCES = (("contrast", _("Contrast (GLM)")), ("symptom", _("Symptom (sLNM)")))
TARGETS = (("lesion", _("Network of a lesion")), ("mask", _("Mask file…")))
DEFAULT_K = 100
DEFAULT_INNER = 500
DEFAULT_SIMS = 20
#: Lower priority of the job (as the engine's jobs): a long calibration must not slow the application down
JOB_NICE = 10
ROW_HEIGHT = 22
TABLE_FONT_PX = 11
CHART_SIZE = (280, 120)
RECOVERY_COLUMNS = (
    ("β", _("Planted effect: the population correlation of the score with the lesions' connectivity to the target")),
    (_("Power (95% CI)"), _("Share of simulations with an FWE-significant voxel inside the target, with its 95% CI")),
    (_("Peak in"), _("Share of simulations whose peak (largest partial r) is inside the target")),
    (_("Dist. mm"), _("Median distance from the peak to the target, mm (after partialling μC_r and PC1 in the tooltip of the next column)")),
    (_("r truth"), _("Mean spatial r of the sLNM map with the truth (the noise-free map of the planted score)")),
    (_("Partial r"), _("The same after partialling μC_r and PC1 out of the map; the tooltip gives the partialled peak")),
)
INTRO = _("Runs your current design on null data, where no effect exists, to measure its real false-positive rate, and plants a score on a "
          "network to measure power and recovery with your own lesions. It runs in the background.")


def _mapWidget():
    return getattr(slicer.modules, "LesionNetworkMappingWidget", None)


def _updateProgress(progressRow, force=False):
    """The progress row at most every 100 ms, repainted later (``LesionNetworkMapping.updateProgressQuietly``)."""
    try:
        from LesionNetworkMapping import updateProgressQuietly
    except ImportError:
        progressRow.update()
        return
    updateProgressQuietly(progressRow, force)


def _readJson(path):
    from LesionNetworkMappingLib import calibrate

    try:
        return calibrate.read_record(path)
    except Exception:
        return None


def _number(value, digits=2):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{number:.{digits}f}" if math.isfinite(number) else "—"


def _rate(summary):
    if not summary or not summary.get("n"):
        return "—"
    low, high = summary.get("ci") or (None, None)
    return f"{_number(summary.get('rate'))} ({_number(low)}\u2013{_number(high)})"


def _statusText(job):
    """Status chip text of a calibration job: "Calibrate · fwer 34% · 12 min left"."""
    from . import jobrunner

    text = _("Calibrate")
    stage = (job.stage or {}).get("name")
    if stage:
        text += f" · {stage}"
    if job.progress is not None:
        text += f" {int(job.progress * 100)}%"
    eta = jobrunner.formatEta(job.eta)
    return f"{text} · {eta}" if eta else text


def powerChart(effects, width=CHART_SIZE[0], height=CHART_SIZE[1], palette=None):
    """A ``qt.QPixmap`` of the power curve: power (0-1) against β, with the 95% CI of each point and a dotted 0.8 line."""
    palette = palette or slicer.app.palette()
    text = palette.color(qt.QPalette.Active, qt.QPalette.WindowText)
    grid = palette.color(qt.QPalette.Active, qt.QPalette.Mid)
    accent = palette.color(qt.QPalette.Active, qt.QPalette.Highlight)
    ratio = 1.0
    try:
        ratio = float(slicer.app.devicePixelRatio())
    except Exception:
        pass
    pixmap = qt.QPixmap(int(width * ratio), int(height * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(qt.QColor(0, 0, 0, 0))
    painter = qt.QPainter(pixmap)
    try:
        painter.setRenderHint(qt.QPainter.Antialiasing, True)
        font = painter.font()
        font.setPixelSize(10)
        painter.setFont(font)
        left, right, top, bottom = 30, 10, 8, 24
        plotW, plotH = width - left - right, height - top - bottom
        betas = [float(effect["beta"]) for effect in effects]
        top_beta = max([0.4, *betas]) if betas else 0.4

        def x(beta):
            return left + plotW * beta / top_beta

        def y(power):
            return top + plotH * (1.0 - power)

        painter.setPen(qt.QPen(grid, 1))
        painter.drawLine(left, int(y(0)), left + plotW, int(y(0)))
        painter.drawLine(left, int(y(0)), left, int(y(1)))
        dotted = qt.QPen(grid, 1)
        dotted.setStyle(qt.Qt.DotLine)
        painter.setPen(dotted)
        painter.drawLine(left, int(y(0.8)), left + plotW, int(y(0.8)))
        painter.setPen(qt.QPen(text, 1))
        for value in (0.0, 0.5, 1.0):
            painter.drawText(qt.QRect(0, int(y(value)) - 7, left - 4, 14), qt.Qt.AlignRight | qt.Qt.AlignVCenter, f"{value:g}")
        for beta in betas:
            painter.drawText(qt.QRect(int(x(beta)) - 20, top + plotH + 3, 40, 14), qt.Qt.AlignCenter, f"{beta:g}")
        painter.drawText(qt.QRect(0, top + plotH + 9, left - 4, 14), qt.Qt.AlignRight, "β")
        pen = qt.QPen(accent, 2)
        painter.setPen(pen)
        points = []
        for effect in effects:
            power = effect.get("power") or {}
            if power.get("rate") is None:
                continue
            px, py = x(float(effect["beta"])), y(float(power["rate"]))
            low, high = power.get("ci") or (power["rate"], power["rate"])
            painter.drawLine(int(px), int(y(low)), int(px), int(y(high)))
            painter.drawLine(int(px) - 3, int(y(low)), int(px) + 3, int(y(low)))
            painter.drawLine(int(px) - 3, int(y(high)), int(px) + 3, int(y(high)))
            points.append((px, py))
        for (x0, y0), (x1, y1) in itertools.pairwise(points):
            painter.drawLine(int(x0), int(y0), int(x1), int(y1))
        painter.setBrush(qt.QBrush(accent))
        for px, py in points:
            painter.drawEllipse(qt.QPointF(px, py), 3.0, 3.0)
    finally:
        painter.end()
    return pixmap


class CalibrateCard:
    """Controller of the Calibrate card (``widget`` is a collapsible card)."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.job = None
        self.notice = ""
        self.targetPath = None
        self.result = None  # (folder, record) on show
        self._cleanedUp = False
        self.widget = widgets.card(_("Calibrate"), objectName="LNMCalibrateCard", collapsed=True)
        layout = qt.QVBoxLayout(self.widget)
        layout.setSpacing(6)
        layout.addWidget(label(INTRO, "LNMCalibrateIntro", "secondaryText"))

        form = qt.QFormLayout()
        form.setFieldGrowthPolicy(qt.QFormLayout.AllNonFixedFieldsGrow)
        self.sourceCombo = qt.QComboBox()
        self.sourceCombo.setObjectName("LNMCalibrateSourceCombo")
        for key, title in SOURCES:
            self.sourceCombo.addItem(title, key)
        self.sourceCombo.setToolTip(_("The analysis to calibrate: the current settings of the Cohort card's Contrast or Symptom tab"))
        self.sourceCombo.connect("currentIndexChanged(int)", self.refresh)
        form.addRow(_("Analysis:"), self.sourceCombo)
        self.designLabel = label("", "LNMCalibrateDesign", "secondaryText")
        form.addRow("", self.designLabel)

        self.kSpin = self._spin("LNMCalibrateKSpin", 20, 1000, DEFAULT_K, _("Null datasets: each is your design run on permuted labels or scores (100 gives a CI of about ±5 points at \u03b1 = 0.05)"))
        self.innerSpin = self._spin("LNMCalibrateInnerSpin", 100, 5000, DEFAULT_INNER, _("Permutations of each null dataset and simulation, identity included"))
        nullRow = qt.QHBoxLayout()
        nullRow.addWidget(self.kSpin)
        nullRow.addWidget(qt.QLabel("\u00d7"))
        nullRow.addWidget(self.innerSpin)
        nullRow.addStretch(1)
        form.addRow(_("Null datasets:"), nullRow)

        self.simsSpin = self._spin("LNMCalibrateSimsSpin", 5, 500, DEFAULT_SIMS, _("Simulations per effect size of the planted network"))
        self.effectsEdit = qt.QLineEdit("0.1, 0.2, 0.3")
        self.effectsEdit.setObjectName("LNMCalibrateEffectsEdit")
        self.effectsEdit.setToolTip(_("Planted effect sizes: correlations of the simulated score with the lesions' connectivity to the target (between 0 and 1)"))
        self.effectsEdit.connect("textChanged(QString)", lambda *args: self.refresh())
        plantRow = qt.QHBoxLayout()
        plantRow.addWidget(self.simsSpin)
        plantRow.addWidget(qt.QLabel(_("sims at β")))
        plantRow.addWidget(self.effectsEdit, 1)
        form.addRow(_("Planted:"), plantRow)

        targetRow = qt.QHBoxLayout()
        self.targetCombo = qt.QComboBox()
        self.targetCombo.setObjectName("LNMCalibrateTargetCombo")
        for key, title in TARGETS:
            self.targetCombo.addItem(title, key)
        self.targetCombo.setToolTip(_("The planted truth is a network: the top 5% of one lesion's Fz map (the lesion of median volume), or a mask on the connectome grid"))
        self.targetCombo.connect("activated(int)", self._onTargetChosen)
        targetRow.addWidget(self.targetCombo, 1)
        form.addRow(_("Target network:"), targetRow)
        self.targetLabel = label("", "LNMCalibrateTargetText", "secondaryText")
        form.addRow("", self.targetLabel)

        self.sensitivityCheckBox = qt.QCheckBox(_("Also the sensitivity route (relocated lesions; slower)"))
        self.sensitivityCheckBox.setObjectName("LNMCalibrateSensitivityCheckBox")
        self.sensitivityCheckBox.setToolTip(_("False-positive rate of the classic overlap rule and of its null-referenced version on relocated lesion sets"))
        form.addRow("", self.sensitivityCheckBox)
        layout.addLayout(form)

        runRow = qt.QHBoxLayout()
        self.runButton = qt.QPushButton(_("Calibrate this analysis"))
        self.runButton.setObjectName("LNMCalibrateRunButton")
        self.runButton.setProperty("slicerRole", "secondary")
        self.runButton.connect("clicked()", self.run)
        runRow.addWidget(self.runButton)
        self.etaLabel = label("", "LNMCalibrateEta", "secondaryText")
        runRow.addWidget(self.etaLabel, 1)
        layout.addLayout(runRow)
        self.stateLabel = label("", "LNMCalibrateState", "secondaryText")
        layout.addWidget(self.stateLabel)
        self.progress = widgets.progressRow(objectNamePrefix="LNMCalibrate")
        self.progress.widget.hide()
        layout.addWidget(self.progress.widget)
        self.noticeLabel = label("", "LNMCalibrateNotice")
        self.noticeLabel.hide()
        layout.addWidget(self.noticeLabel)

        # Results
        self.results = qt.QWidget()
        self.results.setObjectName("LNMCalibrateResults")
        results = qt.QVBoxLayout(self.results)
        results.setContentsMargins(0, 4, 0, 0)
        results.setSpacing(6)
        fwerRow = qt.QHBoxLayout()
        self.fwerChip = widgets.chip(_("FWER"), "neutral")
        self.fwerChip.setObjectName("LNMCalibrateFwerChip")
        fwerRow.addWidget(self.fwerChip, 0, qt.Qt.AlignTop)
        self.fwerLabel = label("", "LNMCalibrateFwerText")
        fwerRow.addWidget(self.fwerLabel, 1)
        results.addLayout(fwerRow)
        self.fdrLabel = label("", "LNMCalibrateFdrText", "secondaryText")
        results.addWidget(self.fdrLabel)
        self.sensitivityLabel = label("", "LNMCalibrateSensitivityText", "secondaryText")
        results.addWidget(self.sensitivityLabel)
        self.plantedTitle = self._title(_("Planted network: power and recovery"))
        results.addWidget(self.plantedTitle)
        self.plantedLabel = label("", "LNMCalibratePlantedText", "secondaryText")
        results.addWidget(self.plantedLabel)
        self.chart = qt.QLabel()
        self.chart.setObjectName("LNMCalibratePowerChart")
        self.chart.setToolTip(_("Power (any FWE voxel inside the target) against the planted effect β, with 95% CIs; the dotted line is 0.8"))
        results.addWidget(self.chart)
        self.table = widgets.tableView([title for title, _tip in RECOVERY_COLUMNS])
        self.table.setObjectName("LNMCalibrateRecoveryTable")
        for column, (_title, tip) in enumerate(RECOVERY_COLUMNS):
            item = self.table.horizontalHeaderItem(column)
            if item is not None:
                item.setToolTip(tip)
        self.table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(qt.QHeaderView.Stretch)
        header.setSectionResizeMode(1, qt.QHeaderView.ResizeToContents)  # the CI is never cut
        widgets._font(self.table, TABLE_FONT_PX)
        results.addWidget(self.table)
        self.evidenceLabel = label("", "LNMCalibrateEvidence", "secondaryText")
        results.addWidget(self.evidenceLabel)
        buttons = qt.QHBoxLayout()
        self.folderButton = qt.QPushButton(_("Show folder"))
        self.folderButton.setObjectName("LNMCalibrateFolderButton")
        self.folderButton.setProperty("slicerRole", "ghost")
        self.folderButton.connect("clicked()", self.showFolder)
        buttons.addWidget(self.folderButton)
        buttons.addStretch(1)
        results.addLayout(buttons)
        self.results.hide()
        layout.addWidget(self.results)
        layout.addWidget(disclaimerCaption("LNMCalibrateDisclaimer"))

        try:
            self.widget.connect("contentsCollapsed(bool)", self._onCollapsed)
        except Exception:
            logging.debug("Lesion network mapping: the Calibrate card cannot follow its expansion", exc_info=True)
        for name in ("bundleChanged", "studyChanged"):
            getattr(ctx, name).append(self.refresh)
        self.refresh()

    @staticmethod
    def _spin(objectName, low, high, value, tip):
        spin = qt.QSpinBox()
        spin.setObjectName(objectName)
        spin.setRange(low, high)
        spin.setValue(value)
        spin.setToolTip(tip)
        return spin

    @staticmethod
    def _title(text):
        title = qt.QLabel(text)
        title.setProperty("slicerRole", "secondaryText")
        widgets._font(title, 12, bold=True)
        return title

    #
    # Card protocol
    #

    def onRunDone(self, job=None):
        self.refresh()

    def onCohortMapsChanged(self, path=None):
        self.refresh()

    def onStudyChanged(self):
        self.result = None
        self.refresh()

    def cleanup(self):
        self._cleanedUp = True
        for name in ("bundleChanged", "studyChanged"):
            callbacks = getattr(self.ctx, name, [])
            if self.refresh in callbacks:
                callbacks.remove(self.refresh)
        if self.job is not None and hasattr(self.job, "isFinished") and not self.job.isFinished():
            self.job.spec.onDone = None
            self.job.spec.onEvent = None
        self.job = None

    def _onCollapsed(self, collapsed):
        if not collapsed:
            self.refresh()

    def expanded(self):
        try:
            return not bool(self.widget.collapsed)
        except (ValueError, RuntimeError):
            return False

    #
    # The analysis to calibrate
    #

    def source(self):
        return self.sourceCombo.currentData or "contrast"

    def tab(self, key=None):
        card = self.ctx.card("cohort")
        return card.tab(key or self.source()) if card is not None and hasattr(card, "tab") else None

    def currentSpec(self):
        """(design.DesignSpec, design.Design or None, error text or None) of the chosen tab's current settings."""
        tab = self.tab()
        if tab is None:
            return None, None, _("Open a study in the Seed card (Seed: Cohort); the analysis is set up in the Cohort card.")
        try:
            design = tab.design if getattr(tab, "design", None) is not None else tab.rebuildDesign()
        except Exception as exc:
            logging.exception("Lesion network mapping: the design to calibrate could not be built")
            return None, None, str(exc)
        if design is None:
            return None, None, getattr(tab, "designError", None) or _("No design.")
        if self.source() == "symptom":
            return design.spec, design, None
        try:
            return tab.spec(), design, None
        except Exception as exc:
            return None, design, str(exc)

    def mapsTsv(self):
        tab = self.tab()
        try:
            return tab.currentMapsTsv() if tab is not None else None
        except Exception:
            return None

    def connectomeDir(self):
        tab = self.tab()
        try:
            return tab.connectomeDir() if tab is not None else None
        except Exception:
            return None

    def folder(self, design=None):
        from LesionNetworkMappingLib import glm

        connDir = self.connectomeDir()
        if not connDir:
            return None
        name = glm.contrast_label(design) if design is not None else "analysis"
        return os.path.join(connDir, CALIBRATION_FOLDER, name)

    def effects(self):
        """The planted effect sizes of the text field, or None when the text is not a list of numbers in (0, 1)."""
        try:
            values = [float(item) for item in str(self.effectsEdit.text).replace(";", ",").split(",") if item.strip()]
        except ValueError:
            return None
        return values if values and all(0.0 < value < 1.0 for value in values) else None

    def estimateSeconds(self, design):
        from LesionNetworkMappingLib import calibrate

        bundle = self.ctx.bundle
        n_voxels = getattr(bundle, "n_voxels", None) if bundle is not None else None
        if design is None or not n_voxels:
            return None
        effects = self.effects() or []
        return calibrate.estimate_seconds(design.n, n_voxels, design.p, int(self.kSpin.value), int(self.innerSpin.value), effects, int(self.simsSpin.value))

    def running(self):
        return self.job is not None and not self.job.isFinished()

    #
    # State
    #

    def runState(self, spec, design, error):
        if self.ctx.study is None:
            return False, _("Open a study in the Seed card (Seed: Cohort).")
        if self.ctx.bundle is None:
            return False, _("Choose a connectome in the Connectome card.")
        if self.running():
            return False, _("Calibration running…")
        if error or spec is None:
            return False, error or _("No design.")
        if design is not None and design.blocking:
            return False, _("The design is blocked by its checks (see the Cohort card).")
        if self.mapsTsv() is None:
            return False, _("Map the cohort first (Cohort card): the calibration uses your lesions' maps.")
        if self.effects() is None:
            return False, _("Effect sizes are numbers between 0 and 1, separated by commas.")
        if self.targetCombo.currentData == "mask" and not self.targetPath:
            return False, _("Choose the target mask file.")
        if self.ctx.runner.isBusy():
            return True, _("Another computation is running: the calibration will wait for it.")
        return True, ""

    def refresh(self, *args):
        """Update the labels and the enabled state (cheap; nothing is computed)."""
        if self._cleanedUp:
            return
        try:
            self.widget.objectName
        except (ValueError, RuntimeError):
            return
        spec, design, error = self.currentSpec() if self.ctx.study is not None else (None, None, None)
        if design is not None:
            covariates = ", ".join(design.spec.covariates) or _("none")
            kind = design.spec.test.replace("_", " ")
            if design.design_type != design.spec.test:
                kind += " · " + design.design_type.replace("_", " ")
            self.designLabel.setText(_("{kind} · n = {n} · covariates: {covariates} · permute: {permute}").format(kind=kind, n=design.n, covariates=covariates, permute=design.permute))
        else:
            self.designLabel.setText(error or "")
        if self.targetCombo.currentData == "mask":
            self.targetLabel.setText(os.path.basename(self.targetPath) if self.targetPath else _("No mask chosen"))
        else:
            self.targetLabel.setText(_("The top 5% of the Fz map of the lesion of median volume: a network, not a parcel."))
        enabled, reason = self.runState(spec, design, error)
        self.runButton.setEnabled(enabled)
        self.runButton.setToolTip(reason or _("Run the calibration as a background job"))
        seconds = self.estimateSeconds(design)
        self.etaLabel.setText(_("About {time}").format(time=duration(seconds)) if seconds is not None else "")
        self.stateLabel.setText(reason)
        self.stateLabel.setVisible(bool(reason))
        self.noticeLabel.setText(self.notice)
        self.noticeLabel.setVisible(bool(self.notice))
        if self.result is None and not self.running():
            self.showLatestResult()

    def _onTargetChosen(self, index=None):
        if self.targetCombo.currentData == "mask":
            path = qt.QFileDialog.getOpenFileName(slicer.util.mainWindow(), _("Target network mask"), self.connectomeDir() or "", _("NIfTI (*.nii *.nii.gz)"))
            if path:
                self.targetPath = path
            elif not self.targetPath:
                self.targetCombo.setCurrentIndex(0)
        self.refresh()

    def _setNotice(self, text):
        self.notice = text or ""
        self.noticeLabel.setText(self.notice)
        self.noticeLabel.setVisible(bool(self.notice))

    #
    # Run
    #

    def jobArgs(self, spec, design, out):
        """Command-line arguments of the calibration job (the design is saved as design.json in ``out``)."""
        tab = self.tab()
        designPath = spec.save(os.path.join(out, "design.json"))
        args = ["--maps", self.mapsTsv(), "--participants", tab.participantsPath(), "--design", designPath, "--mask", tab.maskPath(), "--out", out,
                "--k", str(int(self.kSpin.value)), "--inner-perm", str(int(self.innerSpin.value)), "--sims", str(int(self.simsSpin.value)),
                "--effects", ",".join(f"{value:g}" for value in self.effects()), "--run-root", self.ctx.study.path, "--resume"]
        bundle = self.ctx.bundle
        if bundle is not None and getattr(bundle, "path", None):
            args += ["--bundle", bundle.path]
        if self.targetCombo.currentData == "mask" and self.targetPath:
            args += ["--target", self.targetPath]
        if self.sensitivityCheckBox.checked:
            args.append("--sensitivity")
        return args

    def run(self):
        """Start the calibration job; return it (None with the reason shown when it cannot start)."""
        from . import jobrunner

        spec, design, error = self.currentSpec()
        enabled, reason = self.runState(spec, design, error)
        if not enabled:
            self._setNotice(reason)
            return None
        out = self.folder(design)
        if out is None:
            self._setNotice(_("No output folder (study or connectome missing)."))
            return None
        os.makedirs(out, exist_ok=True)
        try:
            args = self.jobArgs(spec, design, out)
        except Exception as exc:
            logging.exception("Lesion network mapping: the calibration could not be prepared")
            self._setNotice(_("The calibration could not be prepared: {error}").format(error=exc))
            return None
        spec = jobrunner.JobSpec("LesionNetworkMappingLib.calibrate", args, title=_("Calibrate"), heavy=True, kind="calibration", name="calibration", runRoot=os.path.dirname(out),
                                 onEvent=self._onEvent, onDone=lambda job, out=out: self._onDone(job, out), statusText=_statusText,
                                 nice=JOB_NICE)
        self.job = self.ctx.submit(spec)
        self.progress.setJob(self.job)
        self.progress.widget.show()
        self._setNotice("")
        self.refresh()
        return self.job

    def _onEvent(self, event):
        if not self._cleanedUp:
            _updateProgress(self.progress, force=event.get("event") in ("start", "stage", "queued"))

    def _onDone(self, job, out):
        if self._cleanedUp:
            return
        _updateProgress(self.progress, force=True)
        if job.state == "failed":
            self._setNotice(_("The calibration failed: {reason}").format(reason=job.failureReason))
        elif job.state == "cancelled":
            self._setNotice(_("Stopped. Calibrate again with the same settings to continue from where it stopped."))
        else:
            self.progress.widget.hide()
            self._setNotice("")
            self.loadResult(out)
        self.refresh()

    #
    # Results
    #

    def resultFolders(self):
        connDir = self.connectomeDir()
        root = os.path.join(connDir, CALIBRATION_FOLDER) if connDir else None
        if not root or not os.path.isdir(root):
            return []
        folders = [os.path.join(root, name) for name in os.listdir(root) if os.path.isfile(os.path.join(root, name, "calibration.json"))]
        return sorted(folders, key=lambda folder: os.path.getmtime(os.path.join(folder, "calibration.json")))

    def showLatestResult(self):
        folders = self.resultFolders()
        if folders:
            self.loadResult(folders[-1])
        else:
            self.results.hide()

    def loadResult(self, folder):
        record = _readJson(os.path.join(folder, "calibration.json"))
        if not record or record.get("status") != "done":
            self.results.hide()
            return None
        self.result = (folder, record)
        self.showResult(folder, record)
        return record

    def showResult(self, folder, record):
        from LesionNetworkMappingLib import calibrate

        fwer = record.get("fwer") or {}
        level = fwer.get("chip") or calibrate.LEVELS.get(fwer.get("level"), "neutral")
        chipText = {"inflated": _("FWER inflated"), "consistent": _("FWER ok"), "conservative": _("FWER conservative")}.get(fwer.get("level"), _("FWER"))
        widgets.setChipLevel(self.fwerChip, level if level in ("ok", "warn", "danger", "neutral", "busy") else "neutral", chipText)
        design = record.get("design") or {}
        lead = _("{label} ({test}, n = {n}): ").format(label=design.get("label") or "", test=str(design.get("test") or "").replace("_", " "), n=design.get("n"))
        if record.get("fwer") is None:
            self.fwerLabel.setText(lead + _("the empirical FWER was not computed."))
        else:
            source = fwer.get("null_source_text") or ""
            self.fwerLabel.setText(lead + (fwer.get("text") or "") + (" " + _("Null data: {source}.").format(source=source) if source else ""))
        self.fdrLabel.setText(_("BH-FDR (any q ≤ \u03b1): {rate}. FWE is the primary inference; TFCE is not calibrated here.").format(rate=calibrate.rate_text(fwer.get("fdr"))) if fwer.get("fdr") else "")
        self.fdrLabel.setVisible(bool(fwer.get("fdr")))
        sensitivity = record.get("sensitivity") or {}
        if sensitivity.get("classic"):
            text = sensitivity.get("classic_text", "") + " " + _("Null-referenced FWE: {rate}; both: {both}.").format(rate=calibrate.rate_text(sensitivity["null_fwe"]), both=calibrate.rate_text(sensitivity["conjunction"]))
        else:
            text = _("Sensitivity route not calibrated: {reason}").format(reason=sensitivity["reason"]) if sensitivity.get("reason") else ""
        self.sensitivityLabel.setText(text)
        self.sensitivityLabel.setVisible(bool(text))
        planted = record.get("planted") or {}
        effects = planted.get("effects") or []
        self.plantedTitle.setVisible(bool(planted))
        if planted.get("reason"):
            self.plantedLabel.setText(_("Not computed: {reason}").format(reason=planted["reason"]))
        elif planted:
            target = planted.get("target") or {}
            reference = planted.get("reference") or {}
            partial = _("Partialled metrics use μC_r and PC1 (lnm-reference-v1).") if planted.get("partialled") else _("Partialled metrics not computed: {reason}.").format(reason=reference.get("reason") or _("no reference maps"))
            self.plantedLabel.setText(_("Target: {target} ({n} voxels). {sims} simulations per effect, {perm} permutations each. {partial}").format(
                target=target.get("text") or target.get("source") or _("mask"), n=target.get("n_voxels"), sims=planted.get("n_sims"), perm=planted.get("inner_perm"), partial=partial))
        else:
            self.plantedLabel.setText("")
        self.plantedLabel.setVisible(bool(planted))
        if effects:
            self.chart.setPixmap(powerChart(effects))
        self.chart.setVisible(bool(effects))
        self._fillTable(effects)
        status, value, _evidence = calibrate.checklist_evidence(self.ctx.study.path) if self.ctx.study is not None else ("not_run", "", [])
        self.evidenceLabel.setText(_("Checklist (item 8, design and correction): {value}").format(value=value) if status != "not_run" else "")
        self.results.show()

    def _fillTable(self, effects):
        def median(summary):
            return _number((summary or {}).get("median"), 1)

        def mean(summary):
            return _number((summary or {}).get("mean"))

        self.table.setRowCount(0)
        self.table.setRowCount(len(effects))
        for row, effect in enumerate(effects):
            partial = effect.get("partial") or {}
            partialTip = _("After partialling μC_r and PC1: peak inside the target in {share} of the simulations, median distance {distance} mm").format(
                share=_number((partial.get("peak_in_target") or {}).get("rate")), distance=median(partial.get("peak_distance_mm"))) if partial else _("Not computed (no reference maps)")
            cells = [(f"{effect['beta']:g}", ""), (_rate(effect.get("power")), ""), (_number((effect.get("peak_in_target") or {}).get("rate")), ""),
                     (median(effect.get("peak_distance_mm")), ""), (mean(effect.get("spatial_r")), ""), (mean(partial.get("spatial_r")) if partial else "—", partialTip)]
            for column, (text, tip) in enumerate(cells):
                item = qt.QTableWidgetItem(text)
                if tip:
                    item.setToolTip(tip)
                self.table.setItem(row, column, item)
        self.table.setFixedHeight(ROW_HEIGHT * (len(effects) + 1) + 8)
        self.table.setVisible(bool(effects))

    def showFolder(self):
        if self.result is None:
            return
        qt.QDesktopServices.openUrl(qt.QUrl.fromLocalFile(self.result[0]))


#
# Search entry (W3-06 provider)
#


def showCard():
    """Show Map (the Studio workspace, else the module) scrolled to the Calibrate card; return the card or None."""
    from . import jobrunner

    shell = jobrunner._studioShell()
    if shell is not None:
        shell.switchTo("map")
    else:
        slicer.util.selectModule("LesionNetworkMapping")
    widget = _mapWidget()
    ctx = getattr(widget, "ctx", None) if widget is not None else None
    if ctx is None:
        return None
    ctx.focusCard("calibrate")
    return ctx.card("calibrate")


def paletteEntries(shell=None):
    """Search entry "Calibrate this analysis" (dict form of the provider contract, USABILITY 4)."""
    return [
        {"id": "lnm.calibrate", "title": _("Calibrate this analysis"), "category": _("Map"),
         "synonyms": ["calibration", "empirical fwer", "false positive rate", "power", "planted network", "simulation"], "callback": showCard,
         "description": _("Measure the real false-positive rate of the current design and its power to recover a planted network")},
    ]


def registerPaletteProvider():
    """Register the Search entry with the Studio palette (guarded: nothing happens without Studio)."""
    try:
        from StudioLib import palette
    except Exception:
        return False
    try:
        for existing in palette.providers():
            if getattr(existing, "__name__", None) == "paletteEntries" and getattr(existing, "__module__", None) == __name__ and existing is not paletteEntries:
                palette.unregisterProvider(existing)
        palette.registerProvider(paletteEntries)
    except Exception:
        logging.exception("Lesion network mapping: the calibration Search entry could not be registered")
        return False
    return True


registerPaletteProvider()


CARD = {
    "id": "calibrate",
    "title": "Calibrate",
    "order": ORDER_CALIBRATE,
    "modes": ("cohort",),
    "factory": CalibrateCard,
}

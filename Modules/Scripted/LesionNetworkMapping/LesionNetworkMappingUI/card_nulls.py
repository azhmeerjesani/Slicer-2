"""Nulls card of the Map module (order 80, Seed = Cohort; L-38, SPEC_LNM_ADDENDUM §3.3, §3.6, §3.12 and §7.1).

The card puts the classic overlap map next to a null-referenced version: how much of the cohort's overlap is more than random
lesions of the same size would give on this connectome, and how often the classic rule passes by chance. It runs
``LesionNetworkMappingLib.nulls`` in the job runner, with Stop, on the cohort's per-lesion maps (the ``maps.tsv`` of the Cohort card;
the lesions are mapped first when they are not).

- **Null kind**, one click each (radio buttons), every one with a one-line description and an honest status tag:
  ``rl_independent`` [established], ``rl_cohort_shift`` [proposal], ``rl_strength`` [2026, Zalesky & Cash], ``rl_overlap``
  [single preprint, experimental; fast mode only] and the control set (empirical pool) [control set].
- **Mode**: *Exact pooled* (K_pool relocations per lesion mapped exactly in one engine pass; the same rule as the Result card) or *Fast
  (approximate)* (the parcel index; rank rules only; the observed map is recomputed in fast mode). **Fast is disabled, with the reason
  on screen, until a fast index exists for the cohort's subjects and its parity gate passed on the user's own lesions**
  (``manager_fastindex.fastModeState``; "Build fast index…" opens the manager's dialog).
- **Outputs**: the Excess, P, Q and PFwe maps are shown with the "LNM Diverging" colour table in one reused volume (click a row of the
  table; the Excess map is shown when the run ends), the bias map (the share of null cohorts that pass the classic rule at each voxel)
  with a toggle, the classic rule's false-positive-rate sentence, the minimum attainable p and q, the voxel counts, the group QA
  percentile and, in fast mode, the parity gate's exclusions. Every output says whether it is exact-pooled or approximate.
- **Permanent caption** (``nulls.CAPTION``, addendum §3.6) under the card at all times.

The run's folder is ``<cohort maps folder>/nulls-<kind>-<mode>`` (nulls.json, the five maps with sidecars, no absolute path); the
Cohort card's folder is where the QA card looks for the group QA, and the card tells the QA card to look again when a run ends
(``ctx.runChanged``). The card shows the newest result of the chosen kind and mode again after a restart. It works in Classic and
without the Studio shell.
"""

import json
import logging
import os

import qt
import slicer
from slicer.i18n import tr as _

from . import ORDER_NULLS, manager_fastindex, widgets
from .card_validate import _fitTable
from .cohort_contrast import DESIGN_DELAY_MS, _idClickedSignal, duration, label

#: Null kinds in menu order: (kind, label, one-line description, status tag, chip level)
KIND_ROWS = (
    ("rl_independent", "rl_independent", _("Each lesion moved to a random place on its own: its shape is kept, the overlap between lesions is destroyed."), _("established"), "ok"),
    ("rl_cohort_shift", "rl_cohort_shift", _("The whole cohort moved together by one random shift: shapes and the overlap between lesions are kept."), _("proposal"), "warn"),
    ("rl_strength", "rl_strength", _("Each lesion moved to a place with similar mean connectivity (μC_r): its local strength is kept."), _("2026, Zalesky & Cash"), "neutral"),
    ("rl_overlap", "rl_overlap", _("Lesions placed by annealing so that their pairwise-overlap distribution matches yours. Fast mode only."), _("single preprint, experimental"), "warn"),
    ("empirical_pool", _("Control set"), _("Lesions drawn from a control set of real lesions, matched to yours in volume (empirical pool)."), _("control set"), "neutral"),
)
KIND_IDS = tuple(row[0] for row in KIND_ROWS)
KIND_DESCRIPTIONS = {row[0]: row[2] for row in KIND_ROWS}
#: Kinds that draw K_pool relocations per lesion, in exact mode and in fast mode
POOLED_KINDS = ("rl_independent", "rl_strength")
MODES = ("exact", "fast")
DEFAULT_K = 1000
MIN_K = 19  # the smallest K whose minimum attainable p is at most 0.05
MAX_K = 100000
#: The maps listed in the card, in order (the bias map has its own toggle)
OUTPUT_DESCS = ("sensNullExcess", "sensNullP", "sensNullQ", "sensNullPFwe")
BIAS_DESC = "sensNullBias"
OUTPUT_TEXT = {
    "sensNullExcess": _("Excess (percentage points)"),
    "sensNullP": _("p (one-sided)"),
    "sensNullQ": _("q (BH-FDR)"),
    "sensNullPFwe": _("FWE p (max statistic)"),
    BIAS_DESC: _("Bias map"),
}
OUTPUT_TIPS = {
    "sensNullExcess": _("The overlap of your lesions minus the mean overlap of the null cohorts, in percentage points of the lesions"),
    "sensNullP": _("One-sided permutation p of the overlap at each voxel: (1 + #{null overlap >= observed}) / (K + 1)"),
    "sensNullQ": _("Benjamini-Hochberg q of the voxelwise p over the connectome voxels"),
    "sensNullPFwe": _("Family-wise p of the standardised overlap excess (maximum statistic over the voxels)"),
    BIAS_DESC: _("The share of the null cohorts that pass the classic overlap rule at each voxel"),
}
#: Node attribute of the volume that shows the nulls maps (one volume, reused)
VOLUME_ATTRIBUTE = "LNM.NullsMapVolume"
#: Fork-only setting: the control set chosen last (a local path, so never written into a shared file)
CONTROL_SET_SETTING = "Studio/LNM/NullsControlSet"
TOP_PCT_DEFAULT = 10


def _mapWidget():
    return getattr(slicer.modules, "LesionNetworkMappingWidget", None)


def _updateProgress(progressRow, force=False):
    try:
        from LesionNetworkMapping import updateProgressQuietly
    except ImportError:
        progressRow.update()
        return
    updateProgressQuietly(progressRow, force)


def _readJson(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _pText(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{value:.3g}"


def _statusText(job):
    """Status chip text of a nulls job: "Null models · null cohorts 40% · 2 min left"."""
    from . import jobrunner

    text = _("Null models")
    stage = (job.stage or {}).get("name")
    if stage:
        text += f" · {stage}"
    if job.progress is not None:
        text += f" {int(job.progress * 100)}%"
    eta = jobrunner.formatEta(job.eta)
    return f"{text} · {eta}" if eta else text


class NullsCard:
    """Controller of the Nulls card (see the module docstring)."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.job = None
        self.mapsJob = None
        self.notice = ""
        self.kind = "rl_independent"
        self.mode = "exact"
        self.result = None  # (folder, record) of the shown run
        self.shownDesc = None
        self.lastMap = "sensNullExcess"  # the map of the table that the bias toggle returns to
        self.volumeId = None
        self.fast = manager_fastindex.FastModeState()
        self.controlSet = self._readControlSet()
        self.estimateSeconds = None
        self._scope = None
        self._cleanedUp = False
        self._updating = False
        self._kPoolValues = {"exact": 20, "fast": 100}  # K_pool of each mode (their defaults differ), kept when the mode changes
        self._kPoolMode = "exact"
        self._referenceOk = {}
        self._timer = qt.QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(DESIGN_DELAY_MS)
        self._timer.connect("timeout()", self.refresh)
        self.widget = qt.QWidget()
        self.widget.setObjectName("LNMNulls")
        layout = qt.QVBoxLayout(self.widget)
        layout.setSpacing(6)
        layout.setContentsMargins(0, 0, 0, 0)
        self.statusLabel = label("", "LNMNullsStatus", "secondaryText")
        layout.addWidget(self.statusLabel)
        layout.addWidget(label(_("How much of the overlap is more than random lesions would give on this connectome? The classic overlap map is hub-inflated; this puts it next to a null-referenced version."),
                               "LNMNullsIntro", "secondaryText"))
        self._buildKinds(layout)
        self._buildMode(layout)
        self._buildSettings(layout)
        self._buildRun(layout)
        self._buildResults(layout)
        self.captionLabel = label(self._caption(), "LNMNullsCaption", "caption")
        layout.addWidget(self.captionLabel)
        for name in ("studyChanged", "bundleChanged", "ruleChanged", "computeEnabledChanged", "runChanged"):
            getattr(ctx, name).append(self.scheduleRefresh)
        ctx.runner.jobFinished.append(self._onJobFinished)
        self.refresh()

    @staticmethod
    def _caption():
        from LesionNetworkMappingLib import nulls

        return nulls.CAPTION

    #
    # Build
    #

    def _buildKinds(self, layout):
        self.kindGroup = qt.QButtonGroup(self.widget)
        self.kindGroup.setExclusive(True)
        self.kindButtons = {}
        self.kindChips = {}
        title = label(_("Null kind"), "LNMNullsKindTitle", "secondaryText")
        layout.addWidget(title)
        for index, (kind, text, description, tag, level) in enumerate(KIND_ROWS):
            row = qt.QHBoxLayout()
            row.setSpacing(6)
            button = qt.QRadioButton(text)
            button.setObjectName(f"LNMNullsKind_{kind}")
            button.setToolTip(f"{kind}: {description}")
            self.kindGroup.addButton(button, index)
            button.connect("toggled(bool)", lambda checked, kind=kind: self.onKindToggled(kind, checked))
            row.addWidget(button)
            row.addStretch(1)
            chip = widgets.chip(tag, level)
            chip.setObjectName("LNMNullsKindTag")
            chip.setToolTip(_("Status of this null model: {tag}").format(tag=tag))
            row.addWidget(chip)
            layout.addLayout(row)
            descriptionLabel = label(description, f"LNMNullsKindDescription_{kind}", "secondaryText")
            descriptionLabel.setContentsMargins(22, 0, 0, 4)
            layout.addWidget(descriptionLabel)
            self.kindButtons[kind] = button
            self.kindChips[kind] = chip
        self.kindButtons[self.kind].setChecked(True)

    def _buildMode(self, layout):
        row = qt.QHBoxLayout()
        row.addWidget(label(_("Mode"), "LNMNullsModeTitle", "secondaryText"))
        self.modeFrame, self.modeGroup = widgets.segmented([_("Exact pooled"), _("Fast (approximate)")])
        self.modeFrame.setObjectName("LNMNullsMode")
        self.modeFrame.setToolTip(_("Exact pooled: K_pool relocations per lesion are mapped exactly (the Result card's rule). Fast: the parcel index, rank rules only, approximate."))
        self.modeGroup.connect(_idClickedSignal(self.modeGroup), self.onModeClicked)
        row.addWidget(self.modeFrame)
        row.addStretch(1)
        layout.addLayout(row)
        self.modeNote = label("", "LNMNullsModeNote", "secondaryText")
        layout.addWidget(self.modeNote)
        self.fastReason = label("", "LNMNullsFastReason", "secondaryText")
        layout.addWidget(self.fastReason)
        buttonRow = qt.QHBoxLayout()
        self.fastIndexButton = qt.QPushButton(_("Build fast index…"))
        self.fastIndexButton.setObjectName("LNMNullsFastIndexButton")
        self.fastIndexButton.setProperty("slicerRole", "secondary")
        self.fastIndexButton.setToolTip(_("Build the parcel index, then check it on your own lesions (the parity gate): fast mode is offered only after it passes"))
        self.fastIndexButton.connect("clicked()", self.openFastIndex)
        buttonRow.addWidget(self.fastIndexButton)
        buttonRow.addStretch(1)
        layout.addLayout(buttonRow)

    def _spin(self, objectName, low, high, value, toolTip, step=1):
        spin = qt.QSpinBox()
        spin.setObjectName(objectName)
        spin.setRange(low, high)
        spin.setSingleStep(step)
        spin.setValue(value)
        spin.setToolTip(toolTip)
        spin.setKeyboardTracking(False)
        spin.connect("valueChanged(int)", lambda v: self.onSettingsChanged())
        return spin

    def _buildSettings(self, layout):
        form = qt.QFormLayout()
        form.setFieldGrowthPolicy(qt.QFormLayout.AllNonFixedFieldsGrow)
        self.kSpin = self._spin("LNMNullsK", MIN_K, MAX_K, DEFAULT_K, _("Null cohorts: the permutation p has the floor 1 / (K + 1). Fast mode computes at most 10,000."), 100)
        self.kLabel = qt.QLabel(_("Null cohorts (K)"))
        form.addRow(self.kLabel, self.kSpin)
        self.kPoolSpin = self._spin("LNMNullsKPool", 1, 1000, 20, _("Relocations of each lesion that are mapped; every null cohort draws one per lesion (exact default 20, fast default 100)."), 5)
        self.kPoolSpin.connect("valueChanged(int)", self.onKPoolChanged)
        self.kPoolLabel = qt.QLabel(_("Relocations per lesion (K_pool)"))
        form.addRow(self.kPoolLabel, self.kPoolSpin)
        self.topSpin = self._spin("LNMNullsTopPct", 1, 50, TOP_PCT_DEFAULT, _("Fast mode has no T and no exact Fz scale: a lesion's map passes where it is in the top x% of |value| in the brain mask."))
        self.topLabel = qt.QLabel(_("Rank rule: top x %"))
        form.addRow(self.topLabel, self.topSpin)
        self.seedSpin = self._spin("LNMNullsSeed", 0, 1000000, 0, _("Seed of the relocations and the null cohorts (the same seed gives the same nulls)"))
        self.seedLabel = qt.QLabel(_("Seed"))
        form.addRow(self.seedLabel, self.seedSpin)
        self.strengthCombo = qt.QComboBox()
        self.strengthCombo.setObjectName("LNMNullsStrength")
        self.strengthCombo.addItem(_("Signed μC_r"), "signed")
        self.strengthCombo.addItem(_("Absolute hubness"), "abs")
        self.strengthCombo.setToolTip(_("The map whose value each relocated lesion keeps: the signed mean connectivity (default) or the absolute hubness (recorded)"))
        self.strengthCombo.connect("currentIndexChanged(int)", lambda index: self.onSettingsChanged())
        self.strengthLabel = qt.QLabel(_("Strength map"))
        form.addRow(self.strengthLabel, self.strengthCombo)
        layout.addLayout(form)
        self.controlRow = qt.QWidget()
        controlLayout = qt.QHBoxLayout(self.controlRow)
        controlLayout.setContentsMargins(0, 0, 0, 0)
        self.controlButton = qt.QToolButton()
        self.controlButton.setObjectName("LNMNullsControlSetButton")
        self.controlButton.setText(_("Control set…"))
        self.controlButton.setPopupMode(qt.QToolButton.InstantPopup)
        self.controlButton.setToolButtonStyle(qt.Qt.ToolButtonTextOnly)
        self.controlButton.setProperty("slicerRole", "secondary")
        menu = qt.QMenu(self.controlButton)
        for text, kind in ((_("A folder of lesion masks…"), "pool-folder"), (_("A table of lesions (TSV)…"), "pool-tsv"), (_("Mapped control lesions (maps.tsv)…"), "pool-maps")):
            action = menu.addAction(text)
            action.connect("triggered()", lambda kind=kind: self.chooseControlSet(kind))
        self.controlButton.setMenu(menu)
        controlLayout.addWidget(self.controlButton)
        self.controlLabel = label("", "LNMNullsControlSetLabel", "secondaryText")
        controlLayout.addWidget(self.controlLabel, 1)
        layout.addWidget(self.controlRow)
        self.ruleLabel = label("", "LNMNullsRule", "secondaryText")
        layout.addWidget(self.ruleLabel)
        self.minPLabel = label("", "LNMNullsMinP", "secondaryText")
        layout.addWidget(self.minPLabel)

    def _buildRun(self, layout):
        row = qt.QHBoxLayout()
        row.addStretch(1)
        self.runButton = qt.QPushButton(_("Run null models"))
        self.runButton.setObjectName("LNMNullsRunButton")
        self.runButton.setProperty("slicerRole", "secondary")
        self.runButton.connect("clicked()", self.run)
        row.addWidget(self.runButton)
        layout.addLayout(row)
        self.reasonLabel = label("", "LNMNullsReason", "secondaryText")
        layout.addWidget(self.reasonLabel)
        self.estimateLabel = label("", "LNMNullsEstimate", "secondaryText")
        layout.addWidget(self.estimateLabel)
        self.progress = widgets.progressRow(objectNamePrefix="LNMNulls")
        self.progress.label.setWordWrap(False)
        self.progress.label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        self.progress.widget.hide()
        layout.addWidget(self.progress.widget)

    def _buildResults(self, layout):
        self.results = qt.QWidget()
        self.results.setObjectName("LNMNullsResults")
        inner = qt.QVBoxLayout(self.results)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(4)
        head = qt.QHBoxLayout()
        self.modeChip = widgets.chip(_("Exact pooled null"), "ok")
        self.modeChip.setObjectName("LNMNullsModeChip")
        head.addWidget(self.modeChip)
        head.addStretch(1)
        inner.addLayout(head)
        self.basisLabel = label("", "LNMNullsBasis", None)
        inner.addWidget(self.basisLabel)
        self.approxBanner = widgets.banner("", level="warn")
        self.approxBanner.setObjectName("LNMNullsApproximateBanner")
        self.approxBanner.hide()
        inner.addWidget(self.approxBanner)
        self.staleLabel = label("", "LNMNullsStale", "secondaryText")
        self.staleLabel.hide()
        inner.addWidget(self.staleLabel)
        self.fprLabel = label("", "LNMNullsFpr", None)
        inner.addWidget(self.fprLabel)
        self.attainLabel = label("", "LNMNullsAttainable", "secondaryText")
        inner.addWidget(self.attainLabel)
        self.countsLabel = label("", "LNMNullsCounts", "secondaryText")
        inner.addWidget(self.countsLabel)
        self.qaLabel = label("", "LNMNullsGroupQa", "secondaryText")
        inner.addWidget(self.qaLabel)
        self.parityLabel = label("", "LNMNullsParity", "secondaryText")
        inner.addWidget(self.parityLabel)
        self.outputsTable = widgets.tableView([_("Map"), _("Basis")])
        self.outputsTable.setObjectName("LNMNullsOutputsTable")
        self.outputsTable.setToolTip(_("Click a map to show it over the template"))
        self.outputsTable.horizontalHeader().setSectionResizeMode(0, qt.QHeaderView.Stretch)
        self.outputsTable.horizontalHeader().setSectionResizeMode(1, qt.QHeaderView.ResizeToContents)
        self.outputsTable.connect("cellClicked(int,int)", self.onOutputClicked)
        inner.addWidget(self.outputsTable)
        self.biasCheck = qt.QCheckBox(_("Show the bias map"))
        self.biasCheck.setObjectName("LNMNullsBiasToggle")
        self.biasCheck.setToolTip(_("At each voxel, the share of the null cohorts that pass the classic overlap rule: where it is high, the classic rule passes by chance (hub inflation)"))
        self.biasCheck.connect("toggled(bool)", self.onBiasToggled)
        inner.addWidget(self.biasCheck)
        folderRow = qt.QHBoxLayout()
        self.folderButton = qt.QPushButton(_("Show folder"))
        self.folderButton.setObjectName("LNMNullsShowFolderButton")
        self.folderButton.setProperty("slicerRole", "ghost")
        self.folderButton.connect("clicked()", self.showFolder)
        folderRow.addWidget(self.folderButton)
        folderRow.addStretch(1)
        inner.addLayout(folderRow)
        self.results.hide()
        layout.addWidget(self.results)

    #
    # Places and settings
    #

    def cohortCard(self):
        return self.ctx.card("cohort")

    def currentMapsTsv(self):
        card = self.cohortCard()
        if card is not None and hasattr(card, "currentMapsTsv"):
            return card.currentMapsTsv()
        path = getattr(self.ctx, "cohortMaps", None)
        if path and os.path.isfile(path) and (_readJson(os.path.join(os.path.dirname(path), "cohortmaps.json")) or {}).get("status") == "done":
            return path
        return None

    def cohortRecord(self):
        path = self.currentMapsTsv()
        return _readJson(os.path.join(os.path.dirname(path), "cohortmaps.json")) if path else None

    def mapsFolder(self):
        """The folder of the cohort's lesion maps (``…/conn-<label>/cohort``), or None."""
        path = self.currentMapsTsv()
        if path:
            return os.path.dirname(path)
        widget = _mapWidget()
        return widget.cohortOutputDir() if widget is not None and hasattr(widget, "cohortOutputDir") else None

    def outDir(self, kind=None, mode=None):
        """The run folder of a kind and mode: ``<cohort maps folder>/nulls-<kind>-<mode>``."""
        folder = self.mapsFolder()
        return os.path.join(folder, f"nulls-{kind or self.kind}-{mode or self.effectiveMode()}") if folder else None

    def effectiveMode(self):
        """"fast" for rl_overlap (it has no exact mode), else the chosen mode."""
        return "fast" if self.kind == "rl_overlap" else self.mode

    def usesPool(self):
        """Whether K_pool applies (relocations per lesion; the shifts of an exact cohort shift are the K_pool of its K)."""
        mode = self.effectiveMode()
        return self.kind in POOLED_KINDS or (self.kind == "rl_cohort_shift" and mode == "exact")

    def rule(self):
        """The threshold rule of the run: the Result card's rule (exact), or the top-x% rank rule (fast)."""
        from LesionNetworkMappingLib import thresholds

        sign = self.overlapSettings()[1]
        if self.effectiveMode() == "fast":
            return thresholds.normalize_rule({"kind": "top_pct", "value": float(self.topSpin.value), "statistic": "Fz", "sign": sign})
        widget = _mapWidget()
        return thresholds.normalize_rule(self.ctx.rule or (widget.currentRule() if widget is not None and hasattr(widget, "currentRule") else None))

    def overlapSettings(self):
        """(overlap %, sign) of the classic sensitivity rule: the Overlap tab's settings (90 and both without it)."""
        overlap = getattr(self.cohortCard(), "overlap", None)
        try:
            return float(overlap.sensitivitySlider.value), overlap.sign()
        except (AttributeError, TypeError, ValueError):
            return 90.0, "both"

    def kValue(self):
        """The number of null cohorts: K, or K_pool for an exact cohort shift (its null cohorts are the shifts)."""
        if self.kind == "rl_cohort_shift" and self.effectiveMode() == "exact":
            return int(self.kPoolSpin.value)
        return int(self.kSpin.value)

    def lesionCount(self):
        """Lesions of the nulls: the cohort's cases when it has case and control groups, else every mapped lesion."""
        record = self.cohortRecord() or {}
        mapped = int((record.get("lesions") or {}).get("mapped") or 0)
        widget = _mapWidget()
        groups = ((getattr(widget, "cohortInfo", None) or {}).get("groups")) or {}
        cases = int(groups.get("case") or 0)
        return cases if cases >= 2 and cases <= mapped else mapped

    def participantsPath(self):
        study = self.ctx.study
        path = getattr(study, "path", None) if study is not None else None
        path = os.path.join(path, "participants.tsv") if path else None
        return path if path and os.path.isfile(path) else None

    def _readControlSet(self):
        try:
            value = qt.QSettings().value(CONTROL_SET_SETTING)
            data = json.loads(str(value)) if value else None
        except (TypeError, ValueError):
            return None
        if isinstance(data, dict) and data.get("kind") in ("pool", "pool_maps") and data.get("path") and os.path.exists(data["path"]):
            return {"kind": data["kind"], "path": data["path"]}
        return None

    def chooseControlSet(self, how):
        """Choose the control set (``how``: "pool-folder", "pool-tsv" or "pool-maps") with a file dialog."""
        start = os.path.dirname(self.controlSet["path"]) if self.controlSet else os.path.expanduser("~")
        if how == "pool-folder":
            path = qt.QFileDialog.getExistingDirectory(self.widget, _("Folder of control lesion masks (NIfTI)"), start)
        else:
            path = qt.QFileDialog.getOpenFileName(self.widget, _("Lesions table") if how == "pool-tsv" else _("maps.tsv of the mapped control lesions"), start, "TSV (*.tsv);;All files (*)")
        if path:
            self.setControlSet("pool_maps" if how == "pool-maps" else "pool", path)

    def setControlSet(self, kind, path):
        """Use a control set: ``kind`` "pool" (a folder of lesion masks or a lesions TSV) or "pool_maps" (a mapped maps.tsv)."""
        self.controlSet = {"kind": kind, "path": os.fspath(path)}
        try:
            qt.QSettings().setValue(CONTROL_SET_SETTING, json.dumps(self.controlSet))
        except Exception:
            logging.debug("Lesion network mapping: the control set could not be remembered", exc_info=True)
        self.scheduleRefresh()

    def hasReference(self):
        """Whether the connectome has reference maps (rl_strength needs μC_r or the hubness map)."""
        bundle = self.ctx.bundle
        if bundle is None:
            return False
        key = bundle.path
        if key not in self._referenceOk:
            try:
                from LesionNetworkMappingLib import reference

                self._referenceOk[key] = reference.load_reference(bundle) is not None
            except Exception:
                self._referenceOk[key] = False
        return self._referenceOk[key]

    #
    # State
    #

    def scheduleRefresh(self):
        if not self._cleanedUp:
            self._timer.start()

    def onKindToggled(self, kind, checked):
        if checked and not self._updating and kind != self.kind:
            self.kind = kind
            self.notice = ""
            self.refresh()

    def selectKind(self, kind):
        """Select a null kind (the click on its radio button)."""
        if kind not in KIND_IDS:
            raise ValueError(kind)
        self.kindButtons[kind].setChecked(True)
        if self.kind != kind:  # a button that was already checked sends no signal
            self.kind = kind
            self.refresh()

    def onModeClicked(self, index):
        mode = MODES[index] if 0 <= index < len(MODES) else "exact"
        if mode == "fast" and not self.fast.enabled:
            self.refresh()
            return
        self.setMode(mode)

    def setMode(self, mode):
        """Choose exact or fast (fast only when the parity gate allows it)."""
        if mode not in MODES:
            raise ValueError(mode)
        self.mode = mode if (mode == "exact" or self.fast.enabled) else "exact"
        self.notice = ""
        self.refresh()

    def onKPoolChanged(self, value):
        if not self._updating:
            self._kPoolValues[self._kPoolMode] = int(value)

    def onSettingsChanged(self):
        if not self._updating:
            self.notice = ""
            self.scheduleRefresh()

    def openFastIndex(self):
        bundle = self.ctx.bundle
        return manager_fastindex.openFastIndex(self.ctx, bundle.path if bundle is not None else None)

    def running(self):
        return any(job is not None and not job.isFinished() for job in (self.job, self.mapsJob))

    def mapsRunning(self):
        widget = _mapWidget()
        job = getattr(widget, "cohortJob", None) if widget is not None else None
        return job if job is not None and not job.isFinished() else None

    def runState(self):
        """(enabled, reason) of Run."""
        if self.ctx.study is None:
            return False, _("Open a study in the Seed card (Seed: Cohort).")
        if self.ctx.bundle is None:
            return False, _("Choose a connectome in the Connectome card.")
        if self.running():
            return False, _("Mapping the lesions first…") if self.job is None or self.job.isFinished() else _("The null models are running…")
        mode = self.effectiveMode()
        if mode == "fast" and not self.fast.enabled:
            prefix = _("rl_overlap runs in fast mode only. ") if self.kind == "rl_overlap" else ""
            return False, prefix + self.fast.reason
        if self.kind == "empirical_pool" and self.controlSet is None:
            return False, _("Choose the control set (Control set…): a folder of lesion masks, a lesions table or the maps.tsv of mapped control lesions.")
        if self.kind == "rl_strength" and not self.hasReference():
            return False, _("rl_strength needs the connectome's reference maps: build them in the Connectome card (Manage… → Build reference maps).")
        if self.kValue() < MIN_K:
            return False, _("At least {k} null cohorts are needed: below that the smallest attainable p (1 / (K + 1)) is above 0.05.").format(k=MIN_K)
        if mode == "fast" and self.kValue() > 10000:
            return False, _("Fast mode computes at most 10,000 null cohorts.")
        if not self.ctx.computeEnabled():
            return False, " ".join(self.ctx.computeDisabledReasons()) or _("Not ready")
        if self.currentMapsTsv() is None:
            widget = _mapWidget()
            if self.mapsRunning() is not None:
                return True, _("The lesions are being mapped: the null models start when they are done.")
            enabled, reason = widget.cohortComputeState() if widget is not None else (False, _("The Map module is not available."))
            if not enabled:
                return False, reason
            return True, _("Run maps the lesions first (cohort maps), then runs the null models.")
        if self.lesionCount() < 2:
            return False, _("The null models need at least 2 lesions.")
        if self.ctx.runner.isBusy():
            return True, _("Another computation is running: this one will wait for it.")
        return True, ""

    def refresh(self):
        """Show the state of every control and the newest result of the chosen kind and mode."""
        if self._cleanedUp:
            return
        self._timer.stop()
        mapsTsv = self.currentMapsTsv()
        scope = (self.mapsFolder(), self.ctx.bundle.path if self.ctx.bundle is not None else None)
        if scope != self._scope:
            self._scope = scope
            self.result = None
            self.notice = ""
            if not self.running():
                self.progress.setJob(None)
                self.progress.widget.hide()
        self.fast = manager_fastindex.fastModeState(self.ctx.bundle, mapsTsv) if self.ctx.bundle is not None else manager_fastindex.FastModeState(reason=_("Choose a connectome in the Connectome card."))
        if self.mode == "fast" and not self.fast.enabled:
            self.mode = "exact"  # fast mode is gone (another cohort, an out-of-date parity report): never run it silently
        self._updating = True
        try:
            self.showKinds()
            self.showMode()
            self.showSettings()
        finally:
            self._updating = False
        self.showState(mapsTsv)
        self.showResult()

    def showKinds(self):
        if not self.kindButtons[self.kind].isChecked():
            self.kindButtons[self.kind].setChecked(True)
        for kind, button in self.kindButtons.items():
            if kind == "rl_overlap" and not self.fast.enabled:
                button.setToolTip(f"rl_overlap: {_('fast mode only')}. {self.fast.reason}")
            else:
                button.setToolTip(f"{kind}: {KIND_DESCRIPTIONS[kind]}")

    def showMode(self):
        mode = self.effectiveMode()
        fastButton, exactButton = self.modeGroup.button(1), self.modeGroup.button(0)
        fastButton.setEnabled(self.fast.enabled)
        exactButton.setEnabled(self.kind != "rl_overlap")
        self.modeGroup.button(1 if mode == "fast" else 0).setChecked(True)
        if self.kind == "rl_overlap":
            self.modeNote.setText(_("rl_overlap has no exact mode: it runs on the parcel index."))
        elif mode == "exact":
            self.modeNote.setText(_("Pooled exact null: {k} relocations per lesion are mapped exactly, with the Result card's rule.").format(k=self.kPoolSpin.value) if self.usesPool() else _("Exact: every null lesion set is mapped exactly."))
        else:
            self.modeNote.setText(_("Approximate: the parcel index (approximate z-scale), rank rules only; your observed map is recomputed in fast mode, like with like."))
        self.modeNote.setVisible(True)
        if self.fast.enabled:
            self.fastReason.setText(self.fast.reason)
        else:
            self.fastReason.setText(_("Fast is off: {reason}").format(reason=self.fast.reason))
        self.fastReason.setVisible(bool(self.fastReason.text))
        self.fastIndexButton.setVisible(not self.fast.enabled and (self.fast.needsIndex or self.fast.needsParity or self.ctx.bundle is not None))
        self.fastIndexButton.setText(_("Check the index on my lesions…") if self.fast.needsParity else _("Build fast index…"))

    def showSettings(self):
        mode = self.effectiveMode()
        fast = mode == "fast"
        self.kSpin.setMaximum(10000 if fast else MAX_K)
        if self.kind == "rl_cohort_shift" and not fast:
            self.kLabel.setText(_("Null cohorts (K = K_pool)"))
            self.kSpin.setEnabled(False)
            self.kPoolLabel.setText(_("Shared shifts (= null cohorts)"))
        else:
            self.kLabel.setText(_("Null cohorts (K)"))
            self.kSpin.setEnabled(True)
            self.kPoolLabel.setText(_("Relocations per lesion (K_pool)"))
        key = "fast" if fast else "exact"
        if key != self._kPoolMode:  # each mode keeps its own K_pool
            self._kPoolMode = key
        if self.kPoolSpin.value != self._kPoolValues[key]:
            self.kPoolSpin.setValue(self._kPoolValues[key])
        pool = self.usesPool()
        for widget in (self.kPoolLabel, self.kPoolSpin):
            widget.setVisible(pool)
        for widget in (self.topLabel, self.topSpin):
            widget.setVisible(fast)
        strength = self.kind == "rl_strength"
        for widget in (self.strengthLabel, self.strengthCombo):
            widget.setVisible(strength)
        self.controlRow.setVisible(self.kind == "empirical_pool")
        if self.controlSet is not None:
            name = os.path.basename(os.path.normpath(self.controlSet["path"]))
            self.controlLabel.setText(name + (_(" (mapped lesions)") if self.controlSet["kind"] == "pool_maps" else ""))
        else:
            self.controlLabel.setText(_("No control set chosen"))
        self.seedLabel.setVisible(True)
        rule = self.rule()
        from LesionNetworkMappingLib import thresholds

        overlap, sign = self.overlapSettings()
        signText = {"both": "±", "pos": "+", "neg": "−"}[sign]  # noqa: RUF001
        if fast:
            ruleText = _("Rank rule: each lesion's approximate map passes where it is in the top {x:g}% of |value|").format(x=rule["value"])
        else:
            ruleText = _("Rule: {rule}, the Result card's, applied to each lesion's {statistic} map").format(rule=thresholds.rule_label(rule), statistic=self._statistic(rule))
        self.ruleLabel.setText(_("{rule}. Classic overlap criterion: at least {pct:g}% of the lesions ({sign}), as in the Overlap tab.").format(rule=ruleText, pct=overlap, sign=signText))
        self.minPLabel.setText(self.minPText(self.kValue()))

    @staticmethod
    def _statistic(rule):
        from LesionNetworkMappingLib import cohort

        return cohort.rule_statistic(rule)

    def minPText(self, k):
        """"Minimum attainable p = q = 0.00099 (K = 1000); BH reaches q ≤ 0.05 only when at least N voxels reach it."""
        from LesionNetworkMappingLib import nulls

        minimum = 1.0 / (int(k) + 1)
        bundle = self.ctx.bundle
        text = _("Minimum attainable p: {p} (= 1/(K+1), K = {k}); minimum attainable q: {q}.").format(p=_pText(minimum), q=_pText(minimum), k=int(k))
        if bundle is not None:
            text += " " + _("BH-FDR reaches q ≤ 0.05 only where at least {n:,} voxels reach the floor.").format(n=nulls.fdr_floor_voxels(bundle.n_voxels, minimum))
        return text

    def estimateText(self):
        """The estimated run time of the chosen settings, or ""."""
        from LesionNetworkMappingLib import nulls

        bundle = self.ctx.bundle
        record = self.cohortRecord()
        if bundle is None or record is None or (self.kind == "empirical_pool" and self.controlSet is None):
            return ""
        n = self.lesionCount()
        if n < 2:
            return ""
        mode = self.effectiveMode()
        try:
            details = nulls.estimate_details(n, bundle, int(self.kPoolSpin.value) if self.usesPool() or mode == "fast" else None, mode, self.kValue(), self.kind,
                                             subjects=record.get("subject_ids"), rule=self.rule(), n_pool=None)
        except Exception:
            logging.debug("Lesion network mapping: the nulls estimate failed", exc_info=True)
            return ""
        self.estimateSeconds = details["seconds"]
        extra = _(" (without mapping the control set)") if self.kind == "empirical_pool" else ""
        return _("Estimated time: about {time}{extra}.").format(time=duration(details["seconds"]), extra=extra)

    def showState(self, mapsTsv):
        enabled, reason = self.runState()
        self.runButton.setEnabled(enabled)
        reasonText = self.notice or reason
        self.reasonLabel.setText(reasonText)
        self.reasonLabel.setVisible(bool(reasonText))
        if mapsTsv is None:
            self.statusLabel.setText(_("The lesions are not mapped with this connectome yet: Run maps them first."))
        else:
            record = self.cohortRecord() or {}
            self.statusLabel.setText(_("{n} lesions · N={subjects} connectome subjects · conn-{label}").format(
                n=self.lesionCount(), subjects=record.get("n", "?"), label=(record.get("bundle") or {}).get("label", "?")))
        self.statusLabel.setVisible(True)
        estimate = self.estimateText() if not self.running() else ""
        self.estimateLabel.setText(estimate)
        self.estimateLabel.setVisible(bool(estimate))
        self.runButton.setToolTip(_("Runs {kind} in {mode} mode with K = {k}").format(kind=self.kind, mode=_("exact pooled") if self.effectiveMode() == "exact" else _("fast (approximate)"), k=self.kValue()))

    #
    # Run
    #

    def arguments(self, mapsTsv):
        """Arguments of ``LesionNetworkMappingLib.nulls`` for the current settings, or None."""
        from LesionNetworkMappingLib import thresholds

        bundle = self.ctx.bundle
        folder = self.outDir()
        if bundle is None or folder is None:
            return None
        mode = self.effectiveMode()
        overlap, sign = self.overlapSettings()
        args = ["--maps", mapsTsv, "--bundle", bundle.path, "--out", folder, "--kind", self.kind, "--mode", mode, "--k", str(self.kValue()), "--rule", thresholds.rule_to_json(self.rule()),
                "--sign", sign, "--overlap", f"{overlap:g}", "--seed", str(int(self.seedSpin.value))]
        if self.usesPool() or mode == "fast":
            args += ["--k-pool", str(int(self.kPoolSpin.value))]
        participants = self.participantsPath()
        if participants:
            args += ["--participants", participants]
        if self.kind == "rl_strength":
            args += ["--strength", self.strengthCombo.currentData or "signed"]
        if self.kind == "empirical_pool" and self.controlSet is not None:
            args += ["--pool-maps" if self.controlSet["kind"] == "pool_maps" else "--pool", self.controlSet["path"]]
        if mode == "fast":
            args += ["--index", self.fast.index, "--parity", self.fast.parity]
        study = self.ctx.study
        if getattr(study, "path", None):
            args += ["--run-root", study.path]
        return args

    def run(self):
        """Run the null models (after the cohort maps job when the lesions are not mapped); return the job, or None."""
        if self.running():
            return None
        self.notice = ""
        if not self.runState()[0]:
            self.showState(self.currentMapsTsv())
            return None
        mapsTsv = self.currentMapsTsv()
        if mapsTsv is None:
            job = self.mapsRunning()
            if job is None:
                card = self.cohortCard()
                job = card.mapLesions() if card is not None and hasattr(card, "mapLesions") else _mapWidget().computeCohort()
            if job is None:
                self.showState(None)
                return None
            self.mapsJob = job
            self.progress.setJob(job)
            self.progress.widget.show()
            self.showState(None)
            return job
        return self._start(mapsTsv)

    def _start(self, mapsTsv):
        from . import jobrunner

        args = self.arguments(mapsTsv)
        if args is None:
            return None
        folder = self.outDir()
        os.makedirs(os.path.dirname(folder), exist_ok=True)
        spec = jobrunner.JobSpec("LesionNetworkMappingLib.nulls", args, title=_("Null models"), heavy=True, kind="nulls", name="nulls", runRoot=os.path.dirname(folder),
                                 onEvent=self.onEvent, onDone=lambda job, folder=folder: self.onDone(job, folder), statusText=_statusText)
        self.job = self.ctx.submit(spec)
        self.progress.setJob(self.job)
        self.progress.widget.show()
        self.showState(mapsTsv)
        return self.job

    def _onJobFinished(self, job):
        if self._cleanedUp:
            return
        if job is self.mapsJob:
            self.mapsJob = None
            _updateProgress(self.progress, force=True)
            if job.succeeded() and self.currentMapsTsv() is not None:
                enabled, _reason = self.runState()
                if enabled:
                    self._start(self.currentMapsTsv())
                    return
            self.showState(self.currentMapsTsv())
        elif getattr(job, "kind", None) in ("cohortmaps", "fastindex", "fast-parity"):
            self.scheduleRefresh()

    def onEvent(self, event):
        if event.get("event") == "result" and event.get("kind") == "plan" and event.get("estimate_s") is not None:
            self.estimateLabel.setText(_("Estimated time: about {time}.").format(time=duration(float(event["estimate_s"]))))
            self.estimateLabel.show()
        _updateProgress(self.progress, force=event.get("event") != "progress")

    def onDone(self, job, folder=None):
        _updateProgress(self.progress, force=True)
        if job.state == "failed":
            self.notice = _("The null models failed: {reason}").format(reason=job.failureReason)
        elif job.state == "cancelled":
            self.notice = _("Stopped: a null run that did not finish has no results.")
        else:
            self.notice = ""
        self.estimateLabel.hide()
        self.refresh()
        if job.succeeded():
            if self.result is not None and (folder is None or os.path.abspath(folder) == os.path.abspath(self.outDir())):  # not another kind's result when the choice changed during the run
                self.showMap("sensNullExcess")
            self.pushGroupQa()
            self.message(_("Null models done: the maps are in the Nulls card"))

    def pushGroupQa(self):
        """Tell the QA card to look again: it reads the group QA percentile of the newest run next to the cohort maps (``ctx.runChanged``)."""
        try:
            self.ctx.setCurrentRun(self.ctx.currentRun)
        except Exception:
            logging.exception("Lesion network mapping: the QA card could not be told about the nulls run")

    def message(self, text, ms=3000):
        shell = self.ctx.shell()
        if shell is not None:
            shell.toast(text, ms)

    #
    # Results
    #

    def loadRecord(self, folder):
        """The nulls.json of ``folder`` when it is of this kind and mode and belongs to the current lesion maps, else None."""
        from LesionNetworkMappingLib import nulls

        path = os.path.join(folder, nulls.NULLS_JSON)
        record = _readJson(path)
        if record is None or record.get("format") != nulls.FORMAT_NAME:
            return None
        if record.get("kind") != self.kind or record.get("mode") != nulls.MODE_RECORDS[self.effectiveMode()]:
            return None
        mapsTsv = self.currentMapsTsv()
        try:
            from .card_qa import QACard

            if mapsTsv is None or not QACard._nullsMatchMaps(record, os.stat(path).st_mtime_ns, mapsTsv):
                return None
        except (ImportError, OSError):
            return None
        return record

    def staleReasons(self, record):
        """The settings that differ between the shown run and the card now (names), or []."""
        from LesionNetworkMappingLib import thresholds

        reasons = []
        try:
            if thresholds.normalize_rule(record.get("rule")) != self.rule():
                reasons.append(_("rule"))
        except ValueError:
            reasons.append(_("rule"))
        overlap, sign = self.overlapSettings()
        if float(record.get("overlap_pct") or 0) != overlap:
            reasons.append(_("overlap criterion"))
        if record.get("sign") != sign:
            reasons.append(_("sign"))
        if int(record.get("k") or 0) != self.kValue() and not (self.kind == "rl_cohort_shift" and self.effectiveMode() == "exact"):
            reasons.append("K")
        if record.get("k_pool") is not None and self.usesPool() and int(record["k_pool"]) != int(self.kPoolSpin.value):
            reasons.append("K_pool")
        seed = (record.get("seeds") or {}).get("seed")
        if seed is not None and int(seed) != int(self.seedSpin.value):
            reasons.append(_("seed"))
        return reasons

    def showResult(self):
        folder = self.outDir()
        record = self.loadRecord(folder) if folder else None
        self.result = (folder, record) if record is not None else None
        self.results.setVisible(record is not None)
        if record is None:
            return
        approximate = bool(record.get("approximate"))
        widgets.setChipLevel(self.modeChip, "warn" if approximate else "ok", _("Approximate (fast mode)") if approximate else _("Exact pooled null"))
        statusTag = next((row[3] for row in KIND_ROWS if row[0] == record.get("kind")), record.get("kind_status") or "")
        self.basisLabel.setText(_("{kind} [{status}] · {label} · K = {k}").format(kind=record.get("kind"), status=statusTag, label=record.get("mode_label"), k=record.get("k")))
        if approximate:
            widgets.setBannerText(self.approxBanner, _("Approximate: fast mode uses the parcel index (approximate z-scale, rank rules only); your observed map was recomputed in fast mode, like with like. "
                                                       "Fast-mode maps are never final observed maps."))
        self.approxBanner.setVisible(approximate)
        stale = self.staleReasons(record)
        self.staleLabel.setText(_("Settings changed since this run ({names}): run again to update.").format(names=", ".join(stale)) if stale else "")
        self.staleLabel.setVisible(bool(stale))
        classic = record.get("classic_rule") or {}
        self.fprLabel.setText(_("Classic rule's false-positive rate for this cohort: {text}").format(text=classic.get("text") or "—"))
        inference = record.get("inference") or {}
        self.attainLabel.setText(_("Minimum attainable p: {p} · minimum attainable q: {q}. {text}").format(p=_pText(record.get("min_attainable_p")), q=_pText(record.get("min_attainable_q")),
                                                                                                       text=inference.get("discreteness") or ""))
        self.countsLabel.setText(_("Voxels with p ≤ 0.05: {p:,} · q ≤ 0.05: {q:,} · FWE p ≤ 0.05: {f:,} (of {v:,} in the mask).").format(
            p=inference.get("n_p_05", 0), q=inference.get("n_q_05", 0), f=inference.get("n_fwe_05", 0), v=inference.get("n_voxels", 0)))
        self.qaLabel.setText(self.groupQaText(record.get("group_qa")))
        self.parityLabel.setText(self.parityText(record))
        self.parityLabel.setVisible(bool(self.parityLabel.text))
        basis = _("approximate") if approximate else _("exact pooled")
        outputs = record.get("outputs") or {}
        shown = [desc for desc in OUTPUT_DESCS if desc in outputs and os.path.isfile(os.path.join(folder, outputs[desc]))]
        self.outputsTable.setRowCount(len(shown))
        for row, desc in enumerate(shown):
            for column, text in enumerate((OUTPUT_TEXT[desc], basis)):
                item = qt.QTableWidgetItem(text)
                item.setToolTip(OUTPUT_TIPS[desc])
                self.outputsTable.setItem(row, column, item)
        _fitTable(self.outputsTable, len(shown))
        hasBias = BIAS_DESC in outputs and os.path.isfile(os.path.join(folder, outputs[BIAS_DESC]))
        self.biasCheck.setVisible(hasBias)

    @staticmethod
    def groupQaText(group):
        """The group QA line of a nulls record: the percentile of r(group map, μC_r) among the null cohorts' maps."""
        from .card_qa import ordinal

        if not isinstance(group, dict) or not group.get("available", True):
            return _("Group map QA: not calibrated ({text}).").format(text=(group or {}).get("text") or _("no reference maps for this connectome and these subjects"))
        values = group.get("mean") if isinstance(group.get("mean"), dict) else group.get("overlap")
        if not isinstance(values, dict) or values.get("percentile") is None:
            return _("Group map QA: not calibrated.")
        return _("Group map QA (shown in the QA card): r(group map, μC_r) is at the {p} percentile of {k} null cohorts' maps ({text}).").format(
            p=ordinal(round(values["percentile"])), k=int(values.get("k") or 0), text=values.get("text") or "")

    @staticmethod
    def parityText(record):
        """The parity gate line of a fast-mode record: the strata it allowed and the lesions it left out."""
        parity = record.get("parity")
        if not record.get("approximate") or not isinstance(parity, dict):
            return ""
        strata = ", ".join(parity.get("allowed_strata") or []) or _("none")
        text = _("Parity gate (median partial r ≥ {r:g}, top-10% Dice ≥ {d:g}): fast mode allowed for {strata}.").format(
            r=(parity.get("constants") or {}).get("partial_r", 0.0), d=(parity.get("constants") or {}).get("top10_dice", 0.0), strata=strata)
        left = [item.get("participant_id") for item in record.get("excluded") or [] if isinstance(item, dict) and "volume_ml" in item]  # the gate's exclusions carry the lesion's volume
        if left:
            text += " " + _("{n} lesion(s) left out by the gate: {ids}.").format(n=len(left), ids=", ".join(str(i) for i in left[:8]) + (" …" if len(left) > 8 else ""))
        return text

    #
    # Maps
    #

    def volume(self):
        """The volume that shows the nulls maps (one, reused), or None."""
        node = slicer.mrmlScene.GetNodeByID(self.volumeId) if self.volumeId else None
        if node is not None and node.GetAttribute(VOLUME_ATTRIBUTE) == "1":
            return node
        for candidate in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
            if candidate.GetAttribute(VOLUME_ATTRIBUTE) == "1":
                return candidate
        return None

    def showMap(self, desc):
        """Show one nulls map (excess, p, q, FWE p or bias) with its colour table in the nulls volume; return the volume or None."""
        from LesionNetworkMappingLib import display

        widget = _mapWidget()
        if self.result is None or widget is None:
            return None
        folder, record = self.result
        name = (record.get("outputs") or {}).get(desc)
        path = os.path.join(folder, name) if name else None
        if not path or not os.path.isfile(path):
            return None
        basis = _("approximate") if record.get("approximate") else _("exact pooled")
        title = f"Nulls {desc[len('sensNull'):]} ({record.get('kind')}, {basis}, K={record.get('k')})"
        attributes = {VOLUME_ATTRIBUTE: "1", "Studio.Role": "statmap", display.GROUP_MAP_ATTRIBUTE: desc}
        volume = widget.logic.loadMap(path, title, self.volume(), saveWithScene=True, attributes=attributes)
        self.volumeId = volume.GetID()
        display.applyGroupMapDisplay(volume, desc)
        try:
            from StudioLib import mappage

            if self.ctx.shell() is not None:
                mappage.captureLayers()
                template = mappage.templateVolume(self.ctx.bundle)
            else:
                template = widget.logic.templateVolume()
        except ImportError:
            template = widget.logic.templateVolume()
        node = self.ctx.parameterNode
        display.showLayers(template, volume, node.opacity if node is not None else 0.8)
        self.shownDesc = desc
        if desc != BIAS_DESC:
            self.lastMap = desc
        return volume

    def onOutputClicked(self, row, column):
        if self.result is None:
            return
        outputs = (self.result[1].get("outputs") or {})
        shown = [desc for desc in OUTPUT_DESCS if desc in outputs and os.path.isfile(os.path.join(self.result[0], outputs[desc]))]
        if 0 <= row < len(shown):
            if self.biasCheck.isChecked():
                self.biasCheck.setChecked(False)
            self.showMap(shown[row])

    def onBiasToggled(self, checked):
        if self._updating:
            return
        if checked:
            self.showMap(BIAS_DESC)
        else:
            self.showMap(self.lastMap)

    def showFolder(self):
        folder = self.result[0] if self.result is not None else None
        if folder and os.path.isdir(folder) and not slicer.app.testingEnabled():
            qt.QDesktopServices.openUrl(qt.QUrl.fromLocalFile(folder))

    #
    # Card protocol (called by the Map widget)
    #

    def onStudyChanged(self):
        self.scheduleRefresh()

    def onCohortChanged(self):
        self.scheduleRefresh()

    def onCohortMapsChanged(self, path=None):
        self._referenceOk.clear()
        self.scheduleRefresh()

    def onResultChanged(self):
        self.scheduleRefresh()

    def cleanup(self):
        self._cleanedUp = True
        self._timer.stop()
        for name in ("studyChanged", "bundleChanged", "ruleChanged", "computeEnabledChanged", "runChanged"):
            callbacks = getattr(self.ctx, name, [])
            if self.scheduleRefresh in callbacks:
                callbacks.remove(self.scheduleRefresh)
        finished = getattr(self.ctx.runner, "jobFinished", [])
        if self._onJobFinished in finished:
            finished.remove(self._onJobFinished)
        if self.job is not None and not self.job.isFinished():
            self.job.spec.onDone = None
            self.job.spec.onEvent = None


CARD = {
    "id": "nulls",
    "title": "Nulls",
    "order": ORDER_NULLS,
    "modes": ("cohort",),
    "classic": True,
    "factory": NullsCard,
}

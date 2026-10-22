"""Symptom tab of the Cohort card (order 30): symptom lesion network mapping, sLNM 2.0 (SPEC_LNM_ADDENDUM §3.5, §7.1).

The tab maps where the cohort's per-lesion maps covary with a score, with ``LesionNetworkMappingLib.slnm`` in the job
runner: the partial r of the score from the GLM with a continuous regressor (r = t / sqrt(t² + df)), Freedman-Lane
score permutation (within the dataset blocks when every block can be permuted), voxel max-stat FWE p (primary) and
BH-FDR q (secondary). The parametric p of a voxelwise correlation is never computed or shown.

Controls:

- the score column (``Study.score_columns()``); a column whose role nobody confirmed is marked "(role not confirmed)",
  with a link to the Columns card of Library (Studio) where roles are confirmed;
- covariates (lesion volume on, and off only with a reason; a baseline severity column is optional);
- Pearson or Spearman (ranks of the scores and of the maps), Flip sign (TMS cohorts), the permutation decision
  (automatic within-block score permutation) with its reason, and the number of permutations (5000);
- "Leave-one-dataset-out" (enabled when the block column has at least 2 levels; datasets with fewer than 10
  subjects are skipped by the job with a warning) and "Control scores…" (other score columns mapped with the same
  pipeline and compared with the main map).

Run maps the lesions first when the cohort has no current maps.tsv, then runs sLNM into
``<study>/derivatives/lnm/conn-<label>[_n-N]/slnm/<score>``; the job registers its run. The result shows the R,
FWE p and FDR q maps (one at a time in the Cohort card's group map volume), the partial r range, the LODO matrix
(cross-dataset spatial r with its block-permutation p, and each dataset against the others), the descriptive
convergence maps (Stouffer z, I²) and the control-score comparisons ("r = 0.56 → partial(PC1, μC) r = 0.30,
p_perm = 0.11"). "Use as circuit" makes the unthresholded r map the current circuit (``ctx.currentCircuit``: path
and sha256) for the Validation and Circuits cards. The tab works in Classic and without the Studio shell.
"""

import html
import logging
import os
import re

import qt
from slicer.i18n import tr as _

from .cohort_contrast import DEFAULT_PERM, CohortTab, CovariateChips, _fill, _fmtP, _idClickedSignal, _readJson, checksHtml, label, segmented

#: Order of the tab in the Cohort card (Overlap 10, Contrast 20, Symptom 30, VLSM 40)
ORDER_SYMPTOM = 30
METHODS = ("pearson", "spearman")
P_MAPS = ("p_fwe", "p_fdr")
#: Observers of the current circuit (``ctx.currentCircuitChanged``, created on first use: MapContext has no such list)
CIRCUIT_OBSERVERS = "currentCircuitChanged"


class SymptomTab(CohortTab):
    """The Symptom tab (see the module docstring)."""

    PREFIX = "LNMSymptom"
    TITLE = "sLNM"

    def __init__(self, ctx):
        self.sdesign = None
        self.controls = []
        self._scores = []
        self.controlsPopover = None
        super().__init__(ctx)

    def runText(self):
        return _("Run sLNM")

    def previewTitle(self):
        return _("Design preview · Symptom")

    #
    # Controls
    #

    def buildControls(self, layout):
        form = qt.QFormLayout()
        form.setFieldGrowthPolicy(qt.QFormLayout.AllNonFixedFieldsGrow)
        self.scoreCombo = qt.QComboBox()
        self.scoreCombo.setObjectName("LNMSymptomScore")
        self.scoreCombo.setToolTip(_("The score whose partial correlation with the lesion maps is mapped (a column with the role score)"))
        self.scoreCombo.connect("currentIndexChanged(int)", lambda index: self.onSettingsChanged())
        form.addRow(_("Score"), self.scoreCombo)
        layout.addLayout(form)
        roleRow = qt.QHBoxLayout()
        self.roleLabel = label("", "LNMSymptomScoreRole", "secondaryText")
        roleRow.addWidget(self.roleLabel, 1)
        self.columnsButton = qt.QPushButton(_("Columns…"))
        self.columnsButton.setObjectName("LNMSymptomColumnsLink")
        self.columnsButton.setProperty("slicerRole", "ghost")
        self.columnsButton.setToolTip(_("Confirm the roles of the study's columns (score, covariate, group, block) in Library ▸ Columns"))
        self.columnsButton.connect("clicked()", self.openColumns)
        roleRow.addWidget(self.columnsButton)
        self.roleRow = qt.QWidget()
        self.roleRow.setLayout(roleRow)
        roleRow.setContentsMargins(0, 0, 0, 0)
        self.roleRow.hide()
        layout.addWidget(self.roleRow)

        layout.addWidget(label(_("Covariates"), "LNMSymptomCovariatesTitle", "secondaryText"))
        self.covariates = CovariateChips("LNMSymptom", self.onSettingsChanged)
        layout.addWidget(self.covariates.widget)
        self.volumeReasonEdit = qt.QLineEdit()
        self.volumeReasonEdit.setObjectName("LNMSymptomVolumeOffReason")
        self.volumeReasonEdit.setPlaceholderText(_("Why is lesion volume not a covariate? (recorded in the methods)"))
        self.volumeReasonEdit.setToolTip(_("Lesion volume is the main confound of lesion network maps (Moore 2026): turning it off needs a reason"))
        self.volumeReasonEdit.connect("textChanged(QString)", lambda text: self.scheduleRefresh())
        self.volumeReasonEdit.hide()
        layout.addWidget(self.volumeReasonEdit)

        options = qt.QFormLayout()
        methodRow = qt.QHBoxLayout()
        self.methodFrame, self.methodGroup = segmented([_("Pearson"), _("Spearman")], "LNMSymptomMethod",
                                                       _("Pearson: partial r of the score and the maps · Spearman: the same on ranks (scores, numeric covariates and maps)"))
        self.methodGroup.connect(_idClickedSignal(self.methodGroup), lambda index: self.onSettingsChanged())
        methodRow.addWidget(self.methodFrame)
        self.flipCheckBox = qt.QCheckBox(_("Flip sign"))
        self.flipCheckBox.setObjectName("LNMSymptomFlip")
        self.flipCheckBox.setToolTip(_("Multiply the score by -1 (for example improvement after TMS); recorded in the outputs"))
        self.flipCheckBox.connect("toggled(bool)", lambda checked: self.onSettingsChanged())
        methodRow.addWidget(self.flipCheckBox)
        methodRow.addStretch(1)
        options.addRow(_("Method"), methodRow)
        self.blockCombo = qt.QComboBox()
        self.blockCombo.setObjectName("LNMSymptomBlockColumn")
        self.blockCombo.setToolTip(_("Datasets (exchangeability blocks): scores are permuted within them when every block can be permuted"))
        self.blockCombo.connect("currentIndexChanged(int)", lambda index: self.onSettingsChanged())
        options.addRow(_("Datasets"), self.blockCombo)
        self.permSpinBox = qt.QSpinBox()
        self.permSpinBox.setObjectName("LNMSymptomPermutations")
        self.permSpinBox.setRange(100, 100000)
        self.permSpinBox.setSingleStep(1000)
        self.permSpinBox.setValue(DEFAULT_PERM)
        self.permSpinBox.setToolTip(_("Score permutations, the identity included (5000 by default; fewer than 1000 is a preview)"))
        self.permSpinBox.connect("valueChanged(int)", lambda value: self.onSettingsChanged())
        options.addRow(_("Permutations"), self.permSpinBox)
        self.pMapFrame, self.pMapGroup = segmented([_("FWE"), _("FDR")], "LNMSymptomPMap",
                                                   _("Voxel max-statistic FWE p is the primary inference; BH-FDR q is secondary. Both are written; this chooses the map listed first"))
        self.pMapGroup.connect(_idClickedSignal(self.pMapGroup), lambda index: self.onPMapChanged())
        options.addRow(_("Inference"), self.pMapFrame)
        layout.addLayout(options)
        self.permuteLabel = label("", "LNMSymptomPermuteReason", "secondaryText")
        layout.addWidget(self.permuteLabel)

        extras = qt.QHBoxLayout()
        self.lodoCheckBox = qt.QCheckBox(_("Leave-one-dataset-out"))
        self.lodoCheckBox.setObjectName("LNMSymptomLodo")
        self.lodoCheckBox.setToolTip(_("Per-dataset maps, their cross-dataset similarity with a block-permutation p, and convergence (Stouffer z, I²; descriptive). "
                                       "Datasets need at least 10 subjects"))
        self.lodoCheckBox.connect("toggled(bool)", lambda checked: self.onSettingsChanged())
        extras.addWidget(self.lodoCheckBox)
        extras.addStretch(1)
        self.controlsButton = qt.QPushButton(_("Control scores…"))
        self.controlsButton.setObjectName("LNMSymptomControlsButton")
        self.controlsButton.setProperty("slicerRole", "ghost")
        self.controlsButton.setToolTip(_("Other scores mapped with the same pipeline and compared with this map (spatial r and partial r given PC1 and μC)"))
        self.controlsButton.connect("clicked()", self.showControls)
        extras.addWidget(self.controlsButton)
        layout.addLayout(extras)
        self.controlsLabel = label("", "LNMSymptomControlsText", "secondaryText")
        self.controlsLabel.hide()
        layout.addWidget(self.controlsLabel)

    #
    # Settings
    #

    def score(self):
        return self.scoreCombo.currentData or ""

    def method(self):
        index = self.methodGroup.checkedId()
        return METHODS[index] if 0 <= index < len(METHODS) else "pearson"

    def setMethod(self, method):
        self.methodGroup.button(METHODS.index(method)).setChecked(True)
        self.onSettingsChanged()

    def pMap(self):
        index = self.pMapGroup.checkedId()
        return P_MAPS[index] if 0 <= index < len(P_MAPS) else "p_fwe"

    def onPMapChanged(self):
        """List the chosen p map first in the shown result (both are always written)."""
        if self.result is None:
            return
        row = self.mapsTable.currentRow()
        shown = self.outputs[row][2] if 0 <= row < len(self.outputs) else None
        self.showResult(*self.result)
        paths = [path for _desc, _text, path in self.outputs]
        if shown in paths:
            self.mapsTable.selectRow(paths.index(shown))  # the shown map keeps its selection; it is not reloaded

    def nPerm(self):
        return int(self.permSpinBox.value)

    def volumeOn(self):
        from LesionNetworkMappingLib import design as designlib

        return designlib.VOLUME_COLUMN in self.covariates.checked()

    def covariateColumns(self):
        return [name for name in self.covariates.checked() if name != self.score()]

    def volumeOffReason(self):
        return None if self.volumeOn() else (self.volumeReasonEdit.text.strip() or None)

    def blockColumn(self):
        return self.blockCombo.currentData or None

    def setControls(self, controls):
        self.controls = [name for name in controls if name and name != self.score()]
        self.controlsLabel.setText(_("Control scores: {names}").format(names=", ".join(self.controls)))
        self.controlsLabel.setVisible(bool(self.controls))
        self.scheduleRefresh()

    def blockingSettings(self):
        reasons = []
        if not self.score():
            reasons.append(_("The study has no score column (a numeric column with the role score)."))
        if not self.volumeOn() and not self.volumeReasonEdit.text.strip():
            reasons.append(_("Give the reason for turning lesion volume off (it is recorded in the methods)."))
        clashes = [name for name in self.controls if name in self.covariateColumns()]
        if clashes:
            reasons.append(_("A control score cannot also be a covariate: {names}").format(names=", ".join(clashes)))
        return reasons

    def onSettingsChanged(self):
        self.volumeReasonEdit.setVisible(not self.volumeOn())
        self.showRole()
        self.scheduleRefresh()

    def fillColumns(self):
        from LesionNetworkMappingLib import design as designlib

        study = self.ctx.study
        columns = study.columns if study is not None else []
        scores = study.score_columns() if study is not None else []
        if scores != self._scores:
            self._scores = scores
            items = [(name if confirmed else _("{name} (role not confirmed)").format(name=name), name) for name, confirmed in scores]
            current = self.score()
            texts = [text for text, data in items if data == current]
            _fill(self.scoreCombo, items, texts[0] if texts else (items[0][0] if items else ""))
            self.controls = [name for name in self.controls if name in dict(scores)]
        blocks = [column for column in (study.block_columns() if study is not None else []) if column in columns]
        for column in designlib.NESTING_COLUMNS:
            if column in columns and column not in blocks:
                blocks.append(column)
        _fill(self.blockCombo, [(_("Automatic"), "")] + [(column, column) for column in blocks])
        covariates = list(study.covariate_columns()) if study is not None else []
        if designlib.VOLUME_COLUMN not in covariates:
            covariates.insert(0, designlib.VOLUME_COLUMN)
        covariates += [name for name, _confirmed in scores if name not in covariates]  # a baseline severity score, for example
        self.covariates.setColumns(covariates, checked=(designlib.VOLUME_COLUMN,))
        for name, button in self.covariates.buttons.items():
            button.setVisible(name != self.score())  # the score is not its own covariate
            if name == self.score() and button.checked:
                button.setChecked(False)
        self.showRole()

    def showRole(self):
        confirmed = dict(self._scores).get(self.score(), True)
        if self.score() and not confirmed:
            self.roleLabel.setText(_("{name}: role not confirmed. Confirm that it is a score in the Columns card.").format(name=self.score()))
        self.roleRow.setVisible(bool(self.score()) and not confirmed)

    def openColumns(self):
        """Where roles are confirmed: Library's Columns card in Studio (L-31), else a note (Classic)."""
        shell = self.ctx.shell()
        text = _("Confirm the role of {name} (score) in Library ▸ Columns, or in the study's participants.json.").format(name=self.score() or _("the column"))
        if shell is not None:
            shell.switchTo("library")
            try:
                from StudioLib import library

                focus = getattr(library, "focusCard", None)
                if callable(focus):
                    focus("columns")
            except Exception:
                pass
            shell.toast(text, 5000)
        else:
            self.reasonLabel.setText(text)
            self.reasonLabel.show()

    #
    # Control scores popover
    #

    def showControls(self):
        candidates = [name for name, _confirmed in self._scores if name != self.score()]
        if self.controlsPopover is not None:
            self.controlsPopover.close()
        popover = qt.QFrame(self.widget, qt.Qt.Popup)
        popover.setObjectName("LNMSymptomControls")
        popover.setFrameShape(qt.QFrame.StyledPanel)
        popover.setMinimumWidth(260)
        layout = qt.QVBoxLayout(popover)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.addWidget(label(_("Control scores"), "LNMSymptomControlsTitle"))
        if not candidates:
            layout.addWidget(label(_("The study has no other score column."), "LNMSymptomControlsEmpty", "secondaryText"))
        self.controlsList = qt.QListWidget()
        self.controlsList.setObjectName("LNMSymptomControlsList")
        for name in candidates:
            item = qt.QListWidgetItem(name)
            item.setFlags(item.flags() | qt.Qt.ItemIsUserCheckable)
            item.setCheckState(qt.Qt.Checked if name in self.controls else qt.Qt.Unchecked)
            self.controlsList.addItem(item)
        self.controlsList.setMaximumHeight(160)
        self.controlsList.connect("itemChanged(QListWidgetItem*)", lambda item: self._readControls())
        self.controlsList.setVisible(bool(candidates))
        layout.addWidget(self.controlsList)
        layout.addWidget(label(_("Each is mapped like the score, then compared with its map: spatial r and partial r given the reference maps (PC1, μC), with permutation p."),
                               "LNMSymptomControlsHelp", "caption"))
        self.controlsPopover = popover
        popover.adjustSize()
        popover.move(self.controlsButton.mapToGlobal(qt.QPoint(0, self.controlsButton.height)))
        popover.show()
        return popover

    def _readControls(self):
        names = []
        for row in range(self.controlsList.count):
            item = self.controlsList.item(row)
            if item.checkState() == qt.Qt.Checked:
                names.append(item.text())
        self.setControls(names)

    #
    # Design
    #

    def buildDesign(self, study, mapped, noMap):
        from LesionNetworkMappingLib import design as designlib
        from LesionNetworkMappingLib import slnm

        if not self.score():
            raise designlib.DesignError(_("The study has no score column (a numeric column with the role score)."))
        self.sdesign = slnm.slnm_design(study, self.score(), covariates=self.covariateColumns(), block_column=self.blockColumn(), method=self.method(), flip=bool(self.flipCheckBox.checked),
                                        n_perm=self.nPerm(), volume_off_reason=self.volumeOffReason())
        return self.addMapChecks(self.sdesign.design, mapped, noMap, self.nPerm())

    def rebuildDesign(self):
        self.sdesign = None
        return super().rebuildDesign()

    def refresh(self):
        if self._cleanedUp:
            return
        self.fillColumns()
        super().refresh()
        self.showPermuteState()

    def datasetLevels(self):
        """The levels of the dataset (block) column among the design's subjects: what LODO splits by."""
        from LesionNetworkMappingLib import design as designlib
        from LesionNetworkMappingLib.study import is_missing

        design = self.design
        if design is None:
            return []
        column = self.blockColumn() or design.block_column
        if column is None:
            study = self.designStudy()[0] if self.ctx.study is not None else None
            column = next((name for name in designlib.NESTING_COLUMNS if study is not None and name in study.columns), None)
        if column is None:
            return []
        study = self.designStudy()[0]
        sids = set(design.sids)
        return sorted({row.get(column) for row in study.subjects if row["participant_id"] in sids and not is_missing(row.get(column))})

    def showPermuteState(self):
        from LesionNetworkMappingLib import slnm

        design = self.design
        if design is None:
            self.permuteLabel.hide()
            self.lodoCheckBox.setEnabled(False)
            return
        method = _("Spearman (ranks)") if self.method() == "spearman" else _("Pearson")
        self.permuteLabel.setText(_("Permute: {reason} · partial r, {method}{flip}").format(reason=design.permute_reason, method=method, flip=_(" · score × −1") if self.flipCheckBox.checked else ""))  # noqa: RUF001
        self.permuteLabel.show()
        levels = self.datasetLevels()
        enabled = len(levels) >= slnm.MIN_LODO_DATASETS
        self.lodoCheckBox.setEnabled(enabled)
        if not enabled and self.lodoCheckBox.checked:
            self.lodoCheckBox.setChecked(False)
        self.lodoCheckBox.setToolTip(_("Leave-one-dataset-out over {n} datasets ({names}); datasets with fewer than {k} subjects are skipped").format(
            n=len(levels), names=", ".join(levels[:6]), k=slnm.MIN_LODO_SUBJECTS) if enabled else _("Needs a dataset (block) column with at least 2 levels"))

    #
    # Run
    #

    def outputName(self):
        from LesionNetworkMappingLib import slnm

        name = slnm.score_label(self.score())
        if self.method() == "spearman":
            name += "Spearman"
        if self.flipCheckBox.checked:
            name += "Flip"
        return name

    def outputDir(self):
        connDir = self.connectomeDir()
        if not connDir or not self.score():
            return None
        return os.path.join(connDir, "slnm", self.outputName())

    def resultFolders(self):
        connDir = self.connectomeDir()
        root = os.path.join(connDir, "slnm") if connDir else None
        if not root or not os.path.isdir(root):
            return []
        return [os.path.join(root, name) for name in sorted(os.listdir(root)) if os.path.isfile(os.path.join(root, name, "slnm.json"))]

    def readResult(self, folder):
        from LesionNetworkMappingLib import slnm

        return _readJson(os.path.join(folder, slnm.SLNM_JSON))

    def jobSpec(self, mapsTsv):
        from . import jobrunner

        outDir = self.outputDir()
        if outDir is None:
            raise ValueError(_("No output folder (study, connectome or score missing)"))
        os.makedirs(outDir, exist_ok=True)
        self._jobFolder = outDir
        args = ["--maps", mapsTsv, "--participants", self.participantsPath(), "--score", self.score(), "--covariates", ",".join(self.covariateColumns()),
                "--perm", str(self.nPerm()), "--mask", self.maskPath(), "--out", outDir, "--run-root", self.ctx.study.path]
        reason = self.volumeOffReason()
        if reason:
            args += ["--volume-off-reason", reason]
        if self.blockColumn():
            args += ["--block", self.blockColumn()]
        if self.method() == "spearman":
            args.append("--spearman")
        if self.flipCheckBox.checked:
            args.append("--flip")
        if self.lodoCheckBox.checked and self.lodoCheckBox.enabled:
            args.append("--lodo")
        if self.controls:
            args += ["--control-scores", ",".join(self.controls)]
        if self.ctx.bundle is not None:
            args += ["--bundle", self.ctx.bundle.path]
        return jobrunner.JobSpec("LesionNetworkMappingLib.slnm", args, title=_("sLNM"), heavy=True, kind="slnm", runRoot=os.path.dirname(outDir), onEvent=self.onEvent, onDone=self.onDone,
                                 name="slnm", statusText=_slnmStatusText)

    #
    # Results
    #

    def buildResults(self, layout):
        self.lodoLabel = label("", "LNMSymptomLodoSummary")
        self.lodoLabel.setTextFormat(qt.Qt.RichText)
        layout.addWidget(self.lodoLabel)
        self.lodoTable = qt.QTableWidget(0, 0)
        self.lodoTable.setObjectName("LNMSymptomLodoTable")
        self.lodoTable.setEditTriggers(qt.QAbstractItemView.NoEditTriggers)
        self.lodoTable.setToolTip(_("Cross-dataset spatial r of the per-dataset partial r maps; LODO r: each dataset against the combination of the others (one-sided block-permutation p)"))
        self.lodoTable.horizontalHeader().setSectionResizeMode(qt.QHeaderView.Stretch)
        layout.addWidget(self.lodoTable)
        self.comparisonLabel = label("", "LNMSymptomComparisons")
        self.comparisonLabel.setTextFormat(qt.Qt.RichText)
        layout.addWidget(self.comparisonLabel)

    def buildResultButtons(self, row):
        self.circuitButton = qt.QPushButton(_("Use as circuit"))
        self.circuitButton.setObjectName("LNMSymptomUseCircuitButton")
        self.circuitButton.setProperty("slicerRole", "secondary")
        self.circuitButton.setToolTip(_("Make the unthresholded partial r map the current circuit (Validation and Circuits cards)"))
        self.circuitButton.connect("clicked()", self.useAsCircuit)
        row.addWidget(self.circuitButton)

    def showResult(self, folder, record):
        files = record.get("outputs") or {}

        def path(key):
            name = files.get(key)
            return os.path.join(folder, os.path.basename(name)) if name else None

        def desc(key, fallback):
            match = re.search(r"_desc-([A-Za-z0-9]+)_map\.nii", files.get(key) or "")
            return match.group(1) if match else fallback

        score = record.get("score") or ""
        method = _("Spearman") if record.get("method") == "spearman" else _("Pearson")
        rows = [(desc("r", "slnmR"), _("Partial r ({method}) of {score}").format(method=method, score=score), path("r"))]
        pMaps = [("p_fwe", _("FWE p (max-statistic, permutation)")), ("p_fdr", _("FDR q (BH, secondary)"))]
        if self.pMap() == "p_fdr":
            pMaps.reverse()
        rows += [(desc(key, key), text, path(key)) for key, text in pMaps]
        for control in record.get("controls") or []:
            name = control.get("name") or control.get("desc")
            rows.append((desc(f"control:{name}:r", "controlR"), _("Control {name}: partial r").format(name=name), path(f"control:{name}:r")))
            rows.append((desc(f"control:{name}:p_fwe", "controlPFwe"), _("Control {name}: FWE p").format(name=name), path(f"control:{name}:p_fwe")))
        lodo = record.get("lodo") or {}
        if lodo.get("computed"):
            for dataset, n in zip(lodo.get("datasets") or [], lodo.get("n") or [], strict=False):
                rows.append((desc(f"lodo:{dataset}", "lodoR"), _("Dataset {name} (n = {n}): partial r").format(name=dataset, n=n), path(f"lodo:{dataset}")))
            rows.append((desc("lodo_combined", "LodoR"), _("Datasets combined (Fisher z, n−3 weights)"), path("lodo_combined")))  # noqa: RUF001
            rows.append((desc("stouffer_z", "StoufferZ"), _("Stouffer z (descriptive)"), path("stouffer_z")))
            rows.append((desc("i2", "ISq"), _("I² heterogeneity (descriptive)"), path("i2")))
        self.setOutputs(rows)
        inference = record.get("inference") or {}
        permutation = record.get("permutation") or {}
        fwe, fdr = inference.get("fwe") or {}, inference.get("fdr") or {}
        nPerm = permutation.get("n_perm")
        kind = _(" (exhaustive)") if permutation.get("exhaustive") else (_(" (preview)") if permutation.get("preview") else "")
        covariates = ", ".join(record.get("covariates") or []) or _("none")
        lines = [
            "<b>" + html.escape(_("{method} partial r of {score}{flip} · n = {n} · covariates: {covariates}").format(method=method, score=score, flip=_(" × −1") if record.get("flip") else "",  # noqa: RUF001
                                                                                                               n=record.get("n", "?"), covariates=covariates)) + "</b>",
            html.escape(_("{n} score permutations{kind} · smallest attainable p {p} · {scheme}").format(n=f"{nPerm:,}" if isinstance(nPerm, int) else nPerm, kind=kind,
                                                                                                    p=_fmtP(inference.get("min_attainable_p")), scheme=permutation.get("reason") or "")),
            html.escape(_("FWE p ≤ 0.05: {n} voxels (smallest FWE p {p}) · FDR q ≤ 0.05: {q} voxels").format(n=fwe.get("n_significant", "?"), p=_fmtP(fwe.get("min_p")), q=fdr.get("n_significant", "?"))),
        ]
        effectRange = (record.get("partial_r") or {}).get("range")
        if effectRange:
            lines.append(html.escape(_("Effect size: partial r from {low:.2f} to {high:.2f}").format(low=effectRange[0], high=effectRange[1])))
        if fdr.get("note"):
            lines.append(html.escape(fdr["note"]))
        lines.append(html.escape(_("Inference by score permutation (Freedman–Lane); no parametric p is computed.")))  # noqa: RUF001
        self.summaryLabel.setText("".join(f"<div>{line}</div>" for line in lines))
        self.showLodo(lodo)
        comparisons = [control.get("text") for control in record.get("controls") or [] if control.get("text")]
        reason = (record.get("comparison") or {}).get("reference_missing_reason")
        texts = [f"<div>{html.escape(_('Control {name}: {text}').format(name=control.get('name') or control.get('desc') or '', text=control.get('text')))}</div>"
                 for control in record.get("controls") or [] if control.get("text")]
        if comparisons and reason:
            texts.append(f"<div>{html.escape(_('Partial comparison not computed: {reason}').format(reason=reason))}</div>")
        self.comparisonLabel.setText("".join(texts))
        self.comparisonLabel.setVisible(bool(texts))
        notes = []
        warnings = [check for check in (record.get("checks") or []) if check.get("level") == "warn"]
        warnings += [{"level": "warn", "message": text} for text in lodo.get("warnings") or []]
        if warnings:
            notes.append(checksHtml(warnings))
        limitations = record.get("limitations") or []
        if limitations:
            notes.append("<div><b>" + html.escape(_("Limitations")) + "</b></div>" + "".join(f"<div>· {html.escape(text)}</div>" for text in limitations))
        if record.get("volume_off_reason"):
            notes.append("<div>" + html.escape(_("Lesion volume off: {reason}").format(reason=record["volume_off_reason"])) + "</div>")
        self.notesLabel.setText("".join(notes))
        self.notesLabel.setVisible(bool(notes))
        self.circuitButton.setEnabled(bool(path("circuit") and os.path.isfile(path("circuit"))))

    def showLodo(self, lodo):
        table = self.lodoTable
        if not lodo or not lodo.get("computed"):
            table.hide()
            skipped = (lodo or {}).get("skipped") or {}
            if lodo:
                text = _("Leave-one-dataset-out not computed") + (": " + "; ".join(f"{name}: {why}" for name, why in skipped.items()) if skipped else "")
                self.lodoLabel.setText(html.escape(text))
            self.lodoLabel.setVisible(bool(lodo))
            return
        datasets = list(lodo.get("datasets") or [])
        matrix = lodo.get("matrix") or []
        lodoR, lodoP = lodo.get("lodo_r") or [], lodo.get("lodo_p_perm") or []
        headers = [f"{name} (n={n})" for name, n in zip(datasets, lodo.get("n") or [], strict=False)]
        table.clear()
        table.setRowCount(len(datasets))
        table.setColumnCount(len(datasets) + 2)
        table.setHorizontalHeaderLabels(list(datasets) + [_("LODO r"), _("p_perm")])
        table.setVerticalHeaderLabels(headers)
        for row in range(len(datasets)):
            for column in range(len(datasets)):
                value = matrix[row][column] if row < len(matrix) and column < len(matrix[row]) else None
                table.setItem(row, column, qt.QTableWidgetItem("—" if value is None else f"{value:.2f}"))
            table.setItem(row, len(datasets), qt.QTableWidgetItem(f"{lodoR[row]:.2f}" if row < len(lodoR) else "—"))
            table.setItem(row, len(datasets) + 1, qt.QTableWidgetItem(_fmtP(lodoP[row]) if row < len(lodoP) else "—"))
        table.resizeRowsToContents()
        rowsHeight = sum(table.rowHeight(row) for row in range(table.rowCount))
        header, frame = table.horizontalHeader().sizeHint, table.frameWidth  # properties in PythonQt
        header, frame = (header() if callable(header) else header), (frame() if callable(frame) else frame)
        table.setFixedHeight(min(header.height() + rowsHeight + 2 * frame + 2, 220))
        table.show()
        permutation = lodo.get("permutation") or {}
        text = _("Cross-dataset similarity: mean r = {r:.2f}, p_perm = {p} ({n} block permutations, one-sided)").format(r=float(lodo.get("mean_offdiag_r") or 0.0), p=_fmtP(lodo.get("p_perm")),
                                                                                                                 n=permutation.get("n_perm", "?"))
        convergence = lodo.get("convergence") or {}
        detail = _("Convergence maps (Stouffer z, I²) are {label}; median I² {i2:.2f}").format(label=convergence.get("label", "descriptive"), i2=float(convergence.get("i2_median") or 0.0))
        self.lodoLabel.setText(f"<div><b>{html.escape(_('Leave-one-dataset-out'))}</b></div><div>{html.escape(text)}</div><div>{html.escape(detail)}</div>")
        self.lodoLabel.show()

    def useAsCircuit(self):
        """Make the shown result's r map the current circuit: ``ctx.currentCircuit`` = {path, sha256, ...}; return it."""
        from LesionNetworkMappingLib import export

        if not self.result:
            return None
        folder, record = self.result
        name = (record.get("outputs") or {}).get("circuit")
        path = os.path.join(folder, os.path.basename(name)) if name else None
        if not path or not os.path.isfile(path):
            return None
        circuit = {"path": path, "sha256": export.file_sha256(path), "kind": "slnm", "score": record.get("score"), "method": record.get("method"), "flip": bool(record.get("flip")),
                   "run_id": record.get("run_id"), "n": record.get("n")}
        self.ctx.currentCircuit = circuit
        for callback in list(getattr(self.ctx, CIRCUIT_OBSERVERS, None) or []):
            try:
                callback()
            except Exception:
                logging.exception("Lesion network mapping: a currentCircuitChanged observer failed")
        self.message(_("Circuit: partial r map of {score} (for Validation and Circuits)").format(score=record.get("score")))
        return circuit

    def cleanup(self):
        if self.controlsPopover is not None:
            self.controlsPopover.close()
        super().cleanup()


def _slnmStatusText(job):
    """Status chip text of an sLNM job: "sLNM 1200/5000 · 3 min left"."""
    from . import jobrunner

    event = job.lastEvent or {}
    text = _("sLNM")
    stage = (job.stage or {}).get("name")
    if stage and stage not in ("permutations", "maps"):
        text += f" · {stage}"
    if event.get("event") == "progress" and event.get("total") and event.get("stage") == "permutations":
        text += f" {event.get('done')}/{event.get('total')}"
    elif job.progress is not None:
        text += f" {int(job.progress * 100)}%"
    eta = jobrunner.formatEta(job.eta)
    return f"{text} · {eta}" if eta else text


TAB = {
    "id": "symptom",
    "title": "Symptom",
    "order": ORDER_SYMPTOM,
    "factory": SymptomTab,
}

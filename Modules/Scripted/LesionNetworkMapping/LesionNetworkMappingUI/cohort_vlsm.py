"""VLSM tab of the Cohort card (order 40): voxel-based lesion-symptom mapping on the same cohort (SPEC_LNM_ADDENDUM F37).

Where does lesion *location* relate to the group or the score? Running VLSM on the lesions of the cohort answers the
reviewer's question "does the network add anything beyond lesion location?" (Salvalaggio et al. 2021; Moore et al.
2024), and a VLSM cluster can seed a circuit (VLSM-seeded network mapping: Boes 2021; Bowren 2022).

The tab runs ``LesionNetworkMappingLib.vlsm`` in the job runner on the study's binary lesion masks (2 mm template grid),
with the design controls of the Contrast tab (``cohort_contrast``: the group and its levels with the contrast, or a
score with its direction; covariate chips with lesion volume on, and off only with a reason; the permutation decision;
FWE primary and FDR secondary; the number of permutations; the design preview) and the minimum overlap: only voxels
lesioned in at least that many lesions are tested ("Auto": the larger of 5 lesions and 10% of the lesions). It needs
no connectome and no lesion maps.

The result lists the T, FWE p, effect size, FDR q and coverage maps (one shown at a time in the Cohort card's group map
volume), the voxels tested and the FWE clusters. "Seed from VLSM cluster" maps the chosen cluster (or every significant
voxel of one sign) with the Connectome card's connectome as a circuit (``vlsm seed`` → ``circuits.seed_this``, a job):
it goes to ``<study>/derivatives/lnm/circuits/circuit-<name>`` and is selected in the Circuits card, linked to the VLSM
run and its lesions, so that damage scores of the same lesions are same-cohort. Works in Classic and without the shell.
"""

import html
import logging
import os

import qt
from slicer.i18n import tr as _

from . import widgets
from .cohort_contrast import (
    CONTRASTS,
    DEFAULT_PERM,
    PERMUTE_CHOICES,
    CohortTab,
    CovariateChips,
    _fill,
    _fmtP,
    _idClickedSignal,
    _readJson,
    _stat,
    _standInVolume,
    checksHtml,
    contrastText,
    label,
    segmented,
)

#: Order of the tab in the Cohort card (Overlap 10, Contrast 20, Symptom 30, VLSM 40)
ORDER_VLSM = 40
TESTS = ("groups", "score")
SCORE_CONTRASTS = ("two_sided", "positive", "negative")
#: How the VLSM outputs are listed (vlsm.json "outputs" keys)
OUTPUT_LABELS = (
    ("stat", "Statistic (T)"),
    ("p_fwe", "FWE p (max-statistic, permutation)"),
    ("effect", "Effect size"),
    ("p_fdr", "FDR q (BH, secondary)"),
    ("tfce_p_fwe", "TFCE FWE p (permutation)"),
    ("p_unc", "Uncorrected p (permutation)"),
    ("coverage", "Coverage: lesions per voxel"),
)
CIRCUITS_FOLDER = "circuits"
SIGN_TEXT = {"pos": "+", "neg": "−"}  # noqa: RUF001


class VlsmTab(CohortTab):
    """The VLSM tab (see the module docstring)."""

    PREFIX = "LNMVlsm"
    TITLE = "VLSM"

    def __init__(self, ctx):
        self._levels = []
        self._scores = []
        self.seedJob = None
        self.seedFolder = None
        self.seedNotice = ""
        self._seedResultKey = None  # (folder, run id) of the result the seed notice belongs to
        self.clusterTable = []
        super().__init__(ctx)

    def runText(self):
        return _("Run VLSM")

    def previewTitle(self):
        return _("Design preview · VLSM")

    #
    # Controls
    #

    def buildControls(self, layout):
        intro = label(_("Lesion location alone: the binary lesion masks of this cohort, voxel by voxel. Compare with the network maps to ask "
                        "whether the network adds anything beyond location."), "LNMVlsmIntro", "caption")
        layout.addWidget(intro)
        form = qt.QFormLayout()
        form.setRowWrapPolicy(qt.QFormLayout.WrapLongRows)
        form.setFieldGrowthPolicy(qt.QFormLayout.AllNonFixedFieldsGrow)
        self.testFrame, self.testGroup = segmented([_("Groups"), _("Score")], "LNMVlsmTest", _("Test a group factor (lesion location differs between groups) or a score"))
        self.testGroup.button(0).setChecked(True)
        self.testGroup.connect(_idClickedSignal(self.testGroup), lambda index: self.onSettingsChanged())
        form.addRow(_("Test"), self.testFrame)
        layout.addLayout(form)

        # Groups: the Contrast tab's controls
        self.groupBox = qt.QWidget()
        self.groupBox.setObjectName("LNMVlsmGroupControls")
        groupForm = qt.QFormLayout(self.groupBox)
        groupForm.setRowWrapPolicy(qt.QFormLayout.WrapLongRows)
        groupForm.setContentsMargins(0, 0, 0, 0)
        groupForm.setFieldGrowthPolicy(qt.QFormLayout.AllNonFixedFieldsGrow)
        self.groupCombo = qt.QComboBox()
        self.groupCombo.setObjectName("LNMVlsmGroupColumn")
        self.groupCombo.setToolTip(_("The factor tested (a column with the role group in participants.json)"))
        self.groupCombo.connect("currentIndexChanged(int)", lambda index: self.onGroupColumnChanged())
        groupForm.addRow(_("Groups"), self.groupCombo)
        levels = qt.QHBoxLayout()
        self.levelACombo = qt.QComboBox()
        self.levelACombo.setObjectName("LNMVlsmLevelA")
        self.levelACombo.setToolTip(_("Level A"))
        self.levelACombo.connect("currentIndexChanged(int)", lambda index: self.onLevelsChanged())
        levels.addWidget(self.levelACombo, 1)
        levels.addWidget(qt.QLabel(_("vs")))
        self.levelBCombo = qt.QComboBox()
        self.levelBCombo.setObjectName("LNMVlsmLevelB")
        self.levelBCombo.setToolTip(_("Level B"))
        self.levelBCombo.connect("currentIndexChanged(int)", lambda index: self.onLevelsChanged())
        levels.addWidget(self.levelBCombo, 1)
        groupForm.addRow(_("Levels"), levels)
        self.contrastFrame, self.contrastGroup = segmented(["A > B", "B > A", _("Two-sided"), "F"], "LNMVlsmContrast",
                                                          _("Contrast · A > B: lesions of A cover the voxel more often than lesions of B (one-sided); F tests every level"))
        self.contrastGroup.button(2).setChecked(True)
        self.contrastGroup.connect(_idClickedSignal(self.contrastGroup), lambda index: self.onSettingsChanged())
        groupForm.addRow(_("Contrast"), self.contrastFrame)
        layout.addWidget(self.groupBox)

        # Score
        self.scoreBox = qt.QWidget()
        self.scoreBox.setObjectName("LNMVlsmScoreControls")
        scoreForm = qt.QFormLayout(self.scoreBox)
        scoreForm.setRowWrapPolicy(qt.QFormLayout.WrapLongRows)
        scoreForm.setContentsMargins(0, 0, 0, 0)
        scoreForm.setFieldGrowthPolicy(qt.QFormLayout.AllNonFixedFieldsGrow)
        self.scoreCombo = qt.QComboBox()
        self.scoreCombo.setObjectName("LNMVlsmScore")
        self.scoreCombo.setToolTip(_("The score related to lesion location (a column with the role score)"))
        self.scoreCombo.connect("currentIndexChanged(int)", lambda index: self.onSettingsChanged())
        scoreForm.addRow(_("Score"), self.scoreCombo)
        self.directionFrame, self.directionGroup = segmented([_("Two-sided"), _("Higher"), _("Lower")], "LNMVlsmDirection",
                                                            _("Direction · Higher: a lesion in the voxel goes with a higher score (one-sided); Lower: with a lower score"))
        self.directionGroup.button(0).setChecked(True)
        self.directionGroup.connect(_idClickedSignal(self.directionGroup), lambda index: self.onSettingsChanged())
        scoreForm.addRow(_("Direction"), self.directionFrame)
        self.scoreBox.hide()
        layout.addWidget(self.scoreBox)

        layout.addWidget(label(_("Covariates"), "LNMVlsmCovariatesTitle", "secondaryText"))
        self.covariates = CovariateChips("LNMVlsm", self.onSettingsChanged)
        layout.addWidget(self.covariates.widget)
        self.logVolumeCheckBox = qt.QCheckBox(_("log(volume)"))
        self.logVolumeCheckBox.setObjectName("LNMVlsmLogVolume")
        self.logVolumeCheckBox.setToolTip(_("Use the logarithm of lesion volume (suggested when its skewness is above 2)"))
        self.logVolumeCheckBox.connect("toggled(bool)", lambda checked: self.onSettingsChanged())
        layout.addWidget(self.logVolumeCheckBox)
        self.volumeReasonEdit = qt.QLineEdit()
        self.volumeReasonEdit.setObjectName("LNMVlsmVolumeOffReason")
        self.volumeReasonEdit.setPlaceholderText(_("Why is lesion volume not a covariate? (recorded in the methods)"))
        self.volumeReasonEdit.setToolTip(_("Direct control of total lesion volume works best in VLSM (Moore 2026): turning it off needs a reason"))
        self.volumeReasonEdit.connect("textChanged(QString)", lambda text: self.scheduleRefresh())
        self.volumeReasonEdit.hide()
        layout.addWidget(self.volumeReasonEdit)

        overlap = qt.QFormLayout()
        overlap.setRowWrapPolicy(qt.QFormLayout.WrapLongRows)
        overlap.setFieldGrowthPolicy(qt.QFormLayout.AllNonFixedFieldsGrow)
        overlapRow = qt.QHBoxLayout()
        self.minLesionsSpinBox = qt.QSpinBox()
        self.minLesionsSpinBox.setObjectName("LNMVlsmMinLesions")
        self.minLesionsSpinBox.setRange(0, 100000)
        self.minLesionsSpinBox.setSpecialValueText(_("Auto"))
        self.minLesionsSpinBox.setValue(0)
        self.minLesionsSpinBox.setToolTip(_("Test only voxels lesioned in at least this many lesions (Auto: the larger of 5 lesions and 10% of the lesions)"))
        self.minLesionsSpinBox.connect("valueChanged(int)", lambda value: self.onSettingsChanged())
        overlapRow.addWidget(self.minLesionsSpinBox)
        self.minOverlapLabel = label("", "LNMVlsmMinOverlapText", "secondaryText")
        overlapRow.addWidget(self.minOverlapLabel, 1)
        overlap.addRow(_("Minimum overlap"), overlapRow)
        layout.addLayout(overlap)

        permute = qt.QHBoxLayout()
        permute.addWidget(qt.QLabel(_("Permute")))
        self.permuteCombo = qt.QComboBox()
        self.permuteCombo.setObjectName("LNMVlsmPermuteMode")
        for mode, text in PERMUTE_CHOICES:
            self.permuteCombo.addItem(_(text), mode)
        self.permuteCombo.setToolTip(_("Override the automatic exchangeability decision"))
        self.permuteCombo.connect("currentIndexChanged(int)", lambda index: self.onSettingsChanged())
        # Ignored: the combos share the row and never make the inspector wider
        self.permuteCombo.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        permute.addWidget(self.permuteCombo, 1)
        self.blockCombo = qt.QComboBox()
        self.blockCombo.setObjectName("LNMVlsmBlockColumn")
        self.blockCombo.setToolTip(_("Exchangeability blocks: dataset, else source_type (a column with the role block)"))
        self.blockCombo.connect("currentIndexChanged(int)", lambda index: self.onSettingsChanged())
        self.blockCombo.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        permute.addWidget(self.blockCombo, 1)
        layout.addLayout(permute)
        self.permuteLabel = label("", "LNMVlsmPermuteReason", "secondaryText")
        layout.addWidget(self.permuteLabel)

        options = qt.QFormLayout()
        options.setRowWrapPolicy(qt.QFormLayout.WrapLongRows)
        inference = qt.QHBoxLayout()
        self.fweCheckBox = qt.QCheckBox(_("FWE"))
        self.fweCheckBox.setObjectName("LNMVlsmFwe")
        self.fweCheckBox.setChecked(True)
        self.fweCheckBox.setEnabled(False)
        self.fweCheckBox.setToolTip(_("Voxel-wise max-statistic FWE: the primary inference (always computed)"))
        inference.addWidget(self.fweCheckBox)
        self.fdrCheckBox = qt.QCheckBox(_("FDR"))
        self.fdrCheckBox.setObjectName("LNMVlsmFdr")
        self.fdrCheckBox.setChecked(True)
        self.fdrCheckBox.setToolTip(_("Benjamini-Hochberg q of the permutation p (secondary)"))
        self.fdrCheckBox.connect("toggled(bool)", lambda checked: self.onSettingsChanged())
        inference.addWidget(self.fdrCheckBox)
        inference.addStretch(1)
        options.addRow(_("Inference"), inference)
        self.permSpinBox = qt.QSpinBox()
        self.permSpinBox.setObjectName("LNMVlsmPermutations")
        self.permSpinBox.setRange(100, 100000)
        self.permSpinBox.setSingleStep(1000)
        self.permSpinBox.setValue(DEFAULT_PERM)
        self.permSpinBox.setToolTip(_("Permutations, the identity included (5000 by default; fewer than 1000 is a preview)"))
        self.permSpinBox.connect("valueChanged(int)", lambda value: self.onSettingsChanged())
        options.addRow(_("Permutations"), self.permSpinBox)
        layout.addLayout(options)

    def buildResults(self, layout):
        self.clustersTitle = label(_("Seed a circuit from a VLSM cluster"), "LNMVlsmClustersTitle")
        widgets._font(self.clustersTitle, 12, bold=True)
        layout.addWidget(self.clustersTitle)
        row = qt.QHBoxLayout()
        self.clusterCombo = qt.QComboBox()
        self.clusterCombo.setObjectName("LNMVlsmClusterCombo")
        self.clusterCombo.setToolTip(_("Clusters of the voxels with FWE p ≤ 0.05 (26-connected, largest first; extents are descriptive)"))
        self.clusterCombo.setSizeAdjustPolicy(qt.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.clusterCombo.setMinimumContentsLength(12)
        self.clusterCombo.connect("currentIndexChanged(int)", lambda index: self.showSeedState())
        self.clusterCombo.connect("activated(int)", lambda index: self.jumpToCluster())
        # Ignored: the combo shrinks next to the button and never makes the inspector wider
        self.clusterCombo.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        row.addWidget(self.clusterCombo, 1)
        self.seedButton = qt.QPushButton(_("Seed from VLSM cluster"))
        self.seedButton.setObjectName("LNMVlsmSeedButton")
        self.seedButton.setProperty("slicerRole", "secondary")
        self.seedButton.connect("clicked()", self.seedFromCluster)
        row.addWidget(self.seedButton)
        layout.addLayout(row)
        self.seedReasonLabel = label("", "LNMVlsmSeedReason", "secondaryText")
        layout.addWidget(self.seedReasonLabel)
        self.seedProgress = widgets.progressRow(objectNamePrefix="LNMVlsmSeed")
        self.seedProgress.label.setWordWrap(False)
        self.seedProgress.label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        self.seedProgress.widget.hide()
        layout.addWidget(self.seedProgress.widget)
        self.seedNoticeLabel = label("", "LNMVlsmSeedNotice")
        self.seedNoticeLabel.hide()
        layout.addWidget(self.seedNoticeLabel)

    #
    # Settings
    #

    def testKind(self):
        index = self.testGroup.checkedId()
        return TESTS[index] if 0 <= index < len(TESTS) else "groups"

    def setTestKind(self, kind):
        self.testGroup.button(TESTS.index(kind)).setChecked(True)
        self.onSettingsChanged()

    def contrast(self):
        if self.testKind() == "score":
            index = self.directionGroup.checkedId()
            return SCORE_CONTRASTS[index] if 0 <= index < len(SCORE_CONTRASTS) else "two_sided"
        index = self.contrastGroup.checkedId()
        return CONTRASTS[index] if 0 <= index < len(CONTRASTS) else "two_sided"

    def setContrast(self, contrast):
        if self.testKind() == "score":
            self.directionGroup.button(SCORE_CONTRASTS.index(contrast)).setChecked(True)
        else:
            self.contrastGroup.button(CONTRASTS.index(contrast)).setChecked(True)
        self.onSettingsChanged()

    def groupColumn(self):
        return self.groupCombo.currentText or "group"

    def levels(self):
        a, b = self.levelACombo.currentText, self.levelBCombo.currentText
        return [a, b] if a and b and a != b else None

    def score(self):
        return self.scoreCombo.currentData or ""

    def volumeOn(self):
        from LesionNetworkMappingLib import design as designlib

        return designlib.VOLUME_COLUMN in self.covariates.checked() or designlib.VOLUME_COLUMN not in self.covariates.buttons

    def nPerm(self):
        return int(self.permSpinBox.value)

    def minimumOverlap(self):
        """(min_lesions, min_percent) of ``vlsm``: Auto is (5, 10%); a number is that many lesions."""
        from LesionNetworkMappingLib import vlsm

        value = int(self.minLesionsSpinBox.value)
        return (vlsm.DEFAULT_MIN_LESIONS, vlsm.DEFAULT_MIN_PERCENT) if value == 0 else (value, 0.0)

    def covariateColumns(self):
        tested = self.score() if self.testKind() == "score" else self.groupColumn()
        return [name for name in self.covariates.checked() if name != tested]

    def spec(self):
        """The ``design.DesignSpec`` of the current settings (n_perm at least the design minimum; ``nPerm()`` is run)."""
        from LesionNetworkMappingLib import design as designlib

        contrast = self.contrast()
        covariates = self.covariateColumns()
        transforms = {designlib.VOLUME_COLUMN: "log"} if designlib.VOLUME_COLUMN in covariates and self.logVolumeCheckBox.checked else {}
        reason = None if designlib.VOLUME_COLUMN in covariates else (self.volumeReasonEdit.text.strip() or None)
        inference = {"n_perm": max(self.nPerm(), designlib.MIN_N_PERM), "fwe": True, "fdr": bool(self.fdrCheckBox.checked), "tfce": False}
        common = {"covariates": covariates, "transforms": transforms, "block_column": self.blockCombo.currentData or None, "permute": self.permuteCombo.currentData or "auto",
                  "inference": inference, "volume_off_reason": reason}
        if self.testKind() == "score":
            return designlib.DesignSpec(test="continuous", group_column=None, score_column=self.score() or None, contrast=contrast, **common)
        test = "anova" if contrast == "F" else "two_group"
        return designlib.DesignSpec(test=test, group_column=self.groupColumn(), levels=None if contrast == "F" else self.levels(), contrast=contrast, **common)

    def blockingSettings(self):
        reasons = []
        if self.testKind() == "score" and not self.score():
            reasons.append(_("The study has no score column (a numeric column with the role score)."))
        if self.testKind() == "groups" and self.contrast() != "F" and self.levels() is None:
            reasons.append(_("Choose two different levels A and B."))
        if not self.volumeOn() and not self.volumeReasonEdit.text.strip():
            reasons.append(_("Give the reason for turning lesion volume off (it is recorded in the methods)."))
        if self.design is not None and self.design.n:
            from LesionNetworkMappingLib import vlsm

            count = vlsm.min_lesion_count(self.design.n, *self.minimumOverlap())
            if count > self.design.n:
                reasons.append(_("Lower the minimum overlap: no voxel can be lesioned in {k} of the {n} lesions.").format(k=count, n=self.design.n))
        return reasons

    def onSettingsChanged(self):
        from LesionNetworkMappingLib import design as designlib

        score = self.testKind() == "score"
        self.groupBox.setVisible(not score)
        self.scoreBox.setVisible(score)
        volumeOn = self.volumeOn()
        self.volumeReasonEdit.setVisible(not volumeOn)
        self.logVolumeCheckBox.setVisible(volumeOn and designlib.VOLUME_COLUMN in self.covariates.buttons)
        twoGroup = self.contrast() != "F"
        self.levelACombo.setEnabled(twoGroup)
        self.levelBCombo.setEnabled(twoGroup)
        tested = self.score() if score else self.groupColumn()
        for name, button in self.covariates.buttons.items():
            button.setVisible(name != tested)
        self.showMinOverlap()
        self.scheduleRefresh()

    def onGroupColumnChanged(self):
        self.fillLevels()
        self.onSettingsChanged()

    def onLevelsChanged(self):
        a, b = self.levelACombo.currentText or "A", self.levelBCombo.currentText or "B"
        for index, text in enumerate([f"{a} > {b}", f"{b} > {a}"]):
            self.contrastGroup.button(index).setText(text)
        self.onSettingsChanged()

    def fillColumns(self):
        """Group, score, block and covariate columns of the study (kept when they still exist)."""
        from LesionNetworkMappingLib import design as designlib

        study = self.ctx.study
        columns = study.columns if study is not None else []
        groups = [column for column in (study.group_columns() if study is not None else []) if column in columns]
        _fill(self.groupCombo, [(column, column) for column in groups])
        scores = study.score_columns() if study is not None else []
        if scores != self._scores:
            self._scores = scores
            items = [(name if confirmed else _("{name} (role not confirmed)").format(name=name), name) for name, confirmed in scores]
            current = self.score()
            texts = [text for text, data in items if data == current]
            _fill(self.scoreCombo, items, texts[0] if texts else (items[0][0] if items else ""))
        blocks = [column for column in (study.block_columns() if study is not None else []) if column in columns]
        for column in designlib.NESTING_COLUMNS:
            if column in columns and column not in blocks:
                blocks.append(column)
        _fill(self.blockCombo, [(_("Blocks: automatic"), "")] + [(column, column) for column in blocks])
        covariates = list(study.covariate_columns()) if study is not None else []
        if designlib.VOLUME_COLUMN not in covariates:
            covariates.insert(0, designlib.VOLUME_COLUMN)  # computed from the masks when participants.tsv has none
        covariates += [name for name, _confirmed in scores if name not in covariates]  # a baseline severity score, for example
        self.covariates.setColumns(covariates, checked=(designlib.VOLUME_COLUMN,))
        tested = self.score() if self.testKind() == "score" else self.groupColumn()
        for name, button in self.covariates.buttons.items():
            button.setVisible(name != tested)
        self.fillLevels()

    def fillLevels(self):
        from LesionNetworkMappingLib import design as designlib
        from LesionNetworkMappingLib.study import is_missing

        study = self.ctx.study
        column = self.groupColumn()
        levels = []
        if study is not None and column in study.columns:
            values = [row.get(column) for row in study.subjects if not is_missing(row.get(column))]
            try:
                levels = designlib._column_levels(study, column, values)
            except Exception:
                levels = sorted(set(values))
        if levels == self._levels:
            return
        self._levels = levels
        a = self.levelACombo.currentText if self.levelACombo.count else ""
        b = self.levelBCombo.currentText if self.levelBCombo.count else ""
        a = a if a in levels else (levels[0] if levels else "")
        b = b if b in levels and b != a else next((level for level in levels if level != a), "")
        _fill(self.levelACombo, [(level, level) for level in levels], a)
        _fill(self.levelBCombo, [(level, level) for level in levels], b)
        self.onLevelsChanged()

    #
    # Design (on the study's lesions: no connectome and no lesion maps are needed)
    #

    def connectomeDir(self):
        """The scope of the shown results: ``<study>/derivatives/lnm/vlsm`` (VLSM does not depend on the connectome)."""
        from LesionNetworkMappingLib import vlsm

        study = self.ctx.study
        path = getattr(study, "path", None) if study is not None else None
        return vlsm.vlsm_root(path) if path else None

    def designStudy(self):
        """(subjects study, None, [], note): participants.tsv as saved, restricted to the rows with a lesion file; lesion
        volumes missing from participants.tsv get stand-in values in the preview (the job computes them from the masks).
        Cached on the files' modification times.
        """
        from LesionNetworkMappingLib import design as designlib
        from LesionNetworkMappingLib import glm
        from LesionNetworkMappingLib.study import is_missing

        participants = self.participantsPath()
        if not participants or not os.path.isfile(participants):
            raise ValueError(_("The study has no participants.tsv"))
        dictionary = os.path.join(os.path.dirname(participants), "participants.json")
        key = (participants, _stat(participants), _stat(dictionary))
        if self._studyCache[0] == key:
            return self._studyCache[1]
        study, _inStudy = glm._load_study(participants)
        rows = [row for row in study.subjects if not is_missing(row.get("lesion")) and os.path.isfile(study.abspath(row["lesion"]) or "")]
        withLesion = {row["participant_id"] for row in rows}
        missing = [row["participant_id"] for row in rows if is_missing(row.get(designlib.VOLUME_COLUMN))]
        subset, filled = glm._subset_study(study, withLesion, {sid: f"{_standInVolume(sid):.3f}" for sid in missing})
        note = ""
        if filled:
            note = _("{n} lesion volumes are not in participants.tsv yet: Run computes them from the masks (stand-in values in this preview).").format(n=filled)
        value = (subset, None, [], note)
        self._studyCache = (key, value)
        return value

    def buildDesign(self, study, mapped, noMap):
        from LesionNetworkMappingLib import design as designlib
        from LesionNetworkMappingLib import glm

        if self.testKind() == "score" and not self.score():
            raise designlib.DesignError(_("The study has no score column (a numeric column with the role score)."))
        design = designlib.build_design(study, self.spec())
        count, exhaustive = glm.shuffling_count(design, self.nPerm())
        if self.nPerm() != design.spec.n_perm:
            glm._override_permutations(design, self.nPerm(), count, exhaustive)
        return design

    def refresh(self):
        if self._cleanedUp:
            return
        self.fillColumns()
        super().refresh()
        self.showPermuteState()
        self.showMinOverlap()
        self.showSeedState()

    def showPermuteState(self):
        design = self.design
        if design is None:
            self.permuteLabel.setText("")
            self.permuteLabel.hide()
            return
        text = design.permute_reason
        if design.block_column and design.permute != "free":
            text += " · " + _("{n} blocks").format(n=len(set(design.block_labels or [])))
        self.permuteLabel.setText(_("Permute: {reason}").format(reason=text))
        self.permuteLabel.show()

    def showMinOverlap(self):
        from LesionNetworkMappingLib import vlsm

        minLesions, minPercent = self.minimumOverlap()
        n = self.design.n if self.design is not None else None
        if n:
            count = vlsm.min_lesion_count(n, minLesions, minPercent)
            text = _("voxels lesioned in ≥ {k} of {n} lesions").format(k=count, n=n)
        else:
            text = _("voxels lesioned in ≥ {rule} of the lesions").format(rule=vlsm.min_lesion_rule(minLesions, minPercent))
        if int(self.minLesionsSpinBox.value) == 0:
            text += " · " + _("max of 5 and 10%")
        self.minOverlapLabel.setText(text)

    #
    # State and run
    #

    def runState(self):
        """(enabled, reason) of Run: a study, the settings, the design checks and running jobs (no connectome needed)."""
        if self.ctx.study is None:
            return False, _("Open a study in the Seed card (Seed: Cohort).")
        if self.running():
            return False, _("{title} running…").format(title=self.TITLE)
        if self.design is None:
            return False, self.designError or _("No design.")
        settings = self.blockingSettings()
        if settings:
            return False, settings[0]
        if self.design.blocking:
            return False, _("Blocked by the design checks (in red above).")
        if not self.ctx.computeEnabled():
            return False, " ".join(self.ctx.computeDisabledReasons()) or _("Not ready")
        if self.ctx.runner.isBusy():
            return True, _("Another computation is running: this one will wait for it.")
        return True, ""

    def showState(self):
        enabled, reason = self.runState()
        self.runButton.setText(self.runText())
        self.runButton.setEnabled(enabled)
        self.reasonLabel.setText(reason)
        self.reasonLabel.setVisible(bool(reason))
        self.statusLabel.hide()
        self.showSeedState()

    def run(self):
        """Run the VLSM on the study's lesions; return the job, or None."""
        if self.running():
            return None
        self.rebuildDesign()
        self.showDesignState()
        if not self.runState()[0]:
            self.showState()
            return None
        return self._start(None)

    def outputDir(self):
        from LesionNetworkMappingLib import vlsm

        study = self.ctx.study
        if study is None or self.design is None:
            return None
        return vlsm.default_out_dir(study.path, self.design)

    def resultFolders(self):
        root = self.connectomeDir()
        if not root or not os.path.isdir(root):
            return []
        from LesionNetworkMappingLib import vlsm

        return [os.path.join(root, name) for name in sorted(os.listdir(root)) if os.path.isfile(os.path.join(root, name, vlsm.VLSM_JSON))]

    def readResult(self, folder):
        from LesionNetworkMappingLib import vlsm

        return _readJson(os.path.join(folder, vlsm.VLSM_JSON))

    def jobSpec(self, mapsTsv=None):
        from . import jobrunner

        outDir = self.outputDir()
        if outDir is None:
            raise ValueError(_("No output folder (no study)"))
        os.makedirs(outDir, exist_ok=True)
        self._jobFolder = outDir
        spec = self.spec()
        designPath = spec.save(os.path.join(outDir, "design.json"))
        minLesions, minPercent = self.minimumOverlap()
        study = self.ctx.study.path
        args = ["run", "--study", study, "--design", designPath, "--out", outDir, "--run-root", study, "--min-lesions", str(minLesions), "--min-percent", f"{minPercent:g}"]
        if self.nPerm() != spec.n_perm:
            args += ["--perm", str(self.nPerm())]
        return jobrunner.JobSpec("LesionNetworkMappingLib.vlsm", args, title=_("VLSM"), heavy=True, kind="vlsm", runRoot=os.path.dirname(outDir), onEvent=self.onEvent,
                                 onDone=self.onDone, name="vlsm", statusText=_vlsmStatusText)

    #
    # Results
    #

    def showResult(self, folder, record):
        key = (folder, record.get("run_id"))
        if key != self._seedResultKey:  # another result: the notice of a seed from the previous one is stale
            self._seedResultKey = key
            self._setSeedNotice("")
            if self.seedJob is None or self.seedJob.isFinished():
                self.seedProgress.widget.hide()
        files = record.get("outputs") or {}
        desc = (record.get("contrast") or {}).get("desc") or "vlsm"
        effect = (record.get("effect_size") or {}).get("name") or "HedgesG"
        effectText = {"HedgesG": _("Effect size: Hedges g of lesion presence"), "PartialR": _("Effect size: partial r"), "PartialEtaSq": _("Effect size: partial η²")}.get(effect, effect)
        stat = record.get("stat") or "t"
        rows = []
        for key, text in OUTPUT_LABELS:
            name = files.get(key)
            if not name:
                continue
            if key == "effect":
                text = effectText
            elif key == "stat":
                text = {"t": _("Statistic (T)"), "F": _("Statistic (F)"), "v": _("Statistic (Aspin–Welch v)")}.get(stat, text)  # noqa: RUF001
            else:
                text = _(text)
            # The coverage map is shown like an overlap count (lesions per voxel, zero hidden)
            suffix = {"stat": "Fstat" if stat == "F" else "Tstat", "p_unc": "PUnc", "p_fwe": "PFwe", "p_fdr": "PFdr", "tfce_p_fwe": "TfcePFwe", "effect": effect}.get(key)
            rows.append(("overlapCount" if key == "coverage" else desc + suffix, text, os.path.join(folder, os.path.basename(name))))
        self.setOutputs(rows)
        overlap = record.get("min_overlap") or {}
        fwe, fdr = record.get("fwe") or {}, record.get("fdr") or {}
        permutation = record.get("permutation") or {}
        nPerm = permutation.get("n_perm") or record.get("n_perm")
        clusters = (record.get("clusters") or {}).get("table") or []
        perLevel = " · ".join(f"{html.escape(str(level))} {count}" for level, count in (record.get("n_per_level") or {}).items() if level != "all")
        if record.get("test") == "continuous":
            names = (record.get("contrast") or {}).get("columns") or []
            scoreName = names[1] if len(names) > 1 else _("score")
            contrast = {"two_sided": _("{score}, two-sided"), "positive": _("{score}, higher with lesion"), "negative": _("{score}, lower with lesion")}.get(
                (record.get("contrast") or {}).get("spec"), "{score}").format(score=scoreName)
        else:
            contrast = contrastText((record.get("contrast") or {}).get("spec", ""), list(record.get("n_per_level") or {}))
        lines = [
            "<b>" + html.escape(_("VLSM · {contrast} · n = {n}").format(contrast=contrast, n=record.get("n", "?"))) + (f" ({perLevel})" if perLevel else "") + "</b>",
            html.escape(_("{v} voxels tested: lesioned in ≥ {k} lesions ({rule}) · lesion volume {volume}").format(
                v=f"{overlap.get('n_voxels_tested', record.get('n_voxels', 0)):,}", k=overlap.get("min_count", "?"), rule=overlap.get("rule", ""),
                volume=_("covariate") if record.get("volume_covariate") else _("off"))),
            html.escape(_("{n} permutations{kind} · smallest attainable p {p:.2g} · {scheme}").format(
                n=f"{nPerm:,}" if isinstance(nPerm, int) else nPerm, kind=_(" (exhaustive)") if permutation.get("exhaustive") else (_(" (preview)") if permutation.get("preview") else ""),
                p=float(record.get("min_attainable_p") or record.get("min_p") or 1.0), scheme=permutation.get("reason") or permutation.get("scheme") or "")),
            html.escape(_("FWE p ≤ 0.05: {n} voxels in {c} clusters (smallest FWE p {p})").format(n=fwe.get("n_significant", "?"), c=(record.get("clusters") or {}).get("n", len(clusters)),
                                                                                                 p=_fmtP(fwe.get("min_p")))),
        ]
        effectRange = (record.get("effect_size") or {}).get("range")
        if effectRange:
            lines.append(html.escape(_("{effect} from {low:.2f} to {high:.2f}").format(effect=effectText, low=effectRange[0], high=effectRange[1])))
        if fdr.get("enabled", True) and fdr:
            lines.append(html.escape(_("FDR q ≤ 0.05: {n} voxels").format(n=fdr.get("n_significant", "?"))))
        self.summaryLabel.setText("".join(f"<div>{line}</div>" for line in lines))
        notes = []
        if record.get("interpretation"):
            notes.append("<div>" + html.escape(str(record["interpretation"])) + "</div>")
        checks = [check for check in (record.get("checks") or []) if check.get("level") == "warn"]
        if checks:
            notes.append(checksHtml(checks))
        limitations = record.get("limitations") or []
        if limitations:
            notes.append("<div><b>" + html.escape(_("Limitations")) + "</b></div>" + "".join(f"<div>· {html.escape(text)}</div>" for text in limitations))
        if record.get("volume_off_reason"):
            notes.append("<div>" + html.escape(_("Lesion volume off: {reason}").format(reason=record["volume_off_reason"])) + "</div>")
        self.notesLabel.setText("".join(notes))
        self.notesLabel.setVisible(bool(notes))
        self.fillClusters(clusters, record.get("stat") or "t")

    #
    # Seed from VLSM cluster
    #

    def fillClusters(self, clusters, stat="t"):
        """The cluster choices of the shown result: each cluster (largest first), then every significant voxel of each sign."""
        self.clusterTable = list(clusters)
        statName = "F" if stat == "F" else "T"
        items = []
        for item in clusters:
            x, y, z = item.get("peak_xyz") or (0.0, 0.0, 0.0)
            items.append((_("Cluster {id} · {sign} · {n} voxels ({ml:.1f} mL) · peak {stat} {value:.2f} at {where}").format(
                id=item.get("id"), sign=SIGN_TEXT.get(item.get("sign"), ""), n=item.get("voxels"), ml=float(item.get("volume_ml") or 0.0), stat=statName,
                value=float(item.get("peak_stat") or 0.0), where=_whereText(x, y, z)), str(item.get("id"))))
        for sign in ("pos", "neg"):
            if sum(1 for item in clusters if item.get("sign") == sign) > 1:
                items.append((_("All significant voxels ({sign})").format(sign=SIGN_TEXT[sign]), f"all:{sign}"))
        self.clusterCombo.blockSignals(True)
        try:
            current = self.clusterCombo.currentData if self.clusterCombo.count else None
            self.clusterCombo.clear()
            for text, data in items:
                self.clusterCombo.addItem(text, data)
                self.clusterCombo.setItemData(self.clusterCombo.count - 1, text, qt.Qt.ToolTipRole)
            if not items:
                self.clusterCombo.addItem(_("No voxel passes FWE p ≤ 0.05"), "")
            index = self.clusterCombo.findData(current) if current else -1
            self.clusterCombo.setCurrentIndex(max(index, 0))
        finally:
            self.clusterCombo.blockSignals(False)
        self.showSeedState()

    def jumpToCluster(self):
        """Centre the views on the peak of the chosen cluster (the first one for "all significant voxels"); return the peak or None."""
        choice = str(self.clusterCombo.currentData or "")
        if not choice or not self.clusterTable:
            return None
        sign = choice.split(":", 1)[1] if choice.startswith("all:") else None
        item = next((row for row in self.clusterTable if (str(row.get("id")) == choice) or (sign and row.get("sign") == sign)), None)
        if item is None or not item.get("peak_xyz"):
            return None
        x, y, z = (float(value) for value in item["peak_xyz"])
        try:
            import slicer

            slicer.modules.markups.logic().JumpSlicesToLocation(x, y, z, True)
        except Exception:
            logging.exception("Lesion network mapping: the views could not jump to the VLSM cluster")
            return None
        return (x, y, z)

    def _showFirstMap(self):
        super()._showFirstMap()
        if not self._cleanedUp:
            self.jumpToCluster()

    def seedState(self):
        """(enabled, reason) of Seed from VLSM cluster."""
        if self.result is None:
            return False, _("Run VLSM first.")
        if not (self.clusterCombo.currentData or ""):
            return False, _("No cluster: no voxel passes FWE p ≤ 0.05.")
        if self.seedJob is not None and not self.seedJob.isFinished():
            return False, _("Mapping the cluster…")
        if not self.ctx.computeEnabled():
            return False, " ".join(self.ctx.computeDisabledReasons()) or _("Not ready")
        bundle = self.ctx.bundle
        if bundle is None:
            return False, _("Choose a connectome in the Connectome card: the cluster is mapped with it.")
        if getattr(bundle, "kind", "timeseries") != "timeseries":
            return False, _("A circuit needs a time series connectome.")
        return True, ""

    def showSeedState(self):
        if not hasattr(self, "seedButton"):
            return
        enabled, reason = self.seedState()
        self.seedButton.setEnabled(enabled)
        self.seedButton.setToolTip(reason or _("Map the chosen cluster with the connectome as a circuit (listed in the Circuits card)"))
        shown = reason if self.result is not None and reason and reason != _("Mapping the cluster…") else ""
        self.seedReasonLabel.setText(shown)
        self.seedReasonLabel.setVisible(bool(shown))

    def circuitsFolder(self):
        try:
            return os.path.join(self.ctx.runRoot(), CIRCUITS_FOLDER)
        except Exception:
            return None

    def _circuitName(self, choice):
        from LesionNetworkMappingLib import circuits

        record = self.result[1] if self.result else {}
        desc = (record.get("contrast") or {}).get("desc") or "vlsm"
        suffix = f"C{choice}" if not choice.startswith("all:") else "All" + choice.split(":", 1)[1].capitalize()
        return circuits.circuit_name(desc[:1].upper() + desc[1:] + suffix)

    def seedFromCluster(self):
        """Map the chosen cluster as a circuit (a job); return the job, or None with the reason shown."""
        from . import jobrunner

        if not self.seedState()[0]:
            self.showSeedState()
            return None
        choice = str(self.clusterCombo.currentData)
        folder = self.result[0]
        root = self.circuitsFolder()
        if not root:
            return None
        name, counter = self._circuitName(choice), 2
        base = name
        while os.path.exists(os.path.join(root, f"circuit-{name}")):
            # "Run<k>" keeps the cluster number readable ("...C1Run2", not "...C12" that reads as cluster 12)
            name, counter = f"{base}Run{counter}", counter + 1
        out = os.path.join(root, f"circuit-{name}")
        os.makedirs(root, exist_ok=True)  # the job makes the circuit folder: a failed seed leaves no empty one
        study = self.ctx.study
        args = ["seed", "--vlsm", folder, "--bundle", self.ctx.bundle.path, "--out", out, "--name", name, "--run-root", getattr(study, "path", None) or self.ctx.runRoot()]
        if choice.startswith("all:"):
            args += ["--cluster", "all", "--sign", choice.split(":", 1)[1]]
        else:
            args += ["--cluster", choice]
        spec = jobrunner.JobSpec("LesionNetworkMappingLib.vlsm", args, title=_("Circuit"), heavy=True, kind="circuits", name="vlsmseed", runRoot=os.path.dirname(out),
                                 onEvent=self._updateSeedProgress, onDone=lambda job, out=out: self._onSeedDone(job, out))
        self.seedFolder = out
        self.seedJob = self.ctx.submit(spec)
        self.seedProgress.setJob(self.seedJob)
        self.seedProgress.widget.show()
        self._setSeedNotice("")
        self.showSeedState()
        return self.seedJob

    def _updateSeedProgress(self, event):
        try:
            from LesionNetworkMapping import updateProgressQuietly
        except ImportError:
            self.seedProgress.update()
            return
        updateProgressQuietly(self.seedProgress, event.get("event") != "progress")

    def _setSeedNotice(self, text):
        self.seedNotice = text or ""
        self.seedNoticeLabel.setText(self.seedNotice)
        self.seedNoticeLabel.setVisible(bool(self.seedNotice))

    def _onSeedDone(self, job, out):
        if self._cleanedUp:
            return
        self.seedProgress.update()
        name = os.path.basename(out)
        if job.state in ("failed", "cancelled"):
            _removeUnfinished(out)
        if job.state == "failed":
            self._setSeedNotice(_("The circuit could not be made: {reason}").format(reason=job.failureReason))
        elif job.state == "cancelled":
            self._setSeedNotice(_("Stopped: a circuit that did not finish has no maps."))
        else:
            self.seedProgress.widget.hide()
            self._setSeedNotice(_("{name} made from the VLSM cluster: it is selected in the Circuits card.").format(name=name))
            card = self.ctx.card("circuits")
            if card is not None:
                try:
                    card.refresh()
                    if hasattr(card, "selectCircuit"):
                        card.selectCircuit(os.path.join(out, "circuit.json"))
                except Exception:
                    logging.exception("Lesion network mapping: the Circuits card could not show the new circuit")
            self.message(_("Circuit {name} made from the VLSM cluster (Circuits card)").format(name=name))
        self.showSeedState()

    def cleanup(self):
        if self.seedJob is not None and not self.seedJob.isFinished():
            self.seedJob.spec.onDone = None
            self.seedJob.spec.onEvent = None
        super().cleanup()


def _removeUnfinished(folder):
    """Remove the circuit folder of a seed that did not finish (no circuit.json): seedFromCluster chose a new folder name,
    so it holds only that seed's partial files, and the next seed gets the same name again. The job log stays in logs/.
    """
    import shutil

    from LesionNetworkMappingLib import circuits

    try:
        if os.path.isdir(folder) and not os.path.exists(os.path.join(folder, circuits.CIRCUIT_JSON)):
            shutil.rmtree(folder)
    except OSError:
        logging.exception("Lesion network mapping: the unfinished circuit folder could not be removed")


def _whereText(x, y, z):
    """World coordinates with direction letters: "R 7 A 7 S 7" (RAS mm)."""

    def part(value, negative, positive):
        return f"{positive if value >= 0 else negative} {abs(value):.0f}"

    return " ".join([part(x, "L", "R"), part(y, "P", "A"), part(z, "I", "S")])


def _vlsmStatusText(job):
    """Status chip text of a VLSM job: "VLSM 1200/5000 · 3 min left"."""
    from . import jobrunner

    event = job.lastEvent or {}
    text = _("VLSM")
    if (job.stage or {}).get("name") == "permutations" and event.get("event") == "progress" and event.get("total"):
        text += f" {event.get('done')}/{event.get('total')}"
    elif job.progress is not None:
        text += f" {int(job.progress * 100)}%"
    eta = jobrunner.formatEta(job.eta)
    return f"{text} · {eta}" if eta else text


TAB = {
    "id": "vlsm",
    "title": "VLSM",
    "order": ORDER_VLSM,
    "factory": VlsmTab,
}

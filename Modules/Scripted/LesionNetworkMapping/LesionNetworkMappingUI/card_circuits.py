"""Circuits card of the Map module (order 120): re-seed a binary result as a circuit, damage scores and target scores (L-41).

- **Seed this.** The current binary result becomes the seed of an engine run (``circuits seed``, a job): the conjunction
  map of the Cohort card, the cluster selected in the Clusters card, or a sphere around a peak (the selected cluster's
  peak, else the crosshair) with a radius. A signed conjunction map (+1 / -1) seeds one sign at a time (positive by
  default): the positively and the negatively connected regions are two circuits, never one seed. The circuit maps (``circuit-<name>_conn-<label>_desc-T…``) and circuit.json
  go to ``<run root>/circuits/circuit-<name>/``; circuit.json links the parent map, its run and its lesions.
- **Circuit.** The circuits of the run root (or a circuit file opened here); "Show" shows its T map as the Result map.
- **Damage scores.** Each lesion of the cohort maps against the circuit (sum, mean, % in the circuit), written to
  damage.tsv and posted to the study as ``dmg_<name>`` columns. The provenance chip says whether the circuit is frozen
  (independent of these lesions) or same-cohort; an association with an outcome is refused for a same-cohort circuit
  (double dipping) and the card points to Validate.
- **Targets.** User-given targets (a markups point list or a CSV of label, x, y, z, radius) are scored against random
  gray-matter spheres of the same radius (GM prior of the circuit's template family, downloaded only on request).
  Nothing is proposed: the table lists the given targets only, and the research-only label is always visible.

Works in Classic and without a main window; nothing is computed until a button is pressed.
"""

import logging
import math
import os
import threading

import qt
import slicer
from slicer.i18n import tr as _

from . import ORDER_CIRCUITS, widgets

#: Fork-only settings (SPEC Appendix B): the radius of peak seeds and the default radius of targets
SEED_RADIUS_SETTING = "Studio/LNM/Circuits/SeedRadiusMm"
TARGET_RADIUS_SETTING = "Studio/LNM/Circuits/TargetRadiusMm"
DEFAULT_SEED_RADIUS_MM = 6.0
SOURCES = ("conjunction", "cluster", "peak")
SOURCE_TITLES = {"conjunction": _("Conjunction map"), "cluster": _("Selected cluster"), "peak": _("Peak sphere")}
#: Parts of a signed (+1 / -1) conjunction or sensitivity map that can be seeded (one at a time)
SEED_SIGNS = (("pos", _("Positive part")), ("neg", _("Negative part")))
SIGN_TEXT = {"pos": _("positive voxels"), "neg": _("negative voxels"), "both": _("non-zero voxels")}
SIGN_SUFFIX = {"pos": "Pos", "neg": "Neg", "both": ""}
CIRCUITS_FOLDER = "circuits"
ROW_HEIGHT = 22
VISIBLE_ROWS = 8
TABLE_FONT_PX = 11
#: Catalog ids of the GM priors of each template family (TemplateFlow probseg; downloaded only on request)
GM_PRIORS = {"2009": "nlin2009casym-res02-gm-probseg", "fsl6": "nlin6asym-res02-gm-probseg"}
RESEARCH_TEXT = _("Research use only. Circuits are made from your own results: damage scores of the lesions that made a circuit are "
                  "descriptive, and targets are only scored against chance — no stimulation site is proposed.")
METRIC_NAMES = {"auc": "AUC", "or_per_sd": _("OR per SD"), "r": "r"}


def mapWidget():
    return getattr(slicer.modules, "LesionNetworkMappingWidget", None)


def _settings():
    return qt.QSettings()


def _readRadius(key, default):
    try:
        value = float(str(_settings().value(key)).strip())
    except (TypeError, ValueError):
        return default
    return value if 0.0 < value <= 30.0 else default


def _label(objectName, role=None):
    label = qt.QLabel("")
    label.setObjectName(objectName)
    label.setWordWrap(True)
    label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
    label.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
    if role:
        label.setProperty("slicerRole", role)
    return label


def _button(text, objectName, role="secondary", tip=None):
    button = qt.QPushButton(text)
    button.setObjectName(objectName)
    button.setProperty("slicerRole", role)
    if tip:
        button.setToolTip(tip)
    return button


def _number(value, digits=3):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return f"{number:.{digits}g}" if math.isfinite(number) else "n/a"


def whereText(x, y, z):
    """"L 38 · A 44 · S 26": direction letters, never a minus sign (USABILITY §7.5); RAS (MNI) mm."""
    def part(value, negative, positive):
        return f"{negative if value < 0 else positive} {abs(value):.0f}"

    return " · ".join((part(x, "L", "R"), part(y, "P", "A"), part(z, "I", "S")))


def _exec(dialog):
    run = getattr(dialog, "exec_", None) or dialog.exec  # Qt 5 PythonQt: exec_, Qt 6: exec
    return run()


def _table(columns, objectName):
    table = widgets.tableView([title for title, _tip in columns])
    table.setObjectName(objectName)
    for column, (_title, tip) in enumerate(columns):
        item = table.horizontalHeaderItem(column)
        if item is not None and tip:
            item.setToolTip(tip)
    table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
    header = table.horizontalHeader()
    header.setSectionResizeMode(qt.QHeaderView.ResizeToContents)
    header.setSectionResizeMode(0, qt.QHeaderView.Stretch)  # the name column takes the rest: no horizontal scroll
    widgets._font(table, TABLE_FONT_PX)
    table.hide()
    return table


def _setRows(table, rows):
    """Fill a table widget with rows of (text, tooltip) cells."""
    table.setRowCount(0)
    table.setRowCount(len(rows))
    for rowIndex, cells in enumerate(rows):
        for column, (text, tip) in enumerate(cells):
            item = qt.QTableWidgetItem(text)
            if tip:
                item.setToolTip(tip)
            table.setItem(rowIndex, column, item)
    table.setFixedHeight(ROW_HEIGHT * (min(len(rows), VISIBLE_ROWS) + 1) + 8)  # the rows, up to VISIBLE_ROWS, then a scroll bar
    table.setVisible(bool(rows))


class SeedSource:
    """What "Seed this" would map: an ROI file or a peak, the parent map and a name; ``error`` says why it cannot."""

    def __init__(self, kind, roi=None, peak=None, parent=None, name="", text="", error=None, roiData=None, sign="both", signed=False):
        self.kind = kind
        self.sign = sign  # the voxels of ``roi`` to seed: "both" (non-zero), "pos" or "neg"
        self.signed = signed  # ``roi`` holds positive and negative voxels: the sign is the user's choice
        self.roi = roi
        self.peak = peak
        self.parent = parent
        self.name = name
        self.text = text
        self.error = error
        self.roiData = roiData  # (data, affine) of a cluster mask, written when the job starts


class CircuitsCard:
    """Controller of the Circuits card (``widget`` is a collapsible card)."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.job = None
        self.circuits = []  # circuit.json records (absolute paths)
        self.openedFiles = []
        self.damage = None  # the damage_scores result on show
        self.targets = None  # the score_targets result on show
        self.notice = ""
        self.gmFile = None
        self.gmFetch = None
        self._signCache = {}  # path -> ((mtime, size), (has positive, has negative)) of group maps
        self._clustersConnected = False
        self._cleanedUp = False
        self.widget = widgets.card(_("Circuits"), objectName="LNMCircuitsCard", collapsed=True)
        layout = qt.QVBoxLayout(self.widget)

        # Research-only label: always visible at the top of the card
        researchRow = qt.QHBoxLayout()
        self.researchChip = widgets.chip(_("Research only"), "warn")
        self.researchChip.setObjectName("LNMCircuitsResearchChip")
        researchRow.addWidget(self.researchChip, 0, qt.Qt.AlignTop)
        self.researchLabel = _label("LNMCircuitsResearchText", "secondaryText")
        self.researchLabel.setText(RESEARCH_TEXT)
        researchRow.addWidget(self.researchLabel, 1)
        layout.addLayout(researchRow)

        # Seed this
        layout.addWidget(self._title(_("Seed this")))
        sourceRow = qt.QHBoxLayout()
        self.sourceCombo = qt.QComboBox()
        self.sourceCombo.setObjectName("LNMCircuitsSourceCombo")
        for key in SOURCES:
            self.sourceCombo.addItem(SOURCE_TITLES[key], key)
        self.sourceCombo.setToolTip(_("The binary result to map as a seed: the Cohort card's conjunction map, the cluster selected in the Clusters card, or a sphere around a peak"))
        self.sourceCombo.connect("currentIndexChanged(int)", self.refreshSource)
        sourceRow.addWidget(self.sourceCombo, 1)
        self.signCombo = qt.QComboBox()
        self.signCombo.setObjectName("LNMCircuitsSignCombo")
        for key, title in SEED_SIGNS:
            self.signCombo.addItem(title, key)
        self.signCombo.setToolTip(_("The map holds a positively (+1) and a negatively (-1) connected region: seed one of them (each is its own circuit)"))
        self.signCombo.connect("currentIndexChanged(int)", self.refreshSource)
        self.signCombo.hide()
        sourceRow.addWidget(self.signCombo)
        self.radiusSpin = qt.QDoubleSpinBox()
        self.radiusSpin.setObjectName("LNMCircuitsRadiusSpinBox")
        self.radiusSpin.setRange(1.0, 30.0)
        self.radiusSpin.setDecimals(0)
        self.radiusSpin.setSuffix(_(" mm"))
        self.radiusSpin.setValue(_readRadius(SEED_RADIUS_SETTING, DEFAULT_SEED_RADIUS_MM))
        self.radiusSpin.setToolTip(_("Radius of the sphere around the peak"))
        self.radiusSpin.connect("valueChanged(double)", self._onSeedRadius)
        sourceRow.addWidget(self.radiusSpin)
        layout.addLayout(sourceRow)
        nameRow = qt.QHBoxLayout()
        nameRow.addWidget(qt.QLabel(_("Name")))
        self.nameEdit = qt.QLineEdit()
        self.nameEdit.setObjectName("LNMCircuitsNameEdit")
        self.nameEdit.setPlaceholderText(_("letters and digits, e.g. ConjDepression"))
        self.nameEdit.setToolTip(_("The circuit's name: its files are circuit-<name>_…, its damage scores dmg_<name>"))
        self.nameEdit.connect("textEdited(QString)", lambda text: self.nameEdit.setProperty("autoName", False))
        nameRow.addWidget(self.nameEdit, 1)
        layout.addLayout(nameRow)
        self.sourceLabel = _label("LNMCircuitsSourceText", "secondaryText")
        layout.addWidget(self.sourceLabel)
        seedRow = qt.QHBoxLayout()
        self.seedButton = _button(_("Seed this"), "LNMCircuitsSeedButton", tip=_("Map the binary result as a seed with the connectome (an engine run): the circuit's T and Fz maps"))
        self.seedButton.connect("clicked()", self.seedThis)
        seedRow.addWidget(self.seedButton)
        seedRow.addStretch(1)
        layout.addLayout(seedRow)
        self.seedProgress = widgets.progressRow(objectNamePrefix="LNMCircuitsSeed")
        self.seedProgress.widget.hide()
        layout.addWidget(self.seedProgress.widget)
        self.noticeLabel = _label("LNMCircuitsNotice", "secondaryText")
        self.noticeLabel.hide()
        layout.addWidget(self.noticeLabel)

        # Circuit
        layout.addWidget(self._title(_("Circuit")))
        circuitRow = qt.QHBoxLayout()
        self.circuitCombo = qt.QComboBox()
        self.circuitCombo.setObjectName("LNMCircuitsCircuitCombo")
        self.circuitCombo.setToolTip(_("The circuits made here (in the study's derivatives/lnm/circuits), or a circuit map opened from a file"))
        # Circuit names may be long: the combo shares the row with two buttons and never widens the inspector (its size
        # hint is the width of its longest item)
        self.circuitCombo.setSizeAdjustPolicy(qt.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.circuitCombo.setMinimumContentsLength(8)
        self.circuitCombo.connect("currentIndexChanged(int)", self._onCircuitChanged)
        circuitRow.addWidget(self.circuitCombo, 1)
        self.openButton = _button(_("Open…"), "LNMCircuitsOpenButton", "ghost", _("Open a circuit map (a NIfTI on the connectome grid)"))
        self.openButton.connect("clicked()", self.openCircuit)
        circuitRow.addWidget(self.openButton)
        self.showButton = _button(_("Show"), "LNMCircuitsShowButton", "ghost", _("Show the circuit's T map as the Result map"))
        self.showButton.connect("clicked()", self.showCircuit)
        circuitRow.addWidget(self.showButton)
        layout.addLayout(circuitRow)
        self.circuitLabel = _label("LNMCircuitsCircuitText", "secondaryText")
        layout.addWidget(self.circuitLabel)

        # Damage scores
        layout.addWidget(self._title(_("Damage scores")))
        damageRow = qt.QHBoxLayout()
        damageRow.addWidget(qt.QLabel(_("In circuit when |value| ≥")))
        self.thresholdSpin = qt.QDoubleSpinBox()
        self.thresholdSpin.setObjectName("LNMCircuitsThresholdSpinBox")
        self.thresholdSpin.setRange(0.0, 1000.0)
        self.thresholdSpin.setDecimals(2)
        self.thresholdSpin.setSpecialValueText(_("auto"))
        self.thresholdSpin.setToolTip(_("Threshold of the % of each lesion inside the circuit; auto: the circuit's voxels for a binary circuit, no % otherwise"))
        damageRow.addWidget(self.thresholdSpin)
        damageRow.addStretch(1)
        layout.addLayout(damageRow)
        frozenRow = qt.QHBoxLayout()
        frozenRow.addWidget(qt.QLabel(_("Frozen sha256")))
        self.frozenEdit = qt.QLineEdit()
        self.frozenEdit.setObjectName("LNMCircuitsFrozenEdit")
        self.frozenEdit.setPlaceholderText(_("only for a circuit fixed by an independent study"))
        self.frozenEdit.setToolTip(_("Declare the circuit frozen: paste the sha256 it was frozen with. A circuit made from these lesions stays same-cohort."))
        frozenRow.addWidget(self.frozenEdit, 1)
        layout.addLayout(frozenRow)
        scoreRow = qt.QHBoxLayout()
        self.damageButton = _button(_("Score lesions"), "LNMCircuitsDamageButton", tip=_("Score each lesion of the cohort maps against the circuit; post the scores to the study as dmg_<name> columns"))
        self.damageButton.connect("clicked()", self.scoreDamage)
        scoreRow.addWidget(self.damageButton)
        self.provenanceChip = widgets.chip(_("Provenance: not scored"), "neutral")
        self.provenanceChip.setObjectName("LNMCircuitsProvenanceChip")
        scoreRow.addWidget(self.provenanceChip)
        scoreRow.addStretch(1)
        layout.addLayout(scoreRow)
        self.damageLabel = _label("LNMCircuitsDamageText", "secondaryText")
        layout.addWidget(self.damageLabel)
        self.damageColumns = [
            (_("Participant"), ""),
            (_("Sum"), _("Sum over the lesion of occupancy x circuit (the sum of T over the lesion for a T circuit)")),
            (_("Mean"), _("Occupancy-weighted mean of the circuit within the lesion")),
            (_("% in"), _("Percentage of the lesion inside the thresholded circuit")),
        ]
        self.damageTable = _table(self.damageColumns, "LNMCircuitsDamageTable")
        layout.addWidget(self.damageTable)
        self.circularBanner = widgets.banner("", [(_("Open Validate"), self.openValidate)], level="warn")
        self.circularBanner.setObjectName("LNMCircuitsCircularBanner")
        self.circularBanner.hide()
        layout.addWidget(self.circularBanner)
        associationRow = qt.QHBoxLayout()
        associationRow.addWidget(qt.QLabel(_("Outcome")))
        self.outcomeCombo = qt.QComboBox()
        self.outcomeCombo.setObjectName("LNMCircuitsOutcomeCombo")
        self.outcomeCombo.setToolTip(_("A participants column to test the damage scores against (frozen circuits only)"))
        associationRow.addWidget(self.outcomeCombo, 1)
        self.associationButton = _button(_("Test association"), "LNMCircuitsAssociationButton", tip=_("AUC / r of the damage scores with the outcome, with bootstrap CIs (frozen circuits only)"))
        self.associationButton.connect("clicked()", self.testAssociation)
        associationRow.addWidget(self.associationButton)
        layout.addLayout(associationRow)
        self.associationLabel = _label("LNMCircuitsAssociationText", "secondaryText")
        self.associationLabel.hide()
        layout.addWidget(self.associationLabel)

        # Targets
        layout.addWidget(self._title(_("Targets")))
        targetRow = qt.QHBoxLayout()
        self.markupsCombo = slicer.qMRMLNodeComboBox()
        self.markupsCombo.setObjectName("LNMCircuitsMarkupsCombo")
        self.markupsCombo.nodeTypes = ["vtkMRMLMarkupsFiducialNode"]
        self.markupsCombo.noneEnabled = True
        self.markupsCombo.addEnabled = False
        self.markupsCombo.removeEnabled = False
        self.markupsCombo.showChildNodeTypes = False
        self.markupsCombo.setMRMLScene(slicer.mrmlScene)
        self.markupsCombo.setToolTip(_("A point list whose points are the targets (their labels name them; RAS/MNI mm)"))
        self.markupsCombo.connect("currentNodeChanged(vtkMRMLNode*)", self._onTargetsSourceChanged)
        targetRow.addWidget(self.markupsCombo, 1)
        self.importButton = _button(_("Import CSV…"), "LNMCircuitsImportButton", "ghost", _("A CSV or TSV with label, x, y, z and radius (RAS/MNI mm), or a markups .fcsv or .mrk.json file"))
        self.importButton.connect("clicked()", self.importTargets)
        targetRow.addWidget(self.importButton)
        layout.addLayout(targetRow)
        self.targetsFile = None
        self.targetsFileLabel = _label("LNMCircuitsTargetsFile", "secondaryText")
        self.targetsFileLabel.hide()
        layout.addWidget(self.targetsFileLabel)
        radiusRow = qt.QHBoxLayout()
        radiusRow.addWidget(qt.QLabel(_("Radius of targets without one")))
        self.targetRadiusSpin = qt.QDoubleSpinBox()
        self.targetRadiusSpin.setObjectName("LNMCircuitsTargetRadiusSpinBox")
        self.targetRadiusSpin.setRange(1.0, 30.0)
        self.targetRadiusSpin.setDecimals(0)
        self.targetRadiusSpin.setSuffix(_(" mm"))
        self.targetRadiusSpin.setValue(_readRadius(TARGET_RADIUS_SETTING, 5.0))
        self.targetRadiusSpin.connect("valueChanged(double)", lambda value: _settings().setValue(TARGET_RADIUS_SETTING, f"{value:g}"))
        radiusRow.addWidget(self.targetRadiusSpin)
        radiusRow.addStretch(1)
        layout.addLayout(radiusRow)
        gmRow = qt.QHBoxLayout()
        self.gmLabel = _label("LNMCircuitsGmText", "secondaryText")
        gmRow.addWidget(self.gmLabel, 1)
        self.getGmButton = _button(_("Get GM prior…"), "LNMCircuitsGetGmButton", "ghost", _("Download the TemplateFlow gray-matter probability of the circuit's template family (about 1 MB)"))
        self.getGmButton.connect("clicked()", self.getGmPrior)
        gmRow.addWidget(self.getGmButton)
        self.chooseGmButton = _button(_("Choose…"), "LNMCircuitsChooseGmButton", "ghost", _("A gray-matter probability NIfTI (TemplateFlow name tpl-<space>_…_label-GM_probseg) of the circuit's family"))
        self.chooseGmButton.connect("clicked()", self.chooseGmPrior)
        gmRow.addWidget(self.chooseGmButton)
        layout.addLayout(gmRow)
        self.targetsButton = _button(_("Score targets"), "LNMCircuitsTargetsButton", tip=_("Mean and peak circuit value in each target's sphere and their percentiles among 1000 random gray-matter spheres of the same radius"))
        self.targetsButton.connect("clicked()", self.scoreTargets)
        layout.addWidget(self.targetsButton)
        self.targetsLabel = _label("LNMCircuitsTargetsText", "secondaryText")
        layout.addWidget(self.targetsLabel)
        self.targetColumns = [
            (_("Target"), ""),
            (_("Where"), _("Centre of the sphere (RAS/MNI mm): L/R, P/A, I/S")),
            (_("Mean"), _("Mean circuit value in the sphere")),
            (_("Peak"), _("Circuit value of largest magnitude in the sphere")),
            (_("Pctl"), _("Percentile of the mean among random gray-matter spheres of the same radius")),
            (_("Sign"), _("Sign of the mean")),
        ]
        self.targetsTable = _table(self.targetColumns, "LNMCircuitsTargetsTable")
        layout.addWidget(self.targetsTable)
        self.targetsCaption = _label("LNMCircuitsTargetsCaption", "secondaryText")
        from LesionNetworkMappingLib import circuits

        self.targetsCaption.setText(circuits.TARGETS_CAPTION)
        layout.addWidget(self.targetsCaption)

        self.gmTimer = qt.QTimer()
        self.gmTimer.setInterval(150)
        self.gmTimer.connect("timeout()", self._pollGmFetch)
        try:
            self.widget.connect("contentsCollapsed(bool)", self._onCollapsed)
        except Exception:
            logging.debug("Lesion network mapping: the Circuits card cannot follow its expansion", exc_info=True)
        for name in ("resultChanged", "bundleChanged", "studyChanged"):
            getattr(ctx, name).append(self.refresh)
        self.refresh()

    @staticmethod
    def _title(text):
        label = qt.QLabel(text)
        label.setProperty("slicerRole", "secondaryText")
        widgets._font(label, 12, bold=True)
        return label

    #
    # Card protocol
    #

    def onRunDone(self, job=None):
        self.refresh()

    def onCohortMapsChanged(self, path=None):
        self.damage = None
        self._showDamage()
        self.refresh()

    def onStudyChanged(self):
        self.refresh()

    def cleanup(self):
        self._cleanedUp = True
        for name in ("resultChanged", "bundleChanged", "studyChanged"):
            callbacks = getattr(self.ctx, name, [])
            if self.refresh in callbacks:
                callbacks.remove(self.refresh)
        self.gmTimer.stop()
        if self.job is not None and hasattr(self.job, "isFinished") and not self.job.isFinished():
            self.job.spec.onDone = None
            self.job.spec.onEvent = None
        self.job = None
        try:
            self.markupsCombo.setMRMLScene(None)
        except Exception:
            pass

    def expanded(self):
        try:
            return not bool(self.widget.collapsed)
        except (ValueError, RuntimeError):
            return False

    def _onCollapsed(self, collapsed):
        if not collapsed:
            self.refresh()

    #
    # Places
    #

    def circuitsFolder(self):
        """``<run root>/circuits`` (the study's derivatives/lnm/circuits, else the default run root)."""
        try:
            return os.path.join(self.ctx.runRoot(), CIRCUITS_FOLDER)
        except Exception:
            return None

    def studyPath(self):
        study = self.ctx.study
        return getattr(study, "path", None) if study is not None else None

    def registryRoot(self):
        """Root of the run registry (``<root>/derivatives/lnm/runs.jsonl``): the study, else the default run root."""
        return self.studyPath() or self.ctx.runRoot()

    def bundlePath(self):
        bundle = self.ctx.bundle
        return getattr(bundle, "path", None) if bundle is not None else None

    def currentMapsTsv(self):
        """The maps.tsv of the cohort maps (the Cohort card's), or None."""
        card = self.ctx.card("cohort")
        if card is not None and hasattr(card, "currentMapsTsv"):
            try:
                return card.currentMapsTsv()
            except Exception:
                logging.exception("Lesion network mapping: the cohort maps could not be found")
        path = getattr(self.ctx, "cohortMaps", None)
        return path if path and os.path.isfile(path) else None

    #
    # State
    #

    def refresh(self, *args):
        """Update the labels and the enabled state (cheap; nothing is computed)."""
        if self._cleanedUp:
            return
        try:
            self.widget.objectName
        except (ValueError, RuntimeError):
            return
        self._connectClusters()
        self._refreshCircuits()
        self.refreshSource()
        self._refreshDamageState()
        self._refreshOutcomes()
        self._refreshGm()
        self._refreshTargetsState()
        self.noticeLabel.setText(self.notice)
        self.noticeLabel.setVisible(bool(self.notice))

    def _connectClusters(self):
        if self._clustersConnected:
            return
        card = self.ctx.card("clusters")
        view = getattr(card, "view", None)
        if view is None:
            return
        try:
            view.selectionModel().connect("currentRowChanged(QModelIndex,QModelIndex)", lambda *args: self.refreshSource())
            self._clustersConnected = True
        except Exception:
            logging.debug("Lesion network mapping: the Circuits card cannot follow the cluster selection", exc_info=True)

    def _onSeedRadius(self, value):
        _settings().setValue(SEED_RADIUS_SETTING, f"{value:g}")
        self.refreshSource()

    def sourceKind(self):
        return self.sourceCombo.currentData or SOURCES[max(self.sourceCombo.currentIndex, 0)]

    def selectedCluster(self):
        """(row dict, ClusterTable, MapSource) of the row selected in the Clusters card, or (None, None, None)."""
        card = self.ctx.card("clusters")
        table = getattr(card, "table", None)
        source = getattr(card, "source", None)
        view = getattr(card, "view", None)
        rows = getattr(card, "displayRows", None) or []
        if table is None or source is None or view is None:
            return None, None, None
        index = view.currentIndex()
        row = index.row() if index is not None and index.isValid() else -1
        if not 0 <= row < len(rows):
            return None, table, source
        return rows[row], table, source

    def _groupMapPath(self):
        """(path, desc) of the group map shown by the Cohort card when it is a binary (conjunction or sensitivity) map."""
        from LesionNetworkMappingLib import display

        node = None
        for candidate in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
            if candidate.GetAttribute("LNM.GroupMapVolume") == "1" and candidate.GetImageData() is not None:
                node = candidate
                break
        desc = node.GetAttribute(display.GROUP_MAP_ATTRIBUTE) if node is not None else None
        widget = mapWidget()
        mapsPath = getattr(widget, "cohortMapsPath", None) if widget is not None else None
        folder = os.path.dirname(mapsPath) if mapsPath else None
        if desc and display.groupMapKind(desc) != "binary":
            return None, desc
        names = sorted(os.listdir(folder)) if folder and os.path.isdir(folder) else []
        wanted = [desc] if desc else ["conj", "sens"]
        for prefix in wanted:
            for name in names:
                if name.startswith("group_") and f"_desc-{prefix}" in name and name.endswith("_map.nii.gz"):
                    return os.path.join(folder, name), name.split("_desc-", 1)[1].split("_", 1)[0]
        return None, desc

    def _resultMapPath(self):
        """The file of the current result's T map (the parent of a cluster or peak seed from the Result map), or None."""
        run = self.ctx.currentRun
        for path in getattr(run, "outputs", None) or []:
            if "_desc-T" in os.path.basename(path) and os.path.isfile(path):
                return path
        return None

    def mapSigns(self, path):
        """(has positive voxels, has negative voxels) of a group map file, read once per file version."""
        from LesionNetworkMappingLib import circuits

        try:
            info = os.stat(path)
        except OSError:
            return False, False
        stamp = (info.st_mtime_ns, info.st_size)
        cached = self._signCache.get(path)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        data = circuits._volume_of(path)[0]
        signs = (bool((data > 0).any()), bool((data < 0).any()))
        self._signCache = {path: (stamp, signs)}  # only the current group map is kept
        return signs

    def seedSign(self):
        return self.signCombo.currentData or SEED_SIGNS[max(self.signCombo.currentIndex, 0)][0]

    def seedSource(self):
        """The :class:`SeedSource` of the chosen kind (with ``error`` when it cannot be seeded now)."""
        import numpy as np

        kind = self.sourceKind()
        if kind == "conjunction":
            from LesionNetworkMappingLib import display

            path, desc = self._groupMapPath()
            if path is None:
                if not desc:
                    error = _("No binary group map: make the conjunction (or sensitivity) map in the Cohort card.")
                elif display.groupMapKind(desc) != "binary":
                    error = _("The Cohort card shows a {desc} map, not a binary map: show its conjunction map.").format(desc=desc)
                else:
                    error = _("The {desc} map shown is not in the cohort maps' folder: make it again in the Cohort card.").format(desc=desc)
                return SeedSource(kind, error=error)
            # A signed map (+1 / -1, the Cohort card's sign "both") seeds one sign at a time; an unsigned one its non-zero voxels
            positive, negative = self.mapSigns(path)
            signed = positive and negative
            sign = self.seedSign() if signed else ("neg" if negative else "both")
            name = ("Conj" if desc.startswith("conj") else "Sens") + SIGN_SUFFIX[sign]
            text = _("Seed: {name} ({part}).").format(name=os.path.basename(path), part=SIGN_TEXT[sign])
            if signed:
                text += " " + _("The map has a positive and a negative region: each is seeded as its own circuit.")
            return SeedSource(kind, roi=path, parent=path, name=name, text=text, sign=sign, signed=signed)
        row, table, source = self.selectedCluster()
        if kind == "cluster":
            if table is None:
                return SeedSource(kind, error=_("No cluster table: open the Clusters card and make its table."))
            if row is None:
                return SeedSource(kind, error=_("Select a cluster in the Clusters card."))
            mask = (np.asarray(table.cluster_vector).reshape(source.grid.shape) == int(row["cluster"])).astype(np.uint8)
            parent = getattr(source, "path", None) or (self._resultMapPath() if source.key == "result" else None)
            text = _("Seed: cluster {id} of {map} ({voxels:,} voxels, {threshold}; peak {where}).").format(
                id=row["cluster"], map=source.name, voxels=int(mask.sum()), threshold=source.thresholdText(), where=whereText(*row["xyz"]))
            return SeedSource(kind, parent=parent, name=f"Cluster{row['cluster']}", text=text, roiData=(mask, np.asarray(source.grid.affine)))
        # Peak sphere: the selected cluster's peak, else the crosshair
        radius = float(self.radiusSpin.value)
        if row is not None:
            peak = tuple(float(v) for v in row["xyz"])
            parent = getattr(source, "path", None) or (self._resultMapPath() if source.key == "result" else None)
            origin = _("peak of cluster {id}").format(id=row["id"])
        else:
            crosshair = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLCrosshairNode") if slicer.mrmlScene is not None else None
            if crosshair is None:
                return SeedSource(kind, error=_("Select a cluster in the Clusters card, or place the crosshair on the peak."))
            peak = tuple(float(v) for v in crosshair.GetCrosshairRAS())
            parent = self._resultMapPath()
            origin = _("crosshair")
        text = _("Seed: a {radius:g} mm sphere at {where} ({origin}).").format(radius=radius, where=whereText(*peak), origin=origin)
        return SeedSource(kind, peak=peak, parent=parent, name="Peak", text=text)

    def refreshSource(self, *args):
        self.radiusSpin.setVisible(self.sourceKind() == "peak")
        try:
            source = self.seedSource()
        except Exception as exc:
            logging.exception("Lesion network mapping: the seed of the Circuits card could not be read")
            source = SeedSource(self.sourceKind(), error=_("The seed could not be read: {error}").format(error=exc))
        self.signCombo.setVisible(source.signed)
        self.sourceLabel.setText(source.error or source.text)
        if not self.nameEdit.text or self.nameEdit.property("autoName"):
            self.nameEdit.setText(source.name)
            self.nameEdit.setProperty("autoName", True)
        busy = self.job is not None and not self.job.isFinished()
        reason = ""
        if source.error:
            reason = source.error
        elif self.bundlePath() is None:
            reason = _("Choose a connectome in the Connectome card.")
        elif busy:
            reason = _("A circuit is being mapped.")
        self.seedButton.setEnabled(not reason)
        self.seedButton.setToolTip(reason or _("Map the binary result as a seed with the connectome (an engine run): the circuit's T and Fz maps"))
        return source

    #
    # Seed this
    #

    def _uniqueFolder(self, name):
        folder = self.circuitsFolder()
        candidate, counter = name, 2
        while os.path.exists(os.path.join(folder, f"circuit-{candidate}")):
            candidate, counter = f"{name}{counter}", counter + 1
        return candidate, os.path.join(folder, f"circuit-{candidate}")

    def seedThis(self):
        """Start the seed job of the current source; return the job (None with a notice when it cannot start)."""
        from LesionNetworkMappingLib import circuits
        from LesionNetworkMappingLib.nifti import write_nifti

        from . import jobrunner

        source = self.refreshSource()
        if source.error or self.bundlePath() is None:
            return None
        try:
            name = circuits.circuit_name(self.nameEdit.text or source.name)
        except ValueError as exc:
            self._setNotice(str(exc))
            return None
        name, out = self._uniqueFolder(name)
        os.makedirs(out, exist_ok=True)
        args = ["seed", "--bundle", self.bundlePath(), "--out", out, "--name", name]
        if source.roiData is not None:
            roiPath = os.path.join(out, "parent_cluster_mask.nii.gz")
            write_nifti(roiPath, source.roiData[0], source.roiData[1], descrip="LNM cluster selected for a circuit")
            args += ["--roi", roiPath]
        elif source.roi is not None:
            args += ["--roi", source.roi, "--sign", source.sign]
        else:
            args += ["--peak", *[f"{v:.4f}" for v in source.peak], "--radius", f"{float(self.radiusSpin.value):g}"]
        if source.parent:
            args += ["--parent", source.parent]
        mapsTsv = self.currentMapsTsv()
        if mapsTsv and self.sourceKind() == "conjunction":
            args += ["--source-maps", mapsTsv]
        args += ["--run-root", self.registryRoot()]
        spec = jobrunner.JobSpec("LesionNetworkMappingLib.circuits", args, title=_("Circuit"), heavy=True, kind="circuits", name="circuits", runRoot=os.path.dirname(out),
                                 onEvent=lambda event: self.seedProgress.update(), onDone=lambda job, out=out: self._onSeedDone(job, out))
        self.job = self.ctx.submit(spec)
        self.seedProgress.setJob(self.job)
        self.seedProgress.widget.show()
        self._setNotice("")
        self.refreshSource()
        return self.job

    def _onSeedDone(self, job, out):
        if self._cleanedUp:
            return
        self.seedProgress.update()
        if job.state == "failed":
            self._setNotice(_("The circuit could not be made: {reason}").format(reason=job.failureReason))
        elif job.state == "cancelled":
            self._setNotice(_("Stopped: a circuit that did not finish has no maps."))
        else:
            self.seedProgress.widget.hide()
            self._setNotice(_("Circuit made: {folder}.").format(folder=os.path.basename(out)))
        self.nameEdit.setProperty("autoName", True)
        self.refresh()
        if job.succeeded():
            self.selectCircuit(os.path.join(out, "circuit.json"))

    def _setNotice(self, text):
        self.notice = text or ""
        self.noticeLabel.setText(self.notice)
        self.noticeLabel.setVisible(bool(self.notice))

    #
    # Circuits
    #

    def _refreshCircuits(self):
        from LesionNetworkMappingLib import circuits

        folder = self.circuitsFolder()
        records = circuits.find_circuits(folder) if folder else []
        current = self.currentCircuitPath()
        self.circuits = records
        self.circuitCombo.blockSignals(True)
        try:
            self.circuitCombo.clear()
            for record in records:
                self.circuitCombo.addItem(_("circuit-{name} · N={n}").format(name=record.get("name"), n=record.get("n")), record["maps"].get("T"))
            for path in self.openedFiles:
                if self.circuitCombo.findData(path) >= 0:
                    continue
                opened = circuits.read_circuit(path)
                label = _("circuit-{name} · opened").format(name=opened["name"]) if opened else _("File · {name}").format(name=os.path.basename(path))
                self.circuitCombo.addItem(label, path)
            if self.circuitCombo.count == 0:
                self.circuitCombo.addItem(_("No circuit yet: Seed this, or open a circuit map"), "")
            index = self.circuitCombo.findData(current) if current else -1
            self.circuitCombo.setCurrentIndex(max(index, 0))
        finally:
            self.circuitCombo.blockSignals(False)
        if self.currentCircuitPath() != current:
            self._onCircuitChanged()  # the circuit on show went away: its scores and targets go with it
            return
        self._showCircuitInfo()

    def currentCircuitPath(self):
        value = self.circuitCombo.currentData if self.circuitCombo.count else None
        return value or None

    def currentCircuitRecord(self):
        from LesionNetworkMappingLib import circuits

        path = self.currentCircuitPath()
        return circuits.read_circuit(path) if path else None

    def selectCircuit(self, path):
        """Choose a circuit by its circuit.json, folder or map; return whether it was found."""
        from LesionNetworkMappingLib import circuits

        record = circuits.read_circuit(path)
        target = record["maps"].get("T") if record else os.path.abspath(path)
        index = self.circuitCombo.findData(target)
        if index < 0:
            return False
        self.circuitCombo.setCurrentIndex(index)
        return True

    def _onCircuitChanged(self, index=None):
        self.frozenEdit.setText("")  # a frozen sha256 belongs to one circuit
        self.damage = None
        self.targets = None
        self._showDamage()
        self._showTargets()
        self._showCircuitInfo()
        self._refreshDamageState()
        self._refreshGm()
        self._refreshTargetsState()

    def _showCircuitInfo(self):
        record = self.currentCircuitRecord()
        path = self.currentCircuitPath()
        self.showButton.setEnabled(bool(path) and os.path.isfile(path))
        if not path:
            self.circuitLabel.setText("")
            return
        if record is None:
            self.circuitLabel.setText(_("A circuit map from a file: its provenance is not recorded (declare it frozen with its sha256 if an independent study fixed it)."))
            return
        parent = record.get("parent") or {}
        seed = record.get("seed") or {}
        parts = []
        if seed.get("kind") == "peak":
            peak = seed.get("peak_xyz") or (0, 0, 0)
            parts.append(_("{radius:g} mm sphere at {where}").format(radius=seed.get("radius_mm") or 0, where=whereText(*peak)))
        else:
            voxels = _("{voxels:,} seed voxels").format(voxels=int(seed.get("voxels_in_mask") or 0))
            if seed.get("sign") in ("pos", "neg"):
                voxels += " (" + SIGN_TEXT[seed["sign"]] + ")"
            parts.append(voxels)
        if parent.get("file"):
            parts.append(_("from {name}").format(name=os.path.basename(parent["file"])))
        if parent.get("run_id"):
            parts.append(_("run {id}").format(id=parent["run_id"]))
        source = record.get("source") or {}
        if source.get("known"):
            made = _("made from {n} lesion(s)").format(n=len(source.get("lesion_sha256") or []) or len(source.get("participants") or []))
            studyName = (source.get("study_key") or {}).get("name") or (os.path.basename(source["study"]) if source.get("study") else "")
            if studyName:
                made += " " + _("of study {name}").format(name=studyName)
            parts.append(made)
        bundle = record.get("bundle") or {}
        parts.append(f"{bundle.get('name') or '?'} · N={record.get('n')}")
        self.circuitLabel.setText(" · ".join(parts))

    def openCircuit(self, path=None):
        """Add a circuit map from a file (a dialog when ``path`` is None); return its path or None."""
        if path is None:
            path = qt.QFileDialog.getOpenFileName(slicer.util.mainWindow(), _("Open a circuit map"), self.circuitsFolder() or "", _("NIfTI (*.nii *.nii.gz)"))
        if not path:
            return None
        path = os.path.abspath(path)
        from LesionNetworkMappingLib import circuits

        record = circuits.read_circuit(path)
        if record is not None and record["maps"].get("T"):
            path = record["maps"]["T"]  # a circuit.json or another map of the circuit: its T map
        if path not in self.openedFiles:
            self.openedFiles.append(path)
        self._refreshCircuits()
        index = self.circuitCombo.findData(path)
        if index >= 0:
            self.circuitCombo.setCurrentIndex(index)
        return path

    def showCircuit(self):
        """Show the circuit's T map as the Result map (a file shown in the Map module); return the volume or None."""
        path = self.currentCircuitPath()
        widget = mapWidget()
        if not path or widget is None or not hasattr(widget, "showMapFile"):
            return None
        record = self.currentCircuitRecord()
        try:
            return widget.showMapFile(path, label=f"circuit-{record['name']}" if record else None)
        except Exception as exc:
            self._setNotice(_("The circuit could not be shown: {error}").format(error=exc))
            return None

    #
    # Damage scores
    #

    def _refreshDamageState(self):
        reason = ""
        if not self.currentCircuitPath():
            reason = _("Choose a circuit.")
        elif self.currentMapsTsv() is None:
            reason = _("Map the cohort's lesions first (Cohort card): damage scores need their occupancy.")
        self.damageButton.setEnabled(not reason)
        self.damageButton.setToolTip(reason or _("Score each lesion of the cohort maps against the circuit; post the scores to the study as dmg_<name> columns"))
        if self.damage is None:
            self.damageLabel.setText(reason)
        self._refreshAssociationState()

    def damageFolder(self):
        """``<circuit folder>/damage-<cohort folder name>`` (or next to an opened circuit file)."""
        path = self.currentCircuitPath()
        mapsTsv = self.currentMapsTsv()
        label = os.path.basename(os.path.dirname(mapsTsv)) if mapsTsv else "cohort"
        return os.path.join(os.path.dirname(path), f"damage-{label}") if path else None

    def scoreDamage(self):
        """Compute the damage scores of the cohort against the current circuit; return the result (None with a message)."""
        from LesionNetworkMappingLib import circuits

        path = self.currentCircuitPath()
        mapsTsv = self.currentMapsTsv()
        if not path or not mapsTsv:
            return None
        threshold = float(self.thresholdSpin.value) or None
        frozen = self.frozenEdit.text.strip() or None
        try:
            with slicer.util.WaitCursor():
                self.damage = circuits.damage_scores(mapsTsv, path, threshold=threshold, out_dir=self.damageFolder(), bundle=self.bundlePath(), frozen_sha256=frozen,
                                                     study=self.studyPath(), post=bool(self.studyPath()), run_root=self.registryRoot())
        except Exception as exc:
            logging.exception("Lesion network mapping: the damage scores could not be computed")
            self.damage = None
            self.damageLabel.setText(_("The damage scores could not be computed: {error}").format(error=exc))
            self._showDamage(keepText=True)
            return None
        self._showDamage()
        return self.damage

    def _showDamage(self, keepText=False):
        damage = self.damage
        self.associationLabel.hide()
        if damage is None:
            widgets.setChipLevel(self.provenanceChip, "neutral", _("Provenance: not scored"))
            self.damageTable.setRowCount(0)
            self.damageTable.hide()
            self.circularBanner.hide()
            if not keepText:
                self.damageLabel.setText("")
            self._refreshAssociationState()
            return
        provenance = damage["provenance"]
        if provenance["provenance"] == "frozen":
            widgets.setChipLevel(self.provenanceChip, "ok", _("Frozen · {sha}…").format(sha=provenance["sha256"][:8]))
            self.circularBanner.hide()
        else:
            widgets.setChipLevel(self.provenanceChip, "warn", _("Same cohort"))
            from LesionNetworkMappingLib import circuits

            widgets.setBannerText(self.circularBanner, circuits.CIRCULARITY_MESSAGE)
            self.circularBanner.show()
        self.provenanceChip.setToolTip(provenance["basis"])
        rows = []
        for row in damage["rows"]:
            rows.append([(row["participant_id"] or "", ""), (_number(row["sum"], 4), ""), (_number(row["mean"], 3), ""), (_number(row["pct"], 3), "")])
        _setRows(self.damageTable, rows)
        posted = _(" Posted to the study as {columns}.").format(columns=", ".join(damage["columns"].values())) if damage.get("inbox") else ""
        excluded = sum(len(members) for members in damage["excluded"].values())
        text = _("{n} lesion(s) scored ({basis}).").format(n=len(damage["rows"]), basis=provenance["basis"]) + posted
        if excluded:
            text += " " + _("{n} left out (no occupancy).").format(n=excluded)
        if damage.get("threshold") is None and not damage.get("binary"):
            text += " " + _("% in: set a threshold above (e.g. the Result card's T threshold); with auto it is computed for binary circuits only.")
        self.damageLabel.setText(text)
        self._refreshAssociationState()

    def _refreshOutcomes(self):
        study = self.ctx.study
        current = self.outcomeCombo.currentText
        self.outcomeCombo.blockSignals(True)
        try:
            self.outcomeCombo.clear()
            columns = list(getattr(study, "columns", []) or []) if study is not None else []
            skip = {"participant_id", "lesion", "include", "status"}
            for column in columns:
                if column not in skip and not column.startswith("dmg_"):
                    self.outcomeCombo.addItem(column)
            index = self.outcomeCombo.findText(current)
            if index >= 0:
                self.outcomeCombo.setCurrentIndex(index)
        finally:
            self.outcomeCombo.blockSignals(False)
        self._refreshAssociationState()

    def _refreshAssociationState(self):
        damage = self.damage
        reason = ""
        if damage is None:
            reason = _("Score the lesions first.")
        elif damage["provenance"]["provenance"] != "frozen":
            reason = _("Refused for a same-cohort circuit (double dipping): use Validate, or a frozen circuit.")
        elif damage.get("record") is None:
            reason = _("The damage scores were not written.")
        elif self.ctx.study is None or self.outcomeCombo.count == 0:
            reason = _("Open a study with an outcome column.")
        self.associationButton.setEnabled(not reason)
        self.associationButton.setToolTip(reason or _("AUC / r of the damage scores with the outcome, with bootstrap CIs (frozen circuits only)"))

    def testAssociation(self):
        """Test the damage scores against the chosen outcome (frozen circuits only); return the result or None."""
        from LesionNetworkMappingLib import circuits

        damage = self.damage
        if damage is None:
            return None
        outcome = self.outcomeCombo.currentText
        try:
            with slicer.util.WaitCursor():
                found = circuits.association(damage["record"], self.studyPath(), outcome)
        except circuits.CircularityError as exc:
            widgets.setBannerText(self.circularBanner, str(exc))
            self.circularBanner.show()
            return None
        except Exception as exc:
            self.associationLabel.setText(_("The association could not be computed: {error}").format(error=exc))
            self.associationLabel.show()
            return None
        parts = []
        for metric, values in found["metrics"].items():
            low, high = (values.get("ci") or (None, None))[:2]
            parts.append(f"{METRIC_NAMES.get(metric, metric)} {_number(values['value'])} (95% CI {_number(low)} to {_number(high)})")
        self.associationLabel.setText(_("{outcome} ({type}, n = {n}): {metrics}. Research use only.").format(outcome=outcome, type=found["outcome_type"], n=found["n"], metrics="; ".join(parts)))
        self.associationLabel.show()
        return found

    def openValidate(self):
        return self.ctx.focusCard("validate")

    #
    # Targets
    #

    def circuitSpace(self):
        """(space label, family) of the current circuit: its connectome's space, else its grid."""
        from LesionNetworkMappingLib.atlases import mask_family
        from LesionNetworkMappingLib.geometry import Grid, space_family

        record = self.currentCircuitRecord()
        space = ((record or {}).get("bundle") or {}).get("space")
        if space:
            return space, space_family(space)
        path = self.currentCircuitPath()
        if not path or not os.path.isfile(path):
            return None, "unknown"
        try:
            grid = Grid.from_nifti(path)
        except Exception:
            return None, "unknown"
        family, space = mask_family(grid.shape, grid.affine)
        return space, family

    def gmPrior(self):
        """(path, space) of the GM prior: a chosen file, else the cached catalog prior of the circuit's family; (None, None)."""
        from LesionNetworkMappingLib import circuits, fetch

        if self.gmFile and os.path.isfile(self.gmFile):
            return self.gmFile, circuits.space_of_file(self.gmFile)
        _space, family = self.circuitSpace()
        resourceId = GM_PRIORS.get(family)
        if not resourceId:
            return None, None
        try:
            resource = fetch.find_resource(resourceId, ("templates",))
            path = fetch.cached_path(resource)
        except Exception:
            return None, None
        return (path, resource.get("space")) if path else (None, None)

    def _refreshGm(self):
        path, _gmSpace = self.gmPrior()
        _circuitSpace, family = self.circuitSpace()
        fetching = self.gmFetch is not None
        if fetching:
            self.gmLabel.setText(self.gmFetch["state"]["text"])
        elif path:
            self.gmLabel.setText(_("Gray matter: {name} (probability > 0.5)").format(name=os.path.basename(path)))
        elif family in GM_PRIORS:
            self.gmLabel.setText(_("Gray matter: no prior of the {family} family on this computer.").format(family=family))
        else:
            self.gmLabel.setText(_("Gray matter: the circuit's template family is not known; choose a GM prior of its space."))
        resourceId = GM_PRIORS.get(family)
        downloadable = False
        if resourceId and not path:
            try:
                from LesionNetworkMappingLib import fetch

                downloadable = bool(fetch.find_resource(resourceId, ("templates",)).urls)
            except Exception:
                downloadable = False
        self.getGmButton.setVisible(downloadable and not fetching)

    def chooseGmPrior(self, path=None):
        if path is None:
            path = qt.QFileDialog.getOpenFileName(slicer.util.mainWindow(), _("Choose a gray-matter probability"), "", _("NIfTI (*.nii *.nii.gz)"))
        if not path:
            return None
        self.gmFile = os.path.abspath(path)
        self._refreshGm()
        self._refreshTargetsState()
        return self.gmFile

    def getGmPrior(self, confirm=True):
        """Download the catalog GM prior of the circuit's family (an explicit user action, after confirmation)."""
        from LesionNetworkMappingLib import fetch

        _space, family = self.circuitSpace()
        resourceId = GM_PRIORS.get(family)
        if not resourceId or self.gmFetch is not None:
            return False
        resource = fetch.find_resource(resourceId, ("templates",))
        if confirm:
            text = _("Download {title} ({size:.1f} MB, {licence}) from TemplateFlow?").format(title=resource.title, size=max(int(resource.size or 0) / 1e6, 0.1), licence=resource.licence)
            box = qt.QMessageBox(qt.QMessageBox.Question, _("Get GM prior"), text, qt.QMessageBox.Ok | qt.QMessageBox.Cancel, slicer.util.mainWindow())
            if _exec(box) != qt.QMessageBox.Ok:
                return False
        state = {"text": _("Downloading {title}…").format(title=resource.title), "error": None, "done": False}

        def work():
            try:
                fetch.fetch(resource)
            except Exception as exc:
                state["error"] = f"{type(exc).__name__}: {exc}"
            state["done"] = True

        thread = threading.Thread(target=work, name="lnm-gm-fetch", daemon=True)
        self.gmFetch = {"thread": thread, "state": state}
        thread.start()
        self.gmTimer.start()
        self._refreshGm()
        return True

    def _pollGmFetch(self):
        if self.gmFetch is None or self._cleanedUp:
            self.gmTimer.stop()
            return
        state = self.gmFetch["state"]
        if state["done"]:
            self.gmTimer.stop()
            self.gmFetch = None
            if state["error"]:
                self._setNotice(_("The GM prior could not be downloaded: {error}").format(error=state["error"]))
        self._refreshGm()
        self._refreshTargetsState()

    def _onTargetsSourceChanged(self, node=None):
        if node is not None:
            self.targetsFile = None
            self.targetsFileLabel.hide()
        self._refreshTargetsState()

    def importTargets(self, path=None):
        """Use a CSV/TSV/.fcsv file of targets (a dialog when ``path`` is None)."""
        if path is None:
            path = qt.QFileDialog.getOpenFileName(slicer.util.mainWindow(), _("Import targets"), "", _("Targets (*.csv *.tsv *.fcsv *.mrk.json *.txt)"))
        if not path:
            return None
        self.targetsFile = os.path.abspath(path)
        self.markupsCombo.setCurrentNode(None)
        self.targetsFileLabel.setText(_("Targets: {name}").format(name=os.path.basename(path)))
        self.targetsFileLabel.show()
        self._refreshTargetsState()
        return self.targetsFile

    def targetList(self):
        """The targets ([{label, x, y, z, radius_mm}]) of the markups list or the imported file; [] when there are none."""
        from LesionNetworkMappingLib import circuits

        radius = float(self.targetRadiusSpin.value)
        if self.targetsFile:
            return circuits.read_targets(self.targetsFile, default_radius=radius)
        node = self.markupsCombo.currentNode()
        if node is None:
            return []
        targets = []
        for index in range(node.GetNumberOfControlPoints()):
            position = [0.0, 0.0, 0.0]
            node.GetNthControlPointPositionWorld(index, position)
            label = node.GetNthControlPointLabel(index) or f"target{index + 1}"
            targets.append({"label": label, "x": position[0], "y": position[1], "z": position[2], "radius_mm": radius})
        return targets

    def _refreshTargetsState(self):
        reason = ""
        if not self.currentCircuitPath():
            reason = _("Choose a circuit.")
        elif self.gmPrior()[0] is None:
            reason = _("A gray-matter prior of the circuit's template family is needed.")
        elif not self.targetsFile and (self.markupsCombo.currentNode() is None or self.markupsCombo.currentNode().GetNumberOfControlPoints() == 0):
            reason = _("Choose a point list of targets, or import a CSV.")
        self.targetsButton.setEnabled(not reason)
        self.targetsButton.setToolTip(reason or _("Mean and peak circuit value in each target's sphere and their percentiles among 1000 random gray-matter spheres of the same radius"))
        if self.targets is None:
            self.targetsLabel.setText(reason)

    def targetsFolder(self):
        path = self.currentCircuitPath()
        return os.path.join(os.path.dirname(path), "targets") if path else None

    def scoreTargets(self):
        """Score the targets against random gray-matter spheres; return the result (None with a message)."""
        from LesionNetworkMappingLib import circuits

        path = self.currentCircuitPath()
        gm, gmSpace = self.gmPrior()
        if not path or gm is None:
            return None
        space, _family = self.circuitSpace()
        try:
            targets = self.targetList()
            if not targets:
                self.targetsLabel.setText(_("No target: add points to the list, or import a CSV."))
                return None
            with slicer.util.WaitCursor():
                self.targets = circuits.score_targets(targets, path, gm, circuit_space=space, gm_space=gmSpace, out_dir=self.targetsFolder(), run_root=self.registryRoot())
        except Exception as exc:
            logging.exception("Lesion network mapping: the targets could not be scored")
            self.targets = None
            self._showTargets()
            self.targetsLabel.setText(_("The targets could not be scored: {error}").format(error=exc))
            return None
        self._showTargets()
        return self.targets

    def _showTargets(self):
        result = self.targets
        if result is None:
            self.targetsTable.setRowCount(0)
            self.targetsTable.hide()
            self.targetsLabel.setText("")
            return
        rows = []
        for row in result["rows"]:
            where = whereText(row["x"], row["y"], row["z"])
            tip = _("{radius:g} mm sphere, {voxels} voxels; peak percentile {peak}; {gm}").format(
                radius=row["radius_mm"], voxels=row["voxels"], peak=_number(row["percentile_peak"], 3), gm=_("centre in gray matter") if row["in_gm"] else _("centre outside gray matter"))
            if row["note"]:
                tip += f"; {row['note']}"
            rows.append([(row["label"], tip), (where, tip), (_number(row["mean"]), ""), (_number(row["peak"]), ""), (_number(row["percentile_mean"], 3), tip), (row["sign"], "")])
        _setRows(self.targetsTable, rows)
        self.targetsLabel.setText(_("{n} target(s) scored against {k} random gray-matter spheres per radius ({family} family). Research use only.").format(
            n=len(result["rows"]), k=result["k"], family=result["family"]))


CARD = {
    "id": "circuits",
    "title": "Circuits",
    "order": ORDER_CIRCUITS,
    "factory": CircuitsCard,
}

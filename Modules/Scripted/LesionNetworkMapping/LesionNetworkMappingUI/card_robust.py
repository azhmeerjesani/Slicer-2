"""Robust card of the Map module (order 100, Seed = Cohort; L-37, SPEC_LNM_ADDENDUM §3.2, §3.9, §7.1).

The card puts the secondary analyses next to the fixed primary result, so that a reader can see how far the
conclusion moves when one of the researcher's degrees of freedom is changed. Every analysis here is secondary:
none of them replaces the primary map, none selects a "best" setting, and the primary rule and overlap criterion
(the Result card's rule and the Overlap tab's sensitivity) are shown as fixed text, never as a control.

Sections (each one runs ``LesionNetworkMappingLib.robustness`` in the job runner, with Stop):

1. **Thresholds**: the threshold and overlap sweep over the cohort's per-lesion maps. A grid of small multiples
   (one axial slice per setting, drawn with the primitives of ``figures.py``, L/R stamped), a table with mL, Dice and r
   against the primary, and the "conclusion flips at …" sentence. |T| rules below N 900 are listed as disabled with
   the reason.
2. **Replicate on…**: the same lesions mapped on another registered connectome; the threshold rule is re-resolved for
   that connectome's own N and V; unthresholded r, Dice and peak persistence, with the Cotovio reference text and the
   r < 0.7 flags.
3. **Lesion split-half** (1000 random splits): the distribution of r (overlap maps, GLM t maps) and Dice.
4. **Influence**: leave-one-lesion-out; the top 5 lesions with a "Show" link that selects the subject in the Library.
5. **Seed variants** (checkboxes): binarized, probabilistic, extrusion, spheres (with a coordinates file), flipped;
   derived lesion files go to the run folder, never to ``lesions/``.
6. **Hemisphere consolidation**: an analysis option with its caveat; a derived run, the traced masks are not modified.
7. **Connectome split-half**: from an engine run made with ``--split-half`` (the current result's run, or a chosen
   folder), always labelled "sampling stability, not robustness" and never shown next to the lesion split-half.

Results are written to ``<study>/derivatives/lnm/conn-<label>[_n-N]/robustness/<analysis>/`` (sweep.tsv,
connectomes.tsv, splithalf.tsv, influence.tsv, seeds.tsv, ...; paths in them are relative) and registered in the run
registry; the card shows the newest result of each analysis again after a restart. The "Methods sentences" box at
the bottom is the robustness text of ``methods.py`` for the study (L-18). The card works in Classic and without the
Studio shell.
"""

import logging
import math
import os
import re

import qt
import slicer
from slicer.i18n import tr as _

from . import ORDER_ROBUST, widgets
from .card_validate import _fitTable, _interval, _num, _pixmap, _tableItem
from .cohort_contrast import DESIGN_DELAY_MS, _fill, _readJson, _stat, label, levelColor

KINDS = ("sweep", "replicate", "splithalf", "influence", "seeds", "consolidate", "connsplit")
#: The analyses that read the cohort's per-lesion maps (maps.tsv)
MAP_KINDS = ("sweep", "replicate", "splithalf", "influence")
ROBUSTNESS_FOLDER = "robustness"
DEFAULT_SENSITIVITY = 90
DEFAULT_SPLITS = 1000
TOP_K = 5
#: The sweep tiles are drawn only when the maps fit in this many bytes (they are read again in the card's process)
TILE_MAX_BYTES = 600 * 1024 * 1024
TILE_WIDTH_PX = 108
TILE_COLUMNS = 3
#: Rows of the replicate table: the group rows and this many least similar lesions
REPLICATE_LESIONS = 6
#: Seed variants in display order; the spheres need a coordinates file
VARIANT_ORDER = ("binarized", "probabilistic", "extrude2", "extrude4", "flipped", "sphere2", "sphere4", "sphere6")
COORDINATE_VARIANTS = ("sphere2", "sphere4", "sphere6")
DEFAULT_VARIANTS_ON = ("binarized", "probabilistic", "extrude2", "extrude4", "flipped")
TILE_COLOR = "#D1495B"
RESULT_FILES = {
    "sweep": "sweep.json",
    "replicate": "replicate.json",
    "splithalf": "splithalf.json",
    "influence": "influence.json",
    "seeds": "variants.json",
    "consolidate": "consolidation.json",
    "connsplit": "connectome_split_half.json",
}
FOLDER_NAMES = {"connsplit": "connectome_splithalf"}


def _mapWidget():
    return getattr(slicer.modules, "LesionNetworkMappingWidget", None)


def _readTsv(path):
    """The rows of a BIDS-style TSV as dicts of strings ([] when it is missing)."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = [line.rstrip("\n").split("\t") for line in f if line.strip()]
    except OSError:
        return []
    if not lines:
        return []
    header = lines[0]
    return [{name: (row[index] if index < len(row) else "n/a") for index, name in enumerate(header)} for row in lines[1:]]


def _float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _flag(value):
    """True, False or None of a TSV flag ("true", "false", "n/a")."""
    text = str(value).strip().lower()
    return True if text == "true" else False if text == "false" else None


def _median(values):
    values = sorted(value for value in values if value is not None)
    if not values:
        return None
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2.0


def _ml(value):
    number = _float(value)
    return "—" if number is None else (f"{number:.1f}" if number < 100 else f"{number:.0f}")


def _percent(value):
    number = _float(value)
    return "" if number is None else f"{number:g}%"


def _safeName(text):
    return re.sub(r"[^A-Za-z0-9]+", "", str(text)) or "x"


def _text(editor):
    """The plain text of a QPlainTextEdit (a property or a slot, depending on the binding)."""
    value = editor.toPlainText
    return value() if callable(value) else value


def _clear(layout):
    """Remove (and delete) every item of a layout."""
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()
        elif item.layout() is not None:
            _clear(item.layout())


#
# Small multiples of the sweep
#


def sweepSettings(cohort, rule, overlapPct, sign="both"):
    """The settings drawn as tiles: ``[(caption, rule label, overlap %, binary map)]``, the primary first.

    One tile per sweep rule at the primary overlap criterion, then the primary rule at the other overlap
    criteria; the maps are the ones ``robustness.threshold_sweep`` thresholds (same rules for this N, same sign).
    """
    from LesionNetworkMappingLib import robustness

    entries = robustness.DEFAULT_RULES(cohort.n, cohort.statistic, sign)
    lesions = cohort.maps.shape[0]

    def counts(entryRule):
        binary, _resolved = robustness._threshold_maps(cohort.maps, entryRule, cohort.n, cohort.v, sign, cohort.gsr)
        return binary.sum(axis=0)

    out = []
    primaryCounts = counts(rule)
    primaryLabel = robustness.sweep_label(rule)
    out.append((_("Primary"), primaryLabel, float(overlapPct), robustness._overlap_binary(primaryCounts, lesions, float(overlapPct))))
    for entry in entries:
        if entry["enabled"]:
            out.append((entry["label"], entry["label"], float(overlapPct), robustness._overlap_binary(counts(entry["rule"]), lesions, float(overlapPct))))
    for pct in robustness.DEFAULT_OVERLAPS:
        if float(pct) != float(overlapPct):
            out.append((primaryLabel, primaryLabel, float(pct), robustness._overlap_binary(primaryCounts, lesions, float(pct))))
    return out


def sweepTileImages(cohort, settings, widthPx=TILE_WIDTH_PX, convention=None):
    """One axial tile (a Pillow image) per setting of :func:`sweepSettings`, all at the slice where the primary map is largest.

    The tiles are drawn with ``figures.render_slice`` on the display grid of the brain mask: the brain outline in
    grey, the setting's binary map in red, the L and R sides stamped on the tile (``figures.side_labels``).
    Returns ``(images, z of the slice in mm)``.
    """
    import numpy as np
    from PIL import ImageDraw

    from LesionNetworkMappingLib import figures

    convention = convention or figures.DEFAULT_CONVENTION
    shape, affine = tuple(cohort.grid.shape), np.asarray(cohort.grid.affine, dtype=np.float64)

    def volume(values):
        data = np.zeros(int(np.prod(shape)), dtype=np.float32)
        data[cohort.mask_indices] = values
        return figures.Volume(data.reshape(shape), affine, "map")

    brain = volume(np.ones(cohort.v, dtype=np.float32))
    spacing = max(2.0, float(np.linalg.norm(affine[:3, :3], axis=0).min()))
    display = figures.display_grid(brain, spacing=spacing, crop=True)
    mask = figures.reslice(brain, display, order=0) >= 0.5
    primary = figures.reslice(volume(settings[0][3].astype(np.float32)), display, order=0) >= 0.5
    index, _area = figures.max_area_index(primary if primary.any() else mask, 2)
    height = max(1, round(widthPx * display.shape[1] / display.shape[0]))
    left, right = figures.side_labels("axial", convention)
    images = []
    for _caption, _ruleLabel, _pct, binary in settings:
        layer = figures.reslice(volume(binary.astype(np.float32)), display, order=0)
        scene = figures.Scene(display, None, [figures.Layer(layer, kind="binary", color=TILE_COLOR)], outline_mask=mask)
        image = figures.render_slice(scene, "axial", index, (widthPx, height), convention)
        draw = ImageDraw.Draw(image)
        draw.text((3, height // 2 - 5), left, fill=(190, 190, 190))
        draw.text((widthPx - 9, height // 2 - 5), right, fill=(190, 190, 190))
        images.append(image)
    return images, float(display.coordinate(2, index))


#
# The card
#


class RobustCard:
    """Controller of the Robust card (see the module docstring)."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.job = None
        self.mapsJob = None
        self.jobKind = None
        self.jobFolder = None
        self.pendingKind = None  # the analysis that starts when the lesions are mapped
        self.notice = ""
        self.results = {}  # kind -> folder of the shown result
        self.sections = {}
        self.sweepTilesFor = None
        self.sweepStale = False
        self._sweepKey = None  # the primary the shown sweep was compared with
        self._scope = None
        self._cleanedUp = False
        self._coordinates = None  # coordinates file of the sphere variants
        self._engineRun = None  # engine run chosen by the user for the connectome split-half
        self._bundleInfo = {}  # path -> (name, n, bundle) or None
        self._timer = qt.QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(DESIGN_DELAY_MS)
        self._timer.connect("timeout()", self.refresh)
        self.widget = qt.QWidget()
        self.widget.setObjectName("LNMRobust")
        layout = qt.QVBoxLayout(self.widget)
        layout.setSpacing(6)
        layout.setContentsMargins(0, 0, 0, 0)
        self.statusLabel = label("", "LNMRobustStatus", "secondaryText")
        layout.addWidget(self.statusLabel)
        layout.addWidget(label(_("How far does the conclusion move when a choice is changed? These analyses are secondary: they sit next to the fixed primary result and never replace it or pick a best setting."),
                               "LNMRobustIntro", "secondaryText"))
        self.progress = widgets.progressRow(objectNamePrefix="LNMRobust")
        self.progress.label.setWordWrap(False)
        self.progress.label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        self.progress.widget.hide()
        layout.addWidget(self.progress.widget)
        self.noticeLabel = label("", "LNMRobustNotice", "secondaryText")
        self.noticeLabel.hide()
        layout.addWidget(self.noticeLabel)
        for builder in (self._buildSweep, self._buildReplicate, self._buildSplitHalf, self._buildInfluence, self._buildSeeds, self._buildConsolidate, self._buildConnectome):
            builder(layout)
        self._buildMethods(layout)
        layout.addStretch(1)
        for name in ("studyChanged", "bundleChanged", "computeEnabledChanged", "ruleChanged", "runChanged"):
            getattr(ctx, name).append(self.scheduleRefresh)
        ctx.runner.jobFinished.append(self._onJobFinished)
        self.refresh()

    #
    # Build
    #

    def _section(self, kind, title, intro, expanded=False):
        """A collapsible section; its widgets are kept in ``self.sections[kind]``."""
        button = widgets.card(title, f"LNMRobust{kind.capitalize()}Card", collapsed=not expanded)
        inner = qt.QVBoxLayout(button)
        inner.setSpacing(6)
        inner.addWidget(label(intro, f"LNMRobust{kind.capitalize()}Intro", "secondaryText"))
        section = {"card": button, "layout": inner}
        self.sections[kind] = section
        return section

    def _runRow(self, kind, section, text):
        row = qt.QHBoxLayout()
        row.addStretch(1)
        button = qt.QPushButton(text)
        button.setObjectName(f"LNMRobust{kind.capitalize()}Run")
        button.setProperty("slicerRole", "secondary")
        button.connect("clicked()", lambda kind=kind: self.run(kind))
        row.addWidget(button)
        section["layout"].addLayout(row)
        reason = label("", f"LNMRobust{kind.capitalize()}Reason", "secondaryText")
        section["layout"].addWidget(reason)
        result = qt.QVBoxLayout()
        result.setSpacing(4)
        result.setContentsMargins(0, 0, 0, 0)
        holder = qt.QWidget()
        holder.setObjectName(f"LNMRobust{kind.capitalize()}Result")
        holder.setLayout(result)
        holder.hide()
        section["layout"].addWidget(holder)
        section.update(run=button, reason=reason, result=result, holder=holder)

    def _combo(self, objectName, toolTip, callback=None):
        combo = qt.QComboBox()
        combo.setObjectName(objectName)
        combo.setToolTip(toolTip)
        combo.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        combo.connect("currentIndexChanged(int)", lambda index: (callback or self.scheduleRefresh)())
        return combo

    def _buildSweep(self, layout):
        section = self._section("sweep", _("Thresholds"), _("The same lesion maps re-thresholded over the published alternatives (FWE, |T| 5 to 12 for N ≥ 900, top 5 to 25%, overlap 50 to 100%). "
                                                            "Only the table and the tiles move; the primary stays as it is."), expanded=True)
        self.primaryLabel = label("", "LNMRobustPrimary", "secondaryText")
        section["layout"].addWidget(self.primaryLabel)
        self._runRow("sweep", section, _("Run threshold sweep"))
        self.flipLabel = label("", "LNMRobustFlips", None)
        self.flipLabel.hide()
        section["result"].addWidget(self.flipLabel)
        self.tileHolder = qt.QWidget()
        self.tileHolder.setObjectName("LNMRobustTiles")
        self.tileGrid = qt.QGridLayout(self.tileHolder)
        self.tileGrid.setContentsMargins(0, 0, 0, 0)
        self.tileGrid.setSpacing(4)
        section["result"].addWidget(self.tileHolder)
        self.tileNote = label("", "LNMRobustTileNote", "caption")
        section["result"].addWidget(self.tileNote)
        self.sweepTable = widgets.tableView([_("Setting"), "mL", _("Dice"), "r"])
        self.sweepTable.setObjectName("LNMRobustSweepTable")
        header = self.sweepTable.horizontalHeader()
        header.setSectionResizeMode(0, qt.QHeaderView.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, qt.QHeaderView.ResizeToContents)
        section["result"].addWidget(self.sweepTable)
        self.allSettings = qt.QCheckBox(_("Show every overlap criterion"))
        self.allSettings.setObjectName("LNMRobustAllSettings")
        self.allSettings.setToolTip(_("By default the table lists each rule at the primary overlap criterion; this adds the other overlap criteria"))
        self.allSettings.connect("toggled(bool)", lambda checked: self.showSweep(self.results.get("sweep")))
        section["result"].addWidget(self.allSettings)
        section["result"].addWidget(label(_("Secondary results: the primary is fixed and is never replaced; no setting is picked as the best one. Dice and r are against the primary."),
                                          "LNMRobustSweepNote", "caption"))
        layout.addWidget(section["card"])

    def _buildReplicate(self, layout):
        from LesionNetworkMappingLib import robustness

        section = self._section("replicate", _("Replicate on…"), _("The same lesions mapped on another registered connectome. The rule is re-resolved for that connectome's own N and V "
                                                                   "(an FWE threshold for N = 1000 is not the one for N = 100)."))
        form = qt.QFormLayout()
        form.setFieldGrowthPolicy(qt.QFormLayout.AllNonFixedFieldsGrow)
        self.replicateCombo = self._combo("LNMRobustReplicateConnectome", _("Another connectome of the Connectome card's list"))
        form.addRow(_("Connectome"), self.replicateCombo)
        section["layout"].addLayout(form)
        self._runRow("replicate", section, _("Replicate"))
        self.replicateSummary = label("", "LNMRobustReplicateSummary", None)
        section["result"].addWidget(self.replicateSummary)
        self.replicateTable = widgets.tableView([_("Map"), "r", _("Dice"), _("Peaks"), _("Flag")])
        self.replicateTable.setObjectName("LNMRobustReplicateTable")
        section["result"].addWidget(self.replicateTable)
        section["result"].addWidget(label(f"{_('Reference')}: {robustness.CROSS_CONNECTOME_REFERENCE}. {robustness.CROSS_CONNECTOME_NOTE}", "LNMRobustReplicateReference", "caption"))
        layout.addWidget(section["card"])

    def _buildSplitHalf(self, layout):
        section = self._section("splithalf", _("Lesion split-half"), _("The lesions are split at random into two halves, 1000 times; each split gives the r of the halves' overlap maps, the Dice of their "
                                                                       "sensitivity maps and, with the GLM design, the r of their GLM t maps."))
        form = qt.QFormLayout()
        self.splitsSpin = qt.QSpinBox()
        self.splitsSpin.setObjectName("LNMRobustSplits")
        self.splitsSpin.setRange(10, 20000)
        self.splitsSpin.setSingleStep(100)
        self.splitsSpin.setValue(DEFAULT_SPLITS)
        self.splitsSpin.setToolTip(_("Random half splits (seeded: the same seed gives the same splits)"))
        form.addRow(_("Splits"), self.splitsSpin)
        self.splitSeed = qt.QSpinBox()
        self.splitSeed.setObjectName("LNMRobustSplitSeed")
        self.splitSeed.setRange(0, 1000000)
        form.addRow(_("Seed"), self.splitSeed)
        section["layout"].addLayout(form)
        self.designCheck = qt.QCheckBox(_("With the GLM design"))
        self.designCheck.setObjectName("LNMRobustDesign")
        self.designCheck.setToolTip(_("Also uses the newest design of the Contrast tab: the halves' GLM t maps (split-half) and the change of the GLM peak (influence)"))
        section["layout"].addWidget(self.designCheck)
        self._runRow("splithalf", section, _("Run split-half"))
        self.splitTable = widgets.tableView([_("Metric"), _("Median"), _("95% of splits"), _("Range")])
        self.splitTable.setObjectName("LNMRobustSplitTable")
        section["result"].addWidget(self.splitTable)
        self.splitLabel = label("", "LNMRobustSplitLabel", "caption")
        section["result"].addWidget(self.splitLabel)
        layout.addWidget(section["card"])

    def _buildInfluence(self, layout):
        section = self._section("influence", _("Influence"), _("Leave-one-lesion-out: each lesion is removed in turn (no refit) and the sensitivity map, or the GLM peak, is compared with the full result."))
        self._runRow("influence", section, _("Run influence"))
        self.influenceLabel = label("", "LNMRobustInfluenceLabel", "caption")
        section["result"].addWidget(self.influenceLabel)
        self.influenceTable = widgets.tableView(["#", _("Subject"), _("Influence"), _("Dice"), ""])
        self.influenceTable.setObjectName("LNMRobustInfluenceTable")
        section["result"].addWidget(self.influenceTable)
        layout.addWidget(section["card"])

    def _buildSeeds(self, layout):
        from LesionNetworkMappingLib import robustness

        section = self._section("seeds", _("Seed variants"), _("The lesions redefined in other ways (derived files in the run folder; lesions/ is never touched) and mapped again, compared with the primary."))
        self.variantChecks = {}
        for variant in VARIANT_ORDER:
            text, description = robustness.VARIANTS[variant]
            check = qt.QCheckBox(text)
            check.setObjectName(f"LNMRobustVariant{variant}")
            check.setToolTip(description)
            check.setChecked(variant in DEFAULT_VARIANTS_ON)
            check.connect("toggled(bool)", lambda checked: self.scheduleRefresh())
            self.variantChecks[variant] = check
            section["layout"].addWidget(check)
        row = qt.QHBoxLayout()
        self.coordinatesButton = qt.QPushButton(_("Coordinates file…"))
        self.coordinatesButton.setObjectName("LNMRobustCoordinates")
        self.coordinatesButton.setToolTip(_("A TSV with participant_id, x, y, z (RAS mm); needed by the sphere variants"))
        self.coordinatesButton.connect("clicked()", self.chooseCoordinates)
        row.addWidget(self.coordinatesButton)
        self.coordinatesLabel = label("", "LNMRobustCoordinatesLabel", "secondaryText")
        row.addWidget(self.coordinatesLabel, 1)
        section["layout"].addLayout(row)
        self._runRow("seeds", section, _("Run variants"))
        self.seedsTable = widgets.tableView([_("Variant"), _("Changed"), _("Median r"), _("Median Dice")])
        self.seedsTable.setObjectName("LNMRobustSeedsTable")
        section["result"].addWidget(self.seedsTable)
        self.seedsNote = label("", "LNMRobustSeedsNote", "caption")
        section["result"].addWidget(self.seedsNote)
        layout.addWidget(section["card"])

    def _buildConsolidate(self, layout):
        from LesionNetworkMappingLib import robustness

        section = self._section("consolidate", _("Hemisphere consolidation"), _("An analysis option: every lesion placed on one hemisphere, those of the other side mirrored. "
                                                                                "It is never applied automatically."))
        section["layout"].addWidget(label(robustness.CONSOLIDATION_CAVEAT, "LNMRobustConsolidateCaveat", "caption"))
        form = qt.QFormLayout()
        self.sideCombo = self._combo("LNMRobustSide", _("The hemisphere the lesions are consolidated on"))
        self.sideCombo.addItem(_("Left"), "L")
        self.sideCombo.addItem(_("Right"), "R")
        form.addRow(_("Consolidate on"), self.sideCombo)
        section["layout"].addLayout(form)
        self._runRow("consolidate", section, _("Create derived run"))
        self.consolidateLabel = label("", "LNMRobustConsolidateLabel", None)
        section["result"].addWidget(self.consolidateLabel)
        layout.addWidget(section["card"])

    def _buildConnectome(self, layout):
        from LesionNetworkMappingLib import robustness

        section = self._section("connsplit", _("Connectome split-half"), _("Odd against even subjects of the connectome, from an engine run made with the split-half option. "
                                                                           "This is sampling stability, not robustness, and it is not comparable with the lesion split-half."))
        row = qt.QHBoxLayout()
        self.engineButton = qt.QPushButton(_("Engine run folder…"))
        self.engineButton.setObjectName("LNMRobustEngineRun")
        self.engineButton.setToolTip(_("The output folder of an engine run made with --split-half (the current result's run is used when it has one)"))
        self.engineButton.connect("clicked()", self.chooseEngineRun)
        row.addWidget(self.engineButton)
        self.engineLabel = label("", "LNMRobustEngineLabel", "secondaryText")
        row.addWidget(self.engineLabel, 1)
        section["layout"].addLayout(row)
        self._runRow("connsplit", section, _("Summarise connectome split-half"))
        self.connLabel = label("", "LNMRobustConnLabel", None)
        section["result"].addWidget(self.connLabel)
        section["result"].addWidget(label(robustness.CONNECTOME_SPLIT_HALF_LABEL, "LNMRobustConnSampling", "caption"))
        layout.addWidget(section["card"])

    def _buildMethods(self, layout):
        self.methodsHeader = label(_("Methods sentences"), "LNMRobustMethodsHeader", "secondaryText")
        layout.addWidget(self.methodsHeader)
        self.methodsBox = qt.QPlainTextEdit()
        self.methodsBox.setObjectName("LNMRobustMethods")
        self.methodsBox.setReadOnly(True)
        self.methodsBox.setMaximumHeight(110)
        self.methodsBox.setPlaceholderText(_("The robustness sentences of the Methods appear here once an analysis has run."))
        layout.addWidget(self.methodsBox)
        self.copyButton = qt.QPushButton(_("Copy sentences"))
        self.copyButton.setObjectName("LNMRobustCopyMethods")
        self.copyButton.setProperty("slicerRole", "secondary")
        self.copyButton.connect("clicked()", lambda: qt.QApplication.clipboard().setText(_text(self.methodsBox)))
        layout.addWidget(self.copyButton)

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

    def connectomeDir(self):
        """``<study>/derivatives/lnm/conn-<label>[_n-N]`` of the cohort maps, or None."""
        path = self.currentMapsTsv()
        if path:
            return os.path.dirname(os.path.dirname(path))
        widget = _mapWidget()
        cohortDir = widget.cohortOutputDir() if widget is not None and hasattr(widget, "cohortOutputDir") else None
        return os.path.dirname(cohortDir) if cohortDir else None

    def robustnessRoot(self):
        connDir = self.connectomeDir()
        return os.path.join(connDir, ROBUSTNESS_FOLDER) if connDir else None

    def folderFor(self, kind):
        """The run folder of an analysis for the current settings (None when there is no study or connectome)."""
        root = self.robustnessRoot()
        if root is None:
            return None
        if kind == "replicate":
            path = self.replicatePath()
            return os.path.join(root, f"replicate_{_safeName(self.bundleName(path))}") if path else None
        if kind == "consolidate":
            return os.path.join(root, f"consolidate_{self.side()}")
        return os.path.join(root, FOLDER_NAMES.get(kind, kind))

    def rule(self):
        from LesionNetworkMappingLib import thresholds

        return thresholds.normalize_rule(self.ctx.rule)

    def overlapPct(self):
        """The primary overlap criterion: the Overlap tab's sensitivity (default 90)."""
        card = self.cohortCard()
        slider = getattr(getattr(card, "overlap", None), "sensitivitySlider", None)
        try:
            return float(slider.value) if slider is not None else float(DEFAULT_SENSITIVITY)
        except (TypeError, ValueError):
            return float(DEFAULT_SENSITIVITY)

    def primaryKey(self):
        """(rule label, overlap %) of the primary as it stands now."""
        from LesionNetworkMappingLib import robustness

        return (robustness.sweep_label(self.rule()), self.overlapPct())

    def side(self):
        return self.sideCombo.currentData or "L"

    def replicatePath(self):
        return self.replicateCombo.currentData or None

    def bundleName(self, path):
        info = self.bundleInfo(path)
        return info[0] if info else os.path.basename(str(path))

    def bundleInfo(self, path):
        """(name, n subjects, bundle) of a registered connectome, or None when it cannot be opened (cached)."""
        if path not in self._bundleInfo:
            try:
                from LesionNetworkMappingLib.connectome import ConnectomeBundle

                bundle = ConnectomeBundle.open(path)
                self._bundleInfo[path] = (bundle.name, bundle.n_subjects, bundle)
            except Exception:
                logging.debug("Lesion network mapping: a registered connectome could not be opened for the Robust card", exc_info=True)
                self._bundleInfo[path] = None
        return self._bundleInfo[path]

    def otherConnectomes(self):
        """[(display text, path)] of the registered connectomes other than the one in use."""
        widget = _mapWidget()
        try:
            paths = widget.logic.registeredConnectomes() if widget is not None else []
        except Exception:
            paths = []
        current = os.path.normpath(self.ctx.bundle.path) if self.ctx.bundle is not None else None
        items = []
        for path in paths:
            if current is not None and os.path.normpath(path) == current:
                continue
            info = self.bundleInfo(path)
            if info is not None:
                items.append((f"{info[0]} · N={info[1]}", path))
        return items

    def designPath(self):
        """The newest design.json of the Contrast tab under this connectome, or None."""
        connDir = self.connectomeDir()
        root = os.path.join(connDir, "glm") if connDir else None
        if not root or not os.path.isdir(root):
            return None
        found = [os.path.join(root, name, "design.json") for name in os.listdir(root) if os.path.isfile(os.path.join(root, name, "design.json"))]
        return max(found, key=lambda path: _stat(path) or (0, 0)) if found else None

    def selectedVariants(self):
        return [variant for variant in VARIANT_ORDER if self.variantChecks[variant].isChecked()]

    def engineRunDir(self):
        """The engine run folder of the connectome split-half: the chosen one, else the current result's run when it has a split-half."""
        run = getattr(self.ctx, "currentRun", None)
        for path in (self._engineRun, getattr(run, "runDir", None)):
            if path and "split_half" in (_readJson(os.path.join(path, "qa.json")) or {}):
                return path
        return None

    #
    # State
    #

    def mapsRunning(self):
        widget = _mapWidget()
        job = getattr(widget, "cohortJob", None) if widget is not None else None
        return job if job is not None and not job.isFinished() else None

    def running(self):
        return any(job is not None and not job.isFinished() for job in (self.job, self.mapsJob))

    def runState(self, kind):
        """(enabled, reason) of the Run button of an analysis."""
        if self.ctx.study is None:
            return False, _("Open a study in the Seed card (Seed: Cohort).")
        if kind != "connsplit" and kind != "consolidate" and self.ctx.bundle is None:
            return False, _("Choose a connectome in the Connectome card.")
        if self.running():
            return False, _("Mapping the lesions first…") if self.job is None or self.job.isFinished() else _("An analysis is running…")
        if kind == "replicate":
            path = self.replicatePath()
            if not path:
                return False, _("Register another connectome in the Connectome card (Manage…) to replicate on it.")
            info = self.bundleInfo(path)
            widget = _mapWidget()
            if info is not None and widget is not None and hasattr(widget, "logic") and not widget.logic.termsAccepted(info[2]):
                return False, _("Accept the data use terms of {name} in the Connectome card first.").format(name=info[0])
        if kind == "seeds":
            variants = self.selectedVariants()
            if not variants:
                return False, _("Tick at least one variant.")
            if any(variant in COORDINATE_VARIANTS for variant in variants) and not self._coordinates:
                return False, _("The sphere variants need a coordinates file (participant_id, x, y, z).")
        if kind == "connsplit" and self.engineRunDir() is None:
            return False, _("No engine run with a split-half: run the engine with --split-half, or choose its folder.")
        if kind in ("splithalf", "influence") and self.designCheck.isChecked() and self.designPath() is None:
            return False, _("No GLM design yet: run the Contrast tab first, or untick “With the GLM design”.")
        if kind in MAP_KINDS and self.currentMapsTsv() is None:
            if self.mapsRunning() is not None:
                return True, _("The lesions are being mapped: the analysis starts when they are done.")
            widget = _mapWidget()
            enabled, reason = widget.cohortComputeState() if widget is not None and hasattr(widget, "cohortComputeState") else (False, _("The Map module is not available."))
            return (True, _("Run maps the lesions first (cohort maps), then runs the analysis.")) if enabled else (False, reason)
        if kind in MAP_KINDS + ("seeds",) and not self.ctx.computeEnabled():
            return False, " ".join(self.ctx.computeDisabledReasons()) or _("Not ready")
        if self.ctx.runner.isBusy():
            return True, _("Another computation is running: this one will wait for it.")
        return True, ""

    def scheduleRefresh(self, *args):
        if not self._cleanedUp:
            self._timer.start()

    def refresh(self):
        """Show the state of every section (settings, enabled Run buttons, newest results)."""
        if self._cleanedUp:
            return
        self._timer.stop()
        scope = self.connectomeDir()
        if scope != self._scope:  # another study or connectome: its own results
            self._scope = scope
            self.results = {}
            self.sweepTilesFor = None
            for section in self.sections.values():
                section["holder"].hide()
            if not self.running():
                self.progress.setJob(None)
                self.progress.widget.hide()
        self.showSettings()
        for kind in KINDS:
            enabled, reason = self.runState(kind)
            section = self.sections[kind]
            section["run"].setEnabled(enabled)
            section["reason"].setText(reason)
            section["reason"].setVisible(bool(reason))
            if kind in ("replicate", "consolidate"):
                # the result of the selected connectome or side, not of another one
                want = self.folderFor(kind)
                if self.results.get(kind) != want:
                    self.results.pop(kind, None)
                    section["holder"].hide()
                    if want:
                        self.loadResult(kind, want)
            elif kind == "sweep" and kind in self.results and self._sweepKey != self.primaryKey():
                self.loadResult(kind, self.results[kind])  # shown again against the primary as it is now
            elif kind not in self.results:
                self.loadLatest(kind)
        self.statusLabel.setText(self.statusText())

    def statusText(self):
        if self.ctx.study is None:
            return _("Open a study (Seed: Cohort) to run the robustness analyses.")
        done = [kind for kind in KINDS if kind in self.results]
        if not done:
            return _("No robustness analysis has run for this connectome yet.")
        return _("{n} of {total} analyses have results for this connectome.").format(n=len(done), total=len(KINDS))

    def showSettings(self):
        from LesionNetworkMappingLib import robustness

        self.primaryLabel.setText(_("Primary (fixed): {rule}, sensitivity ≥ {pct:g}%. It changes in the Result card and the Overlap tab, not here.").format(rule=robustness.sweep_label(self.rule()), pct=self.overlapPct()))
        _fill(self.replicateCombo, self.otherConnectomes() or [(_("No other connectome registered"), None)])
        designPath = self.designPath()
        self.designCheck.setEnabled(designPath is not None)
        if designPath is None:
            self.designCheck.setChecked(False)
        self.coordinatesLabel.setText(os.path.basename(self._coordinates) if self._coordinates else _("none (needed by the spheres)"))
        for variant in COORDINATE_VARIANTS:
            self.variantChecks[variant].setToolTip(robustness.VARIANTS[variant][1] + ("" if self._coordinates else " " + _("(needs a coordinates file)")))
        engine = self.engineRunDir()
        self.engineLabel.setText(os.path.basename(engine) if engine else _("none with a split-half"))

    #
    # Run
    #

    def run(self, kind):
        """Run one analysis (after the cohort maps job when the lesions are not mapped); return the job, or None."""
        if self.running() or kind not in KINDS:
            return None
        self.setNotice("")
        enabled, reason = self.runState(kind)
        if not enabled:
            self.sections[kind]["reason"].setText(reason)
            self.sections[kind]["reason"].show()
            return None
        if kind in MAP_KINDS and self.currentMapsTsv() is None:
            job = self.mapsRunning()
            if job is None:
                card = self.cohortCard()
                job = card.mapLesions() if card is not None and hasattr(card, "mapLesions") else None
            if job is None:
                return None
            self.mapsJob = job
            self.pendingKind = kind
            self.progress.setJob(job)
            self.progress.widget.show()
            self.refresh()
            return job
        return self.start(kind)

    def arguments(self, kind, folder):
        """The command line of an analysis (after ``-m LesionNetworkMappingLib.robustness``)."""
        from LesionNetworkMappingLib import thresholds

        study = self.ctx.study.path
        rule = thresholds.rule_to_json(self.rule())
        overlap = f"{self.overlapPct():g}"
        maps = self.currentMapsTsv()
        common = ["--out", folder, "--run-root", study]
        if kind == "sweep":
            return ["sweep", "--maps", maps, "--rule", rule, "--overlap", overlap, *common]
        if kind == "replicate":
            return ["replicate", "--maps", maps, "--bundle-b", self.replicatePath(), "--rule", rule, "--overlap", overlap, *common]
        design = ["--study", study, "--design", self.designPath()] if kind in ("splithalf", "influence") and self.designCheck.isChecked() else []
        if kind == "splithalf":
            return ["splithalf", "--maps", maps, "--rule", rule, "--overlap", overlap, "--n-splits", str(self.splitsSpin.value), "--seed", str(self.splitSeed.value), *design, *common]
        if kind == "influence":
            return ["influence", "--maps", maps, "--rule", rule, "--overlap", overlap, "--top-k", str(TOP_K), *design, *common]
        if kind == "seeds":
            args = ["seeds", "--study", study, "--bundle", self.ctx.bundle.path, "--rule", rule, "--overlap", overlap, "--variants", ",".join(self.selectedVariants()), *common]
            if self._coordinates:
                args += ["--coordinates", self._coordinates]
            return args
        if kind == "consolidate":
            return ["consolidate", "--study", study, "--side", self.side(), *common]
        return ["splithalf", "--connectome-run", self.engineRunDir(), *common]

    def jobSpec(self, kind):
        from . import jobrunner

        folder = self.folderFor(kind)
        if folder is None:
            raise ValueError(_("No output folder (study or connectome missing)"))
        os.makedirs(os.path.dirname(folder), exist_ok=True)
        args = self.arguments(kind, folder)
        self.jobFolder = folder
        title = {"sweep": _("Threshold sweep"), "replicate": _("Replicate"), "splithalf": _("Lesion split-half"), "influence": _("Influence"), "seeds": _("Seed variants"),
                 "consolidate": _("Hemisphere consolidation"), "connsplit": _("Connectome split-half")}[kind]
        return jobrunner.JobSpec("LesionNetworkMappingLib.robustness", args, title=title, heavy=kind != "connsplit", kind="robustness", runRoot=self.ctx.study.path, onEvent=self.onEvent,
                                 onDone=self.onDone, name=f"robust-{kind}", statusText=lambda job, title=title: _statusText(job, title))

    def start(self, kind):
        from . import jobrunner

        try:
            spec = self.jobSpec(kind)
        except Exception as exc:
            logging.exception("Lesion network mapping: the robustness job could not be prepared")
            self.setNotice(_("The analysis cannot start: {error}").format(error=exc))
            return None
        if not isinstance(spec, jobrunner.JobSpec):
            return None
        self.jobKind = kind
        self.job = self.ctx.submit(spec)
        self.progress.setJob(self.job)
        self.progress.widget.show()
        self.refresh()
        return self.job

    def _onJobFinished(self, job):
        if self._cleanedUp or job is not self.mapsJob:
            return
        self.mapsJob = None
        self._updateProgress(force=True)
        kind, self.pendingKind = self.pendingKind, None
        if kind and job.succeeded() and self.currentMapsTsv() is not None and self.runState(kind)[0]:
            self.start(kind)
        else:
            self.refresh()

    def _updateProgress(self, force=False):
        try:
            from LesionNetworkMapping import updateProgressQuietly
        except ImportError:
            self.progress.update()
            return
        updateProgressQuietly(self.progress, force)

    def onEvent(self, event):
        self._updateProgress(force=event.get("event") != "progress")

    def onDone(self, job):
        self._updateProgress(force=True)
        kind, folder = self.jobKind, self.jobFolder
        if job.state == "failed":
            self.setNotice(_("Analysis failed: {reason}").format(reason=job.failureReason))
        elif job.state == "cancelled":
            self.setNotice(_("Analysis stopped: a job that did not finish has no results."))
        else:
            self.setNotice("")
        if job.succeeded() and kind:
            self.loadResult(kind, folder)
            self.message(_("Analysis done: the results are in the Robust card"))
        self.refresh()

    def setNotice(self, text):
        self.notice = text
        self.noticeLabel.setText(text)
        self.noticeLabel.setVisible(bool(text))

    def message(self, text, ms=3000):
        """A consequence message: a toast in Studio, the notice line in Classic."""
        shell = self.ctx.shell()
        if shell is not None:
            shell.toast(text, ms)
        else:
            self.noticeLabel.setText(text)
            self.noticeLabel.show()

    #
    # Results
    #

    def resultFolders(self, kind):
        root = self.robustnessRoot()
        if not root or not os.path.isdir(root):
            return []
        prefix = {"replicate": "replicate_", "consolidate": "consolidate_"}.get(kind)
        names = [FOLDER_NAMES.get(kind, kind)] if prefix is None else [name for name in sorted(os.listdir(root)) if name.startswith(prefix)]
        return [os.path.join(root, name) for name in names if os.path.isfile(os.path.join(root, name, RESULT_FILES[kind]))]

    def loadLatest(self, kind):
        """Show the newest finished result of an analysis in this connectome's folder (so that it is shown again after a restart)."""
        best = None
        for folder in self.resultFolders(kind):
            stamp = _stat(os.path.join(folder, RESULT_FILES[kind])) or (0, 0)
            if best is None or stamp > best[0]:
                best = (stamp, folder)
        if best is not None:
            self.loadResult(kind, best[1])

    def loadResult(self, kind, folder):
        if not folder or not os.path.isfile(os.path.join(folder, RESULT_FILES[kind])):
            return False
        self.results[kind] = folder
        section = self.sections[kind]
        show = {"sweep": self.showSweep, "replicate": self.showReplicate, "splithalf": self.showSplitHalf, "influence": self.showInfluence, "seeds": self.showSeeds,
                "consolidate": self.showConsolidate, "connsplit": self.showConnectome}[kind]
        try:
            show(folder)
        except Exception:
            logging.exception(f"Lesion network mapping: the {kind} result could not be shown")
            self.results.pop(kind, None)
            section["holder"].hide()
            return False
        section["holder"].show()
        self.showMethods()
        return True

    def showSweep(self, folder):
        if not folder:
            return
        rows = _readTsv(os.path.join(folder, "sweep.tsv"))
        summary = (_readJson(os.path.join(folder, "sweep.json")) or {}).get("summary") or {}
        primary = next((row for row in rows if _flag(row.get("primary"))), None)
        primaryPct = _float(primary["overlap_pct"]) if primary else None
        self._sweepKey = self.primaryKey()
        storedLabel = primary["setting"].removeprefix("Primary: ") if primary else None
        stale = primary is not None and (storedLabel != self._sweepKey[0] or primaryPct != self._sweepKey[1])
        if stale:
            self.flipLabel.setText(_("Computed for the primary {old}, sensitivity ≥ {oldPct:g}%; the primary is now {new}, sensitivity ≥ {newPct:g}%. Run the sweep again to compare with the current primary.").format(
                old=storedLabel, oldPct=primaryPct or 0.0, new=self._sweepKey[0], newPct=self._sweepKey[1]))
            self.flipLabel.setStyleSheet(f"color: {levelColor('warn')};")
        elif summary.get("conclusion_flips"):
            self.flipLabel.setText(str(summary["conclusion_flips"]))
            self.flipLabel.setStyleSheet(f"color: {levelColor('warn')};")
        elif primary is not None and _flag(primary.get("peak_persists")) is True:
            self.flipLabel.setText(_("No setting changed the conclusion: the primary peak persists within 10 mm in every enabled setting."))
            self.flipLabel.setStyleSheet("")
        else:
            self.flipLabel.setText(_("Conclusion flips could not be checked: the primary has no peak or region of interest to follow."))
            self.flipLabel.setStyleSheet("")
        self.flipLabel.show()
        self.sweepStale = stale
        every = self.allSettings.isChecked()
        shown = [row for row in rows if every or _flag(row.get("primary")) or _float(row.get("overlap_pct")) in (None, primaryPct)]
        self.sweepTable.setRowCount(len(shown))
        for index, row in enumerate(shown):
            isPrimary = bool(_flag(row.get("primary")))
            enabled = _float(row.get("voxels")) is not None
            setting = row["setting"].removeprefix("Primary: ")
            if isPrimary:
                name = f"{_('Primary')}: {setting} · ≥ {_percent(row['overlap_pct'])}"
            elif enabled:
                name = f"{setting} · ≥ {_percent(row['overlap_pct'])}"
            else:
                name = f"{setting} ({_('disabled')})"
            note = row.get("note")
            tooltip = note if note not in ("n/a", "", None) else ""
            if enabled:
                detail = f"{_('Threshold')}: {row['threshold']}; {_('peak within 10 mm')}: {row['peak_persists']}; ROI: {row['roi_survives']}"
                tooltip = f"{tooltip}\n{detail}" if tooltip else detail
            values = [name, _ml(row["ml"]), _num(_float(row["dice_vs_primary"])), _num(_float(row["r_vs_primary"]))]
            for column, text in enumerate(values):
                self.sweepTable.setItem(index, column, _tableItem(text, tooltip or None, right=column > 0, bold=isPrimary, muted=not enabled))
        _fitTable(self.sweepTable, len(shown))
        self.drawTiles(folder, rows)

    def drawTiles(self, folder, rows):
        """The small multiples of the sweep (drawn once per result and primary; skipped for maps that do not fit in memory)."""
        key = (folder, _stat(os.path.join(folder, "sweep.tsv")), self.currentMapsTsv(), self.primaryKey(), self.sweepStale)
        if self.sweepTilesFor == key:
            return
        self.sweepTilesFor = key
        _clear(self.tileGrid)
        self.tileNote.setText("")
        if self.sweepStale:
            self.tileNote.setText(_("The tiles are not drawn: they would show the current primary, not the one this table was computed for."))
            return
        mapsTsv = self.currentMapsTsv()
        if not mapsTsv:
            self.tileNote.setText(_("The tiles need the cohort maps of this connectome; the table above is the result."))
            return
        try:
            from LesionNetworkMappingLib import robustness

            cohort = robustness.load_cohort(mapsTsv)
            if cohort.maps.nbytes > TILE_MAX_BYTES:
                self.tileNote.setText(_("The maps are too large to redraw as tiles here; the table is the result."))
                return
            settings = sweepSettings(cohort, self.rule(), self.overlapPct())
            images, z = sweepTileImages(cohort, settings)
        except Exception as exc:
            logging.exception("Lesion network mapping: the sweep tiles could not be drawn")
            self.tileNote.setText(_("The tiles could not be drawn ({error}); the table is the result.").format(error=exc))
            return
        lookup = {(row["setting"].removeprefix("Primary: "), _float(row["overlap_pct"])): row for row in rows}
        for index, ((caption, ruleLabel, pct, _binary), image) in enumerate(zip(settings, images, strict=True)):
            tile = qt.QLabel()
            tile.setObjectName(f"LNMRobustTile{index}")
            tile.setPixmap(_pixmap(image))
            row = lookup.get((ruleLabel, pct))
            text = caption if index == 0 else f"{caption} · ≥ {pct:g}%"
            if row is not None and index:
                text += f"\nDice {_num(_float(row['dice_vs_primary']))}"
            cell = qt.QWidget()
            cellLayout = qt.QVBoxLayout(cell)
            cellLayout.setContentsMargins(0, 0, 0, 0)
            cellLayout.setSpacing(1)
            cellLayout.addWidget(tile)
            cellLayout.addWidget(label(text, f"LNMRobustTileCaption{index}", "caption"))
            self.tileGrid.addWidget(cell, index // TILE_COLUMNS, index % TILE_COLUMNS)
        self.tileNote.setText(_("Axial slice z = {z:g} mm, where the primary is largest; red is the map of that setting. The L and R sides are marked on each tile.").format(z=z))

    def showReplicate(self, folder):
        record = _readJson(os.path.join(folder, "replicate.json")) or {}
        summary = record.get("summary") or {}
        rows = _readTsv(os.path.join(folder, "connectomes.tsv"))
        a, b = (record.get("connectome_a") or {}), (record.get("connectome_b") or {})
        lines = [_("{a} (N={na}) against {b} (N={nb}): median lesion-map r {r}; {flagged} map(s) flagged below r 0.7.").format(
            a=a.get("label", "A"), na=a.get("n", "?"), b=b.get("label", "B"), nb=b.get("n", "?"), r=_num(_float(summary.get("cross_connectome_r"))), flagged=summary.get("flagged", 0))]
        thresholds = summary.get("thresholds") or {}
        if thresholds:
            lines.append(_("Rule re-resolved for each connectome's own N and V: ") + "; ".join(f"{name} {text}" for name, text in thresholds.items()) + ".")
        self.replicateSummary.setText("\n".join(lines))
        groups = [row for row in rows if row.get("kind") != "lesion"]
        lesions = sorted((row for row in rows if row.get("kind") == "lesion"), key=lambda row: (_float(row["r"]) is None, _float(row["r"]) or 0.0))
        shown = groups + lesions[:REPLICATE_LESIONS]
        self.replicateTable.setRowCount(len(shown))
        for index, row in enumerate(shown):
            r = _float(row.get("r"))
            flagged = row.get("flag") not in ("n/a", "", None) or (row.get("kind") == "lesion" and r is not None and r < 0.7)
            name = row.get("participant_id") if row.get("kind") == "lesion" else row.get("map")
            values = [name, _num(r), _num(_float(row.get("dice"))), row.get("peaks_persist", "—"), _("r < 0.7") if flagged else ""]
            note = row.get("note")
            for column, text in enumerate(values):
                self.replicateTable.setItem(index, column, _tableItem(str(text), note if note not in ("n/a", "", None) else None, right=0 < column < 4, bold=flagged))
        _fitTable(self.replicateTable, len(shown))

    def showSplitHalf(self, folder):
        rows = [row for row in _readTsv(os.path.join(folder, "splithalf.tsv")) if row.get("analysis") == "lesion_split_half"]
        self.splitTable.setRowCount(len(rows))
        for index, row in enumerate(rows):
            low, high = _float(row["p2_5"]), _float(row["p97_5"])
            values = [row["metric"], _num(_float(row["median"])), _interval((low, high)) if low is not None and high is not None else "—", f"{_num(_float(row['min']))} to {_num(_float(row['max']))}"]
            for column, text in enumerate(values):
                self.splitTable.setItem(index, column, _tableItem(text, None, right=column > 0))
        _fitTable(self.splitTable, len(rows))
        first = rows[0] if rows else {}
        self.splitLabel.setText(_("{n} random splits into halves of {half} lesions (seed {seed}). {label}.").format(n=first.get("n_splits", "?"), half=first.get("half_size", "?"), seed=first.get("seed", "?"), label=first.get("label", "")))

    def showInfluence(self, folder):
        record = _readJson(os.path.join(folder, "influence.json")) or {}
        rows = sorted(_readTsv(os.path.join(folder, "influence.tsv")), key=lambda row: _float(row.get("rank")) or 1e9)[:TOP_K]
        self.influenceLabel.setText(_("The {k} most influential lesions, ranked by {measure}. Influence alone is not a reason to remove a lesion.").format(k=len(rows), measure=record.get("influence", "")))
        self.influenceTable.setRowCount(len(rows))
        for index, row in enumerate(rows):
            sid = row["participant_id"]
            values = [row["rank"], sid, _num(_float(row.get("influence")), 3), _num(_float(row.get("dice_vs_full")))]
            for column, text in enumerate(values):
                self.influenceTable.setItem(index, column, _tableItem(str(text), None, right=column != 1))
            button = qt.QToolButton()
            button.setObjectName(f"LNMRobustShow{index}")
            button.setText(_("Show"))
            button.setAutoRaise(True)
            button.setToolButtonStyle(qt.Qt.ToolButtonTextOnly)
            button.setProperty("slicerRole", "ghost")
            button.setToolTip(_("Open {sid} in the Library").format(sid=sid))
            button.connect("clicked()", lambda sid=sid: self.showSubject(sid))
            self.influenceTable.setCellWidget(index, 4, button)
        _fitTable(self.influenceTable, len(rows))

    def showSubject(self, sid):
        """Open the Library with a subject selected; return whether it could."""
        shell = self.ctx.shell()
        if shell is not None:
            shell.switchTo("library")
        elif hasattr(slicer.modules, "studio"):
            slicer.util.selectModule("Studio")
        try:
            from StudioLib import library

            views = list(getattr(library, "_studyViews", []))
            for view in views:
                if hasattr(view, "search"):
                    view.search.setText("")
                if hasattr(view, "setFilter"):
                    view.setFilter("all")
            library._setStudySelection([sid])
            for view in views:
                row = view.model.rowOf(sid) if hasattr(view, "model") else -1
                if row >= 0:
                    view.table.scrollTo(view.model.index(row, 0))
        except Exception:
            logging.exception("Lesion network mapping: the Library could not select the subject")
            return False
        return True

    def showSeeds(self, folder):
        from LesionNetworkMappingLib import robustness

        rows = _readTsv(os.path.join(folder, "seeds.tsv"))
        order = [variant for variant in VARIANT_ORDER if any(row["variant"] == variant for row in rows)]
        self.seedsTable.setRowCount(len(order))
        notes = []
        for index, variant in enumerate(order):
            mine = [row for row in rows if row["variant"] == variant]
            items = [row for row in mine if row.get("kind") == "lesion"]
            changed = sum(1 for row in items if _flag(row.get("changed")) is True)
            text = robustness.VARIANTS[variant][0]
            values = [text, f"{changed}/{len(items)}" if items else "0", _num(_median([_float(row["r"]) for row in items])), _num(_median([_float(row["dice"]) for row in items]))]
            tooltip = "; ".join(sorted({row["note"] for row in mine if row.get("note") not in ("n/a", "", None)})) or None
            if tooltip:
                notes.append(f"{text}: {tooltip}")
            for column, cell in enumerate(values):
                self.seedsTable.setItem(index, column, _tableItem(cell, tooltip, right=column > 0))
        _fitTable(self.seedsTable, len(order))
        self.seedsNote.setText(_("Derived lesion files are in the run folder (robustness/seeds), never in lesions/. ") + " ".join(notes[:2]))

    def showConsolidate(self, folder):
        record = _readJson(os.path.join(folder, "consolidation.json")) or {}
        counts = record.get("counts") or {}
        side = _("left") if str(record.get("side", "")).upper().startswith("L") else _("right")
        self.consolidateLabel.setText(_("Derived run on the {side} hemisphere: {mirrored} lesion(s) mirrored, {kept} already on that side, {other} bilateral or midline kept as traced. The traced masks were not modified.").format(
            side=side, mirrored=counts.get("mirrored", 0), kept=counts.get("kept", 0), other=counts.get("bilateral_or_midline", 0)))

    def showConnectome(self, folder):
        record = _readJson(os.path.join(folder, "connectome_split_half.json")) or {}
        text = _("Connectome split-half (odd {odd} / even {even} subjects): median r {median}, minimum {minimum}.").format(
            odd=record.get("n_odd", "?"), even=record.get("n_even", "?"), median=_num(_float(record.get("median_r")), 3), minimum=_num(_float(record.get("min_r")), 3))
        if record.get("small_halves"):
            text += " " + _("Fewer than 500 subjects per half.")
        self.connLabel.setText(f"{_('Sampling stability, not robustness')}. {text}")

    #
    # Methods sentences (L-18)
    #

    def showMethods(self):
        study = self.ctx.study
        if study is None:
            return
        try:
            from LesionNetworkMappingLib import methods

            statistics = methods.find_section(methods.methods_sections(study.path), "statistics")
            prefix = methods.KIND_LABELS["robustness"]
            lines = [block[1] for block in (statistics.blocks if statistics else []) if block[0] == "p" and block[1].startswith(prefix)]
        except Exception:
            logging.exception("Lesion network mapping: the robustness methods sentences could not be built")
            return
        self.methodsBox.setPlainText("\n".join(lines))

    #
    # Files and hooks
    #

    def chooseCoordinates(self):
        path = qt.QFileDialog.getOpenFileName(self.widget, _("Coordinates file"), "", "TSV (*.tsv *.txt);;All files (*)")
        if path:
            self.setCoordinates(path)

    def setCoordinates(self, path):
        self._coordinates = path or None
        self.scheduleRefresh()

    def chooseEngineRun(self):
        path = qt.QFileDialog.getExistingDirectory(self.widget, _("Engine run folder"))
        if path:
            self.setEngineRun(path)

    def setEngineRun(self, path):
        self._engineRun = path or None
        self.scheduleRefresh()

    def onStudyChanged(self):
        self.scheduleRefresh()

    def onResultChanged(self):
        self.scheduleRefresh()

    def cleanup(self):
        self._cleanedUp = True
        self._timer.stop()
        for name in ("studyChanged", "bundleChanged", "computeEnabledChanged", "ruleChanged", "runChanged"):
            callbacks = getattr(self.ctx, name, [])
            if self.scheduleRefresh in callbacks:
                callbacks.remove(self.scheduleRefresh)
        finished = getattr(self.ctx.runner, "jobFinished", [])
        if self._onJobFinished in finished:
            finished.remove(self._onJobFinished)
        if self.job is not None and not self.job.isFinished():
            self.job.spec.onDone = None
            self.job.spec.onEvent = None


def _statusText(job, title):
    """Status chip text of a robustness job: "Lesion split-half · 40% · 2 min left"."""
    from . import jobrunner

    text = title
    stage = (job.stage or {}).get("name")
    if stage:
        text += f" · {stage}"
    if job.progress is not None:
        text += f" {int(job.progress * 100)}%"
    eta = jobrunner.formatEta(job.eta)
    return f"{text} · {eta}" if eta else text


CARD = {
    "id": "robust",
    "title": "Robust",
    "order": ORDER_ROBUST,
    "modes": ("cohort",),
    "classic": True,
    "factory": RobustCard,
}

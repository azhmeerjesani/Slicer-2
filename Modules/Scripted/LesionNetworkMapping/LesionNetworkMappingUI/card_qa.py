"""QA card of the Map module (order 60): connectome-confound QA of every map (SPEC_LNM_ADDENDUM 3.7, QA v2, L-30).

A lesion network map partly reflects what every map of the connectome shares (its mean connectivity profile, its leading
spatial modes, its hubs), so the card says how far a map is from that, calibrated against matched nulls:

- **Calibrated chip.** The percentile of r(map, μC_r) among the same similarity of the lesion's relocated null maps
  (K and the percentile resolution are shown: "2% at K=50"). Below the 5th percentile is green, 5th-95th amber
  ("indistinguishable from relocated lesions on this axis"), above the 95th red. A group map is calibrated against null
  cohorts when a Nulls run exists, otherwise it says "not calibrated: run Nulls". The fixed 0.3 / 0.6 / 0.75 cut-offs
  are only secondary text (the hubness line); no fixed cut-off is the verdict.
- **Reference rows.** r and rho of the map with μC_r, PC1-3 and hubness, the spatial R² on these four references, with the
  definition of each reference (reference.json, ``lnm-reference-v1``) in the tooltip. No "93%" line.
- **Debiased view.** The map's OLS residual on [1, μC_r, PC1] (``similarity.residual_map``) as a separate volume
  ``desc-<x>Resid`` with the module colour table, captioned "descriptive; not an inferential correction".
- **Analytic RL z.** z = (AvgR - μC_r) / sigmaC_r (``qa.analytic_rl_zmap``), an approximate random-location null; descriptive.
- **Reference maps.** Without them: "Build reference maps (~N min)" runs ``reference build`` as a heavy job (an ETA from the
  bundle size); nothing builds them implicitly.
- The relocated null (always stated, also when it is off), the hubness line (``qa degree``), the GSR negative-peaks note,
  the coverage and space-family notice, and the N / df / preview / partial badges.

The card expands itself when the calibrated chip (or the null) is amber or red.
"""

import glob
import json
import logging
import math
import os

import qt
import slicer
from slicer.i18n import tr as _

from . import ORDER_QA, widgets

#: Chip level of the qa.py levels
_LEVELS = {"neutral": "neutral", "amber": "warn", "red": "danger", "below": "ok", "not_calibrated": "neutral"}
#: Engine cost of 50 relocated lesions per subject for a GSP1000-size connectome (SPEC 7.5: ~146 ms for L = 51 vs ~10 ms)
_NULL_SECONDS_PER_SUBJECT = 0.136
_GSP_VOXELS = 292019
#: Bytes per second of a reference build (reads of every pass, measured on a 228,483-voxel bundle: 3.16 GB in 19.8 s)
REFERENCE_BYTES_PER_SECOND = 160e6
#: Rows of the reference table: (key of the qa.json "reference" values, label, reference.json map, short definition)
REFERENCE_ROWS = (
    ("mu", "μC_r", "mu_r", _("mean r profile")),
    ("pc1", "PC1", "pcs", _("leading mode")),
    ("pc2", "PC2", "pcs", _("second mode")),
    ("pc3", "PC3", "pcs", _("third mode")),
    ("hub", _("Hubness"), "hub_abs_z", _("mean |Fz|")),
)
ROW_HEIGHT = 24
#: Covariates of the debiased view (addendum 3.7)
DEBIASED_COVARIATES = ("mu_r", "pc1")
#: Attributes of the volumes made here
DERIVED_ATTRIBUTE = "LNM.Derived"
#: Names of the files of a group (null) record that may carry the group QA of a cohort map (L-25)
NULLS_RECORDS = ("nulls.json", os.path.join("nulls", "nulls.json"))


def nullCostText(bundle, nSubjects=None):
    """Estimated extra time of the relocated-lesion null for a bundle: "≈ +2 min" or "< 1 min"."""
    if bundle is None:
        return ""
    n = nSubjects or bundle.n_subjects
    seconds = n * _NULL_SECONDS_PER_SUBJECT * bundle.n_voxels / _GSP_VOXELS
    return _("< 1 min") if seconds < 60 else _("≈ +{minutes} min").format(minutes=round(seconds / 60))


def ordinal(value):
    """1 -> "1st", 22 -> "22nd", 73 -> "73rd", 11 -> "11th"."""
    value = int(value)
    suffix = "th" if 10 <= value % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


def referenceFolders(bundlePath):
    """The reference folders of a bundle: ``derived/reference`` (all subjects), then each ``subj-*`` subject list."""
    base = os.path.join(bundlePath, "derived", "reference")
    return [base, *sorted(glob.glob(os.path.join(base, "subj-*")))]


_metadataCache = {}


def referenceMetadata(bundlePath):
    """The reference.json of a bundle (all subjects preferred, else the one built from the most subjects), or None.

    Read cheaply (no hashing): the engine decides whether a reference is valid; this tells whether one was built.
    """
    best = None
    folders = referenceFolders(bundlePath)
    for folder in folders:
        path = os.path.join(folder, "reference.json")
        try:
            stamp = os.stat(path).st_mtime_ns
        except OSError:
            continue
        cached = _metadataCache.get(path)
        if cached is None or cached[0] != stamp:
            try:
                with open(path, encoding="utf-8") as f:
                    cached = (stamp, json.load(f))
            except (OSError, ValueError):
                continue
            _metadataCache[path] = cached
        metadata = cached[1]
        size = len(((metadata or {}).get("subjects") or {}).get("ids") or [])
        if best is None or size > best[0]:
            best = (size, metadata)
        if folder == folders[0]:
            return metadata
    return best[1] if best else None


def buildSeconds(bundle):
    """Estimated seconds of ``reference build`` for a bundle from the bytes its passes read (no measurement)."""
    from LesionNetworkMappingLib import reference

    info = reference.estimate(bundle, measure=False)
    return info["read_bytes"] / REFERENCE_BYTES_PER_SECOND


def buildEtaText(seconds):
    """The ETA text of the build button, for example "~3 min" or "~2 h"."""
    if seconds < 90:
        return _("~1 min")
    if seconds < 90 * 60:
        return _("~{n} min").format(n=round(seconds / 60))
    return _("~{n} h").format(n=round(seconds / 3600))


class QACard:
    """Controller of the QA card (``widget`` is a collapsible card)."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.degreeJob = None
        self.referenceJob = None
        self.groupQaStale = False  # the Nulls run found was of other maps
        self.qa = None
        self.qc = None
        self._eta = {}
        self._reference = None  # (bundle path, Reference) loaded for the overlays
        self._overlays = {}  # kind -> (volume node, run dir, statistic label)
        self._shown = None  # kind of the overlay in the foreground
        self.widget = widgets.card(_("QA"), objectName="LNMQACard", collapsed=True)
        layout = qt.QVBoxLayout(self.widget)
        self.badgesLayout = qt.QHBoxLayout()
        self.nChip = widgets.chip("", "neutral")
        self.dfChip = widgets.chip("", "neutral")
        self.subsetChip = widgets.chip("", "neutral")
        for chip in (self.nChip, self.dfChip, self.subsetChip):
            chip.hide()
            self.badgesLayout.addWidget(chip)
        self.badgesLayout.addStretch(1)
        layout.addLayout(self.badgesLayout)

        # The calibrated chip: the verdict of the card
        calibratedRow = qt.QHBoxLayout()
        self.calibratedChip = widgets.chip(_("Not calibrated"), "neutral")
        self.calibratedChip.setObjectName("LNMQACalibratedChip")
        calibratedRow.addWidget(self.calibratedChip, 0, qt.Qt.AlignTop)
        self.calibratedLabel = self._label("LNMQACalibratedText")
        calibratedRow.addWidget(self.calibratedLabel, 1)
        layout.addLayout(calibratedRow)
        self.referenceButton = qt.QPushButton(_("Build reference maps"))
        self.referenceButton.setObjectName("LNMQABuildReferenceButton")
        self.referenceButton.setProperty("slicerRole", "secondary")
        self.referenceButton.setToolTip(_("Compute the connectome's reference maps once (mean r profile, principal components, hubness); they are stored in the bundle (derived/reference)"))
        self.referenceButton.connect("clicked()", self.buildReference)
        self.referenceButton.hide()
        layout.addWidget(self.referenceButton)
        self.referenceProgress = widgets.progressRow(objectNamePrefix="LNMQAReference")
        self.referenceProgress.widget.hide()
        layout.addWidget(self.referenceProgress.widget)

        # Similarity with the references
        self.tableCaption = self._label("LNMQAReferenceCaption", role="secondaryText")
        self.tableCaption.setText(_("Spatial correlation of the map with the connectome references"))
        layout.addWidget(self.tableCaption)
        self.table = widgets.tableView([_("Reference"), "r", "ρ"])  # noqa: RUF001
        self.table.setObjectName("LNMQAReferenceTable")
        self.table.setRowCount(len(REFERENCE_ROWS))
        self.table.setSelectionMode(qt.QAbstractItemView.NoSelection)
        self.table.setFocusPolicy(qt.Qt.NoFocus)
        self.table.setHorizontalScrollBarPolicy(qt.Qt.ScrollBarAlwaysOff)
        self.table.setVerticalScrollBarPolicy(qt.Qt.ScrollBarAlwaysOff)
        self.table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, qt.QHeaderView.Stretch)
        header.setSectionResizeMode(1, qt.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, qt.QHeaderView.ResizeToContents)
        self.table.setFixedHeight(ROW_HEIGHT * (len(REFERENCE_ROWS) + 1) + 4)
        layout.addWidget(self.table)
        self.r2Label = self._label("LNMQAR2Text", role="secondaryText")
        layout.addWidget(self.r2Label)

        # Debiased view and analytic random-location z (descriptive)
        self.debiasedCheckBox = qt.QCheckBox(_("Debiased view"))
        self.debiasedCheckBox.setObjectName("LNMQADebiasedCheckBox")
        self.debiasedCheckBox.setToolTip(_("Show the map minus what μC_r and PC1 explain, as a separate volume (descriptive; not an inferential correction)"))
        self.debiasedCheckBox.connect("toggled(bool)", self.setDebiased)
        layout.addWidget(self.debiasedCheckBox)
        self.debiasedLabel = self._label("LNMQADebiasedText", role="secondaryText")
        layout.addWidget(self.debiasedLabel)
        self.analyticButton = qt.QPushButton(_("Analytic RL z"))
        self.analyticButton.setObjectName("LNMQAAnalyticButton")
        self.analyticButton.setProperty("slicerRole", "secondary")
        self.analyticButton.setCheckable(True)
        self.analyticButton.setToolTip(_("Approximate random-location null: z = (mean r - μC_r) / \u03c3C_r at every voxel (descriptive)"))
        self.analyticButton.connect("toggled(bool)", self.setAnalytic)
        layout.addWidget(self.analyticButton)
        self.analyticLabel = self._label("LNMQAAnalyticText", role="secondaryText")
        layout.addWidget(self.analyticLabel)

        # Secondary: the hubness line with its fixed cut-offs, and the relocated null
        degreeRow = qt.QHBoxLayout()
        self.degreeChip = widgets.chip(_("Hubness"), "neutral")
        self.degreeChip.setObjectName("LNMQADegreeChip")
        degreeRow.addWidget(self.degreeChip, 0, qt.Qt.AlignTop)
        self.degreeLabel = self._label("LNMQADegreeText", role="secondaryText")
        degreeRow.addWidget(self.degreeLabel, 1)
        layout.addLayout(degreeRow)
        self.degreeButton = qt.QPushButton(_("Compute degree map (~2-3 min for GSP1000)"))
        self.degreeButton.setObjectName("LNMQADegreeButton")
        self.degreeButton.setProperty("slicerRole", "secondary")
        self.degreeButton.setToolTip(_("Compute the connectome's hubness (degree) map once; it is stored in the bundle (derived/degree.npy)"))
        self.degreeButton.connect("clicked()", self.computeDegree)
        self.degreeButton.hide()
        layout.addWidget(self.degreeButton)
        self.degreeProgress = widgets.progressRow(objectNamePrefix="LNMQADegree")
        self.degreeProgress.widget.hide()
        layout.addWidget(self.degreeProgress.widget)

        nullRow = qt.QHBoxLayout()
        self.nullChip = widgets.chip(_("Null"), "neutral")
        self.nullChip.setObjectName("LNMQANullChip")
        nullRow.addWidget(self.nullChip, 0, qt.Qt.AlignTop)
        self.nullLabel = self._label("LNMQANullText")
        nullRow.addWidget(self.nullLabel, 1)
        self.nullRunButton = qt.QPushButton(_("Run"))
        self.nullRunButton.setObjectName("LNMQANullRunButton")
        self.nullRunButton.setProperty("slicerRole", "secondary")
        self.nullRunButton.setToolTip(_("Turn the relocated-lesion null on and map again"))
        self.nullRunButton.connect("clicked()", self.runWithNull)
        self.nullRunButton.hide()
        nullRow.addWidget(self.nullRunButton, 0, qt.Qt.AlignTop)
        layout.addLayout(nullRow)

        self.referenceLabel = self._label("LNMQAReferenceText", role="secondaryText")
        self.referenceLabel.hide()
        layout.addWidget(self.referenceLabel)
        self.gsrLabel = self._label("LNMQAGsrText", role="secondaryText")
        self.gsrLabel.hide()
        layout.addWidget(self.gsrLabel)
        self.coverageLabel = self._label("LNMQACoverageText", role="secondaryText")
        layout.addWidget(self.coverageLabel)

        for name in ("runChanged", "bundleChanged", "seedChanged"):
            getattr(ctx, name).append(self.refresh)
        self._lastMode = self._mode()
        self._observedNode = None
        self._observer = None
        self.refresh()
        # The seed mode (lesion or cohort) is a parameter of the module, which the Map widget sets after it built the cards
        qt.QTimer.singleShot(0, self._observeParameterNode)

    def _observeParameterNode(self):
        try:
            self.widget.objectName
        except (ValueError, RuntimeError):
            return  # the widget was destroyed right after its setup
        wrapper = self.ctx.parameterNode
        node = getattr(wrapper, "parameterNode", wrapper)
        if node is None or node is self._observedNode or not hasattr(node, "AddObserver"):
            return
        import vtk

        self._observedNode = node
        self._observer = node.AddObserver(vtk.vtkCommand.ModifiedEvent, lambda caller, event: self._onParameterModified())
        self._onParameterModified()

    def _onParameterModified(self):
        mode = self._mode()
        if mode != self._lastMode:
            self._lastMode = mode
            self.refresh()

    @staticmethod
    def _label(objectName, role=None):
        label = qt.QLabel("")
        label.setObjectName(objectName)
        label.setWordWrap(True)
        label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        if role:
            label.setProperty("slicerRole", role)
        return label

    #
    # Card protocol
    #

    def onRunEvent(self, event):
        kind = event.get("event")
        if kind == "start":
            self.qa = None
            self._dropOverlays()
            qc = event.get("qc") or []
            self.qc = qc[0] if qc else None
            self.refresh(running=True)
        elif kind == "snapshot":
            self.refresh(running=True, n=event.get("n"), partial=True)
        elif kind == "done":
            qa = event.get("qa") or {}
            self.qa = next(iter(qa.values()), None) if qa else None
            qc = event.get("qc") or []
            self.qc = qc[0] if qc else self.qc

    def onRunSettingsChanged(self):
        self.refresh()

    def onResultChanged(self):
        pass

    def onStudyChanged(self):
        self.refresh()

    def onCohortMapsChanged(self, path=None):
        self.refresh()

    def cleanup(self):
        for name in ("runChanged", "bundleChanged", "seedChanged"):
            callbacks = getattr(self.ctx, name, [])
            if self.refresh in callbacks:
                callbacks.remove(self.refresh)
        if self._observedNode is not None and self._observer is not None:
            self._observedNode.RemoveObserver(self._observer)
        self._observedNode = None
        self._dropOverlays()

    #
    # Display
    #

    def _mode(self):
        node = self.ctx.parameterNode
        try:
            return (node.seedMode or "lesion") if node is not None else "lesion"
        except AttributeError:
            return "lesion"

    def _nullRelocations(self):
        node = self.ctx.parameterNode
        try:
            return int(node.nullRelocations) if node is not None else 50
        except (AttributeError, TypeError, ValueError):
            return 50

    def _hasReferenceFiles(self, bundle):
        return bundle is not None and referenceMetadata(bundle.path) is not None

    def _referenceDefinitionText(self, key, metadata):
        """Tooltip of a reference row: the definition and exactness recorded in reference.json."""
        name = next((row[2] for row in REFERENCE_ROWS if row[0] == key), None)
        item = ((metadata or {}).get("maps") or {}).get(name) or {}
        if not item.get("definition"):
            return ""
        return f"{item.get('label', '')} ({item.get('exactness', '')})\n{item['definition']}"

    def _fillTable(self, values, metadata):
        """Fill the reference rows from the "reference" values of qa.json (None clears them)."""
        for index, (key, label, _mapName, short) in enumerate(REFERENCE_ROWS):
            r = (values or {}).get(f"{key}_r" if key != "mu" else "mu_r")
            rho = (values or {}).get(f"{key}_rho" if key != "mu" else "mu_rho")
            exactness = (((metadata or {}).get("maps") or {}).get(_mapName) or {}).get("exactness")
            name = f"{label} · {short}" + (f" ({exactness})" if exactness else "")
            cells = (name, self._number(r), self._number(rho))
            for column, text in enumerate(cells):
                item = self.table.item(index, column)
                if item is None:
                    item = qt.QTableWidgetItem()
                    self.table.setItem(index, column, item)
                item.setText(text)
                item.setToolTip(self._referenceDefinitionText(key, metadata) if column == 0 else "")
                alignment = (qt.Qt.AlignLeft if column == 0 else qt.Qt.AlignRight) | qt.Qt.AlignVCenter
                try:
                    item.setTextAlignment(alignment)
                except (TypeError, ValueError):
                    item.setTextAlignment(int(alignment))  # Qt 5 takes an int, Qt 6 the flags

    @staticmethod
    def _number(value):
        return f"{value:.2f}" if isinstance(value, (int, float)) and math.isfinite(value) else "\u2013"

    def _setCalibrated(self, level, chipText, text):
        widgets.setChipLevel(self.calibratedChip, _LEVELS.get(level, "neutral") if level in _LEVELS else level, chipText)
        self.calibratedLabel.setText(text)

    def _calibratedFromValues(self, values):
        """(level, chip text, label text) of a "reference" qa value dict (single lesion or group)."""
        level = values.get("level") or "not_calibrated"
        percentile, k = values.get("percentile"), values.get("k") or 0
        sample = values.get("mu_r_sample")
        muR = sample if isinstance(sample, (int, float)) and math.isfinite(sample) else values.get("mu_r")
        muText = f"r(μC_r) = {muR:.2f} · " if isinstance(muR, (int, float)) and math.isfinite(muR) else ""
        group = (values.get("null_kind") or "relocated") != "relocated"
        nulls = _("null cohorts") if group else _("relocated lesions")
        if level == "not_calibrated" or percentile is None:
            return "not_calibrated", _("Not calibrated"), f"{muText}{values.get('text') or _('not calibrated')}"
        chipText = _("{p} percentile").format(p=ordinal(round(percentile)))
        text = _("{mu}{p} percentile of {k} {nulls}: {text}").format(mu=muText, p=ordinal(round(percentile)), k=int(k), nulls=nulls, text=values.get("text") or "")
        return level, chipText, text

    def _findGroupQa(self):
        """The group map QA of a Nulls run (L-25) of the current cohort maps, or None.

        ``nulls.json`` carries ``group_qa`` = {"overlap": values, "mean": values, ...}: the percentile of r(group map, μC_r)
        among the null cohorts' maps. The mean map of the rule's statistic is the group map of the Map module, so it is
        preferred; the run folder is ``nulls`` next to the cohort maps (the newest one wins), or the maps folder itself.
        A run of other maps is not used (``self.groupQaStale`` is then True): see :meth:`_nullsMatchMaps`.
        """
        self.groupQaStale = False
        mapsPath = getattr(self.ctx, "cohortMaps", None)
        if not mapsPath:
            return None
        folder = os.path.dirname(os.fspath(mapsPath))
        candidates = [os.path.join(folder, name) for name in NULLS_RECORDS]
        candidates += glob.glob(os.path.join(folder, "nulls*", "nulls.json"))
        found = []
        for path in dict.fromkeys(candidates):
            try:
                found.append((os.stat(path).st_mtime_ns, path))
            except OSError:
                continue
        for stamp, path in sorted(found, reverse=True):
            try:
                with open(path, encoding="utf-8") as f:
                    record = json.load(f)
            except (OSError, ValueError):
                continue
            group = record.get("group_qa") if isinstance(record, dict) else None
            if not isinstance(group, dict):
                continue
            if not self._nullsMatchMaps(record, stamp, os.fspath(mapsPath)):
                self.groupQaStale = True
                continue
            for key in ("mean", "overlap"):
                values = group.get(key)
                if isinstance(values, dict) and "level" in values:
                    return values
            if "level" in group and group.get("available", True):
                return group
        return None

    @staticmethod
    def _nullsMatchMaps(record, stamp, mapsPath):
        """True when a Nulls run (its ``nulls.json`` record, modified at ``stamp`` ns) belongs to the cohort maps at ``mapsPath``.

        The run must be newer than the maps.tsv, name the same cohortmaps run when both know one, and have analysed the
        maps.tsv subjects (the cases of the maps, the others listed as excluded): a run of an earlier cohort says nothing
        about the current group map.
        """
        try:
            if stamp < os.stat(mapsPath).st_mtime_ns:
                return False
        except OSError:
            return False
        from LesionNetworkMappingLib import cohortmaps

        try:
            ids = {row["participant_id"] for row in cohortmaps.read_maps_tsv(mapsPath, resolve_paths=False)}
            current = (cohortmaps.read_record(os.path.join(os.path.dirname(mapsPath), cohortmaps.COHORTMAPS_JSON)) or {}).get("run_id")
        except (OSError, ValueError, KeyError):
            return False
        used = (record.get("inputs") or {}).get("cohortmaps_run")
        if used and current and used != current:
            return False
        participants = record.get("participants")
        if not isinstance(participants, list):
            return False
        accounted = set(participants) | {item.get("participant_id") for item in record.get("excluded") or [] if isinstance(item, dict)}
        return bool(ids) and set(participants) <= ids and accounted == ids

    def refresh(self, running=False, n=None, partial=None):
        run = self.ctx.currentRun
        bundle = self.ctx.bundle
        cohort = self._mode() == "cohort"
        job = getattr(run, "job", None) if run is not None else None
        if run is not None and not running:
            done = getattr(job, "doneEvent", None) or {}
            qa = done.get("qa") or {}
            if qa:
                self.qa = next(iter(qa.values()), None)
            start = getattr(job, "startEvent", None) or {}
            if start.get("qc"):
                self.qc = start["qc"][0]
        n = n if n is not None else (run.n if run is not None else None)
        partial = partial if partial is not None else bool(run is not None and run.partial)
        preview = bool(run is not None and (run.runJson.get("desc_suffix") or "").startswith("PreviewN"))
        levels = []
        qa = self.qa or {}
        reference = qa.get("reference") if qa else None
        hasFiles = self._hasReferenceFiles(bundle)
        metadata = referenceMetadata(bundle.path) if bundle is not None else None

        # What the mode shows: single-lesion rows or the group chip only
        for widget in (self.nChip, self.dfChip, self.subsetChip):
            if cohort:
                widget.hide()
        for widget in (self.debiasedCheckBox, self.debiasedLabel, self.analyticButton, self.analyticLabel, self.degreeChip, self.degreeLabel, self.nullChip, self.nullLabel, self.coverageLabel):
            widget.setVisible(not cohort)
        if cohort:
            self.degreeButton.hide()
            self.degreeProgress.widget.hide()
            self.nullRunButton.hide()
            self.referenceLabel.hide()

        # Badges: N, df, preview / partial
        if not cohort:
            if n:
                widgets.setChipLevel(self.nChip, "neutral", f"N={int(n):,}")
                widgets.setChipLevel(self.dfChip, "neutral", f"df={max(int(n) - 1, 0):,}")
            self.nChip.setVisible(bool(n))
            self.dfChip.setVisible(bool(n))
            subset = _("partial") if partial else (_("Preview") if preview else "")
            if subset:
                widgets.setChipLevel(self.subsetChip, "warn" if partial else "neutral", subset)
            self.subsetChip.setVisible(bool(subset))

        # The calibrated chip and the reference rows
        shownValues = None
        if cohort:
            group = self._findGroupQa() if bundle is not None else None
            if bundle is None:
                self._setCalibrated("neutral", _("Not calibrated"), _("Group maps: choose a connectome."))
            elif not hasFiles:
                self._setCalibrated("warn", _("Not calibrated"), _("Group maps: not calibrated, this connectome has no reference maps yet."))
            elif group is None and self.groupQaStale:
                self._setCalibrated("neutral", _("Not calibrated"), _("Group maps: not calibrated — the Nulls run is of other maps, run Nulls again."))
            elif group is None:
                self._setCalibrated("neutral", _("Not calibrated"), _("Group maps: not calibrated — run Nulls to compare a group map with null cohorts."))
            else:
                level, chipText, text = self._calibratedFromValues(group)
                self._setCalibrated(level, chipText, _("Group map: {text}").format(text=text))
                levels.append(group.get("level") or "not_calibrated")
                shownValues = group if group.get("mu_r") is not None else None
        elif bundle is None:
            self._setCalibrated("neutral", _("Not calibrated"), _("Choose a connectome to calibrate the QA."))
        elif running or partial:
            self._setCalibrated("neutral", _("Not calibrated"), _("Calibrated when the map is complete."))
        elif reference:
            level, chipText, text = self._calibratedFromValues(reference)
            self._setCalibrated(level, chipText, text)
            levels.append(reference.get("level") or "not_calibrated")
            shownValues = reference
        elif run is None:
            self._setCalibrated("neutral", _("Not calibrated"), _("Shown once the lesion is mapped."))
        elif not hasFiles:
            self._setCalibrated("warn", _("Not calibrated"), _("Not calibrated: this connectome has no reference maps yet. Build them once to compare the map with what any lesion gives."))
        else:
            self._setCalibrated("warn", _("Not calibrated"), _("Not calibrated: this map was computed without valid reference maps (made before them, or they are out of date). Map again."))
        self.tableCaption.setVisible(shownValues is not None)
        self.table.setVisible(shownValues is not None)
        self.r2Label.setVisible(shownValues is not None)
        if shownValues is not None:
            self._fillTable(shownValues, metadata)
            from LesionNetworkMappingLib import qa as qaLib

            r2 = shownValues.get("r2_ref")
            text = (qaLib.R2_REFERENCE_LABEL[:1].upper() + qaLib.R2_REFERENCE_LABEL[1:]) + (f": {r2:.2f}" if isinstance(r2, (int, float)) and math.isfinite(r2) else ": \u2013")
            partialValues = shownValues.get("partial_pc1_mu") or {}
            pc1 = partialValues.get("pc1_given_mu_r")
            if isinstance(pc1, (int, float)) and math.isfinite(pc1):
                text += " · " + _("PC1 beyond μC_r: r = {r:.2f}").format(r=pc1)
            if metadata is not None:
                text += " · " + _("reference {definition}, {k} sampled seeds").format(definition=metadata.get("definition", ""), k=metadata.get("K_seeds", "?"))
            self.r2Label.setText(text)

        # Reference maps: build action
        buildable = bundle is not None and not hasFiles
        referenceRunning = self.referenceJob is not None and not self.referenceJob.isFinished()
        self.referenceButton.setVisible(buildable and not referenceRunning)
        self.referenceProgress.widget.setVisible(referenceRunning)
        if buildable:
            self.referenceButton.setText(_("Build reference maps ({eta})").format(eta=self._etaText(bundle)))

        # Debiased view and analytic z need the reference of the run
        usable = (not cohort) and bool(reference) and run is not None and not running and not partial and bundle is not None
        self.debiasedCheckBox.setEnabled(usable)
        self.analyticButton.setEnabled(usable and metadata is not None)
        if not usable:
            self.debiasedLabel.setText(_("Debiased view: needs a finished map with reference maps."))
            self.analyticLabel.setText("")
        elif "debiased" not in self._overlays:
            self.debiasedLabel.setText(_("Descriptive; not an inferential correction."))
        if usable and "analytic" not in self._overlays:
            self.analyticLabel.setText(_("Descriptive; approximate random-location null."))

        # Hubness (degree) similarity: secondary text with the fixed cut-offs, never the verdict
        if not cohort:
            self._refreshDegree(run, bundle, qa, running, partial)
            levels.extend(self._refreshNull(run, bundle, qa, running, partial, n))
            self._refreshReferenceNote(reference)
            self.coverageLabel.setText(self._coverageText(bundle))

        # GSR negative-peaks note
        note = None
        if bundle is not None:
            from LesionNetworkMappingLib import thresholds

            logic = self.ctx.logic
            try:
                gsr = logic.gsr(bundle) if logic is not None else None
            except Exception:
                gsr = None
            note = thresholds.negative_peaks_note(gsr)
        self.gsrLabel.setVisible(bool(note))
        self.gsrLabel.setText(note or "")

        if any(level in ("amber", "red") for level in levels):
            self.widget.collapsed = False

    def _etaText(self, bundle):
        cached = self._eta.get(bundle.path)
        if cached is None:
            try:
                cached = buildEtaText(buildSeconds(bundle))
            except Exception:
                logging.exception("Lesion network mapping: the reference build could not be estimated")
                cached = _("time unknown")
            self._eta[bundle.path] = cached
        return cached

    def _refreshDegree(self, run, bundle, qa, running, partial):
        from LesionNetworkMappingLib import qa as qaLib

        hasDegree = bundle is not None and self._bundleHasDegree(bundle)
        degreeR = qa.get("degree_r")
        if degreeR is None and hasDegree and run is not None and not running and not partial:
            degreeR, degreeRho, level = self._degreeFromMap(bundle)
            if degreeR is not None:
                qa = dict(qa, degree_r=degreeR, degree_rho=degreeRho, degree_level=level)
        if degreeR is not None:
            _level, text = qaLib.chip_level(degreeR)
            rho = qa.get("degree_rho")
            rhoText = f", ρ = {rho:.2f}" if isinstance(rho, (int, float)) and math.isfinite(rho) else ""  # noqa: RUF001
            self.degreeLabel.setText(_("Hubness (degree) similarity: r = {r:.2f}{rho}. Fixed cut-offs, secondary: {text}").format(r=degreeR, rho=rhoText, text=text))
            widgets.setChipLevel(self.degreeChip, "neutral", _("Hubness"))
        elif bundle is None:
            self.degreeLabel.setText(_("Hubness (degree) similarity: choose a connectome."))
            widgets.setChipLevel(self.degreeChip, "neutral", _("Hubness"))
        elif not hasDegree:
            self.degreeLabel.setText(_("Hubness (degree) similarity: not available, this connectome has no degree map yet."))
            widgets.setChipLevel(self.degreeChip, "warn", _("Hubness"))
        elif running or partial:
            self.degreeLabel.setText(_("Hubness (degree) similarity: computed when the map is complete."))
            widgets.setChipLevel(self.degreeChip, "neutral", _("Hubness"))
        else:
            self.degreeLabel.setText(_("Hubness (degree) similarity: computed with every map."))
            widgets.setChipLevel(self.degreeChip, "neutral", _("Hubness"))
        degreeRunning = self.degreeJob is not None and not self.degreeJob.isFinished()
        self.degreeButton.setVisible(bundle is not None and not hasDegree and not degreeRunning)
        self.degreeProgress.widget.setVisible(degreeRunning)

    def _refreshNull(self, run, bundle, qa, running, partial, n):
        """The relocated-lesion null is always stated; return the levels that expand the card."""
        relocations = self._nullRelocations()
        null = qa.get("null") if qa else None
        self.nullRunButton.hide()
        levels = []
        if null and null.get("r_median") is not None:
            from LesionNetworkMappingLib import qa as qaLib

            level = qa.get("null_level") or "neutral"
            self.nullLabel.setText(qaLib.describe_null(null) + ".")
            widgets.setChipLevel(self.nullChip, _LEVELS.get(level, "neutral"), _("Null"))
            levels.append(level)
        elif run is not None and not running and not partial and not null:
            ran = (run.runJson.get("null") or {}).get("k") if isinstance(run.runJson.get("null"), dict) else None
            if ran:
                self.nullLabel.setText(_("Relocated null: not available for this lesion (no relocated lesion fits in the brain mask)."))
                widgets.setChipLevel(self.nullChip, "warn", _("Null"))
            else:
                self.nullLabel.setText(_("Relocated null: off for this run ({cost}). Run it to compare the map with relocated lesions of the same shape.").format(cost=nullCostText(bundle, n)))
                widgets.setChipLevel(self.nullChip, "warn", _("Null"))
                self.nullRunButton.show()
        elif partial:
            self.nullLabel.setText(_("Relocated null: not computed, the run stopped at N={n} (partial).").format(n=n))
            widgets.setChipLevel(self.nullChip, "warn", _("Null"))
        elif relocations > 0:
            self.nullLabel.setText(_("Relocated null: on, {k} relocated lesions in the same pass (computed with the map).").format(k=relocations))
            widgets.setChipLevel(self.nullChip, "neutral", _("Null"))
        else:
            self.nullLabel.setText(_("Relocated null: off ({cost}). Turn on Specificity QA in Compute to compare the map with relocated lesions.").format(cost=nullCostText(bundle)))
            widgets.setChipLevel(self.nullChip, "warn", _("Null"))
        return levels

    def _refreshReferenceNote(self, reference):
        """A line that states what the calibrated chip rests on (the reference build and the null voxel sample)."""
        if reference and reference.get("k"):
            self.referenceLabel.setText(_("Calibrated on the same voxel sample as the {k} relocated null maps; percentile resolution {res:.3g}% at K={k}.").format(
                k=int(reference["k"]), res=float(reference.get("resolution") or 100.0 / reference["k"])))
            self.referenceLabel.show()
        else:
            self.referenceLabel.hide()

    def _coverageText(self, bundle):
        qc = self.qc
        if not qc or bundle is None:
            return _("Coverage: shown once the lesion is mapped.")
        spacing = abs(float(bundle.grid.spacing[0]))
        text = _("Coverage: {voxels:,} voxels on the connectome grid ({spacing:g} mm) · fractional sum {total:.1f} · {outside:.0%} outside the connectome mask").format(
            voxels=int(qc.get("voxels_in_mask", 0)), spacing=spacing, total=float(qc.get("weight_total", 0.0)), outside=float(qc.get("weight_outside_fraction", 0.0)))
        segNode, _segmentId = self.ctx.seed()
        normalization = segNode.GetAttribute("LNM.Normalization") if segNode is not None else None
        if normalization:
            text += " · " + _("normalized ({details})").format(details=normalization)
        return text

    @staticmethod
    def _bundleHasDegree(bundle):
        return os.path.isfile(os.path.join(bundle.path, "derived", "degree.npy"))

    def _degreeFromMap(self, bundle):
        """Degree similarity of the current T map with a degree map computed after the run."""
        run = self.ctx.currentRun
        paths = [path for path in (run.outputs if run is not None else []) if "_desc-T" in path]
        if not paths:
            return None, None, "neutral"
        try:
            from LesionNetworkMappingLib import nifti
            from LesionNetworkMappingLib import qa as qaLib

            data, _affine, _header = nifti.read_nifti(paths[0])
            values = data.reshape(data.shape[:3]).ravel()[bundle.mask_indices]
            r, rho = qaLib.degree_similarity(values, bundle.degree)
        except Exception:
            logging.exception("Lesion network mapping: the degree similarity could not be computed")
            return None, None, "neutral"
        r = r if math.isfinite(r) else None
        rho = rho if math.isfinite(rho) else None
        return r, rho, qaLib.chip_level(r)[0]

    #
    # Overlays: debiased view and analytic z
    #

    def _statisticPath(self, label):
        """The run's map file whose "desc" label is ``label`` ("T", "AvgRFz" or "AvgR"), or None."""
        import re

        run = self.ctx.currentRun
        for path in (run.outputs if run is not None else []):
            match = re.search(r"_desc-(AvgRFz|AvgR|T)(?=[^A-Za-z]|PreviewN|Subset|_funcmap)", os.path.basename(path))
            if match and match.group(1) == label:
                return path
        return None

    def _displayedLabel(self):
        node = self.ctx.parameterNode
        try:
            statistic = node.statistic if node is not None else "T"
        except AttributeError:
            statistic = "T"
        return {"T": "T", "Fz": "AvgRFz", "r": "AvgR"}.get(statistic, "T")

    def _loadedReference(self):
        """The bundle's reference maps (cached for the bundle in use), or None."""
        bundle = self.ctx.bundle
        if bundle is None:
            return None
        if self._reference is not None and self._reference[0] == bundle.path:
            return self._reference[1]
        from LesionNetworkMappingLib import qa as qaLib

        loaded = qaLib.load_reference(bundle, verify=False)
        self._reference = (bundle.path, loaded)
        return loaded

    def _maskedValues(self, path, bundle):
        from LesionNetworkMappingLib import nifti

        data, _affine, _header = nifti.read_nifti(path)
        return data.reshape(data.shape[:3]).ravel()[bundle.mask_indices].astype("float64")

    def _writeOverlay(self, sourcePath, labelFrom, labelTo, values, description, sidecar):
        """Write a derived map next to the run's maps (exact bundle header) and a JSON sidecar; return its path."""
        import numpy as np

        from LesionNetworkMappingLib import engine, nifti

        bundle = self.ctx.bundle
        path = sourcePath.replace(f"_desc-{labelFrom}", f"_desc-{labelTo}", 1)
        volume = engine.unmask(np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0), bundle)
        nifti.write_nifti(path, volume, bundle.affine, descrip=description)
        with open(path.replace(".nii.gz", ".json"), "w", encoding="utf-8") as f:
            json.dump(sidecar, f, indent=2, ensure_ascii=False)
            f.write("\n")
        return path

    def _makeVolume(self, path, name):
        """A scalar volume of a derived map with the module colour table; hidden from Draw and not saved with the scene."""
        from LesionNetworkMappingLib import display

        widget = getattr(slicer.modules, "LesionNetworkMappingWidget", None)
        logic = widget.logic if widget is not None and widget.logic is not None else self.ctx.logic
        data, affine = logic.readMap(path)
        volume = slicer.util.addVolumeFromArray(data, ijkToRAS=affine, name=name, nodeClassName="vtkMRMLScalarVolumeNode")
        volume.SetAttribute(display.RESULT_ATTRIBUTE, "1")
        volume.SetAttribute(DERIVED_ATTRIBUTE, "1")
        volume.SetSaveWithScene(False)
        display.applyResultDisplay(volume)
        return volume

    def _lesionPrefix(self, path):
        return os.path.basename(path).split("_conn-")[0]

    def _showForeground(self, volume):
        if volume is not None:
            slicer.util.setSliceViewerLayers(foreground=volume)

    def _restoreForeground(self):
        node = self.ctx.parameterNode
        volume = None
        try:
            volume = node.resultVolume if node is not None else None
        except AttributeError:
            volume = None
        volume = volume or self.ctx.resultVolume
        if volume is not None and volume.GetScene() is not None:
            slicer.util.setSliceViewerLayers(foreground=volume)

    def _setChecked(self, widget, checked):
        blocked = widget.blockSignals(True)
        widget.setChecked(checked)
        widget.blockSignals(blocked)

    def setDebiased(self, checked):
        """Show (or hide) the debiased view: the map's residual on [1, μC_r, PC1] as the volume ``desc-<x>Resid``."""
        if not checked:
            if self._shown == "debiased":
                self._shown = None
                self._restoreForeground()
            return None
        if self._shown == "analytic":
            self._setChecked(self.analyticButton, False)
        volume = self.debiasedVolume()
        if volume is None:
            self._setChecked(self.debiasedCheckBox, False)
            return None
        self._shown = "debiased"
        self._showForeground(volume)
        return volume

    def debiasedVolume(self):
        """Make (once per run) the debiased volume of the displayed statistic; return it, or None when it cannot be made."""
        from LesionNetworkMappingLib import similarity

        run, bundle = self.ctx.currentRun, self.ctx.bundle
        label = self._displayedLabel()
        cached = self._overlays.get("debiased")
        if cached is not None and cached[0].GetScene() is not None and run is not None and cached[1] == run.runDir and cached[2] == label:
            return cached[0]
        source = self._statisticPath(label)
        reference = self._loadedReference()
        if run is None or bundle is None or source is None or reference is None:
            self.debiasedLabel.setText(_("The debiased view needs a finished map and valid reference maps."))
            return None
        try:
            values = self._maskedValues(source, bundle)
            residual = similarity.residual_map(values, reference.covariates(DEBIASED_COVARIATES))
            description = f"LNM {label} residual on [1,mu_r,PC1]; descriptive"  # the NIfTI description holds 80 latin-1 bytes
            sidecar = {"description": f"LNM {label} residual on [1, mu_r, PC1]; {similarity.DEBIASED_LABEL}", "caption": similarity.DEBIASED_LABEL, "covariates": list(DEBIASED_COVARIATES), "statistic": label, "N": run.n,
                       "reference_definition": reference.definition, "reference_sha256": reference.sha256, "source": os.path.basename(source)}
            path = self._writeOverlay(source, label, f"{label}Resid", residual, description, sidecar)
            name = f"{self._lesionPrefix(source)} LNM {label}Resid (debiased, descriptive)"
            volume = self._makeVolume(path, name)
        except Exception as exc:
            logging.exception("Lesion network mapping: the debiased view could not be made")
            self.debiasedLabel.setText(_("The debiased view could not be made: {reason}").format(reason=exc))
            return None
        self._removeOverlay("debiased")
        self._overlays["debiased"] = (volume, run.runDir, label)
        self.debiasedLabel.setText(_("Descriptive; not an inferential correction: {label} minus what μC_r and PC1 explain, in the volume {name}.").format(label=label, name=volume.GetName()))
        return volume

    def setAnalytic(self, checked):
        """Show (or hide) the analytic random-location z map ``desc-AnalyticRLz`` (descriptive)."""
        if not checked:
            if self._shown == "analytic":
                self._shown = None
                self._restoreForeground()
            return None
        if self._shown == "debiased":
            self._setChecked(self.debiasedCheckBox, False)
        volume = self.analyticVolume()
        if volume is None:
            self._setChecked(self.analyticButton, False)
            return None
        self._shown = "analytic"
        self._showForeground(volume)
        return volume

    def analyticVolume(self):
        from LesionNetworkMappingLib import qa as qaLib

        run, bundle = self.ctx.currentRun, self.ctx.bundle
        cached = self._overlays.get("analytic")
        if cached is not None and cached[0].GetScene() is not None and run is not None and cached[1] == run.runDir:
            return cached[0]
        source = self._statisticPath("AvgR")
        reference = self._loadedReference()
        if run is None or bundle is None or source is None or reference is None:
            self.analyticLabel.setText(_("The analytic z needs a finished map and valid reference maps."))
            return None
        try:
            z = qaLib.analytic_rl_zmap(self._maskedValues(source, bundle), reference)
            if z is None:
                self.analyticLabel.setText(_("This reference has no \u03c3C_r map: build the reference maps again."))
                return None
            sidecar = {"description": qaLib.ANALYTIC_RL_LABEL, "statistic": "AvgR", "N": run.n, "reference_definition": reference.definition,
                       "reference_sha256": reference.sha256, "source": os.path.basename(source)}
            path = self._writeOverlay(source, "AvgR", "AnalyticRLz", z, "LNM analytic random-location z; descriptive", sidecar)
            volume = self._makeVolume(path, f"{self._lesionPrefix(source)} LNM analytic RL z (descriptive)")
        except Exception as exc:
            logging.exception("Lesion network mapping: the analytic random-location z map could not be made")
            self.analyticLabel.setText(_("The analytic z could not be made: {reason}").format(reason=exc))
            return None
        self._removeOverlay("analytic")
        self._overlays["analytic"] = (volume, run.runDir, "AvgR")
        self.analyticLabel.setText(qaLib.ANALYTIC_RL_LABEL[:1].upper() + qaLib.ANALYTIC_RL_LABEL[1:] + ".")
        return volume

    def _removeOverlay(self, kind):
        entry = self._overlays.pop(kind, None)
        if entry is not None and entry[0].GetScene() is not None:
            slicer.mrmlScene.RemoveNode(entry[0])

    def _dropOverlays(self):
        """Forget the derived maps of the previous run (a new run, or the card is destroyed)."""
        if self._shown is not None:
            self._shown = None
            self._restoreForeground()
        for kind in list(self._overlays):
            self._removeOverlay(kind)
        if hasattr(self, "debiasedCheckBox"):
            self._setChecked(self.debiasedCheckBox, False)
            self._setChecked(self.analyticButton, False)

    #
    # Actions
    #

    def computeDegree(self):
        """Run ``qa degree`` on the current connectome as a job (it writes derived/degree.npy and degree.json)."""
        from . import jobrunner

        bundle = self.ctx.bundle
        if bundle is None:
            return None
        spec = jobrunner.JobSpec(
            "LesionNetworkMapping", ["job", "LesionNetworkMappingLib.qa", "degree", "--bundle", bundle.path], title=_("Degree map"), heavy=True,
            kind="degree", name="degree", onEvent=lambda event: self.degreeProgress.update(), onDone=self._onDegreeDone)
        self.degreeJob = self.ctx.submit(spec)
        self.degreeProgress.setJob(self.degreeJob)
        self.refresh()
        return self.degreeJob

    def _onDegreeDone(self, job):
        self.degreeProgress.update()
        if job.state == "failed":
            slicer.util.warningDisplay(_("The degree map could not be computed: {reason}").format(reason=job.failureReason), windowTitle=_("Degree map"))
        self.refresh()

    def buildReference(self):
        """Build the reference maps of the current connectome (``reference build``, a heavy job; resumes after a Stop)."""
        from . import jobrunner

        bundle = self.ctx.bundle
        if bundle is None or (self.referenceJob is not None and not self.referenceJob.isFinished()):
            return None
        spec = jobrunner.JobSpec(
            "LesionNetworkMappingLib.reference", ["build", "--bundle", bundle.path, "--resume"], title=_("Reference maps"), heavy=True, kind="reference",
            name="reference", runRoot=bundle.path, onEvent=lambda event: self.referenceProgress.update(), onDone=self._onReferenceDone)
        self.referenceJob = self.ctx.submit(spec)
        self.referenceProgress.setJob(self.referenceJob)
        self.refresh()
        return self.referenceJob

    def _onReferenceDone(self, job):
        self.referenceProgress.update()
        self._reference = None
        if job.state == "done":
            self.referenceProgress.setProgress(1.0, _("Reference maps built. Map again to calibrate the chip."))
        elif job.state == "failed":
            slicer.util.warningDisplay(_("The reference maps could not be built: {reason}").format(reason=job.failureReason), windowTitle=_("Reference maps"))
        self.refresh()
        self.referenceProgress.widget.setVisible(job.state == "done")

    def runWithNull(self):
        """Turn the relocated-lesion null on and map again."""
        widget = getattr(slicer.modules, "LesionNetworkMappingWidget", None)
        if widget is None:
            return
        widget.nullCheckBox.setChecked(True)
        widget.onPrimary()


CARD = {
    "id": "qa",
    "title": "QA",
    "order": ORDER_QA,
    "modes": ("lesion", "cohort"),
    "factory": QACard,
}

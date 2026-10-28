"""Cohort card of the Map module (order 70, Seed = Cohort; SPEC 7.8, 7.9, addendum §3.3 and §7.1).

The card hosts the tabs of the cohort analyses: its own **Overlap** tab, then the ``cohort_*.py`` plug-in tabs
(``TAB = {id, title, order, factory(ctx)}``; Contrast and Symptom: L-20, VLSM: L-50), sorted by order. Every tab
reads the cohort's per-lesion maps, the ``maps.tsv`` of the cohort maps job (Compute > Map all N lesions, or the
"Run everything" pipeline): ``currentMapsTsv()``; a tab that needs them and finds none calls ``mapLesions()``.

**Overlap tab** (``LesionNetworkMappingLib.cohort`` through its command line, in the job runner, with Stop):

- each lesion's map is thresholded with the Result card's rule (re-resolved for the maps' N), with the chosen sign;
- the overlap count and percent and the sensitivity map (at least k % of the lesions, slider 50-100, default 90;
  over the cases when the study has case/control groups), labelled as the classic map: "see Nulls for a
  null-referenced version" (it is never the only output once the Nulls card exists);
- optionally the legacy two-group maps (Welch t of the mean Fz maps, Liebermeister p of the binarized maps; "legacy
  two-group, no covariates", descriptive) and their conjunction with the sensitivity map.

The group maps are written next to maps.tsv (``group_conn-<label>_desc-<desc>_map.nii.gz``, peaks.tsv, cohort.json)
and shown one at a time in one volume with its own colour table (``display.applyGroupMapDisplay``); peaks jump the
views. "Browse lesion maps" shows the cohort's per-lesion maps one at a time (``StudioLib.mappage``, Alt+←/→ in
Studio). Works in Classic and without a main window.
"""

import json
import logging
import os

import qt
import slicer
from slicer.i18n import tr as _

from . import ORDER_COHORT, discoverCohortTabs, widgets

#: Node attribute of the volume that shows the group maps (one volume, reused)
GROUP_VOLUME_ATTRIBUTE = "LNM.GroupMapVolume"
SIGNS = ("both", "pos", "neg")
SIGN_LABELS = {"both": "±", "pos": "+", "neg": "−"}  # noqa: RUF001
#: Order of the Overlap tab among the cohort_*.py tabs
OVERLAP_ORDER = 10
#: Top and bottom margin of a cohort tab page (px); the pages have no side margins, see CohortCard
TAB_PAGE_MARGIN = 8


def _mapWidget():
    return getattr(slicer.modules, "LesionNetworkMappingWidget", None)


def _updateProgress(progressRow, force=False):
    """The progress row of a job, throttled and repainted asynchronously (``LesionNetworkMapping.updateProgressQuietly``)."""
    try:
        from LesionNetworkMapping import updateProgressQuietly
    except ImportError:
        progressRow.update()
        return
    updateProgressQuietly(progressRow, force)


def _readJson(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


class OverlapTab:
    """The Overlap tab: overlap, sensitivity, the legacy two-group maps and conjunction (see the module docstring)."""

    DESCRIPTIONS = {
        "overlapCount": _("Overlap: number of lesions"),
        "overlapPct": _("Overlap: percent of lesions"),
        "legacyWelchT": _("Legacy Welch t (no covariates)"),
        "legacyLiebermeisterP": _("Legacy Liebermeister p (no covariates)"),
        "conj": _("Conjunction: sensitivity ∩ specificity"),
    }

    def __init__(self, card):
        self.card = card
        self.ctx = card.ctx
        self.job = None
        self.outputs = {}
        self.peaks = []
        self.widget = qt.QWidget()
        self.widget.setObjectName("LNMOverlapTab")
        layout = qt.QVBoxLayout(self.widget)
        self.ruleLabel = self._label("", "LNMOverlapRule", "secondaryText")
        layout.addWidget(self.ruleLabel)
        row = qt.QHBoxLayout()
        row.addWidget(qt.QLabel(_("Sign")))
        self.signFrame, self.signGroup = widgets.segmented([SIGN_LABELS[sign] for sign in SIGNS])
        self.signFrame.setObjectName("LNMOverlapSign")
        self.signFrame.setToolTip(_("Sign · ± both (sign-specific sensitivity), + positive, − negative"))  # noqa: RUF001
        row.addWidget(self.signFrame)
        row.addStretch(1)
        layout.addLayout(row)
        form = qt.QFormLayout()
        import ctk

        self.sensitivitySlider = ctk.ctkSliderWidget()
        self.sensitivitySlider.setObjectName("LNMOverlapSensitivity")
        self.sensitivitySlider.minimum = 50
        self.sensitivitySlider.maximum = 100
        self.sensitivitySlider.singleStep = 5
        self.sensitivitySlider.pageStep = 5
        self.sensitivitySlider.decimals = 0
        self.sensitivitySlider.suffix = " %"
        self.sensitivitySlider.value = 90
        self.sensitivitySlider.setToolTip(_("Sensitivity: the voxels where at least this percent of the lesions' maps pass the rule"))
        form.addRow(_("Sensitivity"), self.sensitivitySlider)
        layout.addLayout(form)
        # Short labels: the inspector stays 340 px wide (the details are in the tooltips)
        self.legacyCheckBox = qt.QCheckBox(_("Legacy two-group maps"))
        self.legacyCheckBox.setObjectName("LNMOverlapLegacy")
        self.legacyCheckBox.setToolTip(_("Welch t and Liebermeister maps of the cases against the controls: descriptive, without covariates "
                                         "or permutations. The specificity analysis is the GLM of the Contrast tab."))
        self.legacyCheckBox.connect("toggled(bool)", lambda checked: self.refresh())
        layout.addWidget(self.legacyCheckBox)
        self.conjunctionCheckBox = qt.QCheckBox(_("Conjunction with legacy specificity"))
        self.conjunctionCheckBox.setObjectName("LNMOverlapConjunction")
        self.conjunctionCheckBox.setToolTip(_("Conjunction with the legacy specificity map (p ≤ 0.05)"))
        self.conjunctionCheckBox.setChecked(True)
        layout.addWidget(self.conjunctionCheckBox)
        buttons = qt.QHBoxLayout()
        self.computeButton = qt.QPushButton(_("Compute group maps"))
        self.computeButton.setObjectName("LNMOverlapComputeButton")
        self.computeButton.setProperty("slicerRole", "secondary")
        self.computeButton.connect("clicked()", self.compute)
        buttons.addWidget(self.computeButton)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.reasonLabel = self._label("", "LNMOverlapReason", "secondaryText")
        layout.addWidget(self.reasonLabel)
        self.progress = widgets.progressRow(objectNamePrefix="LNMOverlap")
        self.progress.widget.hide()
        layout.addWidget(self.progress.widget)
        self.mapsTable = widgets.tableView([_("Group map"), _("File")])
        self.mapsTable.setObjectName("LNMOverlapMapsTable")
        self.mapsTable.setToolTip(_("Click a group map to show it"))
        self.mapsTable.setMaximumHeight(170)
        self.mapsTable.connect("cellClicked(int,int)", self.onMapClicked)
        self.mapsTable.hide()
        layout.addWidget(self.mapsTable)
        # The texts of cohort.CLASSIC_NOTE and cohort.LEGACY_NOTE (written into cohort.json), translatable here
        self.classicNote = self._label(_("Classic overlap map (hub-inflated); see Nulls for a null-referenced version."), "LNMOverlapClassicNote", "caption")
        layout.addWidget(self.classicNote)
        self.legacyNote = self._label(_("Legacy two-group maps without covariates or permutation inference (descriptive); the specificity analysis is the GLM (Contrast tab)."),
                                      "LNMOverlapLegacyNote", "caption")
        self.legacyNote.hide()
        layout.addWidget(self.legacyNote)
        self.peaksTable = widgets.tableView([_("x"), _("y"), _("z"), _("% lesions")])
        self.peaksTable.setObjectName("LNMOverlapPeaksTable")
        self.peaksTable.setToolTip(_("Peaks of the overlap map, at least 10 mm apart (RAS mm). Click a peak to go there."))
        self.peaksTable.setMaximumHeight(150)
        self.peaksTable.connect("cellClicked(int,int)", self.onPeakClicked)
        self.peaksTable.hide()
        layout.addWidget(self.peaksTable)
        folderRow = qt.QHBoxLayout()
        self.showFolderButton = qt.QPushButton(_("Show folder"))
        self.showFolderButton.setObjectName("LNMOverlapShowFolderButton")
        self.showFolderButton.setProperty("slicerRole", "ghost")
        self.showFolderButton.connect("clicked()", self.card.showFolder)
        self.showFolderButton.hide()
        folderRow.addWidget(self.showFolderButton)
        folderRow.addStretch(1)
        layout.addLayout(folderRow)

    @staticmethod
    def _label(text, objectName, role=None):
        label = qt.QLabel(text)
        label.setObjectName(objectName)
        label.setWordWrap(True)
        if role:
            label.setProperty("slicerRole", role)
        label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        return label

    def sign(self):
        index = self.signGroup.checkedId()
        return SIGNS[index] if 0 <= index < len(SIGNS) else "both"

    def setSign(self, sign):
        self.signGroup.button(SIGNS.index(sign)).setChecked(True)

    def _rule(self):
        from LesionNetworkMappingLib import thresholds

        widget = _mapWidget()
        rule = self.ctx.rule or (widget.currentRule() if widget is not None else None)
        return thresholds.normalize_rule(rule)

    def _groups(self):
        """{"case": n, "control": n} of the cohort's mapped lesions (from the Seed card), or {}."""
        widget = _mapWidget()
        info = getattr(widget, "cohortInfo", None) if widget is not None else None
        return (info or {}).get("groups") or {}

    def refresh(self):
        from LesionNetworkMappingLib import cohort, thresholds

        mapsTsv = self.card.currentMapsTsv()
        meta = self.card.cohortMapsRecord() or {}
        rule = self._rule()
        n, bundle = meta.get("n"), self.ctx.bundle
        statistic = cohort.rule_statistic(rule)
        if n and bundle is not None:
            try:
                resolved = thresholds.resolve(rule, int(n), bundle.n_voxels, gsr=(meta.get("bundle") or {}).get("gsr"))
                line = thresholds.describe(resolved)
                self.ruleLabel.setText(_("Each lesion's {statistic} map: {rule} {line}").format(statistic=statistic, rule=thresholds.rule_label(rule), line=line))
            except Exception as exc:
                self.ruleLabel.setText(str(exc))
        else:
            self.ruleLabel.setText(_("Each lesion's map is thresholded with the Result card's rule: {rule}.").format(rule=thresholds.rule_label(rule)))
        groups = self._groups()
        twoGroups = groups.get("case", 0) >= 2 and groups.get("control", 0) >= 2
        self.legacyCheckBox.setEnabled(twoGroups)
        if not twoGroups:
            self.legacyCheckBox.setToolTip(_("Needs at least 2 cases and 2 controls in the study's group column"))
        self.conjunctionCheckBox.setEnabled(twoGroups and self.legacyCheckBox.checked)
        running = self.job is not None and not self.job.isFinished()
        enabled, reason = self.computeState(mapsTsv, running)
        self.computeButton.setEnabled(enabled)
        self.reasonLabel.setText(reason)
        self.reasonLabel.setVisible(bool(reason))
        self._showOutputs()

    def computeState(self, mapsTsv=None, running=None):
        """(enabled, reason) of Compute group maps: there are lesion maps, no group maps job runs, and nothing
        blocks Compute (a pipeline on the same connectome, a plug-in's check: the Compute card's reasons).
        """
        if running is None:
            running = self.job is not None and not self.job.isFinished()
        if mapsTsv is None:
            return False, _("Map the lesions first (Compute: Map all lesions).")
        if running:
            return False, _("Computing the group maps…")
        if not self.ctx.computeEnabled():
            return False, " ".join(self.ctx.computeDisabledReasons()) or _("Not ready")
        return True, ""

    def arguments(self, mapsTsv):
        """Arguments of ``LesionNetworkMappingLib.cohort`` for the current settings."""
        from LesionNetworkMappingLib import thresholds

        widget = _mapWidget()
        study = self.ctx.study
        args = ["--maps", mapsTsv, "--out", os.path.dirname(mapsTsv), "--json", "--rule", thresholds.rule_to_json(self._rule()), "--sign", self.sign(),
                "--sensitivity", str(round(float(self.sensitivitySlider.value)))]
        participants = os.path.join(study.path, "participants.tsv") if study is not None and getattr(study, "path", None) else None
        if participants and os.path.isfile(participants):
            args += ["--participants", participants, "--run-root", study.path]
        legacy = self.legacyCheckBox.enabled and self.legacyCheckBox.checked
        args += ["--legacy", "welch,liebermeister" if legacy else "none"]
        if legacy and not (self.conjunctionCheckBox.enabled and self.conjunctionCheckBox.checked):
            args.append("--no-conjunction")
        bundlePath = widget.connectomePath() if widget is not None else ""
        if bundlePath:
            args += ["--bundle", bundlePath]
        return args

    def compute(self):
        """Run the group maps job (cohort.py) on the current maps.tsv; return the job, or None when there are no maps yet."""
        from . import jobrunner

        mapsTsv = self.card.currentMapsTsv()
        if not self.computeState(mapsTsv)[0]:
            self.refresh()
            return None
        spec = jobrunner.JobSpec("LesionNetworkMappingLib.cohort", self.arguments(mapsTsv), title=_("Group maps"), heavy=True, kind="cohort", name="cohort",
                                 runRoot=os.path.dirname(mapsTsv), onEvent=self._onEvent, onDone=self._onDone)
        self.job = self.ctx.submit(spec)
        self.progress.setJob(self.job)
        self.progress.widget.show()
        self.refresh()
        return self.job

    def _onEvent(self, event):
        _updateProgress(self.progress, force=event.get("event") != "progress")

    def _onDone(self, job):
        _updateProgress(self.progress, force=True)
        if job.state == "failed":
            self.reasonLabel.setText(_("The group maps failed: {reason}").format(reason=job.failureReason))
            self.reasonLabel.show()
        self.refresh()
        if job.succeeded() and self.outputs:
            first = "overlapPct" if "overlapPct" in self.outputs else next(iter(self.outputs))
            self.card.showGroupMap(first)

    def _showOutputs(self):
        record = self.card.groupRecord()
        folder = self.card.folder()
        self.outputs = {}
        if record is not None and folder:
            for desc, name in (record.get("outputs") or {}).items():
                path = os.path.join(folder, name)
                if desc != "peaks" and os.path.isfile(path):
                    self.outputs[desc] = path
        table = self.mapsTable
        table.setRowCount(len(self.outputs))
        for row, (desc, path) in enumerate(self.outputs.items()):
            text = self.DESCRIPTIONS.get(desc) or (_("Sensitivity ≥ {k} % ({sign})").format(k=desc[4:], sign=record.get("sign")) if desc.startswith("sens") else desc)
            table.setItem(row, 0, qt.QTableWidgetItem(text))
            item = qt.QTableWidgetItem(os.path.basename(path))
            item.setToolTip(path)
            table.setItem(row, 1, item)
        table.setVisible(bool(self.outputs))
        self.legacyNote.setVisible(any(desc.startswith("legacy") for desc in self.outputs))
        self.showFolderButton.setVisible(bool(self.outputs))
        self.peaks = self._readPeaks(os.path.join(folder, "peaks.tsv")) if record is not None and folder else []
        peaksTable = self.peaksTable
        peaksTable.setRowCount(len(self.peaks))
        for row, peak in enumerate(self.peaks):
            for column, value in enumerate(peak):
                peaksTable.setItem(row, column, qt.QTableWidgetItem(f"{value:.1f}"))
        peaksTable.setVisible(bool(self.peaks))

    @staticmethod
    def _readPeaks(path):
        peaks = []
        try:
            with open(path, encoding="utf-8") as f:
                next(f)
                for line in f:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) >= 4:
                        peaks.append(tuple(float(value) for value in parts[:4]))
        except (OSError, StopIteration, ValueError):
            return []
        return peaks[:5]

    def onMapClicked(self, row, column):
        descs = list(self.outputs)
        if 0 <= row < len(descs):
            self.card.showGroupMap(descs[row])

    def onPeakClicked(self, row, column):
        if 0 <= row < len(self.peaks):
            x, y, z = self.peaks[row][:3]
            slicer.modules.markups.logic().JumpSlicesToLocation(x, y, z, True)

    def cleanup(self):
        if self.job is not None and not self.job.isFinished():
            self.job.spec.onDone = None
            self.job.spec.onEvent = None


class CohortCard:
    """Controller of the Cohort card (``widget`` is a collapsible card with the tabs)."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.groupVolumeId = None
        self.widget = widgets.card(_("Cohort"), objectName="LNMCohortCard")
        layout = qt.QVBoxLayout(self.widget)
        self.statusLabel = qt.QLabel("")
        self.statusLabel.setObjectName("LNMCohortMapsStatus")
        self.statusLabel.setWordWrap(True)
        self.statusLabel.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        layout.addWidget(self.statusLabel)
        row = qt.QHBoxLayout()
        self.mapButton = qt.QPushButton(_("Map all lesions"))
        self.mapButton.setObjectName("LNMCohortMapButton")
        self.mapButton.setProperty("slicerRole", "secondary")
        self.mapButton.connect("clicked()", self.mapLesions)
        row.addWidget(self.mapButton)
        self.browseButton = qt.QPushButton(_("Browse lesion maps"))
        self.browseButton.setObjectName("LNMCohortBrowseButton")
        self.browseButton.setProperty("slicerRole", "ghost")
        self.browseButton.setToolTip(_("Show the lesions' maps one at a time with the lesion outline (Alt+← / Alt+→ in Studio)"))
        self.browseButton.connect("clicked()", self.browseMaps)
        row.addWidget(self.browseButton)
        row.addStretch(1)
        layout.addLayout(row)
        self.tabs = qt.QTabWidget()
        self.tabs.setObjectName("LNMCohortTabs")
        self.tabs.setDocumentMode(True)
        layout.addWidget(self.tabs)
        self.overlap = OverlapTab(self)
        self.tabControllers = [("overlap", self.overlap)]
        entries = [(OVERLAP_ORDER, "overlap", _("Overlap"), self.overlap.widget)]
        try:
            specs = discoverCohortTabs()
        except Exception:
            logging.exception("Lesion network mapping: the cohort tabs could not be discovered")
            specs = []
        for spec in specs:
            if spec["id"] == "overlap":
                continue
            try:
                controller = spec["factory"](ctx)
            except Exception:
                logging.exception(f"Lesion network mapping: the cohort tab {spec['id']} could not be created and is skipped")
                continue
            tabWidget = getattr(controller, "widget", controller)
            if tabWidget is None or not hasattr(tabWidget, "setVisible"):
                logging.error(f"Lesion network mapping: the cohort tab {spec['id']} has no widget; it is skipped")
                continue
            self.tabControllers.append((spec["id"], controller))
            entries.append((spec["order"], spec["id"], str(spec.get("title", spec["id"])), tabWidget))
        entries.sort(key=lambda entry: (entry[0], entry[1]))
        for _order, tabId, title, tabWidget in entries:
            # The tab pages have no side margins: the card has its own, and 22 more px would make the inspector wider than
            # its stored width (the segmented controls of the tabs are about 290 px wide)
            pageLayout = tabWidget.layout() if hasattr(tabWidget, "layout") else None
            if pageLayout is not None and hasattr(pageLayout, "setContentsMargins"):
                pageLayout.setContentsMargins(0, TAB_PAGE_MARGIN, 0, TAB_PAGE_MARGIN)
            index = self.tabs.addTab(tabWidget, title)
            self.tabs.setTabToolTip(index, title)
        for name in ("studyChanged", "bundleChanged", "ruleChanged", "computeEnabledChanged"):
            getattr(ctx, name).append(self.refresh)
        self.refresh()

    #
    # State
    #

    def currentMapsTsv(self):
        """maps.tsv of the cohort's finished cohort maps (study, connectome and subjects of the Seed and Compute cards), or None."""
        path = getattr(self.ctx, "cohortMaps", None)
        if path and os.path.isfile(path):
            record = _readJson(os.path.join(os.path.dirname(path), "cohortmaps.json")) or {}
            if record.get("status") == "done":
                return path
        return None

    def cohortMapsRecord(self):
        path = self.currentMapsTsv()
        return _readJson(os.path.join(os.path.dirname(path), "cohortmaps.json")) if path else None

    def folder(self):
        path = self.currentMapsTsv()
        return os.path.dirname(path) if path else None

    def groupRecord(self):
        """cohort.json of the group maps next to the current maps.tsv, or None."""
        folder = self.folder()
        return _readJson(os.path.join(folder, "cohort.json")) if folder else None

    def tab(self, tabId):
        """The controller of a tab ("overlap", "contrast", "symptom"...), or None."""
        return next((controller for key, controller in self.tabControllers if key == tabId), None)

    def refresh(self):
        widget = _mapWidget()
        info = getattr(widget, "cohortInfo", None) if widget is not None else None
        mapsTsv = self.currentMapsTsv()
        record = self.cohortMapsRecord() or {}
        if mapsTsv:
            lesions = record.get("lesions") or {}
            self.statusLabel.setText(_("Lesion maps: {n} lesions · conn-{label} · N={subjects} ({hits} from the cache)").format(
                n=lesions.get("mapped", "?"), label=(record.get("bundle") or {}).get("label", "?"), subjects=record.get("n", "?"), hits=lesions.get("cache_hits", 0)))
        elif info and info.get("n"):
            self.statusLabel.setText((_("The lesion of the study is not mapped with this connectome yet.") if info["n"] == 1 else _("The {n} lesions of the study are not mapped with this connectome yet.")).format(n=info["n"]))
        else:
            self.statusLabel.setText(_("Open a study with traced lesions in the Seed card."))
        running = widget is not None and widget.cohortJob is not None and not widget.cohortJob.isFinished()
        self.mapButton.setVisible(not mapsTsv)
        self.mapButton.setEnabled(widget is not None and widget.cohortComputeState()[0] and not running)
        self.browseButton.setEnabled(bool(mapsTsv))
        self.overlap.refresh()

    #
    # Card protocol (called by the Map widget)
    #

    def onCohortChanged(self):
        self.refresh()

    def onCohortMapsChanged(self, path=None):
        self.refresh()

    def onCohortJobStarted(self, job):
        self.refresh()

    def onStudyChanged(self):
        self.refresh()

    def cleanup(self):
        for name in ("studyChanged", "bundleChanged", "ruleChanged", "computeEnabledChanged"):
            callbacks = getattr(self.ctx, name, [])
            if self.refresh in callbacks:
                callbacks.remove(self.refresh)
        for _key, controller in self.tabControllers:
            callback = getattr(controller, "cleanup", None)
            if callable(callback):
                try:
                    callback()
                except Exception:
                    logging.exception("Lesion network mapping: a cohort tab cleanup failed")

    #
    # Actions
    #

    def mapLesions(self):
        """Map the cohort's lesions (the Compute card's cohort maps job); return the job or None."""
        widget = _mapWidget()
        return widget.computeCohort() if widget is not None else None

    def groupVolume(self):
        """The volume that shows the group maps (one, reused), or None."""
        node = slicer.mrmlScene.GetNodeByID(self.groupVolumeId) if self.groupVolumeId else None
        if node is not None and node.GetAttribute(GROUP_VOLUME_ATTRIBUTE) == "1":
            return node
        for candidate in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
            if candidate.GetAttribute(GROUP_VOLUME_ATTRIBUTE) == "1":
                return candidate
        return None

    def showGroupMap(self, desc):
        """Show one group map (overlap, sensitivity, legacy, conjunction) in the group map volume; return the volume."""
        from LesionNetworkMappingLib import display

        path = self.overlap.outputs.get(desc)
        widget = _mapWidget()
        if not path or widget is None:
            return None
        record = self.groupRecord() or {}
        name = f"Cohort {desc} ({record.get('n_maps', '?')} lesions)"
        attributes = {GROUP_VOLUME_ATTRIBUTE: "1", "Studio.Role": "statmap", display.GROUP_MAP_ATTRIBUTE: desc}
        volume = widget.logic.loadMap(path, name, self.groupVolume(), saveWithScene=True, attributes=attributes)
        self.groupVolumeId = volume.GetID()
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
        return volume

    def browseMaps(self, index=0):
        """Show the cohort's lesion maps one at a time (``mappage.browseCohort``); return the shown item or None."""
        mapsTsv = self.currentMapsTsv()
        widget = _mapWidget()
        if mapsTsv is None or widget is None:
            return None
        from LesionNetworkMappingLib import cohortmaps

        rows = cohortmaps.read_maps_tsv(mapsTsv)
        try:
            from StudioLib import mappage
        except ImportError:
            mappage = None
        if mappage is not None:
            return mappage.browseCohort(rows, index, widget.currentStatistic())
        column = {"T": "T", "Fz": "AvgFz", "r": "AvgR"}.get(widget.currentStatistic(), "T")
        row = rows[index] if 0 <= index < len(rows) else None
        if row is not None and row.get(column) and row.get(column) != "n/a":
            widget.showMapFile(row[column], label=row["participant_id"])
        return row

    def export(self):
        """Ctrl+E in Map with Seed = Cohort: the group maps (computed now when there are none), then their folder."""
        if not self.overlap.outputs:
            return self.overlap.compute()
        self.showFolder()
        shell = self.ctx.shell()
        if shell is not None:
            shell.toast(_("Group maps, peaks.tsv and cohort.json are in {path}").format(path=self.folder()), 3000)
        return None

    def showFolder(self):
        folder = self.folder()
        if folder and os.path.isdir(folder) and not slicer.app.testingEnabled():
            widgets.showInFileManager(folder)


CARD = {
    "id": "cohort",
    "title": "Cohort",
    "order": ORDER_COHORT,
    "modes": ("cohort",),
    "factory": CohortCard,
}

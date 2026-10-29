"""Compare card of the Map module (order 130; L-52, SPEC_LNM_ADDENDUM §3.6, §3.7, §4.4 and §7.1).

Checks whether a network repeats across conditions (van den Heuvel et al. 2026b) with honest statistics:

- **Maps to compare** (tick any): this result, the cohort's group maps, per-lesion maps, group maps of other
  connectomes or subsets of the same study (resampled onto this grid), the connectome's μC_r and PC1, maps built here
  from the study's raw inputs (a score map or a two-group contrast of the per-lesion maps) and the reference maps of
  the local library (``LesionNetworkMappingLib.refmaps``, with their licence shown on every row).
- **Compare** gives the heat-map cross-correlogram (Pearson r below the diagonal, partial r given μC_r and PC1 above
  it), the pairs table (r, Spearman rho, partial r, p) and the ten maps most similar to the first ticked map, with a
  note when a clinically unrelated condition exceeds |r| = 0.6.
- **Permutation p** is computed only for pairs of maps built from in-study raw inputs (scores or labels reshuffled);
  every other pair shows r without p and says why. There is no spin or BrainSMASH p.
- **Export** writes the CSV matrices, compare.json (no local paths) and the figure to ``<runs>/compare/<time>``;
  reference map files are never copied there (the van den Heuvel files are never redistributed).
- **Test for differences** opens the Cohort card's Contrast tab (label permutation GLM).

The library lives in ``<Studio/LNM/DataDirectory>/ReferenceMaps`` (only read here; fork-only, D-S1). Maps are added
only on a click: NeuroVault (public API, CC0), a URL, a file, or the van den Heuvel degree map at commit ca410e6 after
the live GSP data use terms were accepted (``Studio/LNM/AcceptedTermsText/<sha>``, as the Lead-DBS parity check).
Downloads run as light jobs. Works in Classic and without the Studio shell. Search: "Compare maps".
"""

import datetime
import glob
import logging
import math
import os
import re

import qt
import slicer
from slicer.i18n import tr as _

from . import ORDER_COMPARE, widgets
from .cohort_contrast import disclaimerCaption, label

#: Acceptance of a live terms text, per sha256 (SPEC 7.1 rule 6; the key manager_parity.py uses)
ACCEPTED_TERMS_TEXT_SETTING = "Studio/LNM/AcceptedTermsText"
JOB_MODULE = "LesionNetworkMappingLib.refmaps"
TERMS_MODULE = "LesionNetworkMappingLib.leaddbs"
ROW_HEIGHT = 22
TABLE_FONT_PX = 11
HEATMAP_MAX_PX = 380
DEFAULT_PERMUTATIONS = 1000
MAX_LESION_ROWS = 400
GROUPS = (
    ("result", _("This result")),
    ("group", _("Group maps")),
    ("instudy", _("Built from this study (permutation p available)")),
    ("other", _("Other connectomes or subsets")),
    ("connectome", _("Connectome axes")),
    ("reference", _("Reference library")),
    ("lesion", _("Per-lesion maps")),
)
PAIR_COLUMNS = (
    (_("Map A"), ""),
    (_("Map B"), ""),
    ("r", _("Spatial Pearson r over the voxels finite in both maps")),
    ("\u03c1", _("Spatial Spearman \u03c1")),
    (_("Partial r"), _("Pearson r after removing μC_r and PC1 (and the mean) from both maps (lnm-reference-v1)")),
    ("p", _("Permutation p of the partial r (μC_r and PC1 removed) when the connectome axes are available, else of the Pearson r, "
            "on a seeded sample of up to 50,000 voxels. The participants are permuted: one permutation reassigns the scores or labels "
            "of both maps together. Empty for external maps (see the tooltip).")),
)
INTRO = _("Compare maps to see whether a network repeats across conditions. Similar maps of unrelated conditions point to the "
          "connectome's own structure rather than a condition-specific network. Reference maps are downloaded only when you add them.")


def _mapWidget():
    return getattr(slicer.modules, "LesionNetworkMappingWidget", None)


def _number(value, digits=2):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{number:.{digits}f}".replace("-", "\u2212") if math.isfinite(number) else "—"


def _settingTrue(key):
    return str(qt.QSettings().value(key) or "").lower() in ("1", "true", "yes")


def dataDirectory():
    """``Studio/LNM/DataDirectory`` (only read), as the normalization card reads it."""
    try:
        from .normalize_controller import dataDirectory as read

        return read()
    except Exception:
        from LesionNetworkMappingLib import refmaps

        return refmaps.default_data_directory()


def registryRoot():
    from LesionNetworkMappingLib import refmaps

    return refmaps.registry_root(data_directory=dataDirectory())


def interFont():
    """A file copy of the application's Inter font (Qt resource) for Pillow figures, or None (Pillow's font is used)."""
    folder = os.path.join(slicer.app.cachePath, "LNM", "fonts")
    target = os.path.join(folder, "Inter-Regular.ttf")
    if os.path.isfile(target):
        return target
    try:
        os.makedirs(folder, exist_ok=True)
        for name in ("Inter-Regular.ttf", "Inter-SemiBold.ttf"):
            resource = qt.QFile(f":/Fonts/{name}")
            if not resource.exists():
                continue
            resource.copy(os.path.join(folder, name))
    except Exception:
        logging.debug("Lesion network mapping: the Inter font could not be copied for figures", exc_info=True)
    return target if os.path.isfile(target) else None


class MapSource:
    """One map the card can compare: ``key``, ``label``, ``group`` (see :data:`GROUPS`), ``path`` (a NIfTI, or None for
    maps computed here), ``info`` (metadata for compare.json: kind, licence, citation, condition, no paths) and
    ``spec`` (a ``refmaps.InStudyMap`` for maps built from the study's raw inputs).
    """

    def __init__(self, key, label, group, path=None, info=None, spec=None, tooltip="", vector=None, name=None):
        self.key = key
        self.label = label
        self.name = name or label  # the short name in the matrix, figure and CSV (the list label may add the licence)
        self.group = group
        self.path = path
        self.info = dict(info or {})
        self.spec = spec
        self.tooltip = tooltip
        self.vector = vector


class CompareCard:
    """Controller of the Compare card (``widget`` is a collapsible card)."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.sources = {}
        self.inStudy = []  # (kind, column, levels or None)
        self.comparison = None
        self.comparedSources = []
        self.tests = {}
        self.top = []
        self.lastExport = None
        self.lastRunId = None
        self.job = None
        self.notice = ""
        self._raw = None  # (key, matrix, participant ids)
        self._reference = None  # (bundle path, Reference or None)
        self._vectors = {}
        self._cleanedUp = False
        self.widget = widgets.card(_("Compare"), objectName="LNMCompareCard", collapsed=True)
        layout = qt.QVBoxLayout(self.widget)
        layout.setSpacing(6)
        layout.addWidget(label(INTRO, "LNMCompareIntro", "secondaryText"))

        self.mapList = qt.QTreeWidget()
        self.mapList.setObjectName("LNMCompareMapList")
        self.mapList.setHeaderHidden(True)
        self.mapList.setRootIsDecorated(True)
        self.mapList.setUniformRowHeights(True)
        self.mapList.setMinimumHeight(150)
        self.mapList.setMaximumHeight(260)
        self.mapList.setToolTip(_("Tick the maps to compare (the top-10 list is about the first ticked map in list order)"))
        self.mapList.connect("itemChanged(QTreeWidgetItem*,int)", self._onItemChanged)
        layout.addWidget(self.mapList)

        optionsRow = qt.QHBoxLayout()
        self.lesionCheck = qt.QCheckBox(_("List per-lesion maps"))
        self.lesionCheck.setObjectName("LNMCompareLesionCheck")
        self.lesionCheck.connect("toggled(bool)", lambda *args: self.refreshSources())
        # The check box has a row of its own: with the two buttons it is wider than the inspector
        optionsRow.addStretch(1)
        self.addMapButton = qt.QToolButton()
        self.addMapButton.setObjectName("LNMCompareAddMapButton")
        self.addMapButton.setText(_("Build from study"))
        self.addMapButton.setToolTip(_("A score map or a two-group contrast built here from the per-lesion maps: pairs of such maps get a permutation p"))
        self.addMapButton.setPopupMode(qt.QToolButton.InstantPopup)
        self.addMapMenu = qt.QMenu(self.addMapButton)
        self.addMapMenu.connect("aboutToShow()", self._fillStudyMenu)
        self.addMapButton.setMenu(self.addMapMenu)
        optionsRow.addWidget(self.addMapButton)
        self.libraryButton = qt.QToolButton()
        self.libraryButton.setObjectName("LNMCompareLibraryButton")
        self.libraryButton.setText(_("Reference library"))
        self.libraryButton.setPopupMode(qt.QToolButton.InstantPopup)
        menu = qt.QMenu(self.libraryButton)
        for text, objectName, callback in (
            (_("Add NeuroVault image or collection…"), "LNMCompareAddNeuroVault", lambda: self.addReference("neurovault")),
            (_("Add from URL…"), "LNMCompareAddUrl", lambda: self.addReference("url")),
            (_("Add file…"), "LNMCompareAddFile", lambda: self.addReference("file")),
            (_("Add van den Heuvel degree map…"), "LNMCompareAddVdh", lambda: self.addReference("vdh")),
            (None, None, None),
            (_("Remove selected reference"), "LNMCompareRemoveReference", self.removeSelectedReference),
            (_("Open library folder"), "LNMCompareOpenLibrary", self.openLibraryFolder),
        ):
            if text is None:
                menu.addSeparator()
                continue
            action = menu.addAction(text)
            action.setObjectName(objectName)
            action.connect("triggered()", callback)
        self.libraryButton.setMenu(menu)
        self.libraryMenu = menu
        optionsRow.addWidget(self.libraryButton)
        layout.addWidget(self.lesionCheck)
        layout.addLayout(optionsRow)

        form = qt.QFormLayout()
        form.setFieldGrowthPolicy(qt.QFormLayout.AllNonFixedFieldsGrow)
        self.conditionEdit = qt.QLineEdit()
        self.conditionEdit.setObjectName("LNMCompareConditionEdit")
        self.conditionEdit.setPlaceholderText(_("e.g. depression"))
        self.conditionEdit.setToolTip(_("The condition of this study: reference maps of another condition above |r| = 0.6 get a cross-condition note"))
        form.addRow(_("Condition:"), self.conditionEdit)
        self.permSpin = qt.QSpinBox()
        self.permSpin.setObjectName("LNMComparePermSpin")
        self.permSpin.setRange(99, 10000)
        self.permSpin.setSingleStep(100)
        self.permSpin.setValue(DEFAULT_PERMUTATIONS)
        self.permSpin.setToolTip(_("Permutations of the in-study pairs (the smallest p is 1 / (permutations + 1))"))
        form.addRow(_("Permutations:"), self.permSpin)
        layout.addLayout(form)

        buttons = qt.QHBoxLayout()
        self.compareButton = qt.QPushButton(_("Compare"))
        self.compareButton.setObjectName("LNMCompareRunButton")
        self.compareButton.setProperty("slicerRole", "secondary")
        self.compareButton.connect("clicked()", self.compare)
        buttons.addWidget(self.compareButton)
        self.permButton = qt.QPushButton(_("Permutation p"))
        self.permButton.setObjectName("LNMComparePermButton")
        self.permButton.setProperty("slicerRole", "ghost")
        self.permButton.setToolTip(_("Reshuffle the scores or labels of the in-study maps: p for pairs of maps built from this study only"))
        self.permButton.connect("clicked()", self.permute)
        buttons.addWidget(self.permButton)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.stateLabel = label("", "LNMCompareState", "secondaryText")
        layout.addWidget(self.stateLabel)
        self.noticeLabel = label("", "LNMCompareNotice")
        self.noticeLabel.hide()
        layout.addWidget(self.noticeLabel)

        # Results
        self.results = qt.QWidget()
        self.results.setObjectName("LNMCompareResults")
        results = qt.QVBoxLayout(self.results)
        results.setContentsMargins(0, 4, 0, 0)
        results.setSpacing(6)
        self.heatmap = qt.QLabel()
        self.heatmap.setObjectName("LNMCompareHeatmap")
        self.heatmap.setAlignment(qt.Qt.AlignLeft | qt.Qt.AlignTop)
        self.heatmap.setToolTip(_("Pearson r below the diagonal, partial r given μC_r and PC1 above it; the numbers are the maps' order"))
        results.addWidget(self.heatmap)
        self.legendLabel = label("", "LNMCompareLegend", "secondaryText")
        results.addWidget(self.legendLabel)
        self.pairsTable = widgets.tableView([title for title, _tip in PAIR_COLUMNS])
        self.pairsTable.setObjectName("LNMComparePairsTable")
        for column, (_title, tip) in enumerate(PAIR_COLUMNS):
            item = self.pairsTable.horizontalHeaderItem(column)
            if item is not None and tip:
                item.setToolTip(tip)
        self.pairsTable.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        header = self.pairsTable.horizontalHeader()
        for column in range(2, len(PAIR_COLUMNS)):
            header.setSectionResizeMode(column, qt.QHeaderView.ResizeToContents)
        widgets._font(self.pairsTable, TABLE_FONT_PX)
        results.addWidget(self.pairsTable)
        self.topTitle = self._title(_("Most similar to the first map"))
        results.addWidget(self.topTitle)
        self.topLabel = label("", "LNMCompareTopList")
        self.topLabel.setTextFormat(qt.Qt.RichText)
        results.addWidget(self.topLabel)
        self.noteLabel = label(_("Note: ") + self._crossNote(), "LNMCompareCrossNote", "secondaryText")
        results.addWidget(self.noteLabel)
        self.licenceLabel = label("", "LNMCompareLicences", "secondaryText")
        self.licenceLabel.setTextFormat(qt.Qt.RichText)
        self.licenceLabel.setOpenExternalLinks(True)
        self.licenceLabel.setTextInteractionFlags(qt.Qt.TextBrowserInteraction)
        results.addWidget(self.licenceLabel)
        actions = qt.QHBoxLayout()
        self.exportButton = qt.QPushButton(_("Export CSV and figure"))
        self.exportButton.setObjectName("LNMCompareExportButton")
        self.exportButton.setProperty("slicerRole", "ghost")
        self.exportButton.connect("clicked()", self.export)
        actions.addWidget(self.exportButton)
        actions.addStretch(1)
        results.addLayout(actions)
        # Two rows: the three buttons side by side are wider than the inspector
        actions = qt.QHBoxLayout()
        self.folderButton = qt.QPushButton(_("Show folder"))
        self.folderButton.setObjectName("LNMCompareFolderButton")
        self.folderButton.setProperty("slicerRole", "ghost")
        self.folderButton.connect("clicked()", self.showFolder)
        self.folderButton.setEnabled(False)
        actions.addWidget(self.folderButton)
        self.differencesButton = qt.QPushButton(_("Test for differences"))
        self.differencesButton.setObjectName("LNMCompareDifferencesButton")
        self.differencesButton.setProperty("slicerRole", "ghost")
        self.differencesButton.setToolTip(_("Group differences are tested in the Cohort card's Contrast tab (label permutation GLM)"))
        self.differencesButton.connect("clicked()", self.testDifferences)
        actions.addWidget(self.differencesButton)
        actions.addStretch(1)
        results.addLayout(actions)
        self.results.hide()
        layout.addWidget(self.results)
        layout.addWidget(disclaimerCaption("LNMCompareDisclaimer"))

        try:
            self.widget.connect("contentsCollapsed(bool)", self._onCollapsed)
        except Exception:
            logging.debug("Lesion network mapping: the Compare card cannot follow its expansion", exc_info=True)
        for name in ("bundleChanged", "studyChanged"):
            getattr(ctx, name).append(self._onContextChanged)
        self.refresh()

    @staticmethod
    def _title(text):
        title = qt.QLabel(text)
        title.setProperty("slicerRole", "secondaryText")
        widgets._font(title, 12, bold=True)
        return title

    @staticmethod
    def _crossNote():
        from LesionNetworkMappingLib import refmaps

        return refmaps.CROSS_CONDITION_NOTE

    #
    # Card protocol
    #

    def onRunDone(self, job=None):
        self.refresh()

    def onResultChanged(self):
        self.refresh()

    def onCohortMapsChanged(self, path=None):
        self._raw = None
        self.refresh()

    def onStudyChanged(self):
        self._onContextChanged()

    def _onContextChanged(self, *args):
        self._raw = None
        self._vectors = {}
        self.inStudy = []
        self.comparison = None
        self.tests = {}
        self.results.hide()
        self.refresh()

    def cleanup(self):
        self._cleanedUp = True
        for name in ("bundleChanged", "studyChanged"):
            callbacks = getattr(self.ctx, name, [])
            if self._onContextChanged in callbacks:
                callbacks.remove(self._onContextChanged)
        if self.job is not None and hasattr(self.job, "isFinished") and not self.job.isFinished():
            self.job.spec.onDone = None
        self.job = None
        self._raw = None
        self._vectors = {}

    def _onCollapsed(self, collapsed):
        if not collapsed:
            self.refresh()

    def expanded(self):
        try:
            return not bool(self.widget.collapsed)
        except (ValueError, RuntimeError):
            return False

    #
    # Sources
    #

    def reference(self):
        """The connectome's reference maps (μC_r, PC1...) or None (never built here, D-A20)."""
        bundle = self.ctx.bundle
        if bundle is None:
            return None
        if self._reference is None or self._reference[0] != bundle.path:
            try:
                from LesionNetworkMappingLib import reference

                loaded = reference.load_reference(bundle, verify=False)
            except Exception:
                logging.exception("Lesion network mapping: the reference maps could not be loaded")
                loaded = None
            self._reference = (bundle.path, loaded)
        return self._reference[1]

    def mapsTsv(self):
        card = self.ctx.card("cohort")
        if card is not None and hasattr(card, "currentMapsTsv"):
            try:
                return card.currentMapsTsv()
            except Exception:
                return None
        return None

    def connectomeDir(self):
        path = self.mapsTsv()
        return os.path.dirname(os.path.dirname(path)) if path else None

    def outputRoot(self):
        try:
            return self.ctx.runRoot()
        except Exception:
            from . import jobrunner

            return jobrunner.defaultRunRoot()

    def _resultPath(self):
        run = self.ctx.currentRun
        paths = [path for path in (run.outputs if run is not None else []) if str(path).endswith((".nii", ".nii.gz"))]
        for desc in ("_desc-T", "_desc-AvgRFz", "_desc-AvgR"):
            for path in paths:
                if desc in os.path.basename(path) and os.path.isfile(path):
                    return path
        return None

    @staticmethod
    def _groupLabel(path):
        name = os.path.basename(path)
        match = re.search(r"_desc-([A-Za-z0-9]+)", name)
        return match.group(1) if match else re.sub(r"\.nii(\.gz)?$", "", name)

    def discoverSources(self):
        """The available maps (cheap: file lists and the registry, nothing is loaded)."""
        from LesionNetworkMappingLib import refmaps

        sources = []
        result = self._resultPath()
        if result:
            sources.append(MapSource("result", _("This result ({desc})").format(desc=self._groupLabel(result)), "result", result,
                                     {"kind": "result", "file": os.path.basename(result)}))
        mapsTsv = self.mapsTsv()
        folder = os.path.dirname(mapsTsv) if mapsTsv else None
        if folder:
            for path in sorted(glob.glob(os.path.join(folder, "group_*_map.nii*"))):
                desc = self._groupLabel(path)
                sources.append(MapSource(f"group:{os.path.basename(path)}", _("Group: {desc}").format(desc=desc), "group", path,
                                         {"kind": "group", "desc": desc, "file": os.path.basename(path)}))
        for kind, column, levels in self.inStudy:
            key = f"instudy:{kind}:{column}:{levels}"
            text = _("Score map: {column}").format(column=column) if kind == "score" else _("Contrast: {a} \u2212 {b} ({column})").format(a=levels[0], b=levels[1], column=column)
            sources.append(MapSource(key, text, "instudy", None, {"kind": f"in-study {kind}", "column": column, "levels": list(levels) if levels else None},
                                     tooltip=_("Built from the per-lesion maps and participants.tsv; reshuffling its {what} gives the permutation p").format(
                                         what=_("scores") if kind == "score" else _("labels"))))
        connDir = self.connectomeDir()
        studyRoot = os.path.dirname(connDir) if connDir else None
        if studyRoot and os.path.isdir(studyRoot):
            for path in sorted(glob.glob(os.path.join(studyRoot, "conn-*", "*", "group_*_map.nii*"))):
                if connDir and os.path.abspath(path).startswith(os.path.abspath(connDir) + os.sep):
                    continue
                connLabel = os.path.basename(os.path.dirname(os.path.dirname(path)))
                desc = self._groupLabel(path)
                sources.append(MapSource(f"other:{connLabel}:{os.path.basename(path)}", f"{connLabel}: {desc}", "other", path,
                                         {"kind": "group (other connectome or subset)", "connectome": connLabel, "desc": desc, "file": os.path.basename(path)},
                                         tooltip=_("Resampled onto this connectome's grid (fraction mode)")))
        reference = self.reference()
        if reference is not None:
            for key, text in (("mu_r", _("μC_r (mean r profile)")), ("pc1", _("PC1 of the connectome")), ("hub", _("Hubness"))):
                if key in reference.maps:
                    sources.append(MapSource(f"connectome:{key}", text, "connectome", None, {"kind": "connectome", "map": key, "reference_sha256": reference.sha256},
                                             vector=reference.maps[key]))
        try:
            entries = refmaps.entries(registryRoot())
        except Exception as exc:
            entries = []
            self.notice = _("The reference library cannot be read: {error}").format(error=exc)
        for entry in entries:
            licence = entry.get("licence") or "unknown"
            badge = {"green": "●", "amber": "◐", "grey": "○"}.get(entry.get("badge"), "○")
            condition = entry.get("condition")
            text = f"{entry.get('title') or entry.get('id')}" + (f" · {condition}" if condition else "") + f"  {badge} {licence}"
            tip = "\n".join(part for part in (entry.get("licence_note"), entry.get("citation"), _("Never redistributed") if not entry.get("redistribute") else "") if part)
            sources.append(MapSource(f"reference:{entry['id']}", text, "reference", None, name=str(entry.get("title") or entry.get("id")),
                                     info=
                                     {"kind": entry.get("kind") or "reference", "id": entry.get("id"), "title": entry.get("title"), "source": entry.get("source"),
                                      "licence": licence, "licence_note": entry.get("licence_note"), "citation": entry.get("citation"),
                                      "condition": condition, "sha256": entry.get("sha256"), "redistribute": bool(entry.get("redistribute"))}, tooltip=tip))
        if self.lesionCheck.checked and mapsTsv:
            from LesionNetworkMappingLib import cohortmaps

            try:
                rows = cohortmaps.read_maps_tsv(mapsTsv)
            except Exception:
                rows = []
            for row in rows[:MAX_LESION_ROWS]:
                path = row.get("T")
                if path and path != "n/a":
                    sid = row.get("participant_id") or os.path.basename(path)
                    sources.append(MapSource(f"lesion:{sid}", _("Lesion: {sid}").format(sid=sid), "lesion", path, {"kind": "lesion", "participant_id": sid}))
        return sources

    def refreshSources(self):
        """Rebuild the tree, keeping the ticks."""
        checked = set(self.checkedKeys())
        first = not self.sources
        self.sources = {source.key: source for source in self.discoverSources()}
        self.mapList.blockSignals(True)
        try:
            self.mapList.clear()
            for group, title in GROUPS:
                members = [source for source in self.sources.values() if source.group == group]
                if not members:
                    continue
                parent = qt.QTreeWidgetItem([title])
                parent.setFlags(qt.Qt.ItemIsEnabled)
                self.mapList.addTopLevelItem(parent)
                for source in members:
                    item = qt.QTreeWidgetItem([source.label])
                    item.setData(0, qt.Qt.UserRole, source.key)
                    item.setFlags(qt.Qt.ItemIsEnabled | qt.Qt.ItemIsSelectable | qt.Qt.ItemIsUserCheckable)
                    default = first and source.group in ("result", "group", "connectome")
                    item.setCheckState(0, qt.Qt.Checked if source.key in checked or default else qt.Qt.Unchecked)
                    if source.tooltip:
                        item.setToolTip(0, source.tooltip)
                    parent.addChild(item)
                parent.setExpanded(group != "lesion")
        finally:
            self.mapList.blockSignals(False)
        self.updateState()

    def _items(self):
        for top in range(self.mapList.topLevelItemCount):
            parent = self.mapList.topLevelItem(top)
            for index in range(parent.childCount()):
                yield parent.child(index)

    def checkedKeys(self):
        return [str(item.data(0, qt.Qt.UserRole)) for item in self._items() if item.checkState(0) == qt.Qt.Checked]

    def setChecked(self, keys):
        wanted = set(keys)
        self.mapList.blockSignals(True)
        try:
            for item in self._items():
                item.setCheckState(0, qt.Qt.Checked if str(item.data(0, qt.Qt.UserRole)) in wanted else qt.Qt.Unchecked)
        finally:
            self.mapList.blockSignals(False)
        self.updateState()

    def _onItemChanged(self, item=None, column=0):
        self.updateState()

    def selectedReferenceId(self):
        item = self.mapList.currentItem()
        key = str(item.data(0, qt.Qt.UserRole) or "") if item is not None else ""
        return key.split(":", 1)[1] if key.startswith("reference:") else None

    #
    # State
    #

    def runState(self):
        if self.ctx.bundle is None:
            return False, _("Choose a connectome in the Connectome card: maps are compared on its voxels.")
        if len(self.checkedKeys()) < 2:
            return False, _("Tick at least two maps.")
        return True, ""

    def inStudyPairs(self):
        """Index pairs of the last comparison whose maps were both built from in-study raw inputs."""
        if self.comparison is None:
            return []
        from LesionNetworkMappingLib import refmaps

        specs = [source.spec for source in self.comparedSources]
        return [(i, j) for i in range(len(specs)) for j in range(i + 1, len(specs)) if not refmaps.p_reason(specs[i], specs[j])]

    def updateState(self):
        enabled, reason = self.runState()
        self.compareButton.setEnabled(enabled and not self.running())
        self.compareButton.setToolTip(reason or _("Compare the ticked maps"))
        pairs = self.inStudyPairs()
        self.permButton.setEnabled(bool(pairs) and not self.running())
        self.permButton.setToolTip(_("Reshuffle the scores or labels of the in-study maps ({n} pairs)").format(n=len(pairs)) if pairs
                                   else _("Permutation p needs two ticked maps built from this study (Build from study), then Compare"))
        self.stateLabel.setText(reason)
        self.stateLabel.setVisible(bool(reason))
        self.noticeLabel.setText(self.notice)
        self.noticeLabel.setVisible(bool(self.notice))
        self.addMapButton.setEnabled(self.mapsTsv() is not None)

    def refresh(self, *args):
        if self._cleanedUp:
            return
        try:
            self.widget.objectName
        except (ValueError, RuntimeError):
            return
        if not self.expanded():
            self.updateState()
            return
        self.refreshSources()

    def running(self):
        return self.job is not None and not self.job.isFinished()

    def _setNotice(self, text):
        self.notice = text or ""
        self.noticeLabel.setText(self.notice)
        self.noticeLabel.setVisible(bool(self.notice))

    #
    # In-study maps (raw inputs: the per-lesion maps and participants.tsv)
    #

    def participants(self):
        """(header, rows) of participants.tsv, or ([], [])."""
        study = self.ctx.study
        path = os.path.join(study.path, "participants.tsv") if study is not None and getattr(study, "path", None) else None
        if not path or not os.path.isfile(path):
            return [], []
        from LesionNetworkMappingLib.study import read_tsv

        try:
            return read_tsv(path)
        except Exception:
            return [], []

    def studyColumns(self):
        """[(kind, column, levels)]: numeric columns with 4+ distinct values (score maps) and two-level columns (contrasts)."""
        header, rows = self.participants()
        out = []
        for column in header:
            if column in ("participant_id", "lesion", "lesion_sha256"):
                continue
            values = [str(row.get(column) or "").strip() for row in rows]
            values = [value for value in values if value and value.lower() not in ("n/a", "na", "nan")]
            distinct = sorted(set(values))
            numbers = []
            for value in values:
                try:
                    numbers.append(float(value))
                except ValueError:
                    numbers = None
                    break
            if numbers and len(set(numbers)) >= 4:
                out.append(("score", column, None))
            elif len(distinct) == 2:
                out.append(("contrast", column, (distinct[0], distinct[1])))
        return out

    def _fillStudyMenu(self):
        self.addMapMenu.clear()
        columns = self.studyColumns()
        if not columns:
            action = self.addMapMenu.addAction(_("No score or two-group column in participants.tsv"))
            action.setEnabled(False)
            return
        for kind, column, levels in columns:
            text = _("Score map: {column}").format(column=column) if kind == "score" else _("Contrast: {a} \u2212 {b} ({column})").format(a=levels[0], b=levels[1], column=column)
            action = self.addMapMenu.addAction(text)
            action.connect("triggered()", lambda kind=kind, column=column, levels=levels: self.addInStudyMap(kind, column, levels))

    def addInStudyMap(self, kind, column, levels=None):
        """Add (and tick) a map built from the per-lesion maps and a participants.tsv column; return its key."""
        entry = (kind, column, tuple(levels) if levels else None)
        if entry not in self.inStudy:
            self.inStudy.append(entry)
        key = f"instudy:{kind}:{column}:{entry[2]}"
        self.refreshSources()
        self.setChecked([*self.checkedKeys(), key])
        return key

    def rawInputs(self):
        """(matrix (n, V) float32 of the per-lesion T maps, participant ids) of the current maps.tsv (cached)."""
        import numpy as np

        from LesionNetworkMappingLib import cohort, cohortmaps

        path = self.mapsTsv()
        bundle = self.ctx.bundle
        if not path or bundle is None:
            raise ValueError(_("Map the cohort first (Cohort card): maps built from the study use the per-lesion maps."))
        key = (path, os.path.getmtime(path), bundle.path)
        if self._raw is not None and self._raw[0] == key:
            return self._raw[1], self._raw[2]
        rows = [row for row in cohortmaps.read_maps_tsv(path) if row.get("T") and row["T"] != "n/a" and os.path.isfile(row["T"])]
        if not rows:
            raise ValueError(_("The cohort maps have no T maps."))
        matrix = cohort.load_masked([row["T"] for row in rows], np.asarray(bundle.mask_indices), bundle.shape)
        ids = [row.get("participant_id") for row in rows]
        self._raw = (key, matrix, ids)
        return matrix, ids

    def inStudySpec(self, source):
        """The ``refmaps.InStudyMap`` of an in-study source (rows of the raw matrix with their score or 0/1 label)."""
        import numpy as np

        from LesionNetworkMappingLib import refmaps

        _matrix, ids = self.rawInputs()
        kind, column = source.info["kind"].split(" ", 1)[1], source.info["column"]
        levels = source.info.get("levels")
        _header, rows = self.participants()
        byId = {row.get("participant_id"): row for row in rows}
        indices, values = [], []
        for index, sid in enumerate(ids):
            text = str((byId.get(sid) or {}).get(column) or "").strip()
            if kind == "score":
                try:
                    value = float(text)
                except ValueError:
                    continue
            else:
                if text not in levels:
                    continue
                value = 1.0 if text == levels[0] else 0.0
            indices.append(index)
            values.append(value)
        return refmaps.in_study_map(source.label, kind, np.asarray(indices), np.asarray(values))

    #
    # Compare
    #

    def vector(self, source):
        """A source's map on the connectome voxels ((V,) float64; NaN where not covered)."""
        import numpy as np

        from LesionNetworkMappingLib import refmaps

        bundle = self.ctx.bundle
        cacheKey = (source.key, bundle.path, os.path.getmtime(source.path) if source.path and os.path.isfile(source.path) else None)
        if source.group != "instudy" and cacheKey in self._vectors:
            return self._vectors[cacheKey]
        if source.vector is not None:
            values = np.asarray(source.vector, dtype=np.float64)
        elif source.group == "reference":
            values = refmaps.resample_to(source.info["id"], bundle, root=registryRoot())
        elif source.group == "instudy":
            matrix, _ids = self.rawInputs()
            source.spec = self.inStudySpec(source)
            values = refmaps.build_in_study(matrix, source.spec)
        else:
            values = refmaps.map_on_grid(source.path, bundle)
        if source.group != "instudy":
            self._vectors[cacheKey] = values
        return values

    def compare(self):
        """Compare the ticked maps; return the ``refmaps.Comparison`` (None with the reason shown)."""
        import numpy as np

        from LesionNetworkMappingLib import refmaps

        enabled, reason = self.runState()
        if not enabled:
            self._setNotice(reason)
            return None
        selected = [self.sources[key] for key in self.checkedKeys() if key in self.sources]
        maps, used, problems = {}, [], []
        qt.QApplication.setOverrideCursor(qt.Qt.WaitCursor)
        try:
            for source in selected:
                try:
                    values = self.vector(source)
                except Exception as exc:
                    logging.exception(f"Lesion network mapping: the map {source.label} could not be loaded")
                    problems.append(f"{source.label}: {exc}")
                    continue
                if np.count_nonzero(np.isfinite(values)) < 3:
                    problems.append(_("{label}: does not cover this connectome's voxels").format(label=source.label))
                    continue
                name = source.name
                while name in maps:
                    name += "\u2032"
                maps[name] = values
                used.append(source)
            if len(maps) < 2:
                self._setNotice(" ".join(problems) or _("Tick at least two maps."))
                return None
            reference = self.reference()
            try:
                comparison = refmaps.compare(maps, reference if reference is not None else None)
            except Exception as exc:
                logging.exception("Lesion network mapping: the comparison failed")
                self._setNotice(_("The comparison failed: {error}").format(error=exc))
                return None
        finally:
            qt.QApplication.restoreOverrideCursor()
        self.comparison = comparison
        self.comparedSources = used
        self.tests = {}
        self.top = self._topSimilar(maps, used, reference)
        notes = list(problems)
        if reference is None:
            notes.append(_("The connectome has no reference maps (μC_r, PC1): partial r is not shown. Build them in the Connectome card (Manage…)."))
        warnings = [refmaps.family_warning(source.info, self._bundleSpace()) for source in used if source.group == "reference"]
        notes.extend(warning for warning in warnings if warning)
        self._setNotice(" ".join(notes))
        self.showComparison()
        self.updateState()
        return comparison

    def _bundleSpace(self):
        bundle = self.ctx.bundle
        try:
            from LesionNetworkMappingLib import geometry

            return geometry.detect_space(bundle.shape, bundle.affine)
        except Exception:
            return None

    def _topSimilar(self, maps, used, reference):
        from LesionNetworkMappingLib import refmaps

        names = list(maps)
        if len(names) < 2:
            return []
        condition = str(self.conditionEdit.text or "").strip() or None
        candidates = []
        for name, source in zip(names[1:], used[1:], strict=True):
            sourceCondition = source.info.get("condition")
            if sourceCondition is None and source.group in ("result", "group", "instudy", "lesion", "other"):
                sourceCondition = condition  # the study's own maps share its condition
            kind = "connectome" if source.group == "connectome" else source.info.get("kind") or "map"
            candidates.append({"name": name, "values": maps[name], "condition": sourceCondition, "kind": kind})
        try:
            return refmaps.top_similar(maps[names[0]], candidates, k=refmaps.TOP_K, condition=condition, reference=reference)
        except Exception:
            logging.exception("Lesion network mapping: the most similar maps could not be ranked")
            return []

    def _heatmapPixmap(self):
        from LesionNetworkMappingLib import refmaps

        figure = refmaps.heatmap_figure(self.comparison.names, self.comparison.pearson, self.comparison.partial, title=_("Map comparison"), font=interFont())
        pixmap = qt.QPixmap()
        pixmap.loadFromData(qt.QByteArray(figure.png_bytes()), "PNG")
        if pixmap.width() > HEATMAP_MAX_PX:
            pixmap = pixmap.scaledToWidth(HEATMAP_MAX_PX, qt.Qt.SmoothTransformation)
        return pixmap

    def showComparison(self):
        from LesionNetworkMappingLib import refmaps

        comparison = self.comparison
        try:
            self.heatmap.setPixmap(self._heatmapPixmap())
        except Exception:
            logging.exception("Lesion network mapping: the heat map could not be drawn")
            self.heatmap.clear()
        self.legendLabel.setText("  ".join(f"{index + 1} {name}" for index, name in enumerate(comparison.names)))
        rows = refmaps.pairs_rows(comparison, self.tests, {name: source.spec for name, source in zip(comparison.names, self.comparedSources, strict=True)})
        self.pairsTable.setRowCount(0)
        self.pairsTable.setRowCount(len(rows))
        for row, pair in enumerate(rows):
            cells = [pair["map_a"], pair["map_b"], _number(pair["pearson_r"]), _number(pair["spearman_rho"]), _number(pair["partial_r"]),
                     _number(pair["p_perm"], 3) if pair["p_perm"] != "" else "—"]
            for column, text in enumerate(cells):
                item = qt.QTableWidgetItem(text)
                if column == 5:
                    item.setToolTip(pair["p_note"] or _("p of the {statistic} on {voxels} voxels; {n} permutations of the participants ({scheme}); "
                                                        "smallest p {p}").format(statistic=pair["p_statistic"], voxels=pair["p_voxels"],
                                                                                 n=pair["n_perm"], scheme=pair["p_scheme"],
                                                                                 p=_number(1 / (int(pair["n_perm"]) + 1), 4)))
                elif column < 2:
                    item.setToolTip(text)
                self.pairsTable.setItem(row, column, item)
        self.pairsTable.setFixedHeight(ROW_HEIGHT * (min(len(rows), 10) + 1) + 8)
        self._showTop()
        self._showLicences()
        self.results.show()

    def _showTop(self):
        import html

        if not self.top:
            self.topTitle.hide()
            self.topLabel.hide()
            return
        self.topTitle.setText(_("Most similar to {name}").format(name=self.comparison.names[0]))
        lines = []
        for row in self.top:
            partial = row.get("partial_r")
            text = f"<b>{_number(row['r'])}</b> {html.escape(str(row['name']))}"
            if partial is not None:
                text += f" <span>(partial {_number(partial)})</span>"
            if row.get("note"):
                text += f"<br/><i>{html.escape(row['note'])}</i>"
            lines.append(text)
        self.topLabel.setText("<br/>".join(lines))
        self.topTitle.show()
        self.topLabel.show()

    def _showLicences(self):
        import html

        rows = []
        for source in self.comparedSources:
            if source.group != "reference":
                continue
            info = source.info
            text = f"<b>{html.escape(str(info.get('title') or info.get('id')))}</b>: {html.escape(str(info.get('licence') or 'unknown'))}"
            if info.get("licence_note"):
                text += f" — {html.escape(str(info['licence_note']))}"
            if info.get("citation"):
                text += f"<br/>{html.escape(str(info['citation']))}"
            rows.append(text)
        self.licenceLabel.setText((_("Sources and licences:") + "<br/>" + "<br/>".join(rows)) if rows else "")
        self.licenceLabel.setVisible(bool(rows))

    #
    # Permutation p (in-study pairs only)
    #

    def permute(self):
        """Permutation p of every compared pair built from in-study raw inputs; return {(i, j): PermutationResult}."""
        from LesionNetworkMappingLib import refmaps

        pairs = self.inStudyPairs()
        if not pairs:
            self._setNotice(_("Permutation p needs two compared maps built from this study (Build from study)."))
            return {}
        reference = self.reference()
        covariates = reference.covariates(refmaps.PARTIAL_COVARIATES) if reference is not None else None
        try:
            matrix, _ids = self.rawInputs()
        except Exception as exc:
            self._setNotice(str(exc))
            return {}
        nPerm = int(self.permSpin.value)
        tests = {}
        self.permButton.setEnabled(False)
        qt.QApplication.setOverrideCursor(qt.Qt.WaitCursor)
        try:
            for number, (i, j) in enumerate(pairs):
                a, b = self.comparedSources[i].spec, self.comparedSources[j].spec

                def progress(done, total, number=number):
                    self.stateLabel.setText(_("Permutations: pair {pair}/{pairs}, {done}/{total}").format(pair=number + 1, pairs=len(pairs), done=done, total=total))
                    self.stateLabel.show()
                    slicer.app.processEvents(qt.QEventLoop.ExcludeUserInputEvents)

                try:
                    tests[(i, j)] = refmaps.permutation_p(matrix, a, b, n_perm=nPerm, seed=number, covariates=covariates, progress=progress)
                except refmaps.RefmapsError as exc:
                    self._setNotice(f"{a.name} / {b.name}: {exc}")
        finally:
            qt.QApplication.restoreOverrideCursor()
        self.tests = tests
        self.showComparison()
        self.updateState()
        return tests

    #
    # Export
    #

    def export(self):
        """Write the comparison to ``<runs>/compare/<time>`` (CSV, compare.json, figure); return the folder or None."""
        from LesionNetworkMappingLib import refmaps

        if self.comparison is None:
            self._setNotice(_("Compare first."))
            return None
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        out = os.path.join(self.outputRoot(), refmaps.COMPARE_DIR, stamp)
        infos = {}
        for name, source in zip(self.comparison.names, self.comparedSources, strict=True):
            info = dict(source.info)
            info["in_study"] = source.spec
            infos[name] = info
        reference = self.reference()
        try:
            written = refmaps.write_outputs(self.comparison, out, infos=infos, tests=self.tests, top=self.top,
                                            condition=str(self.conditionEdit.text or "").strip() or None, font=interFont(),
                                            reference_sha256=reference.sha256 if reference is not None else None)
        except Exception as exc:
            logging.exception("Lesion network mapping: the comparison could not be exported")
            self._setNotice(_("The comparison could not be exported: {error}").format(error=exc))
            return None
        self.lastExport = out
        self._registerRun(out, infos, reference)
        self.folderButton.setEnabled(True)
        self._setNotice(_("Exported to {folder}").format(folder=os.path.join(refmaps.COMPARE_DIR, stamp)))
        return written["json"] and out

    def registryRunsRoot(self):
        """The root of the run registry (``<root>/derivatives/lnm/runs.jsonl``): the study folder, else the default run root."""
        study = getattr(self.ctx, "study", None)
        path = getattr(study, "path", None) if study is not None else None
        return os.fspath(path) if path else self.outputRoot()

    def _registerRun(self, out, infos, reference):
        """Record the exported comparison as a done run of kind "comparison" (checklist item G10 reads it)."""
        from LesionNetworkMappingLib import runs

        references = [info for info in infos.values() if info.get("id")]  # the library's reference maps (the others carry no library id)
        params = {"maps": list(self.comparison.names), "covariates": list(self.comparison.covariates),
                  "references": [{"id": info.get("id"), "sha256": info.get("sha256"), "condition": info.get("condition")} for info in references],
                  "reference_sha256": reference.sha256 if reference is not None else None}
        summary = {"n_maps": len(self.comparison.names), "n_reference_maps": len(references), "n_tests": len(self.tests or {})}
        self.lastRunId = None
        try:
            self.lastRunId = runs.register(self.registryRunsRoot(), "comparison", out, params=params, status="done", summary=summary)
        except Exception:
            logging.exception("Lesion network mapping: the comparison could not be recorded in the run registry")

    def showFolder(self):
        if self.lastExport:
            widgets.showInFileManager(self.lastExport)

    def testDifferences(self):
        """Open the Cohort card's Contrast tab (differences are tested there, by label permutation)."""
        self.ctx.focusCard("cohort")
        card = self.ctx.card("cohort")
        tab = card.tab("contrast") if card is not None and hasattr(card, "tab") else None
        widget = getattr(tab, "widget", None)
        if widget is not None and hasattr(card, "tabs"):
            card.tabs.setCurrentWidget(widget)
            return True
        return False

    #
    # Reference library (each addition is a click; downloads run as light jobs)
    #

    def openLibraryFolder(self):
        root = registryRoot()
        os.makedirs(root, exist_ok=True)
        widgets.showInFileManager(root)

    def removeSelectedReference(self):
        from LesionNetworkMappingLib import refmaps

        entryId = self.selectedReferenceId()
        if not entryId:
            self._setNotice(_("Select a reference map in the list first."))
            return False
        if not slicer.util.confirmYesNoDisplay(_("Remove {id} from the reference library? Its copied or downloaded file is deleted too (unless another entry uses it).").format(id=entryId)):
            return False
        try:
            refmaps.remove_entry(entryId, root=registryRoot())
        except Exception as exc:
            self._setNotice(str(exc))
            return False
        self.refreshSources()
        return True

    def addReference(self, kind):
        """Ask for the source in a window-modal dialog (shown, not exec'd: Qt 5 and Qt 6); accepting it starts the addition
        (a light job for downloads, kept in ``lastAddition``). Return the dialog.
        """
        previous = getattr(self, "addDialog", None)
        if previous is not None:
            previous.close()
        dialog = AddReferenceDialog(kind, slicer.util.mainWindow())
        dialog.setModal(True)
        self.addDialog = dialog

        def onAccepted():
            self.lastAddition = self.startAddition(kind, dialog.values())

        def onFinished(_result):
            if getattr(self, "addDialog", None) is dialog:
                self.addDialog = None
            dialog.deleteLater()

        dialog.connect("accepted()", onAccepted)
        dialog.connect("finished(int)", onFinished)
        dialog.show()
        return dialog

    def additionArgs(self, kind, values):
        """Command-line arguments of ``refmaps add-<kind>`` for the dialog values."""
        args = [f"add-{kind}", "--root", registryRoot()]
        if values.get("condition"):
            args += ["--condition", values["condition"]]
        if kind == "neurovault":
            text = str(values.get("id") or "").strip().lower()
            match = re.match(r"^(?:c|collection[: ]?)(\d+)$", text) or re.search(r"collections/(\d+)", text)
            if match:
                args += ["--collection", match.group(1)]
            else:
                match = re.search(r"(\d+)", text)
                if not match:
                    raise ValueError(_("Give a NeuroVault image id (or c<id> for a collection)"))
                args += ["--image", match.group(1)]
        elif kind == "url":
            args += ["--url", values["url"]]
        elif kind == "vdh":
            args += ["--accepted-terms-sha256", values["terms_sha256"]]
        if kind == "url":
            for key in ("title", "licence", "citation"):
                if values.get(key):
                    args += [f"--{key}", values[key]]
        return args

    def startAddition(self, kind, values):
        from LesionNetworkMappingLib import refmaps

        from . import jobrunner

        if kind == "file":
            try:
                entry = refmaps.add_file(values["file"], title=values.get("title") or None, licence=values.get("licence") or "unknown",
                                         citation=values.get("citation") or "", condition=values.get("condition") or None, root=registryRoot())
            except Exception as exc:
                self._setNotice(str(exc))
                return None
            self._setNotice(_("Added {title} to the reference library.").format(title=entry.get("title")))
            self.refreshSources()
            self.setChecked([*self.checkedKeys(), f"reference:{entry['id']}"])
            return entry
        try:
            args = self.additionArgs(kind, values)
        except (ValueError, KeyError) as exc:
            self._setNotice(str(exc))
            return None
        spec = jobrunner.JobSpec(JOB_MODULE, args, title=_("Add reference map"), heavy=False, kind="refmaps", name="refmaps",
                                 runRoot=jobrunner.defaultRunRoot(), onDone=self._onAdditionDone)
        self.job = self.ctx.submit(spec)
        self._setNotice(_("Downloading…"))
        self.updateState()
        return self.job

    def _onAdditionDone(self, job):
        if self._cleanedUp:
            return
        if job.state != "done":
            self._setNotice(_("The reference map was not added: {reason}").format(reason=job.failureReason or job.state))
        else:
            added = [event.get("entry") or {} for event in job.results if event.get("kind") == "refmap"]
            self._setNotice(_("Added {titles} to the reference library.").format(titles=", ".join(str(entry.get("title")) for entry in added)))
            self.refreshSources()
            self.setChecked([*self.checkedKeys(), *(f"reference:{entry.get('id')}" for entry in added)])
        self.updateState()


class AddReferenceDialog(qt.QDialog):
    """The source of a reference map: NeuroVault id, URL, file or the van den Heuvel degree map (with the live GSP terms)."""

    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.termsSha = None
        self.termsJob = None
        self.setObjectName("LNMCompareAddDialog")
        self.setWindowTitle({"neurovault": _("Add a NeuroVault map"), "url": _("Add a map from a URL"), "file": _("Add a map file"),
                             "vdh": _("Add the van den Heuvel degree map")}[kind])
        self.setMinimumWidth(460)
        layout = qt.QVBoxLayout(self)
        form = qt.QFormLayout()
        self.idEdit = qt.QLineEdit()
        self.urlEdit = qt.QLineEdit()
        self.fileEdit = qt.QLineEdit()
        self.titleEdit = qt.QLineEdit()
        self.licenceEdit = qt.QLineEdit("unknown")
        self.citationEdit = qt.QLineEdit()
        self.conditionEdit = qt.QLineEdit()
        self.conditionEdit.setPlaceholderText(_("e.g. pain, depression"))
        for edit, name in ((self.idEdit, "Id"), (self.urlEdit, "Url"), (self.fileEdit, "File"), (self.titleEdit, "Title"), (self.licenceEdit, "Licence"),
                           (self.citationEdit, "Citation"), (self.conditionEdit, "Condition")):
            edit.setObjectName(f"LNMCompareAdd{name}Edit")
        if kind == "neurovault":
            note = label(_("Public NeuroVault images are shared under CC0. The map is downloaded now, through NeuroVault's public API, "
                           "and kept in your reference library."), "LNMCompareAddNote", "secondaryText")
            layout.addWidget(note)
            self.idEdit.setPlaceholderText(_("image id (12345), c3324 for a collection, or a neurovault.org link"))
            form.addRow(_("NeuroVault:"), self.idEdit)
        elif kind == "url":
            self.urlEdit.setPlaceholderText("https://…/map.nii.gz")
            form.addRow(_("URL:"), self.urlEdit)
        elif kind == "file":
            row = qt.QHBoxLayout()
            row.addWidget(self.fileEdit, 1)
            browse = qt.QPushButton(_("Browse…"))
            browse.connect("clicked()", self._browse)
            row.addWidget(browse)
            form.addRow(_("File:"), row)
        if kind in ("url", "file"):
            form.addRow(_("Title:"), self.titleEdit)
            self.licenceEdit.setToolTip(_("The licence of the map as its source states it (e.g. CC0-1.0, CC-BY-4.0); 'unknown' when not stated"))
            form.addRow(_("Licence:"), self.licenceEdit)
            form.addRow(_("Citation:"), self.citationEdit)
        if kind == "vdh":
            from LesionNetworkMappingLib import refmaps

            layout.addWidget(label(_("Attribution: {text}").format(text=refmaps.VDH_ATTRIBUTION), "LNMCompareVdhAttribution", "secondaryText"))
            layout.addWidget(label(refmaps.VDH_NOTE, "LNMCompareVdhNote", "secondaryText"))
            layout.addWidget(label(_("The degree map is derived from the GSP connectome: the GSP data use terms (GSP-DUT-2014) apply. Read them below."),
                                   "LNMCompareVdhTermsIntro", "secondaryText"))
            self.termsView = qt.QTextBrowser()
            self.termsView.setObjectName("LNMCompareVdhTerms")
            self.termsView.setOpenExternalLinks(True)
            self.termsView.setMinimumHeight(90)
            self.termsView.setMaximumHeight(160)
            layout.addWidget(self.termsView)
            self.acceptCheck = qt.QCheckBox(_("I accept these data use terms"))
            self.acceptCheck.setObjectName("LNMCompareVdhAccept")
            self.acceptCheck.setEnabled(False)
            self.acceptCheck.connect("toggled(bool)", self._onAccept)
            layout.addWidget(self.acceptCheck)
        form.addRow(_("Condition:"), self.conditionEdit)
        layout.addLayout(form)
        self.buttons = qt.QDialogButtonBox()
        self.okButton = self.buttons.addButton(_("Add") if kind == "file" else _("Download"), qt.QDialogButtonBox.AcceptRole)
        self.okButton.setObjectName("LNMCompareAddOkButton")
        self.okButton.setDefault(True)
        self.buttons.addButton(_("Cancel"), qt.QDialogButtonBox.RejectRole)
        self.buttons.connect("accepted()", self.accept)
        self.buttons.connect("rejected()", self.reject)
        layout.addWidget(self.buttons)
        if kind == "vdh":
            self.okButton.setEnabled(False)
            self.loadTerms()

    def _browse(self):
        path = qt.QFileDialog.getOpenFileName(self, _("Reference map"), "", _("NIfTI (*.nii *.nii.gz)"))
        if path:
            self.fileEdit.setText(path)
            if not self.titleEdit.text:
                self.titleEdit.setText(re.sub(r"\.nii(\.gz)?$", "", os.path.basename(path)))

    def loadTerms(self):
        """Fetch the live GSP terms text in a light job (never on the main thread)."""
        from . import jobrunner

        self.termsView.setPlainText(_("Fetching the GSP data use terms from Harvard Dataverse…"))
        spec = jobrunner.JobSpec(TERMS_MODULE, ["terms"], title=_("Data use terms"), heavy=False, kind="leaddbs-terms", name="leaddbs-terms",
                                 runRoot=jobrunner.defaultRunRoot(), onDone=self._onTermsDone)
        self.termsJob = jobrunner.instance().submit(spec)
        return self.termsJob

    def _onTermsDone(self, job):
        try:
            self.objectName
        except (ValueError, RuntimeError):
            return
        event = next((event for event in job.results if event.get("kind") == "terms"), None)
        if event is None:
            self.termsView.setPlainText(_("The data use terms could not be fetched ({reason}). They must be shown before anything is downloaded: "
                                          "check the connection and try again.").format(reason=job.failureReason or job.state))
            return
        self.setTermsText(event.get("text") or "", event.get("url"))

    def setTermsText(self, text, url=None):
        from LesionNetworkMappingLib import catalog

        self.termsSha = catalog.terms_sha256(text)
        self.termsView.setPlainText(text + (f"\n\n{url}" if url else ""))
        self.acceptCheck.setEnabled(True)
        self.acceptCheck.setChecked(_settingTrue(f"{ACCEPTED_TERMS_TEXT_SETTING}/{self.termsSha}"))
        self.okButton.setEnabled(bool(self.acceptCheck.checked))

    def _onAccept(self, checked):
        if self.termsSha:
            settings = qt.QSettings()
            key = f"{ACCEPTED_TERMS_TEXT_SETTING}/{self.termsSha}"
            if checked:
                settings.setValue(key, "true")
            else:
                settings.remove(key)
        self.okButton.setEnabled(bool(checked) and bool(self.termsSha))

    def values(self):
        return {"id": str(self.idEdit.text).strip(), "url": str(self.urlEdit.text).strip(), "file": str(self.fileEdit.text).strip(),
                "title": str(self.titleEdit.text).strip(), "licence": str(self.licenceEdit.text).strip() or "unknown",
                "citation": str(self.citationEdit.text).strip(), "condition": str(self.conditionEdit.text).strip(), "terms_sha256": self.termsSha}


#
# Search entry (W3-06 provider)
#


def showCard():
    """Show Map (the Studio workspace, else the module) scrolled to the Compare card; return the card or None."""
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
    ctx.focusCard("compare")
    return ctx.card("compare")


def paletteEntries(shell=None):
    """Search entries "Compare maps" and "Add a reference map" (dict form of the provider contract, USABILITY 4)."""
    return [
        {"id": "lnm.compare", "title": _("Compare maps"), "category": _("Map"),
         "synonyms": ["comparison matrix", "cross-correlogram", "spatial correlation", "similarity", "repetition across conditions", "partial r"],
         "callback": showCard, "description": _("Spatial r, \u03c1 and partial r between your maps, μC, PC1 and reference maps")},
        {"id": "lnm.compare.reference", "title": _("Add a reference map"), "category": _("Map"),
         "synonyms": ["neurovault", "reference library", "van den Heuvel", "degree map"], "callback": showCard,
         "description": _("Add a NeuroVault, URL or file map to the local reference library (downloaded only when you click)")},
    ]


def registerPaletteProvider():
    """Register the Search entries with the Studio palette (guarded: nothing happens without Studio)."""
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
        logging.exception("Lesion network mapping: the comparison Search entries could not be registered")
        return False
    return True


registerPaletteProvider()


CARD = {
    "id": "compare",
    "title": "Compare",
    "order": ORDER_COMPARE,
    "modes": ("lesion", "cohort"),
    "factory": CompareCard,
}

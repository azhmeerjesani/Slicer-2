"""Fast-index action of the connectome manager (Manage… menu; L-38, addendum §3.12 and §3.6).

**Build fast index…** opens one non-modal dialog with three parts:

1. **Build**: the atlas pack (the default Schaefer-400 + Tian S2 + Buckner cerebellum + brain stem label, or Schaefer-1000
   + Tian S4 with the same cerebellum and brain stem; supervoxels cover every voxel no atlas claims), the subjects (all,
   the first 100, or the subjects of the cohort's lesion maps), the estimated time and disk, and the job with progress and
   Stop (``fastmap build``, a heavy job). Atlases are never downloaded here unless "Get atlases…" is clicked and confirmed.
2. **Parity**: ``fastmap parity`` on up to 60 of the cohort's exact lesion maps (at least 10 are needed), with the
   per-stratum and per-size table, the median partial r and top-10% Dice against the gate, and the verdict. The report is
   written next to the cohort's lesion maps (``fastparity/parity.json``), which is where the Nulls card looks: **Fast mode
   of the Nulls card is offered only after this gate passed on the user's own lesions** (:func:`fastModeState`).
3. **C parity** (optional): the parcel matrix of the user's own copy of the van den Heuvel et al. 2026 release
   (``GSP1000_FCz_Schaefer1000Melbourne54.mat``) against the index's parcel matrix C, with the GSP data use terms notice.
   The file is read for this comparison only: it is never copied, and the result carries the terms tag.

The pure helpers (:func:`fastModeState`, :func:`parityFolder`, ...) use no Qt and are shared with the Nulls card. A job keeps
running when the dialog is closed (Esc). Works in Classic and in Studio; ``dialog.cache`` and ``dialog.tilingDir`` let tests
and custom workflows choose the atlas cache and an existing tiling folder.
"""

import json
import logging
import math
import os
import shutil

import qt
import slicer
from slicer.i18n import tr as _

from . import jobrunner, widgets
from .card_validate import _fitTable

JOB_MODULE = "LesionNetworkMappingLib.fastmap"
#: Folder of the cohort's lesion maps (next to maps.tsv) that holds the parity report the Nulls card reads
PARITY_FOLDER = "fastparity"
PARITY_JSON = "parity.json"
#: The subjects of the lesion maps, when the index is built for exactly them (ids only: no local path)
SUBJECTS_FILE = "subjects.txt"
C_PARITY_JSON = "c_parity.json"
C_PARITY_FORMAT = "slicer-lnm-fast-cparity"
PACKS = ("standard", "fine", "custom")
#: Parcels (atlas parcels plus about 110 supervoxels) of the packs, for the estimates; an existing index gives its own number
PACK_PARCELS = {"standard": 550, "fine": 1200}
#: The fine pack replaces these atlases of the default tiling
FINE_ATLASES = {"schaefer2018-400-7n-2mm": "schaefer2018-1000-7n-2mm", "tian-s2-2mm": "tian-s4-2mm"}
FIRST_N = 100
#: Exact lesions used for the parity check (at most), and the longest command line used for their file names
MAX_PARITY_LESIONS = 60
ARGUMENT_BUDGET_CHARS = 20000
#: Subjects per checkpoint of the build (fastmap.DEFAULT_CHECKPOINT): more subjects write checkpoints of 4 * R * V bytes
CHECKPOINT_SUBJECTS = 100
SIZE_BIN_TEXT = {"lt1ml": "< 1 mL", "1to10ml": "1–10 mL", "10to50ml": "10–50 mL", "ge50ml": "≥ 50 mL"}  # noqa: RUF001
PARITY_COLUMNS = (_("Group"), _("Lesions"), _("Partial r"), _("Top-10% Dice"), _("Gate"))
PARITY_COLUMN_TIPS = (_("A location stratum or a lesion size bin"), _("Exact lesions in the group"), _("Median partial r of the approximate and the exact map, with the connectome's mean map and PC1 partialled out"),
                      _("Median Dice of the top 10% of the approximate and the exact map"), _("Pass: the medians reach the constants of the gate"))
STRATUM_TEXT = {"cortical": "cortical", "subcortical": "subcortical", "brainstem": "brain stem", "cerebellar": "cerebellar", "white-matter-supervoxel": "white matter (supervoxel)"}
C_NOTICE = _("The parcel matrix of the van den Heuvel et al. 2026 release is derived from GSP1000: the GSP Open Access Data Use Terms (GSP-DUT-2014) apply to it "
             "and to results derived from it. It is read for this comparison only; it is never copied, bundled or uploaded, and the result is tagged with the terms.")
PARCEL_MATRIX_NAME = "GSP1000_FCz_Schaefer1000Melbourne54.mat"

_dialogs = {}


#
# Helpers without Qt
#


def _readJson(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _sha256(path, block=1 << 20):
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            data = f.read(block)
            if not data:
                break
            digest.update(data)
    return digest.hexdigest()


def _number(value, digits=2):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "—"
    return "—" if math.isnan(value) else f"{value:.{digits}f}"


def _duration(seconds):
    """"< 1 min", "12 min", "1 h 20 min"."""
    if seconds is None or not math.isfinite(seconds):
        return "?"
    if seconds < 60:
        return _("< 1 min")
    minutes = round(seconds / 60)
    if minutes < 60:
        return _("{n} min").format(n=minutes)
    hours, minutes = divmod(minutes, 60)
    return _("{h} h {m} min").format(h=hours, m=minutes) if minutes else _("{h} h").format(h=hours)


def _size(nbytes):
    """"0.4 GB", "120 MB"."""
    if nbytes >= 1 << 30:
        return f"{nbytes / (1 << 30):.1f} GB"
    return f"{max(nbytes, 1) / (1 << 20):.0f} MB"


def parityFolder(mapsTsv):
    """Folder of the parity report of a cohort: ``<folder of maps.tsv>/fastparity``."""
    return os.path.join(os.path.dirname(os.path.abspath(os.fspath(mapsTsv))), PARITY_FOLDER)


def parityPath(mapsTsv):
    return os.path.join(parityFolder(mapsTsv), PARITY_JSON)


def cohortSubjects(mapsTsv):
    """The connectome subject ids of the cohort's lesion maps (cohortmaps.json), or None."""
    from LesionNetworkMappingLib import cohortmaps

    record = cohortmaps.read_record(os.path.join(os.path.dirname(os.path.abspath(os.fspath(mapsTsv))), cohortmaps.COHORTMAPS_JSON)) or {}
    ids = record.get("subject_ids")
    return [str(i) for i in ids] if isinstance(ids, list) and ids else None


def indexInfo(folder):
    """{"parcels", "subjects", "created", "sha256"} of a fast index folder (index.json only: no matrix is read), or None."""
    path = os.path.join(folder, "index.json")
    record = _readJson(path)
    if record is None:
        return None
    shape = (((record.get("files") or {}).get("P")) or {}).get("shape") or [None]
    return {"parcels": shape[0], "subjects": (record.get("subjects") or {}).get("n"), "created": (record.get("build") or {}).get("created"), "sha256": _sha256(path),
            "seconds_per_subject": (record.get("build") or {}).get("seconds_per_subject_median")}


class FastModeState:
    """Whether the Nulls card may offer fast mode for the current cohort, and why not.

    ``enabled``; ``reason`` (the sentence that explains a disabled state, or the summary of the gate); ``needsIndex`` (no index
    exists for the cohort's subjects: build one); ``needsParity`` (the index has not been checked on these lesions, or the
    check is out of date); ``index`` (folder of the index or None); ``parity`` (path of parity.json or None); ``report``
    (the parity report or None); ``allowed`` (lesions of the report that the gate allows) and ``total`` (lesions checked).
    """

    def __init__(self, enabled=False, reason="", needsIndex=False, needsParity=False, index=None, parity=None, report=None, allowed=0, total=0):
        self.enabled = bool(enabled)
        self.reason = reason
        self.needsIndex = needsIndex
        self.needsParity = needsParity
        self.index = index
        self.parity = parity
        self.report = report
        self.allowed = allowed
        self.total = total

    def __repr__(self):
        return f"FastModeState(enabled={self.enabled}, reason={self.reason!r})"


def failingText(gate):
    """"brainstem (median partial r 0.61 < 0.80), …": the strata and size bins that failed the gate."""
    parts = []
    for group in ("strata", "size_bins"):
        for name, verdict in (gate.get(group) or {}).items():
            if not verdict.get("passed"):
                label = SIZE_BIN_TEXT.get(name, name) if group == "size_bins" else name
                parts.append(f"{label} ({verdict.get('reason')})")
    return ", ".join(parts)


def fastModeState(bundle, mapsTsv):
    """The :class:`FastModeState` of a connectome (an open ``ConnectomeBundle``) and a cohort's lesion maps (maps.tsv path).

    Fast mode is enabled only when (1) a fast index exists for exactly the cohort's connectome subjects, (2) a parity report
    of that index on the cohort's own exact lesion maps exists, is newer than the lesion maps and belongs to this index, and
    (3) its gate (fastmap.gate: median partial r and top-10% Dice per stratum and size bin, at least 10 lesions) allows at least
    two of the checked lesions.
    """
    from LesionNetworkMappingLib import fastmap, nulls

    if bundle is None:
        return FastModeState(reason=_("Choose a connectome in the Connectome card."))
    if not mapsTsv or not os.path.isfile(mapsTsv):
        return FastModeState(reason=_("Map the lesions first (Cohort card)."))
    ids = cohortSubjects(mapsTsv)
    if not ids:
        return FastModeState(reason=_("The lesion maps do not list the connectome subjects they were computed from."))
    try:
        folder = nulls.find_fast_index(bundle, ids)
    except (OSError, ValueError):
        folder = None
    if folder is None:
        return FastModeState(reason=_("No fast index for this connectome and these {n} subjects yet. Build it in Manage… → Build fast index…, then check it on your lesions.").format(n=len(ids)), needsIndex=True)
    path = parityPath(mapsTsv)
    report = _readJson(path)
    if report is None:
        return FastModeState(reason=_("The fast index has not been checked on your own lesions. Open Build fast index… and run the parity check."), needsParity=True, index=folder)
    info = indexInfo(folder)
    if info is None or (report.get("index") or {}).get("sha256") not in (None, info["sha256"]):
        return FastModeState(reason=_("The parity report belongs to another fast index. Run the parity check again."), needsParity=True, index=folder, parity=path)
    try:
        if os.stat(path).st_mtime_ns < os.stat(mapsTsv).st_mtime_ns:
            return FastModeState(reason=_("The parity report is older than the lesion maps. Run the parity check again."), needsParity=True, index=folder, parity=path)
    except OSError:
        return FastModeState(reason=_("The parity report cannot be read. Run the parity check again."), needsParity=True, index=folder)
    gate = report.get("gate") or fastmap.gate(report)
    total = len(report.get("lesions") or [])
    allowed = total - len(gate.get("excluded") or [])
    if not gate.get("enough_lesions"):
        text = (gate.get("messages") or [""])[0]
        return FastModeState(reason=_("The parity gate needs at least {n} exact lesions. {text}").format(n=gate.get("min_lesions", fastmap.PARITY_MIN_LESIONS), text=text), index=folder, parity=path, report=report, total=total)
    if not gate.get("allowed_strata") or allowed < 2:
        return FastModeState(reason=_("The fast index fails the parity gate on your lesions: {text}.").format(text=failingText(gate) or _("no stratum passed")), index=folder, parity=path, report=report, allowed=allowed, total=total)
    passed = ", ".join(gate["allowed_strata"])
    summary = _("Parity gate passed on your lesions for {passed}: {allowed} of {total} checked lesions allowed.").format(passed=passed, allowed=allowed, total=total)
    failing = failingText(gate)
    if failing:
        summary += " " + _("Left out of fast-mode statistics: {failing}.").format(failing=failing)
    return FastModeState(True, summary, index=folder, parity=path, report=report, allowed=allowed, total=total)


def packAtlasIds(pack, bundle=None):
    """(atlas catalog ids, notices) of an atlas pack; the standard pack lists the default tiling atlases of the connectome's family."""
    from LesionNetworkMappingLib import atlases, fastmap

    entries = [dict(entry) for entry in fastmap.DEFAULT_TILING_ATLASES]
    if pack == "fine":
        for entry in entries:
            entry["id"] = FINE_ATLASES.get(entry["id"], entry["id"])
    notices = []
    if bundle is not None:
        try:
            family = atlases.mask_family(bundle.grid.shape, bundle.grid.affine)[0]
            entries, notices = fastmap._default_entries_for_family(entries, family, False)
        except Exception:
            logging.debug("Lesion network mapping: the atlas family of the connectome could not be determined", exc_info=True)
    return entries, [notice.get("message") for notice in notices if notice.get("message")]


def missingAtlases(ids, cache=None):
    """The atlas ids of ``ids`` that are not in the fetch cache (with their label tables)."""
    from LesionNetworkMappingLib import fetch

    missing = []
    for atlasId in ids:
        try:
            resource = fetch.find_resource(atlasId, ("atlases",))
            labels = resource.label_resource()
            cached = fetch.is_cached(resource, cache) and (labels is None or fetch.is_cached(labels, cache))
        except Exception:
            cached = False
        if not cached:
            missing.append(atlasId)
    return missing


def estimateBuild(bundle, nSubjects, nParcels, threads=None):
    """Estimated build of an index: {"seconds", "final_bytes", "peak_bytes", "memory_bytes"}.

    The time follows the engine model of ``nulls.estimate_details`` (0.13 s per subject plus 4.2 ms per label seed at the
    reference size of 292,019 voxels x 216 frames and 2 threads; at least the time to read the data), with one seed per parcel;
    the files are P (float16, R x V), C (float64, R x R) and the labels, and the checkpoints add 4 * R * V bytes (twice while one
    is replaced) when there are more than 100 subjects. The memory is the running sums (8 * V * R) plus P.
    """
    from LesionNetworkMappingLib import jobs, nulls

    n_voxels = int(bundle.n_voxels)
    ids = list(bundle.subject_ids)[: max(int(nSubjects), 1)]
    sample = ids[:: max(1, len(ids) // 20)] or ids
    mean_t = sum(sum(bundle.runs(i)) for i in sample) / max(len(sample), 1)
    threads = max(int(threads or jobs.budget_threads()), 1)
    scale = n_voxels * mean_t / nulls.ENGINE_REFERENCE_SIZE * (2.0 / threads) ** 0.8
    per_subject = (nulls.ENGINE_SUBJECT_S + nulls.ENGINE_COLUMN_S * nParcels) * scale
    read = n_voxels * mean_t * bundle.dtype.itemsize / nulls.READ_RATE
    seconds = max(per_subject, read) * len(ids)
    final = 2 * nParcels * n_voxels + 8 * nParcels * nParcels + 4 * n_voxels
    checkpoints = 8 * nParcels * n_voxels if len(ids) > CHECKPOINT_SUBJECTS else 0
    return {"seconds": float(seconds), "final_bytes": int(final), "peak_bytes": int(final + checkpoints), "memory_bytes": int(10 * nParcels * n_voxels)}


def _spread(items, count):
    """``count`` items of ``items`` evenly spread over the list (all of them when there are fewer)."""
    if count >= len(items):
        return list(items)
    if count <= 1:
        return list(items[:1])
    return [items[i] for i in sorted({round(k * (len(items) - 1) / (count - 1)) for k in range(count)})]


def parityPairs(mapsTsv, limit=MAX_PARITY_LESIONS, budget=ARGUMENT_BUDGET_CHARS):
    """``[(lesion file, exact AvgFz map)]`` of the cohort's lesion maps: at most ``limit`` evenly spread, within ``budget`` characters."""
    from LesionNetworkMappingLib import cohortmaps, fastmap

    pairs = []
    for row in cohortmaps.read_maps_tsv(mapsTsv):
        lesion, fz = row.get("lesion"), row.get("AvgFz")
        if lesion and fz and cohortmaps.NA not in (lesion, fz) and os.path.isfile(lesion) and os.path.isfile(fz):
            pairs.append((lesion, fz))
    limit = min(limit, len(pairs))
    while True:
        chosen = _spread(pairs, limit)
        if sum(len(a) + len(b) + 10 for a, b in chosen) <= budget or limit <= fastmap.PARITY_MIN_LESIONS:
            return chosen
        limit = max(fastmap.PARITY_MIN_LESIONS, limit - 5)


#
# Dialog
#


def _label(text, objectName, secondary=False, selectable=False):
    label = qt.QLabel(text)
    label.setObjectName(objectName)
    label.setWordWrap(True)
    label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
    if secondary:
        label.setProperty("slicerRole", "secondaryText")
    if selectable:
        label.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
    return label


def _button(text, objectName, primary=False):
    button = qt.QPushButton(text)
    button.setObjectName(objectName)
    button.setProperty("slicerRole", "primary" if primary else "secondary")
    return button


def _title(text, objectName):
    label = qt.QLabel(text)
    label.setObjectName(objectName)
    label.setProperty("slicerRole", "sectionTitle")
    font = qt.QFont(label.font)
    font.setBold(True)
    label.setFont(font)
    return label


class FastIndexDialog(qt.QDialog):
    """Build fast index…: build, parity gate and optional C parity (see the module docstring).

    ``cache`` (atlas cache folder, default: the fetch cache) and ``tilingDir`` (an existing tiling folder, used by the "Existing
    tiling folder" pack) may be set before building.
    """

    def __init__(self, ctx, bundlePath, parent=None):
        qt.QDialog.__init__(self, parent or slicer.util.mainWindow())
        self.ctx = ctx
        self.bundlePath = None
        self.bundle = None
        self.cache = None
        self.tilingDir = None
        self.buildJob = None
        self.parityJob = None
        self.parityResult = None
        self.cResult = None
        self._atlasNotices = []
        self._cohortIds = None
        self._detachedJobs = []  # (job, kind) running or finished while the dialog was closed
        self._timer = qt.QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)
        self._timer.connect("timeout()", self.refresh)
        self.setObjectName("LNMFastIndexDialog")
        self.setWindowTitle(_("Build fast index"))
        self.setMinimumSize(560, 480)
        self.resize(660, 760)
        outer = qt.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 8)
        scroll = qt.QScrollArea()
        scroll.setObjectName("LNMFastIndexScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(qt.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(qt.Qt.ScrollBarAlwaysOff)
        outer.addWidget(scroll, 1)
        content = qt.QWidget()
        content.setObjectName("LNMFastIndexContent")
        scroll.setWidget(content)
        layout = qt.QVBoxLayout(content)
        layout.setSpacing(6)
        layout.addWidget(_label(_("The fast index approximates a lesion's network map from a few hundred parcel-seed maps, so that thousands of null cohorts cost minutes. Its maps are "
                                  "approximate (their own z-scale, rank rules only) and are never final observed maps. Fast mode is offered for your cohort only after the index "
                                  "reproduces your own exact lesion maps (the parity gate below)."), "LNMFastIndexIntro"))
        self.bundleLabel = _label("", "LNMFastIndexBundle", secondary=True, selectable=True)
        layout.addWidget(self.bundleLabel)
        self._buildSection(layout)
        self._paritySection(layout)
        self._matrixSection(layout)
        layout.addStretch(1)
        row = qt.QHBoxLayout()
        row.setContentsMargins(10, 0, 10, 0)
        row.addStretch(1)
        close = _button(_("Close"), "LNMFastIndexClose")
        close.connect("clicked()", self.close)
        row.addWidget(close)
        outer.addLayout(row)
        self.connect("finished(int)", self.onFinished)
        runner = getattr(ctx, "runner", None)
        if runner is not None and hasattr(runner, "jobFinished"):
            runner.jobFinished.append(self._onAnyJobFinished)
        self.setBundle(bundlePath)

    #
    # Build
    #

    def _buildSection(self, layout):
        layout.addWidget(_title(_("1. Build"), "LNMFastIndexBuildTitle"))
        form = qt.QFormLayout()
        form.setFieldGrowthPolicy(qt.QFormLayout.AllNonFixedFieldsGrow)
        self.packCombo = qt.QComboBox()
        self.packCombo.setObjectName("LNMFastIndexPack")
        self.packCombo.addItem(_("Schaefer-400 + Tian S2 + Buckner cerebellum + brain stem + supervoxels"), "standard")
        self.packCombo.addItem(_("Schaefer-1000 + Tian S4 + Buckner cerebellum + brain stem + supervoxels"), "fine")
        self.packCombo.addItem(_("An existing tiling folder (labels.nii.gz and labels.tsv)"), "custom")
        self.packCombo.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        self.packCombo.setToolTip(_("The parcels of the index. Every connectome voxel that no atlas covers (white matter included) belongs to a supervoxel of about 10 mm."))
        self.packCombo.connect("currentIndexChanged(int)", lambda index: self.scheduleRefresh())
        form.addRow(_("Atlas pack"), self.packCombo)
        layout.addLayout(form)
        tilingRow = qt.QHBoxLayout()
        self.tilingButton = _button(_("Choose tiling folder…"), "LNMFastIndexTilingButton")
        self.tilingButton.connect("clicked()", self.chooseTiling)
        tilingRow.addWidget(self.tilingButton)
        self.tilingLabel = _label("", "LNMFastIndexTilingLabel", secondary=True)
        tilingRow.addWidget(self.tilingLabel, 1)
        self.tilingRow = qt.QWidget()
        self.tilingRow.setLayout(tilingRow)
        tilingRow.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tilingRow)
        atlasRow = qt.QHBoxLayout()
        self.atlasStatus = _label("", "LNMFastIndexAtlasStatus", secondary=True)
        atlasRow.addWidget(self.atlasStatus, 1)
        self.getAtlasesButton = _button(_("Get atlases…"), "LNMFastIndexGetAtlases")
        self.getAtlasesButton.setToolTip(_("Download the missing atlases from the open sources of the catalog, after you confirm (nothing is downloaded otherwise)"))
        self.getAtlasesButton.connect("clicked()", self.getAtlases)
        atlasRow.addWidget(self.getAtlasesButton)
        layout.addLayout(atlasRow)
        subjectsForm = qt.QFormLayout()
        subjectsForm.setFieldGrowthPolicy(qt.QFormLayout.AllNonFixedFieldsGrow)
        self.subjectsCombo = qt.QComboBox()
        self.subjectsCombo.setObjectName("LNMFastIndexSubjects")
        self.subjectsCombo.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        self.subjectsCombo.setToolTip(_("The connectome subjects the parcel maps are averaged over. For fast mode the index must use exactly the subjects of the lesion maps."))
        self.subjectsCombo.connect("currentIndexChanged(int)", lambda index: self.scheduleRefresh())
        subjectsForm.addRow(_("Subjects"), self.subjectsCombo)
        layout.addLayout(subjectsForm)
        self.subjectsHint = _label("", "LNMFastIndexSubjectsHint", secondary=True)
        layout.addWidget(self.subjectsHint)
        self.estimateLabel = _label("", "LNMFastIndexEstimate", secondary=True)
        layout.addWidget(self.estimateLabel)
        self.indexLabel = _label("", "LNMFastIndexExisting")
        layout.addWidget(self.indexLabel)
        self.buildReason = _label("", "LNMFastIndexBuildReason", secondary=True)
        layout.addWidget(self.buildReason)
        self.progress = widgets.progressRow(objectNamePrefix="LNMFastIndexBuild")
        self.progress.widget.hide()
        layout.addWidget(self.progress.widget)
        self.statusLabel = _label("", "LNMFastIndexStatus", secondary=True, selectable=True)
        layout.addWidget(self.statusLabel)
        row = qt.QHBoxLayout()
        row.addStretch(1)
        self.buildButton = _button(_("Build index"), "LNMFastIndexBuildButton", primary=True)
        self.buildButton.connect("clicked()", self.startBuild)
        row.addWidget(self.buildButton)
        layout.addLayout(row)

    def _paritySection(self, layout):
        layout.addWidget(_title(_("2. Parity on your lesions"), "LNMFastIndexParityTitle"))
        layout.addWidget(_label(_("The index maps up to 60 of your lesions and the result is compared with their exact maps (the lesion maps of the Cohort card): per location stratum "
                                  "(cortical, subcortical, brain stem, cerebellar, white matter) and size bin, the median partial r (the connectome's mean map and PC1 partialled out) "
                                  "must reach 0.80 and the median top-10% Dice 0.60, with at least 10 lesions. The constants are to be calibrated on GSP1000 and are reported, not promised. "
                                  "Lesions in a failing group are left out of fast-mode statistics."), "LNMFastIndexParityIntro", secondary=True))
        self.paritySource = _label("", "LNMFastIndexParitySource", secondary=True)
        layout.addWidget(self.paritySource)
        row = qt.QHBoxLayout()
        self.gateChip = widgets.chip(_("Not checked"), "neutral")
        self.gateChip.setObjectName("LNMFastIndexGateChip")
        row.addWidget(self.gateChip)
        row.addStretch(1)
        self.parityButton = _button(_("Run parity check"), "LNMFastIndexParityButton", primary=False)
        self.parityButton.connect("clicked()", self.startParity)
        row.addWidget(self.parityButton)
        layout.addLayout(row)
        self.parityReason = _label("", "LNMFastIndexParityReason", secondary=True)
        layout.addWidget(self.parityReason)
        self.parityProgress = widgets.progressRow(objectNamePrefix="LNMFastIndexParity")
        self.parityProgress.widget.hide()
        layout.addWidget(self.parityProgress.widget)
        self.parityTable = widgets.tableView(list(PARITY_COLUMNS))
        self.parityTable.setObjectName("LNMFastIndexParityTable")
        header = self.parityTable.horizontalHeader()
        header.setSectionResizeMode(0, qt.QHeaderView.ResizeToContents)
        for column, tip in enumerate(PARITY_COLUMN_TIPS):
            self.parityTable.horizontalHeaderItem(column).setToolTip(tip)
        self.parityTable.hide()
        layout.addWidget(self.parityTable)
        self.gateLabel = _label("", "LNMFastIndexGateLabel", selectable=True)
        layout.addWidget(self.gateLabel)
        self.coverageLabel = _label("", "LNMFastIndexCoverage", secondary=True)
        layout.addWidget(self.coverageLabel)

    def _matrixSection(self, layout):
        layout.addWidget(_title(_("3. Parcel matrix parity (optional)"), "LNMFastIndexMatrixTitle"))
        layout.addWidget(_label(_("Compares the index's parcel-to-parcel matrix C with the matrix of your own copy of {name} (Schaefer-1000 + Tian S4: build the index with that pack).").format(name=PARCEL_MATRIX_NAME),
                                "LNMFastIndexMatrixIntro", secondary=True))
        self.matrixNotice = _label(C_NOTICE, "LNMFastIndexMatrixNotice", secondary=True)
        layout.addWidget(self.matrixNotice)
        self.matrixAccept = qt.QCheckBox(_("I downloaded this file myself and accept its data use terms"))
        self.matrixAccept.setObjectName("LNMFastIndexMatrixAccept")
        self.matrixAccept.connect("toggled(bool)", lambda checked: self.scheduleRefresh())
        layout.addWidget(self.matrixAccept)
        row = qt.QHBoxLayout()
        row.addStretch(1)
        self.matrixButton = _button(_("Compare with a .mat file…"), "LNMFastIndexMatrixButton")
        self.matrixButton.connect("clicked()", self.chooseMatrix)
        row.addWidget(self.matrixButton)
        layout.addLayout(row)
        self.matrixResult = _label("", "LNMFastIndexMatrixResult", selectable=True)
        layout.addWidget(self.matrixResult)

    #
    # Bundle and cohort
    #

    def setBundle(self, bundlePath):
        from LesionNetworkMappingLib.connectome import ConnectomeBundle

        self.closeBundle()
        self.bundlePath = bundlePath
        if bundlePath:
            try:
                self.bundle = ConnectomeBundle.open(bundlePath)
            except Exception as exc:
                self.bundle = None
                self.bundleLabel.setText(_("The connectome cannot be opened: {error}").format(error=exc))
        else:
            self.bundleLabel.setText(_("Select a connectome first (Connectome card, Manage…)."))
        self.refresh()

    def closeBundle(self):
        bundle, self.bundle = self.bundle, None
        if bundle is not None:
            try:
                bundle.close()
            except Exception:
                logging.debug("Lesion network mapping: the connectome of the fast index dialog could not be closed", exc_info=True)

    def onFinished(self, result=0):
        """The dialog was closed: jobs keep running but no longer call into it; the connectome is closed."""
        self._timer.stop()
        runner = getattr(self.ctx, "runner", None)
        if runner is not None and self._onAnyJobFinished in getattr(runner, "jobFinished", []):
            runner.jobFinished.remove(self._onAnyJobFinished)
        manager = getattr(self.ctx, "lnmAtlasManager", None)
        if manager is not None and self.scheduleRefresh in getattr(manager, "listeners", []):
            manager.listeners.remove(self.scheduleRefresh)
        self._detachedJobs = []
        for job in (self.buildJob, self.parityJob):
            if job is not None and not job.isFinished():
                job.spec.onDone = None
                job.spec.onEvent = None
                self._detachedJobs.append(job)
        self.closeBundle()

    def reattach(self, ctx, bundlePath=None):
        """Bring a dialog that was closed (``onFinished``) while a job ran back to life: connectome, callbacks and the state of its jobs."""
        self.ctx = ctx
        runner = getattr(ctx, "runner", None)
        if runner is not None and hasattr(runner, "jobFinished") and self._onAnyJobFinished not in runner.jobFinished:
            runner.jobFinished.append(self._onAnyJobFinished)
        manager = getattr(ctx, "lnmAtlasManager", None)
        if manager is not None and atlasesBusy(ctx) and self.scheduleRefresh not in manager.listeners:
            manager.listeners.append(self.scheduleRefresh)
        running = any(job is not None and not job.isFinished() for job in (self.buildJob, self.parityJob))
        if self.bundle is None or (not running and bundlePath != self.bundlePath):
            self.setBundle(self.bundlePath if running else bundlePath)  # a running job belongs to the connectome it was started for
        detached, self._detachedJobs = self._detachedJobs, []
        for job in detached:
            isBuild = job is self.buildJob
            if not job.isFinished():
                job.spec.onDone = self.onBuildDone if isBuild else self.onParityDone
                job.spec.onEvent = self.onBuildEvent if isBuild else self.onParityEvent
                (self.progress if isBuild else self.parityProgress).setJob(job)
            elif isBuild:
                self.onBuildDone(job)
            elif job is self.parityJob:
                self.onParityDone(job)
        self.refresh()

    def mapsTsv(self):
        """maps.tsv of the cohort's finished lesion maps (the Cohort card's), or None."""
        path = getattr(self.ctx, "cohortMaps", None)
        if path and os.path.isfile(path) and (_readJson(os.path.join(os.path.dirname(path), "cohortmaps.json")) or {}).get("status") == "done":
            return path
        return None

    def scheduleRefresh(self):
        self._timer.start()

    def _onAnyJobFinished(self, job):
        if getattr(job, "kind", None) in ("fastindex", "fast-parity", "cohortmaps"):
            self.scheduleRefresh()

    #
    # Subjects
    #

    def subjectOptions(self):
        """[(text, key, ids)]: all subjects, the first 100, and the subjects of the lesion maps when they are neither."""
        bundle = self.bundle
        if bundle is None:
            return []
        allIds = list(bundle.subject_ids)
        options = [(_("All {n} subjects").format(n=len(allIds)), "all", allIds)]
        if len(allIds) > FIRST_N:
            options.append((_("The first {n} subjects").format(n=FIRST_N), "first", allIds[:FIRST_N]))
        ids = self._cohortIds
        if ids and ids != allIds and ids != allIds[:FIRST_N]:
            wanted = set(ids)
            if [i for i in allIds if i in wanted] == ids:  # --subject-list returns bundle order
                options.append((_("The {n} subjects of the lesion maps").format(n=len(ids)), "cohort", ids))
        return options

    def selectedSubjects(self):
        """(key, ids) of the chosen subjects."""
        key = self.subjectsCombo.currentData
        for _text, optionKey, ids in self.subjectOptions():
            if optionKey == key:
                return key, ids
        options = self.subjectOptions()
        return (options[0][1], options[0][2]) if options else (None, [])

    def _fillSubjects(self):
        options = self.subjectOptions()
        keys = [key for _text, key, _ids in options]
        current = self.subjectsCombo.currentData
        matching = next((key for _text, key, ids in options if self._cohortIds and ids == self._cohortIds), None)
        wanted = current if current in keys and self.subjectsCombo.count == len(options) else (matching or (keys[0] if keys else None))
        self.subjectsCombo.blockSignals(True)
        try:
            self.subjectsCombo.clear()
            for text, key, _ids in options:
                self.subjectsCombo.addItem(text, key)
            if wanted in keys:
                self.subjectsCombo.setCurrentIndex(keys.index(wanted))
        finally:
            self.subjectsCombo.blockSignals(False)

    #
    # Pack
    #

    def pack(self):
        return self.packCombo.currentData or "standard"

    def packEntries(self):
        """(atlas entries, notices) of the chosen pack; ([], []) for an existing tiling."""
        if self.pack() == "custom":
            return [], []
        return packAtlasIds(self.pack(), self.bundle)

    def atlasIds(self):
        return [entry["id"] for entry in self.packEntries()[0]]

    def chooseTiling(self):
        folder = qt.QFileDialog.getExistingDirectory(self, _("Folder of a tiling (labels.nii.gz, labels.tsv)"), self.tilingDir or os.path.expanduser("~"))
        if folder:
            self.tilingDir = folder
            self.refresh()

    def _tilingParcels(self):
        """Parcels of the chosen tiling folder (the rows of its labels.tsv), or a typical number."""
        try:
            with open(os.path.join(self.tilingDir, "labels.tsv"), encoding="utf-8") as f:
                return max(sum(1 for line in f if line.strip()) - 1, 1)
        except (OSError, TypeError):
            return 300

    def getAtlases(self):
        from . import card_clusters

        ids = missingAtlases(self.atlasIds(), self.cache)
        if not ids:
            return False
        manager = card_clusters.sharedAtlases(self.ctx)
        if not card_clusters.confirmFetch(manager, ids, self):
            return False
        if self.scheduleRefresh not in manager.listeners:
            manager.listeners.append(self.scheduleRefresh)
        return manager.fetch(ids)

    #
    # State
    #

    def refresh(self):
        """Show the state of every control (called when a choice, the study or a job changes)."""
        self._timer.stop()
        bundle = self.bundle
        mapsTsv = self.mapsTsv()
        self._cohortIds = cohortSubjects(mapsTsv) if mapsTsv else None
        self._fillSubjects()
        custom = self.pack() == "custom"
        self.tilingRow.setVisible(custom)
        self.tilingLabel.setText(os.path.basename(os.path.normpath(self.tilingDir)) if self.tilingDir else _("No folder chosen"))
        entries, notices = self.packEntries()
        self._atlasNotices = notices
        missing = missingAtlases([entry["id"] for entry in entries], self.cache) if entries else []
        if custom:
            self.atlasStatus.setText("")
        elif missing:
            self.atlasStatus.setText(_("Not downloaded yet: {names}.").format(names=", ".join(missing)) + (" " + " ".join(notices) if notices else ""))
        else:
            self.atlasStatus.setText(_("The atlases of this pack are on this computer.") + (" " + " ".join(notices) if notices else ""))
        self.atlasStatus.setVisible(bool(self.atlasStatus.text))
        busy = atlasesBusy(self.ctx)
        self.getAtlasesButton.setVisible(bool(missing) and not custom)
        self.getAtlasesButton.setEnabled(not busy)
        if bundle is None:
            self.buildButton.setEnabled(False)
            self.buildReason.setText(_("Select a connectome first (Connectome card, Manage…).") if not self.bundlePath else "")
            self.buildReason.setVisible(bool(self.buildReason.text))
            self.estimateLabel.setText("")
            self.indexLabel.setText("")
            self.subjectsHint.setText("")
            self.bundleLabel.setVisible(bool(self.bundleLabel.text))
            self._refreshParity()
            self._refreshMatrix()
            return
        self.bundleLabel.setText(_("Connectome: {name} · N={n:,} subjects · {v:,} voxels").format(name=bundle.name, n=bundle.n_subjects, v=bundle.n_voxels))
        key, ids = self.selectedSubjects()
        if self._cohortIds:
            self.subjectsHint.setText(_("The lesion maps use {n} subjects.").format(n=len(self._cohortIds)) + ("" if ids == self._cohortIds else " " + _("Fast mode needs an index built from exactly those subjects.")))
        else:
            self.subjectsHint.setText(_("No lesion maps yet: fast mode needs an index built from the subjects of the lesion maps (Cohort card)."))
        existing = self._existingIndex(ids)
        parcels = (existing or {}).get("parcels") or PACK_PARCELS.get(self.pack()) or self._tilingParcels()
        estimate = estimateBuild(bundle, len(ids), parcels)
        self.estimateLabel.setText(_("Estimate{approx}: about {time} · {final} on disk (up to {peak} while building) · about {memory} of memory").format(
            approx="" if existing is None else _(" for a rebuild"), time=_duration(estimate["seconds"]), final=_size(estimate["final_bytes"]), peak=_size(estimate["peak_bytes"]),
            memory=_size(estimate["memory_bytes"])))
        if existing is not None:
            created = (existing.get("created") or "")[:10]
            self.indexLabel.setText(_("An index exists for these subjects: {parcels} parcels, {n} subjects{built}.").format(parcels=existing["parcels"], n=existing["subjects"], built=f", built {created}" if created else ""))
        else:
            self.indexLabel.setText(_("No index for these {n} subjects yet.").format(n=len(ids)))
        reason = self.buildBlocker(estimate, missing, key)
        self.buildReason.setText(reason)
        self.buildReason.setVisible(bool(reason))
        running = self.buildJob is not None and not self.buildJob.isFinished()
        self.buildButton.setEnabled(not reason and not running)
        self.buildButton.setText(_("Rebuild index") if existing is not None else _("Build index"))
        self._refreshParity()
        self._refreshMatrix()

    def _existingIndex(self, ids):
        from LesionNetworkMappingLib import nulls

        try:
            folder = nulls.find_fast_index(self.bundle, ids)
        except (OSError, ValueError):
            return None
        if folder is None:
            return None
        info = indexInfo(folder)
        return None if info is None else {**info, "folder": folder}

    def buildBlocker(self, estimate, missing, subjectsKey):
        """The reason the Build button is disabled, or ""."""
        bundle = self.bundle
        if getattr(bundle, "kind", "timeseries") != "timeseries":
            return _("The fast index needs a time-series connectome.")
        if not os.access(bundle.path, os.W_OK):
            return _("The connectome folder is read-only: the index is stored in it.")
        if self.pack() == "custom" and not (self.tilingDir and os.path.isfile(os.path.join(self.tilingDir, "labels.nii.gz")) and os.path.isfile(os.path.join(self.tilingDir, "labels.tsv"))):
            return _("Choose a tiling folder with labels.nii.gz and labels.tsv.")
        if missing:
            return _("Download the missing atlases first (Get atlases…), or choose another pack.")
        try:
            free = shutil.disk_usage(bundle.path).free
        except OSError:
            free = None
        if free is not None and free < estimate["peak_bytes"]:
            return _("Not enough disk space: about {need} needed, {free} free.").format(need=_size(estimate["peak_bytes"]), free=_size(free))
        if atlasesBusy(self.ctx):
            return _("Atlases are downloading…")
        return ""

    def buildArguments(self):
        """Arguments of ``fastmap build`` for the current choices, or None."""
        if self.bundle is None:
            return None
        args = ["build", "--bundle", self.bundle.path]
        key, ids = self.selectedSubjects()
        if key == "first":
            args += ["--subjects", str(FIRST_N)]
        elif key == "cohort":
            mapsTsv = self.mapsTsv()
            folder = parityFolder(mapsTsv)
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, SUBJECTS_FILE)
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write("participant_id\n" + "".join(f"{i}\n" for i in ids))
            args += ["--subject-list", path]
        if self.pack() == "custom":
            args += ["--tiling", self.tilingDir]
        elif self.pack() == "fine":
            for entry in self.packEntries()[0]:
                args += ["--atlas", f"{entry['id']}:{entry['stratum']}"]
        if self.cache:
            args += ["--cache", self.cache]
        return args

    def startBuild(self):
        args = self.buildArguments()
        if args is None or (self.buildJob is not None and not self.buildJob.isFinished()):
            return None
        self.statusLabel.setText(_("Building the fast index…"))
        self.buildButton.setEnabled(False)
        spec = jobrunner.JobSpec(JOB_MODULE, args, title=_("Fast index"), heavy=True, kind="fastindex", name="fastindex", runRoot=jobrunner.defaultRunRoot(), onEvent=self.onBuildEvent,
                                 onDone=self.onBuildDone, context=self.ctx)
        self.buildJob = self.ctx.submit(spec) if hasattr(self.ctx, "submit") else jobrunner.instance().submit(spec)
        self.progress.setJob(self.buildJob)
        self.progress.widget.show()
        return self.buildJob

    def onBuildEvent(self, event):
        self.progress.update()

    def onBuildDone(self, job):
        self.progress.update()
        if job.succeeded():
            warnings = [event.get("message") for event in job.warnings if event.get("message")]
            self.statusLabel.setText(" ".join([_("The fast index is ready."), *warnings]))
        elif job.state == "cancelled":
            self.statusLabel.setText(_("Stopped: Build index again to resume from the last checkpoint."))
        else:
            self.statusLabel.setText(_("The index could not be built: {reason}").format(reason=job.failureReason))
        self.refresh()

    #
    # Parity
    #

    def cohortIndexFolder(self):
        """Folder of the index for the lesion maps' subjects, or None."""
        from LesionNetworkMappingLib import nulls

        if self.bundle is None or not self._cohortIds:
            return None
        try:
            return nulls.find_fast_index(self.bundle, self._cohortIds)
        except (OSError, ValueError):
            return None

    def parityState(self):
        """(enabled, reason) of the parity button."""
        mapsTsv = self.mapsTsv()
        if self.bundle is None:
            return False, _("Select a connectome first.")
        if mapsTsv is None:
            return False, _("Map the lesions first (Cohort card): the parity check compares the index with their exact maps.")
        if self.parityJob is not None and not self.parityJob.isFinished():
            return False, _("The parity check is running…")
        if self.cohortIndexFolder() is None:
            return False, _("Build the index for the subjects of the lesion maps first ({n} subjects).").format(n=len(self._cohortIds or []))
        from LesionNetworkMappingLib import fastmap

        pairs = parityPairs(mapsTsv)
        if len(pairs) < fastmap.PARITY_MIN_LESIONS:
            return False, _("The gate needs at least {n} exact lesions with their lesion files and AvgFz maps; {have} are available.").format(n=fastmap.PARITY_MIN_LESIONS, have=len(pairs))
        return True, ""

    def _refreshParity(self):
        mapsTsv = self.mapsTsv()
        enabled, reason = self.parityState()
        self.parityButton.setEnabled(enabled)
        self.parityReason.setText(reason)
        self.parityReason.setVisible(bool(reason))
        if mapsTsv is None:
            self.paritySource.setText(_("Exact maps: none yet."))
        else:
            n = len(parityPairs(mapsTsv))
            self.paritySource.setText(_("Exact maps: the {n} lesion maps of the Cohort card that have a lesion file and an AvgFz map (at most {cap} are used).").format(n=n, cap=MAX_PARITY_LESIONS))
        if self.bundle is not None and mapsTsv is not None:
            from LesionNetworkMappingLib import reference

            try:
                missingReference = reference.load_reference(self.bundle) is None
            except Exception:
                missingReference = True
            if missingReference:
                self.parityReason.setText((self.parityReason.text + " " if reason else "") + _("The connectome has no reference maps, so the partial r cannot be computed and no stratum can pass: build them in Manage… → Build reference maps."))
                self.parityReason.show()
        if mapsTsv is not None and self.parityJob is None:
            report = _readJson(parityPath(mapsTsv))
            if report is not None:
                state = fastModeState(self.bundle, mapsTsv)
                self.showParity(report, stale=state.needsParity)
                return
        if mapsTsv is None:
            self.showParity(None)

    def parityArguments(self):
        mapsTsv = self.mapsTsv()
        folder = self.cohortIndexFolder()
        if mapsTsv is None or folder is None:
            return None
        pairs = parityPairs(mapsTsv)
        args = ["parity", "--bundle", self.bundle.path, "--index", folder, "--out", parityFolder(mapsTsv)]
        study = getattr(self.ctx, "study", None)
        if getattr(study, "path", None):
            args += ["--run-root", study.path]
        for lesion, fz in pairs:
            args += ["--pair", lesion, fz]
        return args

    def startParity(self):
        if not self.parityState()[0]:
            self.refresh()
            return None
        args = self.parityArguments()
        if args is None:
            return None
        self.parityResult = None
        spec = jobrunner.JobSpec(JOB_MODULE, args, title=_("Parity of the fast index"), heavy=True, kind="fast-parity", name="fast-parity", runRoot=jobrunner.defaultRunRoot(),
                                 onEvent=self.onParityEvent, onDone=self.onParityDone, context=self.ctx)
        self.parityJob = self.ctx.submit(spec) if hasattr(self.ctx, "submit") else jobrunner.instance().submit(spec)
        self.parityProgress.setJob(self.parityJob)
        self.parityProgress.widget.show()
        self.parityButton.setEnabled(False)
        self.parityReason.setText(_("The parity check is running…"))
        self.parityReason.show()
        return self.parityJob

    def onParityEvent(self, event):
        self.parityProgress.update()

    def onParityDone(self, job):
        self.parityProgress.update()
        self.parityJob = None
        if not job.succeeded():
            self.parityReason.setText(_("Stopped.") if job.state == "cancelled" else _("The parity check failed: {reason}").format(reason=job.failureReason))
            self.parityReason.show()
            self._refreshParity()
            return
        self.refresh()

    def showParity(self, report, stale=False):
        """Fill the table, the verdict and the coverage line from a parity report (None clears them)."""
        self.parityResult = report
        table = self.parityTable
        table.setRowCount(0)
        if report is None:
            table.hide()
            widgets.setChipLevel(self.gateChip, "neutral", _("Not checked"))
            self.gateLabel.setText("")
            self.coverageLabel.setText("")
            return
        from LesionNetworkMappingLib import fastmap

        gate = report.get("gate") or fastmap.gate(report)
        rows = []
        for group, names in (("strata", fastmap.STRATA), ("size_bins", fastmap.SIZE_BIN_NAMES)):
            for name in names:
                verdict = (gate.get(group) or {}).get(name)
                if verdict is not None:
                    rows.append((STRATUM_TEXT.get(name, name) if group == "strata" else _("size {size}").format(size=SIZE_BIN_TEXT.get(name, name)), verdict))
        table.setRowCount(len(rows))
        for index, (name, verdict) in enumerate(rows):
            cells = (name, str(verdict.get("n")), _number(verdict.get("median_partial_r")), _number(verdict.get("median_top10_dice")), _("Pass") if verdict.get("passed") else _("Fail"))
            for column, text in enumerate(cells):
                item = qt.QTableWidgetItem(text)
                item.setToolTip(str(verdict.get("reason") or ""))
                table.setItem(index, column, item)
        table.show()
        _fitTable(table, len(rows))
        constants = gate.get("constants") or {}
        total = len(report.get("lesions") or [])
        allowed = total - len(gate.get("excluded") or [])
        passed = bool(gate.get("enough_lesions") and gate.get("allowed_strata") and allowed >= 2)
        if stale:
            widgets.setChipLevel(self.gateChip, "warn", _("Out of date"))
        else:
            widgets.setChipLevel(self.gateChip, "ok" if passed else "danger", _("Gate passed") if passed else _("Gate failed"))
        failing = failingText(gate)
        lines = [_("Gate: median partial r ≥ {r:g} and median top-10% Dice ≥ {d:g}, per stratum and size bin, at least {n} exact lesions ({have} checked).").format(
            r=constants.get("partial_r", 0.0), d=constants.get("top10_dice", 0.0), n=gate.get("min_lesions"), have=total)]
        if gate.get("allowed_strata"):
            lines.append(_("Passed: {names}. {allowed} of {total} checked lesions are allowed in fast mode.").format(names=", ".join(gate["allowed_strata"]), allowed=allowed, total=total))
        if failing:
            lines.append(_("Failed: {failing}. Lesions in these groups are left out of fast-mode statistics.").format(failing=failing))
        lines += [message for message in gate.get("messages") or [] if message not in " ".join(lines)][:3]
        if stale:
            lines.append(_("This report is out of date (another index or newer lesion maps): run the parity check again."))
        self.gateLabel.setText("\n".join(lines))
        covers = [lesion.get("coverage") for lesion in report.get("lesions") or [] if isinstance(lesion.get("coverage"), dict)]
        atlas = sorted(c["atlas"] for c in covers if c.get("atlas") is not None)
        if atlas:
            median = atlas[len(atlas) // 2]
            self.coverageLabel.setText(_("Per lesion, the share of its occupancy in atlas parcels (the rest in supervoxels): median {median:.0%}, range {low:.0%} to {high:.0%}.").format(median=median, low=atlas[0], high=atlas[-1]))
        else:
            self.coverageLabel.setText("")

    #
    # C parity
    #

    def _refreshMatrix(self):
        folder = self.cohortIndexFolder() or (self._existingIndex(self.selectedSubjects()[1]) or {}).get("folder")
        enabled = bool(folder) and self.matrixAccept.checked
        self.matrixButton.setEnabled(enabled)
        if not folder:
            self.matrixButton.setToolTip(_("Build an index first"))
        elif not self.matrixAccept.checked:
            self.matrixButton.setToolTip(_("Confirm the data use terms first"))
        else:
            self.matrixButton.setToolTip("")

    def chooseMatrix(self):
        path = qt.QFileDialog.getOpenFileName(self, _("Parcel matrix (.mat) of your own copy of the release"), os.path.expanduser("~"), "MATLAB (*.mat);;All files (*)")
        if path:
            return self.compareMatrix(path)
        return None

    def compareMatrix(self, matPath):
        """Compare the index's matrix C with the parcel matrix in ``matPath``; return the result dict (or None, with the reason shown)."""
        import numpy as np

        from LesionNetworkMappingLib import fastmap, jobs

        folder = self.cohortIndexFolder() or (self._existingIndex(self.selectedSubjects()[1]) or {}).get("folder")
        if not folder:
            self.matrixResult.setText(_("Build an index first."))
            return None
        try:
            imported = fastmap.import_parcel_matrix(matPath)
            rows = fastmap.read_labels_tsv(os.path.join(folder, fastmap.LABELS_TSV))
            order = [i for i, row in enumerate(rows) if str(row["source"]).startswith(("schaefer2018-1000", "tian-s4"))]
            size = imported["matrix"].shape[0]
            if len(order) != size:
                self.matrixResult.setText(_("The matrix has {size} parcels but this index has {have} Schaefer-1000 and Tian S4 parcels: build the index with the Schaefer-1000 + Tian S4 pack to compare.").format(size=size, have=len(order)))
                return None
            comparison = fastmap.compare_parcel_matrices(imported["matrix"], np.asarray(np.load(os.path.join(folder, fastmap.C_NAME), mmap_mode="r")), order=order)
        except (OSError, ValueError, KeyError) as exc:
            self.matrixResult.setText(_("The comparison failed: {error}").format(error=exc))
            return None
        record = {"format": C_PARITY_FORMAT, "version": 1, "file": imported["file"], "file_sha256": imported["sha256"], "variable": imported["variable"], "terms": imported["terms"],
                  "acknowledgement": imported["acknowledgement"], "use": imported["use"], "n_parcels": size, "index_sha256": (indexInfo(folder) or {}).get("sha256"), **comparison}
        self.cResult = record
        try:
            jobs.write_json_atomic(os.path.join(folder, C_PARITY_JSON), record)
        except OSError:
            logging.debug("Lesion network mapping: c_parity.json could not be written", exc_info=True)
        self.matrixResult.setText(_("C parity ({terms}): Pearson r = {r:.4f}, Spearman rho = {rho:.4f} over {pairs:,} parcel pairs ({size} parcels). Compared for this check only; the file was not copied.").format(
            terms=imported["terms"], r=comparison["r"] if comparison["r"] is not None else float("nan"), rho=comparison["rho"] if comparison["rho"] is not None else float("nan"), pairs=comparison["n_pairs"], size=size))
        return record


def atlasesBusy(ctx):
    """Whether atlases are being downloaded by the shared atlas manager of the Map context."""
    manager = getattr(ctx, "lnmAtlasManager", None)
    return bool(manager is not None and hasattr(manager, "busy") and manager.busy())


#
# Action
#


def openFastIndex(ctx, bundlePath=None):
    """Show the fast index dialog for ``bundlePath`` (one per application; reused while a job runs, else rebuilt for the connectome)."""
    dialog = _dialogs.get("fastindex")
    if dialog is not None:
        running = any(job is not None and not job.isFinished() for job in (dialog.buildJob, dialog.parityJob))
        if running or (dialog.bundlePath == bundlePath and dialog.isVisible()):
            dialog.reattach(ctx, bundlePath)
            dialog.show()
            dialog.raise_()
            return dialog
        dialog.close()
        dialog.deleteLater()
    dialog = FastIndexDialog(ctx, bundlePath)
    _dialogs["fastindex"] = dialog
    dialog.show()
    dialog.raise_()
    return dialog


ACTIONS = [
    {"id": "fastIndex", "label": _("Build fast index…"), "order": 70, "factory": openFastIndex},
]

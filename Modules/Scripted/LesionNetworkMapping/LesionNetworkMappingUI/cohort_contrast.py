"""Contrast tab of the Cohort card (order 20): the GLM specificity analysis (SPEC_LNM_ADDENDUM §3.4, §7.1).

The tab tests a group contrast of the cohort's per-lesion maps with ``LesionNetworkMappingLib.glm`` (Freedman-Lane
permutation inference, voxel max-stat FWE primary, BH-FDR secondary, optional TFCE) in the job runner, and shows
every decision of the design before it runs:

- the group column (``Study.group_columns()``) and its levels A and B, the contrast (A > B, B > A, two-sided, F);
- covariate chips (``Study.covariate_columns()``): lesion volume is on, and turning it off needs a reason, which
  is recorded as ``DesignSpec.volume_off_reason`` (provenance and methods);
- "Permute": the decision of ``design.build_design`` and its reason (for example "Free exchange: group is nested in
  dataset (cases vs control database); limitations recorded"), with a manual override;
- the input (AvgFz or T maps), the inference (FWE, FDR, TFCE with its estimated time from ``tfce.tfce_eta``) and the
  number of permutations (5000; fewer than 1000 is a preview);
- "Design preview…": a popover with the design matrix as a heat map (painted with numpy into a QImage), the rank,
  VIF, unique permutations, the smallest attainable p, the design type, the checks (blocking in red, warnings in
  amber) and the limitations. A blocking check disables Run.

Run maps the lesions first when the cohort has no current maps.tsv (the Compute card's cohort maps job), then runs
the GLM into ``<study>/derivatives/lnm/conn-<label>[_n-N]/glm/<contrast>``; the job registers its run. The T, FWE p
and Hedges g maps are then listed (one shown at a time in the Cohort card's group map volume, with its own copy of
the module colour table), with glm.json's warnings, the smallest attainable p, the FDR floor and the limitations.
No parametric p is ever shown: every p-value comes from permutations.

After the analysis plan is locked (Recipe header, card_recipe.py), the permutations, covariates, input, FDR and TFCE
of this tab (and the permutations and covariates of the Symptom tab) are compared with the plan
(``recipes.analysis_pending``): a change is shown in the header's reason row and Run waits until its reason is
recorded as a deviation ("contrast.n_perm"), or until "Use the plan's settings" sets the tab back.

"Export for PALM…" writes the design and the maps with ``palm.export_palm`` into ``<study>/derivatives/lnm/palm/<run>``
and validates the files with ``palm.validate_palm_export``; it runs as a job of this module (below), because loading
the n maps and writing the 4D image would block the application. "Import PALM results…" reads PALM's outputs back
(``palm.import_palm``) and shows them. The tab works in Classic and without the Studio shell.

Command line of the PALM export job (JSON events of ``LesionNetworkMappingLib.jobs``; Qt is not imported)::

    python -m LesionNetworkMappingUI.cohort_contrast --maps maps.tsv --participants participants.tsv
        --design design.json --mask mask.nii.gz --out DIR [--perm N] [--threads K] [--cancel-file F]
        [--run-root DIR] [--parent-run ID]
"""

import json
import os
import sys
import time

#: Run kind and lock of the PALM export job
PALM_JOB = "palmexport"
PALM_LOCK = ".palm-export.lock"
PALM_MEMMAP = ".palm-maps.npy"


def _palmParser():
    from LesionNetworkMappingLib import jobs

    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingUI.cohort_contrast", description="Export a GLM design and its per-lesion maps for PALM and FSL randomise.")
    parser.add_argument("--maps", required=True, help="maps.tsv of a cohortmaps run (participant_id, AvgFz, T)")
    parser.add_argument("--participants", required=True, help="participants.tsv of the study")
    parser.add_argument("--design", required=True, help="design.json (the spec)")
    parser.add_argument("--mask", required=True, help="connectome mask (the maps' grid)")
    parser.add_argument("--out", required=True, help="export folder")
    parser.add_argument("--perm", type=jobs.positive_int, default=None, help="permutations of run_palm.sh (default: the design's; at least 1000)")
    jobs.add_common_arguments(parser)
    return parser


def palmExportMain(argv=None):
    """The PALM export job: the design as glm.py builds it, the maps loaded in design order, ``palm.export_palm`` and
    ``palm.validate_palm_export``. A blocked design or an export that does not validate is an error (exit code 2 or 1).
    """
    from LesionNetworkMappingLib import design as designlib
    from LesionNetworkMappingLib import glm, jobs, nifti, palm, runs
    from LesionNetworkMappingLib.export import now_iso
    from LesionNetworkMappingLib.geometry import mask_indices

    import numpy as np

    argv = list(sys.argv[1:] if argv is None else argv)
    args = _palmParser().parse_args(argv)
    emit = jobs.emit
    cancel = jobs.CancelToken(args.cancel_file)
    started = time.monotonic()
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    lock = jobs.OutputLock(os.path.join(out, PALM_LOCK))
    lock.acquire()
    memmap = os.path.join(out, PALM_MEMMAP)
    runRoot, runId, Y = None, None, None
    try:
        study, inStudy = glm._load_study(args.participants)
        try:
            spec = designlib.DesignSpec.load(args.design)
        except (OSError, ValueError) as exc:
            raise jobs.JobError(f"The design {args.design} cannot be read: {exc}", code="usage", exit_code=jobs.EXIT_USAGE) from None
        if args.perm is not None and args.perm >= designlib.MIN_N_PERM:
            spec.inference = {**spec.inference, "n_perm": int(args.perm)}
            spec.validate()
        mapsTsv = os.path.abspath(args.maps)
        mapped = glm._map_rows(mapsTsv, glm.MAP_COLUMNS[spec.input])
        cohort = glm._read_json(os.path.join(os.path.dirname(mapsTsv), "cohortmaps.json"))
        withMaps = {sid for sid in study.subject_ids if sid in mapped}
        if not withMaps:
            raise jobs.JobError(f"No subject of {args.participants} has a map in {mapsTsv}", code="design_blocked", exit_code=jobs.EXIT_USAGE)
        subset, _filled = glm._subset_study(study, withMaps, {sid: values[1] for sid, values in mapped.items()})
        try:
            design = designlib.build_design(subset, spec)
        except designlib.DesignError as exc:
            raise jobs.JobError(f"The design cannot be built: {exc}", code="design_blocked", exit_code=jobs.EXIT_USAGE) from None
        pooling = glm._pooling_checks(design, mapped, cohort)
        if pooling:
            design.checks = designlib._ordered([*pooling, *design.checks])
        if design.blocking:
            raise jobs.JobError("The design is blocked: " + " | ".join(check.message for check in design.blocking), code="design_blocked", exit_code=jobs.EXIT_USAGE,
                                checks=[check.to_dict() for check in design.blocking])
        runRoot = os.path.abspath(args.run_root or (study.path if inStudy else out))
        parents = [run for run in [(cohort or {}).get("run_id"), *args.parent_run] if run]
        params = {"format": palm.EXPORT_FORMAT, "design": runs.params_sha256(design.to_dict(matrix=True)), "n": design.n, "n_perm": spec.n_perm, "input": spec.input}
        runId = runs.register(runRoot, "export", out, parents=parents, params=params)
        emit("start", job=PALM_JOB, argv=argv, threads=args.threads, run_id=runId, n=design.n, design_type=design.design_type)
        emit("stage", name="maps", index=1, total=3)
        maskData, maskAffine, maskHeader = nifti.read_nifti(args.mask)
        maskShape = tuple(maskHeader["shape"][:3])
        indices = mask_indices(np.asarray(maskData).reshape(maskShape) != 0)
        Y = glm._load_maps([mapped[sid][0] for sid in design.sids], maskShape, maskAffine, indices, memmap, emit)
        cancel.raise_if_cancelled(done=0)
        emit("stage", name="write", index=2, total=3)
        paths = palm.export_palm(design, Y, args.mask, out, spec=spec)
        cancel.raise_if_cancelled(done=0)
        emit("stage", name="validate", index=3, total=3)
        checks = palm.validate_palm_export(out)
        blocking = [check for check in checks if check.level == "block"]
        if blocking:
            raise jobs.JobError("The PALM export does not validate: " + " | ".join(check.message for check in blocking), code="export_invalid", checks=[check.to_dict() for check in blocking])
        for check in checks:
            if check.level == "warn":
                emit("warning", code=check.code, message=check.message)
        outputs = list(paths.values())
        runs.update(runRoot, runId, status="done", ended=now_iso(), outputs=[os.path.basename(path) for path in outputs],
                    summary={"n": design.n, "design_type": design.design_type, "checks": len(checks)})
        for path in outputs:
            emit("result", kind="palm_export", path=path)
        emit("done", outputs=outputs, folder=out, run_id=runId, n=design.n, checks=[check.to_dict() for check in checks], elapsed=round(time.monotonic() - started, 3))
        return jobs.EXIT_OK
    except jobs.Cancelled:
        if runId:
            runs.update(runRoot, runId, status="cancelled", ended=now_iso())
        raise
    except BaseException as exc:
        if runId:
            try:
                runs.update(runRoot, runId, status="failed", ended=now_iso(), error=f"{type(exc).__name__}: {exc}")
            except Exception:
                pass
        raise
    finally:
        del Y
        if os.path.exists(memmap):
            try:
                os.remove(memmap)
            except OSError:
                pass
        lock.release()


if __name__ == "__main__":
    from LesionNetworkMappingLib.jobs import run_job, set_blas_threads

    set_blas_threads(sys.argv[1:])  # before numpy is imported
    run_job(palmExportMain, job=PALM_JOB)


# Everything below needs the Slicer application (Qt); the job above never reaches it.

import html
import logging
import math

import qt
import slicer
from slicer.i18n import tr as _

from . import widgets

#: Order of the tab in the Cohort card (Overlap 10, Contrast 20, Symptom 30, VLSM 40)
ORDER_CONTRAST = 20
#: Node attribute of the Cohort card's group map volume (card_cohort.GROUP_VOLUME_ATTRIBUTE)
GROUP_VOLUME_ATTRIBUTE = "LNM.GroupMapVolume"
#: Delay of the design rebuild after a control changed (several changes give one rebuild)
DESIGN_DELAY_MS = 150
#: Delay between loading the template and showing it under a map (two short GUI passes instead of one long one)
TEMPLATE_LAYER_DELAY_MS = 30
#: Below this many permutations a run is a preview (design.MIN_N_PERM)
PREVIEW_PERM = 1000
DEFAULT_PERM = 5000
TFCE_SUGGESTED_PERM = 1000
DESIGN_TYPE_TEXT = {
    "within_cohort": "Within cohort: every dataset holds both levels (symptom-anchored case-control)",
    "cases_vs_control_database": "Cases vs a control database: the group is nested in dataset or source",
    "continuous": "Continuous score (symptom network mapping)",
    "one_sample": "One sample (no comparison group)",
}
#: How the GLM outputs are listed (glm.json "outputs" keys)
GLM_OUTPUT_LABELS = (
    ("stat", "Statistic (T)"),
    ("p_fwe", "FWE p (max-statistic, permutation)"),
    ("effect", "Effect size"),
    ("p_fdr", "FDR q (BH, secondary)"),
    ("tfce_p_fwe", "TFCE FWE p (permutation)"),
    ("p_unc", "Uncorrected p (permutation)"),
)
PERMUTE_CHOICES = (("auto", "Automatic"), ("within", "Within blocks"), ("whole", "Whole blocks"), ("free", "Free exchange"))
CONTRASTS = ("A>B", "B>A", "two_sided", "F")
INPUTS = ("Fz", "T")


#
# Helpers shared with cohort_symptom.py
#


def _mapWidget():
    return getattr(slicer.modules, "LesionNetworkMappingWidget", None)


def _readJson(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _stat(path):
    try:
        info = os.stat(path)
    except (OSError, TypeError):
        return None
    return (info.st_mtime_ns, info.st_size)


def label(text, objectName, role=None, wrap=True):
    """A word-wrapped label that never widens the inspector."""
    widget = qt.QLabel(text)
    widget.setObjectName(objectName)
    widget.setWordWrap(wrap)
    if role:
        widget.setProperty("slicerRole", role)
    widget.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
    widget.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
    return widget


def levelColor(level):
    """Text colour name of a check level: "block" red, "warn" amber, other levels the secondary text colour."""
    chipLevel = {"block": "danger", "warn": "warn"}.get(level)
    ui = widgets._studioUi()
    if chipLevel is None:
        if ui is not None and hasattr(ui, "token"):
            try:
                return ui.token("text2").name()
            except Exception:
                pass
        return slicer.app.palette().color(qt.QPalette.Active, qt.QPalette.WindowText).name()
    if ui is not None and hasattr(ui, "token"):
        try:
            return ui.token({"danger": "danger", "warn": "warning"}[chipLevel]).name()
        except Exception:
            pass
    return widgets._paletteChipColors(chipLevel)[1]


def checksHtml(checks, levels=("block", "warn", "info")):
    """Rich text of design checks (dicts or ``design.Check``): one coloured line each, blocking first."""
    lines = []
    for check in checks:
        level = check.get("level") if isinstance(check, dict) else check.level
        message = check.get("message") if isinstance(check, dict) else check.message
        if level not in levels:
            continue
        mark = {"block": "✕", "warn": "!"}.get(level, "·")  # noqa: RUF001
        lines.append(f'<div style="color:{levelColor(level)}">{mark} {html.escape(str(message))}</div>')
    return "".join(lines)


def disclaimerCaption(objectName):
    """The research-only disclaimer as a caption under an output panel (``export.DISCLAIMER``).

    Its objectName is the panel's own (the Map footer keeps the single ``LNMDisclaimer`` the tour points at).
    """
    from LesionNetworkMappingLib import export

    caption = label(export.DISCLAIMER, objectName, "caption")
    widgets._font(caption, 11)
    ui = widgets._studioUi()
    if ui is not None and hasattr(ui, "bindStyleSheet"):
        ui.bindStyleSheet(caption, lambda: f"QLabel#{objectName} {{ color: {ui.token('text3').name()}; }}")
    else:
        palette = qt.QPalette(caption.palette)
        palette.setColor(qt.QPalette.WindowText, slicer.app.palette().color(qt.QPalette.Disabled, qt.QPalette.WindowText))
        caption.setPalette(palette)
    return caption


def duration(seconds):
    """A duration as "< 1 min", "43 min" or "3 h 36 min"."""
    if seconds is None or not math.isfinite(seconds):
        return "?"
    if seconds < 60:
        return "< 1 min"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def designMatrixImage(X, maxWidth=300, maxHeight=220):
    """The design matrix as a heat map (``qt.QImage``): each column scaled by its largest |value|, negative blue,
    zero light grey, positive red. Painted with numpy and read from PPM bytes (Qt 5 and Qt 6).
    """
    import numpy as np

    X = np.nan_to_num(np.asarray(X, dtype=np.float64), nan=0.0, posinf=0.0, neginf=0.0)
    if X.ndim != 2 or X.size == 0:
        return qt.QImage()
    n, p = X.shape
    if n > maxHeight:  # one pixel row per sampled subject
        X = X[np.linspace(0, n - 1, maxHeight).round().astype(int)]
        n = X.shape[0]
    scale = np.abs(X).max(axis=0)
    scale[scale == 0] = 1.0
    values = np.clip(X / scale, -1.0, 1.0)[..., None]
    middle, positive, negative = np.array([236.0, 236.0, 236.0]), np.array([200.0, 60.0, 45.0]), np.array([45.0, 105.0, 190.0])
    rgb = np.where(values >= 0, middle + (positive - middle) * values, middle + (negative - middle) * -values)
    rgb = np.round(rgb).astype(np.uint8)
    cellWidth = max(6, min(56, maxWidth // max(p, 1)))
    cellHeight = max(1, min(14, maxHeight // max(n, 1)))
    image = np.repeat(np.repeat(rgb, cellHeight, axis=0), cellWidth, axis=1)
    if cellWidth >= 8:  # a 1 px gap between columns
        for column in range(1, p):
            image[:, column * cellWidth] = 128
    height, width = image.shape[:2]
    data = b"P6\n%d %d\n255\n" % (width, height) + np.ascontiguousarray(image).tobytes()
    return qt.QImage.fromData(qt.QByteArray(data), "PPM")


class ShowFilter(qt.QObject):
    """Calls ``callback`` soon after the watched widget is shown (a tab becoming current, the card expanded)."""

    def __init__(self, widget, callback):
        qt.QObject.__init__(self, widget)
        self.callback = callback
        widget.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.Show:
            qt.QTimer.singleShot(0, self.callback)
        return False


class DesignPreview:
    """The design preview popover: design type, the matrix heat map, the numbers, the checks and the limitations."""

    def __init__(self, parent, objectName="LNMDesignPreview"):
        self.frame = qt.QFrame(parent, qt.Qt.Popup)
        self.frame.setObjectName(objectName)
        self.frame.setFrameShape(qt.QFrame.StyledPanel)
        self.frame.setFixedWidth(400)
        layout = qt.QVBoxLayout(self.frame)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)
        self.title = label(_("Design preview"), f"{objectName}Title")
        widgets._font(self.title, 13, bold=True)
        layout.addWidget(self.title)
        self.typeLabel = label("", f"{objectName}Type")
        layout.addWidget(self.typeLabel)
        self.image = qt.QLabel()
        self.image.setObjectName(f"{objectName}Matrix")
        self.image.setToolTip(_("Design matrix: one row per subject, one column per regressor (each column scaled to its largest |value|; red positive, blue negative)"))
        layout.addWidget(self.image)
        self.columnsLabel = label("", f"{objectName}Columns", "secondaryText")
        layout.addWidget(self.columnsLabel)
        self.factsLabel = label("", f"{objectName}Facts")
        self.factsLabel.setTextFormat(qt.Qt.RichText)
        layout.addWidget(self.factsLabel)
        self.checksLabel = label("", f"{objectName}Checks")
        self.checksLabel.setTextFormat(qt.Qt.RichText)
        layout.addWidget(self.checksLabel)
        self.limitationsLabel = label("", f"{objectName}Limitations", "secondaryText")
        self.limitationsLabel.setTextFormat(qt.Qt.RichText)
        layout.addWidget(self.limitationsLabel)
        self.noteLabel = label("", f"{objectName}Note", "caption")
        layout.addWidget(self.noteLabel)
        for widget in (self.title, self.typeLabel, self.columnsLabel, self.factsLabel, self.checksLabel, self.limitationsLabel, self.noteLabel):
            widget.setSizePolicy(qt.QSizePolicy.Preferred, qt.QSizePolicy.Preferred)  # a fixed-width popup: heights follow the wrapped text

    def setDesign(self, design, note="", error=None, title=None):
        """Show a ``design.Design`` (or the error that kept it from being built)."""
        self.title.setText(title or _("Design preview"))
        self.noteLabel.setText(note or "")
        self.noteLabel.setVisible(bool(note))
        if design is None:
            self.typeLabel.setText(error or _("No design yet."))
            self.image.clear()
            for widget in (self.image, self.columnsLabel, self.factsLabel, self.checksLabel, self.limitationsLabel):
                widget.hide()
            return
        self.typeLabel.setText(_("Design type: {text}").format(text=_(DESIGN_TYPE_TEXT.get(design.design_type, design.design_type))))
        self.image.setPixmap(qt.QPixmap.fromImage(designMatrixImage(design.X)))
        self.image.show()
        self.columnsLabel.setText(_("Columns: {columns}").format(columns=" · ".join(str(column) for column in design.columns)))
        self.columnsLabel.show()
        self.factsLabel.setText(self.factsHtml(design))
        self.factsLabel.show()
        self.checksLabel.setText(checksHtml(design.checks))
        self.checksLabel.setVisible(bool(design.checks))
        limitations = list(design.limitations)
        self.limitationsLabel.setText(_("Limitations (written to the methods):") + "".join(f"<div>· {html.escape(text)}</div>" for text in limitations))
        self.limitationsLabel.setVisible(bool(limitations))

    @staticmethod
    def factsHtml(design):
        rows = []
        perLevel = " · ".join(f"{html.escape(str(level))} {count}" for level, count in design.n_per_level.items()) if design.n_per_level else ""
        excluded = sum(len(sids) for sids in design.excluded.values())
        subjects = _("n = {n}").format(n=design.n) + (f" ({perLevel})" if perLevel and list(design.n_per_level) != ["all"] else "")
        if excluded:
            subjects += " · " + _("{n} excluded listwise").format(n=excluded)
        rows.append((_("Subjects"), subjects))
        rows.append((_("Rank"), _("{rank} of {p} columns").format(rank=design.rank, p=design.p)))
        vif = [(name, value) for name, value in design.vif.items() if value is not None]
        if vif:
            rows.append((_("VIF"), " · ".join(f"{html.escape(str(name))} {value:.2f}" if math.isfinite(value) else f"{html.escape(str(name))} ∞" for name, value in vif)))
        unique = design.unique_perms
        uniqueText = f"{unique:,}" if unique < 10**12 else f"10^{math.log10(unique):.1f}"
        rows.append((_("Unique permutations"), uniqueText))
        run = _("{n} (exhaustive)").format(n=f"{design.n_perm:,}") if design.exhaustive else f"{design.n_perm:,}"
        if design.n_perm < PREVIEW_PERM and not design.exhaustive:
            run += " · " + _("preview")
        rows.append((_("Permutations run"), run))
        rows.append((_("Smallest attainable p"), f"{design.min_p:.2g}"))
        rows.append((_("Permutation"), html.escape(design.permute_reason)))
        return "<table cellspacing=2>" + "".join(f"<tr><td><b>{html.escape(name)}</b>&nbsp;</td><td>{value}</td></tr>" for name, value in rows) + "</table>"

    def showAt(self, anchor):
        self.frame.adjustSize()
        point = anchor.mapToGlobal(qt.QPoint(0, anchor.height))
        screen = qt.QGuiApplication.screenAt(point) if hasattr(qt.QGuiApplication, "screenAt") else None
        if screen is not None:
            area = screen.availableGeometry  # a property in PythonQt
            area = area() if callable(area) else area
            x = min(max(point.x(), area.left()), area.right() - self.frame.width)
            y = point.y() if point.y() + self.frame.height <= area.bottom() else max(area.top(), point.y() - anchor.height - self.frame.height)
            point = qt.QPoint(x, y)
        self.frame.move(point)
        self.frame.show()

    def close(self):
        self.frame.close()


#
# Tab base
#


class CohortTab:
    """What the Contrast and Symptom tabs share: the cohort's maps and study, the design rebuilt (debounced) when a
    control changes, the chained cohort maps job, the analysis job and its progress row, the maps table and the
    group map volume. Subclasses set ``PREFIX`` (objectName prefix) and implement ``buildControls``, ``buildDesign``,
    ``jobSpec`` and ``showResult``.
    """

    PREFIX = "LNMCohortTab"
    TITLE = "Analysis"
    #: recipes.ANALYSIS_SCOPES key of the analysis whose settings (``planValues``) follow the locked analysis plan: a
    #: change from the plan is shown in the Recipe header and Run waits for its reason (L-39); None: no plan parameters
    PLAN_SCOPE = None

    def __init__(self, ctx):
        self.ctx = ctx
        self.job = None
        self.mapsJob = None  # the cohort maps job a Run waits for
        self.jobFolder = None  # the output folder of the analysis job (its logs go to the parent folder's logs/)
        self._jobFolder = None
        self.jobScope = None  # (connectomeDir(), study path, study name) when the analysis job started: its result belongs there
        self.design = None
        self.designError = None
        self.designNote = ""
        self.designMapped = None
        self._studyCache = (None, None)
        self.outputs = []  # [(desc, label, path)]
        self.result = None  # (folder, record) of the shown analysis
        self._resultScope = None
        self._cleanedUp = False
        self._planShown = []  # the plan changes the Recipe header was last asked to show
        self.widget = qt.QWidget()
        self.widget.setObjectName(f"{self.PREFIX}Tab")
        self.layout = qt.QVBoxLayout(self.widget)
        self.layout.setSpacing(6)
        self.statusLabel = label("", f"{self.PREFIX}Status", "secondaryText")
        self.layout.addWidget(self.statusLabel)
        self.buildControls(self.layout)
        self.preview = DesignPreview(self.widget, f"{self.PREFIX}DesignPreview")
        buttons = qt.QHBoxLayout()
        self.previewButton = qt.QPushButton(_("Design preview…"))
        self.previewButton.setObjectName(f"{self.PREFIX}PreviewButton")
        self.previewButton.setProperty("slicerRole", "ghost")
        self.previewButton.setToolTip(_("The design matrix, rank, VIF, permutations, checks and limitations of this analysis"))
        self.previewButton.connect("clicked()", self.showPreview)
        buttons.addWidget(self.previewButton)
        buttons.addStretch(1)
        self.runButton = qt.QPushButton(self.runText())
        self.runButton.setObjectName(f"{self.PREFIX}RunButton")
        self.runButton.setProperty("slicerRole", "secondary")
        self.runButton.connect("clicked()", self.run)
        buttons.addWidget(self.runButton)
        self.layout.addLayout(buttons)
        self.checksLabel = label("", f"{self.PREFIX}Checks")
        self.checksLabel.setTextFormat(qt.Qt.RichText)
        self.layout.addWidget(self.checksLabel)
        self.reasonLabel = label("", f"{self.PREFIX}Reason", "secondaryText")
        self.layout.addWidget(self.reasonLabel)
        self.progress = widgets.progressRow(objectNamePrefix=self.PREFIX)
        self.progress.label.setWordWrap(False)
        self.progress.label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        self.progress.widget.hide()
        self.layout.addWidget(self.progress.widget)
        self.results = qt.QWidget()
        self.results.setObjectName(f"{self.PREFIX}Results")
        self.resultsLayout = qt.QVBoxLayout(self.results)
        self.resultsLayout.setContentsMargins(0, 4, 0, 0)
        self.resultsLayout.setSpacing(6)
        self.summaryLabel = label("", f"{self.PREFIX}Summary")
        self.summaryLabel.setTextFormat(qt.Qt.RichText)
        self.resultsLayout.addWidget(self.summaryLabel)
        self.mapsTable = widgets.tableView([_("Map"), _("File")])
        self.mapsTable.setObjectName(f"{self.PREFIX}MapsTable")
        self.mapsTable.setToolTip(_("Click a map to show it"))
        self.mapsTable.setMaximumHeight(190)
        self.mapsTable.connect("cellClicked(int,int)", self.onMapClicked)
        self.resultsLayout.addWidget(self.mapsTable)
        self.buildResults(self.resultsLayout)
        self.notesLabel = label("", f"{self.PREFIX}Notes")
        self.notesLabel.setTextFormat(qt.Qt.RichText)
        self.resultsLayout.addWidget(self.notesLabel)
        folderRow = qt.QHBoxLayout()
        self.folderButton = qt.QPushButton(_("Show folder"))
        self.folderButton.setObjectName(f"{self.PREFIX}ShowFolderButton")
        self.folderButton.setProperty("slicerRole", "ghost")
        self.folderButton.connect("clicked()", self.showFolder)
        folderRow.addWidget(self.folderButton)
        self.buildResultButtons(folderRow)
        folderRow.addStretch(1)
        self.resultsLayout.addLayout(folderRow)
        self.resultsLayout.addWidget(disclaimerCaption(f"{self.PREFIX}Disclaimer"))
        self.results.hide()
        self.layout.addWidget(self.results)
        self.layout.addStretch(1)
        self._designTimer = qt.QTimer()
        self._designTimer.setSingleShot(True)
        self._designTimer.setInterval(DESIGN_DELAY_MS)
        self._designTimer.connect("timeout()", self.refresh)
        self._showFilter = ShowFilter(self.widget, self.refresh)
        for name in ("studyChanged", "bundleChanged", "computeEnabledChanged"):
            getattr(ctx, name).append(self.refresh)
        self.ctx.runner.jobFinished.append(self._onJobFinished)

    #
    # To implement
    #

    def runText(self):
        return _("Run")

    def buildControls(self, layout):
        pass

    def buildResults(self, layout):
        pass

    def buildResultButtons(self, row):
        pass

    def buildDesign(self, study, mapped, noMap):
        """The design of the current settings on ``study`` (the subjects with maps), or raise ``design.DesignError``."""
        raise NotImplementedError

    def blockingSettings(self):
        """Reasons the settings cannot run (not design checks): for example a missing volume-off reason."""
        return []

    def jobSpec(self, mapsTsv):
        raise NotImplementedError

    def resultFolders(self):
        """Folders whose result json may be shown when no job of this session produced one."""
        return []

    def showResult(self, folder, record):
        raise NotImplementedError

    def onScopeChanged(self):
        """Another study or connectome: forget what belonged to the previous one."""
        if not self.running():
            self.progress.setJob(None)
            self.progress.widget.hide()
            self.reasonLabel.setText("")

    #
    # Cohort, study and connectome
    #

    def card(self):
        return self.ctx.card("cohort")

    def currentMapsTsv(self):
        card = self.card()
        if card is not None and hasattr(card, "currentMapsTsv"):
            return card.currentMapsTsv()
        path = getattr(self.ctx, "cohortMaps", None)
        if path and os.path.isfile(path) and (_readJson(os.path.join(os.path.dirname(path), "cohortmaps.json")) or {}).get("status") == "done":
            return path
        return None

    def cohortRecord(self):
        path = self.currentMapsTsv()
        return _readJson(os.path.join(os.path.dirname(path), "cohortmaps.json")) if path else None

    def connectomeDir(self):
        """``<study>/derivatives/lnm/conn-<label>[_n-N]``: the folder of the cohort maps (and of these analyses)."""
        path = self.currentMapsTsv()
        if path:
            return os.path.dirname(os.path.dirname(path))
        widget = _mapWidget()
        cohortDir = widget.cohortOutputDir() if widget is not None and hasattr(widget, "cohortOutputDir") else None
        return os.path.dirname(cohortDir) if cohortDir else None

    def participantsPath(self):
        study = self.ctx.study
        path = getattr(study, "path", None) if study is not None else None
        return os.path.join(path, "participants.tsv") if path else None

    def maskPath(self):
        bundle = self.ctx.bundle
        return os.path.join(bundle.path, "mask.nii.gz") if bundle is not None else None

    def mapsRunning(self):
        widget = _mapWidget()
        job = getattr(widget, "cohortJob", None) if widget is not None else None
        return job if job is not None and not job.isFinished() else None

    def running(self):
        return (self.job is not None and not self.job.isFinished()) or (self.mapsJob is not None and not self.mapsJob.isFinished())

    def mapColumn(self):
        return "AvgFz"

    def designStudy(self):
        """(subjects study, maps.tsv rows {sid: (path, volume, key)} or None, subjects without a map, note).

        As the job builds it: participants.tsv as saved, restricted to the subjects with a map (lesion volumes
        missing from participants.tsv taken from maps.tsv). Before the lesions are mapped: the lesions the cohort
        maps job will map. Cached on the files' modification times.
        """
        from LesionNetworkMappingLib import glm

        participants = self.participantsPath()
        if not participants or not os.path.isfile(participants):
            raise ValueError(_("The study has no participants.tsv"))
        mapsTsv = self.currentMapsTsv()
        widget = _mapWidget()
        info = getattr(widget, "cohortInfo", None) if widget is not None else None
        seeds = tuple(sorted(sid for sid, _path in (info or {}).get("lesions", []))) if not mapsTsv else ()
        dictionary = os.path.join(os.path.dirname(participants), "participants.json")
        key = (participants, _stat(participants), _stat(dictionary), mapsTsv, _stat(mapsTsv), self.mapColumn(), seeds)
        if self._studyCache[0] == key:
            return self._studyCache[1]
        study, _inStudy = glm._load_study(participants)
        if mapsTsv:
            mapped = glm._map_rows(mapsTsv, self.mapColumn())
            withMaps = {sid for sid in study.subject_ids if sid in mapped}
            noMap = [sid for sid in study.subject_ids if sid not in mapped]
            subset, _filled = glm._subset_study(study, withMaps, {sid: values[1] for sid, values in mapped.items()})
            value = (subset, mapped, noMap, "")
        else:
            # Lesion volumes missing from participants.tsv come from the maps: until then the preview holds stand-in
            # values (the design's structure, nesting and permutations do not depend on them; Run checks again).
            subset, filled = glm._subset_study(study, set(seeds), {sid: f"{_standInVolume(sid):.3f}" for sid in seeds})
            note = _("Preview on the {n} lesions to map: Run maps them first, then checks the design again.").format(n=len(seeds))
            if filled:
                note += " " + _("{n} lesion volumes are not in participants.tsv yet: stand-in values are used until the maps give them (VIF and volume checks are rechecked).").format(n=filled)
            value = (subset, None, [], note)
        self._studyCache = (key, value)
        return value

    def addMapChecks(self, design, mapped, noMap, nPerm):
        """The checks glm.py adds at run time: maps that cannot be pooled, subjects without a map, and the
        permutation fields of a run with ``nPerm`` permutations (below the design minimum: a preview).
        """
        from LesionNetworkMappingLib import design as designlib
        from LesionNetworkMappingLib import glm

        if mapped is not None:
            pooling = glm._pooling_checks(design, mapped, self.cohortRecord())
            if pooling:
                design.checks = designlib._ordered([*pooling, *design.checks])
        if noMap:
            design.excluded[glm.EXCLUDED_NO_MAP] = list(noMap)
            design.checks = designlib._ordered([*design.checks, designlib.Check("info", "no_map", _("{n} subject(s) have no map and are excluded").format(n=len(noMap)), {"sids": noMap})])
        count, exhaustive = glm.shuffling_count(design, nPerm)
        if nPerm != design.spec.n_perm:
            glm._override_permutations(design, nPerm, count, exhaustive)
        return design

    def rebuildDesign(self):
        """Build the design of the current settings now; return it (None with ``designError`` set)."""
        from LesionNetworkMappingLib import design as designlib

        self.design, self.designError, self.designNote = None, None, ""
        if self.ctx.study is None:
            self.designError = _("Open a study in the Seed card (Seed: Cohort).")
            return None
        try:
            study, mapped, noMap, note = self.designStudy()
            self.designNote = note
            self.designMapped = mapped
            self.design = self.buildDesign(study, mapped, noMap)
        except (designlib.DesignError, ValueError) as exc:
            self.designError = str(exc)
        except Exception as exc:  # an unreadable table or maps.tsv: shown, never raised into Qt
            logging.exception(f"Lesion network mapping: the {self.TITLE} design could not be built")
            self.designError = _("The design cannot be built: {error}").format(error=getattr(exc, "message", None) or exc)
        return self.design

    def scheduleRefresh(self):
        self._designTimer.start()

    #
    # State
    #

    def runState(self):
        """(enabled, reason) of Run, from the study, connectome, settings, design checks and running jobs."""
        if self.ctx.study is None:
            return False, _("Open a study in the Seed card (Seed: Cohort).")
        if self.ctx.bundle is None:
            return False, _("Choose a connectome in the Connectome card.")
        if self.running():
            return False, _("Mapping the lesions first…") if self.job is None or self.job.isFinished() else _("{title} running…").format(title=self.TITLE)
        pending = self.planPending()
        if pending:
            return False, _("Changed after the analysis plan was locked: {changes}. Give the reason in the Recipe header (top of Map), or use the plan's settings.").format(
                changes="; ".join(change["description"] for change in pending))
        if self.design is None:
            # the error itself is the red line under the controls (showDesignState): say it once
            return False, _("Blocked by the design checks (in red above).") if self.designError else _("No design.")
        settings = self.blockingSettings()
        if settings:
            return False, settings[0]
        if self.design.blocking:
            return False, _("Blocked by the design checks (in red above).")
        if not self.ctx.computeEnabled():
            return False, " ".join(self.ctx.computeDisabledReasons()) or _("Not ready")
        if self.currentMapsTsv() is None:
            widget = _mapWidget()
            if self.mapsRunning() is not None:
                return True, _("The lesions are being mapped: the analysis starts when they are done.")
            enabled, reason = widget.cohortComputeState() if widget is not None else (False, _("The Map module is not available."))
            if not enabled:
                return False, reason
            return True, _("Run maps the lesions first (cohort maps), then runs the analysis.")
        if self.ctx.runner.isBusy():
            return True, _("Another computation is running: this one will wait for it.")
        return True, ""

    def refresh(self):
        """Rebuild the design and show the state (called when a control changes, debounced, and when the tab is shown)."""
        if self._cleanedUp:
            return
        self._designTimer.stop()
        scope = self.connectomeDir()
        if scope != self._resultScope:  # another study or connectome: its own results
            self._resultScope = scope
            self.result, self.outputs = None, []
            self.results.hide()
            self.onScopeChanged()
        self.rebuildDesign()
        self.showDesignState()
        self.showState()
        if self.result is None:
            self.showLatestResult()

    def showDesignState(self):
        """The design's blocking checks and warnings under Run (the preview has all of them)."""
        design = self.design
        if design is None:
            self.checksLabel.setText(checksHtml([{"level": "block", "message": self.designError}]) if self.designError else "")
            self.checksLabel.setVisible(bool(self.designError))
        else:
            shown = [check for check in design.checks if check.level in ("block", "warn")]
            self.checksLabel.setText(checksHtml(shown))
            self.checksLabel.setVisible(bool(shown))
        if self.preview.frame.isVisible():
            self.preview.setDesign(design, self.designNote, self.designError, self.previewTitle())

    def previewTitle(self):
        return _("Design preview")

    def showState(self):
        enabled, reason = self.runState()
        self.showPlanPending()
        self.runButton.setText(self.runText())
        self.runButton.setEnabled(enabled)
        self.reasonLabel.setText(reason)
        self.reasonLabel.setVisible(bool(reason))
        # The Cohort card's own status line already names the lesion maps and the connectome: only say what Run will do first
        mapped = bool(self.currentMapsTsv())
        self.statusLabel.setText("" if mapped else _("The lesions are not mapped with this connectome yet: Run maps them first."))
        self.statusLabel.setVisible(not mapped)

    def showPreview(self):
        self.rebuildDesign()
        self.showDesignState()
        self.preview.setDesign(self.design, self.designNote, self.designError, self.previewTitle())
        self.preview.showAt(self.previewButton)

    #
    # Analysis plan (L-39): after the lock, a change of this analysis's plan parameters waits for its reason
    #

    def planValues(self):
        """{recipes.ANALYSIS_FIELDS field: value} of the plan parameters this analysis holds (none by default)."""
        return {}

    def studyPath(self):
        study = self.ctx.study
        return os.fspath(study.path) if study is not None and getattr(study, "path", None) else None

    def planPending(self):
        """Changes of this analysis's settings from the locked plan that wait for a reason (recipes.analysis_pending)."""
        from LesionNetworkMappingLib import recipes

        path = self.studyPath()
        if self.PLAN_SCOPE is None or not path:
            return []
        try:
            return recipes.analysis_pending(path, self.PLAN_SCOPE, self.planValues())
        except (recipes.RecipeError, TypeError, ValueError, OSError):
            logging.exception("Lesion network mapping: the settings could not be compared with the locked analysis plan")
            return []

    def showPlanPending(self):
        """Let the Recipe header show (or stop showing) this analysis's changes from the locked plan."""
        pending = [change["description"] for change in self.planPending()]
        if pending == self._planShown:
            return
        self._planShown = pending
        header = self.ctx.card("recipe")
        if header is not None and hasattr(header, "refresh"):
            header.refresh()

    def applyPlanValues(self, values):
        """Set the controls of the plan parameters this analysis holds to ``values`` (recipes.analysis_parameters)."""

    #
    # Run
    #

    def run(self):
        """Run the analysis (after the cohort maps job when the lesions are not mapped); return the job, or None."""
        if self.running():
            return None
        self.rebuildDesign()
        self.showDesignState()
        if not self.runState()[0]:
            self.showState()
            return None
        mapsTsv = self.currentMapsTsv()
        if mapsTsv is None:
            job = self.mapsRunning()
            if job is None:
                card = self.card()
                job = card.mapLesions() if card is not None else _mapWidget().computeCohort()
            if job is None:
                self.showState()
                return None
            self.mapsJob = job
            self.progress.setJob(job)
            self.progress.widget.show()
            self.showState()
            return job
        return self._start(mapsTsv)

    def _start(self, mapsTsv):
        from . import jobrunner

        try:
            spec = self.jobSpec(mapsTsv)
        except Exception as exc:
            logging.exception(f"Lesion network mapping: the {self.TITLE} job could not be prepared")
            self.reasonLabel.setText(_("The analysis cannot start: {error}").format(error=exc))
            self.reasonLabel.show()
            return None
        if not isinstance(spec, jobrunner.JobSpec):
            return None
        self.jobFolder = self._jobFolder
        study = self.ctx.study
        self.jobScope = (self.connectomeDir(), self.studyPath(), getattr(study, "name", None) if study is not None else None)
        self.job = self.ctx.submit(spec)
        self.progress.setJob(self.job)
        self.progress.widget.show()
        self.showState()
        return self.job

    def _onJobFinished(self, job):
        if self._cleanedUp:
            return
        if job is self.mapsJob:
            self.mapsJob = None
            self._updateProgress(force=True)
            self._studyCache = (None, None)
            if job.succeeded() and self.currentMapsTsv() is not None:
                self.rebuildDesign()
                self.showDesignState()
                enabled, _reason = self.runState()
                if enabled:
                    self._start(self.currentMapsTsv())
                    return
            self.showState()
        elif getattr(job, "kind", None) == "cohortmaps":
            self._studyCache = (None, None)
            if self.widget.isVisible():
                self.scheduleRefresh()

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
        self.showState()
        scope, studyPath, studyName = self.jobScope or (None, None, None)
        if self.jobScope is not None and scope != self.connectomeDir():
            # Another study or connectome was opened while the job ran: its result stays in its own folder (shown when
            # that study is opened again) and is not shown, or its map loaded, as a result of the one open now
            self.onScopeChanged()
            if job.succeeded():
                shell = self.ctx.shell()
                if shell is not None:
                    if studyPath and studyPath == self.studyPath():
                        text = _("{title} done for another connectome: choose it again to see the maps")
                    elif studyName:
                        text = _("{title} of study “{study}” done: open that study to see the maps")
                    else:
                        text = _("{title} of another study done: open that study to see the maps")
                    shell.toast(text.format(title=self.TITLE, study=studyName), 3000)
            return
        if job.state == "failed":
            # After showState(), which writes the run reason (empty once the job is over) and would hide this label
            self.reasonLabel.setText(_("{title} failed: {reason}").format(title=self.TITLE, reason=job.failureReason))
            self.reasonLabel.show()
        if job.succeeded():
            folder = self.jobFolder
            self.loadResult(folder, show=True)
            shell = self.ctx.shell()
            if shell is not None:
                shell.toast(_("{title} done: the maps are in the Cohort card").format(title=self.TITLE), 3000)

    #
    # Results
    #

    def loadResult(self, folder, show=False):
        """Show the result json of ``folder`` (see ``showResult``); with ``show``, also show its first map."""
        record = self.readResult(folder)
        if record is None or record.get("status") != "done":
            return None
        self.result = (folder, record)
        self.showResult(folder, record)
        self.results.show()
        if show and self.outputs:
            # Next event loop pass: the panel is painted before the map is read and shown (no long GUI stall)
            qt.QTimer.singleShot(0, self._showFirstMap)
        return record

    def _showFirstMap(self):
        if not self._cleanedUp and self.outputs:
            self.showMap(0)

    def readResult(self, folder):
        raise NotImplementedError

    def showLatestResult(self):
        """The newest finished result of this cohort's connectome folder, so that the tab shows it again after a restart."""
        best = None
        for folder in self.resultFolders():
            record = self.readResult(folder)
            if record is not None and record.get("status") == "done":
                stamp = _stat(folder)
                if best is None or (stamp or (0,)) > best[0]:
                    best = (stamp or (0,), folder)
        if best is not None:
            self.loadResult(best[1])

    def setOutputs(self, outputs):
        """[(desc, label, path)] of the maps table."""
        self.outputs = [(desc, text, path) for desc, text, path in outputs if path and os.path.isfile(path)]
        table = self.mapsTable
        table.setRowCount(len(self.outputs))
        for row, (_desc, text, path) in enumerate(self.outputs):
            table.setItem(row, 0, qt.QTableWidgetItem(text))
            item = qt.QTableWidgetItem(os.path.basename(path))
            item.setToolTip(path)
            table.setItem(row, 1, item)
        table.setVisible(bool(self.outputs))

    def onMapClicked(self, row, column):
        self.showMap(row)

    def showMap(self, index):
        """Show one output map in the Cohort card's group map volume; return the volume."""
        if not 0 <= index < len(self.outputs):
            return None
        desc, text, path = self.outputs[index]
        volume = showGroupFile(self.ctx, path, desc, f"{self.TITLE}: {text}")
        if volume is not None:
            self.mapsTable.selectRow(index)
        return volume

    def showFolder(self):
        folder = self.result[0] if self.result else None
        if folder and os.path.isdir(folder) and not slicer.app.testingEnabled():
            widgets.showInFileManager(folder)

    def message(self, text, ms=3000):
        """A consequence message: a toast in Studio, the reason line in Classic."""
        shell = self.ctx.shell()
        if shell is not None:
            shell.toast(text, ms)
        else:
            self.reasonLabel.setText(text)
            self.reasonLabel.show()

    def cleanup(self):
        self._cleanedUp = True
        self._designTimer.stop()
        for name in ("studyChanged", "bundleChanged", "computeEnabledChanged"):
            callbacks = getattr(self.ctx, name, [])
            if self.refresh in callbacks:
                callbacks.remove(self.refresh)
        finished = getattr(self.ctx.runner, "jobFinished", [])
        if self._onJobFinished in finished:
            finished.remove(self._onJobFinished)
        for job in (self.job, getattr(self, "palmJob", None)):
            if job is not None and not job.isFinished():
                job.spec.onDone = None
                job.spec.onEvent = None
        self.preview.close()


def _standInVolume(sid):
    """A deterministic stand-in lesion volume (mL) of a subject whose volume is not known before its map is computed."""
    import hashlib

    return 1.0 + int(hashlib.sha1(str(sid).encode("utf-8")).hexdigest()[:8], 16) % 2000 / 100.0


def groupVolume(ctx):
    """The Cohort card's group map volume (one, reused by every tab), or None."""
    card = ctx.card("cohort")
    if card is not None and hasattr(card, "groupVolume"):
        return card.groupVolume()
    for candidate in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
        if candidate.GetAttribute(GROUP_VOLUME_ATTRIBUTE) == "1":
            return candidate
    return None


def showGroupFile(ctx, path, desc, name):
    """Show a group map file in the group map volume with its own colour table copy (``display.applyGroupMapDisplay``),
    over the template; return the volume, or None.
    """
    from LesionNetworkMappingLib import display

    widget = _mapWidget()
    if widget is None or not path or not os.path.isfile(path):
        return None
    attributes = {GROUP_VOLUME_ATTRIBUTE: "1", "Studio.Role": "statmap", display.GROUP_MAP_ATTRIBUTE: desc}
    try:
        volume = widget.logic.loadMap(path, name, groupVolume(ctx), saveWithScene=True, attributes=attributes)
    except Exception as exc:
        logging.exception(f"Lesion network mapping: {path} could not be shown")
        shell = ctx.shell()
        if shell is not None:
            shell.toast(_("Cannot show {name}: {error}").format(name=os.path.basename(path), error=exc), 4000)
        return None
    card = ctx.card("cohort")
    if card is not None and hasattr(card, "groupVolumeId"):
        card.groupVolumeId = volume.GetID()
    display.applyGroupMapDisplay(volume, desc)
    node = ctx.parameterNode
    opacity = getattr(node, "opacity", 0.8) if node is not None else 0.8
    mappage = None
    if ctx.shell() is not None:
        try:
            from StudioLib import mappage
        except ImportError:
            mappage = None
    if mappage is not None:
        mappage.captureLayers()
        template = mappage.templateVolume(ctx.bundle, load=False)
        if template is None:
            # Loading the template file takes about as long as showing the map: the map is shown now, the template in
            # the next event loop pass, so that neither step stalls the application for long
            def showTemplate(volumeId=volume.GetID()):
                loaded = mappage.templateVolume(ctx.bundle)

                def layers():  # its first render in a later pass than its loading
                    shown = slicer.mrmlScene.GetNodeByID(volumeId)
                    if shown is not None and loaded is not None and loaded.GetScene() is not None:
                        display.showLayers(loaded, shown, opacity)

                qt.QTimer.singleShot(TEMPLATE_LAYER_DELAY_MS, layers)

            qt.QTimer.singleShot(0, showTemplate)
    else:
        template = widget.logic.templateVolume()
    display.showLayers(template, volume, opacity)
    return volume


def segmented(labels, objectName, toolTip=""):
    frame, group = widgets.segmented(labels)
    frame.setObjectName(objectName)
    if toolTip:
        frame.setToolTip(toolTip)
    return frame, group


class CovariateChips:
    """Checkable chips of covariate columns (lesion volume on by default) in rows of three."""

    def __init__(self, objectNamePrefix, onChanged):
        self.prefix = objectNamePrefix
        self.onChanged = onChanged
        self.widget = qt.QWidget()
        self.widget.setObjectName(f"{objectNamePrefix}Covariates")
        self.grid = qt.QGridLayout(self.widget)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(4)
        self.buttons = {}

    def setColumns(self, columns, checked=None):
        """Show chips for ``columns``; keep the checked state of the chips that stay; ``checked`` are checked for new chips."""
        previous = {name: button.checked for name, button in self.buttons.items()}
        if list(columns) == list(self.buttons):
            return
        for button in self.buttons.values():
            self.grid.removeWidget(button)
            button.setParent(None)
            button.deleteLater()
        self.buttons = {}
        for index, name in enumerate(columns):
            button = qt.QToolButton()
            button.setObjectName(f"{self.prefix}Covariate")
            button.setText(name)
            button.setCheckable(True)
            button.setChecked(previous.get(name, name in (checked or ())))
            button.setToolButtonStyle(qt.Qt.ToolButtonTextOnly)
            button.setToolTip(_("Covariate {name} (nuisance regressor) · click to add or remove").format(name=name))
            button.connect("toggled(bool)", lambda _checked: self.onChanged())
            self.grid.addWidget(button, index // 3, index % 3, qt.Qt.AlignLeft)
            self.buttons[name] = button
        self.grid.setColumnStretch(3, 1)  # the chips keep their size, left-aligned

    def checked(self):
        return [name for name, button in self.buttons.items() if button.checked]

    def setChecked(self, name, checked):
        button = self.buttons.get(name)
        if button is not None:
            button.setChecked(bool(checked))


#
# Contrast tab
#


class ContrastTab(CohortTab):
    """The Contrast tab (see the module docstring)."""

    PREFIX = "LNMContrast"
    TITLE = "GLM"
    PLAN_SCOPE = "contrast"

    def __init__(self, ctx):
        self.palmJob = None
        self.palmFolder = None
        self.palmImported = None
        self._tfceBox = (None, None)
        self._levels = []
        super().__init__(ctx)

    def runText(self):
        return _("Run GLM")

    def previewTitle(self):
        return _("Design preview · Contrast")

    #
    # Controls
    #

    def buildControls(self, layout):
        from LesionNetworkMappingLib import design as designlib

        form = qt.QFormLayout()
        form.setRowWrapPolicy(qt.QFormLayout.WrapLongRows)
        form.setFieldGrowthPolicy(qt.QFormLayout.AllNonFixedFieldsGrow)
        self.groupCombo = qt.QComboBox()
        self.groupCombo.setObjectName("LNMContrastGroupColumn")
        self.groupCombo.setToolTip(_("The factor tested (a column with the role group in participants.json)"))
        self.groupCombo.connect("currentIndexChanged(int)", self.onGroupColumnChanged)
        form.addRow(_("Groups"), self.groupCombo)
        levels = qt.QHBoxLayout()
        self.levelACombo = qt.QComboBox()
        self.levelACombo.setObjectName("LNMContrastLevelA")
        self.levelACombo.setToolTip(_("Level A"))
        self.levelACombo.connect("currentIndexChanged(int)", lambda index: self.onLevelsChanged())
        levels.addWidget(self.levelACombo, 1)
        levels.addWidget(qt.QLabel(_("vs")))
        self.levelBCombo = qt.QComboBox()
        self.levelBCombo.setObjectName("LNMContrastLevelB")
        self.levelBCombo.setToolTip(_("Level B"))
        self.levelBCombo.connect("currentIndexChanged(int)", lambda index: self.onLevelsChanged())
        levels.addWidget(self.levelBCombo, 1)
        form.addRow(_("Levels"), levels)
        self.contrastFrame, self.contrastGroup = segmented(["A > B", "B > A", _("Two-sided"), "F"], "LNMContrastContrast",
                                                          _("Contrast · A > B and B > A are one-sided; F tests every level of the group column (cell means)"))
        self.contrastGroup.button(2).setChecked(True)
        self.contrastGroup.connect(_idClickedSignal(self.contrastGroup), lambda index: self.onSettingsChanged())
        form.addRow(_("Contrast"), self.contrastFrame)
        layout.addLayout(form)

        layout.addWidget(label(_("Covariates"), "LNMContrastCovariatesTitle", "secondaryText"))
        self.covariates = CovariateChips("LNMContrast", self.onSettingsChanged)
        layout.addWidget(self.covariates.widget)
        self.logVolumeCheckBox = qt.QCheckBox(_("log(volume)"))
        self.logVolumeCheckBox.setObjectName("LNMContrastLogVolume")
        self.logVolumeCheckBox.setToolTip(_("Use the logarithm of lesion volume (suggested when its skewness is above 2)"))
        self.logVolumeCheckBox.connect("toggled(bool)", lambda checked: self.onSettingsChanged())
        layout.addWidget(self.logVolumeCheckBox)
        self.volumeReasonEdit = qt.QLineEdit()
        self.volumeReasonEdit.setObjectName("LNMContrastVolumeOffReason")
        self.volumeReasonEdit.setPlaceholderText(_("Why is lesion volume not a covariate? (recorded in the methods)"))
        self.volumeReasonEdit.setToolTip(_("Lesion volume is the main confound of lesion network maps (Moore 2026): turning it off needs a reason"))
        self.volumeReasonEdit.connect("textChanged(QString)", lambda text: self.scheduleRefresh())
        self.volumeReasonEdit.hide()
        layout.addWidget(self.volumeReasonEdit)

        permute = qt.QHBoxLayout()
        permute.addWidget(qt.QLabel(_("Permute")))
        self.permuteCombo = qt.QComboBox()
        self.permuteCombo.setObjectName("LNMContrastPermuteMode")
        for mode, text in PERMUTE_CHOICES:
            self.permuteCombo.addItem(_(text), mode)
        self.permuteCombo.setToolTip(_("Override the automatic exchangeability decision"))
        self.permuteCombo.connect("currentIndexChanged(int)", lambda index: self.onSettingsChanged())
        # Ignored: the combos share the row and never make the inspector wider
        self.permuteCombo.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        permute.addWidget(self.permuteCombo, 1)
        self.blockCombo = qt.QComboBox()
        self.blockCombo.setObjectName("LNMContrastBlockColumn")
        self.blockCombo.setToolTip(_("Exchangeability blocks: dataset, else source_type (a column with the role block)"))
        self.blockCombo.connect("currentIndexChanged(int)", lambda index: self.onSettingsChanged())
        self.blockCombo.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        permute.addWidget(self.blockCombo, 1)
        layout.addLayout(permute)
        self.permuteLabel = label("", "LNMContrastPermuteReason", "secondaryText")
        self.permuteLabel.setToolTip(_("How labels are exchanged, decided from the nesting of the group in the blocks (design.py)"))
        layout.addWidget(self.permuteLabel)
        self.restrictCheckBox = qt.QCheckBox(_("Only the blocks that hold both levels (within-cohort design)"))
        self.restrictCheckBox.setObjectName("LNMContrastRestrictBlocks")
        self.restrictCheckBox.connect("toggled(bool)", lambda checked: self.onSettingsChanged())
        # A long text: it wraps (a check box shows it on one line and would widen the inspector)
        self.restrictRow = widgets.wrapCheckBox(self.restrictCheckBox)
        self.restrictRow.hide()
        layout.addWidget(self.restrictRow)
        self.varianceCheckBox = qt.QCheckBox(_("Unequal variances (Aspin–Welch v)"))  # noqa: RUF001
        self.varianceCheckBox.setObjectName("LNMContrastVarianceGroups")
        self.varianceCheckBox.setToolTip(_("Use the groups as variance groups (two-group tests; suggested when the variance ratio is above 4)"))
        self.varianceCheckBox.connect("toggled(bool)", lambda checked: self.onSettingsChanged())
        layout.addWidget(self.varianceCheckBox)

        options = qt.QFormLayout()
        options.setRowWrapPolicy(qt.QFormLayout.WrapLongRows)
        self.inputFrame, self.inputGroup = segmented(["Fz", "T"], "LNMContrastInput", _("Per-lesion maps tested: mean Fisher z (AvgFz, default; independent of the connectome N) or T"))
        self.inputGroup.connect(_idClickedSignal(self.inputGroup), lambda index: self.onSettingsChanged())
        options.addRow(_("Input"), self.inputFrame)
        inference = qt.QHBoxLayout()
        self.fweCheckBox = qt.QCheckBox(_("FWE"))
        self.fweCheckBox.setObjectName("LNMContrastFwe")
        self.fweCheckBox.setChecked(True)
        self.fweCheckBox.setEnabled(False)
        self.fweCheckBox.setToolTip(_("Voxel-wise max-statistic FWE: the primary inference (always computed)"))
        inference.addWidget(self.fweCheckBox)
        self.fdrCheckBox = qt.QCheckBox(_("FDR"))
        self.fdrCheckBox.setObjectName("LNMContrastFdr")
        self.fdrCheckBox.setChecked(True)
        self.fdrCheckBox.setToolTip(_("Benjamini-Hochberg q of the permutation p (secondary)"))
        self.fdrCheckBox.connect("toggled(bool)", lambda checked: self.onSettingsChanged())
        inference.addWidget(self.fdrCheckBox)
        self.tfceCheckBox = qt.QCheckBox(_("TFCE"))
        self.tfceCheckBox.setObjectName("LNMContrastTfce")
        self.tfceCheckBox.setToolTip(_("Threshold-free cluster enhancement (H = 2, E = 0.5, 26-connectivity): slow, opt-in"))
        self.tfceCheckBox.connect("toggled(bool)", lambda checked: self.onSettingsChanged())
        inference.addWidget(self.tfceCheckBox)
        inference.addStretch(1)
        options.addRow(_("Inference"), inference)
        self.permSpinBox = qt.QSpinBox()
        self.permSpinBox.setObjectName("LNMContrastPermutations")
        self.permSpinBox.setRange(100, 100000)
        self.permSpinBox.setSingleStep(1000)
        self.permSpinBox.setValue(DEFAULT_PERM)
        self.permSpinBox.setToolTip(_("Permutations, the identity included (5000 by default; fewer than 1000 is a preview)"))
        self.permSpinBox.connect("valueChanged(int)", lambda value: self.onSettingsChanged())
        options.addRow(_("Permutations"), self.permSpinBox)
        layout.addLayout(options)
        self.tfceLabel = label("", "LNMContrastTfceEta", "secondaryText")
        layout.addWidget(self.tfceLabel)

        palmRow = qt.QHBoxLayout()
        self.exportPalmButton = qt.QPushButton(_("Export for PALM…"))
        self.exportPalmButton.setObjectName("LNMContrastExportPalmButton")
        self.exportPalmButton.setProperty("slicerRole", "ghost")
        self.exportPalmButton.setToolTip(_("Write the maps, design, contrast, blocks and run_palm.sh that reproduce this design in PALM (validated after writing)"))
        self.exportPalmButton.connect("clicked()", self.exportPalm)
        palmRow.addWidget(self.exportPalmButton)
        self.importPalmButton = qt.QPushButton(_("Import PALM results…"))
        self.importPalmButton.setObjectName("LNMContrastImportPalmButton")
        self.importPalmButton.setProperty("slicerRole", "ghost")
        self.importPalmButton.setToolTip(_("Show the maps PALM wrote for an export folder (-logp outputs are converted to p)"))
        self.importPalmButton.connect("clicked()", self.onImportPalm)
        palmRow.addWidget(self.importPalmButton)
        palmRow.addStretch(1)
        layout.addLayout(palmRow)
        self.palmLabel = label("", "LNMContrastPalmStatus")
        self.palmLabel.setTextFormat(qt.Qt.RichText)
        self.palmLabel.hide()
        layout.addWidget(self.palmLabel)
        self.palmProgress = widgets.progressRow(objectNamePrefix="LNMContrastPalm")
        self.palmProgress.label.setWordWrap(False)
        self.palmProgress.label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        self.palmProgress.widget.hide()
        layout.addWidget(self.palmProgress.widget)
        self._defaultPerm = designlib.DEFAULT_N_PERM

    #
    # Settings
    #

    def contrast(self):
        index = self.contrastGroup.checkedId()
        return CONTRASTS[index] if 0 <= index < len(CONTRASTS) else "two_sided"

    def setContrast(self, contrast):
        self.contrastGroup.button(CONTRASTS.index(contrast)).setChecked(True)
        self.onSettingsChanged()

    def input(self):
        index = self.inputGroup.checkedId()
        return INPUTS[index] if 0 <= index < len(INPUTS) else "Fz"

    def mapColumn(self):
        return {"Fz": "AvgFz", "T": "T"}[self.input()]

    def groupColumn(self):
        return self.groupCombo.currentText or "group"

    def levels(self):
        a, b = self.levelACombo.currentText, self.levelBCombo.currentText
        return [a, b] if a and b and a != b else None

    def volumeOn(self):
        from LesionNetworkMappingLib import design as designlib

        return designlib.VOLUME_COLUMN in self.covariates.checked() or designlib.VOLUME_COLUMN not in self.covariates.buttons

    def nPerm(self):
        return int(self.permSpinBox.value)

    def planValues(self):
        return {"n_perm": self.nPerm(), "design.covariates": self.covariates.checked(), "design.tfce": bool(self.tfceCheckBox.checked),
                "fdr": bool(self.fdrCheckBox.checked), "statistic": self.input()}

    def applyPlanValues(self, values):
        if values.get("n_perm") is not None:
            self.permSpinBox.setValue(int(values["n_perm"]))
        if values.get("design.covariates") is not None:
            for name, button in self.covariates.buttons.items():
                button.setChecked(name in values["design.covariates"])
        if values.get("design.tfce") is not None:
            self.tfceCheckBox.setChecked(bool(values["design.tfce"]))
        if values.get("fdr") is not None:
            self.fdrCheckBox.setChecked(bool(values["fdr"]))
        if values.get("statistic") in INPUTS:
            self.inputGroup.button(INPUTS.index(values["statistic"])).setChecked(True)
        self.onSettingsChanged()

    def spec(self):
        """The ``design.DesignSpec`` of the current settings (n_perm at least the design minimum; ``nPerm()`` is run)."""
        from LesionNetworkMappingLib import design as designlib

        contrast = self.contrast()
        covariates = self.covariates.checked()
        transforms = {designlib.VOLUME_COLUMN: "log"} if designlib.VOLUME_COLUMN in covariates and self.logVolumeCheckBox.checked else {}
        reason = None if designlib.VOLUME_COLUMN in covariates else (self.volumeReasonEdit.text.strip() or None)
        test = "anova" if contrast == "F" else "two_group"
        levels = None if contrast == "F" else self.levels()
        return designlib.DesignSpec(
            test=test,
            group_column=self.groupColumn(),
            levels=levels,
            contrast=contrast,
            covariates=covariates,
            transforms=transforms,
            block_column=self.blockCombo.currentData or None,
            permute=self.permuteCombo.currentData or "auto",
            input=self.input(),
            inference={"n_perm": max(self.nPerm(), designlib.MIN_N_PERM), "fwe": True, "fdr": bool(self.fdrCheckBox.checked), "tfce": bool(self.tfceCheckBox.checked)},
            variance_groups=bool(self.varianceCheckBox.checked and self.varianceCheckBox.enabled and test == "two_group"),
            volume_off_reason=reason,
            restrict_to_mixed_blocks=bool(self.restrictCheckBox.checked and self.restrictCheckBox.isVisibleTo(self.widget)),
        )

    def blockingSettings(self):
        reasons = []
        if not self.volumeOn() and not self.volumeReasonEdit.text.strip():
            reasons.append(_("Give the reason for turning lesion volume off (it is recorded in the methods)."))
        if self.contrast() != "F" and self.levels() is None:
            reasons.append(_("Choose two different levels A and B."))
        return reasons

    def onSettingsChanged(self):
        from LesionNetworkMappingLib import design as designlib

        volumeOn = self.volumeOn()
        self.volumeReasonEdit.setVisible(not volumeOn)
        self.logVolumeCheckBox.setVisible(volumeOn and designlib.VOLUME_COLUMN in self.covariates.buttons)
        twoGroup = self.contrast() != "F"
        pair = len(self._levels) >= 2  # a one-sided contrast compares two different levels of the group column
        self.levelACombo.setEnabled(twoGroup and pair)
        self.levelBCombo.setEnabled(twoGroup and pair)
        for index in (0, 1):
            self.contrastGroup.button(index).setEnabled(pair)
        tip = "" if pair else _("The group column has fewer than two levels among the subjects: there is nothing to contrast")
        self.levelACombo.setToolTip(tip or _("Level A"))
        self.levelBCombo.setToolTip(tip or _("Level B"))
        self.varianceCheckBox.setEnabled(twoGroup)
        self.showTfceEta()
        self.scheduleRefresh()

    def onGroupColumnChanged(self, index=None):
        self.fillLevels()
        self.onSettingsChanged()

    def onLevelsChanged(self):
        a, b = (self.levelACombo.currentText or "A", self.levelBCombo.currentText or "B") if len(self._levels) >= 2 else ("A", "B")
        for index, text in enumerate([f"{a} > {b}", f"{b} > {a}"]):
            self.contrastGroup.button(index).setText(text)
        self.onSettingsChanged()

    def fillColumns(self):
        """Group, block and covariate columns of the study (kept when they still exist)."""
        from LesionNetworkMappingLib import design as designlib

        study = self.ctx.study
        columns = study.columns if study is not None else []
        groups = [column for column in (study.group_columns() if study is not None else []) if column in columns]
        _fill(self.groupCombo, [(column, column) for column in groups])
        blocks = [column for column in (study.block_columns() if study is not None else []) if column in columns]
        for column in designlib.NESTING_COLUMNS:
            if column in columns and column not in blocks:
                blocks.append(column)
        _fill(self.blockCombo, [(_("Blocks: automatic"), "")] + [(column, column) for column in blocks])
        covariates = [column for column in (study.covariate_columns() if study is not None else []) if column != self.groupColumn()]
        if designlib.VOLUME_COLUMN not in covariates:
            covariates.insert(0, designlib.VOLUME_COLUMN)  # filled from the maps when participants.tsv has none
        self.covariates.setColumns(covariates, checked=(designlib.VOLUME_COLUMN,))
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
        # Keep A and B when the new levels still offer them; otherwise default to a valid, distinct pair
        a = self.levelACombo.currentText if self.levelACombo.count else ""
        b = self.levelBCombo.currentText if self.levelBCombo.count else ""
        a = a if a in levels else (levels[0] if levels else "")
        b = b if b in levels and b != a else next((level for level in levels if level != a), "")
        _fill(self.levelACombo, [(level, level) for level in levels], a)
        _fill(self.levelBCombo, [(level, level) for level in levels] if len(levels) >= 2 else [], b)  # one level: no "A vs A"
        self.onLevelsChanged()

    #
    # Design
    #

    def buildDesign(self, study, mapped, noMap):
        from LesionNetworkMappingLib import design as designlib

        design = designlib.build_design(study, self.spec())
        return self.addMapChecks(design, mapped, noMap, self.nPerm())

    def refresh(self):
        if self._cleanedUp:
            return
        self.fillColumns()
        super().refresh()
        self.showPermuteState()
        self.showTfceEta()
        self.showPalmState()

    def showState(self):
        super().showState()
        self.showPalmState()

    def onScopeChanged(self):
        super().onScopeChanged()
        if self.palmJob is None or self.palmJob.isFinished():
            self.palmJob, self.palmFolder, self.palmImported = None, None, None
            self.palmLabel.hide()
            self.palmProgress.setJob(None)
            self.palmProgress.widget.hide()

    def showPermuteState(self):
        design = self.design
        nested = next((check for check in design.checks if check.code == "nested"), None) if design is not None else None
        mixed = (nested.detail or {}).get("mixed_blocks") if nested is not None and isinstance(nested.detail, dict) else None
        self.restrictRow.setVisible(bool(mixed) or self.restrictCheckBox.checked)
        if design is None:
            self.permuteLabel.setText("")
            self.permuteLabel.hide()
            return
        text = design.permute_reason
        if design.block_column and design.permute != "free":
            text += " · " + _("{n} blocks").format(n=len(set(design.block_labels or [])))
        self.permuteLabel.setText(_("Permute: {reason}").format(reason=text))
        self.permuteLabel.show()

    def tfceBoxVoxels(self):
        """Voxels of the bounding box of the connectome mask (what TFCE costs), cached per connectome."""
        from LesionNetworkMappingLib import tfce

        bundle = self.ctx.bundle
        if bundle is None:
            return None
        if self._tfceBox[0] != bundle.path:
            try:
                height, width, depth = (int(item.stop) - int(item.start) for item in tfce.bounding_box(bundle.mask))
                voxels = height * width * depth
            except Exception:
                logging.exception("Lesion network mapping: the mask box of the TFCE estimate could not be read")
                voxels = None
            self._tfceBox = (bundle.path, voxels)
        return self._tfceBox[1]

    def tfceEta(self, nPerm=None):
        """Seconds of TFCE for ``nPerm`` permutations (tfce.tfce_eta), or None."""
        from LesionNetworkMappingLib import tfce

        voxels = self.tfceBoxVoxels()
        if not voxels:
            return None
        twoSided = self.contrast() == "two_sided"
        return tfce.tfce_eta(None, nPerm or self.nPerm(), twoSided, box_voxels=voxels)

    def showTfceEta(self):
        eta = self.tfceEta()
        if eta is None:
            self.tfceLabel.setText(_("TFCE: opt-in; its time depends on the connectome mask (choose a connectome)."))
            return
        text = _("TFCE adds about {time} for {n} permutations.").format(time=duration(eta), n=f"{self.nPerm():,}")
        if self.nPerm() > TFCE_SUGGESTED_PERM:
            text += " " + _("Use 1000 permutations (about {time}) or Export for PALM.").format(time=duration(self.tfceEta(TFCE_SUGGESTED_PERM)))
        self.tfceLabel.setText(text)
        self.tfceLabel.setProperty("slicerRole", "secondaryText" if not self.tfceCheckBox.checked else "")

    #
    # Run
    #

    def outputDir(self):
        from LesionNetworkMappingLib import glm

        connDir = self.connectomeDir()
        if not connDir or self.design is None:
            return None
        return os.path.join(connDir, "glm", glm.contrast_label(self.design))

    def resultFolders(self):
        connDir = self.connectomeDir()
        root = os.path.join(connDir, "glm") if connDir else None
        if not root or not os.path.isdir(root):
            return []
        return [os.path.join(root, name) for name in sorted(os.listdir(root)) if os.path.isfile(os.path.join(root, name, "glm.json"))]

    def readResult(self, folder):
        from LesionNetworkMappingLib import glm

        return _readJson(os.path.join(folder, glm.GLM_JSON))

    def jobSpec(self, mapsTsv):
        from . import jobrunner

        outDir = self.outputDir()
        if outDir is None:
            raise ValueError(_("No output folder (study or connectome missing)"))
        os.makedirs(outDir, exist_ok=True)
        self._jobFolder = outDir
        spec = self.spec()
        designPath = spec.save(os.path.join(outDir, "design.json"))
        args = ["--maps", mapsTsv, "--participants", self.participantsPath(), "--design", designPath, "--mask", self.maskPath(), "--out", outDir, "--run-root", self.ctx.study.path]
        if self.nPerm() != spec.n_perm:
            args += ["--perm", str(self.nPerm())]
        if spec.inference.get("tfce"):
            args.append("--tfce")
        return jobrunner.JobSpec("LesionNetworkMappingLib.glm", args, title=_("GLM"), heavy=True, kind="glm", runRoot=os.path.dirname(outDir), onEvent=self.onEvent, onDone=self.onDone,
                                 name="glm", statusText=_glmStatusText)

    #
    # Results
    #

    def buildResults(self, layout):
        pass

    def showResult(self, folder, record):
        files = record.get("outputs") or {}
        desc = (record.get("contrast") or {}).get("label") or ""
        effect = (record.get("effect_size") or {}).get("name") or "HedgesG"
        effectText = {"HedgesG": _("Effect size: Hedges g"), "PartialR": _("Effect size: partial r"), "PartialEtaSq": _("Effect size: partial η²")}.get(effect, effect)
        stat = (record.get("stat") or "t")
        rows = []
        for key, text in GLM_OUTPUT_LABELS:
            name = files.get(key)
            if not name:
                continue
            if key == "effect":
                text = effectText
            elif key == "stat":
                text = {"t": _("Statistic (T)"), "F": _("Statistic (F)"), "v": _("Statistic (Aspin–Welch v)")}.get(stat, text)  # noqa: RUF001
            else:
                text = _(text)
            suffix = {"stat": "Fstat" if stat == "F" else "Tstat", "p_unc": "PUnc", "p_fwe": "PFwe", "p_fdr": "PFdr", "tfce_p_fwe": "TfcePFwe", "effect": effect}[key]
            rows.append((desc + suffix, text, os.path.join(folder, os.path.basename(name))))
        self.setOutputs(rows)
        fwe, fdr = record.get("fwe") or {}, record.get("fdr") or {}
        permutation = record.get("permutation") or {}
        nPerm = permutation.get("n_perm") or record.get("n_perm")
        perLevel = " · ".join(f"{html.escape(str(level))} {count}" for level, count in (record.get("n_per_level") or {}).items())
        lines = [
            "<b>" + html.escape(_("{test} · {contrast} · n = {n}").format(test=_(DESIGN_TYPE_TEXT.get(record.get("design_type"), record.get("design_type") or "")).split(":")[0],
                                                                           contrast=contrastText((record.get("contrast") or {}).get("spec", ""), list(record.get("n_per_level") or {})),
                                                                           n=record.get("n", "?"))) + (f" ({perLevel})" if perLevel else "") + "</b>",
            html.escape(_("{n} permutations{kind} · smallest attainable p {p:.2g} · {scheme}").format(
                n=f"{nPerm:,}" if isinstance(nPerm, int) else nPerm, kind=_(" (exhaustive)") if permutation.get("exhaustive") else (_(" (preview)") if permutation.get("preview") else ""),
                p=float(record.get("min_attainable_p") or record.get("min_p") or 1.0), scheme=permutation.get("reason") or permutation.get("scheme") or "")),
            html.escape(_("FWE p ≤ 0.05: {n} voxels (smallest FWE p {p})").format(n=fwe.get("n_significant", "?"), p=_fmtP(fwe.get("min_p")))),
        ]
        effectRange = (record.get("effect_size") or {}).get("range")
        if effectRange:
            lines.append(html.escape(_("{effect} from {low:.2f} to {high:.2f}").format(effect=effectText, low=effectRange[0], high=effectRange[1])))
        if fdr.get("enabled", True) and fdr:
            lines.append(html.escape(_("FDR q ≤ 0.05: {n} voxels · {note}").format(n=fdr.get("n_significant", "?"), note=fdr.get("note") or "")))
        tfce = record.get("tfce") or {}
        if tfce.get("enabled"):
            lines.append(html.escape(_("TFCE FWE p ≤ 0.05: {n} voxels").format(n=tfce.get("n_significant", "?"))))
        self.summaryLabel.setText("".join(f"<div>{line}</div>" for line in lines))
        notes = []
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

    #
    # PALM
    #

    def palmRoot(self):
        study = self.ctx.study
        return os.path.join(study.path, "derivatives", "lnm", "palm") if study is not None and getattr(study, "path", None) else None

    def palmFolders(self):
        root = self.palmRoot()
        if not root or not os.path.isdir(root):
            return []
        from LesionNetworkMappingLib import palm

        folders = [os.path.join(root, name) for name in os.listdir(root) if os.path.isfile(os.path.join(root, name, palm.EXPORT_JSON))]
        return sorted(folders, key=lambda path: _stat(path) or (0,))

    def palmState(self):
        """(enabled, reason) of Export for PALM."""
        if self.palmJob is not None and not self.palmJob.isFinished():
            return False, _("Exporting for PALM…")
        if self.currentMapsTsv() is None:
            return False, _("Map the lesions first (Run, or Compute: Map all lesions).")
        if self.design is None:
            return False, self.designError or _("No design.")
        settings = self.blockingSettings()
        if settings:
            return False, settings[0]
        if self.design.blocking:
            return False, _("Blocked by the design checks (in red above).")
        return True, ""

    def showPalmState(self):
        enabled, reason = self.palmState()
        self.exportPalmButton.setEnabled(enabled)
        self.exportPalmButton.setToolTip(reason or _("Write the maps, design, contrast, blocks and run_palm.sh that reproduce this design in PALM (validated after writing)"))
        self.importPalmButton.setEnabled(self.ctx.study is not None)

    def exportPalm(self, folder=None):
        """Export the current design and maps for PALM (a job); return the job, or None."""
        from LesionNetworkMappingLib import export, glm

        from . import jobrunner

        self.rebuildDesign()
        self.showDesignState()
        enabled, reason = self.palmState()
        if not enabled:
            self.palmLabel.setText(html.escape(reason))
            self.palmLabel.show()
            return None
        mapsTsv = self.currentMapsTsv()
        if folder is None:
            label = export.connectome_label((self.cohortRecord() or {}).get("bundle", {}).get("name") or (self.ctx.bundle.name if self.ctx.bundle is not None else "connectome"))
            stamp = time.strftime("%Y%m%dT%H%M%S")
            folder = os.path.join(self.palmRoot(), f"conn-{label}_desc-{glm.contrast_label(self.design)}_{stamp}")
        os.makedirs(folder, exist_ok=True)
        spec = self.spec()
        designPath = spec.save(os.path.join(folder, "design.json"))
        args = ["--maps", mapsTsv, "--participants", self.participantsPath(), "--design", designPath, "--mask", self.maskPath(), "--out", folder, "--run-root", self.ctx.study.path]
        jobSpec = jobrunner.JobSpec("LesionNetworkMappingUI.cohort_contrast", args, title=_("PALM export"), heavy=True, kind=PALM_JOB, runRoot=os.path.dirname(folder),
                                    onEvent=self._onPalmEvent, onDone=self._onPalmDone, name="palmexport")
        self.palmFolder = folder
        self.palmJob = self.ctx.submit(jobSpec)
        self.palmProgress.setJob(self.palmJob)
        self.palmProgress.widget.show()
        self.palmLabel.hide()
        self.showPalmState()
        return self.palmJob

    def _onPalmEvent(self, event):
        try:
            from LesionNetworkMapping import updateProgressQuietly
        except ImportError:
            self.palmProgress.update()
            return
        updateProgressQuietly(self.palmProgress, event.get("event") != "progress")

    def _onPalmDone(self, job):
        self.palmProgress.update()
        if job.succeeded():
            checks = (job.doneEvent or {}).get("checks") or []
            warnings = [check for check in checks if check.get("level") == "warn"]
            text = "<div>" + html.escape(_("Exported for PALM and validated ({n} subjects): {folder}").format(n=(job.doneEvent or {}).get("n", "?"), folder=self.palmFolder)) + "</div>"
            text += "<div>" + html.escape(_("Run run_palm.sh there (PALM is not part of Slicer Studio), then Import PALM results.")) + "</div>"
            text += checksHtml(warnings)
            self.palmLabel.setText(text)
            self.message(_("PALM export written and validated"))
        elif job.state == "failed":
            self.palmLabel.setText(checksHtml([{"level": "block", "message": _("The PALM export failed: {reason}").format(reason=job.failureReason)}]))
        else:
            self.palmLabel.setText(html.escape(_("The PALM export was stopped.")))
        self.palmLabel.show()
        self.showPalmState()

    def onImportPalm(self):
        folders = self.palmFolders()
        start = folders[-1] if folders else (self.palmRoot() or "")
        folder = qt.QFileDialog.getExistingDirectory(slicer.util.mainWindow(), _("Choose a PALM export folder (or its out folder)"), start)
        if folder:
            self.importPalm(folder)

    def importPalm(self, folder, prefix="palm"):
        """Read PALM's outputs of ``folder`` (``palm.import_palm``), list them and show the statistic; return the import dict or None."""
        from LesionNetworkMappingLib import nifti, palm

        slicer.app.setOverrideCursor(qt.Qt.WaitCursor)
        try:
            imported = palm.import_palm(folder, prefix=prefix)
        except (OSError, ValueError) as exc:
            slicer.app.restoreOverrideCursor()
            self.palmLabel.setText(checksHtml([{"level": "block", "message": _("No PALM results read: {error}").format(error=exc)}]))
            self.palmLabel.show()
            return None
        try:
            rows = []
            files = imported["files"][min(imported["files"])]
            arrays = imported["maps"]
            stat = imported.get("stat") or "tstat"
            converted = os.path.join(os.path.dirname(imported["folder"]) if os.path.basename(imported["folder"]) == "out" else imported["folder"], "imported")
            for key in sorted(files, key=_palmOrder):
                kind, _sep, ptype = key.partition("_")
                path = files[key]
                isP = ptype.split("_")[0] in ("fwep", "cfwep", "fdrp", "cfdrp", "uncp")
                if isP and imported.get("logp"):
                    os.makedirs(converted, exist_ok=True)
                    header = nifti.read_nifti_header(path)
                    path = os.path.join(converted, f"{prefix}_{key}_p.nii.gz")
                    nifti.write_nifti(path, arrays[key].astype("float32"), header["affine"], sform_code=header["sform_code"] or nifti.XFORM_MNI, qform_code=header["qform_code"] or nifti.XFORM_MNI,
                                      descrip=f"PALM {key} as p (converted from -log10 p)")
                desc = "palm" + kind.capitalize() + ({"fwep": "PFwe", "cfwep": "ClusterPFwe", "fdrp": "PFdr", "cfdrp": "ClusterPFdr", "uncp": "PUnc"}.get(ptype.split("_")[0], stat.capitalize()) if ptype != "stat" else stat.capitalize())
                rows.append((desc, _("PALM {kind} {what}").format(kind=kind, what=ptype if ptype != "stat" else stat), path))
        finally:
            slicer.app.restoreOverrideCursor()
        self.palmImported = imported
        self.result = (imported["folder"], {"status": "done", "palm": True})
        self.setOutputs(rows)
        lines = [html.escape(_("PALM results: {folder}").format(folder=imported["folder"]))]
        if imported.get("command"):
            lines.append(html.escape(_("PALM command: {command}").format(command=imported["command"])))
        if imported.get("exported_command") and imported.get("command") and imported["exported_command"].split() != imported["command"].split():
            lines.append(checksHtml([{"level": "warn", "message": _("PALM ran another command than the exported run_palm.sh")}]))
        lines.append(html.escape(_("-log10(p) converted to p ({source}).").format(source=imported.get("logp_source")) if imported.get("logp") else _("p-values as written by PALM.")))
        self.summaryLabel.setText("".join(f"<div>{line}</div>" for line in lines))
        self.notesLabel.hide()
        self.results.show()
        if self.outputs:
            self.showMap(0)
        return imported

    def cleanup(self):
        if self.palmJob is not None and not self.palmJob.isFinished():
            self.palmJob.spec.onDone = None
            self.palmJob.spec.onEvent = None
        super().cleanup()


def contrastText(contrast, levels):
    """A contrast as read: "case > control", "control > case", "two-sided", "F (all levels)"."""
    a, b = (list(levels) + ["A", "B"])[:2]
    return {"A>B": f"{a} > {b}", "B>A": f"{b} > {a}", "two_sided": _("two-sided"), "F": _("F (all levels)")}.get(contrast, str(contrast))


def _palmOrder(key):
    kind, _sep, ptype = key.partition("_")
    return ({"vox": 0, "tfce": 1}.get(kind, 2), {"stat": 0, "fwep": 1, "fdrp": 2, "uncp": 3}.get(ptype.split("_")[0], 4), key)


def _fmtP(value):
    try:
        return f"{float(value):.3g}"
    except (TypeError, ValueError):
        return "?"


def _idClickedSignal(group):
    """The QButtonGroup signal carrying the clicked button's id: buttonClicked(int) is gone in Qt 6; idClicked(int) exists since Qt 5.15."""
    return "idClicked(int)" if hasattr(group, "idClicked") else "buttonClicked(int)"


def _fill(combo, items, select=None):
    """Replace the items of a combo box (text, data) without signals; keep the current text when it is still offered."""
    current = select if select is not None else combo.currentText
    texts = [text for text, _data in items]
    if [combo.itemText(index) for index in range(combo.count)] == texts and current == combo.currentText:
        return
    combo.blockSignals(True)
    try:
        combo.clear()
        for text, data in items:
            combo.addItem(text, data)
        if current in texts:
            combo.setCurrentIndex(texts.index(current))
    finally:
        combo.blockSignals(False)


def _glmStatusText(job):
    """Status chip text of a GLM job: "GLM 1200/5000 · 3 min left"."""
    from . import jobrunner

    event = job.lastEvent or {}
    text = _("GLM")
    if (job.stage or {}).get("name") == "permutations" and event.get("event") == "progress" and event.get("total"):
        text += f" {event.get('done')}/{event.get('total')}"
    elif job.progress is not None:
        text += f" {int(job.progress * 100)}%"
    eta = jobrunner.formatEta(job.eta)
    return f"{text} · {eta}" if eta else text


TAB = {
    "id": "contrast",
    "title": "Contrast",
    "order": ORDER_CONTRAST,
    "factory": ContrastTab,
}

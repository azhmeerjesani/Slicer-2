"""Run everything card of the Map module (order 35, Seed = Cohort; A-04c, LNM addendum §6.2 and §7.1).

One click runs the cohort pipeline of A-04 (``python -m LesionNetworkMappingLib.pipeline run``) for the study and the
connectome of the Seed and Connectome cards, as one heavy job of the application's ``JobRunner``:

- **Primary action.** The card registers ``MapContext.setPrimaryOverride("cohort", "Run everything · N lesions", …)``:
  with Seed = Cohort the blue Compute button and Ctrl+Enter (Studio) run everything, and the cohort maps alone become
  the secondary "Maps only" (D-A26). Nothing else gets a key; Stop is a button, never Esc.
- **Run.** The job gets ``--parent-pid``, ``--events json``, ``--nice 5``, ``--keep-awake``, ``--software`` and, only when
  ``Studio/LNM/AcceptedTerms/<sha>`` is true, ``--accept-terms``; the options of the dialog (``Studio/LNM/Pipeline/Options``)
  and ``Studio/LNM/MemoryBudgetGB``. The runner adds ``--cancel-file`` and ``--threads``. The open study session is
  flushed first (the pipeline reads participants.tsv). The click does no hashing: it reuses the last plan.
- **Progress.** Every pipeline job of the runner feeds a :class:`PipelineModel` (module level, so a card created later
  re-attaches to a running job); a 100 ms coalescing timer applies it to the card (nine stage rows, the
  estimate-weighted percent, the running row's batch and ETA). The runner shows "Cohort · Network maps 34% · 6 min
  left" in the Studio status chip.
- **Stop.** ``Job.stop()`` writes the cancel file; the runner terminates the worker it reported, then the launcher, when
  the job does not end (never only the launcher). The pipeline's parent and orphan watchdogs are the backstop, and the
  STOP file of the output folder is written when the application quits. Completed work is kept: the next run resumes.
- **Staleness and re-attach.** ``pipeline.plan(…, hash_lesions=True)`` runs in a worker thread (file reads and hashing;
  no MRML or Qt) when the card is shown, the study, connectome, rule or options change; the card then says "3 new or
  changed lesions · 37 up to date". A run of another process (the command line, a restarted Slicer) is found by the
  heartbeat of ``.pipeline/lock.json`` and followed through ``.pipeline/progress.json`` every 2 s.
- **Results.** Open report and Show folder (the system browser, else an in-app web viewer, else a message with the
  path), Load maps (at most 6 group maps, one per 30 ms tick, over the template), Warnings (a non-modal list).
- **Statuses.** When the run is done, the study session (the single writer of participants.tsv) merges the pipeline's
  inbox; the card never writes participants.tsv.

Search entries (``StudioLib.palette.registerProvider``): "Run cohort analysis (lesion network mapping)…", "Open last
cohort report" and "Show cohort analysis folder". Settings: writes ``Studio/LNM/Pipeline/Options`` only; reads
``Studio/LNM/AcceptedTerms/<sha>``, ``Studio/LNM/MaxJobThreads`` and ``Studio/LNM/MemoryBudgetGB``. Works in Classic
(the module panel) and without a main window. "Keep analysis up to date" is not part of this build (D-A22, backlog).
"""

import contextlib
import json
import logging
import math
import os
import queue
import re
import sys
import threading
import time

import qt
import slicer
from slicer.i18n import tr as _

from . import ORDER_RUN_EVERYTHING, jobrunner, widgets

#: The job module (A-04's command line)
PIPELINE_MODULE = "LesionNetworkMappingLib.pipeline"
#: Kind of the runner's pipeline jobs (``JobSpec.kind``)
JOB_KIND = "pipeline"
#: Fork-only settings (SPEC Appendix B, D-S1): the options of the dialog (JSON, written) and the memory budget (read only)
OPTIONS_SETTING = "Studio/LNM/Pipeline/Options"
MEMORY_BUDGET_SETTING = "Studio/LNM/MemoryBudgetGB"
#: Lower priority of the pipeline (POSIX nice; the pipeline applies it to itself and its children)
NICE = 5

#: The widgets are updated at most this often while events arrive (one coalescing single-shot timer)
APPLY_MS = 100
#: Quiet time after a change before the staleness check (the plan) runs
RECHECK_MS = 500
#: The plan thread's results are drained this often; while it runs, each tick lends it the interpreter for PUMP_SLEEP_S
PUMP_MS = 15
PUMP_SLEEP_S = 0.008
#: A run of another process is followed this often (progress.json) while the card is visible
EXTERNAL_POLL_MS = 2000
#: Load maps: one map per tick
LOAD_TICK_MS = 30
#: At most this many maps are loaded by Load maps
MAX_LOADED_MAPS = 6
#: Inspector width the card must fit (SPEC 7.9)
CARD_WIDTH = 340
#: The options dialog is at most this wide
DIALOG_MAX_WIDTH = 420

DEFAULT_OPTIONS = {
    "preview_n": None,
    "preview_seed": 0,
    "extra_rules": [],
    "sensitivity": 90.0,
    "specificity": "auto",
    "volume_off_reason": None,
    "perm": 5000,
    "tfce": False,
    "scores": "auto",
    "null_relocations": "auto",
    "nulls": "auto",
    "build_references": False,
    "native": "normalize",
    "atlas": "none",
    "threads": None,
    "keep_awake": True,
}
#: Options of the dialog that are also accepted from the stored JSON (tests); others are ignored
EXTRA_OPTION_KEYS = ("memory_budget_gb",)
PREVIEW_N = 100
#: Extra rules offered by the dialog: (key, label, rule)
EXTRA_RULES = (
    ("fwe", "FWE (Bonferroni)", {"kind": "fwe_bonferroni", "alpha": 0.05}),
    ("t7", "|T| ≥ 7", {"kind": "t_convention", "value": 7.0, "reference_n": 1000}),
    ("fz", "Fz ≥ 0.05", {"kind": "fz_abs", "value": 0.05}),
    ("top10", "Top 10%", {"kind": "top_pct", "value": 10.0, "statistic": "Fz", "sign": "both"}),
)
SPECIFICITY_CHOICES = (("auto", "GLM case vs control"), ("legacy-welch", "Legacy Welch"), ("legacy-liebermeister", "Legacy Liebermeister"), ("off", "Off"))
NULL_CHOICES = (("auto", "Auto"), ("exact", "Exact"), ("fast", "Fast"), ("off", "Off"))
NATIVE_CHOICES = (("normalize", "Normalize (results need review in Library)"), ("skip", "Skip"), ("include-unreviewed", "Include unreviewed (recorded override)"))
#: The atlas of the cluster labels offered by the dialog (L-15 catalog id; downloaded only on an explicit click)
SCHAEFER_ID = "schaefer2018-400-7n-2mm"
ATLAS_CHOICES = (("none", "None"), ("schaefer400", "Schaefer-400 (7 networks)"))
#: Minimum subjects per group for the GLM (the pipeline's MIN_PER_GROUP)
MIN_PER_GROUP = 3
#: The |T| >= 7 convention needs N close to 1000 (thresholds.T_CONVENTION_MIN_N)
T_CONVENTION_MIN_N = 900

#: The nine rows of the stage list: (id, label, pipeline stages)
ROWS = (
    ("prepare", "Prepare", ("prepare", "preflight")),
    ("normalize", "Normalize", ("normalize",)),
    ("reference", "Reference maps", ("reference",)),
    ("maps", "Network maps", ("maps",)),
    ("nulls", "Null models", ("nulls",)),
    ("statistics", "Statistics", ("overlap", "lesionset", "specificity", "slnm", "conjunction")),
    ("qa", "QA & clusters", ("qa", "clusters")),
    ("export", "Export", ("export",)),
    ("report", "Report", ("report", "finish")),
)
GLYPHS = {"pending": "○", "running": "●", "done": "✓", "reused": "↺", "skipped": "–", "failed": "!"}
GLYPH_LEVELS = {"pending": "text3", "running": "accent", "done": "ok", "reused": "text2", "skipped": "text3", "failed": "danger"}
#: Stage statuses of the pipeline (events, pipeline.json, plan) and the row status they count as
STAGE_STATUS = {"running": "running", "done": "done", "reused": "reused", "reuse": "reused", "skipped": "skipped", "skip": "skipped", "unavailable": "skipped",
                "failed": "failed", "blocked": "failed", "planned": "pending", "pending": "pending", "cancelled": "pending"}
FINISHED_ROW = ("done", "reused", "skipped")

_IS_LINUX = sys.platform.startswith("linux")


#
# Small helpers
#


def _mapWidget():
    return getattr(slicer.modules, "LesionNetworkMappingWidget", None)


def _pipeline():
    from LesionNetworkMappingLib import pipeline

    return pipeline


def _readJson(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _samePath(a, b):
    if not a or not b:
        return False
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def formatDuration(seconds):
    """"< 1 min", "12 min", "1 h 20 min" ("" when unknown)."""
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return ""
    if math.isnan(seconds) or seconds < 0:
        return ""
    if seconds < 60:
        return _("< 1 min")
    minutes = round(seconds / 60)
    if minutes < 60:
        return _("{n} min").format(n=max(1, minutes))
    hours, minutes = int(seconds // 3600), round(seconds % 3600 / 60)
    if minutes == 60:
        hours, minutes = hours + 1, 0
    return _("{h} h {m} min").format(h=hours, m=minutes) if minutes else _("{h} h").format(h=hours)


def formatAge(isoText, now=None):
    """"2 h ago", "5 min ago", "just now" of an ISO time ("" when it cannot be read)."""
    import datetime

    try:
        when = datetime.datetime.fromisoformat(str(isoText).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.UTC)
    seconds = ((now or datetime.datetime.now(datetime.UTC)) - when).total_seconds()
    if seconds < 60:
        return _("just now")
    if seconds < 3600:
        return _("{n} min ago").format(n=int(seconds // 60))
    if seconds < 48 * 3600:
        return _("{n} h ago").format(n=int(seconds // 3600))
    return _("{n} days ago").format(n=int(seconds // 86400))


def _plural(n, one, many, **fields):
    """The singular or plural form of a translatable text with ``{n}`` (and other ``fields``)."""
    return _(one if n == 1 else many).format(n=n, **fields)


def _levelColor(level):
    """The text colour of a level: ok, warn, danger, accent, text2, text3 or text (the application palette)."""
    palette = slicer.app.palette()
    if level in ("ok", "warn", "danger"):
        return qt.QColor(widgets._paletteChipColors(level)[1])
    if level == "accent":
        return palette.color(qt.QPalette.Active, qt.QPalette.Highlight)
    if level == "text3":
        return palette.color(qt.QPalette.Disabled, qt.QPalette.WindowText)
    text = palette.color(qt.QPalette.Active, qt.QPalette.WindowText)
    if level == "text2":
        window = palette.color(qt.QPalette.Active, qt.QPalette.Window)
        return qt.QColor((2 * text.red() + window.red()) // 3, (2 * text.green() + window.green()) // 3, (2 * text.blue() + window.blue()) // 3)
    return text


def _setLevel(label, level):
    """Colour a label's text by level through its palette (no stylesheet); nothing happens when it is unchanged."""
    key = f"{level}:{slicer.app.palette().cacheKey()}"
    if label.property("lnmLevel") == key:
        return
    label.setProperty("lnmLevel", key)
    palette = qt.QPalette(label.palette)
    palette.setColor(qt.QPalette.WindowText, _levelColor(level))
    label.setPalette(palette)


def _setText(widget, text):
    """``setText`` only when the text changed (no relayout for an unchanged text)."""
    text = text or ""
    if str(widget.text) != text:
        widget.setText(text)
        return True
    return False


def _setToolTip(widget, text):
    text = text or ""
    if str(widget.toolTip) != text:
        widget.setToolTip(text)


def _setVisible(widget, visible):
    if widget.isHidden() == bool(visible):
        widget.setVisible(bool(visible))


def _lucideIcon(name, size=16, color=None):
    """A Lucide icon of the Studio module (``Resources/Icons/Lucide/<name>.svg``) tinted with ``color``, or None.

    Rendered at the device pixel ratio; without the Studio module (or its icons) the buttons stay text-only.
    """
    module = getattr(slicer.modules, "studio", None)
    path = getattr(module, "path", "") if module is not None else ""
    if not path:
        return None
    svg = os.path.join(os.path.dirname(path), "Resources", "Icons", "Lucide", f"{name}.svg")
    if not os.path.isfile(svg):
        return None
    ratio = max(1.0, float(slicer.app.devicePixelRatio()))
    reader = qt.QImageReader(svg)
    reader.setScaledSize(qt.QSize(round(size * ratio), round(size * ratio)))
    image = reader.read()
    if image.isNull():
        return None
    image = image.convertToFormat(qt.QImage.Format_ARGB32_Premultiplied)
    painter = qt.QPainter(image)
    painter.setCompositionMode(qt.QPainter.CompositionMode_SourceIn)
    painter.fillRect(image.rect(), color if color is not None else slicer.app.palette().color(qt.QPalette.Active, qt.QPalette.ButtonText))
    painter.end()
    pixmap = qt.QPixmap.fromImage(image)
    pixmap.setDevicePixelRatio(ratio)
    return qt.QIcon(pixmap)


def _studySession(studyPath):
    """The Studio study session (``StudioLib.study_ui``) when it holds the study at ``studyPath``, else None."""
    try:
        from StudioLib import study_ui
    except Exception:
        return None
    try:
        session = study_ui.session()
    except Exception:
        return None
    if session is None or not _samePath(getattr(session.study, "path", None), studyPath):
        return None
    return session


def _toast(text, ms=3000):
    shell = jobrunner._studioShell()
    if shell is None:
        logging.info(f"Lesion network mapping: {text}")
        return
    try:
        shell.toast(text, ms)
    except Exception:
        logging.exception("Lesion network mapping: the toast could not be shown")


def termsAccepted(bundle):
    """Whether the data use terms of ``bundle`` were accepted (``Studio/LNM/AcceptedTerms/<manifest sha256>``; read only)."""
    if bundle is None:
        return False
    try:
        from LesionNetworkMapping import LesionNetworkMappingLogic

        return bool(LesionNetworkMappingLogic.termsAccepted(bundle))
    except Exception:
        logging.exception("Lesion network mapping: the data use terms could not be checked")
        return False


def memoryBudgetSetting():
    """The positive number of ``Studio/LNM/MemoryBudgetGB`` (GB), or None (read only, never written)."""
    try:
        value = float(str(qt.QSettings().value(MEMORY_BUDGET_SETTING)).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 and math.isfinite(value) else None


def loadOptions():
    """The options of the dialog (``Studio/LNM/Pipeline/Options``), with the defaults filled in."""
    options = dict(DEFAULT_OPTIONS)
    text = qt.QSettings().value(OPTIONS_SETTING)
    if text:
        try:
            stored = json.loads(str(text))
        except ValueError:
            stored = {}
        if isinstance(stored, dict):
            options.update({key: value for key, value in stored.items() if key in DEFAULT_OPTIONS or key in EXTRA_OPTION_KEYS})
    return options


def saveOptions(options):
    """Store the options as JSON in ``Studio/LNM/Pipeline/Options`` (the only setting this card writes)."""
    kept = {key: value for key, value in options.items() if key in DEFAULT_OPTIONS or key in EXTRA_OPTION_KEYS}
    qt.QSettings().setValue(OPTIONS_SETTING, json.dumps(kept, sort_keys=True))


def _atlasPaths(choice):
    """(atlas, labels) paths of the chosen atlas when it is cached (never downloads), else (None, None)."""
    if choice != "schaefer400":
        return None, None
    try:
        from LesionNetworkMappingLib import fetch

        resource = fetch.find_resource(SCHAEFER_ID)
        path = fetch.cached_path(resource)
        labels = fetch.label_table_path(resource) if path else None
    except Exception:
        return None, None
    return (path, labels) if path else (None, None)


def configDict(studyPath, bundlePath, options, rule=None, software=None, acceptTerms=False, parentPid=None, recompute=False):
    """The ``pipeline.PipelineConfig`` fields of a run (also what the staleness plan uses)."""
    config = {"study": studyPath, "bundle": bundlePath, "rule": rule, "extra_rules": list(options.get("extra_rules") or []),
              "sensitivity": float(options.get("sensitivity", 90.0)), "specificity": options.get("specificity") or "auto",
              "perm": int(options.get("perm") or 5000), "tfce": bool(options.get("tfce")), "scores": options.get("scores", "auto"),
              "null_relocations": options.get("null_relocations", "auto"), "nulls": options.get("nulls") or "auto",
              "build_references": bool(options.get("build_references")), "native": options.get("native") or "normalize",
              "recompute": bool(recompute), "accept_terms": bool(acceptTerms), "nice": NICE,
              "keep_awake": "auto" if options.get("keep_awake", True) else "off", "events": "json"}
    if options.get("preview_n"):
        config["preview_n"] = int(options["preview_n"])
        config["preview_seed"] = int(options.get("preview_seed") or 0)
    reason = (options.get("volume_off_reason") or "").strip()
    if reason:
        config["volume_off_reason"] = reason
        config["covariates"] = []
    if config["native"] == "include-unreviewed":
        config["include_unreviewed"] = True
    atlas, labels = _atlasPaths(options.get("atlas"))
    if atlas:
        config["atlas"] = atlas
        if labels:
            config["atlas_labels"] = labels
    budget = options.get("memory_budget_gb") or memoryBudgetSetting()
    if budget:
        config["memory_budget_gb"] = float(budget)
    if options.get("threads"):
        config["threads"] = int(options["threads"])
    if software:
        config["software"] = dict(software)
    if parentPid:
        config["parent_pid"] = int(parentPid)
    return config


def commandArguments(config):
    """``pipeline run`` arguments of a :func:`configDict` (the runner adds --cancel-file and --threads)."""
    args = ["run", "--study", config["study"], "--bundle", config["bundle"], "--events", config.get("events", "json")]
    if config.get("parent_pid"):
        args += ["--parent-pid", str(config["parent_pid"])]
    args += ["--nice", str(config.get("nice", NICE)), "--keep-awake", config.get("keep_awake", "auto")]
    if config.get("software"):
        args += ["--software", json.dumps(config["software"], sort_keys=True)]
    if config.get("accept_terms"):
        args.append("--accept-terms")
    if config.get("rule"):
        args += ["--rule", json.dumps(config["rule"], sort_keys=True)]
    for rule in config.get("extra_rules") or []:
        args += ["--extra-rule", json.dumps(rule, sort_keys=True)]
    if config.get("preview_n"):
        args += ["--preview", str(config["preview_n"]), "--preview-seed", str(config.get("preview_seed", 0))]
    args += ["--sensitivity", f"{float(config.get('sensitivity', 90.0)):g}", "--specificity", config.get("specificity", "auto"), "--perm", str(config.get("perm", 5000))]
    if config.get("tfce"):
        args.append("--tfce")
    if config.get("volume_off_reason"):
        args += ["--no-volume-covariate", config["volume_off_reason"]]
    scores = config.get("scores", "auto")
    args += ["--scores", ",".join(scores) if isinstance(scores, (list, tuple)) else str(scores or "auto")]
    args += ["--null-relocations", str(config.get("null_relocations", "auto")), "--nulls", config.get("nulls", "auto"), "--native", config.get("native", "normalize")]
    if config.get("include_unreviewed"):
        args.append("--include-unreviewed")
    if config.get("build_references"):
        args.append("--build-references")
    if config.get("atlas"):
        args += ["--atlas", config["atlas"]]
        if config.get("atlas_labels"):
            args += ["--atlas-labels", config["atlas_labels"]]
    if config.get("memory_budget_gb"):
        args += ["--memory-budget-gb", f"{float(config['memory_budget_gb']):g}"]
    if config.get("recompute"):
        args.append("--recompute")
    return args


def planKey(config):
    """What the staleness plan depends on (the analysis settings, not the run options)."""
    ignored = ("accept_terms", "parent_pid", "software", "events", "nice", "keep_awake", "threads", "recompute")
    return json.dumps({key: value for key, value in config.items() if key not in ignored}, sort_keys=True, default=str)


def _localeEnvironment(args):
    """On Linux under a non-UTF-8 locale, the PythonSlicer launcher drops non-ASCII arguments: run it under C.UTF-8."""
    if not _IS_LINUX or all(str(arg).isascii() for arg in args):
        return {}
    for name in ("LC_ALL", "LC_CTYPE", "LANG"):
        value = os.environ.get(name)
        if value:
            return {} if "utf-8" in value.lower() or "utf8" in value.lower() else {"LC_ALL": "C.UTF-8"}
    return {"LC_ALL": "C.UTF-8"}


#
# Plan worker
#


def summarizePlan(result):
    """The plain summary of a ``pipeline.Plan`` that the card shows (built in the worker thread)."""
    pipeline = _pipeline()
    counts = {status: result.count(status) for status in ("compute", "reuse", "normalize", "excluded")}
    groups = {}
    for row in result.rows:
        if row["status"] in ("compute", "reuse", "normalize"):
            groups[row["group"]] = groups.get(row["group"], 0) + 1
    batch = pipeline.engine_batch_size(result.bundle.n_voxels, result.budget_bytes)
    toMap = counts["compute"] + counts["normalize"]
    return {
        "out": result.out,
        "counts": counts,
        "eligible": counts["compute"] + counts["reuse"] + counts["normalize"],
        "groups": groups,
        "review": sum(1 for row in result.rows if row.get("reason") == pipeline.NORM_NEEDS_REVIEW),
        "estimate_s": float(result.estimate_s),
        "passes": int(math.ceil(toMap / batch)) if toMap else 0,
        "bundle": result.bundle.name,
        "n_subjects": result.n_subjects,
        "blocks": [dict(block) for block in result.blocks],
        "stages": [dict(stage) for stage in result.stages],
        "exit_code": result.exit_code,
        "scores": list(result.scores),
        "elapsed": float(result.elapsed),
    }


class PlanWorker:
    """Runs ``pipeline.plan`` in a background thread and hands the summaries to ``callback(key, summary, error)`` on the main thread.

    Python threads only run while the main thread runs Python; while the plan runs, a 15 ms timer drains the result
    queue and sleeps a few milliseconds, so the plan progresses without freezing the GUI (D19). One plan at a time:
    a request made while one runs replaces the pending one.
    """

    def __init__(self, callback):
        self.callback = callback
        self._queue = queue.Queue()
        self._thread = None
        self._pending = None
        self._timer = qt.QTimer()
        self._timer.setInterval(PUMP_MS)
        self._timer.connect("timeout()", self._pump)

    def isBusy(self):
        return self._thread is not None and self._thread.is_alive()

    def request(self, key, config):
        if self.isBusy():
            self._pending = (key, config)
            return
        self._pending = None
        self._thread = threading.Thread(target=self._work, args=(key, dict(config), self._queue), name="lnm-pipeline-plan", daemon=True)
        self._thread.start()
        self._timer.start()

    @staticmethod
    def _work(key, config, results):
        try:
            pipeline = _pipeline()
            result = pipeline.plan(pipeline.PipelineConfig.from_json(config), hash_lesions=True)
            results.put((key, summarizePlan(result), None))
        except Exception as exc:
            results.put((key, None, f"{type(exc).__name__}: {exc}"))

    def _pump(self):
        while True:
            try:
                key, summary, error = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                self.callback(key, summary, error)
            except Exception:
                logging.exception("Lesion network mapping: the plan result could not be shown")
        if self.isBusy():
            time.sleep(PUMP_SLEEP_S)  # lends the interpreter to the plan thread
            return
        if self._pending is not None:
            key, config = self._pending
            self.request(key, config)
            return
        if self._queue.empty():
            self._timer.stop()

    def waitForIdle(self, timeoutS=30.0):
        """Process events until no plan runs or waits (tests)."""
        deadline = time.monotonic() + timeoutS
        while (self.isBusy() or self._pending is not None or not self._queue.empty()) and time.monotonic() < deadline:
            slicer.app.processEvents()
            time.sleep(0.005)
        slicer.app.processEvents()
        return not self.isBusy()

    def stop(self):
        self._pending = None
        self._timer.stop()


#
# Run state (one model per pipeline job of the runner)
#


class PipelineModel:
    """What the events of one pipeline job say: stages, progress, warnings and the outcome (see the module docstring)."""

    def __init__(self, job=None):
        self.job = job
        self.state = "queued"
        self.startEvent = None
        self.stages = []  # [{name, label, status, estimate_s}] of the start event
        self.status = {}  # stage name: status
        self.reasons = {}  # stage name: reason
        self.current = None
        self.progress = {}  # the last progress fields of the current stage
        self.warnings = []
        self.infos = []
        self.doneEvent = None
        self.cancelledEvent = None
        self.errorEvent = None
        self.queuedFor = None
        self.version = 0
        self.finishedAt = None

    @property
    def out(self):
        return getattr(getattr(self.job, "spec", None), "lnmOut", None) or (self.startEvent or {}).get("out")

    def isActive(self):
        return self.state in ("queued", "running")

    def apply(self, event):
        kind = event.get("event")
        if kind == "queued":
            self.state = "queued"
            self.queuedFor = event.get("waitingFor")
        elif kind == "start":
            self.state = "running"
            self.startEvent = event
            self.stages = [dict(stage) for stage in event.get("stages") or [] if isinstance(stage, dict)]
            for stage in self.stages:
                self.status.setdefault(stage.get("name"), stage.get("status") or "planned")
                if stage.get("status") in ("skip", "unavailable", "reuse"):
                    self.reasons.setdefault(stage.get("name"), None)
        elif kind == "stage":
            name = event.get("name") or event.get("stage")
            status = event.get("status")
            self.status[name] = status
            if event.get("reason"):
                self.reasons[name] = event.get("reason")
            if status == "running":
                self.current, self.progress = name, {}
            elif name == self.current:
                self.progress = {}
            self.state = "running" if self.state == "queued" else self.state
        elif kind == "progress":
            stage = event.get("stage")
            if stage and stage != self.current and self.status.get(stage) != "running":
                self.current = stage
            self.progress = {key: value for key, value in event.items() if key not in ("event", "t")}
        elif kind == "warning":
            self.warnings.append(event)
        elif kind == "info":
            self.infos.append(event)
        elif kind == "done":
            self.doneEvent = event
        elif kind == "cancelled":
            self.cancelledEvent = event
        elif kind == "error":
            self.errorEvent = event
        self.version += 1

    def finish(self, job):
        """The job ended (``job.state``: done, failed or cancelled)."""
        self.state = job.state
        if self.current and self.status.get(self.current) == "running":
            self.status[self.current] = "failed" if job.state == "failed" else "cancelled"
        self.finishedAt = time.time()
        self.version += 1

    def weights(self):
        """Estimate of each stage (s, at least 0.1): the weights of the overall percent (as in the pipeline's progress.json)."""
        return {stage.get("name"): max(float(stage.get("estimate_s") or 0.0), 0.1) for stage in self.stages}

    def fraction(self):
        """Progress of the current stage (0..1) from its last progress event, or None."""
        done, total = self.progress.get("done"), self.progress.get("total")
        try:
            return min(max(float(done) / float(total), 0.0), 1.0) if total else None
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    def percent(self):
        """Estimate-weighted percent of the whole run (0-100)."""
        if self.state == "done":
            return 100.0
        weights = self.weights()
        total = sum(weights.values())
        if not total:
            return 0.0
        value = sum(weight for name, weight in weights.items() if STAGE_STATUS.get(self.status.get(name)) in FINISHED_ROW)
        fraction = self.fraction()
        if fraction is not None and self.current in weights and STAGE_STATUS.get(self.status.get(self.current)) not in FINISHED_ROW:
            value += weights[self.current] * fraction
        return min(100.0, 100.0 * value / total)

    def eta(self):
        """Seconds left: the current stage's own estimate, then the planned estimates of the stages after it; None when unknown."""
        if not self.stages:
            return None
        names = [stage.get("name") for stage in self.stages]
        weights = {stage.get("name"): float(stage.get("estimate_s") or 0.0) for stage in self.stages}
        current = self.progress.get("eta")
        try:
            current = float(current) if current is not None else None
        except (TypeError, ValueError):
            current = None
        if current is None and self.current in weights:
            fraction = self.fraction() or 0.0
            current = weights[self.current] * (1.0 - fraction)
        later = 0.0
        if self.current in names:
            for name in names[names.index(self.current) + 1:]:
                if STAGE_STATUS.get(self.status.get(name)) not in FINISHED_ROW:
                    later += weights.get(name, 0.0)
        return (current or 0.0) + later

    def currentLabel(self):
        return rowLabelOf(self.current) if self.current else _("Prepare")

    def statusText(self):
        """The Studio status chip: "Cohort · Network maps 34% · 6 min left"."""
        if self.state == "queued":
            return _("Cohort · waiting for {job}").format(job=self.queuedFor) if self.queuedFor else _("Cohort · waiting")
        text = _("Cohort · {stage} {percent}%").format(stage=self.currentLabel(), percent=int(self.percent()))
        eta = jobrunner.formatEta(self.eta())
        return f"{text} · {eta}" if eta else text

    def detail(self):
        """The running row's detail: "batch 1/2 · 312/1000 subjects · ~6 min left"."""
        fields = self.progress
        parts = []
        if fields.get("batch") and fields.get("batches") and int(fields.get("batches") or 0) > 1:
            parts.append(_("batch {b}/{n}").format(b=fields["batch"], n=fields["batches"]))
        if fields.get("subject") is not None and fields.get("subjects"):
            parts.append(_("{d}/{n} subjects").format(d=fields["subject"], n=fields["subjects"]))
        elif fields.get("done") is not None and fields.get("total"):
            unit = str(fields.get("unit") or "")
            parts.append(f"{fields['done']}/{fields['total']}" + (f" {unit}" if unit and unit != "units" else ""))
        eta = fields.get("eta")
        if eta is not None:
            text = formatDuration(eta)
            if text:
                parts.append(_("~{eta} left").format(eta=text))
        return " · ".join(parts)


def rowOf(stage):
    """The row id of a pipeline stage."""
    for rowId, _label, stages in ROWS:
        if stage in stages:
            return rowId
    return None


def rowLabelOf(stage):
    rowId = rowOf(stage)
    return next((_(label) for key, label, _stages in ROWS if key == rowId), stage or "")


def rowStatus(statuses, active=False):
    """The status of a row from the statuses of its stages (running, failed, done, reused, skipped or pending)."""
    mapped = [STAGE_STATUS.get(status, "pending") for status in statuses]
    if not mapped:
        return "pending"
    if "failed" in mapped:
        return "failed"
    if "running" in mapped:
        return "running"
    if all(status in FINISHED_ROW for status in mapped):
        if "done" in mapped:
            return "done"
        return "reused" if "reused" in mapped else "skipped"
    if active and any(status in FINISHED_ROW for status in mapped):
        return "running"
    return "pending"


_models = {}  # job id: PipelineModel
_cards = []  # the live cards (refreshed when a model changes)


def modelFor(job):
    """The model of a pipeline job (created on first use)."""
    model = _models.get(job.id)
    if model is None:
        model = _models[job.id] = PipelineModel(job)
        model.apply({"event": "queued"} if job.isQueued() else {"event": "stage", "name": "prepare", "status": "running"})
        if job.startEvent:
            model.apply(job.startEvent)
    return model


def _isPipelineJob(job):
    return getattr(getattr(job, "spec", None), "kind", None) == JOB_KIND


def _notifyCards():
    for card in list(_cards):
        card.scheduleApply()


def _onJobEvent(job, event):
    if _isPipelineJob(job):
        modelFor(job).apply(event)
        _notifyCards()


def _onJobStarted(job):
    if _isPipelineJob(job):
        modelFor(job)
        _notifyCards()


def _onJobFinished(job):
    if not _isPipelineJob(job):
        return
    model = modelFor(job)
    model.finish(job)
    if job.state == jobrunner.DONE:
        _afterDone(job, model)
    for card in list(_cards):
        card.onJobFinished(job)


def _afterDone(job, model):
    """The run is done: the study session merges the pipeline's inbox (main thread) and a toast says so."""
    studyPath = getattr(job.spec, "lnmStudy", None)
    session = _studySession(studyPath)
    model.inboxApplied = False
    if session is not None:
        try:
            session.applyInbox()
            model.inboxApplied = True
        except Exception:
            logging.exception("Lesion network mapping: the study inbox could not be merged")
    report = (model.doneEvent or {}).get("report")
    _toast(_("Cohort analysis finished · report ready") if report else _("Cohort analysis finished"), 4000)


def _hookRunner():
    """Follow every pipeline job of the application's runner (once; a reloaded module replaces its earlier hooks)."""
    runner = jobrunner.instance()
    for listName, callback in (("jobEvent", _onJobEvent), ("jobStarted", _onJobStarted), ("jobFinished", _onJobFinished)):
        callbacks = getattr(runner, listName)
        for existing in list(callbacks):
            if getattr(existing, "__name__", None) == callback.__name__ and getattr(existing, "__module__", None) == __name__ and existing is not callback:
                callbacks.remove(existing)
        if callback not in callbacks:
            callbacks.append(callback)
    return runner


def pipelineJobs(out=None):
    """Running or queued pipeline jobs of the runner (for one output folder when ``out`` is given)."""
    runner = jobrunner.instance()
    jobs = [job for job in runner.queuedJobs() + runner.runningJobs() if _isPipelineJob(job)]
    if out:
        jobs = [job for job in jobs if _samePath(getattr(job.spec, "lnmOut", None), out)]
    return jobs


#
# Opening files
#


def _openUrl(url):
    """Open a URL with the desktop (the system browser or file manager); return whether it was handed over. Tests patch it."""
    return bool(qt.QDesktopServices.openUrl(url))


def _webBuild():
    """The web/Docker build: the system browser of the container cannot be seen (``/.dockerenv`` is only a hint)."""
    return os.environ.get("SLICER_STUDIO_WEB") == "1" or os.path.exists("/.dockerenv")


class _PathDialog:
    """A non-modal message with a path and [Copy path] (the last fallback of Open report and Show folder)."""

    def __init__(self, title, path):
        self.dialog = qt.QDialog(slicer.util.mainWindow())
        self.dialog.setObjectName("LNMPipelinePathDialog")
        self.dialog.setWindowTitle(title)
        self.dialog.setModal(False)
        layout = qt.QVBoxLayout(self.dialog)
        label = qt.QLabel(_("This build cannot open it here. Its location:"))
        label.setWordWrap(True)
        layout.addWidget(label)
        field = qt.QLineEdit(path)
        field.setObjectName("LNMPipelinePathField")
        field.setReadOnly(True)
        layout.addWidget(field)
        buttons = qt.QHBoxLayout()
        buttons.addStretch(1)
        copy = qt.QPushButton(_("Copy path"))
        copy.setObjectName("LNMPipelineCopyPathButton")
        copy.connect("clicked()", lambda: qt.QApplication.clipboard().setText(path))
        buttons.addWidget(copy)
        close = qt.QPushButton(_("Close"))
        close.connect("clicked()", self.dialog.close)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.dialog.setMaximumWidth(DIALOG_MAX_WIDTH)
        self.dialog.show()


class _WebViewer:
    """A non-modal in-app viewer (``slicer.qSlicerWebWidget``) of a local file or folder."""

    def __init__(self, title, path):
        self.dialog = qt.QDialog(slicer.util.mainWindow())
        self.dialog.setObjectName("LNMPipelineReportViewer")
        self.dialog.setWindowTitle(title)
        self.dialog.setModal(False)
        layout = qt.QVBoxLayout(self.dialog)
        layout.setContentsMargins(0, 0, 0, 0)
        self.web = slicer.qSlicerWebWidget()
        self.web.setObjectName("LNMPipelineReportWebWidget")
        layout.addWidget(self.web)
        self.web.setUrl(qt.QUrl.fromLocalFile(path).toString())
        mainWindow = slicer.util.mainWindow()
        if mainWindow is not None:
            self.dialog.resize(min(1100, int(mainWindow.width * 0.8)), min(800, int(mainWindow.height * 0.85)))
        self.dialog.show()


def openPath(path, title, viewers=None):
    """Open a file or folder: the desktop, else (or in the web build) the in-app viewer, else the path message.

    Return "desktop", "viewer" or "dialog"; the viewer or dialog object is appended to ``viewers`` (kept alive).
    """
    if not _webBuild() and _openUrl(qt.QUrl.fromLocalFile(path)):
        return "desktop"
    if hasattr(slicer, "qSlicerWebWidget"):
        try:
            viewer = _WebViewer(title, path)
            if viewers is not None:
                viewers.append(viewer)
            return "viewer"
        except Exception:
            logging.exception("Lesion network mapping: the in-app viewer could not be opened")
    dialog = _PathDialog(title, path)
    if viewers is not None:
        viewers.append(dialog)
    return "dialog"


#
# Load maps
#


def headlineMaps(record, alpha=0.05):
    """The group maps that Load maps shows, in order: [(desc, path relative to the output folder, kind, mask)].

    conjPos, conjNeg; the GLM case > control T map masked by its PFwe map (else meanT); sens<k>Pos; overlapPctPos; the first
    sLNM R map. ``kind`` is "stat" (T-like, the diverging table), "percent" or "binary"; ``mask`` is (p map path, alpha) or None.
    """
    group = [entry for entry in ((record or {}).get("outputs") or {}).get("group") or [] if isinstance(entry, dict) and entry.get("desc") and entry.get("path")]
    byDesc = {entry["desc"]: entry for entry in group}
    chosen = []

    def add(desc, kind, mask=None):
        if desc in byDesc and all(item[0] != desc for item in chosen):
            chosen.append((desc, byDesc[desc]["path"], kind, mask))

    add("conjPos", "binary")
    add("conjNeg", "binary")
    glm = next((desc for desc in byDesc if desc.endswith("Tstat") and not desc.startswith(("slnm", "sens"))), None)
    if glm is not None:
        pfwe = glm[: -len("Tstat")] + "PFwe"
        add(glm, "stat", (byDesc[pfwe]["path"], alpha) if pfwe in byDesc else None)
    else:
        add("meanT", "stat")
    sens = sorted(desc for desc in byDesc if re.fullmatch(r"sens\d+Pos", desc))
    if sens:
        add(sens[0], "binary")
    add("overlapPctPos", "percent")
    slnm = sorted(desc for desc in byDesc if desc.startswith("slnm") and desc.endswith("R"))
    if slnm:
        add(slnm[0], "stat")
    return chosen[:MAX_LOADED_MAPS]


#
# The card
#


class _VisibilityFilter(qt.QObject):
    """Calls ``callback()`` when the watched widget is shown."""

    def __init__(self, widget, callback):
        qt.QObject.__init__(self, widget)
        self.callback = callback
        widget.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.Show:
            self.callback()
        return False


class PipelineCard:
    """Controller of the Run everything card (``widget`` is the card's content; the Map widget wraps it in a card)."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.job = None  # the pipeline job this card shows (started here, or adopted)
        self.external = None  # pipeline.status() of a run of another process
        self.plan = None  # summarizePlan() of the last staleness check
        self.planKey = None
        self.planError = None
        self.planAt = 0.0
        self.record = None  # pipeline.json of the output folder
        self.recordStatus = None  # pipeline.status() of the output folder
        self.note = ""  # a second line (unsaved Draw edits, statuses not merged)
        self.viewers = []
        self.optionsDialog = None
        self.warningsDialog = None
        self.mapQueue = []
        self.loadedVolumes = []
        self._loadStack = None
        self._lastLoadTemplate = None
        self._publishedPrimary = None
        self._computeBlocked = False
        self._appliedVersion = None
        self._quitConnected = False
        self.applyCount = 0
        self.applySeconds = 0.0
        self.worker = PlanWorker(self._onPlan)
        self._buildWidget()
        self.applyTimer = qt.QTimer()
        self.applyTimer.setSingleShot(True)
        self.applyTimer.setInterval(APPLY_MS)
        self.applyTimer.connect("timeout()", self.apply)
        self.recheckTimer = qt.QTimer()
        self.recheckTimer.setSingleShot(True)
        self.recheckTimer.setInterval(RECHECK_MS)
        self.recheckTimer.connect("timeout()", self.recheck)
        self.externalTimer = qt.QTimer()
        self.externalTimer.setInterval(EXTERNAL_POLL_MS)
        self.externalTimer.connect("timeout()", self._pollExternal)
        self.loadTimer = qt.QTimer()
        self.loadTimer.setSingleShot(True)
        self.loadTimer.setInterval(LOAD_TICK_MS)
        self.loadTimer.connect("timeout()", self._loadNext)
        self._visibility = _VisibilityFilter(self.widget, self.scheduleRecheck)
        for name in ("studyChanged", "bundleChanged", "ruleChanged"):
            getattr(ctx, name).append(self.scheduleRecheck)
        ctx.computeEnabledChanged.append(self.scheduleApply)
        _hookRunner()
        _cards.append(self)
        self._connectQuit()
        widget = _mapWidget()
        terms = getattr(widget, "acceptTermsCheckBox", None) if widget is not None else None
        if terms is not None:
            terms.connect("toggled(bool)", lambda checked: self.scheduleApply())
        self.adoptRunningJob()
        self.apply()

    #
    # Widgets
    #

    def _label(self, objectName, role=None, wrap=True):
        label = qt.QLabel()
        label.setObjectName(objectName)
        label.setWordWrap(wrap)
        if role:
            label.setProperty("slicerRole", role)
        label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        return label

    def _button(self, text, objectName, callback, role="secondary", icon=None):
        button = qt.QPushButton(text)
        button.setObjectName(objectName)
        button.setProperty("slicerRole", role)
        if icon is not None:
            button.setIcon(icon)
        button.connect("clicked()", callback)
        return button

    def _buildWidget(self):
        self.widget = qt.QFrame()
        self.widget.setObjectName("LNMPipelineSection")
        self.widget.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.summaryLabel = self._label("LNMPipelineSummaryLabel")
        layout.addWidget(self.summaryLabel)
        self.stateLabel = self._label("LNMPipelineStateLabel")
        layout.addWidget(self.stateLabel)
        self.noteLabel = self._label("LNMPipelineNoteLabel", "secondaryText")
        self.noteLabel.hide()
        layout.addWidget(self.noteLabel)
        self.estimateLabel = self._label("LNMPipelineEstimateLabel", "secondaryText")
        layout.addWidget(self.estimateLabel)

        row = qt.QHBoxLayout()
        row.setSpacing(6)
        onAccent = slicer.app.palette().color(qt.QPalette.Active, qt.QPalette.HighlightedText)
        self.runButton = self._button(_("Run everything"), "LNMPipelineRunButton", self.run, "secondary", _lucideIcon("play", 16, onAccent))
        self.runButton.setFixedHeight(32)
        self.runButton.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Fixed)
        row.addWidget(self.runButton, 1)
        self.stopButton = self._button(_("Stop"), "LNMPipelineStopButton", self.stop, "secondary", _lucideIcon("square", 16))
        self.stopButton.setFixedHeight(32)
        self.stopButton.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Fixed)
        self.stopButton.setToolTip(_("Stop the cohort analysis. Completed work is kept: the next run resumes from it."))
        self.stopButton.hide()
        row.addWidget(self.stopButton, 1)
        self.optionsButton = self._button("⋯", "LNMPipelineOptionsButton", self.showOptions, "ghost", None)
        self.optionsButton.setFixedSize(32, 32)
        self.optionsButton.setToolTip(_("Options of the cohort analysis…"))
        self.optionsButton.setAccessibleName(_("Options"))
        row.addWidget(self.optionsButton)
        layout.addLayout(row)

        self.stagesFrame = qt.QFrame()
        self.stagesFrame.setObjectName("LNMPipelineStages")
        grid = qt.QGridLayout(self.stagesFrame)
        grid.setContentsMargins(0, 2, 0, 2)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(1)
        grid.setColumnStretch(2, 1)
        self.rowWidgets = {}
        for index, (rowId, label, _stages) in enumerate(ROWS):
            glyph = qt.QLabel(GLYPHS["pending"])
            glyph.setObjectName(f"LNMPipelineStageGlyph_{rowId}")
            glyph.setFixedWidth(14)
            glyph.setAlignment(qt.Qt.AlignCenter)
            name = qt.QLabel(_(label))
            name.setObjectName(f"LNMPipelineStageName_{rowId}")
            detail = qt.QLabel("")
            detail.setObjectName(f"LNMPipelineStageDetail_{rowId}")
            detail.setProperty("slicerRole", "secondaryText")
            detail.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
            detail.setAlignment(qt.Qt.AlignRight | qt.Qt.AlignVCenter)
            grid.addWidget(glyph, index, 0)
            grid.addWidget(name, index, 1)
            grid.addWidget(detail, index, 2)
            self.rowWidgets[rowId] = (glyph, name, detail)
        layout.addWidget(self.stagesFrame)

        self.progressBar = qt.QProgressBar()
        self.progressBar.setObjectName("LNMPipelineProgress")
        self.progressBar.setRange(0, 1000)
        self.progressBar.setTextVisible(False)
        self.progressBar.setFixedHeight(widgets.PROGRESS_BAR_HEIGHT)
        self.progressBar.hide()
        layout.addWidget(self.progressBar)

        self.resultFrame = qt.QWidget()
        self.resultFrame.setObjectName("LNMPipelineResultRow")
        result = qt.QVBoxLayout(self.resultFrame)
        result.setContentsMargins(0, 0, 0, 0)
        result.setSpacing(4)
        buttons = qt.QHBoxLayout()
        buttons.setSpacing(4)
        self.openReportButton = self._button(_("Open report"), "LNMPipelineOpenReportButton", self.openReport, "secondary")
        self.openReportButton.setToolTip(_("Open report.html of the last run (figures, statistics, methods)"))
        self.showFolderButton = self._button(_("Show folder"), "LNMPipelineShowFolderButton", self.showFolder, "secondary", _lucideIcon("folder-open", 14))
        self.showFolderButton.setToolTip(_("Show the output folder of the cohort analysis"))
        self.loadMapsButton = self._button(_("Load maps"), "LNMPipelineLoadMapsButton", self.loadMaps, "secondary")
        self.loadMapsButton.setToolTip(_("Show the headline group maps over the template (conjunction, specificity, sensitivity, overlap, sLNM)"))
        for button in (self.openReportButton, self.showFolderButton, self.loadMapsButton):
            button.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Fixed)
            buttons.addWidget(button)
        result.addLayout(buttons)
        extra = qt.QHBoxLayout()
        self.warningsButton = self._button(_("Warnings"), "LNMPipelineWarningsButton", self.showWarnings, "ghost")
        extra.addWidget(self.warningsButton)
        extra.addStretch(1)
        result.addLayout(extra)
        self.resultFrame.hide()
        layout.addWidget(self.resultFrame)

        # Tab order: Run → Options → Stop → Open report → Show folder → Load maps (→ Warnings)
        qt.QWidget.setTabOrder(self.runButton, self.optionsButton)
        qt.QWidget.setTabOrder(self.optionsButton, self.stopButton)
        qt.QWidget.setTabOrder(self.stopButton, self.openReportButton)
        qt.QWidget.setTabOrder(self.openReportButton, self.showFolderButton)
        qt.QWidget.setTabOrder(self.showFolderButton, self.loadMapsButton)
        qt.QWidget.setTabOrder(self.loadMapsButton, self.warningsButton)

        # Classic: the run is also an action of the card (its context menu); no shortcut
        self.runAction = qt.QAction(_("Run cohort analysis"), self.widget)
        self.runAction.setObjectName("LNMRunCohortAnalysisAction")
        self.runAction.setToolTip(_("Run the whole cohort analysis (lesion network mapping)"))
        self.runAction.connect("triggered()", self.run)
        self.widget.addAction(self.runAction)
        self.widget.setContextMenuPolicy(qt.Qt.ActionsContextMenu)

    #
    # Inputs
    #

    def study(self):
        widget = _mapWidget()
        study = getattr(widget, "cohortStudy", None) if widget is not None else None
        return study if study is not None else self.ctx.study

    def bundle(self):
        return self.ctx.bundle

    def cohortInfo(self):
        widget = _mapWidget()
        return (getattr(widget, "cohortInfo", None) if widget is not None else None) or {}

    def options(self):
        return loadOptions()

    def rule(self):
        from LesionNetworkMappingLib import thresholds

        widget = _mapWidget()
        rule = self.ctx.rule
        if rule is None and widget is not None and hasattr(widget, "currentRule"):
            try:
                rule = widget.currentRule()
            except Exception:
                rule = None
        try:
            return thresholds.normalize_rule(rule)
        except (ValueError, TypeError):
            return thresholds.normalize_rule(None)

    @staticmethod
    def software():
        app = slicer.app
        return {"slicer": str(app.applicationVersion), "fork_revision": str(getattr(app, "repositoryRevision", "") or getattr(app, "revision", ""))}

    def config(self, options=None, recompute=False, forRun=False):
        """The pipeline configuration of the current study, connectome, rule and options (None without a study or connectome)."""
        study, bundle = self.study(), self.bundle()
        if study is None or bundle is None:
            return None
        options = options if options is not None else self.options()
        return configDict(study.path, bundle.path, options, rule=self.rule(), software=self.software() if forRun else None, acceptTerms=forRun and termsAccepted(bundle),
                          parentPid=os.getpid() if forRun else None, recompute=recompute)

    def outDir(self, options=None):
        """The output folder of the run: ``<study>/derivatives/lnm/conn-<label>[_n-<N>]`` (the pipeline's default)."""
        study, bundle = self.study(), self.bundle()
        if study is None or bundle is None:
            return None
        from LesionNetworkMappingLib import export

        options = options if options is not None else self.options()
        return export.derivatives_dir(study.path, bundle.name, int(options["preview_n"]) if options.get("preview_n") else None)

    def eligibleCount(self):
        """Lesions the run maps: the last plan's (eligible and to normalize), else the Seed card's count."""
        if self.plan is not None and self.planKey == self._currentPlanKey():
            return int(self.plan.get("eligible") or 0)
        return int(self.cohortInfo().get("n") or 0)

    def _currentPlanKey(self):
        config = self.config()
        return planKey(config) if config is not None else None

    #
    # State
    #

    def model(self):
        return modelFor(self.job) if self.job is not None else None

    def running(self):
        model = self.model()
        return model is not None and model.isActive()

    def externalRunning(self):
        return self.external is not None and self.external.get("status") == "running"

    def runState(self):
        """``(enabled, reason)`` of Run everything; the reason is shown in the tooltip and the state line."""
        if self.running():
            return False, _("The cohort analysis is running.")
        study, bundle = self.study(), self.bundle()
        if study is None:
            return False, _("Open a study with traced lesions (Seed card).")
        if bundle is None:
            return False, _("Choose a connectome (Connectome card).")
        if self.eligibleCount() < 2:
            return False, _("The analysis needs at least 2 lesions that can be mapped.")
        if not termsAccepted(bundle):
            return False, _("Accept the data use terms in the Connectome card.")
        if self.externalRunning():
            return False, _("Another run writes to this output folder.")
        if jobrunner.instance().isBusy():
            return False, _("Another lesion network mapping job is running; run everything when it is done.")
        return True, ""

    def canRun(self):
        return self.runState()[0]

    def primaryLabel(self):
        n = self.eligibleCount()
        return _("Run everything · {n} lesions").format(n=n) if n else _("Run everything")

    def stateLine(self):
        """``(text, level)`` of the state line (see the module docstring for the texts)."""
        model = self.model()
        if model is not None and model.isActive():
            if model.state == "queued":
                return _("Waiting for {job}").format(job=model.queuedFor or _("another job")), "text2"
            if model.job is not None and model.job.stopRequested:
                return _("Stopping…"), "text2"
            return _("Running · {stage} {percent}%").format(stage=model.currentLabel(), percent=int(model.percent())), "accent"
        if self.externalRunning():
            info = self.external
            stage = rowLabelOf(info.get("stage")) if info.get("stage") else _("Prepare")
            percent = info.get("percent")
            text = _("Running in another process · {stage}").format(stage=stage)
            return (f"{text} {int(float(percent))}%" if percent is not None else text), "accent"
        if model is not None and model.state == jobrunner.FAILED:
            job = model.job
            message = (model.errorEvent or {}).get("message") or (job.failureReason if job is not None else "") or _("unknown error")
            return _("Failed: {message}").format(message=message), "danger"
        stopped = (model is not None and model.state == jobrunner.CANCELLED) or (model is None and (self.recordStatus or {}).get("status") == "cancelled")
        if stopped:
            if self.plan is not None and self.planKey == self._currentPlanKey():
                kept = int(self.plan["counts"].get("reuse", 0))
                return _("Stopped · {k} of {n} maps kept").format(k=kept, n=int(self.plan.get("eligible") or 0)), "text2"
            return _("Stopped"), "text2"
        failedStages = []
        if model is not None and model.state == jobrunner.DONE:
            failedStages = list((model.doneEvent or {}).get("failed_stages") or [])
        elif self.record is not None and self.record.get("status") == "done_with_failures":
            failedStages = list(self.record.get("failed_stages") or [])
        status = (self.record or {}).get("status")
        if status == "error" and model is None:
            error = (self.record or {}).get("error") or {}
            return _("Failed: {message}").format(message=error.get("message") or _("see pipeline.log")), "danger"
        stale = 0
        upToDate = 0
        if self.plan is not None and self.planKey == self._currentPlanKey():
            stale = int(self.plan["counts"].get("compute", 0)) + int(self.plan["counts"].get("normalize", 0))
            upToDate = int(self.plan["counts"].get("reuse", 0))
        if self.record is None and model is None:
            if self.plan is not None and self.plan.get("review"):
                return self._reviewLine(), "warn"
            return _("Never run"), "text2"
        if stale and status in ("done", "done_with_failures", None):
            return _plural(stale, "{n} new or changed lesion", "{n} new or changed lesions") + " · " + _("{n} up to date").format(n=upToDate), "warn"
        if failedStages:
            return _plural(len(failedStages), "Finished with {n} failed stage", "Finished with {n} failed stages"), "warn"
        if self.plan is not None and self.plan.get("review"):
            return self._reviewLine(), "warn"
        if status in ("done", "done_with_failures") or (model is not None and model.state == jobrunner.DONE):
            age = formatAge((self.record or {}).get("updated")) if self.record else _("just now")
            return _("Up to date · finished {age}").format(age=age) if age else _("Up to date"), "ok"
        return _("Never run"), "text2"

    def _reviewLine(self):
        return _plural(int(self.plan.get("review") or 0), "{n} native lesion needs normalization review in Library", "{n} native lesions need normalization review in Library")

    def summaryLine(self):
        info = self.cohortInfo()
        study = self.study()
        if study is None:
            return _("Open a study to analyse its cohort.")
        if self.plan is not None and self.planKey == self._currentPlanKey():
            n, groups = int(self.plan.get("eligible") or 0), self.plan.get("groups") or {}
        else:
            n, groups = int(info.get("n") or 0), info.get("groups") or {}
        parts = [_plural(n, "{n} lesion", "{n} lesions")]
        if groups.get("case") or groups.get("control"):
            parts.append(_("{cases} cases / {controls} controls").format(cases=groups.get("case", 0), controls=groups.get("control", 0)))
        scores = self._scores(study)
        if scores:
            parts.append(_("scores: {names}").format(names=", ".join(scores)))
        return " · ".join(parts)

    def _scores(self, study):
        if self.plan is not None and self.planKey == self._currentPlanKey():
            return list(self.plan.get("scores") or [])
        try:
            return [column for column, confirmed in study.score_columns() if confirmed]
        except Exception:
            return []

    def estimateLine(self):
        if self.plan is None or self.planKey != self._currentPlanKey():
            return _("Estimating the time…") if self.worker.isBusy() else ""
        passes = int(self.plan.get("passes") or 0)
        bundle = self.plan.get("bundle") or ""
        if passes:
            detail = _plural(passes, "{n} pass over {bundle}", "{n} passes over {bundle}", bundle=bundle)
        else:
            detail = _("every map is reused")
        return _("≈ {time} (estimate; {detail})").format(time=formatDuration(self.plan.get("estimate_s")) or _("< 1 min"), detail=detail)

    def stageStatuses(self):
        """{row id: (status, detail, tooltip)} of the stage list, and the set of rows to show."""
        model = self.model()
        statuses, reasons, active, detailFor = {}, {}, False, None
        if model is not None and (model.stages or model.status):
            statuses, reasons = dict(model.status), dict(model.reasons)
            active = model.isActive()
            detailFor = model.current if active else None
        elif self.externalRunning():
            names = [name for name, _label in _pipeline().STAGES]
            index = int(self.external.get("index") or 1)
            for position, name in enumerate(names, 1):
                statuses[name] = "done" if position < index else "running" if position == index else "planned"
            active = True
        elif self.record is not None:
            for stage in self.record.get("stages") or []:
                if isinstance(stage, dict):
                    statuses[stage.get("name")] = stage.get("status")
                    reasons[stage.get("name")] = stage.get("reason")
        elif self.plan is not None:
            for stage in self.plan.get("stages") or []:
                statuses[stage.get("name")] = "skip" if stage.get("status") in ("skip", "unavailable") else stage.get("status")
                reasons[stage.get("name")] = stage.get("reason")
        rows = {}
        for rowId, _label, stages in ROWS:
            status = rowStatus([statuses.get(stage) for stage in stages], active)
            tips = [f"{_pipeline().STAGE_LABELS.get(stage, stage)}: {reasons[stage]}" for stage in stages if reasons.get(stage)]
            detail = ""
            if status == "running" and model is not None and detailFor in stages:
                detail = model.detail()
            elif status == "running" and self.externalRunning():
                eta = formatDuration(self.external.get("eta"))
                detail = _("~{eta} left").format(eta=eta) if eta else ""
            rows[rowId] = (status, detail, "\n".join(tips))
        shown = {rowId for rowId, _label, _stages in ROWS}
        if rows["normalize"][0] in ("skipped", "pending") and not (model is not None and model.startEvent and int(model.startEvent.get("n_normalize") or 0)):
            shown.discard("normalize")  # only when native lesions are normalized
        return rows, shown

    def warnings(self):
        """Warning messages of the last run: its events (this session), else pipeline.json."""
        model = self.model()
        if model is not None and model.warnings:
            return [self._warningText(event) for event in model.warnings]
        record = self.record or {}
        return [self._warningText(item) if isinstance(item, dict) else str(item) for item in record.get("warnings") or []]

    @staticmethod
    def _warningText(item):
        who = item.get("participant_id")
        return (f"{who}: " if who else "") + str(item.get("message") or item.get("code") or "")

    #
    # Applying the state (coalesced, at most every 100 ms)
    #

    def scheduleApply(self):
        if not self.applyTimer.isActive():
            self.applyTimer.start()

    def apply(self):
        """Show the state: change-detected texts, the stage rows, the buttons and the primary action (≤ 5 ms)."""
        started = time.perf_counter()
        self.adoptRunningJob()
        model = self.model()
        running = model is not None and model.isActive()
        external = self.externalRunning()
        _setText(self.summaryLabel, self.summaryLine())
        text, level = self.stateLine()
        _setText(self.stateLabel, text)
        _setLevel(self.stateLabel, level)
        _setText(self.noteLabel, self.note)
        _setVisible(self.noteLabel, bool(self.note))
        estimate = "" if running or external else self.estimateLine()
        _setText(self.estimateLabel, estimate)
        _setVisible(self.estimateLabel, bool(estimate))
        enabled, reason = self.runState()
        _setVisible(self.runButton, not (running or external))
        _setVisible(self.stopButton, running or external)
        self.stopButton.setEnabled(not (model is not None and model.job is not None and model.job.stopRequested))
        _setText(self.runButton, _("Run everything"))
        self.runButton.setEnabled(enabled)
        self.runAction.setEnabled(enabled)
        primary = self.ctx.primaryOverride("cohort") is None
        if self.runButton.property("slicerRole") != ("primary" if primary else "secondary"):
            self.runButton.setProperty("slicerRole", "primary" if primary else "secondary")
            self.runButton.style().unpolish(self.runButton)
            self.runButton.style().polish(self.runButton)
        _setToolTip(self.runButton, reason if not enabled else _("Map every new or changed lesion with the connectome, then run the group analyses, the null models, "
                                                                 "the statistics and the report.\nResumes: only new or changed lesions are mapped."))
        rows, shown = self.stageStatuses()
        for rowId, (glyph, name, detail) in self.rowWidgets.items():
            status, detailText, tip = rows[rowId]
            _setVisible(glyph, rowId in shown)
            _setVisible(name, rowId in shown)
            _setVisible(detail, rowId in shown)
            _setText(glyph, GLYPHS[status])
            _setLevel(glyph, GLYPH_LEVELS[status])
            _setLevel(name, "text" if status in ("running", "done", "failed") else "text2")
            _setText(detail, detailText)
            for item in (glyph, name, detail):
                _setToolTip(item, tip)
        _setVisible(self.progressBar, running or external)
        if running:
            value = int(model.percent() * 10)
        elif external:
            value = int(float(self.external.get("percent") or 0.0) * 10)
        else:
            value = 0
        if self.progressBar.value != value:
            self.progressBar.setValue(value)
        report = self.reportPath()
        showResult = not (running or external) and self.record is not None
        _setVisible(self.resultFrame, showResult or (model is not None and model.state == jobrunner.FAILED))
        self.openReportButton.setEnabled(bool(report))
        self.loadMapsButton.setEnabled(bool(self.record and headlineMaps(self.record)) and not self.mapQueue)
        warnings = self.warnings()
        failed = model is not None and model.state == jobrunner.FAILED
        _setText(self.warningsButton, _("Details (log)") if failed else _("Warnings ({n})").format(n=len(warnings)))
        _setVisible(self.warningsButton, bool(warnings) or failed)
        self._publishPrimary()
        self._blockCompute(running or external)
        if external and not self.externalTimer.isActive():
            self.externalTimer.start()
        elif not external and self.externalTimer.isActive():
            self.externalTimer.stop()
        self.applyCount += 1
        self.applySeconds = time.perf_counter() - started

    def _publishPrimary(self):
        """The primary action of Seed = Cohort (D-A26): "Run everything · N lesions" (re-published when it changes)."""
        if not hasattr(self.ctx, "setPrimaryOverride"):
            return
        state = (self.primaryLabel(), self.canRun())
        if state != self._publishedPrimary:
            self._publishedPrimary = state
            self.ctx.setPrimaryOverride("cohort", state[0], self.run, self.canRun)

    def _blockCompute(self, blocked):
        """Map network and Maps only are disabled (with the reason) while the pipeline runs."""
        if blocked == self._computeBlocked:
            return
        self._computeBlocked = blocked
        self.ctx.setComputeEnabled(not blocked, _("The cohort analysis is running (Run everything)."), key="pipeline")

    #
    # Staleness and re-attach
    #

    def scheduleRecheck(self):
        self.recheckTimer.start()

    def recheck(self):
        """Read pipeline.json and the lock of the output folder, and plan in the worker thread (no hashing here)."""
        out = self.outDir()
        self.record, self.recordStatus, self.external = None, None, None
        if out:
            pipeline = _pipeline()
            self.record = _readJson(os.path.join(out, pipeline.PIPELINE_JSON))
            if self.record is not None and self.record.get("format") != pipeline.FORMAT_NAME:
                self.record = None
            try:
                self.recordStatus = pipeline.status(out)
            except Exception:
                logging.exception("Lesion network mapping: the state of the cohort analysis could not be read")
                self.recordStatus = None
            if (self.recordStatus or {}).get("status") == "running" and not pipelineJobs(out):
                self.external = self.recordStatus
        config = self.config()
        if config is not None and not self.running():
            self.worker.request(planKey(config), config)
        self.scheduleApply()

    def _onPlan(self, key, summary, error):
        if key != self._currentPlanKey():
            return  # an older plan (the settings changed meanwhile)
        if error:
            logging.warning(f"Lesion network mapping: the cohort analysis could not be planned: {error}")
            self.planError = error
            return
        self.plan, self.planKey, self.planError, self.planAt = summary, key, None, time.time()
        self.scheduleApply()

    def _pollExternal(self):
        if not self.externalRunning() or not self.widget.isVisible():
            if not self.externalRunning():
                self.externalTimer.stop()
            return
        out = self.outDir()
        try:
            info = _pipeline().status(out) if out else None
        except Exception:
            info = None
        if info is None or info.get("status") != "running":
            self.recheck()
            return
        self.external = info
        self.scheduleApply()

    def adoptRunningJob(self):
        """Follow the runner's pipeline job of this output folder (a card created while it runs re-attaches to it)."""
        if self.job is not None and self.job.isFinished() is False:
            return
        out = self.outDir()
        jobs = pipelineJobs(out) if out else []
        if jobs and jobs[-1] is not self.job:
            self.job = jobs[-1]
            self.external = None

    #
    # Card protocol (called by the Map widget)
    #

    def onCohortChanged(self):
        self.scheduleRecheck()
        self.scheduleApply()

    def onStudyChanged(self):
        self.scheduleRecheck()

    def onCohortJobStarted(self, job):
        self.scheduleApply()

    def onCohortMapsChanged(self, path=None):
        self.scheduleApply()

    def onRunSettingsChanged(self):
        self.scheduleRecheck()

    def onJobFinished(self, job):
        """A pipeline job ended (module hook): show it and re-check what is up to date."""
        if job is self.job or _samePath(getattr(job.spec, "lnmOut", None), self.outDir()):
            self.job = job
            model = modelFor(job)
            if job.state == jobrunner.DONE and not getattr(model, "inboxApplied", False) and _studySession(getattr(job.spec, "lnmStudy", None)) is None:
                self.note = _("Statuses will update when the study is opened in Library.")
            elif job.state == jobrunner.DONE:
                self.note = ""
            widget = _mapWidget()
            if widget is not None and hasattr(widget, "updateComputeState"):
                qt.QTimer.singleShot(0, widget.updateComputeState)
            self.recheck()
        self.scheduleApply()

    def cleanup(self):
        if self in _cards:
            _cards.remove(self)
        for name in ("studyChanged", "bundleChanged", "ruleChanged"):
            callbacks = getattr(self.ctx, name, [])
            while self.scheduleRecheck in callbacks:
                callbacks.remove(self.scheduleRecheck)
        callbacks = getattr(self.ctx, "computeEnabledChanged", [])
        while self.scheduleApply in callbacks:
            callbacks.remove(self.scheduleApply)
        for timer in (self.applyTimer, self.recheckTimer, self.externalTimer, self.loadTimer):
            timer.stop()
        self.worker.stop()
        self._finishLoading()
        if hasattr(self.ctx, "clearPrimaryOverride"):
            self.ctx.clearPrimaryOverride("cohort")
        if self._computeBlocked:
            self.ctx.setComputeEnabled(True, key="pipeline")
            self._computeBlocked = False
        if self._quitConnected:
            try:
                slicer.app.disconnect("aboutToQuit()", self._onAboutToQuit)
            except Exception:
                pass
            self._quitConnected = False

    #
    # Run and Stop
    #

    def run(self, options=None, recompute=False):
        """Run everything (the button, the primary action and Ctrl+Enter); return the job, or None with the reason shown.

        Takes a few milliseconds: no hashing (the last plan is reused), one flush of the open study session, one process.
        """
        enabled, reason = self.runState()
        if not enabled:
            _setText(self.stateLabel, reason)
            _setLevel(self.stateLabel, "warn")
            return None
        options = dict(options) if options is not None else self.options()
        config = self.config(options, recompute=recompute, forRun=True)
        study = self.study()
        self.note = ""
        session = _studySession(study.path)
        if session is not None:
            try:
                session.flush(lockTimeout=0.2)
            except Exception:
                logging.exception("Lesion network mapping: the study could not be saved before the cohort analysis")
            try:
                current = session.current()
                if current and session.isDirty():
                    self.note = _("{sid} has unsaved edits in Draw; they are not included.").format(sid=current)
            except Exception:
                pass
        args = commandArguments(config)
        out = self.outDir(options)
        spec = jobrunner.JobSpec(PIPELINE_MODULE, args, title=_("Cohort analysis"), heavy=True, kind=JOB_KIND, runRoot=os.path.join(study.path, "derivatives", "lnm"),
                                 env=_localeEnvironment(args), threads=config.get("threads"), statusText=lambda job: modelFor(job).statusText(), name="pipeline")
        spec.lnmOut = out
        spec.lnmStudy = study.path
        self.job = self.ctx.submit(spec)
        modelFor(self.job)
        self.external = None
        self._connectQuit()
        self.apply()
        return self.job

    def stop(self):
        """Stop: the job's cancel file (the runner escalates to the worker it reported); a run of another process gets the STOP file."""
        model = self.model()
        if model is not None and model.isActive() and model.job is not None:
            model.job.stop()
        elif self.externalRunning():
            self._writeStop(self.outDir())
        self.apply()

    @staticmethod
    def _writeStop(out):
        if not out:
            return
        pipeline = _pipeline()
        path = os.path.join(out, pipeline.PRIVATE_DIR, pipeline.STOP_NAME)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8"):
                pass
        except OSError as exc:
            logging.warning(f"Lesion network mapping: could not write {path}: {exc}")

    def _connectQuit(self):
        if self._quitConnected or getattr(slicer, "app", None) is None:
            return
        try:
            slicer.app.connect("aboutToQuit()", self._onAboutToQuit)
            self._quitConnected = True
        except Exception:
            pass

    def _onAboutToQuit(self):
        """The application quits: the running pipeline gets its STOP file (it stops within seconds and keeps its finished work)."""
        for job in pipelineJobs():
            self._writeStop(getattr(job.spec, "lnmOut", None))

    #
    # Results
    #

    def outFolder(self):
        out = self.outDir()
        return out if out and os.path.isdir(out) else None

    def reportPath(self):
        out = self.outFolder()
        path = os.path.join(out, "report.html") if out else None
        return path if path and os.path.isfile(path) else None

    def openReport(self):
        path = self.reportPath()
        if not path:
            return None
        return openPath(path, _("Cohort analysis report"), self.viewers)

    def showFolder(self):
        out = self.outFolder()
        if not out:
            return None
        return openPath(out, _("Cohort analysis folder"), self.viewers)

    def loadMaps(self):
        """Queue the headline group maps; they are loaded one per 30 ms tick (Load maps). Return the queued descs."""
        out = self.outFolder()
        if not out or self.record is None:
            return []
        alpha = float(((self.record.get("config") or {}).get("alpha")) or 0.05)
        self.mapQueue = [(desc, os.path.join(out, path), kind, (os.path.join(out, mask[0]), mask[1]) if mask else None) for desc, path, kind, mask in headlineMaps(self.record, alpha)]
        self.loadedVolumes = []
        if not self.mapQueue:
            return []
        if self._loadStack is None:
            self._loadStack = contextlib.ExitStack()
            try:
                from StudioLib import autosave

                self._loadStack.enter_context(autosave.suspended("loading maps"))
            except Exception:
                pass  # no Studio autosave: nothing to suspend
        self.loadMapsButton.setEnabled(False)
        self.loadTimer.start()
        return [item[0] for item in self.mapQueue]

    def _loadNext(self):
        if not self.mapQueue:
            self._finishLoading()
            return
        desc, path, kind, mask = self.mapQueue.pop(0)
        try:
            volume = self._loadOne(desc, path, kind, mask)
            if volume is not None:
                self.loadedVolumes.append(volume)
        except Exception:
            logging.exception(f"Lesion network mapping: the group map {desc} could not be loaded")
        if self.mapQueue:
            self.loadTimer.start()
        else:
            self._showLoaded()
            self._finishLoading()

    def _loadOne(self, desc, path, kind, mask):
        from LesionNetworkMappingLib import display

        widget = _mapWidget()
        logic = getattr(widget, "logic", None) if widget is not None else None
        if logic is None:
            from LesionNetworkMapping import LesionNetworkMappingLogic

            logic = LesionNetworkMappingLogic()
        name = f"LNM {desc}"
        existing = None
        for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
            if node.GetAttribute("LNM.PipelineMap") == desc and node.GetName() == name:
                existing = node
                break
        attributes = {"LNM.PipelineMap": desc, "Studio.Role": "statmap"}
        volume = logic.loadMap(path, name, existing, saveWithScene=True, attributes=attributes)
        if mask is not None:
            import numpy as np

            pData, _affine = logic.readMap(mask[0])
            array = slicer.util.arrayFromVolume(volume)
            if pData.shape == array.shape:
                array[~(np.asarray(pData) < float(mask[1]))] = 0
                slicer.util.arrayFromVolumeModified(volume)
                volume.SetAttribute("LNM.MaskedBy", f"{os.path.basename(mask[0])} < {mask[1]:g}")
        if kind == "stat":
            display.applyResultDisplay(volume, display.ensureLUT(volume.GetScene()))  # the shared "LNM Diverging" table
        else:
            display.applyGroupMapDisplay(volume, desc)  # percent and binary maps: the positive half of its own table
        return volume

    def _showLoaded(self):
        """The last loaded map in the foreground (opacity 0.8) over the template."""
        if not self.loadedVolumes or slicer.app.layoutManager() is None:
            return
        from LesionNetworkMappingLib import display

        template = None
        try:
            if self.ctx.shell() is not None:
                from StudioLib import mappage

                mappage.captureLayers()
                template = mappage.templateVolume(self.bundle())
        except Exception:
            template = None
        if template is None:
            widget = _mapWidget()
            logic = getattr(widget, "logic", None) if widget is not None else None
            template = logic.templateVolume() if logic is not None else None
        self._lastLoadTemplate = template
        display.showLayers(template, self.loadedVolumes[-1], 0.8)

    def _finishLoading(self):
        self.mapQueue = []
        if self._loadStack is not None:
            stack, self._loadStack = self._loadStack, None
            try:
                stack.close()
            except Exception:
                logging.exception("Lesion network mapping: autosave could not be resumed")
        self.scheduleApply()

    def showWarnings(self):
        """A non-modal list of the warnings of the last run (and the log tail of a failed run) with Copy."""
        lines = self.warnings()
        model = self.model()
        if model is not None and model.state == jobrunner.FAILED and model.job is not None:
            lines += ["", _("Last output lines:"), *[str(line) for line in model.job.logTail]]
            if model.job.logPath:
                lines.append(_("Full log: {path}").format(path=model.job.logPath))
        text = "\n".join(lines) or _("No warnings.")
        if self.warningsDialog is None:
            dialog = qt.QDialog(slicer.util.mainWindow())
            dialog.setObjectName("LNMPipelineWarningsDialog")
            dialog.setWindowTitle(_("Cohort analysis warnings"))
            dialog.setModal(False)
            layout = qt.QVBoxLayout(dialog)
            view = qt.QPlainTextEdit()
            view.setObjectName("LNMPipelineWarningsText")
            view.setReadOnly(True)
            layout.addWidget(view)
            buttons = qt.QHBoxLayout()
            buttons.addStretch(1)
            copy = qt.QPushButton(_("Copy"))
            copy.setObjectName("LNMPipelineWarningsCopyButton")
            copy.connect("clicked()", lambda: qt.QApplication.clipboard().setText(view.toPlainText()))
            buttons.addWidget(copy)
            close = qt.QPushButton(_("Close"))
            close.connect("clicked()", dialog.close)
            buttons.addWidget(close)
            layout.addLayout(buttons)
            dialog.resize(DIALOG_MAX_WIDTH, 360)
            self.warningsDialog = (dialog, view)
        dialog, view = self.warningsDialog
        view.setPlainText(text)
        dialog.show()
        dialog.raise_()
        return dialog

    #
    # Options
    #

    def showOptions(self):
        """The options dialog (non-modal; Start runs with them, Cancel keeps the stored ones)."""
        if self.optionsDialog is None:
            self.optionsDialog = OptionsDialog(self)
        self.optionsDialog.load(self.options())
        self.optionsDialog.dialog.show()
        self.optionsDialog.dialog.raise_()
        return self.optionsDialog


class OptionsDialog:
    """LNMPipelineOptionsDialog: the settings of Run everything, stored as JSON in ``Studio/LNM/Pipeline/Options``."""

    def __init__(self, card):
        self.card = card
        self.atlasJob = None
        dialog = self.dialog = qt.QDialog(slicer.util.mainWindow())
        dialog.setObjectName("LNMPipelineOptionsDialog")
        dialog.setWindowTitle(_("Cohort analysis options"))
        dialog.setModal(False)
        dialog.setMaximumWidth(DIALOG_MAX_WIDTH)
        outer = qt.QVBoxLayout(dialog)
        scroll = qt.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(qt.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(qt.Qt.ScrollBarAlwaysOff)
        body = qt.QWidget()
        form = qt.QFormLayout(body)
        form.setFieldGrowthPolicy(qt.QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(qt.QFormLayout.WrapLongRows)

        subjects = qt.QHBoxLayout()
        self.subjectsFrame, self.subjectsGroup = widgets.segmented([_("All"), _("Preview")])
        self.subjectsFrame.setObjectName("LNMPipelineSubjects")
        subjects.addWidget(self.subjectsFrame)
        self.previewSpin = self._spin("LNMPipelinePreviewN", 2, 100000, PREVIEW_N, prefix="N ")
        subjects.addWidget(self.previewSpin)
        self.previewSeedSpin = self._spin("LNMPipelinePreviewSeed", 0, 999999, 0, prefix=_("seed "))
        subjects.addWidget(self.previewSeedSpin)
        subjects.addStretch(1)
        self.subjectsGroup.connect("idClicked(int)", lambda index: self._update())
        form.addRow(_("Subjects"), subjects)

        self.ruleLabel = self._caption("LNMPipelinePrimaryRule")
        form.addRow(_("Rule"), self.ruleLabel)
        rules = qt.QVBoxLayout()
        self.ruleChecks = {}
        for key, label, _rule in EXTRA_RULES:
            box = qt.QCheckBox(_(label))
            box.setObjectName(f"LNMPipelineExtraRule_{key}")
            self.ruleChecks[key] = box
            rules.addWidget(box)
        form.addRow(_("Extra rules"), rules)

        self.sensitivitySpin = self._spin("LNMPipelineSensitivity", 50, 100, 90, suffix=" %", step=5)
        form.addRow(_("Sensitivity"), self.sensitivitySpin)

        self.specificityCombo = self._combo("LNMPipelineSpecificity", SPECIFICITY_CHOICES)
        self.specificityReason = self._caption("LNMPipelineSpecificityReason")
        specificity = qt.QVBoxLayout()
        specificity.addWidget(self.specificityCombo)
        specificity.addWidget(self.specificityReason)
        form.addRow(_("Specificity"), specificity)

        self.volumeCheck = qt.QCheckBox(_("Lesion volume covariate"))
        self.volumeCheck.setObjectName("LNMPipelineVolumeCovariate")
        self.volumeCheck.connect("toggled(bool)", lambda checked: self._update())
        self.volumeReason = qt.QLineEdit()
        self.volumeReason.setObjectName("LNMPipelineVolumeOffReason")
        self.volumeReason.setPlaceholderText(_("Why is lesion volume left out? (recorded)"))
        self.volumeReason.connect("textChanged(QString)", lambda text: self._update())
        volume = qt.QVBoxLayout()
        volume.addWidget(self.volumeCheck)
        volume.addWidget(self.volumeReason)
        form.addRow(_("Covariates"), volume)

        self.permSpin = self._spin("LNMPipelinePermutations", 1000, 10000, 5000, step=1000)
        self.permSpin.connect("valueChanged(int)", lambda value: self._update())
        form.addRow(_("Permutations"), self.permSpin)
        self.tfceCheck = qt.QCheckBox(_("TFCE"))
        self.tfceCheck.setObjectName("LNMPipelineTfce")
        self.tfceCheck.connect("toggled(bool)", lambda checked: self._update())
        self.tfceEta = self._caption("LNMPipelineTfceEta")
        tfce = qt.QVBoxLayout()
        tfce.addWidget(self.tfceCheck)
        tfce.addWidget(self.tfceEta)
        form.addRow("", tfce)

        self.scoresFrame = qt.QWidget()
        self.scoresFrame.setObjectName("LNMPipelineScores")
        self.scoresLayout = qt.QVBoxLayout(self.scoresFrame)
        self.scoresLayout.setContentsMargins(0, 0, 0, 0)
        self.scoreChecks = {}
        form.addRow(_("Scores"), self.scoresFrame)

        self.relocationsSpin = self._spin("LNMPipelineRelocations", -1, 100, -1)
        self.relocationsSpin.setSpecialValueText(_("Auto"))
        form.addRow(_("Per-lesion relocations"), self.relocationsSpin)

        self.nullsCombo = self._combo("LNMPipelineNulls", NULL_CHOICES)
        self.nullsEta = self._caption("LNMPipelineNullsEta")
        nulls = qt.QVBoxLayout()
        nulls.addWidget(self.nullsCombo)
        nulls.addWidget(self.nullsEta)
        form.addRow(_("Null models"), nulls)

        self.referenceCheck = qt.QCheckBox(_("Build missing reference maps (≈ 20 min)"))
        self.referenceCheck.setObjectName("LNMPipelineBuildReferences")
        self.referenceCheck.setToolTip(_("Reference maps calibrate the QA of the network maps. They are built only when you ask (one pass over the connectome)."))
        form.addRow(_("Reference maps"), self.referenceCheck)

        self.nativeCombo = self._combo("LNMPipelineNative", NATIVE_CHOICES)
        self.nativeCombo.connect("currentIndexChanged(int)", lambda index: self._update())
        self.nativeNote = self._caption("LNMPipelineNativeNote")
        native = qt.QVBoxLayout()
        native.addWidget(self.nativeCombo)
        native.addWidget(self.nativeNote)
        form.addRow(_("Native lesions"), native)

        self.atlasCombo = self._combo("LNMPipelineAtlas", ATLAS_CHOICES)
        self.atlasCombo.connect("currentIndexChanged(int)", lambda index: self._update())
        self.atlasDownload = qt.QPushButton(_("Download"))
        self.atlasDownload.setObjectName("LNMPipelineAtlasDownload")
        self.atlasDownload.setProperty("slicerRole", "secondary")
        self.atlasDownload.setToolTip(_("Download the Schaefer 2018 atlas (400 parcels, 7 networks) from TemplateFlow; nothing is downloaded otherwise"))
        self.atlasDownload.connect("clicked()", self.downloadAtlas)
        self.atlasStatus = self._caption("LNMPipelineAtlasStatus")
        atlas = qt.QHBoxLayout()
        atlas.addWidget(self.atlasCombo, 1)
        atlas.addWidget(self.atlasDownload)
        atlasColumn = qt.QVBoxLayout()
        atlasColumn.addLayout(atlas)
        atlasColumn.addWidget(self.atlasStatus)
        form.addRow(_("Atlas labels"), atlasColumn)

        self.recomputeCheck = qt.QCheckBox(_("Recompute everything (ignore saved stages and the map cache)"))
        self.recomputeCheck.setObjectName("LNMPipelineRecompute")
        form.addRow("", self.recomputeCheck)
        self.threadsSpin = self._spin("LNMPipelineThreads", 0, max(1, os.cpu_count() or 1), 0)
        self.threadsSpin.setSpecialValueText(_("Auto"))
        self.threadsSpin.setToolTip(_("Auto: all processors but one, at most Studio/LNM/MaxJobThreads"))
        form.addRow(_("Threads"), self.threadsSpin)
        self.awakeCheck = qt.QCheckBox(_("Keep the computer awake during long runs"))
        self.awakeCheck.setObjectName("LNMPipelineKeepAwake")
        form.addRow("", self.awakeCheck)

        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        self.problemLabel = self._caption("LNMPipelineOptionsProblem")
        outer.addWidget(self.problemLabel)
        buttons = qt.QHBoxLayout()
        buttons.addStretch(1)
        self.cancelButton = qt.QPushButton(_("Cancel"))
        self.cancelButton.setObjectName("LNMPipelineOptionsCancel")
        self.cancelButton.connect("clicked()", dialog.close)
        buttons.addWidget(self.cancelButton)
        self.startButton = qt.QPushButton(_("Start"))
        self.startButton.setObjectName("LNMPipelineOptionsStart")
        self.startButton.setProperty("slicerRole", "primary")
        self.startButton.connect("clicked()", self.start)
        buttons.addWidget(self.startButton)
        outer.addLayout(buttons)
        dialog.resize(DIALOG_MAX_WIDTH, 560)
        self.extra = {}

    @staticmethod
    def _spin(objectName, minimum, maximum, value, prefix="", suffix="", step=1):
        spin = qt.QSpinBox()
        spin.setObjectName(objectName)
        spin.setRange(minimum, maximum)
        spin.setSingleStep(step)
        spin.setValue(value)
        if prefix:
            spin.setPrefix(prefix)
        if suffix:
            spin.setSuffix(suffix)
        return spin

    @staticmethod
    def _combo(objectName, choices):
        combo = qt.QComboBox()
        combo.setObjectName(objectName)
        for value, label in choices:
            combo.addItem(_(label), value)
        return combo

    @staticmethod
    def _caption(objectName):
        label = qt.QLabel()
        label.setObjectName(objectName)
        label.setWordWrap(True)
        label.setProperty("slicerRole", "secondaryText")
        label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        return label

    @staticmethod
    def _select(combo, value):
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def load(self, options):
        """Show ``options`` (the stored ones) and the facts of the current study and connectome."""
        card = self.card
        self.extra = {key: options[key] for key in EXTRA_OPTION_KEYS if key in options}
        preview = options.get("preview_n")
        self.subjectsGroup.button(1 if preview else 0).setChecked(True)
        self.previewSpin.setValue(int(preview or PREVIEW_N))
        self.previewSeedSpin.setValue(int(options.get("preview_seed") or 0))
        from LesionNetworkMappingLib import thresholds

        rule = card.rule()
        self.ruleLabel.setText(_("Primary: {rule} (from the Result card)").format(rule=thresholds.rule_label(rule)))
        stored = [thresholds.normalize_rule(item) for item in options.get("extra_rules") or []]
        for key, _label, ruleDict in EXTRA_RULES:
            self.ruleChecks[key].setChecked(thresholds.normalize_rule(ruleDict) in stored)
        bundle = card.bundle()
        n = int(preview) if preview else (bundle.n_subjects if bundle is not None else 0)
        t7 = self.ruleChecks["t7"]
        t7.setEnabled(n >= T_CONVENTION_MIN_N)
        t7.setToolTip("" if t7.enabled else _("The |T| ≥ 7 convention needs about 1000 connectome subjects (N = {n}).").format(n=n))
        if not t7.enabled:
            t7.setChecked(False)
        self.sensitivitySpin.setValue(int(round(float(options.get("sensitivity") or 90))))
        self._select(self.specificityCombo, options.get("specificity") or "auto")
        groups = (card.plan or {}).get("groups") or card.cohortInfo().get("groups") or {}
        small = [name for name in ("case", "control") if int(groups.get(name, 0)) < MIN_PER_GROUP]
        self.specificityCombo.setEnabled(not small)
        self.specificityReason.setText(_("Needs at least {k} cases and {k} controls (now {cases} / {controls}).").format(
            k=MIN_PER_GROUP, cases=groups.get("case", 0), controls=groups.get("control", 0)) if small else "")
        self.specificityReason.setVisible(bool(small))
        reason = options.get("volume_off_reason") or ""
        self.volumeCheck.setChecked(not reason)
        self.volumeReason.setText(reason)
        self.permSpin.setValue(int(options.get("perm") or 5000))
        self.tfceCheck.setChecked(bool(options.get("tfce")))
        self._loadScores(options.get("scores", "auto"))
        relocations = options.get("null_relocations", "auto")
        self.relocationsSpin.setValue(-1 if relocations in (None, "auto") else int(relocations))
        self._select(self.nullsCombo, options.get("nulls") or "auto")
        nulls = next((stage for stage in (card.plan or {}).get("stages") or [] if stage.get("name") == "nulls"), None)
        self.nullsEta.setText(_("Auto plan: {status}{eta}").format(status=nulls.get("status"), eta=f" · ≈ {formatDuration(nulls.get('estimate_s'))}" if nulls.get("estimate_s") else "")
                              if nulls else "")
        self.referenceCheck.setChecked(bool(options.get("build_references")))
        self._select(self.nativeCombo, options.get("native") or "normalize")
        self._select(self.atlasCombo, options.get("atlas") or "none")
        self.recomputeCheck.setChecked(False)
        self.threadsSpin.setValue(int(options.get("threads") or 0))
        self.awakeCheck.setChecked(bool(options.get("keep_awake", True)))
        self._update()

    def _loadScores(self, chosen):
        while self.scoresLayout.count():
            item = self.scoresLayout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.scoreChecks = {}
        study = self.card.study()
        columns = []
        if study is not None:
            try:
                columns = list(study.score_columns())
            except Exception:
                columns = []
        if not columns:
            label = self._caption("LNMPipelineNoScores")
            label.setText(_("No score column (role score in Library ▸ Columns)."))
            self.scoresLayout.addWidget(label)
            return
        for column, confirmed in columns:
            box = qt.QCheckBox(column)
            box.setObjectName(f"LNMPipelineScore_{column}")
            box.setEnabled(bool(confirmed))
            if confirmed:
                box.setChecked(chosen == "auto" or (isinstance(chosen, list) and column in chosen))
            else:
                box.setToolTip(_("confirm in Library ▸ Columns"))
                box.setText(f"{column} — " + _("confirm in Library ▸ Columns"))
            self.scoreChecks[column] = (box, bool(confirmed))
            self.scoresLayout.addWidget(box)

    def _atlasCached(self):
        return _atlasPaths("schaefer400")[0] is not None

    def _update(self):
        preview = self.subjectsGroup.checkedId() == 1
        self.previewSpin.setEnabled(preview)
        self.previewSeedSpin.setEnabled(preview)
        self.volumeReason.setVisible(not self.volumeCheck.checked)
        bundle = self.card.bundle()
        if bundle is not None:
            from LesionNetworkMappingLib.pipeline import estimate_glm

            eta = estimate_glm(self.permSpin.value, bundle.n_voxels, tfce=True)
            self.tfceEta.setText(_("TFCE adds ≈ {time} per GLM").format(time=formatDuration(eta - estimate_glm(self.permSpin.value, bundle.n_voxels))))
        include = self.nativeCombo.currentData == "include-unreviewed"
        self.nativeNote.setText(_("Unreviewed normalizations are mapped; the report shows this override in amber.") if include else "")
        self.nativeNote.setVisible(include)
        schaefer = self.atlasCombo.currentData == "schaefer400"
        cached = schaefer and self._atlasCached()
        busy = self.atlasJob is not None and self.atlasJob.get("thread") is not None and self.atlasJob["thread"].is_alive()
        self.atlasDownload.setVisible(schaefer and not cached)
        self.atlasDownload.setEnabled(not busy)
        if busy:
            self.atlasStatus.setText(self.atlasJob.get("text") or _("Downloading…"))
        elif schaefer:
            self.atlasStatus.setText(_("Cached: peaks and clusters get Schaefer-400 labels.") if cached else _("Not downloaded yet (about 1 MB)."))
        else:
            self.atlasStatus.setText(_("Labels from atlases already cached on this computer, if any."))
        problem = ""
        if not self.volumeCheck.checked and not self.volumeReason.text.strip():
            problem = _("Give a reason for leaving lesion volume out (one line; it is recorded).")
        self.problemLabel.setText(problem)
        self.problemLabel.setVisible(bool(problem))
        self.startButton.setEnabled(not problem and self.card.canRun())
        self.startButton.setToolTip("" if self.card.canRun() else self.card.runState()[1])

    def values(self):
        """The options shown in the dialog (the JSON stored in ``Studio/LNM/Pipeline/Options``)."""
        scores = "auto"
        if self.scoreChecks:
            confirmed = [column for column, (box, ok) in self.scoreChecks.items() if ok]
            checked = [column for column, (box, ok) in self.scoreChecks.items() if ok and box.checked]
            scores = "auto" if checked == confirmed else (checked or "none")
        relocations = self.relocationsSpin.value
        options = {
            "preview_n": int(self.previewSpin.value) if self.subjectsGroup.checkedId() == 1 else None,
            "preview_seed": int(self.previewSeedSpin.value),
            "extra_rules": [rule for key, _label, rule in EXTRA_RULES if self.ruleChecks[key].checked and self.ruleChecks[key].enabled],
            "sensitivity": float(self.sensitivitySpin.value),
            "specificity": self.specificityCombo.currentData if self.specificityCombo.enabled else "auto",
            "volume_off_reason": None if self.volumeCheck.checked else (self.volumeReason.text.strip() or None),
            "perm": int(self.permSpin.value),
            "tfce": bool(self.tfceCheck.checked),
            "scores": scores,
            "null_relocations": "auto" if relocations < 0 else int(relocations),
            "nulls": self.nullsCombo.currentData,
            "build_references": bool(self.referenceCheck.checked),
            "native": self.nativeCombo.currentData,
            "atlas": self.atlasCombo.currentData,
            "threads": int(self.threadsSpin.value) or None,
            "keep_awake": bool(self.awakeCheck.checked),
        }
        options.update(self.extra)
        return options

    def start(self):
        """Store the options and run everything with them."""
        self._update()
        if not self.startButton.enabled:
            return None
        options = self.values()
        saveOptions(options)
        self.dialog.close()
        self.card.scheduleRecheck()
        return self.card.run(options, recompute=bool(self.recomputeCheck.checked))

    def downloadAtlas(self):
        """Download Schaefer-400 (7 networks) on this explicit click only (L-15 fetch, in a background thread)."""
        if self.atlasJob is not None and self.atlasJob.get("thread") is not None and self.atlasJob["thread"].is_alive():
            return
        state = {"text": _("Downloading…"), "error": None, "done": False}

        def work():
            try:
                from LesionNetworkMappingLib import fetch

                def progress(done, total):
                    state["text"] = _("Downloading… {pct}%").format(pct=int(100 * done / total)) if total else _("Downloading… {mb:.1f} MB").format(mb=done / 1e6)

                fetch.fetch_set(fetch.find_resource(SCHAEFER_ID), progress=progress)
            except Exception as exc:
                state["error"] = f"{type(exc).__name__}: {exc}"
            state["done"] = True

        timer = qt.QTimer()
        timer.setInterval(100)

        def poll():
            if state["done"]:
                timer.stop()
                if state["error"]:
                    self.atlasStatus.setText(_("The download failed: {error}").format(error=state["error"]))
                    self.atlasJob = None
                    return
                self.atlasJob = None
                self._update()
                return
            time.sleep(0.01)  # lends the interpreter to the download thread
            self.atlasStatus.setText(state["text"])

        timer.connect("timeout()", poll)
        thread = threading.Thread(target=work, name="lnm-atlas-download", daemon=True)
        self.atlasJob = {"thread": thread, "timer": timer, "state": state, "text": state["text"]}
        thread.start()
        timer.start()
        self._update()


#
# Search entries and navigation (also used by StudioLib.libcard_analysis)
#


def setCohortSeed(widget):
    """Seed = Cohort in the Map widget (as a click on the segmented control)."""
    if widget is None or widget.seedMode() == "cohort" or "cohort" not in getattr(widget, "seedModes", ()):
        return
    index = widget.seedModes.index("cohort")
    widget.seedModeGroup.button(index).setChecked(True)
    widget.onSeedModeClicked(index)


def showInMap(then=None):
    """Show Map (Studio workspace, else the module) with Seed = Cohort and the Run everything card; then ``then(card)``."""
    shell = jobrunner._studioShell()
    if shell is not None:
        shell.switchTo("map")
    else:
        slicer.util.selectModule("LesionNetworkMapping")
    module = getattr(slicer.modules, "lesionnetworkmapping", None)
    widget = module.widgetRepresentation().self() if module is not None else _mapWidget()
    if widget is None:
        return None
    setCohortSeed(widget)
    widget.ctx.focusCard("pipeline")
    card = widget.ctx.card("pipeline")
    if card is not None and then is not None:
        return then(card)
    return card


def _cardOrNone():
    widget = _mapWidget()
    return widget.ctx.card("pipeline") if widget is not None and getattr(widget, "ctx", None) is not None else None


def paletteEntries(shell=None):
    """Search entries of the cohort analysis (dict form of the provider contract, SPEC 2.12, USABILITY 4)."""

    def hasReport():
        card = _cardOrNone()
        return card is None or bool(card.reportPath())

    def hasFolder():
        card = _cardOrNone()
        return card is None or bool(card.outFolder())

    return [
        {"id": "lnm.run-everything", "title": _("Run cohort analysis (lesion network mapping)…"), "category": _("Map"),
         "synonyms": ["map all lesions", "cohort", "pipeline", "run everything"], "callback": lambda: showInMap(lambda card: card.showOptions()),
         "description": _("Map every lesion of the study and run the group analyses, statistics and report"), "icon": "play"},
        {"id": "lnm.open-report", "title": _("Open last cohort report"), "category": _("Map"), "synonyms": ["cohort report", "report.html"],
         "callback": lambda: showInMap(lambda card: card.openReport()), "enabled": hasReport, "description": _("Open report.html of the last cohort analysis")},
        {"id": "lnm.show-analysis-folder", "title": _("Show cohort analysis folder"), "category": _("Map"), "synonyms": ["cohort folder", "derivatives"],
         "callback": lambda: showInMap(lambda card: card.showFolder()), "enabled": hasFolder, "description": _("Show the output folder of the cohort analysis")},
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
        logging.exception("Lesion network mapping: the cohort analysis Search entries could not be registered")
        return False
    return True


registerPaletteProvider()


CARD = {
    "id": "pipeline",
    "title": "Run everything",
    "order": ORDER_RUN_EVERYTHING,
    "modes": ("cohort",),
    "classic": True,
    "factory": PipelineCard,
}

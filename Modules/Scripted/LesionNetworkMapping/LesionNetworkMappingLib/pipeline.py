"""Automated cohort pipeline ("Run everything"): one command maps a study's lesions and runs every cohort analysis.

    PythonSlicer -m LesionNetworkMappingLib.pipeline run --study S --bundle B --accept-terms

This module orchestrates the stage jobs of the library; it computes no statistics and no maps itself.
Each stage has a *provider* (the module that does the work), resolved when the run is planned:
the preferred module, else a fallback of the wave-1 library, else "not available in this build"
(the stage is then reported, never re-implemented). The provider and the sha256 of its source file
are recorded per stage, so that an updated module recomputes its stage.

Stages, in order (``STAGES``):

====================  =========================================================  =================================
stage                 preferred provider                                         fallback
====================  =========================================================  =================================
prepare               this module: lock, data use terms, plan, input snapshots    -
preflight             preflight.run_preflight (in-process)                       the plan's own row checks
normalize             normalize batch (child job)                                unavailable: native rows excluded
reference             reference build (child; only with --build-references)      qa degree (child)
maps                  cohortmaps (child; batched, per-lesion cache)              engine per batch (children)
nulls                 nulls (child; exact pooled or fast)                        unavailable
overlap               cohort.group_maps (in-process)                             stats.binarize/overlap/sensitivity
lesionset             lesionset (in-process)                                     skipped
specificity           design.build_design + glm (child)                          unavailable
slnm                  slnm (child, once per confirmed score)                     unavailable
conjunction           cohort.conjunction (in-process)                            stats.conjunction
qa                    qa.group_qa / qa.lesion_qa (in-process)                    qa.degree_similarity
clusters              clusters.cluster_table + atlases (in-process)              stats.peak_table (labels n/a)
export                export.write_run + export.write_methods (L-18)             export.write_run's methods
report                report.render_report (A-04b)                               "report not available"
finish                inbox post (study status traced -> mapped)                 -
====================  =========================================================  =================================

``LNM_PIPELINE_PROVIDERS=baseline`` forces the fallbacks (tests). ``LNM_PIPELINE_NORMALIZER`` names a
module or a .py file that replaces the normalize batch job (tests).

Resume works at three levels: a stage whose fingerprint (provider, its source sha256, parameters and
the fingerprints of its inputs) matches a completed state with all its outputs present is reused
without starting anything; the cohort maps job keeps a per-lesion cache; and child jobs resume from
their own progress (``--resume``).

Output folder (``config.out``; default ``<study>/derivatives/lnm/conn-<label>[_n-<N>]``)::

    pipeline.json, report.html, reproduce_pipeline.sh, provenance.json, methods.md/.html/.txt,
    checklist.md, reproduce.sh, qa.json, lesionset.json, peaks.tsv, clusters_<desc>.tsv/.html,
    <sid>_conn-<label>_desc-{AvgR,AvgRFz,T}[PreviewN<k>]_funcmap.nii.gz and <sid>_conn-<label>_lnm.json,
    group_conn-<label>_desc-<desc>_map.nii.gz with a .json sidecar each,
    inputs/lesions/<sid>_desc-lesion_mask.nii.gz   byte copies of the analysed lesions
    stages/<stage>/                                child job folders (cohortmaps, glm, slnm-<Score>, nulls, ...)
    .pipeline/                                     lock.json, progress.json, STOP, plan.json, lesions.tsv,
                                                   mask.nii.gz, pipeline.log, stages/<stage>.json

Per-lesion maps are copied from the map cache (D-A21: never hard-linked). Every shared file holds
paths relative to the output folder (pipeline.json, sidecars, TSVs); provenance.json, reproduce.sh and
reproduce_pipeline.sh keep local paths by design.

Process control: children run as ``<python> -m LesionNetworkMappingLib.<module>`` with argument lists,
BLAS-capped through ``--threads`` and stopped through one STOP file (``.pipeline/STOP``), which the
pipeline creates when any cancel source fires: the STOP file itself, ``--cancel-file``, SIGINT/SIGTERM
(SIGBREAK on Windows), the ``--parent-pid`` watchdog and the orphan watchdog of ``jobs``. Events are
the JSON lines of ``jobs`` (``--events json``), or readable lines (``--events text``); the progress of
the run is also in ``.pipeline/progress.json``, which a GUI reads to re-attach.

Command line (exit codes 0 done or done with failed optional stages, 1 error, 2 usage, 3 cancelled,
4 data use terms not accepted, 5 blocked by preflight, 6 no eligible lesion, 75 locked)::

    python -m LesionNetworkMappingLib.pipeline init --from DIR [--participants TSV|CSV] --study OUT [--name NAME]
        [--score-columns a,b] [--group-column C] [--group-map 'patient=case,HC=control'] [--lesion-rule RX] [--id-regex RX]
    python -m LesionNetworkMappingLib.pipeline plan|run --study S --bundle B [options; see --help]
    python -m LesionNetworkMappingLib.pipeline status --out DIR [--json]
    python -m LesionNetworkMappingLib.pipeline report --out DIR [--atlas A --atlas-labels L --template T --figure-convention C]

Inside the Slicer application: ``pipeline.slicer_main([...])`` runs the command and exits the application
with its code. This module never imports slicer, qt, vtk or ctk, and imports numpy only when it runs.
"""

import os
import sys

if not __package__:  # run as a script (Slicer --python-script <Lib>/pipeline.py -- run ...)
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "LesionNetworkMappingLib"  # noqa: A001

if __name__ == "__main__":
    from LesionNetworkMappingLib.jobs import set_blas_threads

    set_blas_threads(sys.argv[1:])  # before numpy is imported: BLAS reads these variables once

import argparse
import collections
import dataclasses
import datetime
import glob
import hashlib
import importlib
import importlib.util
import json
import math
import queue
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time

from . import jobs

PACKAGE = __package__ or "LesionNetworkMappingLib"
LIB_DIR = os.path.dirname(os.path.abspath(__file__))
LIB_PARENT = os.path.dirname(LIB_DIR)

FORMAT_NAME = "slicer-lnm-pipeline"
FORMAT_VERSION = 1
GROUP_SIDECAR_FORMAT = "slicer-lnm-group-map"
QA_FORMAT = "slicer-lnm-pipeline-qa"
PIPELINE_JSON = "pipeline.json"
REPRODUCE_PIPELINE = "reproduce_pipeline.sh"
PRIVATE_DIR = ".pipeline"
STOP_NAME = "STOP"
LOCK_NAME = "lock.json"
PROGRESS_JSON = "progress.json"
PLAN_JSON = "plan.json"
LESIONS_TSV = "lesions.tsv"
#: maps.tsv of the kept lesions, written next to the cohortmaps maps.tsv when some lesions are excluded after mapping
PIPELINE_MAPS_TSV = "maps_kept.tsv"
MASK_NAME = "mask.nii.gz"
LOG_NAME = "pipeline.log"
LOG_MAX_BYTES = 5 * 1024**2
INPUTS_DIR = ("inputs", "lesions")
STAGES_DIR = "stages"
DATASET_DESCRIPTION = "dataset_description.json"
GENERATED_BY = "Slicer Studio LesionNetworkMappingLib"

EXIT_OK = jobs.EXIT_OK
EXIT_ERROR = jobs.EXIT_ERROR
EXIT_USAGE = jobs.EXIT_USAGE
EXIT_CANCELLED = jobs.EXIT_CANCELLED
EXIT_TERMS = 4
EXIT_BLOCKED = 5
EXIT_NO_LESION = 6
EXIT_LOCKED = jobs.EXIT_LOCKED

PROVIDERS_ENV = "LNM_PIPELINE_PROVIDERS"
NORMALIZER_ENV = "LNM_PIPELINE_NORMALIZER"
SLICER_ENV = "SLICER_EXECUTABLE"

#: (name, label) of the stages, in order
STAGES = (
    ("prepare", "Prepare"),
    ("preflight", "Preflight"),
    ("normalize", "Normalize native lesions"),
    ("reference", "Reference maps"),
    ("maps", "Network maps"),
    ("nulls", "Null models"),
    ("overlap", "Overlap and sensitivity"),
    ("lesionset", "Lesion-set diagnostics"),
    ("specificity", "Specificity (GLM)"),
    ("slnm", "Symptom network maps (sLNM)"),
    ("conjunction", "Conjunction"),
    ("qa", "Quality checks"),
    ("clusters", "Clusters and peaks"),
    ("export", "Export and methods"),
    ("report", "Figures and report"),
    ("finish", "Finish"),
)
STAGE_LABELS = dict(STAGES)
#: A failure of these stages fails the run (exit 1); the others end it as done_with_failures
CRITICAL_STAGES = ("prepare", "maps", "overlap", "export")
SPECIFICITY_MODES = ("auto", "glm", "legacy-welch", "legacy-liebermeister", "off")
NULL_MODES = ("auto", "exact", "fast", "off")
NATIVE_MODES = ("normalize", "skip", "include-unreviewed")
POPULATIONS = ("auto", "cases", "all")
EVENT_MODES = ("auto", "json", "text")
KEEP_AWAKE_MODES = ("auto", "on", "off")
INPUTS = ("AvgFz", "T")
MIN_PER_GROUP = 3  # subjects per contrast group for the GLM (preflight's MIN_PER_CONTRAST_GROUP)
MIN_SLNM_N = 10
DEFAULT_SENSITIVITY = 90.0
DEFAULT_PERM = 5000
MIN_PERM = 1000
NULL_BUDGET_MIN = 30.0
PROGRESS_INTERVAL_S = 0.25  # at most 4 progress events per second
PROGRESS_JSON_INTERVAL_S = 1.0
POLL_S = 0.1
CHILD_STOP_WAIT_S = 5.0
LOCKED_WAIT_S = 60.0
SESSION_STALE_S = 60.0
KEEP_AWAKE_AFTER_S = 600.0
LINK_MODE = "copy"  # D-A21: per-lesion maps are copied from the cache, never hard-linked

# Estimate model (seconds; measured on a 4-core host, see the A-04 work package)
ENGINE_SUBJECT_S = 0.13
ENGINE_LESION_S = 0.0042
ENGINE_NULL_S = 0.0005
ENGINE_REFERENCE = 292_019 * 216  # V x time points of the measurements
READ_RATE = 400e6  # bytes per second of a connectome pass
ENGINE_BYTES_PER_VOXEL = 36  # addendum §3.13 (L_max = budget / (36 V))
GLM_PERM_S = 0.011  # per permutation at V = 292,019 (3 threads; glm.py measured 10.8 ms at n = 40, 6.8 ms at n = 200)
TFCE_PERM_S = 2.6
REFERENCE_PC_S = 900.0
NORMALIZE_SUBJECT_S = 31.0
REPORT_S = 2.0
REPORT_LESION_S = 0.05
IN_PROCESS_S = 1.0

NOT_AVAILABLE = "not available in this build"
NULLS_TOO_LONG = "Null models not run: the exact pooled null would take ≈ {} on this connectome; run it from Map ▸ Nulls"
REFERENCE_MISSING = "Reference maps not built: they are built only when asked (--build-references; about {})"
REFERENCE_READ_ONLY = "Reference maps not computed: the connectome folder is read-only (build them in Connectome ▸ Manage…)"
UNCONFIRMED_SCORE = "confirm the column role in Library ▸ Columns"
NORM_NEEDS_REVIEW = "normalized; needs review in Library (norm_qc)"
NATIVE_UNAVAILABLE = "native space: normalization not available in this build"
NATIVE_SKIPPED = "native space: skipped (--native skip)"
NATIVE_EXCLUDED = "native space: not normalized"
CHANGED_SNAPSHOT = "changed during snapshot"
CHANGED_RUN = "changed during the run"
OUTSIDE_MASK = "no lesion voxel inside the connectome mask (its maps would be all zero)"
QC_FAIL = "qc=fail"
#: Sidecar "kind" of the nulls job's group maps (nulls.OUTPUT_DESCS): the excess is in signed percentage points
NULL_MAP_KINDS = {"sensNullExcess": "stat", "sensNullP": "p", "sensNullQ": "p", "sensNullPFwe": "p", "sensNullBias": "fraction"}
UNIVERSAL_NOTE = "Group maps of the pipeline are descriptive unless they carry a permutation p-value; specificity is never claimed from overlap maps."

_DESC = re.compile(r"[^A-Za-z0-9]")


class PipelineError(jobs.JobError):
    """An expected failure of the run (error event, exit code)."""


# Small helpers


def _now_iso():
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def fingerprint(obj):
    """sha256 of the canonical JSON of ``obj`` (sorted keys)."""
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


def file_sha256(path, block=1 << 20):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(block), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data


def _number(value):
    """``value`` as a float, or None when it is missing or not a number ("n/a")."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _write_text_atomic(path, text, mode=None):
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return path


def _copy_atomic(source, target):
    tmp = f"{target}.{os.getpid()}.tmp"
    try:
        shutil.copyfile(source, tmp)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return target


def relpath(path, base):
    """``path`` relative to ``base`` with "/" separators (absolute when on another drive), None for None."""
    if path is None:
        return None
    try:
        return os.path.relpath(os.path.abspath(os.fspath(path)), os.path.abspath(os.fspath(base))).replace(os.sep, "/")
    except ValueError:
        return os.path.abspath(os.fspath(path))


def abspath(path, base):
    """Inverse of :func:`relpath`."""
    if path is None:
        return None
    path = os.fspath(path)
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(os.fspath(base), *path.split("/")))


def _duration_text(seconds):
    if seconds is None:
        return "unknown"
    seconds = float(seconds)
    if seconds < 90:
        return f"{max(seconds, 1):.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def rule_tag(rule):
    """Short tag of a threshold rule for desc labels: FWE05, T7, Fz005, Top10, T5p5 (letters and digits)."""
    from . import thresholds

    rule = thresholds.normalize_rule(rule)

    def number(value):
        text = f"{float(value):g}"
        return text.replace(".", "p").replace("-", "m")

    kind = rule["kind"]
    if kind == "fwe_bonferroni":
        return "FWE" + f"{float(rule.get('alpha', 0.05)):g}".replace("0.", "").replace(".", "")
    if kind == "t_convention":
        return "T" + number(rule.get("value", 7))
    if kind == "custom_t":
        return "T" + number(rule.get("value", 5))
    if kind == "fz_abs":
        return "Fz" + f"{float(rule.get('value', 0.05)):g}".replace(".", "")
    if kind == "top_pct":
        return "Top" + number(rule.get("value", 10))
    return _DESC.sub("", kind.title())


def score_label(column):
    """CamelCase letters and digits of a column: "aphasia_score" gives "AphasiaScore"."""
    words = [word for word in re.split(r"[^A-Za-z0-9]+", str(column)) if word]
    return "".join(word[:1].upper() + word[1:] for word in words) or "Score"


def unique_labels(names, make=score_label):
    out, seen = [], set()
    for name in names:
        label = make(name)
        base, counter = label, 2
        while label in seen:
            label, counter = f"{base}{counter}", counter + 1
        seen.add(label)
        out.append(label)
    return out


def _python_executable():
    """The Python interpreter of the child jobs: sys.executable, or PythonSlicer inside the Slicer application."""
    executable = sys.executable or ""
    name = os.path.basename(executable).lower()
    if name.startswith("slicer") or not executable:
        folder = os.path.dirname(executable)
        for candidate in ("PythonSlicer", "PythonSlicer.exe", "python-real", "python3", "python"):
            path = os.path.join(folder, candidate)
            if os.path.isfile(path):
                return path
        found = shutil.which("PythonSlicer")
        if found:
            return found
    return executable


def _requires_slicer(origin):
    """True when a stage module declares ``REQUIRES_SLICER = True`` (read from its source, not imported)."""
    try:
        with open(origin, encoding="utf-8") as f:
            return re.search(r"^REQUIRES_SLICER\s*=\s*True\b", f.read(), re.MULTILINE) is not None
    except OSError:
        return False


def _slicer_executable(configured=None):
    return configured or os.environ.get(SLICER_ENV) or shutil.which("Slicer")


# Providers


@dataclasses.dataclass
class Provider:
    """The module that runs a stage: ``module`` (None: this module), ``origin`` (its source file) and ``sha256``."""

    stage: str
    module: str | None
    kind: str  # "preferred", "fallback", "builtin" or "unavailable"
    origin: str | None = None
    sha256: str | None = None
    reason: str | None = None

    @property
    def available(self):
        return self.kind != "unavailable"

    def to_dict(self):
        return {"stage": self.stage, "module": self.module, "kind": self.kind, "sha256": self.sha256, "reason": self.reason}


_SOURCE_CACHE = {}


def _module_source(module):
    """(origin, text) of a library module (or a .py path), or (None, None) when it does not exist."""
    if module in _SOURCE_CACHE:
        return _SOURCE_CACHE[module]
    origin = None
    if module.endswith(".py") or os.sep in module or "/" in module:
        origin = module if os.path.isfile(module) else None
    else:
        name = module if "." in module else f"{PACKAGE}.{module}"
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            spec = None
        origin = getattr(spec, "origin", None) if spec is not None else None
    text = None
    if origin and os.path.isfile(origin):
        with open(origin, encoding="utf-8") as f:
            text = f.read()
    _SOURCE_CACHE[module] = (origin if text is not None else None, text)
    return _SOURCE_CACHE[module]


def _has(text, names):
    return text is not None and all(re.search(rf"^(def {re.escape(name)}\(|class {re.escape(name)}\b|{re.escape(name)}\s*=)", text, re.MULTILINE) for name in names)


def _provider(stage, module, names, kind):
    origin, text = _module_source(module)
    if origin is None or not _has(text, names):
        return None
    return Provider(stage, module, kind, origin, hashlib.sha256(text.encode("utf-8")).hexdigest())


#: Preferred and fallback providers: stage -> [(module, required names)]; "builtin" is this module
PROVIDER_TABLE = {
    "preflight": ([("preflight", ("run_preflight", "save_report", "blocking_checks"))], "builtin"),
    "normalize": ([("normalize", ("batch", "COMMANDS"))], None),
    "reference": ([("reference", ("build", "load_reference"))], ("qa", ("_degree_command",))),
    "maps": ([("cohortmaps", ("run", "lookup", "read_maps_tsv"))], ("engine", ("main", "output_paths"))),
    "nulls": ([("nulls", ("run", "estimate", "find_fast_index"))], None),
    "overlap": ([("cohort", ("group_maps", "load_masked"))], ("stats", ("binarize", "overlap", "sensitivity_map"))),
    "lesionset": ([("lesionset", ("load_occupancies", "overlap_maps", "summarize"))], None),
    "specificity": ([("glm", ("main", "contrast_label")), ("design", ("build_design", "save_design"))], None),
    "slnm": ([("slnm", ("main", "score_label"))], None),
    "conjunction": ([("cohort", ("conjunction",))], ("stats", ("conjunction",))),
    "qa": ([("qa", ("group_qa", "lesion_qa", "load_reference"))], ("qa", ("degree_similarity",))),
    "clusters": ([("clusters", ("cluster_table", "write_clusters", "atlas_labeler"))], ("stats", ("peak_table",))),
    "export": ([("export", ("write_run", "write_methods")), ("methods", ("methods_sections",)), ("checklist", ("write_checklist",))], ("export", ("write_run",))),
    "report": ([("report", ("render_report",))], None),
}


def resolve_providers(baseline=None):
    """{stage: Provider} for every stage; ``baseline`` (default: $LNM_PIPELINE_PROVIDERS == "baseline") forces the fallbacks."""
    if baseline is None:
        baseline = os.environ.get(PROVIDERS_ENV, "").strip().lower() == "baseline"
    out = {"prepare": Provider("prepare", None, "builtin"), "finish": Provider("finish", None, "builtin")}
    for stage, (preferred, fallback) in PROVIDER_TABLE.items():
        chosen = None
        if not baseline:
            found = [_provider(stage, module, names, "preferred") for module, names in preferred]
            if found and all(found):
                chosen = found[0]
        if stage == "normalize" and os.environ.get(NORMALIZER_ENV):
            override = os.environ[NORMALIZER_ENV]
            origin, text = _module_source(override)
            chosen = Provider(stage, override, "preferred", origin, hashlib.sha256((text or "").encode("utf-8")).hexdigest()) if origin else None
        if chosen is None and fallback == "builtin":
            chosen = Provider(stage, None, "builtin")
        elif chosen is None and fallback is not None:
            chosen = _provider(stage, fallback[0], fallback[1], "fallback")
        if chosen is None:
            chosen = Provider(stage, None, "unavailable", reason=NOT_AVAILABLE)
        elif chosen.origin and _requires_slicer(chosen.origin) and not _slicer_executable():
            chosen = Provider(stage, chosen.module, "unavailable", chosen.origin, chosen.sha256, "unavailable: needs the Slicer application (pass --slicer)")
        out[stage] = chosen
    return out


# Configuration


@dataclasses.dataclass
class PipelineConfig:
    """Settings of a pipeline run (``to_json`` / ``from_json``; unknown keys are rejected). Defaults: addendum §3."""

    study: str | None = None
    bundle: str | None = None
    out: str | None = None
    preview_n: int | None = None
    preview_seed: int = 0
    rule: dict | None = None
    extra_rules: list = dataclasses.field(default_factory=list)
    allow_rule_anyway: bool = False
    sensitivity: float = DEFAULT_SENSITIVITY
    sensitivity_population: str = "auto"
    specificity: str = "auto"
    input: str = "AvgFz"
    perm: int = DEFAULT_PERM
    perm_seed: int = 0
    alpha: float = 0.05
    fdr: bool = True
    tfce: bool = False
    covariates: list = dataclasses.field(default_factory=lambda: ["volume_ml"])
    volume_off_reason: str | None = None
    scores: object = "auto"
    slnm_method: str = "pearson"
    min_slnm_n: int = MIN_SLNM_N
    null_relocations: object = "auto"
    null_seed: int = 0
    nulls: str = "auto"
    nulls_budget_min: float = NULL_BUDGET_MIN
    reference: str = "auto"
    build_references: bool = False
    native: str = "normalize"
    include_unreviewed: bool = False
    strict: bool = False
    memory_budget_gb: float | None = None
    threads: int | None = None
    nice: int = 0
    keep_awake: str = "auto"
    template: str | None = None
    atlas: str | None = None
    atlas_labels: str | None = None
    figure_convention: str | None = None
    report: bool = True
    recompute: bool = False
    software: dict = dataclasses.field(default_factory=dict)
    parent_pid: int | None = None
    parent_run: list = dataclasses.field(default_factory=list)
    events: str = "auto"
    slicer: str | None = None
    accept_terms: bool = False

    #: Keys that are about this invocation, not about the analysis (not stored in pipeline.json "config")
    RUN_KEYS = ("out", "recompute", "parent_pid", "parent_run", "events", "accept_terms", "slicer", "nice", "keep_awake", "threads", "memory_budget_gb")
    #: Path keys (stored relative to pipeline.json's folder)
    PATH_KEYS = ("study", "bundle", "out", "atlas", "atlas_labels", "template")

    def __post_init__(self):
        self.validate()

    def validate(self):
        from . import thresholds

        if self.preview_n is not None:
            self.preview_n = int(self.preview_n)
            if self.preview_n < 1:
                raise ValueError("--preview must be at least 1")
        self.rule = thresholds.normalize_rule(self.rule)
        self.extra_rules = [thresholds.normalize_rule(rule) for rule in (self.extra_rules or [])]
        self.sensitivity = float(self.sensitivity)
        if not 50.0 <= self.sensitivity <= 100.0:
            raise ValueError(f"The sensitivity must be between 50 and 100 %, got {self.sensitivity:g}")
        for key, allowed in (("sensitivity_population", POPULATIONS), ("specificity", SPECIFICITY_MODES), ("input", INPUTS), ("nulls", NULL_MODES), ("native", NATIVE_MODES),
                             ("events", EVENT_MODES), ("keep_awake", KEEP_AWAKE_MODES), ("slnm_method", ("pearson", "spearman")), ("reference", ("auto", "off"))):
            if getattr(self, key) not in allowed:
                raise ValueError(f"{key} must be one of {', '.join(allowed)}, got {getattr(self, key)!r}")
        self.perm = int(self.perm)
        if self.perm < MIN_PERM:
            raise ValueError(f"--perm must be at least {MIN_PERM} (got {self.perm})")
        if not 0.0 < float(self.alpha) < 1.0:
            raise ValueError("--alpha must be between 0 and 1")
        self.alpha = float(self.alpha)
        self.covariates = [str(name) for name in (self.covariates or [])]
        if "volume_ml" not in self.covariates and not (self.volume_off_reason or "").strip():
            raise ValueError("Turning the lesion-volume covariate off needs a reason (--no-volume-covariate REASON)")
        if isinstance(self.scores, str) and self.scores not in ("auto", "none"):
            self.scores = [name.strip() for name in self.scores.split(",") if name.strip()]
        if isinstance(self.null_relocations, str) and self.null_relocations != "auto":
            self.null_relocations = int(self.null_relocations)
        if isinstance(self.null_relocations, int) and self.null_relocations < 0:
            raise ValueError("--null-relocations must be at least 0")
        if int(self.min_slnm_n) < 3:
            raise ValueError("--min-slnm-n must be at least 3")
        if self.memory_budget_gb is not None and float(self.memory_budget_gb) <= 0:
            raise ValueError("--memory-budget-gb must be positive")
        if self.threads is not None and int(self.threads) < 1:
            raise ValueError("--threads must be at least 1")
        if not isinstance(self.software, dict):
            raise ValueError("--software must be a JSON object")
        if isinstance(self.parent_run, str):
            self.parent_run = [self.parent_run]
        self.parent_run = [str(run_id) for run_id in (self.parent_run or []) if run_id]
        return self

    @property
    def rules(self):
        """The primary rule, then the extra rules (secondary results only)."""
        return [self.rule, *self.extra_rules]

    def to_json(self, base=None, analysis_only=False):
        """JSON-compatible dict; with ``base`` the paths are relative to it; ``analysis_only`` leaves out the run options."""
        out = {}
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if analysis_only and field.name in self.RUN_KEYS:
                continue
            if base is not None and field.name in self.PATH_KEYS and isinstance(value, str) and os.path.isabs(value):
                value = relpath(value, base)
            out[field.name] = value
        return json.loads(_canonical(out))

    @classmethod
    def from_json(cls, data, base=None):
        """A config from :meth:`to_json` content, or from a pipeline.json (its "config"); relative paths are resolved against ``base``."""
        if isinstance(data, dict) and data.get("format") == FORMAT_NAME:
            data = data.get("config") or {}
        if not isinstance(data, dict):
            raise ValueError("A pipeline configuration must be a JSON object")
        names = {field.name for field in dataclasses.fields(cls)}
        unknown = sorted(set(data) - names)
        if unknown:
            raise ValueError(f"Unknown configuration key(s): {', '.join(unknown)}")
        values = dict(data)
        if base is not None:
            for key in cls.PATH_KEYS:
                if isinstance(values.get(key), str) and values[key] and not os.path.isabs(values[key]):
                    values[key] = abspath(values[key], base)
        return cls(**values)

    @classmethod
    def load(cls, path):
        path = os.path.abspath(os.fspath(path))
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_json(data, base=os.path.dirname(path))


# Estimates


def engine_seconds_per_subject(n_lesions, null_k=0, n_voxels=292_019, time_points=216, threads=2):
    """Engine time per connectome subject of one pass of ``n_lesions`` lesions (in memory): the model of the plan."""
    scale = n_voxels * time_points / ENGINE_REFERENCE * (2.0 / max(int(threads or 2), 1)) ** 0.8
    return (ENGINE_SUBJECT_S + ENGINE_LESION_S * n_lesions + ENGINE_NULL_S * null_k * n_lesions) * scale


def engine_batch_size(n_voxels, budget_bytes):
    return int(min(100, max(1, budget_bytes // max(ENGINE_BYTES_PER_VOXEL * n_voxels, 1))))


def estimate_maps(n_lesions, n_subjects, n_voxels, time_points, subject_bytes, null_k, threads, budget_bytes):
    """Seconds of the maps stage: passes of at most L_max lesions, each at least one read of the connectome."""
    if n_lesions <= 0:
        return 0.0
    size = engine_batch_size(n_voxels, budget_bytes)
    total, left = 0.0, n_lesions
    while left > 0:
        count = min(size, left)
        compute = n_subjects * engine_seconds_per_subject(count, null_k, n_voxels, time_points, threads)
        total += max(compute, n_subjects * subject_bytes / READ_RATE)
        left -= count
    return total


def estimate_glm(n_perm, n_voxels, tfce=False):
    return n_perm * (GLM_PERM_S * n_voxels / 292_019 + (TFCE_PERM_S if tfce else 0.0))


def _default_budget_bytes():
    total = None
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        total = None
    if not total:
        return 1024**3
    return int(min(0.25 * total, 4 * 1024**3))


# Events


class Emitter:
    """Events of the run: JSON lines (``jobs.emit``) or readable text, mirrored to pipeline.log and progress.json.

    Progress events are throttled to at most 4 per second; progress.json is rewritten at most once per second
    (and at every stage change). ``sink`` (a callable ``sink(event, fields)``) replaces standard output.
    """

    def __init__(self, mode="auto", sink=None):
        if mode == "auto":
            try:
                mode = "text" if sys.stdout.isatty() else "json"
            except (AttributeError, ValueError):
                mode = "json"
        self.mode = mode
        self.sink = sink
        self.log_path = None
        self.progress_path = None
        self.replacements = []
        self.progress = {"pid": os.getpid(), "run_id": None, "started": _now_iso(), "updated": None, "stage": "prepare", "index": 1, "total": len(STAGES), "done": None, "n": None, "eta": None, "percent": 0.0, "message": None}
        self.weights = {}
        self._last_progress = 0.0
        self._last_file = 0.0
        self._lock = threading.Lock()
        self.counts = collections.Counter()

    def attach(self, log_path=None, progress_path=None, replacements=()):
        self.log_path, self.progress_path = log_path, progress_path
        self.replacements = sorted(((os.path.abspath(a), b) for a, b in replacements if a), key=lambda item: -len(item[0]))

    def _portable(self, text):
        for path, label in self.replacements:
            text = text.replace(path, label)
        return text

    def __call__(self, event, **fields):
        now = time.monotonic()
        if event == "progress":
            self._update_progress(fields)
            if now - self._last_progress < PROGRESS_INTERVAL_S and not fields.get("final"):
                self._maybe_write_progress(now)
                return None
            self._last_progress = now
        elif event == "stage":
            self._update_stage(fields)
        self.counts[event] += 1
        fields.pop("final", None)
        if self.sink is not None:
            record = {"event": event, "t": round(jobs.elapsed(), 3), **fields}
            self.sink(event, record)
        elif self.mode == "text":
            line = self.text(event, fields)
            if line:
                try:
                    sys.stdout.write(line + "\n")
                    sys.stdout.flush()
                except (OSError, ValueError):
                    pass
        else:
            self._write_json(event, fields)
        self._log(event, fields)
        self._maybe_write_progress(now, force=event in ("stage", "start", "done", "error", "cancelled"))
        return fields

    def _write_json(self, event, fields):
        """One event line in the schema of ``jobs.emit`` (event and t first; start with version and pid), as ASCII JSON."""
        if event == "start":
            fields.setdefault("pid", os.getpid())
            fields.setdefault("version", jobs.library_version())
        record = {"event": event, "t": round(jobs.elapsed(), 3), **{key: value for key, value in fields.items() if key not in ("event", "t")}}
        line = json.dumps(json.loads(jobs.to_json(record)), ensure_ascii=True, allow_nan=False) + "\n"
        with self._lock:
            try:
                sys.stdout.write(line)
                sys.stdout.flush()
            except (BrokenPipeError, ValueError, OSError):
                pass  # the reader is gone: the run goes on (the log and progress.json still record it)

    def text(self, event, fields):
        """Readable line of an event (``--events text``)."""
        stage = fields.get("stage") or fields.get("name")
        names = [name for name, _ in STAGES]
        head = f"[{names.index(stage) + 1}/{len(STAGES)} {STAGE_LABELS[stage]}]" if stage in STAGE_LABELS else ""
        if event == "start":
            return f"Pipeline {fields.get('run_id') or ''}: {fields.get('n_rows')} subjects, {fields.get('n_compute')} to map, {fields.get('n_reuse')} reused, {fields.get('n_excluded')} excluded; about {_duration_text(fields.get('estimate_s'))}"
        if event == "stage":
            reason = fields.get("reason")
            return f"{head} {fields.get('status')}" + (f": {reason}" if reason else "")
        if event == "progress":
            parts = []
            if fields.get("batch") and fields.get("batches"):
                parts.append(f"batch {fields['batch']}/{fields['batches']}")
            if fields.get("done") is not None and fields.get("total"):
                parts.append(f"{fields['done']}/{fields['total']} {fields.get('unit') or 'units'}")
            if fields.get("eta") is not None:
                parts.append(f"~{_duration_text(fields['eta'])} left")
            if fields.get("message"):
                parts.append(str(fields["message"]))
            return f"{head} " + " · ".join(parts)
        if event == "warning":
            who = fields.get("participant_id")
            return f"{head} warning: " + (f"{who}: " if who else "") + str(fields.get("message"))
        if event == "info":
            return f"{head} " + str(fields.get("message"))
        if event == "error":
            return f"error ({fields.get('code')}): {fields.get('message')}" + "".join(f"\n  - {fix}" for fix in fields.get("fixes") or [])
        if event == "cancelled":
            return f"cancelled during {STAGE_LABELS.get(fields.get('stage'), fields.get('stage'))}; kept: {', '.join(fields.get('kept') or []) or 'nothing'}"
        if event == "done":
            failed = fields.get("failed_stages") or []
            return f"done: {fields.get('computed')} computed, {fields.get('reused')} reused, {len(fields.get('excluded') or [])} excluded" + (f"; failed stages: {', '.join(failed)}" if failed else "") + f"; {fields.get('out')}"
        if event == "result":
            return None
        return f"{head} {event}: " + ", ".join(f"{key}={value}" for key, value in fields.items() if key not in ("stage",))[:300]

    def _update_stage(self, fields):
        with self._lock:
            self.progress.update({"stage": fields.get("name"), "index": fields.get("index"), "total": fields.get("total"), "done": None, "n": None, "eta": None, "message": fields.get("status")})
            if fields.get("status") in ("done", "reused", "skipped", "unavailable", "failed", "blocked"):
                self.weights.setdefault("_done", set()).add(fields.get("name"))
            self.progress["percent"] = self._percent(None)

    def _update_progress(self, fields):
        with self._lock:
            done, total = fields.get("done"), fields.get("total")
            self.progress.update({"done": done, "n": total, "eta": fields.get("eta"), "message": fields.get("message")})
            fraction = done / total if isinstance(done, (int, float)) and isinstance(total, (int, float)) and total else None
            self.progress["percent"] = self._percent(fraction)

    def _percent(self, fraction):
        weights = {key: value for key, value in self.weights.items() if key != "_done"}
        total = sum(weights.values())
        if not total:
            return round(100.0 * (self.progress.get("index") or 0) / max(self.progress.get("total") or 1, 1), 1)
        finished = self.weights.get("_done", set())
        value = sum(weight for name, weight in weights.items() if name in finished)
        current = self.progress.get("stage")
        if fraction is not None and current in weights and current not in finished:
            value += weights[current] * min(max(fraction, 0.0), 1.0)
        return round(100.0 * value / total, 1)

    def _maybe_write_progress(self, now, force=False):
        if not self.progress_path or (not force and now - self._last_file < PROGRESS_JSON_INTERVAL_S):
            return
        self._last_file = now
        with self._lock:
            self.progress["updated"] = _now_iso()
            content = dict(self.progress)
        try:
            jobs.write_json_atomic(self.progress_path, content)
        except OSError:
            pass

    def _log(self, event, fields):
        if not self.log_path:
            return
        line = self.text(event, dict(fields)) if event != "result" else f"result: {fields.get('kind')} {fields.get('path') or ''}"
        if not line:
            return
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            if os.path.exists(self.log_path) and os.path.getsize(self.log_path) > LOG_MAX_BYTES:
                os.replace(self.log_path, self.log_path + ".1")
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"{stamp} {self._portable(line)}\n")
        except OSError:
            pass


# Cancellation


class Cancel:
    """Cancellation of the run: the STOP file, --cancel-file, signals and watchdogs (via ``jobs``).

    When any source fires, the STOP file is created, so the child jobs (which watch it) stop as well.
    Usable wherever the library accepts ``cancel`` (callable, ``is_set``, ``cancelled``, ``raise_if_cancelled``).
    """

    def __init__(self, stop_path, extra_path=None, signals=None):
        self.stop_path = stop_path
        self.extra_path = extra_path
        self.token = jobs.CancelToken(stop_path, signals=signals)
        self.reason = None

    def cancelled(self):
        fired = self.token.cancelled()
        if not fired and self.extra_path and os.path.exists(self.extra_path):
            self.token.cancel("cancel file")
            fired = True
        if fired:
            self.reason = self.reason or self.token.reason
            if not os.path.exists(self.stop_path):
                try:
                    with open(self.stop_path, "w", encoding="utf-8") as f:
                        f.write(f"{self.reason or 'cancelled'}\n")
                except OSError:
                    pass
        return fired

    is_set = cancelled

    def __call__(self):
        return self.cancelled()

    def cancel(self, reason=None):
        self.token.cancel(reason)
        self.cancelled()

    def raise_if_cancelled(self, done=None):
        if self.cancelled():
            raise jobs.Cancelled(done=done, reason=self.reason)


# Plan


@dataclasses.dataclass
class Plan:
    """What a run will do (:func:`plan`): rows with a status, stages with providers and estimates, blocks and warnings."""

    config: PipelineConfig
    out: str
    study: object
    bundle: object
    providers: dict
    subject_ids: list
    subjects_sha256: str
    manifest_sha256: str
    rows: list
    stages: list
    blocks: list
    warnings: list
    infos: list
    overrides: list
    estimate_s: float
    null_k: int
    specificity: str
    specificity_reason: str | None
    scores: list
    preflight: dict | None
    budget_bytes: int
    elapsed: float = 0.0

    @property
    def n_subjects(self):
        return len(self.subject_ids)

    def count(self, status):
        return sum(1 for row in self.rows if row["status"] == status)

    @property
    def eligible(self):
        return [row for row in self.rows if row["status"] in ("compute", "reuse")]

    @property
    def exit_code(self):
        if self.blocks:
            return EXIT_BLOCKED
        if not self.eligible and not self.count("normalize"):
            return EXIT_NO_LESION
        return EXIT_OK

    def to_json(self, portable=True):
        base = self.out if portable else None

        def path(value):
            return relpath(value, base) if base and value else value

        rows = []
        for row in self.rows:
            item = {key: value for key, value in row.items() if key not in ("path", "row")}
            item["file"] = path(row.get("path"))
            item["snapshot"] = path(row.get("snapshot"))
            rows.append(item)
        return {
            "format": FORMAT_NAME + "-plan",
            "version": FORMAT_VERSION,
            "created": _now_iso(),
            "study": path(self.study.path),
            "bundle": path(self.bundle.path),
            "out": None if portable else self.out,
            "connectome": {"name": self.bundle.name, "manifest_sha256": self.manifest_sha256, "N": self.n_subjects, "subject_ids_sha256": self.subjects_sha256, "n_voxels": self.bundle.n_voxels},
            "preview": self.config.preview_n,
            "rows": rows,
            "counts": {status: self.count(status) for status in ("compute", "reuse", "normalize", "excluded")},
            "stages": self.stages,
            "providers": {name: provider.to_dict() for name, provider in self.providers.items()},
            "blocks": self.blocks,
            "warnings": self.warnings,
            "infos": self.infos,
            "overrides": self.overrides,
            "null_relocations": self.null_k,
            "specificity": {"mode": self.specificity, "reason": self.specificity_reason},
            "scores": self.scores,
            "estimate_s": round(self.estimate_s, 1),
            "elapsed": round(self.elapsed, 4),
        }


def default_out(config, bundle_name):
    from .export import derivatives_dir

    return os.path.abspath(config.out) if config.out else derivatives_dir(config.study, bundle_name, config.preview_n)


def _snapshot_name(sid, path):
    extension = ".nii.gz" if path.lower().endswith(".nii.gz") else ".nii" if path.lower().endswith(".nii") else os.path.splitext(path)[1]
    return f"{sid}_desc-lesion_mask{extension}"


def _lesion_sha(path):
    try:
        return file_sha256(path)
    except OSError:
        return None


def _overrides(config):
    overrides = set()
    if config.include_unreviewed:
        overrides |= {"unreviewed", "auto_unreviewed"}
    if config.native == "include-unreviewed":
        overrides.add("norm_qc")
    return sorted(overrides)


def _builtin_row_checks(study, config):
    """{sid: reason} of the rows the plan's own checks exclude (the fallback of preflight.run_preflight)."""
    from . import nifti
    from .study import EXCLUDED_INCLUDE, EXCLUDED_NORM_QC, EXCLUDED_REVIEW, is_missing

    excluded = {}
    review = study.review_required and not config.include_unreviewed
    for row in study.subjects:
        sid = row["participant_id"]
        if row.get("include", "1") in ("0", "false", "no"):
            excluded[sid] = EXCLUDED_INCLUDE
            continue
        path = study.abspath(row.get("lesion"))
        if not path or not os.path.isfile(path):
            excluded[sid] = "lesion file missing"
            continue
        try:
            header = nifti.read_nifti_header(path)
        except (OSError, EOFError, ValueError, IndexError):
            excluded[sid] = "lesion file unreadable"
            continue
        if len([n for n in header["shape"] if n > 1]) > 3 or len(header["shape"]) < 3:
            excluded[sid] = "lesion file not 3D"
            continue
        volume = row.get("volume_ml")
        if not is_missing(volume):
            try:
                if float(volume) <= 0:
                    excluded[sid] = "empty lesion"
                    continue
            except ValueError:
                pass
        try:
            data = nifti.read_nifti(path)[0]
            if not (data != 0).any():
                excluded[sid] = "empty lesion"
                continue
        except (OSError, EOFError, ValueError):
            excluded[sid] = "lesion file unreadable"
            continue
        normalized = not is_missing(row.get("norm_method"))
        if row.get("space") == "native" and not normalized:
            excluded[sid] = NATIVE_EXCLUDED
            continue
        if normalized and row.get("norm_qc") != "pass" and config.native != "include-unreviewed":
            excluded[sid] = EXCLUDED_NORM_QC
            continue
        if review and row.get("review_status") != "approved":
            excluded[sid] = EXCLUDED_REVIEW
    return excluded


def _specificity_decision(config, groups):
    """("glm" | "skip" | "unavailable" | "legacy-...", reason) from the groups of the rows to be mapped."""
    counts = collections.Counter(groups)
    cases, controls = counts.get("case", 0), counts.get("control", 0)
    if config.specificity == "off":
        return "skip", "specificity is off (--specificity off)"
    if config.specificity.startswith("legacy"):
        return "unavailable", "the legacy two-group route has no permutation inference in this build (use --specificity glm)"
    if cases >= MIN_PER_GROUP and controls >= MIN_PER_GROUP:
        return "glm", None
    reason = f"needs at least {MIN_PER_GROUP} cases and {MIN_PER_GROUP} controls (have {cases} and {controls})"
    return ("block" if config.specificity == "glm" else "skip"), reason


def plan(config, hash_lesions=True, providers=None):
    """Plan a run without computing or writing anything (stdlib, numpy and file reads; never MRML or Qt).

    :return: :class:`Plan`. ``Plan.exit_code`` is 0, 5 (run-level blocks) or 6 (no eligible lesion).
    """
    from . import engine
    from .connectome import MANIFEST_NAME, ConnectomeBundle
    from .export import subjects_sha256
    from .study import Study, is_missing

    started = time.perf_counter()
    if not config.study or not config.bundle:
        raise ValueError("--study and --bundle are required")
    providers = providers or resolve_providers()
    study = Study.open(config.study)
    bundle = ConnectomeBundle.open(config.bundle)
    out = default_out(config, bundle.name)
    subject_ids = engine.resolve_subjects(bundle, config.preview_n, bool(config.preview_n), config.preview_seed)
    subjects_sha = subjects_sha256(subject_ids)
    manifest_sha = file_sha256(os.path.join(bundle.path, MANIFEST_NAME))
    overrides = _overrides(config)
    blocks, warnings, infos = [], [], []
    report = None

    # Rows: preflight (L-17), else the plan's own checks
    if providers["preflight"].kind == "preferred":
        from . import preflight

        design_spec = None
        if config.specificity == "glm":
            design_spec = {"test": "two_group", "group_column": "group", "levels": ["case", "control"], "contrast": "two_sided", "covariates": config.covariates,
                           "input": "Fz" if config.input == "AvgFz" else "T", "inference": {"n_perm": config.perm, "seed": config.perm_seed, "tfce": config.tfce, "fdr": config.fdr},
                           "volume_off_reason": config.volume_off_reason}
        report = preflight.run_preflight(study, bundle, recipe_rule=config.rule, design_spec=design_spec, accepted_terms=bool(config.accept_terms), strict=config.strict,
                                         overrides=overrides, n_subjects=len(subject_ids), out_dir=out)
        excluded = dict(report["excluded"])
        for check in preflight.blocking_checks(report):
            if check["id"] in ("terms_not_accepted", "no_eligible"):
                continue
            blocks.append({"id": check["id"], "message": check["message"], "fix": (check.get("fix") or {}).get("label")})
        for check in report["checks"]:
            if check["level"] == "warn" and check["scope"] == "run":
                warnings.append({"code": check["id"], "message": check["message"]})
            elif check["level"] == "info" and check["id"] not in ("include_off",):
                infos.append({"code": check["id"], "message": check["message"]})
    else:
        excluded = _builtin_row_checks(study, config)

    rows = []
    for row in study.subjects:
        sid = row["participant_id"]
        lesion = None if is_missing(row.get("lesion")) else row["lesion"]
        path = study.abspath(lesion) if lesion else None
        item = {"participant_id": sid, "group": row.get("group") or "n/a", "lesion": lesion, "path": path, "space": row.get("space"), "sha256": None, "status": "compute", "reason": None,
                "snapshot": None, "source_status": row.get("status")}
        reason = excluded.get(sid)
        if reason is None and row.get("qc") == "fail":
            reason = QC_FAIL
        if reason == NATIVE_EXCLUDED or (reason and reason.startswith("native space")):
            if config.native == "skip":
                reason = NATIVE_SKIPPED
            elif not providers["normalize"].available:
                reason = NATIVE_UNAVAILABLE
            else:
                item["status"], reason = "normalize", None
        if reason:
            item["status"], item["reason"] = "excluded", reason
        elif item["status"] == "compute" and hash_lesions and path:
            item["sha256"] = _lesion_sha(path)
            if item["sha256"] is None:
                item["status"], item["reason"] = "excluded", "lesion file unreadable"
        if item["status"] in ("compute", "normalize") and path:
            item["snapshot"] = os.path.join(out, *INPUTS_DIR, _snapshot_name(sid, path))
        rows.append(item)

    eligible = [row for row in rows if row["status"] == "compute"]
    n_future = len(eligible) + sum(1 for row in rows if row["status"] == "normalize")
    if config.null_relocations == "auto":
        null_k = 50 if n_future <= 10 else 10 if n_future <= 100 else 0
        if n_future > 100:
            infos.append({"code": "null_relocations", "message": f"Per-lesion relocation nulls are off for {n_future} lesions (more than 100); set --null-relocations K to compute them."})
    else:
        null_k = int(config.null_relocations)
    budget = int(float(config.memory_budget_gb) * 1024**3) if config.memory_budget_gb else None

    # Reuse: the map cache (cohortmaps.lookup), or the fallback's per-lesion fingerprints
    if eligible and hash_lesions:
        if providers["maps"].kind == "preferred" and not config.recompute:
            from . import cohortmaps

            found = cohortmaps.lookup([(row["participant_id"], row["path"], row["sha256"]) for row in eligible], bundle, subjects=config.preview_n, random_subset=bool(config.preview_n), seed=config.preview_seed,
                                      study_path=study.path, out_dir=os.path.join(out, STAGES_DIR, "cohortmaps"), null_relocations=null_k, null_seed=config.null_seed, budget_bytes=budget)
            hits = {item["participant_id"]: item["hit"] and (item["qa_hit"] or not null_k) for item in found["lesions"]}
            for row in eligible:
                if hits.get(row["participant_id"]):
                    row["status"] = "reuse"
            if budget is None:
                budget = int(found["budget_bytes"])
        elif providers["maps"].kind == "fallback" and not config.recompute:
            state = _read_json(os.path.join(out, PRIVATE_DIR, STAGES_DIR, "maps.json")) or {}
            lesions = state.get("lesions") or {}
            for row in eligible:
                entry = lesions.get(row["participant_id"])
                if entry and entry.get("fp") == _lesion_fingerprint(row["sha256"], manifest_sha, subjects_sha, null_k, config.null_seed, providers["maps"]) and all(os.path.isfile(abspath(p, out)) for p in entry.get("maps", {}).values()):
                    row["status"] = "reuse"
    if budget is None:
        budget = _default_budget_bytes()

    # Groups, specificity and scores
    groups = [row["group"] for row in rows if row["status"] in ("compute", "reuse", "normalize")]
    specificity, specificity_reason = _specificity_decision(config, groups)
    if specificity == "glm" and not providers["specificity"].available:
        specificity, specificity_reason = "unavailable", NOT_AVAILABLE
    if specificity == "block":
        blocks.append({"id": "contrast_group_size", "message": f"Specificity is on (--specificity glm) but the GLM {specificity_reason}.", "fix": "Add subjects, or run with --specificity off"})
    score_columns = []
    if config.scores == "auto":
        for column, confirmed in study.score_columns():
            if confirmed:
                score_columns.append(column)
            else:
                infos.append({"code": "unconfirmed_score", "message": f"The column {column} is not used as a score: {UNCONFIRMED_SCORE}."})
    elif isinstance(config.scores, list):
        for column in config.scores:
            if column not in study.columns:
                blocks.append({"id": "unknown_score", "message": f"The score column {column} is not in participants.tsv.", "fix": "Check --scores"})
            else:
                score_columns.append(column)

    # Disk space (addendum §3.13)
    n_compute = sum(1 for row in rows if row["status"] in ("compute", "normalize"))
    volume_bytes = 1
    for n in bundle.shape:
        volume_bytes *= int(n)
    required = (n_compute * 3 + 40) * volume_bytes * 4 * 0.6
    folder = out
    while folder and not os.path.exists(folder):
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        free = None
    if free is not None and free < 1.5 * required:
        blocks.append({"id": "disk_space", "message": f"Not enough disk space: about {required / 1024**3:.1f} GB needed, {free / 1024**3:.1f} GB free.", "fix": "Free disk space or choose another output folder (--out)"})

    # Warnings
    if config.preview_n:
        warnings.append({"code": "preview", "message": f"Preview run on {len(subject_ids)} of {bundle.n_subjects} connectome subjects: the maps are labelled PreviewN{config.preview_n} and are not the final maps."})
    try:
        out.encode("ascii")
    except UnicodeEncodeError:
        if not any(item.get("code") == "non_ascii_path" for item in infos):
            infos.append({"code": "non_ascii_out", "message": "The output folder has non-ASCII characters: official 3D Slicer 5.12.4 on Linux opens such paths only under a UTF-8 locale."})
    # Stages and estimates
    threads = jobs.budget_threads(config.threads)
    time_points = 0.0
    try:
        time_points = sum(sum(bundle.runs(sid)) for sid in subject_ids) / max(len(subject_ids), 1)
    except (KeyError, TypeError):
        time_points = 216.0
    subject_bytes = bundle.n_voxels * time_points * bundle.dtype.itemsize
    n_mapped = sum(1 for row in rows if row["status"] in ("compute", "reuse", "normalize"))
    n_new = sum(1 for row in rows if row["status"] in ("compute", "normalize"))
    stages = []
    estimates = {
        "prepare": 0.5,
        "preflight": IN_PROCESS_S,
        "normalize": NORMALIZE_SUBJECT_S * sum(1 for row in rows if row["status"] == "normalize"),
        "reference": len(subject_ids) * subject_bytes / READ_RATE + REFERENCE_PC_S,
        "maps": estimate_maps(n_new, len(subject_ids), bundle.n_voxels, time_points, subject_bytes, null_k, threads, budget),
        "overlap": IN_PROCESS_S + 0.002 * n_mapped * len(config.rules),
        "lesionset": IN_PROCESS_S + 0.02 * n_mapped,
        "specificity": estimate_glm(config.perm, bundle.n_voxels, config.tfce) if specificity == "glm" else 0.0,
        "slnm": estimate_glm(config.perm, bundle.n_voxels) * len(score_columns),
        "conjunction": 0.2,
        "qa": IN_PROCESS_S,
        "clusters": IN_PROCESS_S,
        "export": IN_PROCESS_S + 0.05 * n_mapped,
        "report": REPORT_S + REPORT_LESION_S * n_mapped,
        "finish": 0.2,
    }
    nulls_mode, nulls_reason = _nulls_decision(config, providers, bundle, subject_ids, n_mapped, threads, budget)
    estimates["nulls"] = nulls_mode[1] if isinstance(nulls_mode, tuple) else 0.0
    nulls_mode = nulls_mode[0] if isinstance(nulls_mode, tuple) else nulls_mode
    for name, label in STAGES:
        provider = providers.get(name) or Provider(name, None, "builtin")
        status, reason = "planned", None
        if not provider.available:
            status, reason = "unavailable", provider.reason
        elif name == "normalize" and not any(row["status"] == "normalize" for row in rows):
            status, reason = "skip", "no native-space lesion to normalize"
        elif name == "reference" and (config.reference == "off" or not config.build_references):
            status, reason = "skip", "reference maps are used when present; built only with --build-references" if config.reference != "off" else "--reference off"
        elif name == "maps" and n_new == 0:
            status, reason = "reuse", "every map is in the cache"
        elif name == "nulls" and nulls_mode in ("skip", "unavailable"):
            status, reason = nulls_mode, nulls_reason
        elif name == "specificity" and specificity != "glm":
            status, reason = ("unavailable" if specificity == "unavailable" else "skip"), specificity_reason
        elif name == "slnm" and not score_columns:
            status, reason = "skip", "no confirmed score column"
        elif name == "lesionset" and n_mapped > 100:
            status, reason = "planned", None
        elif name == "report" and not config.report:
            status, reason = "skip", "--no-report"
        estimate = estimates.get(name, 0.0) if status in ("planned",) else 0.0
        stages.append({"name": name, "label": label, "provider": provider.module, "provider_kind": provider.kind, "mode": _stage_mode(name), "status": status, "reason": reason, "estimate_s": round(estimate, 1)})
    total = sum(stage["estimate_s"] for stage in stages)
    result = Plan(config, out, study, bundle, providers, subject_ids, subjects_sha, manifest_sha, rows, stages, blocks, warnings, infos, overrides, total, null_k, specificity, specificity_reason,
                  score_columns, report, budget)
    result.nulls_mode = nulls_mode  # "exact" | "fast" | "skip" | "unavailable"
    result.elapsed = time.perf_counter() - started
    return result


def _stage_mode(name):
    return "child" if name in ("normalize", "reference", "maps", "nulls", "specificity", "slnm") else "in-process"


def _nulls_decision(config, providers, bundle, subject_ids, n_lesions, threads, budget):
    """((mode, estimate_s) or mode, reason): exact | fast | skip | unavailable (decision D-A23)."""
    if config.nulls == "off":
        return "skip", "null models are off (--nulls off)"
    if not providers["nulls"].available:
        return "unavailable", NOT_AVAILABLE
    if n_lesions < 2:
        return "skip", "null models need at least 2 lesions"
    from . import nulls, thresholds

    top_pct = thresholds.normalize_rule(config.rule)["kind"] == "top_pct"
    if config.nulls in ("auto", "fast"):
        index = nulls.find_fast_index(bundle, subject_ids) if top_pct else None
        if index is not None:
            return ("fast", nulls.estimate(n_lesions, bundle, mode="fast", subjects=list(subject_ids), threads=threads, rule=config.rule)), None
        if config.nulls == "fast":
            return "skip", "fast null models need the fast index of this connectome and a top-x% rule"
    seconds = nulls.estimate(n_lesions, bundle, mode="exact", subjects=list(subject_ids), threads=threads, budget_bytes=budget, rule=config.rule)
    if config.nulls == "auto" and seconds > config.nulls_budget_min * 60:
        return "skip", NULLS_TOO_LONG.format(_duration_text(seconds))
    return ("exact", seconds), None


def _lesion_fingerprint(sha256, manifest_sha, subjects_sha, null_k, null_seed, provider):
    return fingerprint({"lesion": sha256, "manifest": manifest_sha, "subjects": subjects_sha, "null_k": null_k, "null_seed": null_seed, "provider": provider.sha256})


# Process primitives


@dataclasses.dataclass
class ChildResult:
    rc: int
    status: str  # "done", "blocked", "error"
    terminal: dict | None
    events: list
    run_id: str | None
    message: str | None = None


def _read_log_tail(path, lines=20):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return [line.rstrip("\n") for line in collections.deque(f, maxlen=lines)]
    except OSError:
        return []


# Running


class Runner:
    """One pipeline run (:func:`run`)."""

    def __init__(self, config, emit=None, cancel=None, argv=None):
        self.config = config
        self.emit = emit if emit is not None else Emitter(config.events)
        self.argv = list(argv or [])
        self.cancel_extra = cancel
        self.providers = resolve_providers()
        self.results = {}
        self.fps = {}
        self.stage_records = {}
        self.warnings = []
        self.overrides = []
        self.failed = []
        self.current = "prepare"
        self.kept = []
        self.children = []
        self.run_id = None
        self.started = time.monotonic()
        self.created = _now_iso()
        self.current_outputs = []
        self.lock = None
        self.keep_awake = None
        self.plan = None

    # Paths

    def path(self, *parts):
        return os.path.join(self.out, *parts)

    def private(self, *parts):
        return os.path.join(self.out, PRIVATE_DIR, *parts)

    def stage_dir(self, name):
        folder = self.path(STAGES_DIR, name)
        os.makedirs(folder, exist_ok=True)
        return folder

    def rel(self, path):
        return relpath(path, self.out)

    def absp(self, path):
        return abspath(path, self.out)

    # Events

    def _portable(self, text):
        """``text`` with the local folders of the run replaced by labels (for files that are shared)."""
        replacements = [(self.out, "."), (self.config.study, "<study>"), (self.config.bundle, "<bundle>"), (LIB_PARENT, "<library>"), (os.path.dirname(_python_executable()), "<python>"),
                        (os.path.expanduser("~"), "~")]
        for path, label in sorted(((os.path.abspath(a), b) for a, b in replacements if a), key=lambda item: -len(item[0])):
            text = str(text).replace(path, label)
        return text

    def warn(self, message, code="warning", **fields):
        self.warnings.append({"code": code, "message": message, **{key: value for key, value in fields.items() if key in ("participant_id", "stage")}})
        self.emit("warning", stage=self.current, code=code, message=message, **fields)

    def info(self, message, code="info", **fields):
        self.emit("info", stage=self.current, code=code, message=message, **fields)

    def stage_event(self, name, status, reason=None, **fields):
        index = [stage for stage, _ in STAGES].index(name) + 1
        self.emit("stage", name=name, stage=name, label=STAGE_LABELS[name], index=index, total=len(STAGES), status=status, reason=reason, provider=self.providers[name].module, **fields)

    # Main

    def run(self):
        """Run every stage; return the exit code."""
        config = self.config
        jobs.reset_cancel()
        from .connectome import ConnectomeBundle

        if not config.study or not config.bundle:
            raise PipelineError("--study and --bundle are required", code="usage", exit_code=EXIT_USAGE)
        try:
            bundle = ConnectomeBundle.open(config.bundle)
        except (OSError, ValueError) as exc:
            raise PipelineError(f"The connectome cannot be opened: {exc}", code="bundle") from None
        if not config.accept_terms:
            self.emit("error", code="terms_not_accepted", message=f"The data use terms of the connectome {bundle.name} must be accepted (--accept-terms).", data_use_terms=bundle.data_use_terms, connectome=bundle.name)
            return EXIT_TERMS
        self.out = default_out(config, bundle.name)
        os.makedirs(self.private(STAGES_DIR), exist_ok=True)
        self.lock = jobs.OutputLock(self.private(LOCK_NAME))
        try:
            self.lock.acquire()
        except jobs.Locked as exc:
            self.emit("error", code="locked", message=f"Another pipeline run writes to this folder: {exc.message}", holder=exc.holder)
            return EXIT_LOCKED
        try:
            return self._run_locked(bundle)
        finally:
            if self.keep_awake is not None:
                self.keep_awake.stop()
            for path in (self.private(STOP_NAME),):
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            self.lock.release()

    def _run_locked(self, bundle):
        config = self.config
        if self.lock.reclaimed is not None:
            self.warn(f"A stale lock of process {self.lock.reclaimed.get('pid')} was reclaimed", code="stale_lock")
        stop = self.private(STOP_NAME)
        if os.path.exists(stop):
            os.remove(stop)
        self.cancel = Cancel(stop, self.cancel_extra)
        if config.parent_pid:
            jobs.start_watchdogs(self.cancel.token, parent_pid=config.parent_pid, orphan=False)
        if isinstance(self.emit, Emitter):
            replacements = [(self.out, "."), (config.study, "<study>"), (config.bundle, "<bundle>"), (LIB_PARENT, "<library>"), (os.path.expanduser("~"), "~")]
            self.emit.attach(self.private(LOG_NAME), self.private(PROGRESS_JSON), replacements)
        try:
            return self._stages(bundle)
        except jobs.Cancelled as exc:
            for path in self.current_outputs:
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            self.stage_records.setdefault(self.current, {}).update({"status": "cancelled"})
            self._write_pipeline_json("cancelled")
            self._update_run("cancelled")
            self.emit("cancelled", stage=self.current, kept=self.kept, done=exc.done, reason=exc.reason or self.cancel.reason)
            return EXIT_CANCELLED
        except PipelineError as exc:
            if self.run_id:
                self._write_pipeline_json("error", error={"code": exc.code, "message": exc.message, "stage": self.current})
                self._update_run("failed")
            self.emit("error", code=exc.code, message=exc.message, stage=self.current, **exc.fields)
            return exc.exit_code
        except Exception as exc:
            import traceback

            tail = [line for line in traceback.format_exc().splitlines() if line.strip()][-5:]
            if self.run_id:
                self._write_pipeline_json("error", error={"code": "exception", "message": f"{type(exc).__name__}: {exc}", "stage": self.current})
                self._update_run("failed")
            self.emit("error", code="exception", message=f"{type(exc).__name__}: {exc}", stage=self.current, traceback=tail)
            return EXIT_ERROR

    def _stages(self, bundle):
        config = self.config
        self.current = "prepare"
        self.plan = plan_result = plan(config, providers=self.providers)
        self.bundle, self.study = plan_result.bundle, plan_result.study
        self.rows = plan_result.rows
        self.overrides = [{"what": what, "reason": "requested on the command line"} for what in plan_result.overrides]
        if config.volume_off_reason and "volume_ml" not in config.covariates:
            self.overrides.append({"what": "no_volume_covariate", "reason": config.volume_off_reason})
        if config.allow_rule_anyway:
            self.overrides.append({"what": "allow_rule_anyway", "reason": "requested on the command line"})
        for item in plan_result.warnings:
            self.warn(item["message"], code=item.get("code", "plan"))
        for item in plan_result.infos:
            self.info(item["message"], code=item.get("code", "plan"))
        if plan_result.blocks:
            raise PipelineError("The run is blocked: " + " | ".join(block["message"] for block in plan_result.blocks), code="blocked", exit_code=EXIT_BLOCKED,
                                fixes=[block.get("fix") or block["message"] for block in plan_result.blocks], blocks=plan_result.blocks)
        if plan_result.exit_code == EXIT_NO_LESION:
            raise PipelineError("No lesion can be analysed: every subject is excluded.", code="no_eligible", exit_code=EXIT_NO_LESION, excluded={row["participant_id"]: row["reason"] for row in self.rows})
        for row in self.rows:
            if row["status"] == "excluded":
                self.warn(f"excluded: {row['reason']}", code="excluded", participant_id=row["participant_id"])
        self.subject_ids = plan_result.subject_ids
        self.null_k = plan_result.null_k
        self.threads = jobs.budget_threads(config.threads)
        self.label = self.bundle.name
        jobs.write_json_atomic(self.private(PLAN_JSON), plan_result.to_json())
        if isinstance(self.emit, Emitter):
            self.emit.weights = {stage["name"]: max(stage["estimate_s"], 0.1) for stage in plan_result.stages}

        # Snapshots of the analysed lesions
        self.cancel.raise_if_cancelled()
        self._snapshot([row for row in self.rows if row["status"] in ("compute", "reuse")])
        mask_path = self.private(MASK_NAME)
        source_mask = os.path.join(self.bundle.path, "mask.nii.gz")
        if not os.path.isfile(mask_path) or file_sha256(mask_path) != file_sha256(source_mask):
            _copy_atomic(source_mask, mask_path)
        self.mask_path = mask_path

        # Run registration, priority, keep-awake
        from . import runs

        runs.register_kind("pipeline")
        self.run_id = runs.new_run_id("pipeline")
        runs.register(self.study.path, "pipeline", self.out, parents=config.parent_run, params=config.to_json(base=self.out, analysis_only=True), seeds={"preview": config.preview_seed, "permutation": config.perm_seed, "null": config.null_seed},
                      run_id=self.run_id)
        if isinstance(self.emit, Emitter):
            self.emit.progress["run_id"] = self.run_id
        jobs.set_priority(config.nice)
        if config.keep_awake == "on" or (config.keep_awake == "auto" and plan_result.estimate_s > KEEP_AWAKE_AFTER_S):
            self.keep_awake = jobs.KeepAwake().start()
        self.emit(
            "start",
            job="pipeline",
            run_id=self.run_id,
            argv=self.argv,
            threads=self.threads,
            study=self.study.path,
            bundle=self.bundle.path,
            out=self.out,
            preview_n=config.preview_n,
            n_rows=len(self.rows),
            n_compute=plan_result.count("compute"),
            n_reuse=plan_result.count("reuse"),
            n_normalize=plan_result.count("normalize"),
            n_excluded=plan_result.count("excluded"),
            stages=[{key: stage[key] for key in ("name", "label", "status", "estimate_s")} for stage in plan_result.stages],
            estimate_s=round(plan_result.estimate_s, 1),
        )
        self.stage_records["prepare"] = {"status": "done", "start": self.created, "end": _now_iso(), "elapsed": round(time.monotonic() - self.started, 3)}
        self.stage_event("prepare", "done")
        self.kept.append("prepare")
        self._write_pipeline_json("running")

        for name, _label in STAGES[1:-1]:
            self.cancel.raise_if_cancelled()
            self.current = name
            if name == "maps" and not self.mapped_rows:
                raise PipelineError("No lesion can be analysed: every subject is excluded.", code="no_eligible", exit_code=EXIT_NO_LESION,
                                    excluded={row["participant_id"]: row["reason"] for row in self.rows})
            self._stage(name)
            if name == "maps" and not self.mapped_rows:
                raise PipelineError("No lesion can be analysed: every subject is excluded.", code="no_eligible", exit_code=EXIT_NO_LESION,
                                    excluded={row["participant_id"]: row["reason"] for row in self.rows})
        self.current = "finish"
        return self._finish()

    # Stage machinery

    def _state_path(self, name):
        return self.private(STAGES_DIR, f"{name}.json")

    def _stage(self, name):
        provider = self.providers[name]
        planned = next(stage for stage in self.plan.stages if stage["name"] == name)
        method = getattr(self, f"_stage_{name}")
        started, start_iso = time.monotonic(), _now_iso()
        record = {"name": name, "label": STAGE_LABELS[name], "provider": provider.module, "provider_kind": provider.kind, "provider_sha256": provider.sha256, "mode": _stage_mode(name), "start": start_iso}
        if not provider.available:
            record.update({"status": "unavailable", "reason": provider.reason, "reused": False})
            self.stage_records[name] = record
            self.fps[name] = f"unavailable:{name}"
            self.stage_event(name, "unavailable", provider.reason)
            if name == "report":
                self.info("report not available in this build", code="report_unavailable")
            return
        self.current_outputs = []
        self.stage_event(name, "running")  # the current stage of progress.json (a GUI re-attaches to it)
        outcome = method(planned)
        status = outcome.get("status", "done")
        record.update({
            "status": status,
            "reason": outcome.get("reason"),
            "reused": bool(outcome.get("reused")),
            "fingerprint": outcome.get("fingerprint"),
            "end": _now_iso(),
            "elapsed": round(time.monotonic() - started, 3),
            "child_run_id": outcome.get("child_run_id"),
            "outputs": outcome.get("outputs_rel") or {},
        })
        self.stage_records[name] = record
        self.results[name] = outcome.get("result") or {}
        if status in ("done", "reused"):
            self.kept.append(name)
        if status in ("failed", "blocked", "error"):
            self.failed.append(name)
            if name in CRITICAL_STAGES:
                raise PipelineError(f"{STAGE_LABELS[name]} failed: {outcome.get('reason')}", code="stage_failed", exit_code=EXIT_ERROR, failed_stage=name)
        self.stage_event(name, "reused" if outcome.get("reused") else status, outcome.get("reason"))
        self._write_pipeline_json("running")

    def _reusable(self, name, fp):
        if self.config.recompute:
            return None
        state = _read_json(self._state_path(name))
        if not state or state.get("fingerprint") != fp or state.get("status") != "done":
            return None
        for path, size in (state.get("outputs") or {}).items():
            full = self.absp(path)
            if not os.path.isfile(full) or os.path.getsize(full) != size:
                return None
        return state

    def _cached(self, name, params, deps=()):
        """(fingerprint, reusable outcome or None) of a stage."""
        provider = self.providers[name]
        fp = fingerprint({"stage": name, "provider": provider.module, "provider_sha256": provider.sha256, "parameters": params, "inputs": {dep: self.fps.get(dep) for dep in deps}})
        self.fps[name] = fp
        state = self._reusable(name, fp)
        if state is None:
            try:
                os.remove(self._state_path(name))
            except OSError:
                pass
            return fp, None
        return fp, {"status": "done", "reused": True, "fingerprint": fp, "result": state.get("result") or {}, "outputs_rel": state.get("outputs") or {}, "child_run_id": state.get("child_run_id")}

    def _save(self, name, fp, result, outputs, child_run_id=None, events=None, start=None):
        sizes = {}
        for path in outputs:
            if path and os.path.isfile(path):
                sizes[self.rel(path)] = os.path.getsize(path)
        provider = self.providers[name]
        state = {"fingerprint": fp, "provider": provider.module, "provider_sha256": provider.sha256, "status": "done", "outputs": sizes, "start": start, "end": _now_iso(), "result": result,
                 "child_run_id": child_run_id, "events_tail": [self._portable(jobs.to_json(event)) for event in (events or [])[-10:]]}
        jobs.write_json_atomic(self._state_path(name), state)
        return {"status": "done", "fingerprint": fp, "result": result, "outputs_rel": sizes, "child_run_id": child_run_id}

    def _skip(self, name, reason, status="skipped"):
        self.fps[name] = f"{status}:{reason}"
        return {"status": status, "reason": reason, "result": {"reason": reason}}

    # Child jobs

    def _child(self, stage, module, args, folder, on_event=None):
        """Run a library job as a child process; relay its events; return a :class:`ChildResult` (raises Cancelled when stopped)."""
        python = _python_executable()
        if module.endswith(".py"):
            command = [python, module, *args]
        else:
            command = [python, "-m", module if "." in module else f"{PACKAGE}.{module}", *args]
            origin, _text = _module_source(module)
            if origin and _requires_slicer(origin):
                slicer_exe = _slicer_executable(self.config.slicer)
                command = [slicer_exe, "--no-splash", "--no-main-window", "--python-script", origin, "--", *args]
        env = dict(os.environ)
        env["PYTHONPATH"] = LIB_PARENT + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["PYTHONIOENCODING"] = "utf-8"
        if sys.platform.startswith("linux") and not any("UTF-8" in env.get(key, "").upper().replace("UTF8", "UTF-8") for key in ("LC_ALL", "LC_CTYPE", "LANG")):
            env["LC_CTYPE"] = "C.UTF-8"  # the CTK launcher (PythonSlicer) drops non-ASCII arguments under the POSIX locale
        os.makedirs(folder, exist_ok=True)
        log_path = os.path.join(folder, "job.log")
        events = collections.deque(maxlen=50)
        terminal, run_id = None, None
        with open(log_path, "a", encoding="utf-8") as log:
            shown = [os.path.basename(command[0]), *(self._portable(token) for token in command[1:])]
            log.write(f"# {_now_iso()} {stage}: {shlex.join(shown)}\n")
            log.flush()
            proc = subprocess.Popen(command, cwd=LIB_PARENT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log, text=True, encoding="utf-8", errors="replace")
            self.children.append(proc)
            lines = queue.Queue()

            def reader():
                try:
                    for line in proc.stdout:
                        lines.put(line)
                finally:
                    lines.put(None)

            thread = threading.Thread(target=reader, name=f"lnm-pipeline-{stage}", daemon=True)
            thread.start()
            stop_at, term_at, killed = None, None, False
            while True:
                try:
                    line = lines.get(timeout=POLL_S)
                except queue.Empty:
                    line = ""
                if line is None:
                    break
                if line.strip().startswith("{"):
                    try:
                        event = json.loads(line)
                    except ValueError:
                        event = None
                    if isinstance(event, dict):
                        events.append(event)
                        kind = event.get("event")
                        if kind == "start":
                            run_id = event.get("run_id") or run_id
                        elif kind == "progress":
                            fields = {key: value for key, value in event.items() if key not in ("event", "t", "stage")}
                            fields.setdefault("unit", {"maps": "subject passes", "specificity": "permutations", "slnm": "permutations", "normalize": "subjects"}.get(stage, "units"))
                            self.emit("progress", stage=stage, child_stage=event.get("stage"), **fields)
                        elif kind == "warning":
                            fields = {key: value for key, value in event.items() if key not in ("event", "t", "message", "code", "stage")}
                            self.warn(str(event.get("message")), code=event.get("code") or "child", **fields)
                        elif kind in ("done", "error", "cancelled"):
                            terminal = event
                            run_id = event.get("run_id") or run_id
                        if on_event is not None:
                            on_event(event)
                now = time.monotonic()
                if stop_at is None and self.cancel.cancelled():
                    stop_at = now
                if stop_at is not None and term_at is None and now - stop_at > CHILD_STOP_WAIT_S and proc.poll() is None:
                    proc.terminate()
                    term_at = now
                if term_at is not None and not killed and now - term_at > CHILD_STOP_WAIT_S and proc.poll() is None:
                    proc.kill()
                    killed = True
            rc = proc.wait()
            thread.join(timeout=2)
        if stop_at is not None or rc in (EXIT_CANCELLED, 130) or (terminal or {}).get("event") == "cancelled":
            if stop_at is None:
                self.cancel.cancel("child job cancelled")
            raise jobs.Cancelled(done=None, reason=self.cancel.reason or "cancelled")
        if rc == 0 and (terminal or {}).get("event") == "done":
            return ChildResult(rc, "done", terminal, list(events), run_id)
        code = (terminal or {}).get("code")
        message = (terminal or {}).get("message") or f"exit code {rc}"
        tail = _read_log_tail(log_path)
        if tail and code not in ("blocked", "design", "design_blocked"):
            message += " | " + " / ".join(self._portable(line) for line in tail[-20:] if not line.startswith("#"))[-2000:]
        status = "blocked" if code in ("blocked", "design", "design_blocked") else "error"
        return ChildResult(rc, status, terminal, list(events), run_id, message)

    def _common_child_args(self, cancel=True, resume=True, threads=True):
        args = []
        if threads:
            args += ["--threads", str(self.threads)]
        if cancel:
            args += ["--cancel-file", self.private(STOP_NAME)]
        if resume and not self.config.recompute:
            args += ["--resume"]
        args += ["--run-root", self.study.path, "--parent-run", self.run_id]
        return args

    # Snapshots

    def _snapshot(self, rows):
        """Byte copies of the analysed lesions in inputs/lesions/ (verified against the planned sha256)."""
        folder = self.path(*INPUTS_DIR)
        os.makedirs(folder, exist_ok=True)
        for row in rows:
            self.cancel.raise_if_cancelled()
            target = row["snapshot"] or os.path.join(folder, _snapshot_name(row["participant_id"], row["path"]))
            row["snapshot"] = target
            try:
                with open(row["path"], "rb") as f:
                    data = f.read()
            except OSError:
                self._exclude(row, "lesion file unreadable")
                continue
            sha = hashlib.sha256(data).hexdigest()
            if row.get("sha256") and sha != row["sha256"]:
                # Changed after planning: plan the row once more (a second read must agree), else exclude it
                try:
                    with open(row["path"], "rb") as f:
                        again = f.read()
                except OSError:
                    again = None
                if again is None or hashlib.sha256(again).hexdigest() != sha:
                    self._exclude(row, CHANGED_SNAPSHOT)
                    continue
                row["status"] = "compute"
            row["sha256"] = sha
            if os.path.isfile(target) and os.path.getsize(target) == len(data) and file_sha256(target) == sha:
                continue
            tmp = f"{target}.{os.getpid()}.tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, target)

    def _exclude(self, row, reason):
        row["status"], row["reason"] = "excluded", reason
        self.warn(f"excluded: {reason}", code="excluded", participant_id=row["participant_id"])

    @property
    def mapped_rows(self):
        return [row for row in self.rows if row["status"] in ("compute", "reuse")]

    # Stages

    def _stage_preflight(self, planned):
        report = self.plan.preflight
        if self.providers["preflight"].kind != "preferred" or report is None:
            fp, reused = self._cached("preflight", {"rows": [(row["participant_id"], row.get("sha256"), row["reason"]) for row in self.rows]})
            return reused or self._save("preflight", fp, {"builtin": True, "excluded": {row["participant_id"]: row["reason"] for row in self.rows if row["status"] == "excluded"}}, [])
        checks = [{key: check[key] for key in ("id", "level", "scope", "subjects", "message", "excludes")} for check in report["checks"]]
        fp, reused = self._cached("preflight", {"checks": checks, "rows": [(row["participant_id"], row.get("sha256"), row["reason"]) for row in self.rows]})
        if reused:
            return reused
        from . import preflight

        start = _now_iso()
        path = preflight.save_report(self.study, report)
        for check in report["checks"]:
            if check["level"] == "warn" and check["scope"] == "subject" and not check["excludes"]:
                for sid in check["subjects"][:50]:
                    self.emit("warning", stage="preflight", participant_id=sid, code=check["id"], message=check["message"])
        result = {"report": relpath(path, self.study.path), "run_id": report.get("run_id"), "counts": report["counts"], "blocked": report["blocked"]}
        return self._save("preflight", fp, result, [], child_run_id=report.get("run_id"), start=start)

    def _stage_normalize(self, planned):
        rows = [row for row in self.rows if row["status"] == "normalize"]
        if not rows:
            return self._skip("normalize", "no native-space lesion to normalize")
        provider = self.providers["normalize"]
        params = {"subjects": [(row["participant_id"], _lesion_sha(row["path"]) if row["path"] else None) for row in rows], "native": self.config.native, "template": self.config.template or "auto"}
        fp, reused = self._cached("normalize", params)
        if reused is None:
            start = _now_iso()
            folder = self.stage_dir("normalize")
            results = self._normalize_child(provider, [row["participant_id"] for row in rows], folder)
            locked = [sid for sid, entry in results.items() if entry.get("status") == "locked"]
            if locked:
                self.info(f"Waiting for normalizations running elsewhere: {', '.join(locked)}", code="normalize_locked")
                deadline = time.monotonic() + LOCKED_WAIT_S
                while time.monotonic() < deadline and any(os.path.exists(os.path.join(self.study.path, "derivatives", "normalization", sid, ".normalize.lock")) for sid in locked):
                    self.cancel.raise_if_cancelled()
                    time.sleep(0.5)
                results.update(self._normalize_child(provider, locked, folder))
            outcome = self._save("normalize", fp, {"subjects": results}, [])
        else:
            outcome = reused
        results = outcome["result"].get("subjects") or {}
        normalized = []
        for row in rows:
            entry = results.get(row["participant_id"]) or {"status": "failed", "reason": "no result"}
            status = entry.get("status")
            values = entry.get("values") or {}
            if status == "locked":
                self._exclude(row, "normalization running elsewhere")
            elif status in ("done", "reused", "normalized") and values.get("lesion"):
                if self.config.native == "include-unreviewed":
                    row["path"] = self.study.abspath(values["lesion"])
                    row["lesion"] = values["lesion"]
                    row["status"] = "compute"
                    row["snapshot"] = self.path(*INPUTS_DIR, _snapshot_name(row["participant_id"], row["path"]))
                    row["sha256"] = None
                    normalized.append(row)
                    self.overrides.append({"what": "norm_qc", "participant_id": row["participant_id"], "reason": "--native include-unreviewed: the normalized lesion is mapped before review"})
                else:
                    self._exclude(row, NORM_NEEDS_REVIEW)
            else:
                self._exclude(row, f"normalization {status or 'failed'}: {entry.get('reason') or 'no lesion'}")
        if normalized:
            self._snapshot(normalized)
        return outcome

    def _normalize_child(self, provider, sids, folder):
        args = ["batch", "--study", self.study.path, "--subjects", ",".join(sids), "--template", self.config.template or "auto", *self._common_child_args(resume=False)]
        results = {}

        def on_event(event):
            if event.get("event") == "result" and event.get("kind") == "subject" and event.get("subject"):
                results[event["subject"]] = {key: event.get(key) for key in ("status", "reason", "values", "suggestion")}
                self.emit("progress", stage="normalize", participant_id=event["subject"], message=f"{event['subject']}: {event.get('status')}", done=len(results), total=len(sids), unit="subjects")

        child = self._child("normalize", provider.module, args, folder, on_event)
        if child.status != "done" and not results:
            for sid in sids:
                results[sid] = {"status": "failed", "reason": child.message}
        return results

    def _stage_reference(self, planned):
        from . import qa

        provider = self.providers["reference"]
        if self.config.reference == "off":
            return self._skip("reference", "--reference off")
        from .export import subjects_sha256

        if provider.kind == "preferred":
            found = qa.load_reference(self.bundle, subjects_sha256(self.subject_ids))
            if found is not None:
                self.fps["reference"] = f"reference:{getattr(found, 'sha256', None) or found.metadata.get('created')}"
                return {"status": "done", "reason": "reference maps found", "result": {"found": True}}
        elif self.bundle.degree is not None:
            self.fps["reference"] = "degree:present"
            return {"status": "done", "reason": "degree map found", "result": {"found": True, "degree": True}}
        estimate = len(self.subject_ids) * self.bundle.n_voxels * 216 * self.bundle.dtype.itemsize / READ_RATE + REFERENCE_PC_S
        if not self.config.build_references:
            return self._skip("reference", REFERENCE_MISSING.format(_duration_text(estimate)))
        if not os.access(self.bundle.path, os.W_OK):
            self.warn(REFERENCE_READ_ONLY, code="reference_read_only")
            return self._skip("reference", REFERENCE_READ_ONLY)
        fp, _reused = self._cached("reference", {"manifest": self.plan.manifest_sha256, "subjects": self.plan.subjects_sha256})
        folder = self.stage_dir("reference")
        if provider.kind == "preferred":
            args = ["build", "--bundle", self.bundle.path]
            if self.config.preview_n:
                args += ["--subject-list", ",".join(self.subject_ids)]
            args += self._common_child_args()
            child = self._child("reference", provider.module, args, folder)
        else:
            args = ["degree", "--bundle", self.bundle.path, "--threads", str(self.threads)]
            child = self._child("reference", provider.module, args, folder)
        if child.status != "done":
            return {"status": "failed", "reason": child.message, "fingerprint": fp}
        return self._save("reference", fp, {"built": True}, [], child_run_id=child.run_id, events=child.events)

    # Maps

    def _write_lesions_tsv(self, rows):
        path = self.private(LESIONS_TSV)
        lines = ["participant_id\tlesion\tlesion_sha256"]
        for row in rows:
            lines.append(f"{row['participant_id']}\t{relpath(row['snapshot'], os.path.dirname(path))}\t{row['sha256']}")
        _write_text_atomic(path, "\n".join(lines) + "\n")
        return path

    def _stage_maps(self, planned):
        rows = self.mapped_rows
        provider = self.providers["maps"]
        params = {"lesions": sorted((row["participant_id"], row["sha256"]) for row in rows), "manifest": self.plan.manifest_sha256, "subjects": self.plan.subjects_sha256, "null_k": self.null_k,
                  "null_seed": self.config.null_seed, "preview": self.config.preview_n, "recompute": bool(self.config.recompute)}
        fp, reused = self._cached("maps", params, ("normalize",))
        if reused is not None:
            self._apply_maps(reused["result"])
            return reused
        start = _now_iso()
        if provider.kind == "preferred":
            outcome = self._maps_cohortmaps(rows, fp, start)
        else:
            outcome = self._maps_engine(rows, fp, start)
        if outcome.get("status") == "done":
            self._apply_maps(outcome["result"])
        return outcome

    def _apply_maps(self, result):
        """Set each row's maps from the stage result; rows without maps are excluded."""
        maps = result.get("maps") or {}
        for row in self.mapped_rows:
            entry = maps.get(row["participant_id"])
            if entry is None:
                self._exclude(row, (result.get("excluded") or {}).get(row["participant_id"]) or "not mapped")
                continue
            if entry.get("lesion_sha256") and entry["lesion_sha256"] != row["sha256"]:
                self._exclude(row, CHANGED_RUN)
                continue
            row["maps"] = {key: self.absp(value) for key, value in entry["files"].items()}
            row["lesion_meta"] = entry.get("meta") or {}
            row["qa"] = entry.get("qa")
        self.maps_tsv = self.absp(result["maps_tsv"])

    def _maps_cohortmaps(self, rows, fp, start):
        from . import cohortmaps, preflight

        folder = self.stage_dir("cohortmaps")
        lesions_tsv = self._write_lesions_tsv(rows)
        cache = os.path.join(folder, "cache") if self.config.recompute else os.path.join(self.study.path, *cohortmaps.CACHE_RELPATH)
        args = ["--lesions-tsv", lesions_tsv, "--bundle", self.bundle.path, "--out", folder, "--cache", cache, "--null-relocations", str(self.null_k), "--null-seed", str(self.config.null_seed)]
        if self.config.preview_n:
            args += ["--subjects", str(self.config.preview_n), "--random", "--seed", str(self.config.preview_seed)]
        if self.config.memory_budget_gb:
            args += ["--budget-gb", f"{float(self.config.memory_budget_gb):g}"]
        args += self._common_child_args()
        child = self._child("maps", "cohortmaps", args, folder)
        if child.status != "done":
            return {"status": "failed", "reason": child.message, "fingerprint": fp}
        maps_tsv = os.path.join(folder, cohortmaps.MAPS_TSV)
        record = cohortmaps.read_record(os.path.join(folder, cohortmaps.COHORTMAPS_JSON)) or {}
        mapped = cohortmaps.read_maps_tsv(maps_tsv)
        excluded = {sid: reason for reason, sids in (record.get("excluded") or {}).items() for sid in sids}
        # A lesion without any voxel in the connectome mask has all-zero maps: exclude that subject only (like an
        # empty lesion), and give the later stages a maps.tsv of the kept lesions (next to cohortmaps.json)
        outside = {item["participant_id"] for item in mapped if _number(item.get("voxels_in_mask")) == 0}
        if outside:
            mapped = [item for item in mapped if item["participant_id"] not in outside]
            excluded.update(dict.fromkeys(outside, OUTSIDE_MASK))
            maps_tsv = self._filter_maps_tsv(maps_tsv, os.path.join(folder, PIPELINE_MAPS_TSV), outside)
        if mapped:
            failures = preflight.assert_run_outputs(maps_tsv, expected_n=len(self.subject_ids), kind="cohortmaps", reference_header=self.bundle.path, expected_lesions=len(mapped))
            if failures:
                return {"status": "failed", "reason": "; ".join(f"{item['code']}: {item['message']}" for item in failures[:5]), "fingerprint": fp}
        qa_values = record.get("qa") or {}
        result_maps, outputs = {}, [maps_tsv, os.path.join(folder, cohortmaps.COHORTMAPS_JSON)]
        for item in mapped:
            sid = item["participant_id"]
            files = {key: item[key] for key in cohortmaps.MAP_KEYS}
            outputs += list(files.values())
            volume = _number(item.get("volume_ml"))
            meta = {"voxels_in_mask": _number(item.get("voxels_in_mask")), "weight_total": _number(item.get("fractional_sum")), "volume_mm3": None if volume is None else volume * 1000.0,
                    "weight_outside_fraction": _number(item.get("weight_outside_fraction")), "volume_ml": volume}
            result_maps[sid] = {"files": {key: self.rel(value) for key, value in files.items()}, "lesion_sha256": item.get("lesion_sha256"), "meta": meta, "qa": qa_values.get(sid), "cache": item.get("cache")}
        lesions = record.get("lesions") or {}
        result = {"maps_tsv": self.rel(maps_tsv), "maps": result_maps, "excluded": excluded, "computed": lesions.get("computed"), "cache_hits": lesions.get("cache_hits"), "provider": "cohortmaps",
                  "run_id": record.get("run_id")}
        return self._save("maps", fp, result, outputs, child_run_id=child.run_id, events=child.events, start=start)

    @staticmethod
    def _filter_maps_tsv(source, target, drop):
        """Copy the maps.tsv ``source`` to ``target`` (same folder: its relative paths stay valid) without the rows of ``drop``."""
        with open(source, encoding="utf-8") as f:
            lines = f.read().splitlines()
        column = lines[0].split("\t").index("participant_id") if lines else 0
        kept = lines[:1] + [line for line in lines[1:] if line and line.split("\t")[column] not in drop]
        _write_text_atomic(target, "\n".join(kept) + "\n")
        return target

    def _maps_engine(self, rows, fp, start):
        """Fallback: the wave-1 engine CLI per batch (stages/maps/batch-<sha12>), per-lesion fingerprints in the stage state."""
        from . import engine, thresholds

        provider = self.providers["maps"]
        folder = self.stage_dir("maps")
        state_path = self._state_path("maps")
        previous = _read_json(state_path) or {}
        lesions = dict(previous.get("lesions") or {})
        todo = []
        for row in rows:
            lesion_fp = _lesion_fingerprint(row["sha256"], self.plan.manifest_sha256, self.plan.subjects_sha256, self.null_k, self.config.null_seed, provider)
            row["lesion_fp"] = lesion_fp
            entry = lesions.get(row["participant_id"])
            if self.config.recompute or not entry or entry.get("fp") != lesion_fp or not all(os.path.isfile(self.absp(p)) for p in entry.get("maps", {}).values()):
                todo.append(row)
        budget = self.plan.budget_bytes
        per_lesion = 36 * self.bundle.n_voxels + 12 * self.null_k * min(self.bundle.n_voxels, engine.NULL_MAX_VOXELS)
        size = int(min(100, max(1, budget // max(per_lesion, 1))))
        batches = [todo[index:index + size] for index in range(0, len(todo), size)]
        for number, batch in enumerate(batches):
            self.cancel.raise_if_cancelled()
            key = fingerprint([row["lesion_fp"] for row in batch])[:12]
            batch_dir = os.path.join(folder, f"batch-{key}")
            os.makedirs(batch_dir, exist_ok=True)
            run_json = os.path.join(batch_dir, "run.json")
            args = ["--bundle", self.bundle.path, "--out", batch_dir, "--run-json", run_json, "--threads", str(self.threads), "--cancel-file", self.private(STOP_NAME),
                    "--null-relocations", str(self.null_k), "--null-seed", str(self.config.null_seed), "--threshold-rule", thresholds.rule_to_json(self.config.rule)]
            if self.config.preview_n:
                args += ["--subjects", str(self.config.preview_n), "--random", "--seed", str(self.config.preview_seed), "--desc-suffix", f"PreviewN{self.config.preview_n}"]
            for row in batch:
                args += ["--lesion", row["snapshot"]]
            self.emit("progress", stage="maps", batch=number + 1, batches=len(batches), done=number, total=len(batches), unit="batches", message=f"engine pass {number + 1}/{len(batches)}")
            child = self._child("maps", "engine", args, batch_dir)
            if child.status != "done":
                return {"status": "failed", "reason": child.message, "fingerprint": fp}
            meta = _read_json(run_json) or {}
            jobs.write_json_atomic(run_json, _portable_paths(meta, batch_dir))  # a shared file: paths relative to its folder
            name_of = {os.path.normcase(os.path.abspath(entry["file"])): entry for entry in meta.get("lesions", [])}
            qa_values = (child.terminal or {}).get("qa") or {}
            for row in batch:
                entry = name_of.get(os.path.normcase(os.path.abspath(row["snapshot"])))
                if entry is None:
                    continue
                maps = (meta.get("maps") or {}).get(entry["name"]) or {}
                lesions[row["participant_id"]] = {"fp": row["lesion_fp"], "maps": {key: self.rel(value) for key, value in maps.items()}, "lesion_sha256": entry.get("sha256"),
                                                  "meta": {key: entry.get(key) for key in ("voxels_in_mask", "weight_total", "volume_mm3", "weight_outside_fraction")}, "qa": qa_values.get(entry["name"])}
            jobs.write_json_atomic(state_path, {"status": "running", "fingerprint": None, "lesions": lesions})
            self.emit("progress", stage="maps", batch=number + 1, batches=len(batches), done=number + 1, total=len(batches), unit="batches", final=True)
        result_maps, outputs, excluded = {}, [], {}
        for row in rows:
            entry = lesions.get(row["participant_id"])
            if not entry:
                continue
            meta = dict(entry.get("meta") or {})
            if _number(meta.get("voxels_in_mask")) == 0:
                excluded[row["participant_id"]] = OUTSIDE_MASK
                continue
            if meta.get("volume_mm3") is not None:
                meta["volume_ml"] = float(meta["volume_mm3"]) / 1000.0
            result_maps[row["participant_id"]] = {"files": entry["maps"], "lesion_sha256": entry.get("lesion_sha256"), "meta": meta, "qa": entry.get("qa")}
            outputs += [self.absp(value) for value in entry["maps"].values()]
        maps_tsv = os.path.join(folder, "maps.tsv")
        self._write_fallback_maps_tsv(maps_tsv, rows, result_maps)
        outputs.append(maps_tsv)
        result = {"maps_tsv": self.rel(maps_tsv), "maps": result_maps, "excluded": excluded, "computed": len(todo), "cache_hits": len(rows) - len(todo), "provider": "engine"}
        outcome = self._save("maps", fp, result, outputs, start=start)
        state = _read_json(state_path)
        state["lesions"] = lesions
        jobs.write_json_atomic(state_path, state)
        return outcome

    def _write_fallback_maps_tsv(self, path, rows, result_maps):
        """maps.tsv (the cohortmaps columns that the stage jobs read) of the fallback provider's maps."""
        folder = os.path.dirname(path)
        columns = ("participant_id", "lesion", "lesion_sha256", "AvgR", "AvgFz", "T", "volume_ml", "voxels_in_mask", "fractional_sum", "weight_outside_fraction")
        lines = ["\t".join(columns)]
        for row in rows:
            entry = result_maps.get(row["participant_id"])
            if entry is None:
                continue
            meta = entry["meta"]

            def text(value):
                return "n/a" if value is None else f"{value:.6g}" if isinstance(value, float) else str(value)

            values = [row["participant_id"], relpath(row["snapshot"], folder), entry.get("lesion_sha256") or row["sha256"], *(relpath(self.absp(entry["files"][key]), folder) for key in ("AvgR", "AvgFz", "T")),
                      text(meta.get("volume_ml")), text(meta.get("voxels_in_mask")), text(meta.get("weight_total")), text(meta.get("weight_outside_fraction"))]
            lines.append("\t".join(values))
        _write_text_atomic(path, "\n".join(lines) + "\n")

    # Group map files

    def _mask_geometry(self):
        if not hasattr(self, "_geometry"):
            from . import nifti

            self._geometry = (self.bundle.mask_indices, tuple(self.bundle.shape), nifti.read_raw_header(self.mask_path))
        return self._geometry

    def _sidecar(self, path, desc, kind, **fields):
        from .export import DISCLAIMER, sidecar_path

        info = self._connectome_info()
        content = {
            "format": GROUP_SIDECAR_FORMAT,
            "version": 1,
            "desc": desc,
            "kind": kind,
            **{key: value for key, value in fields.items() if value is not None},
            "connectome": info,
            "inputs": [os.path.basename(sidecar_path(self.out, row["participant_id"], self.bundle.name, self.config.preview_n)) for row in self.mapped_rows],
            "pipeline_run": self.run_id,
            "disclaimer": DISCLAIMER,
        }
        licence = (self.bundle.provenance or {}).get("licence") if hasattr(self.bundle, "provenance") else None
        if licence and licence != "unknown":
            content["terms"] = licence
        side = path[: -len(".nii.gz")] + ".json" if path.endswith(".nii.gz") else os.path.splitext(path)[0] + ".json"
        jobs.write_json_atomic(side, content)
        return side

    def _connectome_info(self):
        return {"name": self.bundle.name, "manifest_sha256": self.plan.manifest_sha256, "N": len(self.subject_ids), "subject_ids_sha256": self.plan.subjects_sha256}

    def _write_group(self, desc, vector, kind, dtype="float32", fill=0.0, **sidecar):
        """Write a masked vector as group_conn-<label>_desc-<desc>_map.nii.gz in the bundle mask's exact header, with its sidecar."""
        import numpy as np

        from . import geometry, glm, nifti
        from .export import group_map_path

        indices, shape, record = self._mask_geometry()
        path = group_map_path(self.out, self.bundle.name, desc)
        self.current_outputs.append(path)
        descrip = f"LNM {desc} N={len(self.subject_ids)}"
        if dtype == "float32":
            glm._write_on_mask(path, np.asarray(vector, dtype=np.float32), record, indices, shape, fill=fill, descrip=descrip)
        else:
            volume = geometry.from_masked_vector(np.asarray(vector).astype(dtype), indices, shape, dtype=np.dtype(dtype))
            nifti.write_like(path, np.transpose(volume, (2, 1, 0)), self.bundle.affine, self.mask_path, dtype=np.dtype(dtype), descrip=descrip)
        side = self._sidecar(path, desc, kind, **sidecar)
        self.current_outputs.append(side)
        return path, side

    def _copy_group(self, source, kind, provider, **sidecar):
        """Copy a group map written by a child job into the output folder, with a sidecar (D-A21: copies, not links)."""
        match = re.search(r"_desc-([A-Za-z0-9]+)_map\.nii(\.gz)?$", os.path.basename(source))
        desc = match.group(1) if match else os.path.basename(source)
        target = self.path(os.path.basename(source))
        self.current_outputs.append(target)
        _copy_atomic(source, target)
        side = self._sidecar(target, desc, kind, provider=provider, **sidecar)
        self.current_outputs.append(side)
        return desc, target, side

    def _load_vectors(self, column, rows):
        from . import cohort

        paths = [row["maps"][column] for row in rows]
        indices, shape, _record = self._mask_geometry()
        return cohort.load_masked(paths, indices, shape, affine=self.bundle.affine, cancel=self.cancel)

    def _load_vector(self, path):
        from . import cohort

        indices, shape, _record = self._mask_geometry()
        return cohort.load_masked([path], indices, shape, affine=self.bundle.affine)[0]

    # Nulls

    def _stage_nulls(self, planned):
        mode = getattr(self.plan, "nulls_mode", "skip")
        if mode in ("skip", "unavailable") or planned["status"] in ("skip", "unavailable"):
            return self._skip("nulls", planned["reason"] or "skipped", "unavailable" if mode == "unavailable" else "skipped")
        from . import thresholds

        # the nulls job takes the cases from --participants: the group of every mapped subject is a parameter
        population = self._population(self.mapped_rows)[0]
        params = {"mode": mode, "kind": "rl_independent", "rule": self.config.rule, "overlap": self.config.sensitivity, "seed": self.config.null_seed, "k": 1000, "groups": self._groups_fp(),
                  "population": population}
        fp, reused = self._cached("nulls", params, ("maps",))
        if reused:
            return reused
        if population == "all" and any(row["group"] == "case" for row in self.mapped_rows):
            self.info("The null models compare the cases with their null cohorts; the overlap maps use all mapped lesions (--sensitivity-population all).", code="nulls_population")
        start = _now_iso()
        folder = self.stage_dir("nulls")
        args = ["--maps", self.maps_tsv, "--bundle", self.bundle.path, "--kind", "rl_independent", "--mode", mode, "--k", "1000", "--rule", thresholds.rule_to_json(self.config.rule),
                "--overlap", f"{self.config.sensitivity:g}", "--seed", str(self.config.null_seed), "--participants", os.path.join(self.study.path, "participants.tsv"), "--out", folder]
        if self.config.memory_budget_gb:
            args += ["--budget-gb", f"{float(self.config.memory_budget_gb):g}"]
        args += self._common_child_args()
        child = self._child("nulls", "nulls", args, folder)
        if child.status != "done":
            return {"status": "failed" if child.status == "error" else "blocked", "reason": child.message, "fingerprint": fp}
        from . import nulls

        record = _read_json(os.path.join(folder, nulls.NULLS_JSON)) or {}
        groups, outputs = [], []
        for name in sorted(os.listdir(folder)):
            if name.startswith("group_conn-") and name.endswith("_map.nii.gz"):
                desc = re.search(r"_desc-([A-Za-z0-9]+)_map", name).group(1)
                kind = NULL_MAP_KINDS.get(desc, "p" if desc.endswith(("P", "Q", "PFwe")) else "stat")
                desc, target, side = self._copy_group(os.path.join(folder, name), kind, "nulls", statistic=record.get("statistic"), caption=record.get("caption"), mode=record.get("mode_label"),
                                                      k=record.get("k"), k_pool=record.get("k_pool"), rule=self.config.rule, sign=record.get("sign"))
                groups.append(desc)
                outputs += [target, side]
        result = {key: record.get(key) for key in ("kind", "mode", "mode_label", "approximate", "k", "k_pool", "min_attainable_p", "min_attainable_q", "fdr_floor_voxels", "caption", "classic_rule", "group_qa")}
        result.update({"descs": groups, "folder": self.rel(folder), "null_group_maps": os.path.isfile(os.path.join(folder, nulls.NULL_GROUP_FILE))})
        return self._save("nulls", fp, result, outputs, child_run_id=child.run_id, events=child.events, start=start)

    # Overlap

    def _population(self, rows):
        groups = [row["group"] for row in rows]
        population = self.config.sensitivity_population
        if population == "auto":
            population = "cases" if "case" in groups else "all"
        return population, (groups if population == "cases" else None)

    def _groups_fp(self):
        return [(row["participant_id"], row["group"]) for row in self.mapped_rows]

    def _stage_overlap(self, planned):
        import numpy as np

        from . import thresholds

        rows = self.mapped_rows
        population, groups = self._population(rows)
        params = {"rules": self.config.rules, "allow": self.config.allow_rule_anyway, "sensitivity": self.config.sensitivity, "population": population, "groups": self._groups_fp(), "N": len(self.subject_ids)}
        fp, reused = self._cached("overlap", params, ("maps",))
        if reused:
            return reused
        start = _now_iso()
        provider = self.providers["overlap"]
        n, v = len(self.subject_ids), self.bundle.n_voxels
        k = round(self.config.sensitivity)
        thresholds_out, outputs, counts = [], [], {row["participant_id"]: {} for row in rows}
        loaded, primary_cuts = {}, None
        for index, rule in enumerate(self.config.rules):
            self.cancel.raise_if_cancelled()
            tag = "" if index == 0 else rule_tag(rule)
            statistic = "T"
            if provider.kind == "preferred":
                from . import cohort

                statistic = cohort.rule_statistic(rule)
            elif rule["kind"] in ("fz_abs",) or (rule["kind"] == "top_pct" and rule.get("statistic", "Fz") == "Fz"):
                statistic = "Fz"
            column = "T" if statistic == "T" else "AvgFz"
            if column not in loaded:
                loaded[column] = self._load_vectors(column, rows)
            Y = loaded[column]
            resolved = thresholds.resolve(rule, n, v, values=Y[0] if rule["kind"] == "top_pct" else None)
            allowed = bool(resolved.allowed or rule.get("override") or self.config.allow_rule_anyway)
            entry = {"tag": rule_tag(rule), "primary": index == 0, "rule": rule, "resolved": resolved.to_dict(), "derived": thresholds.describe(resolved), "allowed": allowed}
            thresholds_out.append(entry)
            if not allowed:
                entry["refused"] = resolved.banner or "not allowed for this N"
                self.warn(f"The threshold rule {thresholds.rule_label(rule)} is refused for N = {n}: {entry['refused']}", code="rule_refused")
                continue
            if self.config.allow_rule_anyway and not resolved.allowed:
                rule = {**rule, "override": True}
            for sign, suffix in (("pos", "Pos"), ("neg", "Neg")):
                if provider.kind == "preferred":
                    result = cohort.group_maps(Y, rule, groups=groups, sensitivity_pct=self.config.sensitivity, sign=sign, n=n, legacy=(), conjunction_map=False, ids=[row["participant_id"] for row in rows])
                    count, percent, sens = result.maps["overlapCount"], result.maps["overlapPct"], result.maps[cohort.sensitivity_desc(self.config.sensitivity)]
                    values = [item.value for item in result.resolved]
                    population_rows = [rows[i] for i in result.cases]
                else:
                    from . import stats

                    case_index = [i for i, row in enumerate(rows) if groups is None or row["group"] == "case"] or list(range(len(rows)))
                    values = [thresholds.resolve(rule, n, v, values=Y[i] if rule["kind"] == "top_pct" else None).value for i in range(len(rows))]
                    binary = np.stack([stats.binarize(Y[i], values[i], sign) for i in case_index])
                    count, percent = stats.overlap(binary)
                    sens = stats.sensitivity_map(percent, self.config.sensitivity).astype(np.uint8)
                    population_rows = [rows[i] for i in case_index]
                if index == 0 and rule["kind"] == "top_pct" and primary_cuts is None:
                    # a rank rule has a cut per map: the export stage records each lesion's own cut in its sidecar
                    primary_cuts = {row["participant_id"]: values[i] for i, row in enumerate(rows)}
                for i, row in enumerate(rows):
                    passing = Y[i] >= values[i] if sign == "pos" else Y[i] <= -values[i]
                    counts[row["participant_id"]][f"{entry['tag']}{suffix}"] = int(np.count_nonzero(passing))
                common = {"statistic": statistic, "sign": sign, "population": population, "rule": rule, "resolved": resolved.to_dict(), "derived": thresholds.describe(resolved),
                          "n_maps": len(population_rows), "n_cases": sum(1 for row in population_rows if row["group"] == "case"), "n_controls": sum(1 for row in rows if row["group"] == "control"),
                          "provider": provider.module, "rule_tag": entry["tag"], "note": "Classic overlap map (hub-inflated); see the null models for a null-referenced version."}
                count_dtype = "uint8" if len(population_rows) <= 255 else "int16"
                for desc, vector, kind, dtype in ((f"overlapCount{suffix}{tag}", count, "count", count_dtype), (f"overlapPct{suffix}{tag}", percent, "percent", "float32"),
                                                  (f"sens{k}{suffix}{tag}", sens, "binary", "uint8")):
                    path, side = self._write_group(desc, vector, kind, dtype, **common, sensitivity_pct=self.config.sensitivity if kind == "binary" else None)
                    outputs += [path, side]
        # meanT (descriptive) over the sensitivity population
        if "T" not in loaded:
            loaded["T"] = self._load_vectors("T", rows)
        members = [i for i, row in enumerate(rows) if groups is None or row["group"] == "case"] or list(range(len(rows)))
        mean_t = loaded["T"][members].mean(axis=0)
        path, side = self._write_group("meanT", mean_t, "mean", "float32", statistic="T", sign="both", population=population, n_maps=len(members), provider="numpy mean",
                                       note="Mean of the per-lesion T maps (descriptive; not a test statistic).")
        outputs += [path, side]
        result = {"thresholds": thresholds_out, "per_lesion_counts": counts, "population": population, "n_population": len(members), "sensitivity": self.config.sensitivity, "k": k}
        if primary_cuts is not None:
            result["primary_cuts"] = {sid: float(value) if math.isfinite(value) else None for sid, value in primary_cuts.items()}
        return self._save("overlap", fp, result, outputs, start=start)

    # Lesion set

    def _stage_lesionset(self, planned):
        from . import lesionset

        rows = self.mapped_rows
        params = {"lesions": sorted((row["participant_id"], row["sha256"], row["group"]) for row in rows), "grid": self.plan.manifest_sha256}
        fp, reused = self._cached("lesionset", params, ("maps",))
        if reused:
            return reused
        start = _now_iso()
        from .study import Study

        space = (self.bundle.metadata.get("space") if isinstance(self.bundle.metadata, dict) else None) or self.study.space or lesionset.DEFAULT_SPACE
        subset_rows = []
        for row in rows:
            copy = self.study.subject(row["participant_id"])
            copy["lesion"] = row["snapshot"]
            copy["include"] = "1"
            if row.get("lesion") != copy.get("lesion") and "norm_method" in copy:
                copy["norm_method"] = "n/a"
            if not copy.get("space") or copy.get("space") in ("n/a", "native"):
                copy["space"] = space
            subset_rows.append(copy)
        subset = Study(self.study.path, dict(self.study.metadata, review_required=False), self.study.columns, subset_rows, self.study.dictionary)
        try:
            choice = lesionset.resolve_grid(subset, self.bundle, space=space)
            occ = lesionset.load_occupancies(subset, choice, require_review=False, space=space, cancel=self.cancel)
        except (OSError, ValueError) as exc:
            return {"status": "failed", "reason": f"{type(exc).__name__}: {exc}", "fingerprint": fp}
        maps = lesionset.overlap_maps(occ)
        summary = lesionset.summarize(occ, maps=maps)
        indices, _shape, _record = self._mask_geometry()
        count = maps.count.reshape(-1)[indices]
        percent = maps.percent.reshape(-1)[indices]
        outputs = []
        common = {"statistic": "lesion", "sign": "pos", "population": "all", "n_maps": maps.n, "provider": "lesionset", "legend": maps.legend, "binarize_at": maps.binarize_at,
                  "note": "Exact integer lesion overlap on the connectome grid (lesion voxels outside the connectome mask are not shown)."}
        for desc, vector, kind, dtype in (("lesionCount", count, "count", "uint8" if maps.n <= 255 else "int16"), ("lesionPct", percent, "percent", "float32")):
            path, side = self._write_group(desc, vector, kind, dtype, **common)
            outputs += [path, side]
        stage_folder = self.stage_dir("lesionset")
        json_path, _inbox = lesionset.write_lesionset_json(self.study.path, summary, occ, out_dir=stage_folder, post=False, outputs={"lesionCount": os.path.basename(outputs[0]), "lesionPct": os.path.basename(outputs[2])})
        target = self.path(lesionset.LESIONSET_JSON)
        _copy_atomic(json_path, target)
        outputs += [json_path, target]
        dice = summary.get("dice") or {}
        result = {"n": summary.get("n"), "dice_mean": dice.get("mean"), "dice_median": dice.get("median"), "volume": summary.get("volume"), "hemisphere": summary.get("hemisphere"), "excluded": summary.get("excluded"),
                  "legend": maps.legend}
        return self._save("lesionset", fp, result, outputs, start=start)

    # Specificity

    def _participant_values(self, columns, rows):
        values = []
        for row in rows:
            subject = self.study.subject(row["participant_id"])
            values.append([row["participant_id"], *(subject.get(column) for column in columns)])
        return values

    def _subset_study(self, rows):
        from .study import Study, is_missing

        out = []
        for row in rows:
            copy = self.study.subject(row["participant_id"])
            volume = (row.get("lesion_meta") or {}).get("volume_ml")
            if is_missing(copy.get("volume_ml")) and volume is not None:
                copy["volume_ml"] = f"{volume:.6g}"
            out.append(copy)
        return Study(self.study.path, dict(self.study.metadata), self.study.columns, out, self.study.dictionary)

    def _design_spec(self, test="two_group", score=None):
        from . import design as designlib

        inference = {"fwe": True, "fdr": self.config.fdr, "tfce": self.config.tfce if test == "two_group" else False, "n_perm": self.config.perm, "seed": self.config.perm_seed}
        covariates = list(self.config.covariates)
        if test == "two_group":
            return designlib.DesignSpec(test="two_group", group_column="group", levels=["case", "control"], contrast="two_sided", covariates=covariates, input="Fz" if self.config.input == "AvgFz" else "T",
                                        inference=inference, volume_off_reason=self.config.volume_off_reason)
        return designlib.DesignSpec(test="continuous", score_column=score, contrast="two_sided", covariates=covariates, input="Fz" if self.config.input == "AvgFz" else "T", inference=inference,
                                    volume_off_reason=self.config.volume_off_reason)

    def _stage_specificity(self, planned):
        if planned["status"] in ("skip", "unavailable") or self.plan.specificity != "glm":
            return self._skip("specificity", planned["reason"] or self.plan.specificity_reason or "skipped", "unavailable" if self.plan.specificity == "unavailable" else "skipped")
        import numpy as np

        from . import design as designlib

        rows = self.mapped_rows
        columns = ["group", *self.config.covariates]
        params = {"design": self._design_spec().to_dict(), "values": self._participant_values(columns, rows), "perm": self.config.perm, "seed": self.config.perm_seed, "alpha": self.config.alpha}
        fp, reused = self._cached("specificity", params, ("maps",))
        if reused:
            return reused
        start = _now_iso()
        folder = self.stage_dir("glm")
        spec = self._design_spec()
        try:
            design = designlib.build_design(self._subset_study(rows), spec)
        except designlib.DesignError as exc:
            return {"status": "blocked", "reason": f"The design cannot be built: {exc}", "fingerprint": fp}
        design_json, _matrix = designlib.save_design(design, folder)
        if design.blocking:
            return {"status": "blocked", "reason": "The design is blocked: " + " | ".join(check.message for check in design.blocking), "fingerprint": fp, "result": {"design_type": design.design_type}}
        args = ["--maps", self.maps_tsv, "--participants", os.path.join(self.study.path, "participants.tsv"), "--design", design_json, "--mask", self.mask_path, "--out", folder, "--perm", str(self.config.perm),
                "--seed", str(self.config.perm_seed), "--conn-label", self.bundle.name]
        if self.config.tfce:
            args.append("--tfce")
        args += self._common_child_args()
        child = self._child("specificity", "glm", args, folder)
        if child.status != "done":
            return {"status": "blocked" if child.status == "blocked" else "failed", "reason": child.message, "fingerprint": fp}
        from . import glm

        record = glm.read_record(os.path.join(folder, glm.GLM_JSON)) or {}
        label = (record.get("contrast") or {}).get("label") or glm.contrast_label(design)
        outputs, descs = [os.path.join(folder, glm.GLM_JSON)], {}
        common = {"population": "cases and controls", "n_maps": record.get("n"), "n_cases": (record.get("n_per_level") or {}).get("case"), "n_controls": (record.get("n_per_level") or {}).get("control"),
                  "design": {"type": record.get("design_type"), "test": record.get("test"), "contrast": label, "limitations": record.get("limitations")}, "perm": (record.get("permutation") or {}).get("n_perm"),
                  "perm_seed": self.config.perm_seed, "alpha": self.config.alpha, "covariates": record.get("covariates"), "statistic": self.config.input}
        kinds = {"Tstat": "t", "Fstat": "t", "PUnc": "p", "PFwe": "p", "PFdr": "p", "TfcePFwe": "p", "HedgesG": "g", "PartialR": "r", "PartialEtaSq": "g"}
        for key, name in (record.get("outputs") or {}).items():
            source = os.path.join(folder, name)
            stat = next((suffix for suffix in kinds if name.endswith(f"{suffix}_map.nii.gz")), None)
            desc, target, side = self._copy_group(source, kinds.get(stat, "t"), "glm", sign="both", **common)
            descs[key] = desc
            outputs += [target, side]
        t = self._load_vector(self.path(os.path.basename(record["outputs"]["stat"]))) if "stat" in (record.get("outputs") or {}) else None
        p = self._load_vector(self.path(os.path.basename(record["outputs"]["p_fwe"]))) if "p_fwe" in (record.get("outputs") or {}) else None
        voxel_ml = float(abs(np.linalg.det(self.bundle.affine[:3, :3]))) / 1000.0
        fwe = {}
        if t is not None and p is not None:
            for sign, test in (("pos", t > 0), ("neg", t < 0)):
                count = int(np.count_nonzero(test & (p < self.config.alpha)))
                fwe[sign] = {"voxels": count, "ml": round(count * voxel_ml, 3)}
        result = {"label": label, "descs": descs, "design_type": record.get("design_type"), "limitations": record.get("limitations"), "fwe": fwe, "min_p": record.get("min_attainable_p"),
                  "fdr_floor": (record.get("fdr") or {}).get("floor_voxels"), "n": record.get("n"), "n_per_level": record.get("n_per_level"), "excluded": record.get("excluded"), "run_id": record.get("run_id")}
        return self._save("specificity", fp, result, outputs, child_run_id=child.run_id, events=child.events, start=start)

    # sLNM

    def _score_rows(self, column, rows):
        from .study import is_missing

        out = []
        for row in rows:
            value = self.study.subject(row["participant_id"]).get(column)
            if is_missing(value):
                continue
            try:
                out.append((row, float(value)))
            except ValueError:
                continue
        return out

    def _stage_slnm(self, planned):
        columns = list(self.plan.scores)
        if not columns or planned["status"] in ("skip", "unavailable"):
            return self._skip("slnm", planned["reason"] or "no confirmed score column", "skipped")
        import numpy as np

        rows = self.mapped_rows
        params = {"scores": columns, "values": self._participant_values([*columns, *self.config.covariates], rows), "perm": self.config.perm, "seed": self.config.perm_seed, "method": self.config.slnm_method,
                  "covariates": self.config.covariates, "min_n": self.config.min_slnm_n, "input": self.config.input, "alpha": self.config.alpha, "volume_off_reason": self.config.volume_off_reason}
        fp, reused = self._cached("slnm", params, ("maps",))
        if reused:
            return reused
        start = _now_iso()
        from . import slnm

        labels = unique_labels(columns)
        results, outputs, failed, run_ids = {}, [], [], []
        for column, label in zip(columns, labels, strict=True):
            self.cancel.raise_if_cancelled()
            scored = self._score_rows(column, rows)
            if len(scored) < self.config.min_slnm_n:
                self.info(f"sLNM of {column} not run: {len(scored)} mapped subjects have a value (at least {self.config.min_slnm_n} needed).", code="slnm_min_n")
                results[label] = {"column": column, "n": len(scored), "skipped": f"fewer than {self.config.min_slnm_n} values"}
                continue
            folder = self.stage_dir(f"slnm-{label}")
            args = ["--maps", self.maps_tsv, "--participants", os.path.join(self.study.path, "participants.tsv"), "--score", column, "--covariates", ",".join(self.config.covariates)]
            if self.config.volume_off_reason and "volume_ml" not in self.config.covariates:
                args += ["--volume-off-reason", self.config.volume_off_reason]
            if self.config.slnm_method == "spearman":
                args.append("--spearman")
            args += ["--perm", str(self.config.perm), "--seed", str(self.config.perm_seed), "--bundle", self.bundle.path, "--mask", self.mask_path, "--out", folder, "--conn-label", self.bundle.name,
                     "--input", "Fz" if self.config.input == "AvgFz" else "T"]
            args += self._common_child_args()
            child = self._child("slnm", "slnm", args, folder)
            if child.status != "done":
                failed.append(f"{column}: {child.message}")
                results[label] = {"column": column, "n": len(scored), "failed": child.message}
                continue
            run_ids.append(child.run_id)
            record = _read_json(os.path.join(folder, slnm.SLNM_JSON)) or {}
            desc_stem = record.get("desc") or slnm.score_label(column)
            descs = {}
            for key, name in (record.get("outputs") or {}).items():
                if not (isinstance(name, str) and name.startswith("group_conn-")) or ":" in key or key.startswith(("lodo", "stouffer", "i2")):
                    continue
                kind = "r" if name.endswith("R_map.nii.gz") else "p"
                desc, target, side = self._copy_group(os.path.join(folder, name), kind, "slnm", score=column, method=self.config.slnm_method, covariates=self.config.covariates, perm=self.config.perm,
                                                      perm_seed=self.config.perm_seed, alpha=self.config.alpha, n_maps=record.get("n"), statistic="partial r", sign="both", population="mapped lesions with a score")
                descs[key] = desc
                outputs += [target, side]
            volumes = [((row.get("lesion_meta") or {}).get("volume_ml"), value) for row, value in scored]
            pairs = np.array([(v, s) for v, s in volumes if v is not None], dtype=np.float64)
            r_volume = float(np.corrcoef(pairs[:, 0], pairs[:, 1])[0, 1]) if len(pairs) > 2 and np.ptp(pairs[:, 0]) > 0 and np.ptp(pairs[:, 1]) > 0 else None
            r_map = None
            fwe_voxels = None
            if "r" in descs:
                r_map = self._load_vector(self.path(os.path.basename(record["outputs"]["r"])))
                if "p_fwe" in descs:
                    p_map = self._load_vector(self.path(os.path.basename(record["outputs"]["p_fwe"])))
                    fwe_voxels = int(np.count_nonzero(p_map < self.config.alpha))
            results[label] = {"column": column, "n": record.get("n"), "method": self.config.slnm_method, "covariates": self.config.covariates, "max_abs_r": None if r_map is None else float(np.nanmax(np.abs(r_map))),
                              "fwe_voxels": fwe_voxels, "r_with_volume": None if r_volume is None or not math.isfinite(r_volume) else round(r_volume, 4), "descs": descs, "stem": desc_stem,
                              "folder": self.rel(folder), "run_id": record.get("run_id")}
        status = "done"
        reason = None
        if failed and len(failed) == sum(1 for value in results.values() if "skipped" not in value):
            status, reason = "failed", " | ".join(failed)
        elif failed:
            reason = "some scores failed: " + " | ".join(failed)
            self.warn(reason, code="slnm_failed")
        if status != "done":
            return {"status": status, "reason": reason, "fingerprint": fp, "result": {"scores": results}}
        outcome = self._save("slnm", fp, {"scores": results}, outputs, child_run_id=",".join(run for run in run_ids if run) or None, start=start)
        outcome["reason"] = reason
        return outcome

    # Conjunction

    def _stage_conjunction(self, planned):
        spec = self.results.get("specificity") or {}
        overlap = self.results.get("overlap") or {}
        if self.stage_records.get("specificity", {}).get("status") not in ("done",) or not spec.get("descs"):
            return self._skip("conjunction", "specificity did not run: the conjunction needs the GLM maps")
        import numpy as np

        params = {"alpha": self.config.alpha, "k": overlap.get("k")}
        fp, reused = self._cached("conjunction", params, ("overlap", "specificity"))
        if reused:
            return reused
        start = _now_iso()
        from .export import group_map_path

        k = overlap.get("k", round(self.config.sensitivity))
        t = self._load_vector(group_map_path(self.out, self.bundle.name, spec["descs"]["stat"]))
        p = self._load_vector(group_map_path(self.out, self.bundle.name, spec["descs"]["p_fwe"]))
        significant = (p < self.config.alpha).astype(np.uint8)
        voxel_ml = float(abs(np.linalg.det(self.bundle.affine[:3, :3]))) / 1000.0
        outputs, result = [], {"alpha": self.config.alpha, "sensitivity": k}
        for sign, suffix in (("pos", "Pos"), ("neg", "Neg")):
            sens = (self._load_vector(group_map_path(self.out, self.bundle.name, f"sens{k}{suffix}")) != 0).astype(np.uint8)
            if self.providers["conjunction"].kind == "preferred":
                from . import cohort

                conj = cohort.conjunction(sens, t.astype(np.float32), significant, sign=sign).astype(np.uint8)
            else:
                from . import stats

                conj = stats.conjunction(sens, (t > 0) if sign == "pos" else (t < 0), significant).astype(np.uint8)
            count = int(np.count_nonzero(conj))
            result[f"{sign}_voxels"] = count
            result[f"{sign}_ml"] = round(count * voxel_ml, 3)
            path, side = self._write_group(f"conj{suffix}", conj, "binary", "uint8", statistic=f"sens{k}{suffix} AND sign(t) AND PFwe < {self.config.alpha:g}", sign=sign, alpha=self.config.alpha,
                                           provider=self.providers["conjunction"].module, note="Conjunction of the classic sensitivity map with the GLM specificity map (sign-specific).")
            outputs += [path, side]
        return self._save("conjunction", fp, result, outputs, start=start)

    # QA

    def _stage_qa(self, planned):
        import numpy as np

        from . import qa as qalib
        from .export import group_map_path, subjects_sha256

        rows = self.mapped_rows
        reference = qalib.load_reference(self.bundle, subjects_sha256(self.subject_ids)) if self.providers["qa"].kind == "preferred" else None
        ref_id = None if reference is None else getattr(reference, "sha256", None) or str((reference.metadata or {}).get("created"))
        params = {"reference": ref_id, "degree": self.bundle.degree is not None}
        fp, reused = self._cached("qa", params, ("maps", "overlap", "specificity", "slnm", "nulls"))
        if reused:
            return reused
        start = _now_iso()
        lesions, levels, mu = {}, collections.Counter(), []
        T = self._load_vectors("T", rows) if reference is not None else None
        for i, row in enumerate(rows):
            values = dict(row.get("qa") or {})
            if reference is not None:
                try:
                    values["reference"] = qalib.lesion_qa(T[i], reference=reference).get("reference")
                except ValueError as exc:
                    values["reference"] = {"level": "not_calibrated", "text": f"not available ({exc})"}
            ref = values.get("reference") or {}
            if ref.get("mu_r") is not None:
                mu.append(ref["mu_r"])
            chips = [ref.get("level"), values.get("degree_level"), values.get("null_level")]
            worst = "red" if "red" in chips else "amber" if "amber" in chips else "ok"
            levels[worst] += 1
            values["chip"] = worst
            values["calibrated"] = ref.get("level") not in (None, "not_calibrated")
            lesions[row["participant_id"]] = values
        group = {}
        null_maps, null_voxels = None, None
        nulls_result = self.results.get("nulls") or {}
        if nulls_result.get("null_group_maps"):
            from . import nulls

            try:
                null_maps, null_voxels, _statistic = nulls.load_null_group_maps(self.absp(nulls_result["folder"]))
            except (OSError, ValueError):
                null_maps = None
        targets = [("meanT", "meanT", null_maps)]
        spec = self.results.get("specificity") or {}
        if spec.get("descs", {}).get("stat"):
            targets.append((spec["descs"]["stat"], spec["descs"]["stat"], None))
        for label, value in ((self.results.get("slnm") or {}).get("scores") or {}).items():
            if (value.get("descs") or {}).get("r"):
                targets.append((value["descs"]["r"], value["descs"]["r"], None))
        degree = self.bundle.degree
        for key, desc, nulls_for in targets:
            path = group_map_path(self.out, self.bundle.name, desc)
            if not os.path.isfile(path):
                continue
            vector = self._load_vector(path)
            if reference is not None:
                group[key] = qalib.group_qa(vector, reference, nulls_for, null_voxels if nulls_for is not None else None, null_kind="rl_independent")
            elif degree is not None:
                r, rho = qalib.degree_similarity(vector, degree)
                group[key] = {"degree_r": None if not np.isfinite(r) else float(r), "degree_rho": None if not np.isfinite(rho) else float(rho), "level": qalib.chip_level(r)[0]}
            else:
                group[key] = {"level": "not_available", "text": "QA not available: no reference maps or degree map for this connectome"}
        summary = {"median_r_mu": None if not mu else float(np.median(mu)), "n_amber": levels["amber"], "n_red": levels["red"], "n": len(rows), "reference": reference is not None,
                   "calibrated": any(value.get("calibrated") for value in lesions.values())}
        path = self.path("qa.json")
        self.current_outputs.append(path)
        jobs.write_json_atomic(path, {"format": QA_FORMAT, "version": 1, "summary": summary, "lesions": lesions, "group": group,
                                      "note": "Per-lesion chips are calibrated only when the lesion's relocation null maps are kept; otherwise they are 'not calibrated'."})
        return self._save("qa", fp, {"summary": summary, "lesions": lesions, "group": group}, [path], start=start)

    # Clusters

    def _labeler(self):
        try:
            from . import atlases, clusters
        except ImportError:
            return None, []
        found = []
        try:
            if self.config.atlas:
                from . import nifti

                data, affine, _header = nifti.read_nifti(self.config.atlas)
                labels = atlases.read_label_table(self.config.atlas_labels) if self.config.atlas_labels else None
                family = atlases.mask_family(data.shape[:3], affine)
                found = [atlases.Atlas(os.path.basename(self.config.atlas).split(".")[0], data, affine, labels=labels, family=family)]
            else:
                found = atlases.load_cached()
        except Exception as exc:  # labels are optional: a broken atlas never fails the stage
            self.warn(f"Atlas labels not available ({type(exc).__name__}: {exc})", code="atlas")
            found = []
        if not found:
            return None, []
        self._atlases = found
        return clusters.atlas_labeler(found), [atlas.id for atlas in found]

    def _stage_clusters(self, planned):
        import numpy as np

        from . import stats, thresholds
        from .export import group_map_path, write_peaks_tsv

        atlas_key = None if not self.config.atlas else (file_sha256(self.config.atlas) if os.path.isfile(self.config.atlas) else self.config.atlas)
        params = {"alpha": self.config.alpha, "atlas": atlas_key, "rule": self.config.rule}
        fp, reused = self._cached("clusters", params, ("overlap", "specificity", "slnm", "conjunction"))
        if reused:
            return reused
        start = _now_iso()
        indices, _shape, _record = self._mask_geometry()
        grid = self.bundle.grid
        n = len(self.subject_ids)
        headline = []
        spec = self.results.get("specificity") or {}
        if spec.get("descs", {}).get("stat") and spec["descs"].get("p_fwe"):
            headline.append((spec["descs"]["stat"], spec["descs"]["p_fwe"], "Tstat"))
        for value in ((self.results.get("slnm") or {}).get("scores") or {}).values():
            descs = value.get("descs") or {}
            if descs.get("r") and descs.get("p_fwe"):
                headline.append((descs["r"], descs["p_fwe"], "partial r"))
        outputs, tables = [], {}
        preferred = self.providers["clusters"].kind == "preferred"
        labeler, atlas_ids = self._labeler() if preferred else (None, [])

        def write_table(desc, values, threshold, sign, statistic):
            if not preferred:
                return
            from . import clusters

            table = clusters.cluster_table(values, grid, indices, threshold, sign=sign, labeler=labeler, statistic=statistic)
            tsv, html = self.path(f"clusters_{desc}.tsv"), self.path(f"clusters_{desc}.html")
            self.current_outputs += [tsv, html]
            caption = clusters.cluster_caption(table, n=n if statistic == "T" else None, atlases=atlas_ids)
            clusters.write_clusters(table, tsv, html, caption=caption)
            outputs.extend([tsv, html])
            tables[desc] = {"clusters": sum(1 for row in table if not row["subpeak"]), "tsv": os.path.basename(tsv), "html": os.path.basename(html), "threshold": float(table.threshold)}

        for desc, p_desc, statistic in headline:
            self.cancel.raise_if_cancelled()
            values = self._load_vector(group_map_path(self.out, self.bundle.name, desc)).astype(np.float64)
            p = self._load_vector(group_map_path(self.out, self.bundle.name, p_desc))
            significant = p < self.config.alpha
            if not significant.any():
                tables[desc] = {"clusters": 0, "note": f"no voxel with PFwe < {self.config.alpha:g}"}
                continue
            masked = np.where(significant, values, np.nan)
            threshold = float(np.nanmin(np.abs(masked)))
            write_table(desc, masked, max(threshold, 1e-12), "both", statistic)
        mean_path = group_map_path(self.out, self.bundle.name, "meanT")
        mean_t = self._load_vector(mean_path).astype(np.float64) if os.path.isfile(mean_path) else None
        rule = self.config.rule
        t_rule = rule["kind"] in ("fwe_bonferroni", "t_convention", "custom_t") or (rule["kind"] == "top_pct" and rule.get("statistic") == "T")
        if mean_t is not None and t_rule:
            resolved = thresholds.resolve(rule, n, self.bundle.n_voxels, values=mean_t if rule["kind"] == "top_pct" else None)
            write_table("meanT", mean_t, resolved, "both", "T")
        elif mean_t is not None:
            tables["meanT"] = {"clusters": None, "note": "the primary rule applies to mean Fz, not to T: no meanT cluster table"}
        for suffix in ("Pos", "Neg"):
            path = group_map_path(self.out, self.bundle.name, f"conj{suffix}")
            if os.path.isfile(path):
                write_table(f"conj{suffix}", self._load_vector(path).astype(np.float64), 0.5, "pos", "conjunction")
        # peaks.tsv of the headline map: the GLM Tstat when specificity ran, else meanT
        peak_source = spec["descs"]["stat"] if spec.get("descs", {}).get("stat") else "meanT"
        peak_path = group_map_path(self.out, self.bundle.name, peak_source)
        if os.path.isfile(peak_path):
            peaks = stats.peak_table(self._load_vector(peak_path), grid, top_k=10, min_distance_mm=10.0, mask_indices=indices, sign="both")
            labels = None
            if labeler is not None and peaks and getattr(self, "_atlases", None):
                from . import atlases as atlaslib

                try:
                    labels = [_label_text(found) for found in atlaslib.label_points([peak["world"] for peak in peaks], self._atlases)]
                except Exception:  # labels are optional
                    labels = None
            target = self.path("peaks.tsv")
            self.current_outputs.append(target)
            write_peaks_tsv(target, peaks, labels)
            outputs.append(target)
            tables["peaks"] = {"source": peak_source, "n": len(peaks)}
        return self._save("clusters", fp, {"tables": tables, "atlases": atlas_ids}, outputs, start=start)

    # Export

    def _engine_argv(self, rows):
        from . import thresholds

        argv = ["--bundle", self.bundle.path]
        for row in rows:
            argv += ["--lesion", row["snapshot"]]
        if self.config.preview_n:
            argv += ["--subjects", str(self.config.preview_n), "--random", "--seed", str(self.config.preview_seed), "--desc-suffix", f"PreviewN{self.config.preview_n}"]
        argv += ["--null-relocations", str(self.null_k), "--null-seed", str(self.config.null_seed), "--threshold-rule", thresholds.rule_to_json(self.config.rule)]
        return argv

    def _stage_export(self, planned):
        from . import export, thresholds

        rows = self.mapped_rows
        sids = [row["participant_id"] for row in rows]
        params = {"config": self.config.to_json(analysis_only=True, base=self.out), "software": self.config.software, "rows": [(row["participant_id"], row["sha256"], row.get("lesion")) for row in rows]}
        fp, reused = self._cached("export", params, tuple(name for name, _ in STAGES[1:13]))
        if reused:
            return reused
        start = _now_iso()
        from .export import subjects_sha256

        run_meta = {
            "n": len(self.subject_ids),
            "subject_ids_sha256": subjects_sha256(self.subject_ids),
            "subjects": {"n": len(self.subject_ids), "requested": self.config.preview_n, "random": bool(self.config.preview_n), "seed": self.config.preview_seed},
            "seed": self.config.preview_seed,
            "desc_suffix": export.preview_suffix(self.config.preview_n),
            "threshold_rule": self.config.rule,
            "lesions": [{"name": row["participant_id"], "file": row["snapshot"], "sha256": row["sha256"], **{key: (row.get("lesion_meta") or {}).get(key) for key in ("voxels_in_mask", "weight_total", "volume_mm3", "weight_outside_fraction")}}
                        for row in rows],
            "null": {"k": self.null_k, "seed": self.config.null_seed},
            "cwd": LIB_PARENT,
            "start": self.created,
            "end": _now_iso(),
            "status": "done",
            "versions": export.versions(),
            "host": export.host_info(),
            "argv": self._engine_argv(rows),
            "threads": self.threads,
        }
        maps = {row["participant_id"]: {key: row["maps"][key] for key in ("AvgR", "AvgFz", "T")} for row in rows}
        qa = {sid: (self.results.get("qa") or {}).get("lesions", {}).get(sid) for sid in sids}
        per_lesion = self._top_pct_resolved(rows) if self.config.rule["kind"] == "top_pct" else {}
        if per_lesion:
            # a rank rule has no single value: the run records the first lesion's cut, and each sidecar its own (below)
            resolved = per_lesion[sids[0]]
        else:
            resolved = thresholds.resolve(self.config.rule, len(self.subject_ids), self.bundle.n_voxels)
        package = export.write_run(self.out, maps, sids, self.bundle, rule=self.config.rule, resolved=resolved, qa=qa, run_meta=run_meta, preview_n=self.config.preview_n, software=self.config.software or None)
        outputs = list(package["maps"]) + list(package["sidecars"]) + [package["provenance"], package["reproduce"]]
        self.current_outputs += outputs
        for row, sidecar in zip(rows, package["sidecars"], strict=True):
            content = _read_json(sidecar) or {}
            if row["participant_id"] in per_lesion and isinstance(content.get("threshold"), dict):
                own = per_lesion[row["participant_id"]]
                content["threshold"] = {**content["threshold"], "resolved": own.to_dict(), "derived": thresholds.describe(own)}
            content["pipeline"] = {"run_id": self.run_id, "source": row.get("lesion"), "group": row["group"], "export_link_mode": LINK_MODE}
            jobs.write_json_atomic(sidecar, content)
        methods_result = {"provider": "export.write_run"}
        self._write_pipeline_json("running")
        written = None
        if self.providers["export"].kind == "preferred":
            try:
                written = export.write_methods(self.study.path, self.out, formats=("md", "html", "txt"))
            except jobs.Cancelled:
                raise
            except Exception as exc:  # the data outputs are complete: keep write_run's methods.md
                self.warn(f"The methods sections could not be written ({type(exc).__name__}: {exc}); methods.md is the export's methods paragraph.", code="methods_failed")
        if written:
            methods_result = {"provider": "methods (L-18)", "files": [os.path.basename(written[fmt]) for fmt in ("md", "html", "txt")], "checklist": os.path.basename(written["checklist"]) if written.get("checklist") else None,
                              "battery": written.get("battery"), "items": written.get("items")}
            outputs += [written[fmt] for fmt in ("md", "html", "txt")] + ([written["checklist"]] if written.get("checklist") else [])
        else:
            outputs.append(package["methods"])
        try:
            from . import methods

            with open(self.path("methods.md"), encoding="utf-8") as f:
                lint = methods.lint_wording(f.read())
        except (ImportError, OSError, AttributeError):
            lint = []
        if lint:
            self.warn(f"methods.md uses wording the reporting rules avoid: {', '.join(sorted(set(lint)))}", code="wording")
        methods_result["lint"] = lint
        reproduce = self._write_reproduce_pipeline()
        outputs.append(reproduce)
        dataset = self._write_dataset_description()
        result = {"package": {key: (self.rel(value) if isinstance(value, str) else [self.rel(item) for item in value]) for key, value in package.items()}, "methods": methods_result, "reproduce_pipeline": self.rel(reproduce),
                  "dataset_description": relpath(dataset, self.out) if dataset else None, "export_link_mode": LINK_MODE}
        return self._save("export", fp, json.loads(jobs.to_json(result)), outputs, start=start)

    def _top_pct_resolved(self, rows):
        """{participant_id: thresholds.Resolved} of the top-x% primary rule, each with the lesion's own cut.

        The cuts come from the overlap stage; a lesion without one is resolved from its map.
        """
        from . import thresholds

        rule = self.config.rule
        n, v = len(self.subject_ids), self.bundle.n_voxels
        cuts = (self.results.get("overlap") or {}).get("primary_cuts") or {}
        column = "T" if rule.get("statistic") == "T" else "AvgFz"
        out = {}
        for row in rows:
            cut = cuts.get(row["participant_id"])
            if cut is not None:
                # what thresholds.resolve gives for the map's values, without the fraction of passing voxels
                out[row["participant_id"]] = dataclasses.replace(thresholds.resolve(rule, n, v), value=float(cut), banner=None, allowed=True, fraction=None)
            else:
                self.cancel.raise_if_cancelled()
                out[row["participant_id"]] = thresholds.resolve(rule, n, v, values=self._load_vector(row["maps"][column]))
        return out

    def _write_reproduce_pipeline(self):
        from .export import DISCLAIMER

        pythonslicer = shutil.which("PythonSlicer") or "PythonSlicer"
        path = self.path(REPRODUCE_PIPELINE)
        lines = [
            "#!/bin/sh",
            "# Re-run this lesion network mapping pipeline from its configuration (generated by Slicer Studio's LesionNetworkMappingLib).",
            "# Usage: ACCEPT_TERMS=1 [PYTHONSLICER=/path/to/PythonSlicer] [OUT=output_folder] sh reproduce_pipeline.sh",
            "# The connectome's data use terms must be accepted explicitly: ACCEPT_TERMS=1 adds --accept-terms (without it the run stops with exit code 4).",
            "# The study, the connectome bundle and the configuration are read through pipeline.json, next to this script.",
            "# Windows (cmd.exe), from this folder:",
            '#   set PYTHONPATH=<LesionNetworkMapping module folder> && "C:\\path\\to\\PythonSlicer.exe" -m LesionNetworkMappingLib.pipeline run --config pipeline.json --out reproduced --recompute --accept-terms',
            f"# {DISCLAIMER}",
            "set -e",
            'HERE="$(cd "$(dirname "$0")" && pwd)"',
            f"DEFAULT_PYTHONSLICER={shlex.quote(pythonslicer)}",
            'PYTHONSLICER="${PYTHONSLICER:-$DEFAULT_PYTHONSLICER}"',
            'OUT="${OUT:-reproduced}"',
            f"DEFAULT_LNM_LIBRARY_DIR={shlex.quote(LIB_PARENT)}",
            'LNM_LIBRARY_DIR="${LNM_LIBRARY_DIR:-$DEFAULT_LNM_LIBRARY_DIR}"',
            'PYTHONPATH="$LNM_LIBRARY_DIR${PYTHONPATH:+:$PYTHONPATH}"',
            "export PYTHONPATH",
            '"$PYTHONSLICER" -m LesionNetworkMappingLib.pipeline run --config "$HERE/pipeline.json" --out "$OUT" --recompute ${ACCEPT_TERMS:+--accept-terms}',
            "",
        ]
        self.current_outputs.append(path)
        _write_text_atomic(path, "\n".join(lines), mode=0o755)
        return path

    def _write_dataset_description(self):
        from .export import DISCLAIMER, LIBRARY_VERSION

        path = os.path.join(self.study.path, "derivatives", "lnm", DATASET_DESCRIPTION)
        if os.path.exists(path):
            return path
        content = {"Name": "Lesion network mapping", "BIDSVersion": "1.9.0", "DatasetType": "derivative", "GeneratedBy": [{"Name": GENERATED_BY, "Version": LIBRARY_VERSION, "Description": DISCLAIMER}]}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        jobs.write_json_atomic(path, content)
        return path

    def _stage_report(self, planned):
        if not self.config.report:
            return self._skip("report", "--no-report")
        fp, reused = self._cached("report", {"atlas": self.config.atlas, "template": self.config.template, "convention": self.config.figure_convention}, ("export",))
        if reused:
            return reused
        start = _now_iso()
        try:
            report = importlib.import_module(f"{PACKAGE}.report")
            path = report.render_report(self.out, self.emit, self.cancel, atlas=self.config.atlas, atlas_labels=self.config.atlas_labels, template=self.config.template, convention=self.config.figure_convention)
        except jobs.Cancelled:
            raise
        except Exception as exc:  # a report failure never invalidates the data outputs
            self.warn(f"The report could not be rendered ({type(exc).__name__}: {exc}); the data outputs are complete.", code="report_failed")
            return {"status": "failed", "reason": f"{type(exc).__name__}: {exc}", "fingerprint": fp}
        return self._save("report", fp, {"report": self.rel(path) if path else None}, [path] if path else [], start=start)

    # Finish

    def _finish(self):
        from .study import Study

        state_path = self._state_path("finish")
        state = _read_json(state_path) or {}
        posted = dict(state.get("posted") or {})
        fresh = Study.open(self.study.path)
        current = {row["participant_id"]: row for row in fresh.subjects}
        pending = set()  # subjects already posted as mapped by a pipeline run and not merged yet
        for path in glob.glob(os.path.join(self.study.path, ".studio", "inbox", "*-pipeline.json")):
            content = _read_json(path) or {}
            pending |= {sid for sid, item in (content.get("rows") or {}).items() if isinstance(item, dict) and item.get("status") == "mapped"}
        values = {}
        for row in self.mapped_rows:
            sid = row["participant_id"]
            if (current.get(sid) or {}).get("status") == "traced" and posted.get(sid) != row["sha256"] and sid not in pending:
                values[sid] = {"status": "mapped"}
        inbox = None
        if values:
            audit = {"event": "pipeline", "run_id": self.run_id, "subjects": sorted(values), "at": _now_iso()}
            inbox = Study.post_results(self.study.path, values, "pipeline", audit=audit)
            for sid in values:
                posted[sid] = next(row["sha256"] for row in self.mapped_rows if row["participant_id"] == sid)
            jobs.write_json_atomic(state_path, {"posted": posted})
        status = "done_with_failures" if self.failed else "done"
        self.stage_records["finish"] = {"name": "finish", "label": "Finish", "status": "done", "provider": None, "start": _now_iso(), "end": _now_iso(), "reused": False, "outputs": {},
                                        "inbox": relpath(inbox, self.study.path) if inbox else None}
        self.stage_event("finish", "done")
        self._write_pipeline_json(status)
        computed = sum(1 for row in self.mapped_rows if ((self.results.get("maps") or {}).get("maps") or {}).get(row["participant_id"], {}).get("cache") == "new")
        maps_result = self.results.get("maps") or {}
        if maps_result.get("provider") == "engine":
            computed = maps_result.get("computed") or 0
        if self.stage_records.get("maps", {}).get("reused"):
            computed = 0
        reused = len(self.mapped_rows) - computed
        self._update_run(status, summary={"mapped": len(self.mapped_rows), "computed": computed, "reused": reused, "failed_stages": self.failed})
        self.emit("done", out=self.out, report=self.path("report.html") if os.path.isfile(self.path("report.html")) else None, pipeline_json=self.path(PIPELINE_JSON), computed=computed, reused=reused,
                  excluded=[{"participant_id": row["participant_id"], "reason": row["reason"]} for row in self.rows if row["status"] == "excluded"], failed_stages=self.failed,
                  elapsed=round(time.monotonic() - self.started, 3), stages={name: record.get("status") for name, record in self.stage_records.items()}, warnings=len(self.warnings), run_id=self.run_id,
                  inbox=inbox, status=status)
        return EXIT_OK

    def _update_run(self, status, summary=None):
        if not self.run_id:
            return
        from . import runs

        mapping = {"done": "done", "done_with_failures": "done", "cancelled": "cancelled", "error": "failed", "failed": "failed"}
        try:
            runs.update(self.study.path, self.run_id, status=mapping.get(status, status), ended=_now_iso(), summary=summary or {"status": status, "failed_stages": self.failed})
        except OSError:
            pass

    # pipeline.json

    def _write_pipeline_json(self, status, error=None):
        if not getattr(self, "plan", None):
            return
        from . import export

        config = self.config
        rows = []
        for row in self.rows:
            final = row["status"]
            if final == "compute":
                cache = (((self.results.get("maps") or {}).get("maps") or {}).get(row["participant_id"]) or {}).get("cache")
                final = "reused" if cache == "hit" or self.stage_records.get("maps", {}).get("reused") else "computed"
            elif final == "reuse":
                final = "reused"
            rows.append({"participant_id": row["participant_id"], "group": row["group"], "lesion": row.get("lesion"), "sha256": row.get("sha256"), "status": final, "reason": row.get("reason"),
                         "snapshot": self.rel(row["snapshot"]) if row.get("snapshot") and row["status"] != "excluded" else None})
        mapped = self.mapped_rows
        groups = collections.Counter(row["group"] for row in mapped)
        overlap = self.results.get("overlap") or {}
        group_outputs = []
        for name in sorted(os.listdir(self.out)) if os.path.isdir(self.out) else []:
            match = re.match(r"group_conn-[A-Za-z0-9]+_desc-([A-Za-z0-9]+)_map\.nii\.gz$", name)
            if not match:
                continue
            sidecar = _read_json(self.path(name[: -len(".nii.gz")] + ".json")) or {}
            group_outputs.append({"desc": match.group(1), "path": name, "sidecar": name[: -len(".nii.gz")] + ".json", "kind": sidecar.get("kind"), "sign": sidecar.get("sign"), "rule_tag": sidecar.get("rule_tag"),
                                  "score": sidecar.get("score")})
        if status in ("done", "done_with_failures"):
            for item in group_outputs:
                item["sha256"] = file_sha256(self.path(item["path"]))
        exclusions = {}
        for row in self.rows:
            if row["status"] == "excluded":
                exclusions.setdefault(row["reason"], []).append(row["participant_id"])
        stages = []
        for name, label in STAGES:
            record = dict(self.stage_records.get(name) or {})
            planned = next((stage for stage in self.plan.stages if stage["name"] == name), {})
            record.setdefault("name", name)
            record.setdefault("label", label)
            record.setdefault("provider", self.providers[name].module)
            record.setdefault("provider_sha256", self.providers[name].sha256)
            record.setdefault("status", "pending" if status == "running" else "not run")
            record.setdefault("reason", planned.get("reason"))
            record.setdefault("estimate_s", planned.get("estimate_s"))
            stages.append(record)
        specificity = self.results.get("specificity") or {}
        conj = self.results.get("conjunction") or {}
        export_result = self.results.get("export") or {}
        package = export_result.get("package") or {}
        content = {
            "format": FORMAT_NAME,
            "version": FORMAT_VERSION,
            "run_id": self.run_id,
            "status": status,
            "created": self.created,
            "updated": _now_iso(),
            "software": config.software or {},
            "versions": export.versions(),
            "host": {"system": __import__("platform").system(), "machine": __import__("platform").machine()},
            "study": relpath(self.study.path, self.out),
            "bundle": relpath(self.bundle.path, self.out),
            "config": config.to_json(base=self.out, analysis_only=True),
            "inputs": {
                "participants_tsv_sha256": _lesion_sha(os.path.join(self.study.path, "participants.tsv")),
                "study_json_sha256": _lesion_sha(os.path.join(self.study.path, "study.json")),
                "rows": rows,
            },
            "subjects": {"n": len(self.subject_ids), "subject_ids_sha256": self.plan.subjects_sha256, "preview": config.preview_n, "seed": config.preview_seed},
            "connectome": self._connectome_info(),
            "stages": stages,
            "thresholds": overlap.get("thresholds") or [],
            "cohort": {
                "n_mapped": len(mapped),
                "n_cases": groups.get("case", 0),
                "n_controls": groups.get("control", 0),
                "n_unassigned": sum(count for group, count in groups.items() if group not in ("case", "control")),
                "population": overlap.get("population"),
                "per_lesion_counts": overlap.get("per_lesion_counts"),
                "specificity": {"design_type": specificity.get("design_type"), "limitations": specificity.get("limitations"), "fwe": specificity.get("fwe"), "min_p": specificity.get("min_p"),
                                "fdr_floor": specificity.get("fdr_floor"), "label": specificity.get("label"), "n": specificity.get("n")} if specificity else None,
                "conjunction": {key: conj.get(key) for key in ("pos_voxels", "neg_voxels", "pos_ml", "neg_ml")} if conj else None,
                "slnm": {label: {key: value.get(key) for key in ("column", "n", "method", "covariates", "max_abs_r", "fwe_voxels", "r_with_volume", "skipped", "failed")}
                         for label, value in ((self.results.get("slnm") or {}).get("scores") or {}).items()},
                "nulls": self.results.get("nulls") or None,
                "qa": (self.results.get("qa") or {}).get("summary"),
                "lesionset": self.results.get("lesionset") or None,
            },
            "outputs": {
                "per_lesion": package.get("maps"),
                "sidecars": package.get("sidecars"),
                "group": group_outputs,
                "clusters": ((self.results.get("clusters") or {}).get("tables")),
                "provenance": package.get("provenance"),
                "methods": ((export_result.get("methods") or {}).get("files")) or (["methods.md"] if package else None),
                "checklist": (export_result.get("methods") or {}).get("checklist"),
                "reproduce": package.get("reproduce"),
                "reproduce_pipeline": export_result.get("reproduce_pipeline"),
                "report": "report.html" if os.path.isfile(self.path("report.html")) else None,
                "qa": "qa.json" if os.path.isfile(self.path("qa.json")) else None,
            },
            "battery": (export_result.get("methods") or {}).get("battery"),
            "export_link_mode": LINK_MODE,
            "overrides": self.overrides,
            "exclusions": exclusions,
            "warnings": [self._portable(item["message"]) for item in self.warnings][-200:],
            "failed_stages": self.failed,
            "disclaimer": export.DISCLAIMER,
            "note": UNIVERSAL_NOTE,
        }
        if error:
            content["error"] = error
        jobs.write_json_atomic(self.path(PIPELINE_JSON), json.loads(jobs.to_json(content)))


def _portable_paths(value, folder):
    """A copy of a JSON value with every absolute path string made relative to ``folder`` (``relpath``)."""
    if isinstance(value, dict):
        return {key: _portable_paths(item, folder) for key, item in value.items()}
    if isinstance(value, list):
        return [_portable_paths(item, folder) for item in value]
    if isinstance(value, str) and os.path.isabs(value) and len(value) > 1:
        return relpath(value, folder)
    return value


def _label_text(found):
    if not found:
        return None
    try:
        items = list(found)
    except TypeError:
        return str(found)
    texts = []
    for item in items:
        label = item.get("label") if isinstance(item, dict) else item[1] if isinstance(item, (tuple, list)) and len(item) > 1 else item
        if label:
            texts.append(str(label))
    return "; ".join(texts[:2]) or None


def run(config, emit=None, cancel=None, argv=None):
    """Run the pipeline; return the exit code (0 done or done with failed optional stages, 1, 3, 4, 5, 6 or 75).

    :param config: :class:`PipelineConfig`.
    :param emit: an :class:`Emitter` or a callable ``emit(event, **fields)`` (default: JSON lines or text on standard output).
    :param cancel: optional path of an extra cancel file (``--cancel-file``).
    """
    runner = Runner(config, emit if emit is not None else Emitter(config.events), cancel, argv)
    try:
        return runner.run()
    except PipelineError as exc:
        runner.emit("error", code=exc.code, message=exc.message, **exc.fields)
        return exc.exit_code


# status


def status(out):
    """{"status": never | running | done | done_with_failures | cancelled | error, ...} of an output folder."""
    out = os.path.abspath(os.fspath(out))
    data = _read_json(os.path.join(out, PIPELINE_JSON)) or {}
    lock_path = os.path.join(out, PRIVATE_DIR, LOCK_NAME)
    progress = _read_json(os.path.join(out, PRIVATE_DIR, PROGRESS_JSON)) or {}
    lock = jobs.OutputLock(lock_path)
    holder = lock.read()
    if os.path.exists(lock_path) and not lock.is_stale(holder):
        age = time.time() - os.stat(lock_path).st_mtime
        return {"status": "running", "pid": (holder or {}).get("pid"), "host": (holder or {}).get("host"), "stage": progress.get("stage"), "index": progress.get("index"), "total": progress.get("total"),
                "percent": progress.get("percent"), "eta": progress.get("eta"), "message": progress.get("message"), "heartbeat_age_s": round(age, 1), "run_id": progress.get("run_id") or data.get("run_id")}
    if not data:
        return {"status": "never"}
    state = data.get("status")
    if state == "running":
        state = "error"  # the process that wrote it is gone
    return {"status": state, "run_id": data.get("run_id"), "updated": data.get("updated"), "failed_stages": data.get("failed_stages"), "stages": {stage.get("name"): stage.get("status") for stage in data.get("stages") or []}}


# init


def _session_live(study_path):
    """The heartbeat of a GUI session that has the study open (.studio/session.json, younger than 60 s), or None."""
    path = os.path.join(study_path, ".studio", "session.json")
    data = _read_json(path)
    if not isinstance(data, dict):
        return None
    beat = data.get("heartbeat_epoch")
    if beat is None:
        try:
            beat = os.stat(path).st_mtime
        except OSError:
            return None
    if time.time() - float(beat) > SESSION_STALE_S:
        return None
    if data.get("host") == socket.gethostname() and data.get("pid") and not jobs.pid_alive(data.get("pid")):
        return None
    return data


def _read_table(path):
    """(columns, rows) of a TSV or CSV file (tab for .tsv, else the separator sniffed from the header line)."""
    import csv

    with open(path, encoding="utf-8-sig", newline="") as f:
        text = f.read()
    if path.lower().endswith(".tsv"):
        delimiter = "\t"
    else:
        # a .csv may use ";" (spreadsheets with a decimal comma): the most frequent separator of the header line
        header = next((line for line in text.splitlines() if line.strip()), "")
        delimiter = max((",", ";", "\t"), key=header.count) if any(char in header for char in ",;\t") else ","
    reader = csv.reader(text.splitlines(), delimiter=delimiter)
    lines = [line for line in reader if any(cell.strip() for cell in line)]
    if not lines:
        raise ValueError(f"{path} is empty")
    columns = [column.strip() for column in lines[0]]
    rows = [{column: (line[index].strip() if index < len(line) else "") for index, column in enumerate(columns)} for line in lines[1:]]
    return columns, rows


def _group_map(text):
    mapping = {}
    for item in (text or "").split(","):
        if "=" in item:
            key, value = item.split("=", 1)
            mapping[key.strip().lower()] = value.strip()
    return mapping


def init_study(source, study_path, participants=None, name=None, score_columns=(), group_column=None, group_map=None, lesion_rule=None, id_regex=None):
    """Create (or extend) a study from a folder of lesion files and an optional spreadsheet; return a summary dict.

    Files are referenced in place (never moved or modified). The study is written only through ``Study.save``
    (lock and merge). :raises jobs.Locked: when a GUI session has the study open (heartbeat younger than 60 s).
    """
    import tempfile

    from .study import NA, Study, write_tsv

    study_path = os.path.abspath(os.fspath(study_path))
    if Study.is_study(study_path):
        live = _session_live(study_path)
        if live:
            raise jobs.Locked(f"study open in Slicer (process {live.get('pid')} on {live.get('host')})", holder=live)
        study = Study.open(study_path)
    else:
        study = Study.create(study_path, name=name)
    report = study.add_files([source], lesion_rule=lesion_rule, id_regex=id_regex)
    imported = None
    if participants:
        columns, rows = _read_table(participants)
        id_column = "participant_id" if "participant_id" in columns else columns[0]
        mapping = _group_map(group_map)
        out_rows = []
        for row in rows:
            values = {column: (row[column] if row[column] != "" else NA) for column in columns if column != id_column}
            values["participant_id"] = row[id_column]
            source_group = group_column or "group"
            if source_group in row:
                raw = row[source_group]
                values["group"] = mapping.get(raw.strip().lower(), raw if raw in ("case", "control") else NA) if (mapping or source_group != "group") else (raw or NA)
            out_rows.append(values)
        out_columns = ["participant_id"] + [column for column in columns if column != id_column]
        if "group" not in out_columns and any("group" in row for row in out_rows):
            out_columns.append("group")
        with tempfile.TemporaryDirectory() as folder:
            tsv = os.path.join(folder, "participants.tsv")
            write_tsv(tsv, out_columns, [{column: row.get(column, NA) for column in out_columns} for row in out_rows])
            imported = study.import_tsv(tsv)
    roles = {}
    for column in score_columns or ():
        study.set_column_role(column, "score")
        roles[column] = "score"
    if group_column and group_column != "group" and group_column in study.columns:
        study.set_column_role(group_column, "group")
        roles[group_column] = "group"
    study.save()
    summary = {
        "study": study.path,
        "subjects": len(study),
        "lesions_found": sum(1 for pair in report["pairs"].values() if pair.get("lesion")),
        "scans_found": sum(1 for pair in report["pairs"].values() if pair.get("scan")),
        "unmatched_files": [item["path"] for item in report["skipped"]],
        "added": report["added"],
        "imported": imported,
        "columns": {column: study.column_spec(column).role for column in study.columns if column not in ("participant_id",)},
        "roles_confirmed": roles,
        "warnings": report["warnings"],
    }
    return summary


# Command line


def _add_run_arguments(parser):
    S = argparse.SUPPRESS
    parser.add_argument("--study", default=S, help="study folder (study.json, participants.tsv)")
    parser.add_argument("--bundle", default=S, help="connectome bundle (.lnmc)")
    parser.add_argument("--out", default=S, help="output folder (default: <study>/derivatives/lnm/conn-<label>[_n-<N>])")
    parser.add_argument("--config", default=None, help="pipeline configuration JSON (or a pipeline.json); command-line flags override it")
    parser.add_argument("--preview", dest="preview_n", type=int, default=S, help="preview run on N randomly chosen connectome subjects")
    parser.add_argument("--preview-seed", dest="preview_seed", type=int, default=S, help="seed of the preview subjects (default 0)")
    parser.add_argument("--rule", default=S, help="primary threshold rule as JSON (default FWE alpha 0.05)")
    parser.add_argument("--extra-rule", dest="extra_rules", action="append", default=S, help="secondary threshold rule as JSON (repeatable; secondary results only)")
    parser.add_argument("--allow-rule-anyway", action="store_true", default=S, help="apply a rule that is refused for this N (recorded)")
    parser.add_argument("--sensitivity", type=float, default=S, help="sensitivity map: percent of the maps (50-100; default 90)")
    parser.add_argument("--sensitivity-population", choices=POPULATIONS, default=S, help="auto (cases when any), cases or all")
    parser.add_argument("--specificity", choices=SPECIFICITY_MODES, default=S, help="auto (GLM when both groups have 3+ subjects), glm, off (legacy routes: unavailable)")
    parser.add_argument("--input", choices=INPUTS, default=S, help="per-lesion maps of the GLM and sLNM: AvgFz (default) or T")
    parser.add_argument("--perm", type=int, default=S, help=f"permutations (at least {MIN_PERM}; default {DEFAULT_PERM})")
    parser.add_argument("--perm-seed", type=int, default=S, help="permutation seed (default 0)")
    parser.add_argument("--alpha", type=float, default=S, help="FWE alpha (default 0.05)")
    parser.add_argument("--no-fdr", dest="fdr", action="store_false", default=S, help="do not write the FDR maps")
    parser.add_argument("--tfce", action="store_true", default=S, help="add TFCE to the GLM (slow)")
    parser.add_argument("--no-volume-covariate", dest="volume_off_reason", default=S, metavar="REASON", help="leave lesion volume out of the covariates (the reason is recorded)")
    parser.add_argument("--covariates", default=S, help="comma-separated covariate columns (default volume_ml)")
    parser.add_argument("--scores", default=S, help="auto (confirmed score columns), none, or a comma-separated list")
    parser.add_argument("--slnm-method", choices=("pearson", "spearman"), default=S)
    parser.add_argument("--min-slnm-n", type=int, default=S, help=f"subjects with a score needed for sLNM (default {MIN_SLNM_N})")
    parser.add_argument("--null-relocations", default=S, help="per-lesion relocation nulls: auto (50 / 10 / 0 by cohort size) or K")
    parser.add_argument("--null-seed", type=int, default=S)
    parser.add_argument("--nulls", choices=NULL_MODES, default=S, help="cohort null models: auto, exact, fast or off")
    parser.add_argument("--nulls-budget-min", type=float, default=S, help="auto runs the exact pooled null only below this estimate (default 30)")
    parser.add_argument("--reference", choices=("auto", "off"), default=S)
    parser.add_argument("--build-references", action="store_true", default=S, help="build missing reference maps (about 15-25 min on GSP1000)")
    parser.add_argument("--native", choices=NATIVE_MODES, default=S, help="native-space lesions: normalize (default), skip or include-unreviewed")
    parser.add_argument("--include-unreviewed", action="store_true", default=S, help="map lesions that review has not approved (recorded)")
    parser.add_argument("--strict", action="store_true", default=S, help="per-subject blocks block the whole run")
    parser.add_argument("--memory-budget-gb", type=float, default=S)
    parser.add_argument("--template", default=S)
    parser.add_argument("--atlas", default=S, help="label atlas NIfTI for the cluster tables (default: the cached catalog atlases; never downloads)")
    parser.add_argument("--atlas-labels", default=S)
    parser.add_argument("--figure-convention", default=S)
    parser.add_argument("--no-report", dest="report", action="store_false", default=S)
    parser.add_argument("--recompute", action="store_true", default=S, help="ignore every saved stage and the map cache")
    parser.add_argument("--accept-terms", action="store_true", default=S, help="accept the data use terms of the connectome")
    parser.add_argument("--software", default=S, help="JSON object recorded as the exporting software")
    parser.add_argument("--parent-pid", type=int, default=S, help="stop when this process is gone")
    parser.add_argument("--nice", type=int, default=S)
    parser.add_argument("--keep-awake", choices=KEEP_AWAKE_MODES, default=S)
    parser.add_argument("--slicer", default=S, help="Slicer executable for stage modules that need the application")
    parser.add_argument("--events", choices=EVENT_MODES, default=S, help="auto (text on a terminal), json or text")
    parser.add_argument("--json", dest="plan_json", action="store_true", default=False, help="plan: print the plan as JSON")
    jobs.add_common_arguments(parser, threads_default=None)


def _parser():
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.pipeline", description="Automated lesion network mapping cohort pipeline.")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=jobs.JsonArgumentParser)
    init = commands.add_parser("init", help="create a study from a folder of lesions and a spreadsheet")
    init.add_argument("--from", dest="source", required=True, help="folder of lesion (and scan) NIfTI files")
    init.add_argument("--participants", default=None, help="participants table (TSV or CSV; participant_id first or named)")
    init.add_argument("--study", required=True, help="study folder to create (or extend)")
    init.add_argument("--name", default=None)
    init.add_argument("--score-columns", default="", help="comma-separated columns confirmed as scores")
    init.add_argument("--group-column", default=None, help="column holding the group (mapped to case/control)")
    init.add_argument("--group-map", default=None, help="value mapping, e.g. 'patient=case,HC=control,1=case,0=control'")
    init.add_argument("--lesion-rule", default=None)
    init.add_argument("--id-regex", default=None)
    for name in ("plan", "run"):
        _add_run_arguments(commands.add_parser(name, help=f"{name} the pipeline"))
    st = commands.add_parser("status", help="state of an output folder")
    st.add_argument("--out", required=True)
    st.add_argument("--json", action="store_true")
    rep = commands.add_parser("report", help="render report.html again (no computation)")
    rep.add_argument("--out", required=True)
    for option in ("--atlas", "--atlas-labels", "--template", "--figure-convention"):
        rep.add_argument(option, default=None)
    return parser


_CONFIG_PARSE = {
    "rule": lambda text: json.loads(text) if isinstance(text, str) else text,
    "extra_rules": lambda items: [json.loads(item) if isinstance(item, str) else item for item in items],
    "software": lambda text: json.loads(text) if isinstance(text, str) else text,
    "covariates": lambda text: [name.strip() for name in text.split(",") if name.strip()] if isinstance(text, str) else text,
}


def config_from_args(args):
    """A :class:`PipelineConfig` from --config (if any) and the flags given on the command line (which win)."""
    names = {field.name for field in dataclasses.fields(PipelineConfig)}
    base = {}
    if args.config:
        base = PipelineConfig.load(args.config).to_json()
    for key, value in vars(args).items():
        if key in names and key not in ("threads",):
            base[key] = _CONFIG_PARSE.get(key, lambda v: v)(value)
    if getattr(args, "threads", None):
        base["threads"] = args.threads
    if base.get("volume_off_reason") and "covariates" not in vars(args) and "volume_ml" in (base.get("covariates") or ["volume_ml"]):
        base["covariates"] = [name for name in (base.get("covariates") or ["volume_ml"]) if name != "volume_ml"]
    for key in ("study", "bundle", "out", "atlas", "atlas_labels"):
        if isinstance(base.get(key), str) and base[key]:
            base[key] = os.path.abspath(base[key])
    return PipelineConfig.from_json(base)


def _print_plan(result, as_json):
    data = result.to_json(portable=False)
    if as_json or not sys.stdout.isatty():
        sys.stdout.write(json.dumps(data, ensure_ascii=True) + "\n")
        return
    lines = [f"Plan for {result.study.name} with {result.bundle.name} (N = {result.n_subjects}); output {result.out}", ""]
    lines.append(f"{'participant':<16}{'group':<10}{'status':<11}reason")
    for row in result.rows:
        lines.append(f"{row['participant_id']:<16}{row['group']:<10}{row['status']:<11}{row['reason'] or ''}")
    lines += ["", f"{'stage':<32}{'provider':<14}{'status':<13}{'estimate':>10}  reason"]
    for stage in result.stages:
        lines.append(f"{stage['label']:<32}{stage['provider'] or '-':<14}{stage['status']:<13}{_duration_text(stage['estimate_s']) if stage['estimate_s'] else '-':>10}  {stage['reason'] or ''}")
    lines.append(f"\nEstimated time: {_duration_text(result.estimate_s)}")
    for item in result.blocks:
        lines.append(f"BLOCKED: {item['message']} (fix: {item.get('fix')})")
    for item in result.warnings:
        lines.append(f"warning: {item['message']}")
    for item in result.infos:
        lines.append(f"info: {item['message']}")
    sys.stdout.write("\n".join(lines) + "\n")


def _main(argv):
    args = _parser().parse_args(argv)
    if args.command == "status":
        info = status(args.out)
        if args.json or not sys.stdout.isatty():
            sys.stdout.write(json.dumps(info, ensure_ascii=True) + "\n")
        else:
            extra = f" (pid {info.get('pid')}, {info.get('stage')}, {info.get('percent')}%, ETA {_duration_text(info.get('eta'))}, heartbeat {info.get('heartbeat_age_s')} s ago)" if info["status"] == "running" else ""
            sys.stdout.write(f"{info['status']}{extra}\n")
        return EXIT_OK
    if args.command == "report":
        if importlib.util.find_spec(f"{PACKAGE}.report") is None:
            jobs.emit("error", code="unavailable", message="report not available in this build")
            return EXIT_ERROR
        report = importlib.import_module(f"{PACKAGE}.report")
        path = report.render_report(os.path.abspath(args.out), jobs.emit, None, atlas=args.atlas, atlas_labels=args.atlas_labels, template=args.template, convention=args.figure_convention)
        jobs.emit("done", outputs=[path] if path else [], elapsed=round(jobs.elapsed(), 3))
        return EXIT_OK
    if args.command == "init":
        try:
            summary = init_study(args.source, args.study, args.participants, args.name, [name.strip() for name in args.score_columns.split(",") if name.strip()], args.group_column, args.group_map,
                                 args.lesion_rule, args.id_regex)
        except jobs.Locked as exc:
            jobs.emit("error", code="locked", message=exc.message)
            return EXIT_LOCKED
        except (OSError, ValueError, KeyError) as exc:
            jobs.emit("error", code="input", message=f"{type(exc).__name__}: {exc}")
            return EXIT_ERROR
        jobs.emit("result", kind="init", **summary)
        jobs.emit("done", outputs=[os.path.join(summary["study"], "participants.tsv")], elapsed=round(jobs.elapsed(), 3))
        return EXIT_OK
    try:
        config = config_from_args(args)
    except (OSError, ValueError, TypeError) as exc:
        jobs.emit("error", code="usage", message=f"Invalid configuration: {exc}")
        return EXIT_USAGE
    if args.command == "plan":
        try:
            result = plan(config)
        except (OSError, ValueError) as exc:
            jobs.emit("error", code="input", message=f"{type(exc).__name__}: {exc}")
            return EXIT_ERROR
        _print_plan(result, args.plan_json)
        return result.exit_code
    return run(config, argv=argv, cancel=args.cancel_file)


def main(argv=None):
    """Command-line entry point; returns the exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--":
        argv = argv[1:]
    # The --parent-pid watchdog of a run is started by the run itself (it creates the STOP file for the children)
    return jobs.run_job(_main, argv, job="pipeline", exit=False)


def slicer_main(argv):
    """Run a pipeline command inside the Slicer application, then exit the application with its code."""
    code = EXIT_ERROR
    try:
        code = main(list(argv))
    finally:
        import slicer

        slicer.app.exit(code)
    return code


if __name__ == "__main__":
    sys.exit(main())

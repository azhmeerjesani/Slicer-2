"""Analysis recipes, the analysis-plan lock and its deviations log (standard library only; no Slicer imports).

A *recipe* is a named, cited and fully visible analysis pipeline (SPEC_LNM_ADDENDUM §3.2, §3.4, §3.11, F23):
the primary threshold rule, the overlap (sensitivity) level, the group statistic, the design defaults, the
null models, the number of permutations, the validation scheme and the robustness set, plus the ordered
steps it runs and the library modules those steps need. No recipe is labelled "recommended": a recipe is
a disclosed set of choices, each with its source. A recipe adapted from a publication lists every
difference from it (``differences_from_source``), shown as "adapted from <source>".

Recipe JSON (format "slicer-lnm-recipe", version 1)::

    {"format": "slicer-lnm-recipe", "version": 1, "id": "classic_ferguson2019_adapted", "recipe_version": "1",
     "title": "...", "status": "established" | "adapted" | "2026-preprint" | "lab",
     "cited_as": "Ferguson et al. 2019", "citations": ["10.1038/..."], "differences_from_source": ["..."],
     "description": "...", "parameters": {...}, "steps": ["maps", "overlap", ...], "requires": ["cohortmaps", ...],
     "based_on": {"id", "recipe_version", "sha256"} (lab recipes)}

Built-in recipes live in ``recipes/*.json`` next to this file; a study's own recipes in ``<study>/recipes``;
lab recipes in the folders of the fork-only setting ``Studio/LNM/RecipeFolders`` (the caller passes them,
this module never reads settings). :func:`load_recipes` reads all three; :func:`available_steps` tells
which steps this build can run (``importlib.util.find_spec`` of their modules): a missing step is shown
"not available in this build" (a partial-recipe badge) and is never skipped silently.

:func:`apply` gives a recipe's parameters (defaults filled in); :func:`diff` lists the parameters that
differ from a recipe ("Custom (based on X)", :func:`custom_label`); :func:`save_lab_recipe` writes the
current parameters as a shareable lab recipe (no absolute paths).

**Plan lock** (pre-registration-lite): :func:`lock_plan` writes ``<study>/derivatives/lnm/analysis_plan.json``
with the recipe, the parameters and the hypotheses (primary contrast or score, a-priori ROI masks with their
sha256, primary rule, alpha, n_perm, seeds) and the plan's own sha256 (of its canonical JSON without the
``sha256`` field). After the lock, every parameter change is appended with its reason to
``analysis_plan_deviations.jsonl`` (:func:`record_deviation`), which the methods text reports (methods.py,
"Deviations from the analysis plan"). :func:`plan_status` says whether the plan is open, locked or modified
after the lock (its sha256 no longer matches). Paths in the plan are relative to the study (SPEC 9.7).
The analyses of the Map Cohort card (the Contrast GLM and the Symptom sLNM) hold their own permutations,
covariates and inference options: :func:`analysis_pending` compares them with the locked plan apart from the
threshold rule and Run everything's options, and their deviations are recorded as "<analysis>.<field>" (for
example "contrast.n_perm"), so a deviation of one never changes what the other follows.

Pipeline (A-04): :func:`to_pipeline_config` turns a recipe into ``pipeline.PipelineConfig`` fields and a
recipe record (with the locked plan's sha256) for pipeline.json.

Command line::

    python -m LesionNetworkMappingLib.recipes list [--study S] [--folder F ...] [--json]
    python -m LesionNetworkMappingLib.recipes show NAME [--study S] [--folder F ...]
    python -m LesionNetworkMappingLib.recipes config NAME --study S [--out config.json]   (pipeline run --config)
    python -m LesionNetworkMappingLib.recipes status --study S [--json]
    python -m LesionNetworkMappingLib.recipes validate FILE [FILE ...]
"""

import copy
import datetime
import glob
import hashlib
import importlib.util
import json
import logging
import math
import os
import re
import sys

if not __package__:  # run as a script
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "LesionNetworkMappingLib"  # noqa: A001

PACKAGE = __package__ or "LesionNetworkMappingLib"

FORMAT_NAME = "slicer-lnm-recipe"
FORMAT_VERSION = 1
PLAN_FORMAT = "slicer-lnm-analysis-plan"
PLAN_VERSION = 1
SELECTION_FORMAT = "slicer-lnm-recipe-selection"
BUILTIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recipes")
#: Folder of a study's own recipes
STUDY_RECIPES_DIR = "recipes"
#: Fork-only setting (SPEC Appendix B, D-S1) holding extra recipe folders (a list); read by the user interface
RECIPE_FOLDERS_SETTING = "Studio/LNM/RecipeFolders"
PLAN_JSON = ("derivatives", "lnm", "analysis_plan.json")
DEVIATIONS_JSONL = ("derivatives", "lnm", "analysis_plan_deviations.jsonl")
SELECTION_JSON = ("derivatives", "lnm", "recipe.json")

#: Statuses of built-in recipes; "lab" is the status of a recipe saved by a lab (:func:`save_lab_recipe`)
STATUSES = ("established", "adapted", "2026-preprint")
LAB_STATUS = "lab"
ALL_STATUSES = (*STATUSES, LAB_STATUS)
#: Words no recipe title or description may use (a recipe is a disclosed choice, never an endorsement)
FORBIDDEN_WORDS = ("recommended",)
NOT_AVAILABLE = "not available in this build"
CUSTOM_PREFIX = "Custom (based on {})"

#: Steps a recipe may name, in pipeline order: (label, library modules needed, pipeline stage or None).
#: A step without a pipeline stage runs from its Map card (and is listed by :func:`to_pipeline_config`).
STEPS = {
    "coordinates": ("Coordinate seeds", ("coords",), None),
    "maps": ("Network maps", ("cohortmaps",), "maps"),
    "overlap": ("Overlap and sensitivity", ("cohort",), "overlap"),
    "nulls": ("Null models", ("nulls",), "nulls"),
    "specificity": ("Specificity (permutation GLM)", ("glm", "design"), "specificity"),
    "slnm": ("Symptom network maps (sLNM)", ("slnm",), "slnm"),
    "roi": ("A-priori ROI inference", ("roi", "glm"), None),
    "conjunction": ("Conjunction", ("cohort",), "conjunction"),
    "qa": ("Connectome QA (degree / PC1)", ("qa",), "qa"),
    "validation": ("Validation against baselines", ("validate",), None),
    "robustness": ("Robustness sweep", ("robustness",), None),
    "clusters": ("Clusters and peaks", ("clusters",), "clusters"),
    "report": ("Methods and report", ("methods", "report"), "report"),
}
#: Where a step without a pipeline stage is run
MANUAL_STEP_PLACES = {
    "coordinates": "Map ▸ Seed ▸ Coordinates",
    "roi": "Map ▸ Recipe (ROI inference) or python -m LesionNetworkMappingLib.roi",
    "validation": "Map ▸ Validate",
    "robustness": "Map ▸ Robust",
}

TESTS = ("two_group", "anova", "continuous", "one_sample")
NULL_MODES = ("off", "auto", "exact", "fast")
NULL_KINDS = ("rl_independent", "rl_cohort_shift", "rl_strength", "rl_overlap", "empirical_pool")
VALIDATION_SCHEMES = ("none", "auto", "lodo", "kfold", "loo")
STATISTICS = ("Fz", "T")
#: Specificity tests (pipeline --specificity; Map's "auto" option is the permutation GLM)
SPECIFICITY_METHODS = ("glm", "legacy-welch", "legacy-liebermeister")
#: Subjects outside template space (pipeline --native)
NATIVE_MODES = ("normalize", "skip", "include-unreviewed")
MIN_N_PERM = 1000

#: Parameters of every recipe; a recipe states only what differs (:func:`apply` fills the rest)
DEFAULT_PARAMETERS = {
    "rule": {"kind": "fwe_bonferroni", "alpha": 0.05},
    "extra_rules": [],
    "overlap": 90.0,
    "statistic": "Fz",
    "design": {"test": "two_group", "method": "glm", "contrast": "A>B", "covariates": ["volume_ml"], "permute": "auto", "tfce": False, "volume_off_reason": None},
    "nulls": {"mode": "off", "kinds": [], "k": 1000, "relocations": "auto"},
    "n_perm": 5000,
    "alpha": 0.05,
    "fdr": True,
    "validation": {"scheme": "none"},
    "robustness": {"overlaps": [50, 75, 90, 95, 100], "t_values": [5, 7, 9, 12], "top_pct": [5, 10, 25], "second_connectome": False},
    "preview_n": None,
    "seeds": {"permutation": 0, "nulls": 0, "preview": 0},
    "native": "normalize",
}
#: Readable names of the parameters (deviation descriptions and the user interface)
FIELD_LABELS = {
    "rule": "primary threshold rule",
    "extra_rules": "secondary threshold rules",
    "overlap": "overlap level (%)",
    "statistic": "group statistic",
    "design.test": "test",
    "design.method": "specificity test",
    "design.contrast": "contrast",
    "design.covariates": "covariates",
    "design.permute": "permutation scheme",
    "design.tfce": "TFCE",
    "design.volume_off_reason": "reason volume is not a covariate",
    "nulls.mode": "null models",
    "nulls.kinds": "null kinds",
    "nulls.k": "null cohorts",
    "nulls.relocations": "per-lesion null relocations",
    "n_perm": "n_perm",
    "alpha": "alpha",
    "fdr": "FDR maps",
    "validation.scheme": "validation scheme",
    "robustness.overlaps": "robustness overlaps",
    "robustness.t_values": "robustness |T| values",
    "robustness.top_pct": "robustness top-x%",
    "robustness.second_connectome": "second connectome",
    "preview_n": "preview subjects",
    "native": "subjects in native space",
    "seeds.permutation": "permutation seed",
    "seeds.nulls": "null seed",
    "seeds.preview": "preview subset seed",
    "steps": "steps",
}
#: Leaves of the parameters that are compared as whole values (the rule dicts and lists)
#: Parameters that are objects of named values (their keys are checked)
_SECTIONS = ("design", "nulls", "validation", "robustness", "seeds")
_WHOLE = ("rule", "extra_rules", "design.covariates", "nulls.kinds", "robustness.overlaps", "robustness.t_values", "robustness.top_pct")
_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,79}$")
_DOI = re.compile(r"^10\.\d{4,9}/\S+$")


class RecipeError(ValueError):
    """An invalid recipe, or a plan operation that is not allowed."""


# Canonical JSON and hashing


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_of(obj):
    """Hex sha256 of the canonical JSON of ``obj`` (sorted keys, no spaces, UTF-8)."""
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


def file_sha256(path, block=1 << 20):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(block):
            digest.update(chunk)
    return digest.hexdigest()


def _now_iso():
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _write_json_atomic(path, obj):
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    os.replace(tmp, path)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _study_path(study):
    """The folder of a study (a ``study.Study``, or a path)."""
    path = getattr(study, "path", study)
    if not path:
        raise RecipeError("A study folder is needed")
    return os.fspath(path)


# Parameters


def _merge(base, override):
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict) and key != "rule":
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _rule(rule):
    from . import thresholds

    return thresholds.normalize_rule(rule)


def normalize_parameters(params):
    """Parameters with the defaults filled in, validated and normalized (rules through thresholds.py).

    :raises RecipeError: an invalid value.
    """
    params = {} if params is None else params
    if not isinstance(params, dict):
        raise RecipeError(f"The parameters must be an object, got {type(params).__name__}")
    for section in _SECTIONS:
        value = params.get(section)
        if value is None:
            continue
        if not isinstance(value, dict):
            raise RecipeError(f"{section} must be an object, got {type(value).__name__}")
        unknown = sorted(set(value) - set(DEFAULT_PARAMETERS[section]))
        if unknown:
            raise RecipeError(f"Unknown {section} parameter(s): {', '.join(map(str, unknown))}")
    out = _merge(DEFAULT_PARAMETERS, params)
    for field in ("extra_rules", "design.covariates", "nulls.kinds", "robustness.overlaps", "robustness.t_values", "robustness.top_pct"):
        section, _dot, key = field.rpartition(".")
        value = (out[section] if section else out).get(key)
        if value is not None and not isinstance(value, list):
            raise RecipeError(f"{field} must be a list, got {type(value).__name__}")
    try:
        out["rule"] = _rule(out["rule"])
        out["extra_rules"] = [_rule(rule) for rule in out.get("extra_rules") or []]
    except (ValueError, TypeError) as exc:
        raise RecipeError(f"Invalid threshold rule: {exc}") from None
    try:
        out["overlap"] = float(out["overlap"])
        out["n_perm"] = int(out["n_perm"])
        out["alpha"] = float(out["alpha"])
        out["nulls"]["k"] = int(out["nulls"]["k"])
        for key in ("permutation", "nulls", "preview"):
            out["seeds"][key] = int(out["seeds"].get(key) or 0)
        if out["nulls"].get("relocations") is None or str(out["nulls"]["relocations"]) == "auto":
            out["nulls"]["relocations"] = "auto"
        elif isinstance(out["nulls"]["relocations"], bool) or int(out["nulls"]["relocations"]) < 0:
            raise ValueError(f"nulls.relocations must be 'auto' or a count, got {out['nulls']['relocations']!r}")
        else:
            out["nulls"]["relocations"] = int(out["nulls"]["relocations"])
        if out.get("preview_n") is not None:
            out["preview_n"] = int(out["preview_n"])
        robustness = out["robustness"]
        for key in ("overlaps", "t_values", "top_pct"):
            robustness[key] = list(robustness.get(key) or [])
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in robustness[key]):
                raise ValueError(f"robustness.{key} must hold numbers")
        robustness["second_connectome"] = bool(robustness.get("second_connectome"))
    except (TypeError, ValueError) as exc:
        raise RecipeError(f"Invalid number in the parameters: {exc}") from None
    if not 50.0 <= out["overlap"] <= 100.0 or not math.isfinite(out["overlap"]):
        raise RecipeError(f"The overlap level must be between 50 and 100 %, got {out['overlap']:g}")
    if out["n_perm"] < MIN_N_PERM:
        raise RecipeError(f"n_perm must be at least {MIN_N_PERM}, got {out['n_perm']}")
    if not 0.0 < out["alpha"] < 1.0:
        raise RecipeError(f"alpha must be between 0 and 1, got {out['alpha']:g}")
    if out["statistic"] not in STATISTICS:
        raise RecipeError(f"The group statistic must be one of {', '.join(STATISTICS)}, got {out['statistic']!r}")
    design = out["design"]
    if design.get("test") not in TESTS:
        raise RecipeError(f"Unknown test {design.get('test')!r}; expected one of {', '.join(TESTS)}")
    if design.get("method") not in SPECIFICITY_METHODS:
        raise RecipeError(f"Unknown specificity test {design.get('method')!r}; expected one of {', '.join(SPECIFICITY_METHODS)}")
    design["covariates"] = [str(name) for name in design.get("covariates") or []]
    design["tfce"] = bool(design.get("tfce"))
    if "volume_ml" not in design["covariates"] and not (design.get("volume_off_reason") or "").strip():
        raise RecipeError("A recipe without the lesion-volume covariate must give design.volume_off_reason")
    nulls = out["nulls"]
    if nulls.get("mode") not in NULL_MODES:
        raise RecipeError(f"Unknown null mode {nulls.get('mode')!r}; expected one of {', '.join(NULL_MODES)}")
    nulls["kinds"] = [str(kind) for kind in nulls.get("kinds") or []]
    unknown = [kind for kind in nulls["kinds"] if kind not in NULL_KINDS]
    if unknown:
        raise RecipeError(f"Unknown null kind(s) {', '.join(unknown)}")
    if out["native"] not in NATIVE_MODES:
        raise RecipeError(f"Unknown native mode {out['native']!r}; expected one of {', '.join(NATIVE_MODES)}")
    if out["validation"].get("scheme") not in VALIDATION_SCHEMES:
        raise RecipeError(f"Unknown validation scheme {out['validation'].get('scheme')!r}")
    out["fdr"] = bool(out["fdr"])
    if out.get("preview_n") is not None:
        if out["preview_n"] < 1:
            raise RecipeError("preview_n must be at least 1")
    unknown_keys = sorted(set(out) - set(DEFAULT_PARAMETERS))
    if unknown_keys:
        raise RecipeError(f"Unknown parameter(s): {', '.join(unknown_keys)}")
    return out


def _flatten(params, prefix=""):
    out = {}
    for key, value in params.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict) and name not in _WHOLE:
            out.update(_flatten(value, name + "."))
        else:
            out[name] = value
    return out


def flatten_parameters(params):
    """{dotted field: value} of normalized parameters (rules and lists are single values)."""
    return _flatten(normalize_parameters(params))


def get_field(params, field):
    """The value of a dotted field of (normalized) parameters."""
    return flatten_parameters(params).get(field)


def set_field(params, field, value):
    """A copy of ``params`` with the dotted ``field`` set to ``value``."""
    out = copy.deepcopy(params)
    parts = field.split(".")
    target = out
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = copy.deepcopy(value)
    return out


def _same(a, b):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        return math.isclose(float(a), float(b), rel_tol=1e-12, abs_tol=1e-12)
    return _canonical(a) == _canonical(b)


def _field_order(field):
    keys = list(FIELD_LABELS)
    return (keys.index(field) if field in keys else len(keys), field)


def compare_parameters(old, new):
    """[{"field", "old", "new"}] of the dotted fields whose values differ between two parameter sets."""
    a, b = flatten_parameters(old), flatten_parameters(new)
    changes = [{"field": field, "old": a.get(field), "new": b.get(field)} for field in sorted(set(a) | set(b), key=_field_order) if not _same(a.get(field), b.get(field))]
    return changes


def format_value(field, value):
    """Readable text of a parameter value (rules as by :func:`rule_text`, lists comma-separated)."""
    if value is None:
        return "none"
    if field == "rule" or (isinstance(value, dict) and "kind" in value):
        return rule_text(value)
    if field == "extra_rules":
        return "; ".join(rule_text(rule) for rule in value) if value else "none"
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) if value else "none"
    return str(value)


def rule_text(rule):
    """Short text of a threshold rule: "FWE (Bonferroni) alpha 0.05" (with the Greek letter), "top 10% (Fz, both)", "|T| ≥ 7"."""
    try:
        rule = _rule(rule)
    except (ValueError, TypeError):
        return str(rule)
    kind = rule["kind"]
    if kind == "fwe_bonferroni":
        text = f"FWE (Bonferroni) α {rule['alpha']:g}"  # noqa: RUF001
    elif kind == "top_pct":
        text = f"top {rule['value']:g}% ({rule['statistic']}, {rule['sign']})"
    elif kind == "fz_abs":
        text = f"|Fz| ≥ {rule['value']:g}"
    else:
        text = f"|T| ≥ {rule['value']:g}"
    if rule.get("override"):
        text += " (kept for another N)"
    return text


def describe_change(field, old, new):
    """Text of one change: "n_perm 1000 instead of 5000"."""
    return f"{FIELD_LABELS.get(field, field)} {format_value(field, new)} instead of {format_value(field, old)}"


# Recipes


class Recipe:
    """A loaded recipe: the JSON content (``data``), where it came from (``origin``: builtin, study or folder) and its ``path``."""

    def __init__(self, data, origin="builtin", path=None):
        self.data = data
        self.origin = origin
        self.path = path

    def __getitem__(self, key):
        return self.data[key]

    def get(self, key, default=None):
        return self.data.get(key, default)

    @property
    def id(self):
        return self.data["id"]

    @property
    def title(self):
        return self.data["title"]

    @property
    def status(self):
        return self.data["status"]

    @property
    def steps(self):
        return list(self.data.get("steps") or [])

    @property
    def sha256(self):
        """sha256 of the recipe content (canonical JSON)."""
        return sha256_of(self.data)

    def parameters(self):
        return apply(self)

    def status_text(self):
        """The status tag: "established", "adapted from Ferguson et al. 2019", "2026 preprint", "lab recipe (based on …)"."""
        return status_text(self.data)

    def record(self):
        """{"id", "recipe_version", "title", "status", "sha256", "origin"} for plans and pipeline.json."""
        out = {"id": self.id, "recipe_version": str(self.data.get("recipe_version") or "1"), "title": self.title, "status": self.status, "sha256": self.sha256, "origin": self.origin}
        if self.data.get("based_on"):
            out["based_on"] = dict(self.data["based_on"])
        return out

    def __repr__(self):
        return f"Recipe({self.id!r}, origin={self.origin!r})"


def status_text(data):
    status = data.get("status")
    if status == "adapted":
        return f"adapted from {data.get('cited_as') or 'the cited source'}"
    if status == "2026-preprint":
        return f"2026 preprint ({data['cited_as']})" if data.get("cited_as") else "2026 preprint"
    if status == LAB_STATUS:
        based = (data.get("based_on") or {}).get("title") or (data.get("based_on") or {}).get("id")
        return f"lab recipe (based on {based})" if based else "lab recipe"
    return f"established ({data['cited_as']})" if data.get("cited_as") else "established"


def _forbidden(text):
    lowered = str(text or "").lower()
    return [word for word in FORBIDDEN_WORDS if word in lowered]


def validate_recipe(data, builtin=False):
    """Schema check of recipe JSON content; returns the list of problems (empty when valid)."""
    problems = []
    if not isinstance(data, dict):
        return ["A recipe must be a JSON object"]
    if data.get("format") != FORMAT_NAME:
        problems.append(f"format must be {FORMAT_NAME!r}")
    if data.get("version") != FORMAT_VERSION:
        problems.append(f"version must be {FORMAT_VERSION}")
    if not isinstance(data.get("id"), str) or not _ID.match(data["id"]):
        problems.append("id must be lower-case letters, digits, '_', '.' or '-' (at most 80 characters)")
    for key in ("title", "description"):
        if key == "title" and not (isinstance(data.get("title"), str) and data["title"].strip()):
            problems.append("title must be a non-empty text")
        for word in _forbidden(data.get(key)):
            problems.append(f"{key} must not contain {word!r} (a recipe is a disclosed choice, not an endorsement)")
    allowed = STATUSES if builtin else ALL_STATUSES
    status = data.get("status")
    if status not in allowed:
        problems.append(f"status must be one of {', '.join(allowed)}")
    citations = data.get("citations")
    if not isinstance(citations, list) or not all(isinstance(doi, str) and _DOI.match(doi) for doi in citations):
        problems.append("citations must be a list of DOIs (10.xxxx/...)")
    elif builtin and not citations:
        problems.append("a built-in recipe must cite its source")
    differences = data.get("differences_from_source")
    if not isinstance(differences, list) or not all(isinstance(text, str) and text.strip() for text in differences):
        problems.append("differences_from_source must be a list of texts")
    elif status == "adapted" and not differences:
        problems.append("an adapted recipe must list its differences from the source")
    if status in ("adapted", "2026-preprint") and not data.get("cited_as"):
        problems.append("cited_as (for example 'Ferguson et al. 2019') is needed for the status tag")
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        problems.append("steps must be a non-empty list")
    else:
        unknown = [step for step in steps if step not in STEPS]
        if unknown:
            problems.append(f"unknown step(s): {', '.join(map(str, unknown))}")
        if len(set(steps)) != len(steps):
            problems.append("steps must not repeat")
    requires = data.get("requires")
    if not isinstance(requires, list) or not all(isinstance(name, str) and re.match(r"^[a-z_][a-z0-9_]*$", name) for name in requires):
        problems.append("requires must be a list of library module names")
    elif isinstance(steps, list):
        needed = {module for step in steps if step in STEPS for module in STEPS[step][1]}
        missing = sorted(needed - set(requires))
        if missing:
            problems.append(f"requires must list the modules of the steps: {', '.join(missing)}")
    if not isinstance(data.get("parameters", {}), dict):
        problems.append("parameters must be an object")
    else:
        try:
            params = normalize_parameters(data.get("parameters") or {})
        except (RecipeError, TypeError, ValueError, AttributeError, KeyError) as exc:  # a hand-written file: never raise
            problems.append(f"parameters: {exc}")
        else:
            if isinstance(steps, list) and ("nulls" in steps) != (params["nulls"]["mode"] != "off"):
                problems.append("the nulls step and parameters.nulls.mode must agree (a nulls step needs a mode other than 'off')")
    for key in ("based_on",):
        if key in data and not isinstance(data[key], dict):
            problems.append(f"{key} must be an object")
    return problems


def read_recipe(path, origin="folder"):
    """A :class:`Recipe` from a JSON file; raises RecipeError when it is invalid."""
    data = _read_json(path)
    if data is None:
        raise RecipeError(f"{os.path.basename(path)} is not readable JSON")
    problems = validate_recipe(data, builtin=origin == "builtin")
    if problems:
        raise RecipeError(f"{os.path.basename(path)}: " + "; ".join(problems))
    return Recipe(data, origin, os.path.abspath(path))


def recipe_folders(study=None, folders=()):
    """[(origin, folder)] searched by :func:`load_recipes`: built-ins, the study's ``recipes`` folder, then ``folders``."""
    out = [("builtin", BUILTIN_DIR)]
    if study is not None:
        out.append(("study", os.path.join(_study_path(study), STUDY_RECIPES_DIR)))
    for folder in folders or ():
        if folder:
            out.append(("folder", os.fspath(folder)))
    return out


def load_recipes(paths=None, study=None, folders=(), errors=None):
    """Recipes of the built-in folder, ``<study>/recipes`` and ``folders`` (``Studio/LNM/RecipeFolders``), in that order.

    :param paths: [(origin, folder)] to read instead of :func:`recipe_folders` (or plain folders, read as "folder").
    :param errors: a list that receives (path, message) for every invalid or duplicate file (they are also logged).
    :return: [Recipe]; the first recipe of an id wins (a lab recipe cannot replace a built-in one).
    """
    entries = recipe_folders(study, folders) if paths is None else [entry if isinstance(entry, tuple) else ("folder", entry) for entry in paths]
    out, seen = [], {}
    for origin, folder in entries:
        for path in sorted(glob.glob(os.path.join(folder, "*.json"))):
            try:
                recipe = read_recipe(path, origin)
            except Exception as exc:  # one bad file (shared lab recipes are hand-edited) never hides the others
                message = str(exc) if isinstance(exc, RecipeError) else f"{os.path.basename(path)}: {type(exc).__name__}: {exc}"
                logging.warning(f"Recipe skipped: {message}")
                if errors is not None:
                    errors.append((path, message))
                continue
            if recipe.id in seen:
                message = f"{os.path.basename(path)}: the id {recipe.id!r} is already used by {seen[recipe.id]}"
                logging.warning(f"Recipe skipped: {message}")
                if errors is not None:
                    errors.append((path, message))
                continue
            seen[recipe.id] = f"the {origin} recipe {os.path.basename(path)}"
            out.append(recipe)
    return out


def find_recipe(name, recipes=None, **kwargs):
    """The recipe whose id (or file name, or path) is ``name``; raises RecipeError when there is none."""
    if name and os.path.isfile(name):
        return read_recipe(name, "folder")
    recipes = load_recipes(**kwargs) if recipes is None else recipes
    for recipe in recipes:
        if recipe.id == name or (recipe.path and os.path.splitext(os.path.basename(recipe.path))[0] == name):
            return recipe
    raise RecipeError(f"No recipe {name!r} (known: {', '.join(recipe.id for recipe in recipes)})")


def _module_available(module):
    try:
        return importlib.util.find_spec(f"{PACKAGE}.{module}") is not None
    except (ImportError, ValueError):
        return False


def available_steps(recipe, module_available=None):
    """Steps of a recipe with what this build can run: [{"id", "label", "modules", "available", "missing", "status"}].

    ``status`` is "available" or :data:`NOT_AVAILABLE`; ``module_available(name) -> bool`` replaces the
    ``importlib.util.find_spec`` lookup (tests). The modules a recipe ``requires`` that no step names are
    checked as well and reported as an extra entry with id "requires".
    """
    check = module_available or _module_available
    data = recipe.data if isinstance(recipe, Recipe) else recipe
    cache = {}

    def available(name):
        if name not in cache:
            cache[name] = bool(check(name))
        return cache[name]

    out = []
    named = set()
    for step in data.get("steps") or []:
        label, modules, _stage = STEPS[step]
        named.update(modules)
        missing = [module for module in modules if not available(module)]
        out.append({"id": step, "label": label, "modules": list(modules), "available": not missing, "missing": missing, "status": "available" if not missing else NOT_AVAILABLE})
    extra = [module for module in data.get("requires") or [] if module not in named]
    missing = [module for module in extra if not available(module)]
    if missing:
        out.append({"id": "requires", "label": "Other modules", "modules": extra, "available": False, "missing": missing, "status": NOT_AVAILABLE})
    return out


def is_partial(recipe, module_available=None):
    """Whether some step of the recipe is not available in this build (the partial-recipe badge)."""
    return any(not step["available"] for step in available_steps(recipe, module_available))


def partial_badge(recipe, module_available=None):
    """"" or "2 steps not available in this build: Coordinate seeds, …"."""
    missing = [step["label"] for step in available_steps(recipe, module_available) if not step["available"]]
    if not missing:
        return ""
    count = "1 step" if len(missing) == 1 else f"{len(missing)} steps"
    return f"{count} {NOT_AVAILABLE}: {', '.join(missing)}"


def apply(recipe):
    """The parameters of a recipe (normalized, defaults filled in)."""
    data = recipe.data if isinstance(recipe, Recipe) else recipe
    return normalize_parameters(data.get("parameters") or {})


def diff(recipe, params):
    """Deviations of ``params`` from a recipe: [{"field", "recipe", "value", "description"}] (empty: the recipe as is)."""
    return [{"field": change["field"], "recipe": change["old"], "value": change["new"], "description": describe_change(change["field"], change["old"], change["new"])} for change in compare_parameters(apply(recipe), params)]


def custom_label(recipe, params):
    """The recipe's title, or "Custom (based on <title>)" when ``params`` differ from it."""
    return CUSTOM_PREFIX.format(recipe.title) if diff(recipe, params) else recipe.title


def _slug(text):
    slug = re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")
    return (slug or "lab_recipe")[:60]


def save_lab_recipe(path, params, base, title=None, recipe_id=None, description=None, steps=None, author=None):
    """Write the parameters as a lab recipe (status "lab") based on recipe ``base``; return the :class:`Recipe`.

    The differences from ``base`` are listed in ``differences_from_source``; the citations and the steps of
    ``base`` are kept (``steps`` replaces them). The file holds no path, so it can be shared.
    """
    params = normalize_parameters(params)
    title = (title or CUSTOM_PREFIX.format(base.title)).strip()
    if _forbidden(title) or _forbidden(description):
        raise RecipeError("A recipe title or description must not say 'recommended'")
    changes = diff(base, params)
    steps = ordered_steps(steps if steps is not None else base.steps)
    requires = sorted({module for step in steps for module in STEPS[step][1]} | set(base.get("requires") or []))
    data = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "id": recipe_id or _slug(title),
        "recipe_version": "1",
        "title": title,
        "status": LAB_STATUS,
        "cited_as": base.get("cited_as"),
        "citations": list(base.get("citations") or []),
        "differences_from_source": [f"From {base.title}: {change['description']}" for change in changes] or [f"Same parameters as {base.title}"],
        "description": description or f"Lab recipe based on {base.title} ({base.status_text()}).",
        "parameters": params,
        "steps": steps,
        "requires": requires,
        "based_on": {"id": base.id, "recipe_version": str(base.get("recipe_version") or "1"), "title": base.title, "sha256": base.sha256},
        "created": _now_iso(),
    }
    if author:
        data["author"] = str(author)
    if not data["cited_as"]:
        data.pop("cited_as")
    problems = validate_recipe(data)
    if problems:
        raise RecipeError("; ".join(problems))
    _write_json_atomic(path, data)
    return Recipe(data, "folder", os.path.abspath(path))


# Selection (which recipe the study uses before a plan is locked)


def select_recipe(study, recipe):
    """Remember the recipe chosen for a study (``derivatives/lnm/recipe.json``; no paths)."""
    path = os.path.join(_study_path(study), *SELECTION_JSON)
    _write_json_atomic(path, {"format": SELECTION_FORMAT, "version": 1, "recipe": recipe.record(), "selected": _now_iso()})


def selected_recipe(study):
    """The recipe record chosen for a study (the locked plan's recipe wins), or None."""
    plan = read_plan(study)
    if plan is not None:
        return plan.get("recipe")
    data = _read_json(os.path.join(_study_path(study), *SELECTION_JSON))
    return data.get("recipe") if isinstance(data, dict) and data.get("format") == SELECTION_FORMAT else None


# Plan lock and deviations


def plan_path(study):
    return os.path.join(_study_path(study), *PLAN_JSON)


def deviations_path(study):
    return os.path.join(_study_path(study), *DEVIATIONS_JSONL)


def plan_sha256(plan):
    """sha256 of a plan without its ``sha256`` field (what :func:`lock_plan` stores)."""
    return sha256_of({key: value for key, value in plan.items() if key != "sha256"})


def _portable_path(study_root, path):
    """A path relative to the study when inside it, else the file name only (no absolute local path in shared files)."""
    absolute = os.path.abspath(path)
    try:
        relative = os.path.relpath(absolute, study_root)
    except ValueError:  # another drive on Windows
        relative = None
    if relative is not None and not relative.startswith(".."):
        return relative.replace(os.sep, "/"), True
    return os.path.basename(absolute), False


def roi_entry(study, path, name=None):
    """{"name", "path" (study-relative or the file name), "inside_study", "sha256"} of an a-priori ROI mask."""
    root = _study_path(study)
    portable, inside = _portable_path(root, path)
    name = name or re.sub(r"\.nii(\.gz)?$", "", os.path.basename(path))
    return {"name": name, "path": portable, "inside_study": inside, "sha256": file_sha256(path)}


def normalize_hypotheses(study, hypotheses, params):
    """Validated hypotheses: {"primary": {"kind": "contrast"|"score", ...}, "rois": [...], "primary_rule", "alpha", "n_perm", "seeds"}.

    ROI masks may be given as paths (hashed here) or as :func:`roi_entry` dicts.
    """
    hypotheses = dict(hypotheses or {})
    primary = hypotheses.get("primary")
    if isinstance(primary, str):
        primary = {"kind": "score", "score": primary} if not primary.startswith(("contrast:", "A>B", "B>A", "two_sided", "F")) else {"kind": "contrast", "contrast": primary.replace("contrast:", "")}
    if not isinstance(primary, dict) or primary.get("kind") not in ("contrast", "score"):
        raise RecipeError("The plan needs a primary hypothesis: {'kind': 'contrast', 'contrast': 'A>B'} or {'kind': 'score', 'score': COLUMN}")
    if primary["kind"] == "contrast":
        primary = {"kind": "contrast", "contrast": str(primary.get("contrast") or params["design"]["contrast"]), "group_column": str(primary.get("group_column") or "group"),
                   "levels": [str(level) for level in primary.get("levels") or ["case", "control"]]}
    else:
        if not primary.get("score"):
            raise RecipeError("A score hypothesis needs the score column")
        primary = {"kind": "score", "score": str(primary["score"]), "direction": str(primary.get("direction") or "two_sided")}
    rois = []
    for item in hypotheses.get("rois") or hypotheses.get("roi_masks") or []:
        if isinstance(item, dict) and item.get("sha256"):
            rois.append({key: item[key] for key in ("name", "path", "inside_study", "sha256") if key in item})
        else:
            path = item.get("path") if isinstance(item, dict) else item
            if not path or not os.path.isfile(_absolute_in_study(study, path)):
                raise RecipeError(f"The ROI mask {path} does not exist")
            rois.append(roi_entry(study, _absolute_in_study(study, path), item.get("name") if isinstance(item, dict) else None))
    names = [roi["name"] for roi in rois]
    if len(set(names)) != len(names):
        raise RecipeError("ROI names must be unique")
    try:
        rule = _rule(hypotheses.get("primary_rule") or params["rule"])
    except (ValueError, TypeError) as exc:
        raise RecipeError(f"Invalid primary rule: {exc}") from None
    alpha = float(hypotheses.get("alpha", params["alpha"]))
    n_perm = int(hypotheses.get("n_perm", params["n_perm"]))
    if not 0.0 < alpha < 1.0:
        raise RecipeError("alpha must be between 0 and 1")
    if n_perm < MIN_N_PERM:
        raise RecipeError(f"n_perm must be at least {MIN_N_PERM}")
    seeds = {key: int(value) for key, value in {**params["seeds"], **(hypotheses.get("seeds") or {})}.items()}
    return {"primary": primary, "rois": rois, "primary_rule": rule, "alpha": alpha, "n_perm": n_perm, "seeds": seeds}


def _absolute_in_study(study, path):
    return path if os.path.isabs(path) else os.path.join(_study_path(study), path)


def read_plan(study):
    """The locked plan dict, or None when there is none."""
    data = _read_json(plan_path(study))
    return data if isinstance(data, dict) and data.get("format") == PLAN_FORMAT else None


def lock_plan(study, recipe, params, hypotheses, now=None, locked_by=None, supersede_reason=None):
    """Lock the analysis plan of a study; return the plan dict (with its ``sha256``).

    :param recipe: the :class:`Recipe` the plan starts from (its record and the differences are stored).
    :param params: the parameters to lock (normalized here); hypotheses' n_perm, alpha, rule and seeds win over them.
    :param hypotheses: see :func:`normalize_hypotheses`.
    :param now: lock time (ISO text; default: now, UTC).
    :param supersede_reason: replace an existing plan (archived as ``analysis_plan.superseded-<sha8>.json``); the
      replacement is recorded in the deviations log with this reason. Without it, an existing plan raises RecipeError.
    """
    root = _study_path(study)
    path = plan_path(study)
    previous = read_plan(study)
    if previous is not None and not (supersede_reason or "").strip():
        raise RecipeError(f"The analysis plan is already locked ({previous.get('locked_at')} · {str(previous.get('sha256'))[:8]}); changes are recorded as deviations")
    params = normalize_parameters(params)
    hyp = normalize_hypotheses(study, hypotheses, params)
    # The hypotheses state the primary rule, alpha, n_perm and seeds: the parameters follow them
    params = normalize_parameters({**params, "rule": hyp["primary_rule"], "alpha": hyp["alpha"], "n_perm": hyp["n_perm"], "seeds": hyp["seeds"]})
    name = getattr(study, "name", None) or os.path.basename(os.path.normpath(root))
    plan = {
        "format": PLAN_FORMAT,
        "version": PLAN_VERSION,
        "locked": True,
        "locked_at": now or _now_iso(),
        "study": {"name": name},
        "recipe": recipe.record(),
        "recipe_label": custom_label(recipe, params),
        "deviations_from_recipe": [{key: change[key] for key in ("field", "recipe", "value", "description")} for change in diff(recipe, params)],
        "steps": ordered_steps(recipe.steps),
        "parameters": params,
        "hypotheses": hyp,
        "note": "Pre-registration-lite plan: every later change of these parameters is recorded with its reason in analysis_plan_deviations.jsonl.",
    }
    if locked_by:
        plan["locked_by"] = str(locked_by)
    if previous is not None:
        plan["supersedes"] = {"sha256": previous.get("sha256"), "locked_at": previous.get("locked_at")}
    plan["sha256"] = plan_sha256(plan)
    if previous is not None:
        archive = os.path.join(os.path.dirname(path), f"analysis_plan.superseded-{str(previous.get('sha256') or 'unknown')[:8]}.json")
        _write_json_atomic(archive, previous)
        _write_json_atomic(path, plan)
        _append_deviation(study, {"field": "plan", "old": previous.get("sha256"), "new": plan["sha256"],
                                  "description": f"analysis plan re-locked ({plan['sha256'][:8]} replaces {str(previous.get('sha256'))[:8]})", "reason": supersede_reason.strip()}, now)
    else:
        _write_json_atomic(path, plan)
    return plan


def _append_deviation(study, record, now=None):
    path = deviations_path(study)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    record = {"time": now or _now_iso(), **record}
    line = json.dumps(record, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    return record


def record_deviation(study, field, old, new, reason, now=None, description=None):
    """Append a deviation from the locked plan to ``analysis_plan_deviations.jsonl``; return the record.

    :param reason: why the parameter changed (required; never empty).
    :raises RecipeError: no locked plan, or no reason.
    """
    reason = (reason or "").strip()
    if not reason:
        raise RecipeError("A deviation from the locked plan needs a reason")
    plan = read_plan(study)
    if plan is None:
        raise RecipeError("There is no locked analysis plan: parameter changes are not deviations")
    record = {"field": str(field), "old": old, "new": new, "description": description or describe_change(field, old, new), "reason": reason, "plan_sha256": plan.get("sha256")}
    return _append_deviation(study, json.loads(_canonical(record)), now)


def read_deviations(study):
    """Deviation records of the log (malformed lines are skipped)."""
    out = []
    try:
        with open(deviations_path(study), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict):
                    out.append(item)
    except OSError:
        pass
    return out


def plan_status(study):
    """{"state": "open" | "locked" | "modified", "locked_at", "sha256", "sha8", "recipe", "deviations", "path", "chip"}.

    "modified" means the plan file no longer matches its own sha256 (it was edited after the lock).
    ``chip`` is the text of the Map header chip: "Plan: open" or "Plan: locked 2026-10-18 · 3f2a1b9c".
    """
    path = plan_path(study)
    plan = read_plan(study)
    deviations = read_deviations(study) if plan is not None else []
    if plan is None:
        return {"state": "open", "locked_at": None, "sha256": None, "sha8": None, "recipe": None, "deviations": 0, "path": path, "chip": "Plan: open"}
    stored = str(plan.get("sha256") or "")
    state = "locked" if stored and stored == plan_sha256(plan) else "modified"
    date = str(plan.get("locked_at") or "")[:10]
    chip = f"Plan: locked {date} · {stored[:8]}" if state == "locked" else f"Plan: modified after lock ({stored[:8]})"
    return {"state": state, "locked_at": plan.get("locked_at"), "sha256": stored, "sha8": stored[:8], "recipe": plan.get("recipe"), "deviations": len(deviations), "path": path, "chip": chip}


def effective_parameters(study):
    """The locked parameters with every recorded deviation applied (None without a plan)."""
    plan = read_plan(study)
    if plan is None:
        return None
    params = normalize_parameters(plan.get("parameters") or {})
    for item in read_deviations(study):
        field = item.get("field")
        if field and field in flatten_parameters(params):
            try:
                params = normalize_parameters(set_field(params, field, item.get("new")))
            except (RecipeError, TypeError, ValueError, AttributeError) as exc:
                logging.warning(f"Deviation of {field} skipped (invalid recorded value {item.get('new')!r}): {exc}")
    return params


def effective_steps(study):
    """The locked steps with the recorded "steps" deviations applied (None without a plan)."""
    plan = read_plan(study)
    if plan is None:
        return None
    steps = list(plan.get("steps") or [])
    for item in read_deviations(study):
        if item.get("field") == "steps" and isinstance(item.get("new"), list):
            steps = list(item["new"])
    return steps


def pending_deviations(study, current, steps=None):
    """Changes of ``current`` parameters (and ``steps``) from the locked plan, with the deviations already recorded
    applied, that still need a reason.

    :return: [{"field", "old", "new", "description"}]; empty without a plan.
    """
    recorded = effective_parameters(study)
    if recorded is None:
        return []
    out = [{**change, "description": describe_change(change["field"], change["old"], change["new"])} for change in compare_parameters(recorded, current)]
    planned = ordered_steps(effective_steps(study))
    if steps is not None and ordered_steps(steps) != planned:
        steps = ordered_steps(steps)
        out.append({"field": "steps", "old": planned, "new": steps, "description": describe_change("steps", planned, steps)})
    return out


#: Analyses of the Map Cohort card that hold their own copy of plan parameters, with the text of their deviations
ANALYSIS_SCOPES = {"contrast": "Contrast GLM", "symptom": "Symptom sLNM"}
#: Plan parameters such an analysis may hold (the reason volume is off is a reason, not a setting: it is not compared)
ANALYSIS_FIELDS = ("n_perm", "design.covariates", "design.tfce", "fdr", "statistic")


def _analysis_value(field, value):
    """``value`` of an analysis ``field`` in the plan's types (raises ValueError or TypeError)."""
    if field == "n_perm":
        if isinstance(value, bool):
            raise ValueError("n_perm must be a number")
        return int(value)
    if field == "design.covariates":
        if not isinstance(value, (list, tuple)):
            raise ValueError("covariates must be a list")
        return [str(name) for name in value]
    if field in ("design.tfce", "fdr"):
        if not isinstance(value, bool):
            raise ValueError(f"{field} must be true or false")
        return value
    return str(value)


def analysis_parameters(study, scope):
    """{field: value} of :data:`ANALYSIS_FIELDS` that the Cohort-card analysis ``scope`` follows under the locked
    plan: the plan's parameters with the deviations of the plan (as :func:`effective_parameters`) and the analysis's
    own deviations ("<scope>.<field>") applied in the order recorded (the latest change of a field wins).

    :return: None without a locked plan, or when its parameters are not valid.
    """
    if scope not in ANALYSIS_SCOPES:
        raise RecipeError(f"Unknown analysis {scope!r}; expected one of {', '.join(ANALYSIS_SCOPES)}")
    plan = read_plan(study)
    if plan is None:
        return None
    try:
        params = normalize_parameters(plan.get("parameters") or {})
    except RecipeError as exc:
        logging.warning(f"The locked plan's parameters are not valid: {exc}")
        return None
    own = {}  # the analysis's own deviations
    prefix = f"{scope}."
    for item in read_deviations(study):
        field = str(item.get("field") or "")
        if field.startswith(prefix) and field[len(prefix):] in ANALYSIS_FIELDS:
            field = field[len(prefix):]
            try:
                own[field] = _analysis_value(field, item.get("new"))
            except (TypeError, ValueError) as exc:
                logging.warning(f"Deviation of {scope}.{field} skipped (invalid recorded value {item.get('new')!r}): {exc}")
        elif field and field in flatten_parameters(params):  # a deviation of the plan, as effective_parameters applies it
            try:
                params = normalize_parameters(set_field(params, field, item.get("new")))
            except (RecipeError, TypeError, ValueError, AttributeError):
                continue
            own.pop(field, None)
    flat = flatten_parameters(params)
    return {field: copy.deepcopy(own[field] if field in own else flat.get(field)) for field in ANALYSIS_FIELDS}


def analysis_pending(study, scope, current):
    """Changes of the Cohort-card analysis ``scope``'s ``current`` settings ({field: value} of
    :data:`ANALYSIS_FIELDS`) from what it follows under the locked plan (:func:`analysis_parameters`) that still
    need a reason. Record each with :func:`record_deviation` (``field`` "<scope>.<field>", ``description``).

    :return: [{"field", "old", "new", "description", "scope"}]; empty without a plan.
    """
    planned = analysis_parameters(study, scope)
    if planned is None:
        return []
    out = []
    for field in ANALYSIS_FIELDS:
        if field not in current:
            continue
        new, old = _analysis_value(field, current[field]), planned.get(field)
        if field == "design.covariates":
            same = sorted(old or []) == sorted(new)  # the order of the covariates does not change the inference
        else:
            same = _same(old, new)
        if not same:
            out.append({"field": f"{scope}.{field}", "old": old, "new": new, "description": f"{ANALYSIS_SCOPES[scope]}: {describe_change(field, old, new)}", "scope": scope})
    return out


def ordered_steps(steps):
    """Known steps in pipeline (:data:`STEPS`) order, then unknown ones as given."""
    order = list(STEPS)
    steps = list(steps or [])
    return sorted(steps, key=lambda step: order.index(step) if step in order else len(order))


def steps_from_options(steps, options):
    """A recipe's steps as the Map pipeline options run them: nulls, specificity and sLNM follow the options."""
    out = list(steps)
    wanted = {"nulls": (options.get("nulls") or "auto") != "off", "specificity": (options.get("specificity") or "auto") != "off", "slnm": (options.get("scores") or "auto") != "none"}
    for step, on in wanted.items():
        if on and step not in out:
            out.append(step)
        elif not on and step in out:
            out.remove(step)
    order = list(STEPS)
    return sorted(out, key=order.index)


# Map and pipeline


#: Pipeline options of the Map "Run everything" card (card_pipeline.DEFAULT_OPTIONS keys) and the parameters they hold
OPTION_FIELDS = (("sensitivity", "overlap"), ("perm", "n_perm"), ("tfce", "design.tfce"), ("extra_rules", "extra_rules"), ("preview_n", "preview_n"))


def pipeline_options(params, steps, options=None):
    """The Map pipeline options (card_pipeline's dialog, ``Studio/LNM/Pipeline/Options``) for a recipe's parameters and steps.

    Steps that are not in the recipe are turned off (nulls, specificity, sLNM); the other options are kept.
    """
    params = normalize_parameters(params)
    out = dict(options or {})
    out["sensitivity"] = params["overlap"]
    out["perm"] = params["n_perm"]
    out["tfce"] = params["design"]["tfce"]
    out["extra_rules"] = list(params["extra_rules"])
    out["preview_n"] = params["preview_n"]
    out["preview_seed"] = params["seeds"]["preview"]
    out["null_relocations"] = params["nulls"]["relocations"]
    out["native"] = params["native"]
    mode = params["nulls"]["mode"]
    out["nulls"] = (mode if mode in ("exact", "fast", "auto") else "off") if "nulls" in steps else "off"
    method = params["design"]["method"]
    out["specificity"] = ("auto" if method == "glm" else method) if "specificity" in steps else "off"
    out["scores"] = "auto" if "slnm" in steps else "none"
    if "volume_ml" in params["design"]["covariates"]:
        out["volume_off_reason"] = None
    else:
        out["volume_off_reason"] = params["design"]["volume_off_reason"]
    return out


def parameters_from_map(base, rule=None, options=None):
    """``base`` parameters with what the Map module holds: the threshold rule and the pipeline options."""
    params = normalize_parameters(base)
    if rule is not None:
        params["rule"] = _rule(rule)
    if options:
        for option, field in OPTION_FIELDS:
            if (option in options and options[option] is not None) or (option == "preview_n" and option in options):
                value = options[option]
                if option == "sensitivity":
                    value = float(value)
                elif option == "perm":
                    value = int(value)
                elif option == "tfce":
                    value = bool(value)
                elif option == "extra_rules":
                    value = [_rule(item) for item in value or []]
                params = set_field(params, field, value)
        if options.get("preview_seed") is not None:
            params["seeds"]["preview"] = int(options["preview_seed"])
        if options.get("null_relocations") is not None:
            params["nulls"]["relocations"] = options["null_relocations"]
        if options.get("native") in NATIVE_MODES:
            params["native"] = options["native"]
        specificity = options.get("specificity")
        if specificity is not None and str(specificity) != "off":
            method = "glm" if str(specificity) in ("auto", "glm") else str(specificity)
            params["design"]["method"] = method if method in SPECIFICITY_METHODS else params["design"]["method"]
        if "nulls" in options and options["nulls"] is not None:
            mode = str(options["nulls"])
            params["nulls"]["mode"] = mode if mode in NULL_MODES else params["nulls"]["mode"]
        reason = (options.get("volume_off_reason") or "").strip() if isinstance(options.get("volume_off_reason"), str) else ""
        if reason:
            params["design"]["covariates"] = [name for name in params["design"]["covariates"] if name != "volume_ml"]
            params["design"]["volume_off_reason"] = reason
        elif "volume_off_reason" in options and "volume_ml" not in params["design"]["covariates"]:
            params["design"]["covariates"] = ["volume_ml", *params["design"]["covariates"]]
            params["design"]["volume_off_reason"] = None
    return normalize_parameters(params)


def to_pipeline_config(recipe, study=None, params=None, module_available=None):
    """``pipeline.PipelineConfig`` fields for a recipe, and the recipe record for pipeline.json.

    :param params: parameters to use instead of the recipe's (for example the locked plan with its deviations;
      default: the effective parameters of the study's locked plan when it is based on this recipe, else the recipe's).
    :return: {"config": {...} (PipelineConfig.from_json input), "recipe": record (with "plan_sha256" when a plan is
      locked and "label"), "steps": :func:`available_steps`, "unavailable": [step ids], "outside_pipeline":
      [{"id", "label", "where"}]}.
    """
    plan = read_plan(study) if study is not None else None
    if params is None:
        params = effective_parameters(study) if plan is not None and (plan.get("recipe") or {}).get("id") == recipe.id else apply(recipe)
    params = normalize_parameters(params)
    steps = recipe.steps
    options = pipeline_options(params, steps)
    config = {
        "rule": params["rule"],
        "extra_rules": params["extra_rules"],
        "sensitivity": params["overlap"],
        "specificity": options["specificity"],
        "input": "AvgFz" if params["statistic"] == "Fz" else "T",
        "perm": params["n_perm"],
        "perm_seed": params["seeds"]["permutation"],
        "alpha": params["alpha"],
        "fdr": params["fdr"],
        "tfce": params["design"]["tfce"],
        "covariates": params["design"]["covariates"],
        "scores": options["scores"],
        "nulls": options["nulls"],
        "null_seed": params["seeds"]["nulls"],
        "null_relocations": params["nulls"]["relocations"],
        "native": params["native"],
    }
    if params["design"]["volume_off_reason"] and "volume_ml" not in params["design"]["covariates"]:
        config["volume_off_reason"] = params["design"]["volume_off_reason"]
    if params["preview_n"]:
        config["preview_n"] = params["preview_n"]
        config["preview_seed"] = params["seeds"]["preview"]
    if "report" not in steps:
        config["report"] = False
    if study is not None:
        config["study"] = os.path.abspath(_study_path(study))
    checked = available_steps(recipe, module_available)
    record = {**recipe.record(), "label": custom_label(recipe, params), "steps": [{"id": step["id"], "status": step["status"]} for step in checked]}
    if plan is not None:
        record["plan_sha256"] = plan.get("sha256")
    outside = [{"id": step, "label": STEPS[step][0], "where": MANUAL_STEP_PLACES.get(step, "Map")} for step in steps if STEPS[step][2] is None]
    return {"config": config, "recipe": record, "steps": checked, "unavailable": [step["id"] for step in checked if not step["available"]], "outside_pipeline": outside}


# Command line


def _parser():
    import argparse

    parser = argparse.ArgumentParser(prog="python -m LesionNetworkMappingLib.recipes", description="Analysis recipes and the analysis plan of lesion network mapping studies.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("list", "show", "config"):
        sub = commands.add_parser(name)
        if name != "list":
            sub.add_argument("name", help="recipe id, file name or path")
        sub.add_argument("--study", default=None, help="study folder (its recipes/ folder and locked plan)")
        sub.add_argument("--folder", action="append", default=[], help="extra recipe folder (Studio/LNM/RecipeFolders; repeatable)")
        sub.add_argument("--json", action="store_true")
        if name == "config":
            sub.add_argument("--out", default=None, help="write the pipeline configuration JSON here (for 'pipeline run --config')")
    status = commands.add_parser("status")
    status.add_argument("--study", required=True)
    status.add_argument("--json", action="store_true")
    validate = commands.add_parser("validate")
    validate.add_argument("files", nargs="+")
    return parser


def _describe(recipe):
    lines = [f"{recipe.title} [{recipe.id}] · {recipe.status_text()}"]
    if recipe.get("description"):
        lines.append(f"  {recipe['description']}")
    for doi in recipe.get("citations") or []:
        lines.append(f"  doi:{doi}")
    for text in recipe.get("differences_from_source") or []:
        lines.append(f"  difference: {text}")
    for step in available_steps(recipe):
        lines.append(f"  step {step['id']}: {step['label']} ({step['status']})")
    return "\n".join(lines)


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            failed = 0
            for path in args.files:
                data = _read_json(path)
                problems = ["not readable JSON"] if data is None else validate_recipe(data, builtin=os.path.dirname(os.path.abspath(path)) == BUILTIN_DIR)
                print(f"{path}: {'ok' if not problems else '; '.join(problems)}")
                failed += bool(problems)
            return 1 if failed else 0
        if args.command == "status":
            status = plan_status(args.study)
            print(json.dumps(status, indent=2) if args.json else status["chip"] + (f" · {status['deviations']} deviation(s)" if status["state"] != "open" else ""))
            return 0
        recipes = load_recipes(study=args.study, folders=args.folder)
        if args.command == "list":
            if args.json:
                print(json.dumps([{**recipe.record(), "status_text": recipe.status_text(), "partial": partial_badge(recipe)} for recipe in recipes], indent=2, ensure_ascii=False))
            else:
                for recipe in recipes:
                    badge = partial_badge(recipe)
                    print(f"{recipe.id:36s} {recipe.title} · {recipe.status_text()}" + (f" · {badge}" if badge else ""))
            return 0
        recipe = find_recipe(args.name, recipes)
        if args.command == "show":
            print(json.dumps(recipe.data, indent=2, ensure_ascii=False) if args.json else _describe(recipe))
            return 0
        result = to_pipeline_config(recipe, args.study)
        for step in result["unavailable"]:
            print(f"Step {step}: {NOT_AVAILABLE}", file=sys.stderr)
        for step in result["outside_pipeline"]:
            print(f"Step {step['id']} ({step['label']}) is not part of the pipeline: run it from {step['where']}", file=sys.stderr)
        if args.out:
            _write_json_atomic(args.out, result["config"])
            print(args.out)
        else:
            print(json.dumps(result if args.json else result["config"], indent=2, ensure_ascii=False))
        return 0
    except RecipeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

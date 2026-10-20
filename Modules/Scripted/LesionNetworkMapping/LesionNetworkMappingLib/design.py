"""GLM design builder for lesion network mapping group statistics (numpy only; no Slicer imports).

A :class:`DesignSpec` (``design.json``, format ``"slicer-lnm-design"`` version 1) says what to test;
:func:`build_design` turns it and a study's typed columns (``Study.analysis_rows`` and roles) into a
:class:`Design`: the design matrix X, the contrast, the subjects used and those excluded (listwise,
never imputed), the exchangeability scheme and the checks that block or warn (SPEC_LNM_ADDENDUM §3.4).

Design types:

- ``within_cohort``: every block (dataset) holds both levels of the tested factor (symptom-anchored
  case-control, Petersen 2026); labels are permuted within blocks.
- ``cases_vs_control_database``: the group is nested in dataset or source (literature cases against a
  control-lesion database, Ferguson 2019). Exchange is free, and the confounds that cannot be adjusted
  for (Cramér's V >= 0.9 with group) are listed in :attr:`Design.limitations` for the methods text.
  A dataset covariate collinear with group blocks the design with an explanation; it is never dropped.
- ``continuous`` (symptom LNM: a score regressor) and ``one_sample`` (sign flipping; always warned).

Coding: an intercept plus treatment dummies of the group (the reference level is the last level), or
cell means (one indicator per level, no intercept) for F contrasts. Numeric covariates are mean-centred
after an optional transform (log or z); categorical covariates get dummies with the first level as reference.

Permutations: :func:`permutations` (and :func:`sign_flips` for one-sample designs) yield index arrays,
the identity first. Two subjects whose design rows are identical are interchangeable, so the number of
unique permutations (:func:`unique_count`) counts distinct arrangements of design rows, as PALM does
(Winkler 2014). Enumeration is exhaustive when that number is at most the requested number of permutations.

:func:`matrix_checks` (rank, intercept, estimability, one-sample) is shared with the PALM export
validation (``palm.validate_palm_export``) and preflight checks, so imported designs pass the same checks.
"""

import dataclasses
import itertools
import json
import math
import os

import numpy as np

from . import schema

DESIGN_FORMAT = "slicer-lnm-design"
DESIGN_VERSION = 1
DESIGN_JSON = "design.json"
DESIGN_MATRIX_TSV = "design_matrix.tsv"
TESTS = ("two_group", "anova", "continuous", "one_sample")
GROUP_TESTS = ("two_group", "anova")
#: Contrasts of group tests: A and B are the first two levels; "F" uses cell-means coding
GROUP_CONTRASTS = ("A>B", "B>A", "two_sided", "F")
#: Contrasts of the score (continuous) or mean (one_sample) effect
EFFECT_CONTRASTS = ("two_sided", "positive", "negative")
PERMUTE_MODES = ("auto", "within", "free", "whole")
INPUTS = ("Fz", "T")
TRANSFORMS = ("log", "z")
DESIGN_TYPES = ("within_cohort", "cases_vs_control_database", "continuous", "one_sample")
CHECK_LEVELS = ("block", "warn", "info")
DEFAULT_N_PERM = 5000
MIN_N_PERM = 1000
#: Fewer unique permutations than this block the design (the smallest attainable p would exceed 0.01)
MIN_UNIQUE_PERMS = 100
#: Exhaustive enumeration is never attempted above this many unique permutations
EXHAUSTIVE_CAP = 1_000_000
VOLUME_COLUMN = "volume_ml"
#: Columns that define blocks when the spec names none, in order of preference
NESTING_COLUMNS = ("dataset", "source_type")
#: Columns whose association with group is reported as a limitation (Cramér's V >= CRAMERS_V_LIMIT)
CONFOUND_COLUMNS = ("dataset", "source_type", "dimensionality", "etiology", "image_modality", "scanner")
CRAMERS_V_LIMIT = 0.9
VIF_LIMIT = 5.0
MIN_PER_GROUP = 10
SKEW_LIMIT = 2.0
VARIANCE_RATIO_LIMIT = 4.0
#: Difference of the share of 2D tracings between groups (percentage points) that suggests a dimensionality covariate
DIMENSIONALITY_SHARE_LIMIT = 20.0
TWO_D_LEVELS = ("2D", "multi2D")
INTERCEPT = "intercept"
ONE_SAMPLE_MESSAGE = "One-sample test: no comparison group; one-sample LNM maps converge on connectome degree (Petersen 2026)"
DEFAULT_INFERENCE = {"fwe": True, "fdr": True, "tfce": False, "n_perm": DEFAULT_N_PERM, "seed": 0, "tails": None}
_SPEC_KEYS = (
    "test",
    "group_column",
    "levels",
    "contrast",
    "score_column",
    "covariates",
    "transforms",
    "block_column",
    "permute",
    "input",
    "inference",
    "variance_groups",
    "volume_off_reason",
    "restrict_to_mixed_blocks",
)
_TOLERANCE = 1e-8


class DesignError(ValueError):
    """The spec cannot be turned into a design (unknown column, invalid value, no subjects left)."""


# Spec


@dataclasses.dataclass
class DesignSpec:
    """What to test (``design.json``; format "slicer-lnm-design" version 1).

    :param test: "two_group", "anova", "continuous" or "one_sample".
    :param group_column: factor of group tests (default "group").
    :param levels: ordered levels of the factor (A, B, ...); default: the column's declared levels
      that occur, in declared order. Rows with other values are excluded. For continuous and
      one-sample tests, levels (one or more) only select the subjects (for example ["case"]).
    :param contrast: "A>B", "B>A", "two_sided" or "F" for group tests (A>B compares the first two
      levels); "two_sided", "positive" or "negative" for the score or the mean.
    :param score_column: regressor of continuous tests.
    :param covariates: nuisance columns; lesion volume is on by default.
    :param transforms: {column: "log" | "z"} applied to numeric covariates and the score before centring.
    :param block_column: exchangeability blocks; default: "dataset", else "source_type" when present.
    :param permute: "auto" (decided from the nesting of group in blocks), "within", "free" or "whole".
    :param input: "Fz" (default) or "T" per-lesion maps (recorded for the methods).
    :param inference: {"fwe", "fdr", "tfce", "n_perm" (default 5000, at least 1000), "seed", "tails"};
      tails are derived from the contrast (2 for two_sided, else 1); a stored value is replaced.
    :param variance_groups: use the Aspin-Welch v statistic with the groups as variance groups (two-group tests).
    :param volume_off_reason: why lesion volume is not a covariate (recorded in provenance and methods).
    :param restrict_to_mixed_blocks: exclude the subjects of blocks that hold a single group level, so
      that a partly nested design becomes a within-cohort design.
    :param extra: keys of design.json this version does not know (kept when the spec is saved).
    """

    test: str = "two_group"
    group_column: str | None = "group"
    levels: list | None = None
    contrast: str = "two_sided"
    score_column: str | None = None
    covariates: list = dataclasses.field(default_factory=lambda: [VOLUME_COLUMN])
    transforms: dict = dataclasses.field(default_factory=dict)
    block_column: str | None = None
    permute: str = "auto"
    input: str = "Fz"
    inference: dict = dataclasses.field(default_factory=dict)
    variance_groups: bool = False
    volume_off_reason: str | None = None
    restrict_to_mixed_blocks: bool = False
    extra: dict = dataclasses.field(default_factory=dict)

    def __post_init__(self):
        self.validate()

    def validate(self):
        """Check and normalize the fields (inference defaults filled in, tails resolved).

        :raises DesignError: for an invalid value.
        """
        if self.test not in TESTS:
            raise DesignError(f"Unknown test {self.test!r}; expected one of {', '.join(TESTS)}")
        group_test = self.test in GROUP_TESTS
        allowed = GROUP_CONTRASTS if group_test else EFFECT_CONTRASTS
        if self.contrast not in allowed:
            raise DesignError(f"Contrast {self.contrast!r} is not available for a {self.test} test; expected one of {', '.join(allowed)}")
        if group_test and not self.group_column:
            raise DesignError(f"A {self.test} test needs a group column")
        if self.test == "continuous" and not self.score_column:
            raise DesignError("A continuous test needs a score column")
        if self.levels is not None:
            levels = [str(level) for level in self.levels]
            if len(set(levels)) != len(levels) or len(levels) < (2 if group_test else 1):
                raise DesignError(f"levels must be at least {2 if group_test else 1} distinct value(s), got {self.levels!r}")
            if not group_test and not self.group_column:
                raise DesignError(f"levels select the subjects of a {self.test} test by group_column, which is not set")
            if self.test == "two_group" and len(levels) != 2:
                raise DesignError(f"A two-group test needs exactly 2 levels, got {len(levels)}")
            self.levels = levels
        self.covariates = [str(name) for name in (self.covariates or [])]
        if len(set(self.covariates)) != len(self.covariates):
            raise DesignError(f"Duplicate covariates: {self.covariates!r}")
        tested = self.group_column if group_test else self.score_column
        if tested and tested in self.covariates:
            raise DesignError(f"{tested} is tested and cannot also be a covariate")
        self.transforms = {str(name): value for name, value in (self.transforms or {}).items()}
        for name, transform in self.transforms.items():
            if transform not in TRANSFORMS:
                raise DesignError(f"Unknown transform {transform!r} of {name}; expected one of {', '.join(TRANSFORMS)}")
        if self.permute not in PERMUTE_MODES:
            raise DesignError(f"Unknown permutation mode {self.permute!r}; expected one of {', '.join(PERMUTE_MODES)}")
        if self.input not in INPUTS:
            raise DesignError(f"Unknown input {self.input!r}; expected one of {', '.join(INPUTS)}")
        inference = {**DEFAULT_INFERENCE, **(self.inference or {})}
        try:
            inference["n_perm"] = int(inference["n_perm"])
            inference["seed"] = int(inference["seed"])
        except (TypeError, ValueError):
            raise DesignError(f"n_perm and seed must be integers, got {inference['n_perm']!r} and {inference['seed']!r}") from None
        if inference["n_perm"] < MIN_N_PERM:
            raise DesignError(f"n_perm must be at least {MIN_N_PERM}, got {inference['n_perm']}")
        if inference["seed"] < 0:
            raise DesignError(f"seed must not be negative, got {inference['seed']}")
        for key in ("fwe", "fdr", "tfce"):
            inference[key] = bool(inference[key])
        # Tails are derived from the contrast. A stored value is the one resolved when the spec was
        # last validated or saved, so it is replaced (the contrast may have been changed since).
        if inference["tails"] not in (None, 1, 2, "1", "2"):
            raise DesignError(f"tails must be 1 or 2, got {inference['tails']!r}")
        inference["tails"] = 2 if self.contrast == "two_sided" else 1
        self.inference = inference
        self.variance_groups = bool(self.variance_groups)
        self.restrict_to_mixed_blocks = bool(self.restrict_to_mixed_blocks)
        if self.volume_off_reason is not None:
            self.volume_off_reason = str(self.volume_off_reason).strip() or None

    @property
    def tails(self):
        return self.inference["tails"]

    @property
    def n_perm(self):
        return self.inference["n_perm"]

    @property
    def seed(self):
        return self.inference["seed"]

    @property
    def cell_means(self):
        """Whether the group is coded as cell means (F contrasts)."""
        return self.test in GROUP_TESTS and self.contrast == "F"

    def to_dict(self):
        """design.json content: format, version, the spec keys, then the unknown keys that were read."""
        out = {"format": DESIGN_FORMAT, "version": DESIGN_VERSION}
        out.update({key: getattr(self, key) for key in _SPEC_KEYS})
        out["levels"] = list(self.levels) if self.levels is not None else None
        out["covariates"] = list(self.covariates)
        out["transforms"] = dict(self.transforms)
        out["inference"] = dict(self.inference)
        for key, value in self.extra.items():
            out.setdefault(key, value)
        return out

    @classmethod
    def from_dict(cls, data):
        """A spec from design.json content (the "resolved" summary written by :func:`save_design` is ignored).

        :raises DesignError: for another format, a newer version or an invalid value.
        """
        if not isinstance(data, dict):
            raise DesignError("A design spec must be a JSON object")
        if data.get("format", DESIGN_FORMAT) != DESIGN_FORMAT:
            raise DesignError(f"Not a design spec: format {data.get('format')!r}")
        try:
            version = int(data.get("version", DESIGN_VERSION))
        except (TypeError, ValueError):
            raise DesignError(f"Invalid design version {data.get('version')!r}") from None
        if version > DESIGN_VERSION:
            raise DesignError(f"Design version {data.get('version')} is newer than supported ({DESIGN_VERSION})")
        fields = {key: data[key] for key in _SPEC_KEYS if key in data}
        extra = {key: value for key, value in data.items() if key not in (*_SPEC_KEYS, "format", "version", "resolved")}
        return cls(**fields, extra=extra)

    @classmethod
    def load(cls, path):
        """Read a spec from a design.json file (or a folder holding one)."""
        path = os.fspath(path)
        if os.path.isdir(path):
            path = os.path.join(path, DESIGN_JSON)
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    def save(self, path):
        """Write the spec alone as design.json (a file path, or a folder)."""
        path = os.fspath(path)
        if os.path.isdir(path):
            path = os.path.join(path, DESIGN_JSON)
        schema.write_text_atomic(path, json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n")
        return path


def as_spec(spec):
    """A :class:`DesignSpec` from a spec, a dict, a design.json path, or None (the defaults)."""
    if spec is None:
        return DesignSpec()
    if isinstance(spec, DesignSpec):
        return spec
    if isinstance(spec, dict):
        return DesignSpec.from_dict(spec)
    return DesignSpec.load(spec)


# Design


@dataclasses.dataclass(frozen=True)
class Check:
    """A design check: level "block" (the analysis must not run), "warn" or "info"."""

    level: str
    code: str
    message: str
    detail: object = None

    def to_dict(self):
        return {"level": self.level, "code": self.code, "message": self.message, "detail": _jsonable(self.detail)}


@dataclasses.dataclass
class Design:
    """A GLM design built by :func:`build_design` (rows in :attr:`sids` order).

    Main fields (see SPEC_LNM_ADDENDUM §3.4):

    - ``X`` (n x p float64), ``columns`` (names), ``C`` (contrast, r x p; one row for t contrasts),
      ``nuisance`` (indices of the columns with zero contrast weight; an F design's intercept lies in
      the span of its cells, so partition by contrast, as in Winkler 2014, rather than by these columns);
    - ``sids``, ``excluded`` ({reason: [participant ids]});
    - ``blocks`` (int codes 1..B per row when permutations use blocks, else None);
    - ``design_type``, ``permute`` ("free", "within" or "whole") with ``permute_reason``, ``exchange``
      ("permutation" or "sign_flip"), ``row_labels`` (rows with equal codes have equal design rows);
    - ``checks``, ``vif``, ``rank``, ``unique_perms`` (exact int), ``n_perm`` (permutations to run,
      identity included), ``exhaustive``, ``min_p`` (1 / n_perm), ``n_per_level``, ``limitations``.
    """

    spec: DesignSpec
    X: np.ndarray
    columns: list
    C: np.ndarray
    nuisance: list
    sids: list
    excluded: dict
    blocks: np.ndarray | None
    design_type: str
    checks: list
    vif: dict
    rank: int
    unique_perms: int
    min_p: float
    n_per_level: dict
    limitations: list
    permute: str = "free"
    permute_reason: str = ""
    exchange: str = "permutation"
    row_labels: np.ndarray | None = None
    n_perm: int = DEFAULT_N_PERM
    exhaustive: bool = False
    levels: list | None = None
    group_labels: list | None = None
    variance_groups: np.ndarray | None = None
    block_column: str | None = None
    block_labels: list | None = None
    crosstab: dict | None = None
    cramers_v: dict = dataclasses.field(default_factory=dict)
    contrast_names: list = dataclasses.field(default_factory=list)
    stat: str = "t"

    @property
    def n(self):
        return len(self.sids)

    @property
    def p(self):
        return int(self.X.shape[1])

    @property
    def tails(self):
        return self.spec.tails

    @property
    def seed(self):
        return self.spec.seed

    @property
    def blocking(self):
        """The checks that block the analysis."""
        return [check for check in self.checks if check.level == "block"]

    @property
    def warnings(self):
        return [check for check in self.checks if check.level == "warn"]

    @property
    def ok(self):
        """True when no check blocks the analysis."""
        return not self.blocking

    def to_dict(self, matrix=False):
        """JSON-compatible summary (the "resolved" part of a saved design.json).

        :param matrix: include the design matrix (rows in :attr:`sids` order).
        """
        out = {
            "test": self.spec.test,
            "design_type": self.design_type,
            "n": self.n,
            "p": self.p,
            "columns": list(self.columns),
            "contrast": self.C.tolist(),
            "contrast_names": list(self.contrast_names),
            "stat": self.stat,
            "tails": self.tails,
            "nuisance": [int(index) for index in self.nuisance],
            "sids": list(self.sids),
            "excluded": {reason: list(sids) for reason, sids in self.excluded.items()},
            "n_excluded": sum(len(sids) for sids in self.excluded.values()),
            "levels": self.levels,
            "n_per_level": dict(self.n_per_level),
            "block_column": self.block_column,
            "crosstab": self.crosstab,
            "blocks": None if self.blocks is None else [int(value) for value in self.blocks],
            "variance_groups": None if self.variance_groups is None else [int(value) for value in self.variance_groups],
            "permute": self.permute,
            "permute_reason": self.permute_reason,
            "exchange": self.exchange,
            "unique_perms": _count_json(self.unique_perms),
            "unique_perms_log10": _log10(self.unique_perms),
            "n_perm": int(self.n_perm),
            "n_perm_requested": int(self.spec.n_perm),
            "exhaustive": bool(self.exhaustive),
            "seed": self.seed,
            "min_p": float(self.min_p),
            "rank": int(self.rank),
            "vif": {name: _finite_or_none(value) for name, value in self.vif.items()},
            "cramers_v": {name: _finite_or_none(value) for name, value in self.cramers_v.items()},
            "limitations": list(self.limitations),
            "checks": [check.to_dict() for check in self.checks],
            "blocked": not self.ok,
            "input": self.spec.input,
            "covariates": list(self.spec.covariates),
            "transforms": dict(self.spec.transforms),
            "volume_off_reason": self.spec.volume_off_reason,
        }
        if matrix:
            out["matrix"] = [[_finite_or_none(value) for value in row] for row in self.X.tolist()]
        return out


def _as_study(study):
    if isinstance(study, (str, os.PathLike)):
        from .study import Study

        return Study.open(study)
    return study


def _column_levels(study, column, values):
    """Observed values of a categorical column in declared order (participants.json/registry), then sorted."""
    declared = list((study.column_spec(column).levels or {}).keys())
    present = list(dict.fromkeys(values))
    ordered = [level for level in declared if level in present]
    return ordered + sorted(level for level in present if level not in ordered)


def _is_numeric(study, column, values):
    spec = study.column_spec(column)
    if spec.type == "numeric":
        return True
    if spec.type == "ordinal":
        return all(schema._number(value) is not None for value in values)
    return False


def _transform(name, values, transform):
    """(column name, transformed values, error message or None) of a numeric column."""
    values = np.asarray(values, dtype=np.float64)
    if transform == "log":
        if np.any(values <= 0):
            return f"log({name})", np.full_like(values, np.nan), f"log transform of {name} needs positive values (minimum {values.min():g})"
        return f"log({name})", np.log(values), None
    if transform == "z":
        sd = values.std(ddof=1) if len(values) > 1 else 0.0
        if not sd > 0:
            return f"z({name})", np.full_like(values, np.nan), f"z transform of {name} needs values that vary"
        return f"z({name})", (values - values.mean()) / sd, None
    return name, values, None


def _cramers_v(a, b):
    """Cramér's V of two categorical sequences (pairs with a missing value are skipped), or None."""
    pairs = [(x, y) for x, y in zip(a, b, strict=True) if not schema.is_missing(x) and not schema.is_missing(y)]
    if not pairs:
        return None
    rows = sorted({x for x, _ in pairs})
    cols = sorted({y for _, y in pairs})
    if len(rows) < 2 or len(cols) < 2:
        return None
    table = np.zeros((len(rows), len(cols)))
    for x, y in pairs:
        table[rows.index(x), cols.index(y)] += 1
    n = table.sum()
    expected = table.sum(axis=1, keepdims=True) * table.sum(axis=0, keepdims=True) / n
    chi2 = float(((table - expected) ** 2 / expected).sum())
    return math.sqrt(chi2 / (n * (min(len(rows), len(cols)) - 1)))


def _skewness(values):
    values = np.asarray(values, dtype=np.float64)
    centred = values - values.mean()
    m2 = float(np.mean(centred**2))
    return float(np.mean(centred**3) / m2**1.5) if m2 > 0 else 0.0


def _in_span(vector, matrix):
    """Whether a vector lies in the column space of a matrix (relative tolerance 1e-8)."""
    matrix = np.asarray(matrix, dtype=np.float64)
    if matrix.ndim != 2 or matrix.shape[1] == 0:
        return False
    coef = np.linalg.lstsq(matrix, vector, rcond=None)[0]
    return float(np.linalg.norm(vector - matrix @ coef)) <= _TOLERANCE * max(float(np.linalg.norm(vector)), 1.0)


def _null_space(matrix, tolerance=1e-10):
    """Orthonormal basis (columns) of the null space of a matrix."""
    matrix = np.atleast_2d(np.asarray(matrix, dtype=np.float64))
    _, s, vt = np.linalg.svd(matrix)
    rank = int(np.sum(s > tolerance * max(matrix.shape) * (s[0] if s.size else 1.0)))
    return vt[rank:].T


def _deficiency(matrix):
    return matrix.shape[1] - (np.linalg.matrix_rank(matrix) if matrix.shape[1] else 0)


def matrix_checks(X, C, columns=None):
    """Checks of a design matrix and contrast that need no study: the checks that an exported or
    imported design (PALM files) must pass too.

    - block: non-finite values; contrast of the wrong size; rank-deficient X; no intercept (the
      constant is not in the span of X, as in the Petersen 2026 PALM coding error; cell-means F
      designs pass because their cells sum to the constant); a contrast that is not estimable; no
      residual degrees of freedom;
    - warn: the contrast tests the mean (one-sample or intercept-only design).

    :param X: n x p design matrix.
    :param C: contrast (p values, or r x p rows).
    :param columns: optional column names for the messages.
    :return: list of :class:`Check`.
    """
    X = np.asarray(X, dtype=np.float64)
    C = np.atleast_2d(np.asarray(C, dtype=np.float64))
    if X.ndim != 2:
        return [Check("block", "matrix_shape", f"The design matrix must be 2-dimensional, got shape {X.shape}")]
    n, p = X.shape
    names = list(columns) if columns is not None else [f"column {index + 1}" for index in range(p)]
    checks = []
    if C.shape[1] != p:
        return [Check("block", "contrast_size", f"The contrast has {C.shape[1]} weights but the design has {p} columns")]
    bad = [names[index] for index in range(p) if not np.all(np.isfinite(X[:, index]))]
    if bad or not np.all(np.isfinite(C)):
        return [Check("block", "non_finite_design", f"The design has non-finite values in {', '.join(bad) or 'the contrast'}", {"columns": bad})]
    if n == 0 or p == 0:
        return [Check("block", "empty_design", f"The design is empty ({n} subjects, {p} columns)")]
    rank = int(np.linalg.matrix_rank(X))
    if rank < p:
        null = _null_space(X)
        involved = [names[index] for index in range(p) if np.any(np.abs(null[index]) > 1e-6)]
        checks.append(
            Check(
                "block",
                "rank_deficient",
                f"The design matrix is rank-deficient (rank {rank} < {p} columns): {', '.join(involved)} are linearly dependent, so their effects cannot be separated",
                {"rank": rank, "columns": involved},
            ),
        )
    ones = np.ones(n)
    has_intercept = _in_span(ones, X)
    if not has_intercept:
        checks.append(
            Check(
                "block",
                "no_intercept",
                "The design has no intercept (the constant is not in the span of its columns): the test then mixes the group "
                "difference with the mean of the maps (the PALM coding error of Petersen 2026). Add an intercept column, or code the groups as cell means",
            ),
        )
    if rank < p and not np.allclose(C @ (np.eye(p) - np.linalg.pinv(X) @ X), 0.0, atol=1e-6):
        checks.append(Check("block", "contrast_not_estimable", "The contrast is not estimable with this design matrix"))
    if n - rank < 1:
        checks.append(Check("block", "no_residual_df", f"No residual degrees of freedom ({n} subjects, rank {rank})", {"n": n, "rank": rank}))
    if has_intercept and not _in_span(ones, X @ _null_space(C)):
        checks.append(Check("warn", "one_sample", ONE_SAMPLE_MESSAGE))
    return checks


def variance_inflation(X, columns, skip=(INTERCEPT,)):
    """{column: VIF} = 1 / (1 - R²) of each column regressed on the others plus a constant.

    Constant columns and the names in ``skip`` are left out; perfectly collinear columns get inf.
    """
    X = np.asarray(X, dtype=np.float64)
    out = {}
    for index, name in enumerate(columns):
        values = X[:, index]
        total = float(np.sum((values - values.mean()) ** 2))
        if name in skip or total <= 0 or not np.all(np.isfinite(values)):
            continue
        others = np.delete(X, index, axis=1)
        if not _in_span(np.ones(len(values)), others):
            others = np.column_stack([np.ones(len(values)), others])
        coef = np.linalg.lstsq(others, values, rcond=None)[0]
        residual = float(np.sum((values - others @ coef) ** 2))
        fraction = residual / total
        out[name] = math.inf if fraction < 1e-12 else 1.0 / fraction
    return out


def build_design(study, spec=None, run_metas=None, maps=None):
    """Build the GLM design of a study (see the module docstring).

    :param study: ``study.Study`` (or a study folder).
    :param spec: :class:`DesignSpec`, design.json dict or path (default: two-group case vs control
      with a volume covariate).
    :param run_metas: run records (run.json or provenance.json) of the per-lesion maps; their
      ``export.check_poolable`` messages block the design.
    :param maps: optional per-lesion maps ({participant_id: 1-D array}, or an array in study row
      order of the subjects used) for :func:`map_checks` (non-finite values, variance ratio).
    :return: :class:`Design`. Problems of the data are checks; problems of the spec raise.
    :raises DesignError: unknown columns, a categorical column given a transform, no subjects left.
    """
    spec = as_spec(spec)
    study = _as_study(study)
    group_test = spec.test in GROUP_TESTS
    tested = spec.group_column if group_test else spec.score_column if spec.test == "continuous" else None
    selector = spec.group_column if not group_test and spec.levels is not None else None
    named = [column for column in (tested, selector, *spec.covariates, spec.block_column) if column]
    unknown = [column for column in named if column not in study.columns]
    if unknown:
        raise DesignError(f"Unknown column(s) {', '.join(unknown)} in study {study.name!r}")
    for column in spec.transforms:
        if column not in named:
            raise DesignError(f"Transform of {column}, which is neither the score nor a covariate")
    required = [column for column in named if column]
    rows, excluded = study.analysis_rows(required)
    excluded = {reason: list(sids) for reason, sids in excluded.items()}

    def exclusions():
        return "; ".join(f"{reason}: {len(sids)}" for reason, sids in excluded.items())

    def check_left():
        if not rows:
            raise DesignError(f"No subjects left after exclusions ({exclusions() or 'the study has no subjects'})")

    check_left()

    def exclude(reason, keep):
        nonlocal rows
        dropped = [row["participant_id"] for row, flag in zip(rows, keep, strict=True) if not flag]
        if dropped:
            excluded.setdefault(reason, []).extend(dropped)
        rows = [row for row, flag in zip(rows, keep, strict=True) if flag]

    # Group levels
    levels = None
    if group_test:
        values = [row[spec.group_column] for row in rows]
        levels = list(spec.levels) if spec.levels is not None else _column_levels(study, spec.group_column, values)
        exclude(f"{spec.group_column} not in levels ({', '.join(levels)})", [value in levels for value in values])
        if spec.levels is None and len(levels) < 2:
            excluded_text = f" (excluded: {exclusions()})" if excluded else ""
            raise DesignError(f"{spec.group_column} has {len(levels)} level(s) among the included subjects ({', '.join(levels) or 'none'}){excluded_text}; a {spec.test} test needs at least 2")
        if spec.test == "two_group" and len(levels) != 2:
            raise DesignError(f"A two-group test needs exactly 2 levels of {spec.group_column}; found {', '.join(levels)}. Choose the levels, or an ANOVA")
    elif selector is not None:
        # Continuous and one-sample tests: the levels select the subjects (for example the cases only)
        exclude(f"{selector} not in levels ({', '.join(spec.levels)})", [row[selector] in spec.levels for row in rows])

    # Blocks (exchangeability, and the nesting of group in dataset or source)
    block_column = spec.block_column
    if block_column is None:
        for column in NESTING_COLUMNS:
            if column in study.columns and any(not schema.is_missing(row.get(column)) for row in rows):
                block_column = column
                break
    if block_column is not None and group_test and spec.restrict_to_mixed_blocks:
        held = {}
        for row in rows:
            held.setdefault(row.get(block_column, schema.NA), set()).add(row[spec.group_column])
        exclude(f"{block_column} holds a single level of {spec.group_column}", [len(held[row.get(block_column, schema.NA)]) >= 2 for row in rows])
    check_left()
    sids = [row["participant_id"] for row in rows]
    n = len(rows)
    block_labels = None if block_column is None else [row.get(block_column) if not schema.is_missing(row.get(block_column)) else schema.NA for row in rows]
    group_labels = [row[spec.group_column] for row in rows] if group_test else None

    # Columns
    checks = []
    effect_columns, effect_names = [], []
    treatment_columns, treatment_names = [], []
    if group_test:
        indicators = {level: np.array([1.0 if value == level else 0.0 for value in group_labels]) for level in levels}
        treatment_columns = [np.ones(n)] + [indicators[level] for level in levels[:-1]]
        treatment_names = [INTERCEPT] + [f"{spec.group_column}[{level}]" for level in levels[:-1]]
        if spec.cell_means:
            effect_columns = [indicators[level] for level in levels]
            effect_names = [f"{spec.group_column}[{level}]" for level in levels]
        else:
            effect_columns, effect_names = treatment_columns, treatment_names
        empty = [level for level in levels if not indicators[level].any()]
        if empty:
            checks.append(Check("block", "empty_level", f"No included subject has {spec.group_column} = {', '.join(empty)}", {"levels": empty}))
    elif spec.test == "continuous":
        values = [float(row[spec.score_column]) for row in rows]
        name, values, error = _transform(spec.score_column, values, spec.transforms.get(spec.score_column))
        if error:
            checks.append(Check("block", "invalid_transform", error))
        effect_columns = [np.ones(n), values - np.mean(values)]
        effect_names = [INTERCEPT, name]
        treatment_columns, treatment_names = effect_columns, effect_names
    else:
        effect_columns, effect_names = [np.ones(n)], [INTERCEPT]
        treatment_columns, treatment_names = effect_columns, effect_names

    covariate_columns, covariate_names, covariate_index = [], [], {}
    for column in spec.covariates:
        values = [row[column] for row in rows]
        start = len(covariate_names)
        if _is_numeric(study, column, values):
            name, numbers, error = _transform(column, [float(value) for value in values], spec.transforms.get(column))
            if error:
                checks.append(Check("block", "invalid_transform", error))
            covariate_columns.append(numbers - np.mean(numbers))
            covariate_names.append(name)
            if np.all(np.isfinite(numbers)) and np.ptp(numbers) == 0:
                checks.append(Check("block", "constant_covariate", f"Covariate {column} has the same value ({values[0]}) for every included subject; remove it", {"covariate": column}))
        else:
            if column in spec.transforms:
                raise DesignError(f"{column} is categorical; transforms apply to numeric columns only")
            column_levels = _column_levels(study, column, values)
            if len(column_levels) < 2:
                checks.append(Check("block", "constant_covariate", f"Covariate {column} has a single level ({column_levels[0]}) among the included subjects; remove it", {"covariate": column}))
            for level in column_levels[1:]:
                covariate_columns.append(np.array([1.0 if value == level else 0.0 for value in values]))
                covariate_names.append(f"{column}[{level}]")
        covariate_index[column] = list(range(start, len(covariate_names)))

    X = np.column_stack(effect_columns + covariate_columns).astype(np.float64)
    columns = effect_names + covariate_names
    p = X.shape[1]
    X_treatment = np.column_stack(treatment_columns + covariate_columns).astype(np.float64)

    # Contrast
    C = np.zeros((1, p))
    contrast_names = [spec.contrast]
    stat = "t"
    if spec.cell_means:
        k = len(levels)
        C = np.zeros((k - 1, p))
        for index in range(k - 1):
            C[index, index], C[index, k - 1] = 1.0, -1.0
        contrast_names = [f"{levels[index]}-{levels[-1]}" for index in range(k - 1)]
        stat = "F"
    elif group_test:
        # Treatment coding: column 1 + i is level i (the last level is the reference)
        a, b = 1, (2 if len(levels) > 2 else None)
        C[0, a] = 1.0
        if b is not None:
            C[0, b] = -1.0
        if spec.contrast == "B>A":
            C = -C
        contrast_names = [{"A>B": f"{levels[0]}>{levels[1]}", "B>A": f"{levels[1]}>{levels[0]}", "two_sided": f"{levels[0]}vs{levels[1]}"}[spec.contrast]]
    else:
        C[0, 1 if spec.test == "continuous" else 0] = -1.0 if spec.contrast == "negative" else 1.0
    if spec.variance_groups:
        if spec.test != "two_group" or spec.cell_means:
            checks.append(Check("block", "variance_groups_two_group_only", "Variance groups (the Aspin-Welch v statistic) are available for two-group t contrasts only"))
        else:
            stat = "v"
    nuisance = [index for index in range(p) if np.all(C[:, index] == 0)]

    # Nesting of group in blocks, design type and limitations
    # A defaulted block column may be missing for some subjects: they form a block of their own
    # ("n/a"), which does not decide the nesting (an explicit block_column is required listwise).
    unknown_block = spec.block_column is None and block_column is not None and schema.NA in block_labels
    crosstab, nested, single_level = None, False, []
    if group_test and block_column is not None:
        crosstab = {}
        for block, level in zip(block_labels, group_labels, strict=True):
            crosstab.setdefault(block, dict.fromkeys(levels, 0))[level] += 1
        single_level = [block for block, counts in crosstab.items() if sum(1 for count in counts.values() if count) < 2 and not (unknown_block and block == schema.NA)]
        nested = bool(single_level)
    if spec.test == "continuous":
        design_type = "continuous"
    elif spec.test == "one_sample":
        design_type = "one_sample"
    else:
        design_type = "cases_vs_control_database" if nested else "within_cohort"
    limitations, cramers_v = [], {}
    if group_test:
        confounds = list(CONFOUND_COLUMNS) + ([block_column] if block_column and block_column not in CONFOUND_COLUMNS else [])
        for column in confounds:
            if column not in study.columns:
                continue
            value = _cramers_v([row.get(column) for row in rows], group_labels)
            if value is None:
                continue
            cramers_v[column] = value
            if value >= CRAMERS_V_LIMIT - 1e-12:
                limitations.append(f"{column} (Cramér's V = {value:.2f} with {spec.group_column}) cannot be adjusted for in this design")

    # Exchangeability
    row_labels = _row_codes(X)
    exchange = "sign_flip" if spec.test == "one_sample" else "permutation"
    block_codes = None
    if block_column is not None:
        names = sorted(set(block_labels))
        block_codes = np.array([names.index(value) + 1 for value in block_labels], dtype=np.int64)
    n_blocks = 0 if block_codes is None else len(set(block_codes.tolist()))
    permute, reason = _resolve_permute(spec, design_type, block_column, block_codes, n_blocks, crosstab, single_level, row_labels, checks)
    blocks = block_codes if permute in ("within", "whole") else None
    if unknown_block and (group_test or blocks is not None):
        missing = sum(1 for value in block_labels if value == schema.NA)
        checks.append(
            Check(
                "warn",
                "missing_block",
                f"{missing} subject(s) have no {block_column}: they are treated as one {block_column} of their own"
                + (f" and do not decide whether {spec.group_column} is nested in {block_column}" if group_test else "")
                + f". Fill in {block_column}, or choose the block column",
                {"block_column": block_column, "count": missing},
            ),
        )
    if exchange == "sign_flip":
        units = n_blocks if permute == "whole" else n
        unique = 2**units
    elif permute == "whole" and blocks is not None and len(set(np.bincount(blocks)[1:].tolist()) - {0}) > 1:
        unique = 0
    else:
        unique = unique_count(n, blocks, row_labels, mode=permute)
    exhaustive = unique <= min(spec.n_perm, EXHAUSTIVE_CAP)
    n_perm = int(unique) if exhaustive else spec.n_perm
    min_p = 1.0 / max(n_perm, 1)

    # Checks
    # The explanation of a collinear covariate comes before the rank and estimability checks it causes
    matrix = matrix_checks(X, C, columns)
    if group_test:
        for column, indices in covariate_index.items():
            if not indices:
                continue
            reduced = np.delete(X, [len(effect_names) + index for index in indices], axis=1)
            if _deficiency(X) <= _deficiency(reduced):
                continue
            values = [row[column] for row in rows]
            held = {}
            for value, level in zip(values, group_labels, strict=True):
                held.setdefault(value, set()).add(level)
            if all(len(group) == 1 for group in held.values()):
                message = (
                    f"Covariate {column} is collinear with {spec.group_column}: every {column} holds a single {spec.group_column} level, so the two effects cannot be "
                    f"separated and the test would have no power. Remove {column} from the covariates: the design is then cases vs a control database "
                    f"with free exchange, and {column} is reported as a limitation"
                )
            else:
                message = f"Covariate {column} is collinear with {spec.group_column} and the other columns; its effect cannot be separated from them. Remove it or change the design"
            checks.append(Check("block", "collinear_covariate", message, {"covariate": column}))
    elif spec.test == "continuous":
        for column, indices in covariate_index.items():
            reduced = np.delete(X, [len(effect_names) + index for index in indices], axis=1)
            if indices and _deficiency(X) > _deficiency(reduced):
                checks.append(Check("block", "collinear_covariate", f"Covariate {column} is collinear with {spec.score_column} and the other columns; remove it", {"covariate": column}))
    checks.extend(matrix)
    if run_metas:
        from .export import check_poolable

        for message in check_poolable(run_metas):
            checks.append(Check("block", "not_poolable", f"The per-lesion maps cannot be pooled: {message}", {"message": message}))
    if blocks is not None:
        counts = {name: int(np.sum(blocks == code)) for code, name in enumerate(sorted(set(block_labels)), 1)}
        single = [name for name, count in counts.items() if count == 1]
        if single:
            checks.append(Check("block", "singleton_block", f"{block_column} {', '.join(single)} holds a single subject: it cannot be exchanged with anyone", {"blocks": single}))
    if unique < MIN_UNIQUE_PERMS:
        checks.append(
            Check(
                "block",
                "too_few_permutations",
                f"Only {unique} unique {'sign flips' if exchange == 'sign_flip' else 'permutations'} exist (at least {MIN_UNIQUE_PERMS} are needed): the smallest attainable p would be {1 / max(unique, 1):.3g}",
                {"unique_perms": int(unique)},
            ),
        )
    elif exhaustive:
        checks.append(
            Check(
                "warn",
                "exhaustive",
                f"All {unique} unique {'sign flips' if exchange == 'sign_flip' else 'permutations'} are enumerated (exhaustive; fewer than the {spec.n_perm} requested): the smallest attainable p is {min_p:.3g}",
                {"unique_perms": int(unique)},
            ),
        )
    n_per_level = {level: int(sum(1 for value in group_labels if value == level)) for level in levels} if group_test else {"all": n}
    if group_test:
        small = {level: count for level, count in n_per_level.items() if 0 < count < MIN_PER_GROUP}
        if small:
            checks.append(Check("warn", "small_group", "Fewer than {} subjects in {}".format(MIN_PER_GROUP, ", ".join(f"{level} (n = {count})" for level, count in small.items())), small))
    vif = variance_inflation(X_treatment, treatment_names + covariate_names)
    high = {name: value for name, value in vif.items() if value > VIF_LIMIT}
    if high:
        checks.append(Check("warn", "vif", "High variance inflation (VIF > {:g}): {}".format(VIF_LIMIT, ", ".join(f"{name} {value:.1f}" for name, value in high.items())), high))
    if VOLUME_COLUMN not in spec.covariates:
        if spec.volume_off_reason:
            checks.append(Check("info", "volume_off", f"Lesion volume is not a covariate: {spec.volume_off_reason}", {"reason": spec.volume_off_reason}))
        else:
            checks.append(Check("warn", "volume_off", "Lesion volume is not a covariate and no reason is given (direct lesion-volume control is recommended, Moore 2026)"))
    elif spec.transforms.get(VOLUME_COLUMN) != "log":
        volumes = np.array([float(row[VOLUME_COLUMN]) for row in rows])
        skew = _skewness(volumes)
        if skew > SKEW_LIMIT:
            checks.append(Check("warn", "volume_skew", f"Lesion volume is skewed (skewness {skew:.1f} > {SKEW_LIMIT:g}): consider the log(volume) transform", {"skewness": skew}))
    if group_test and "dimensionality" in study.columns and "dimensionality" not in spec.covariates:
        shares = {}
        for level in levels:
            values = [row.get("dimensionality") for row, value in zip(rows, group_labels, strict=True) if value == level and not schema.is_missing(row.get("dimensionality"))]
            if values:
                shares[level] = 100.0 * sum(1 for value in values if value in TWO_D_LEVELS) / len(values)
        if len(shares) >= 2 and max(shares.values()) - min(shares.values()) > DIMENSIONALITY_SHARE_LIMIT and cramers_v.get("dimensionality", 0.0) < CRAMERS_V_LIMIT:
            text = ", ".join(f"{level} {share:.0f}%" for level, share in shares.items())
            checks.append(Check("warn", "dimensionality_imbalance", f"The share of 2D tracings differs between groups by more than {DIMENSIONALITY_SHARE_LIMIT:.0f} points ({text}): consider dimensionality as a covariate", shares))
    if excluded:
        total = sum(len(values) for values in excluded.values())
        text = "; ".join(f"{reason}: {len(values)}" for reason, values in excluded.items())
        checks.append(Check("info", "excluded", f"{total} subject(s) excluded listwise ({text}); missing values are never imputed", {reason: len(values) for reason, values in excluded.items()}))
    checks.append(Check("info", "permutation_scheme", reason, {"permute": permute, "exchange": exchange, "n_perm": n_perm, "exhaustive": exhaustive, "min_p": min_p}))

    variance_groups = None
    if stat == "v":
        variance_groups = np.array([levels.index(value) + 1 for value in group_labels], dtype=np.int64)
    design = Design(
        spec=spec,
        X=X,
        columns=columns,
        C=C,
        nuisance=nuisance,
        sids=sids,
        excluded=excluded,
        blocks=blocks,
        design_type=design_type,
        checks=_ordered(checks),
        vif=vif,
        rank=int(np.linalg.matrix_rank(X)) if np.all(np.isfinite(X)) else 0,
        unique_perms=int(unique),
        min_p=min_p,
        n_per_level=n_per_level,
        limitations=limitations,
        permute=permute,
        permute_reason=reason,
        exchange=exchange,
        row_labels=row_labels,
        n_perm=n_perm,
        exhaustive=exhaustive,
        levels=levels,
        group_labels=group_labels,
        variance_groups=variance_groups,
        block_column=block_column,
        block_labels=block_labels,
        crosstab=crosstab,
        cramers_v=cramers_v,
        contrast_names=contrast_names,
        stat=stat,
    )
    if maps is not None:
        design.checks = _ordered(design.checks + map_checks(design, maps))
    return design


def _blocks_text(blocks, limit=5):
    names = [str(block) for block in blocks]
    return ", ".join(names[:limit]) + (f" and {len(names) - limit} more" if len(names) > limit else "")


def _resolve_permute(spec, design_type, block_column, block_codes, n_blocks, crosstab, single_level, row_labels, checks):
    """(permutation mode, reason shown to the user); appends the checks of the decision.

    :param single_level: the blocks that hold a single level of the group (the group is nested in
      the blocks when there is any).
    """
    nested = bool(single_level)
    if spec.test == "one_sample":
        if spec.permute == "whole" and n_blocks >= 2:
            return "whole", f"Sign flipping of whole {block_column} blocks (one-sample test)"
        return "free", "Sign flipping (one-sample test)"
    if spec.permute == "free":
        if design_type == "within_cohort" and n_blocks >= 2:
            checks.append(Check("info", "blocks_off", f"Free exchange was chosen although every {block_column} holds both levels; within-{block_column} permutation is the recommended scheme"))
        return "free", "Free exchange (chosen)"
    if spec.permute in ("within", "whole"):
        if block_column is None or n_blocks < 2:
            what = "no block column (dataset or source_type)" if block_column is None else f"a single {block_column} block"
            checks.append(Check("block", "no_blocks", f"{spec.permute.capitalize()}-block permutation needs at least 2 blocks; the study has {what}"))
            return spec.permute, f"{spec.permute.capitalize()}-block permutation (no blocks)"
        if spec.permute == "within" and nested:
            checks.append(
                Check(
                    "block",
                    "nested_blocks",
                    f"{spec.group_column} is nested in {block_column}: within-{block_column} permutation never exchanges levels in the blocks that hold a single level. "
                    f"Use free exchange (cases vs a control database; confounds are listed as limitations) or restrict the analysis to blocks that hold both levels",
                    {"crosstab": crosstab},
                ),
            )
        if spec.permute == "whole":
            sizes = set(np.bincount(block_codes)[1:].tolist()) - {0}
            if len(sizes) > 1:
                checks.append(Check("block", "unequal_blocks", f"Whole-block permutation needs blocks of equal size; the {block_column} blocks have {', '.join(str(size) for size in sorted(sizes))} subjects"))
        return spec.permute, f"{spec.permute.capitalize()}-{block_column} permutation (chosen)"
    # auto
    if block_column is None or n_blocks < 2:
        return "free", "Free exchange: one cohort (no dataset or source column)" if block_column is None else f"Free exchange: all subjects are in one {block_column}"
    if spec.test in GROUP_TESTS and nested:
        mixed = [block for block, counts in crosstab.items() if sum(1 for count in counts.values() if count) >= 2]
        if mixed:
            # Partly nested: name the blocks that hold a single level rather than calling the whole design nested
            what = f"{block_column} {_blocks_text(single_level)} {'holds' if len(single_level) == 1 else 'hold'} a single level of {spec.group_column}"
        else:
            what = f"{spec.group_column} is nested in {block_column}"
        checks.append(
            Check(
                "info",
                "nested",
                f"{what} (cases vs a control database): exchange is free and the confounds are listed as limitations"
                + (f". Alternatively, restrict the analysis to the {len(mixed)} {block_column} block(s) that hold both levels" if mixed else ""),
                {"crosstab": crosstab, "mixed_blocks": mixed, "single_level_blocks": list(single_level)},
            ),
        )
        return "free", f"Free exchange: {what} (cases vs control database); limitations recorded"
    counts = np.bincount(block_codes)[1:]
    if spec.test in GROUP_TESTS:
        too_small = [block for block, cell in crosstab.items() if any(0 < count < 2 for count in cell.values())]
        problem = f"{block_column} {', '.join(too_small)} has fewer than 2 subjects of a level" if too_small else None
    else:
        too_small = [str(index + 1) for index, count in enumerate(counts) if count < 2]
        problem = f"{len(too_small)} {block_column} block(s) hold a single subject" if too_small else None
    if problem is None:
        unique = unique_count(len(block_codes), block_codes, row_labels, mode="within")
        if unique < MIN_UNIQUE_PERMS:
            problem = f"only {unique} unique within-{block_column} permutations exist"
    if problem is not None:
        checks.append(Check("warn", "blocks_not_used", f"Within-{block_column} permutation is not possible ({problem}): exchange is free across {block_column}"))
        return "free", f"Free exchange: {problem}"
    if spec.test in GROUP_TESTS:
        return "within", f"Within-{block_column} permutation: every {block_column} holds both levels (within-cohort design)"
    return "within", f"Within-{block_column} permutation of the scores"


def _ordered(checks):
    order = {level: index for index, level in enumerate(CHECK_LEVELS)}
    return sorted(checks, key=lambda check: order.get(check.level, len(order)))


def map_checks(design, maps):
    """Checks of the per-lesion maps of a design.

    - block: non-finite map values;
    - warn: the variance of the maps differs between groups (median voxel ratio of the largest to the
      smallest group variance > 4): suggest variance groups (the Aspin-Welch v statistic).

    :param maps: n x V array in design order, or {participant_id: 1-D array}.
    """
    if isinstance(maps, dict):
        missing = [sid for sid in design.sids if sid not in maps]
        if missing:
            return [Check("block", "missing_maps", f"No map for {len(missing)} subject(s): {', '.join(missing[:10])}", {"sids": missing})]
        maps = np.stack([np.asarray(maps[sid]).reshape(-1) for sid in design.sids])
    maps = np.asarray(maps)
    if maps.ndim != 2 or maps.shape[0] != design.n:
        return [Check("block", "map_shape", f"Expected {design.n} maps in design order, got an array of shape {maps.shape}")]
    checks = []
    bad = [design.sids[index] for index in range(design.n) if not np.all(np.isfinite(maps[index]))]
    if bad:
        checks.append(Check("block", "non_finite_maps", f"{len(bad)} map(s) have non-finite values: {', '.join(bad[:10])}", {"sids": bad}))
        return checks
    if design.group_labels is not None and design.stat != "v":
        variances = []
        for level in design.levels:
            rows = np.array([value == level for value in design.group_labels])
            if rows.sum() >= 2:
                variances.append(np.var(maps[rows], axis=0, ddof=1, dtype=np.float64))
        if len(variances) >= 2:
            stacked = np.stack(variances)
            low = stacked.min(axis=0)
            usable = low > 0
            if np.any(usable):
                ratio = float(np.median(stacked.max(axis=0)[usable] / low[usable]))
                if ratio > VARIANCE_RATIO_LIMIT:
                    checks.append(
                        Check(
                            "warn",
                            "variance_ratio",
                            f"The variance of the maps differs between groups (median ratio {ratio:.1f} > {VARIANCE_RATIO_LIMIT:g}): consider variance groups (Aspin-Welch v statistic)",
                            {"median_ratio": ratio},
                        ),
                    )
    return checks


# Permutations


def _codes(labels, n):
    """Integer codes (0..) of per-row labels in order of first appearance; every row distinct when None."""
    if labels is None:
        return np.arange(n, dtype=np.int64)
    labels = list(labels.tolist() if isinstance(labels, np.ndarray) else labels)
    if len(labels) != n:
        raise ValueError(f"Expected {n} labels, got {len(labels)}")
    first = {}
    return np.array([first.setdefault(label, len(first)) for label in labels], dtype=np.int64)


def _row_codes(X):
    """Codes of the rows of a design matrix (equal codes for equal rows, up to 1e-10)."""
    X = np.asarray(X, dtype=np.float64)
    if not np.all(np.isfinite(X)):
        return np.arange(X.shape[0], dtype=np.int64)
    return _codes([tuple(np.round(row, 10).tolist()) for row in X], X.shape[0])


def _multinomial(counts):
    total, out = 0, 1
    for count in counts:
        total += int(count)
        out *= math.comb(total, int(count))
    return out


def _block_rows(blocks, n):
    """[(block code, row indices)] in ascending block order; one block of all rows when blocks is None."""
    if blocks is None:
        return [(0, np.arange(n))]
    blocks = np.asarray(blocks).reshape(-1)
    if len(blocks) != n:
        raise ValueError(f"Expected {n} block codes, got {len(blocks)}")
    return [(code, np.flatnonzero(blocks == code)) for code in sorted(set(blocks.tolist()))]


def _whole_patterns(blocks, codes, n):
    groups = _block_rows(blocks, n)
    sizes = {len(rows) for _, rows in groups}
    if len(sizes) > 1:
        raise ValueError(f"Whole-block permutation needs blocks of equal size, got sizes {sorted(sizes)}")
    return groups, _codes([tuple(codes[rows].tolist()) for _, rows in groups], len(groups))


def unique_count(n, blocks=None, group_labels=None, mode=None):
    """Number of distinct permutations of n rows (an exact int).

    Rows with equal labels (equal design rows, or group levels) are interchangeable; with no labels
    every row is distinct (n!). For two groups of 4 without blocks it is C(8, 4) = 70.

    :param blocks: per-row block codes, or None.
    :param group_labels: per-row labels, or None.
    :param mode: "free", "within" (default when blocks are given) or "whole" (blocks of equal size
      permuted as units).
    """
    codes = _codes(group_labels, n)
    mode = mode or ("within" if blocks is not None else "free")
    if mode == "free" or blocks is None:
        return _multinomial(np.bincount(codes).tolist()) if n else 1
    if mode == "within":
        out = 1
        for _, rows in _block_rows(blocks, n):
            out *= _multinomial(np.bincount(codes[rows]).tolist())
        return out
    if mode == "whole":
        _, patterns = _whole_patterns(blocks, codes, n)
        return _multinomial(np.bincount(patterns).tolist())
    raise ValueError(f"Unknown permutation mode {mode!r}")


def _multiset_permutations(codes):
    """Distinct arrangements of a sequence of codes: the sequence itself first, then the others in lexicographic order."""
    original = list(codes)
    yield list(original)
    current = sorted(original)
    while True:
        if current != original:
            yield list(current)
        # Next lexicographic permutation
        i = len(current) - 2
        while i >= 0 and current[i] >= current[i + 1]:
            i -= 1
        if i < 0:
            return
        j = len(current) - 1
        while current[j] <= current[i]:
            j -= 1
        current[i], current[j] = current[j], current[i]
        current[i + 1 :] = reversed(current[i + 1 :])


def _arrangement_index(codes, arrangement):
    """Index array P with codes[P] == arrangement (the identity for the original arrangement)."""
    queues = {}
    for index, code in enumerate(codes):
        queues.setdefault(code, []).append(index)
    positions = dict.fromkeys(queues, 0)
    out = np.empty(len(codes), dtype=np.intp)
    for index, code in enumerate(arrangement):
        out[index] = queues[code][positions[code]]
        positions[code] += 1
    return out


def permutations(n, blocks=None, mode="free", n_perm=DEFAULT_N_PERM, seed=0, labels=None):
    """Yield permutation index arrays P (data rows Y[P]); the first is always the identity.

    :param blocks: per-row block codes (needed by "within" and "whole").
    :param mode: "free" (rng.permutation), "within" (permute inside each block, never across) or
      "whole" (permute equal-size blocks as units).
    :param n_perm: number of arrays, identity included. When the number of unique permutations
      (:func:`unique_count` with ``labels``) is at most n_perm (and at most 1e6), every unique
      permutation is yielded once instead (exhaustive enumeration).
    :param labels: per-row labels; rows with equal labels are interchangeable (default: all distinct).
    """
    if mode != "free" and blocks is None:
        raise ValueError(f"{mode} permutation needs blocks")
    codes = _codes(labels, n)
    unique = unique_count(n, blocks, codes, mode=mode)
    yield np.arange(n, dtype=np.intp)
    if unique <= min(n_perm, EXHAUSTIVE_CAP):
        yield from itertools.islice(_exhaustive(n, blocks, mode, codes), 1, None)
        return
    rng = np.random.default_rng(seed)
    groups = _block_rows(blocks if mode != "free" else None, n)
    for _ in range(n_perm - 1):
        if mode == "free":
            yield rng.permutation(n).astype(np.intp)
        elif mode == "within":
            out = np.arange(n, dtype=np.intp)
            for _, rows in groups:
                out[rows] = rows[rng.permutation(len(rows))]
            yield out
        else:
            order = rng.permutation(len(groups))
            out = np.empty(n, dtype=np.intp)
            for (_, rows), source in zip(groups, order, strict=True):
                out[rows] = groups[source][1]
            yield out


def _exhaustive(n, blocks, mode, codes):
    """Every unique permutation once, the identity first."""
    if mode == "whole":
        groups, patterns = _whole_patterns(blocks, codes, n)
        for arrangement in _multiset_permutations(patterns.tolist()):
            sources = _arrangement_index(patterns, arrangement)
            out = np.empty(n, dtype=np.intp)
            for (_, rows), source in zip(groups, sources, strict=True):
                out[rows] = groups[source][1]
            yield out
        return
    groups = _block_rows(blocks if mode == "within" else None, n)
    per_block = [[rows[_arrangement_index(codes[rows], arrangement)] for arrangement in _multiset_permutations(codes[rows].tolist())] for _, rows in groups]
    for combination in itertools.product(*per_block):
        out = np.arange(n, dtype=np.intp)
        for (_, rows), arranged in zip(groups, combination, strict=True):
            out[rows] = arranged
        yield out


def sign_flips(n, n_perm=DEFAULT_N_PERM, seed=0, blocks=None, mode="free"):
    """Yield sign-flip vectors (n floats of ±1) for one-sample tests; the first is all +1.

    Exhaustive (all 2^units, where units are the rows, or the blocks for mode "whole") when that is at
    most n_perm (and at most 1e6), else n_perm vectors drawn at random.
    """
    groups = _block_rows(blocks, n) if mode == "whole" else [(index, np.array([index])) for index in range(n)]
    units = len(groups)
    yield np.ones(n)
    if 2**units <= min(n_perm, EXHAUSTIVE_CAP):
        for mask in range(1, 2**units):
            out = np.ones(n)
            for unit, (_, rows) in enumerate(groups):
                if mask >> unit & 1:
                    out[rows] = -1.0
            yield out
        return
    rng = np.random.default_rng(seed)
    for _ in range(n_perm - 1):
        signs = rng.choice((-1.0, 1.0), size=units)
        out = np.empty(n)
        for (_, rows), sign in zip(groups, signs, strict=True):
            out[rows] = sign
        yield out


def design_shufflings(design):
    """The permutation index arrays (or sign-flip vectors, when ``design.exchange`` is "sign_flip") of a design."""
    if design.exchange == "sign_flip":
        return sign_flips(design.n, design.spec.n_perm, design.seed, design.blocks, design.permute)
    return permutations(design.n, design.blocks, design.permute, design.spec.n_perm, design.seed, design.row_labels)


# Preview and files


def _finite_or_none(value):
    value = float(value)
    return value if math.isfinite(value) else None


def _count_json(count):
    """An exact count when JavaScript can represent it (< 2^53), else None (see unique_perms_log10)."""
    return int(count) if count < 2**53 else None


def _log10(count):
    return math.log10(int(count)) if count > 0 else None


def _jsonable(value):
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, np.ndarray):
        return _jsonable(value.tolist())
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return _finite_or_none(value)
    return value


def design_preview(design):
    """JSON-compatible dict for the design preview of the UI: the matrix, column names, rank, VIF,
    unique permutations, min p, checks (blocking first), design type, permutation scheme and limitations.
    """
    return _jsonable(design.to_dict(matrix=True))


def save_design(design, folder):
    """Write design.json (the spec plus a "resolved" summary) and design_matrix.tsv (participant_id plus the columns).

    :return: (design.json path, design_matrix.tsv path).
    """
    folder = os.fspath(folder)
    os.makedirs(folder, exist_ok=True)
    content = design.spec.to_dict()
    content["resolved"] = _jsonable(design.to_dict())
    json_path = os.path.join(folder, DESIGN_JSON)
    schema.write_text_atomic(json_path, json.dumps(content, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    lines = ["\t".join(["participant_id", *design.columns])]
    lines += ["\t".join([sid, *(_format(value) for value in row)]) for sid, row in zip(design.sids, design.X.tolist(), strict=True)]
    tsv_path = os.path.join(folder, DESIGN_MATRIX_TSV)
    schema.write_text_atomic(tsv_path, "\n".join(lines) + "\n")
    return json_path, tsv_path


def read_design_json(path):
    """(:class:`DesignSpec`, resolved summary dict or None) of a design.json written by :func:`save_design`."""
    path = os.fspath(path)
    if os.path.isdir(path):
        path = os.path.join(path, DESIGN_JSON)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return DesignSpec.from_dict(data), data.get("resolved")


def _format(value):
    value = float(value)
    if not math.isfinite(value):
        return "n/a"
    return "%.10g" % (0.0 if value == 0 else value)

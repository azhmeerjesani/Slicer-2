"""Out-of-sample validation of lesion network mapping circuits against mandatory baselines (SPEC_LNM_ADDENDUM §3.8).

numpy and scipy only; no Slicer imports. Does a circuit predict held-out patients better than lesion volume,
the connectome's degree map (μC_r), its first principal component (PC1) and lesion location (Treeratana 2026)?

- Folds (:func:`make_folds`): leave-one-dataset-out (LODO) when every held-out dataset holds both classes
  (case-control) or at least 10 subjects (scores); otherwise stratified 10-fold x 5 repeats, with the reason.
  LOO is allowed with the warning :data:`LOO_WARNING`.
- In-fold circuits, from the training subjects only: the unthresholded GLM t map of the outcome
  ([1, outcome, volume, covariates], OLS per voxel), the sLNM partial r map (r = t / sqrt(t² + df)) or the
  top-x% ROI of the in-fold t map (a fixed rank rule). No permutation-FWE threshold is ever applied inside a
  fold (nested permutations are infeasible; addendum §3.8). The fit uses the sufficient statistics XᵀX and XᵀY:
  per repeat the sums of every (fold, inner block) cell are computed once and each map's excluded cells are
  subtracted. A t map is invariant to an affine recoding of the covariates (with an intercept), so the constant
  centring used for conditioning equals the training-fitted standardisation. Every builder calls
  :func:`assert_disjoint` (a held-out lesion never enters its own circuit or location map).
- Cross-fitting: a circuit built from the training lesions fits them too well, so their in-sample scores would
  give the score an inflated model weight. Each training subject is therefore scored with an inner circuit built
  without its inner block (:data:`INNER_FOLDS` blocks of the training subjects); the held-out subjects are scored
  with the circuit of all the training subjects. The lesion-location baseline is cross-fitted the same way.
- Scores (scores.py): the Fisher-z spatial r of each lesion's network map (AvgFz) with the circuit (default),
  the same r with [μC_r, PC1] partialled out (an extra "circuit (partial μC, PC1)" row), the damage score
  (occupancy x circuit) or the ROI connectivity (top-x% circuits).
- Models (fitted on the training subjects' in-fold scores, standardised on the training subjects): logistic
  regression (numpy IRLS, ridge 1e-6) for a binary outcome, least squares (ridge 1e-6) for a continuous one;
  lesion volume (and the covariates) are in every model.
- Baselines, always reported and computed in the same folds: volume only; volume + μC_r score; volume + PC1
  score; volume + the in-fold lesion-location map (proportional subtraction case% - control% of the training
  lesions' occupancy, or the outcome-weighted occupancy for scores, scored as damage); optional reference
  maps. A baseline that cannot be computed is reported with the reason.
- Metrics of the out-of-fold predictions, per repeat then averaged over repeats: AUC (Mann-Whitney) and the
  odds ratio per SD for a binary outcome; r and the out-of-sample R² (against each fold's training mean; may be
  negative) for a continuous one.
- Uncertainty: whole-CV bootstrap CIs (B = 200; subjects resampled within strata, dataset x class, and the
  folds, circuits and models rerun; a resampled subject's copies always stay in the same fold; bias-corrected
  percentile intervals, see :func:`bootstrap_interval`), permutation p
  (K = 1000; the outcome permuted within datasets and the whole pipeline rerun; p = (1 + #{null >= observed}) /
  (K + 1), one-sided). Bootstrapping pooled predictions would ignore the fold dependence (too narrow).
- Incremental value: Δ(primary metric) of volume + circuit against volume alone, with bootstrap CI and
  permutation p; the circuit against every other baseline with bootstrap CIs. :func:`verdict_text` is neutral,
  for example "does not exceed the degree baseline (ΔAUC 0.01, 95% CI -0.04 to 0.06)" (with a minus sign).
- :func:`apply_frozen`: a circuit fixed by another study (file and sha256) scored on a new cohort, with metrics
  and a simple bootstrap CI (no fitting on the new cohort).

Command line (JSON events, see ``jobs``; exit codes 0 done, 1 error, 2 usage, 3 cancelled, 75 locked)::

    python -m LesionNetworkMappingLib.validate --maps maps.tsv --participants P --outcome COL
        --type binary|continuous --circuit glm|slnm|topk [--covariates a,b] [--scheme auto|lodo|kfold|loo]
        [--bundle B] [--refmaps a.nii.gz,b.nii.gz] --out DIR [--score similarity|damage|roi] [--top-pct 10]
        [--positive LABEL] [--dataset COL|none] [--k 10] [--repeats 5] [--boot 200] [--perm 1000] [--seed S]
        [--input Fz|T] [--mask M] [--threads K] [--cancel-file F] [--run-root DIR] [--parent-run ID]
    python -m LesionNetworkMappingLib.validate --frozen circuit.nii.gz --frozen-sha256 HEX --maps maps.tsv
        --participants P --outcome COL --type binary|continuous --out DIR [--score ...] [--boot 1000]

Cost (measured on this 4-core host, 3 BLAS threads, GSP mask V = 228,483, n = 200, GLM t circuit, 10-fold x 5
repeats with cross-fitting): about 2 s per cross-validation, so the defaults (1 + 200 + 1000 runs) take about
45 minutes; top-x% circuits about twice that per run (n = 100: 4.3 s). The job reports the estimate first.

The output folder (usually ``derivatives/lnm/validation``) receives validation.json, scores.tsv (one row per
subject: outcome, volume, held-out scores and predictions) and folds.tsv (participant, repeat, fold). Paths in
validation.json are relative to it (SPEC 9.7 rule 10). The run is registered (kind "validation") with the
cohortmaps run of maps.tsv as parent. After the observed cross-validation the job reports an ETA for the
bootstrap and permutation work, then progress with ETA per rerun. ``--mask`` defaults to the mask of the
connectome bundle (``--bundle`` or the bundle recorded in cohortmaps.json); the lesion occupancy (damage
scores and the lesion-location baseline) is read from the weights.npz of each map's cache entry.
"""

import os
import sys

if __name__ == "__main__":
    from LesionNetworkMappingLib.jobs import set_blas_threads

    set_blas_threads(sys.argv[1:])  # before numpy is imported: BLAS reads these variables once

import dataclasses
import math
import time
import types

import numpy as np

from . import jobs, runs, scores

FORMAT_NAME = "slicer-lnm-validation"
FORMAT_VERSION = 1
VALIDATION_JSON = "validation.json"
SCORES_TSV = "scores.tsv"
FOLDS_TSV = "folds.tsv"
LOCK_NAME = ".validate.lock"
MAPS_MEMMAP = ".validate-maps.npy"
#: Paths of validation.json (dotted keys; lists hold paths), stored relative to its folder
RECORD_PATH_FIELDS = ("inputs.maps_tsv", "inputs.participants", "inputs.mask", "inputs.cohortmaps_json", "inputs.refmaps", "connectome.bundle.path", "reference.path", "frozen.circuit")

SCHEMES = ("auto", "lodo", "kfold", "loo")
CIRCUITS = ("glm", "slnm", "topk")
SCORE_KINDS = ("similarity", "damage", "roi")
OUTCOME_TYPES = ("binary", "continuous")
DEFAULT_K = 10
DEFAULT_REPEATS = 5
DEFAULT_TOP_PCT = 10.0
DEFAULT_BOOT = 200
DEFAULT_PERM = 1000
DEFAULT_FROZEN_BOOT = 1000
#: LODO needs at least this many subjects in every dataset for a continuous outcome (addendum §3.8)
MIN_LODO_SUBJECTS = 10
RIDGE = 1e-6
CI_LEVEL = 0.95
#: Below this many finite bootstrap values the BC interval is degenerate (z0 is clipped): the plain percentile
#: interval is used instead
MIN_BC_BOOT = 20
#: ``--dataset none``: no dataset column (one cohort), even when the participants table has one
NO_DATASET = "none"
#: |AvgFz| above this inside the mask is far outside the Fisher z range of averaged correlations (a corrupt map?)
FZ_WARN = 10.0
LOO_WARNING = "pooled LOO predictions are negatively biased for r/AUC and have high variance"
#: Reference maps partialled out of the circuit similarity and used as the degree and PC1 baselines
REFERENCE_KEYS = ("mu_r", "pc1")
#: Inner blocks of the cross-fitting: a training subject's score comes from the in-fold circuit (and location map)
#: built without its block, so that the models are fitted on out-of-sample training scores
INNER_FOLDS = 5
#: In-fold maps (outer and inner circuits) of one pass over the maps
MAX_MAPS_PER_PASS = 512
#: Bytes of the per-chunk temporaries of a pass (cell sums and maps); the voxel chunk is sized to fit
PASS_BYTES = 96 * 1024**2
#: Precision of the in-fold map arithmetic of a pass (cell sums, maps, products); sums over chunks are float64.
#: The maps are centred per voxel first, so float32 keeps about 6 significant digits of t (as glm's float32 runs).
PASS_DTYPE = np.float32
#: A voxel whose residual sum of squares is below this fraction of its total is fitted exactly: its map value is 0
SSE_RTOL = {np.dtype(np.float32): 1e-5, np.dtype(np.float64): 1e-12}
#: Bytes of stored in-fold maps per pass (top-x% circuits need the whole map before thresholding)
STORE_BYTES = 256 * 1024**2
CHUNK_VOXELS = scores.CHUNK_VOXELS
CIRCUIT_LABELS = {
    "glm": "in-fold GLM t map of the outcome (unthresholded)",
    "slnm": "in-fold sLNM partial r map of the outcome (unthresholded)",
    "topk": "top-x% ROI of the in-fold GLM t map (fixed rank rule)",
}
BASELINE_LABELS = {
    "volume": "lesion-volume baseline",
    "mu_r": "degree baseline",
    "pc1": "PC1 baseline",
    "location": "lesion-location baseline",
}
METRICS = {"binary": ("auc", "or_per_sd"), "continuous": ("r", "r2")}
PRIMARY = {"binary": "auc", "continuous": "r2"}
METRIC_NAMES = {"auc": "AUC", "or_per_sd": "OR per SD", "r": "r", "r2": "R²"}
MODEL_NOTE = "Models are fitted on the training subjects' in-fold scores (the held-out subjects never enter a circuit, a location map, a standardisation or a model)."
_TINY = 1e-300


class LeakageError(AssertionError):
    """A held-out subject would enter its own fold's circuit, location map or model."""


def _index_set(values):
    values = np.asarray(values)
    if values.dtype == bool:
        return np.flatnonzero(values)
    return values.astype(np.int64).ravel()


def assert_disjoint(train, test):
    """Raise :class:`LeakageError` when a subject is both in ``train`` and ``test`` (boolean masks or index arrays)."""
    train = np.asarray(train)
    test = _index_set(test)
    if train.dtype == bool:
        common = test[train[test]] if test.size else test
    else:
        common = np.intersect1d(train.astype(np.int64).ravel(), test)
    if common.size:
        raise LeakageError(f"{np.unique(common).size} held-out subject(s) are in the training set (indices {np.unique(common)[:5].tolist()})")


# Folds


@dataclasses.dataclass
class Folds:
    """Cross-validation folds of the n subjects.

    ``assignment`` (repeats x n int): the fold of each subject in each repeat, -1 for a subject not in the
    sample (a bootstrap count of 0). ``labels``: per repeat, the fold names (dataset names for LODO).
    ``scheme``: the scheme used ("lodo", "kfold" or "loo"); ``requested``: the scheme asked for; ``reason``:
    why this scheme (the LODO decision); ``message``: set when the scheme differs from the request.
    """

    scheme: str
    requested: str
    assignment: np.ndarray
    labels: list
    k: int | None = None
    reason: str = ""
    message: str | None = None
    warnings: list = dataclasses.field(default_factory=list)

    @property
    def repeats(self):
        return int(self.assignment.shape[0])

    def n_folds(self, repeat=0):
        return len(self.labels[repeat])

    def iter_folds(self):
        """(repeat, fold, test indices, train boolean mask) of every fold."""
        for repeat in range(self.repeats):
            row = self.assignment[repeat]
            for fold in range(self.n_folds(repeat)):
                test = np.flatnonzero(row == fold)
                if test.size == 0:
                    continue
                yield repeat, fold, test, (row >= 0) & (row != fold)

    def describe(self):
        return {
            "requested": self.requested,
            "used": self.scheme,
            "k": self.k,
            "repeats": self.repeats,
            "n_folds": [self.n_folds(repeat) for repeat in range(self.repeats)],
            "reason": self.reason,
            "message": self.message,
            "warnings": list(self.warnings),
        }


def infer_outcome_type(outcome):
    values = np.asarray(outcome, dtype=np.float64)
    finite = values[np.isfinite(values)]
    return "binary" if finite.size and np.all(np.isin(finite, (0.0, 1.0))) else "continuous"


def _dataset_labels(datasets, n):
    if datasets is None:
        return None
    labels = [str(value) for value in datasets]
    if len(labels) != n:
        raise ValueError(f"{len(labels)} dataset labels for {n} subjects")
    return labels


def lodo_check(outcome, datasets, outcome_type, counts=None, min_subjects=MIN_LODO_SUBJECTS):
    """(LODO possible, reason) for these subjects (``counts`` > 0 are in the sample)."""
    outcome = np.asarray(outcome, dtype=np.float64)
    n = outcome.size
    labels = _dataset_labels(datasets, n)
    if labels is None:
        return False, "no dataset column"
    included = np.ones(n, dtype=bool) if counts is None else np.asarray(counts) > 0
    groups = {}
    for index in np.flatnonzero(included):
        groups.setdefault(labels[index], []).append(index)
    if len(groups) < 2:
        return False, "a single dataset"
    names = sorted(groups)
    if outcome_type == "binary":
        single = [name for name in names if len(set(outcome[groups[name]].tolist())) < 2]
        if single:
            return False, f"dataset {', '.join(single[:3])}{' …' if len(single) > 3 else ''} hold{'s' if len(single) == 1 else ''} one class"
        return True, f"every one of the {len(names)} datasets holds both classes"
    small = [name for name in names if len(groups[name]) < min_subjects]
    if small:
        return False, f"dataset {', '.join(small[:3])}{' …' if len(small) > 3 else ''} {'has' if len(small) == 1 else 'have'} fewer than {min_subjects} subjects"
    return True, f"every one of the {len(names)} datasets has at least {min_subjects} subjects"


def _strata(outcome, datasets, outcome_type, units):
    """{stratum key: unit indices}: dataset x class (binary) or dataset (continuous)."""
    labels = datasets if datasets is not None else ["all"] * len(outcome)
    out = {}
    for index in units:
        key = (labels[index], outcome[index]) if outcome_type == "binary" else (labels[index],)
        out.setdefault(key, []).append(int(index))
    return {key: np.asarray(out[key], dtype=np.int64) for key in sorted(out, key=lambda key: tuple(str(part) for part in key))}


def _kfold_assignment(rng, outcome, datasets, outcome_type, units, k):
    """One stratified k-fold assignment of the units (balanced per stratum and overall)."""
    out = np.full(len(outcome), -1, dtype=np.int64)
    offset = 0
    for members in _strata(outcome, datasets, outcome_type, units).values():
        members = rng.permutation(members)
        if outcome_type == "continuous":
            # Ordered by outcome, every run of k consecutive subjects is spread over the k folds
            members = members[np.argsort(outcome[members], kind="stable")]
            for start in range(0, len(members), k):
                block = members[start : start + k]
                out[block] = rng.permutation(k)[: len(block)]
        else:
            out[members] = (offset + np.arange(len(members))) % k
            offset += len(members)
    mapping = rng.permutation(k)
    out[units] = mapping[out[units]]
    return out


def _folds(scheme, outcome, datasets, outcome_type, seed, k, repeats, counts, requested, reason="", message=None):
    outcome = np.asarray(outcome, dtype=np.float64)
    n = outcome.size
    labels = _dataset_labels(datasets, n)
    units = np.flatnonzero(np.ones(n, dtype=bool) if counts is None else np.asarray(counts) > 0)
    if units.size < 2:
        raise ValueError(f"Cross-validation needs at least 2 subjects, got {units.size}")
    warnings = []
    if scheme == "lodo":
        names = sorted({labels[index] for index in units})
        assignment = np.full((1, n), -1, dtype=np.int64)
        position = {name: index for index, name in enumerate(names)}
        assignment[0, units] = [position[labels[index]] for index in units]
        return Folds("lodo", requested, assignment, [names], None, reason, message, warnings)
    if scheme == "loo":
        assignment = np.full((1, n), -1, dtype=np.int64)
        assignment[0, units] = np.arange(units.size)
        warnings.append(LOO_WARNING)
        return Folds("loo", requested, assignment, [[str(index + 1) for index in range(units.size)]], None, reason, message, warnings)
    k_used = min(int(k), int(units.size))
    if k_used < int(k):
        warnings.append(f"{units.size} subjects: {k_used} folds instead of {k}")
    rng = np.random.default_rng(seed)
    assignment = np.vstack([_kfold_assignment(rng, outcome, labels, outcome_type, units, k_used) for _ in range(int(repeats))])
    return Folds("kfold", requested, assignment, [[str(index + 1) for index in range(k_used)] for _ in range(int(repeats))], k_used, reason, message, warnings)


def make_folds(scheme, outcome, datasets, seed, outcome_type=None, k=DEFAULT_K, repeats=DEFAULT_REPEATS, counts=None, min_lodo_subjects=MIN_LODO_SUBJECTS):
    """Cross-validation folds (addendum §3.8).

    :param scheme: "auto" (LODO when possible, else stratified k-fold), "lodo" (falls back to stratified
      k-fold with a message when LODO is not possible), "kfold" (stratified, ``k`` x ``repeats``) or "loo"
      (warns :data:`LOO_WARNING`).
    :param outcome: (n,) outcome (0/1 for a binary outcome).
    :param datasets: (n,) dataset labels or None.
    :param seed: seed of the k-fold assignments.
    :param counts: (n,) bootstrap multiplicities (0: not in the sample); the folds are made over the subjects
      in the sample, so the copies of a subject always share a fold.
    :return: :class:`Folds`.
    """
    if scheme not in SCHEMES:
        raise ValueError(f"scheme must be one of {', '.join(SCHEMES)}, got {scheme!r}")
    outcome = np.asarray(outcome, dtype=np.float64)
    outcome_type = outcome_type or infer_outcome_type(outcome)
    if outcome_type not in OUTCOME_TYPES:
        raise ValueError(f"outcome_type must be one of {', '.join(OUTCOME_TYPES)}, got {outcome_type!r}")
    kfold_text = f"stratified {k}-fold × {repeats} repeats"  # noqa: RUF001 (a multiplication sign)
    if scheme in ("auto", "lodo"):
        possible, why = lodo_check(outcome, datasets, outcome_type, counts, min_lodo_subjects)
        if possible:
            return _folds("lodo", outcome, datasets, outcome_type, seed, k, repeats, counts, scheme, f"LODO: {why}")
        reason = f"LODO not possible: {why} → {kfold_text}"
        return _folds("kfold", outcome, datasets, outcome_type, seed, k, repeats, counts, scheme, reason, reason if scheme == "lodo" else None)
    if scheme == "loo":
        return _folds("loo", outcome, datasets, outcome_type, seed, k, repeats, counts, scheme, "LOO as requested")
    return _folds("kfold", outcome, datasets, outcome_type, seed, k, repeats, counts, scheme, f"{kfold_text} as requested")


def rerun_folds(folds, outcome, datasets, outcome_type, seed, counts=None):
    """Folds of a bootstrap or permutation rerun: the scheme the observed run used, made again on the new sample."""
    return _folds(folds.scheme, outcome, datasets, outcome_type, seed, folds.k or DEFAULT_K, folds.repeats if folds.scheme == "kfold" else 1, counts, folds.requested, folds.reason, folds.message)


# Metrics


def _weights(n, weights):
    return np.ones(n) if weights is None else np.asarray(weights, dtype=np.float64)


def auc(scores_, labels, weights=None):
    """Area under the ROC curve (Mann-Whitney U / (n1 n0)), ties counting 1/2; ``weights`` are subject multiplicities.

    :return: NaN without both classes.
    """
    s = np.asarray(scores_, dtype=np.float64)
    y = np.asarray(labels, dtype=np.float64)
    w = _weights(s.size, weights)
    valid = np.isfinite(s) & np.isfinite(y) & (w > 0)
    s, y, w = s[valid], y[valid], w[valid]
    positive = y == 1
    w_pos, w_neg = float(w[positive].sum()), float(w[~positive].sum())
    if w_pos <= 0 or w_neg <= 0:
        return float("nan")
    values, inverse = np.unique(s, return_inverse=True)
    pos = np.bincount(inverse, weights=np.where(positive, w, 0.0), minlength=values.size)
    neg = np.bincount(inverse, weights=np.where(positive, 0.0, w), minlength=values.size)
    below = np.concatenate(([0.0], np.cumsum(neg)[:-1]))
    return float(pos @ (below + 0.5 * neg) / (w_pos * w_neg))


def weighted_pearson(a, b, weights=None):
    """Pearson r with subject multiplicities (NaN for a constant vector)."""
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    w = _weights(a.size, weights)
    valid = np.isfinite(a) & np.isfinite(b) & (w > 0)
    a, b, w = a[valid], b[valid], w[valid]
    if w.sum() <= 0 or a.size < 3:
        return float("nan")
    a = a - (w @ a) / w.sum()
    b = b - (w @ b) / w.sum()
    denominator = math.sqrt(float((w * a) @ a) * float((w * b) @ b))
    return float((w * a) @ b) / denominator if denominator > 0 else float("nan")


def oos_r2(y, prediction, baseline, weights=None):
    """Out-of-sample R² = 1 - SS(y - prediction) / SS(y - baseline), the baseline being each subject's training-mean
    prediction. It is negative when the predictions are worse than the training mean.
    """
    y = np.asarray(y, dtype=np.float64)
    prediction = np.asarray(prediction, dtype=np.float64)
    baseline = np.asarray(baseline, dtype=np.float64)
    w = _weights(y.size, weights)
    valid = np.isfinite(y) & np.isfinite(prediction) & np.isfinite(baseline) & (w > 0)
    reference = float(w[valid] @ (y[valid] - baseline[valid]) ** 2)
    if reference <= 0:
        return float("nan")
    return 1.0 - float(w[valid] @ (y[valid] - prediction[valid]) ** 2) / reference


def _sigmoid(eta):
    return 0.5 * (1.0 + np.tanh(0.5 * eta))


def fit_logistic(X, y, weights=None, ridge=RIDGE, max_iter=100, tol=1e-10):
    """Logistic regression by IRLS (Newton) with a ridge penalty and step halving.

    :param X: (n, p) design including the intercept column.
    :return: (p,) coefficients. Under separation the ridge keeps them finite.
    """
    X = np.asarray(X, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    w = _weights(y.size, weights)
    p = X.shape[1]
    beta = np.zeros(p)
    total = w.sum()
    if total > 0 and X.shape[1] and np.allclose(X[:, 0], 1.0):
        prevalence = min(max(float(w @ y) / total, 1e-6), 1 - 1e-6)
        beta[0] = math.log(prevalence / (1 - prevalence))

    def objective(b):
        eta = X @ b
        return float(w @ (y * eta - np.logaddexp(0.0, eta))) - 0.5 * ridge * float(b @ b)

    current = objective(beta)
    penalty = ridge * np.eye(p)
    for _ in range(max_iter):
        mu = _sigmoid(X @ beta)
        gradient = X.T @ (w * (y - mu)) - ridge * beta
        hessian = X.T @ (X * (w * mu * (1 - mu))[:, None]) + penalty
        try:
            step = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError:
            step = np.linalg.lstsq(hessian, gradient, rcond=None)[0]
        scale = 1.0
        for _ in range(40):
            candidate = beta + scale * step
            value = objective(candidate)
            if value >= current - 1e-12 * abs(current):
                break
            scale *= 0.5
        beta, previous, current = candidate, current, value
        if np.max(np.abs(scale * step)) < tol or abs(current - previous) <= 1e-14 * max(abs(current), 1.0):
            break
    return beta


def fit_linear(X, y, weights=None, ridge=RIDGE):
    """Weighted least squares with a ridge penalty: (p,) coefficients (X includes the intercept column)."""
    X = np.asarray(X, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    w = _weights(y.size, weights)
    gram = X.T @ (X * w[:, None]) + ridge * np.eye(X.shape[1])
    try:
        return np.linalg.solve(gram, X.T @ (w * y))
    except np.linalg.LinAlgError:
        return np.linalg.lstsq(gram, X.T @ (w * y), rcond=None)[0]


def odds_ratio_per_sd(prediction, labels, weights=None):
    """Odds ratio per SD of a predictor: exp(slope) of a logistic regression of the labels on the z-scored predictor."""
    x = np.asarray(prediction, dtype=np.float64)
    y = np.asarray(labels, dtype=np.float64)
    w = _weights(x.size, weights)
    valid = np.isfinite(x) & np.isfinite(y) & (w > 0)
    x, y, w = x[valid], y[valid], w[valid]
    if w.sum() <= 0 or len(set(y.tolist())) < 2:
        return float("nan")
    mean = float(w @ x) / w.sum()
    sd = math.sqrt(float(w @ (x - mean) ** 2) / w.sum())
    if sd <= 0:
        return float("nan")
    beta = fit_logistic(np.column_stack([np.ones(x.size), (x - mean) / sd]), y, w)
    return float(math.exp(min(beta[1], 700.0)))


def _standardise(X, train, w):
    """X (n, q) standardised with the training subjects' weighted mean and SD (NaN imputed by the training mean);
    columns constant (or missing) in training are dropped. Returns (n, q') with an intercept column first.
    """
    columns = [np.ones(X.shape[0])]
    for column in X.T:
        finite = np.isfinite(column)
        mask = train & finite
        weight = w[mask]
        if weight.sum() <= 0:
            continue
        mean = float(weight @ column[mask]) / weight.sum()
        sd = math.sqrt(float(weight @ (column[mask] - mean) ** 2) / weight.sum())
        if not sd > 1e-12 * max(abs(mean), 1.0):
            continue
        columns.append(np.where(finite, (column - mean) / sd, 0.0))
    return np.column_stack(columns)


def _fit_predict(X, y, train, test, w, outcome_type):
    """Linear predictor of the held-out subjects from a model fitted on the training subjects."""
    assert_disjoint(train, test)
    Z = _standardise(X, train, w)
    ytrain, wtrain = y[train], w[train]
    if outcome_type == "binary":
        if len(set(ytrain[wtrain > 0].tolist())) < 2:
            prevalence = min(max(float(wtrain @ ytrain) / wtrain.sum(), 1e-6), 1 - 1e-6)
            return np.full(test.size, math.log(prevalence / (1 - prevalence)))
        beta = fit_logistic(Z[train], ytrain, wtrain)
    else:
        beta = fit_linear(Z[train], ytrain, wtrain)
    return Z[test] @ beta


# Data and preparation


@dataclasses.dataclass
class ValidationData:
    """Inputs of :func:`validate` (in memory).

    ``maps``: (n, V) per-lesion network maps (AvgFz), finite. ``outcome``: (n,) 0/1 or scores.
    ``volume``: (n,) lesion volumes (in every model). ``covariates``: (n, k) or None. ``datasets``: (n,)
    labels or None. ``occupancy``: (n, V) scipy.sparse lesion occupancy (damage scores, location baseline) or
    None. ``reference``: {"mu_r", "pc1"} (V,) maps or None. ``refmaps``: {name: (V,) map}. The ``*_reason``
    fields say why an optional input is missing.
    """

    sids: list
    maps: np.ndarray
    outcome: np.ndarray
    outcome_type: str
    volume: np.ndarray
    covariates: np.ndarray | None = None
    covariate_names: tuple = ()
    datasets: list | None = None
    occupancy: object = None
    reference: dict | None = None
    refmaps: dict = dataclasses.field(default_factory=dict)
    occupancy_reason: str | None = None
    reference_reason: str | None = None

    @property
    def n(self):
        return len(self.sids)

    def check(self):
        n = self.n
        if self.maps.shape[0] != n:
            raise ValueError(f"{self.maps.shape[0]} maps for {n} subjects")
        self.outcome = np.asarray(self.outcome, dtype=np.float64)
        self.volume = np.asarray(self.volume, dtype=np.float64)
        if self.outcome.shape != (n,) or self.volume.shape != (n,):
            raise ValueError("outcome and volume need one value per subject")
        if self.outcome_type not in OUTCOME_TYPES:
            raise ValueError(f"outcome_type must be one of {', '.join(OUTCOME_TYPES)}, got {self.outcome_type!r}")
        if not (np.all(np.isfinite(self.outcome)) and np.all(np.isfinite(self.volume))):
            raise ValueError("outcome and volume must be finite (missing values are excluded before validation)")
        if self.outcome_type == "binary" and not np.all(np.isin(self.outcome, (0.0, 1.0))):
            raise ValueError("A binary outcome must be coded 0/1")
        if self.covariates is not None:
            self.covariates = np.asarray(self.covariates, dtype=np.float64).reshape(n, -1)
            if not np.all(np.isfinite(self.covariates)):
                raise ValueError("The covariates must be finite")
        self.datasets = _dataset_labels(self.datasets, n)
        n_voxels = self.maps.shape[1]
        if self.occupancy is not None and tuple(self.occupancy.shape) != (n, n_voxels):
            raise ValueError(f"The occupancy has shape {self.occupancy.shape}, expected ({n}, {n_voxels})")
        for name, vector in [*(self.reference or {}).items(), *self.refmaps.items()]:
            if np.asarray(vector).shape != (n_voxels,) or not np.all(np.isfinite(vector)):
                raise ValueError(f"The map {name} must be finite with {n_voxels} voxels")
        return self


@dataclasses.dataclass
class _Predictor:
    name: str
    label: str
    role: str  # "circuit" or "baseline"
    kind: str  # "volume", "circuit", "circuit_partial", "fixed", "location"
    status: str = "computed"
    reason: str | None = None
    fixed: np.ndarray | None = None  # (n,) score of a fixed map

    def to_dict(self):
        return {"name": self.name, "label": self.label, "role": self.role, "status": self.status, "reason": self.reason}


@dataclasses.dataclass
class _Prepared:
    data: ValidationData
    circuit: str
    score: str
    top_pct: float
    predictors: list
    nuisance: np.ndarray  # (n, q) volume and covariates
    voxel_mean: np.ndarray  # (V,) per-voxel mean over all subjects: a constant shift for conditioning only (t is invariant)
    bases: list
    lesion_terms: tuple
    gram: np.ndarray | None  # (n, n) occupancy inner products (lesion-location scores)
    occupancy_csc: object
    bases_pass: list = dataclasses.field(default_factory=list)  # the bases in the pass precision


def _map_score(prep_maps, data, vector, score, circuit, top_pct, bases_terms):
    """(n,) score of every lesion with a fixed map (transformed like the circuit: top-x% ROI for topk circuits)."""
    vector = np.asarray(vector, dtype=np.float64)
    if circuit == "topk":
        vector = scores.top_pct_roi(vector, top_pct).astype(np.float64)
    if score == "damage":
        return scores.damage_matrix(data.occupancy, vector[None, :])[:, 0]
    if score == "roi":
        return scores.roi_matrix(prep_maps, vector[None, :])[:, 0]
    bases, lesion = bases_terms
    cross = scores.cross_products(prep_maps, vector[None, :])
    return scores.fisher_z(scores.similarity_from_terms(cross, lesion, scores.map_terms(vector[None, :], bases), 0)[:, 0])


def _prepare(data, circuit, score, top_pct):
    data.check()
    if circuit not in CIRCUITS:
        raise ValueError(f"circuit must be one of {', '.join(CIRCUITS)}, got {circuit!r}")
    score = score or ("roi" if circuit == "topk" else "similarity")
    if score not in SCORE_KINDS:
        raise ValueError(f"score must be one of {', '.join(SCORE_KINDS)}, got {score!r}")
    if score == "roi" and circuit != "topk":
        raise ValueError("ROI connectivity needs a top-x% circuit (circuit 'topk')")
    if score == "damage" and data.occupancy is None:
        raise ValueError(f"Damage scores need the lesion occupancy ({data.occupancy_reason or 'not given'})")
    Y = data.maps
    n_voxels = Y.shape[1]
    reference = data.reference if data.reference and all(key in data.reference for key in REFERENCE_KEYS) else None
    covariates = np.vstack([np.asarray(reference[key], dtype=np.float64) for key in REFERENCE_KEYS]) if reference else None
    bases = [scores.basis(None, n_voxels)] + ([scores.basis(covariates, n_voxels)] if reference else [])
    lesion = scores.lesion_terms(Y, bases)
    voxel_mean = np.zeros(n_voxels)
    for part in scores._chunks(n_voxels, CHUNK_VOXELS):
        voxel_mean[part] = np.asarray(Y[:, part], dtype=np.float64).mean(axis=0)
    nuisance = [data.volume[:, None]]
    if data.covariates is not None and data.covariates.size:
        nuisance.append(data.covariates)
    nuisance = np.hstack(nuisance)
    gram = None
    if data.occupancy is not None:
        gram = data.occupancy @ data.occupancy.T
        gram = np.asarray(gram.toarray() if hasattr(gram, "toarray") else gram, dtype=np.float64)
    terms = (bases, lesion)

    score_label = {"similarity": "Fisher-z spatial r", "damage": "damage score", "roi": "ROI connectivity"}[score]
    predictors = [_Predictor("circuit", f"Circuit ({CIRCUIT_LABELS[circuit]}; {score_label})", "circuit", "circuit")]
    if score == "similarity":
        partial = _Predictor("circuit_partial", "Circuit, partial μC_r and PC1 (Fisher-z partial spatial r)", "circuit", "circuit_partial")
        if reference is None:
            partial.status, partial.reason = "not computed", data.reference_reason or "no reference maps (μC_r, PC1) for this connectome"
        predictors.append(partial)
    predictors.append(_Predictor("volume", "Lesion volume", "baseline", "volume"))
    for key, label in (("mu_r", "μC_r (degree)"), ("pc1", "PC1")):
        item = _Predictor(key, label, "baseline", "fixed")
        if reference is None:
            item.status, item.reason = "not computed", data.reference_reason or "no reference maps (μC_r, PC1) for this connectome"
        else:
            item.fixed = _map_score(Y, data, reference[key], score, circuit, top_pct, terms)
        predictors.append(item)
    location = _Predictor("location", "Lesion location (in-fold proportional subtraction)" if data.outcome_type == "binary" else "Lesion location (in-fold outcome-weighted occupancy)", "baseline", "location")
    if gram is None:
        location.status, location.reason = "not computed", data.occupancy_reason or "no lesion occupancy (weights.npz) for these maps"
    predictors.append(location)
    for name, vector in data.refmaps.items():
        predictors.append(_Predictor(f"ref:{name}", f"Reference map {name}", "baseline", "fixed", fixed=_map_score(Y, data, vector, score, circuit, top_pct, terms)))
    occupancy_csc = data.occupancy.tocsc() if data.occupancy is not None and score == "damage" else None
    return _Prepared(data, circuit, score, float(top_pct), predictors, nuisance, voxel_mean, bases, lesion, gram, occupancy_csc, [b.astype(PASS_DTYPE) for b in bases])


# One pass of cross-validations


@dataclasses.dataclass
class _Run:
    """One cross-validation: an outcome vector, subject multiplicities, folds and the seed of the inner blocks."""

    y: np.ndarray
    counts: np.ndarray
    folds: Folds
    inner_seed: int = 0


@dataclasses.dataclass
class _RunContext:
    run: _Run
    X: np.ndarray  # (n, p) design [1, outcome, volume, covariates]
    Xw: np.ndarray  # X times the multiplicities
    w: np.ndarray
    blocks: np.ndarray  # (repeats, n) inner block of every subject in the sample, -1 outside it
    n_blocks: int


@dataclasses.dataclass
class _Item:
    """One outer fold of one run."""

    run: int
    repeat: int
    fold: int
    test: np.ndarray
    train: np.ndarray


@dataclasses.dataclass
class _Job:
    """One in-fold map: the outer circuit of an item (block -1) or one of its inner (cross-fitting) circuits."""

    item: int
    block: int
    train: np.ndarray
    rows: np.ndarray  # the subjects scored with this map
    inv: np.ndarray | None = None
    df: float = 0.0
    valid: bool = True


def _design(prep, y):
    """[1, outcome, volume, covariates] centred and scaled with constants (conditioning only: an OLS t of the outcome
    is invariant to an affine recoding of the columns when the intercept is in the model).
    """
    X = np.column_stack([y, prep.nuisance])
    X = X - X.mean(axis=0)
    scale = np.sqrt((X * X).mean(axis=0))
    X = X / np.where(scale > 0, scale, 1.0)
    return np.column_stack([np.ones(X.shape[0]), X])


def _context(prep, run):
    X = _design(prep, run.y)
    w = np.asarray(run.counts, dtype=np.float64)
    units = np.flatnonzero(w > 0)
    n_blocks = max(2, min(INNER_FOLDS, units.size))
    rng = np.random.default_rng(run.inner_seed)
    blocks = np.vstack([_kfold_assignment(rng, run.y, prep.data.datasets, prep.data.outcome_type, units, n_blocks) for _ in range(run.folds.repeats)])
    return _RunContext(run, X, X * w[:, None], w, blocks, n_blocks)


def _job_setup(job, item, X, w):
    """XᵀWX, its pseudoinverse and the residual df of a map's training subjects; the leakage assertions."""
    assert_disjoint(job.train, item.test)
    assert_disjoint(job.train, job.rows)
    weight = np.where(job.train, w, 0.0)
    gram = X.T @ (X * weight[:, None])
    values, vectors = np.linalg.eigh(gram)
    keep = values > max(float(values.max(initial=0.0)), _TINY) * X.shape[1] * np.finfo(float).eps * 1e3
    job.df = float(weight.sum()) - int(np.count_nonzero(keep))
    job.inv = (vectors[:, keep] / values[keep]) @ vectors[:, keep].T  # pseudoinverse
    x = X[weight > 0, 1]
    job.valid = job.df >= 1 and x.size > 1 and float(np.ptp(x)) > 0 and job.inv[1, 1] > 0


def _maps_from_sums(inv, df, valid, xty, yy, kind):
    """In-fold maps (t, or partial r for sLNM) of J jobs on a voxel chunk from their training sums.

    :param inv: (J, p, p) pseudoinverses of XᵀWX; :param df: (J,) residual df; :param valid: (J,) usable fits.
    :param xty: (J, p, c) XᵀWY; :param yy: (J, c) yᵀWy (Y centred per voxel with a constant).
    :return: (J, c) maps; 0 for an invalid job or a voxel without residual variance.
    """
    dtype = xty.dtype
    inv = inv.astype(dtype, copy=False)
    df = np.maximum(df, 1.0).astype(dtype)
    beta = np.matmul(inv, xty)
    sse = yy - np.einsum("jpv,jpv->jv", beta, xty)
    usable = (sse > SSE_RTOL[np.dtype(dtype)] * yy) & valid[:, None]
    t = np.zeros(yy.shape, dtype=dtype)
    np.divide(beta[:, 1], np.sqrt(np.maximum(sse, 0) / df[:, None] * inv[:, 1, 1][:, None]), out=t, where=usable)
    if kind == "slnm":
        t /= np.sqrt(t * t + df[:, None])
    return t


class _ScoreAccumulator:
    """Accumulates, over voxel chunks, what the scores of every job's rows need: the cross products with the rows'
    lesion maps (one GEMM per group of jobs sharing a superset of rows), the maps' own terms (sum of squares and
    projections on the similarity bases), |map| sums (ROI connectivity) and the damage scores.
    """

    def __init__(self, prep, jobs, groups):
        self.prep = prep
        self.jobs = jobs
        self.groups = groups  # [(row superset, job indices, positions of each job's rows in the superset)]
        self.products = [np.zeros((rows.size, len(members))) for rows, members, _ in groups]
        self.squares = np.zeros(len(jobs))
        self.projections = [np.zeros((len(jobs), b.shape[1])) for b in prep.bases]
        self.abs_sums = np.zeros(len(jobs))
        self.damage = np.zeros((prep.data.n, len(jobs))) if prep.score == "damage" else None

    def add(self, part, raw, maps):
        """Add a voxel chunk: ``raw`` (n, c) lesion maps and ``maps`` (jobs, c), in the pass precision (sums in float64)."""
        self.squares += np.einsum("jv,jv->j", maps, maps, dtype=np.float64)
        for out, b in zip(self.projections, self.prep.bases_pass, strict=True):
            out += maps @ b[part]
        if self.prep.score == "roi":
            self.abs_sums += np.abs(maps).sum(axis=1, dtype=np.float64)
        for (rows, members, _), out in zip(self.groups, self.products, strict=True):
            out += raw[rows] @ maps[members].T
        if self.damage is not None:
            self.damage += np.asarray(self.prep.occupancy_csc[:, part] @ maps.T.astype(np.float64))

    def cross(self):
        """The cross product of every job's map with each of its rows' lesion maps."""
        out = [None] * len(self.jobs)
        for (_, members, positions), products in zip(self.groups, self.products, strict=True):
            for column, (index, where) in enumerate(zip(members, positions, strict=True)):
                out[index] = products[where, column]
        return out

    def write(self, out):
        """out[kind][item][rows] = the scores of every job (NaN for an invalid fit)."""
        prep = self.prep
        cross = self.cross()
        yy, y_proj = prep.lesion_terms
        for index, job in enumerate(self.jobs):
            rows = job.rows
            values = {}
            if prep.score == "damage":
                values["circuit"] = self.damage[rows, index]
            elif prep.score == "roi":
                values["circuit"] = cross[index] / self.abs_sums[index] if self.abs_sums[index] > 0 else np.full(rows.size, np.nan)
            else:
                maps = (self.squares[index : index + 1], [p[index : index + 1] for p in self.projections])
                lesion = (yy[rows], [p[rows] for p in y_proj])
                values["circuit"] = scores.fisher_z(scores.similarity_from_terms(cross[index][:, None], lesion, maps, 0)[:, 0])
                if len(prep.bases) > 1:
                    values["circuit_partial"] = scores.fisher_z(scores.similarity_from_terms(cross[index][:, None], lesion, maps, 1)[:, 0])
            for key, value in values.items():
                out[key][job.item][rows] = value if job.valid else np.nan


def _row_groups(contexts, items, jobs):
    """Jobs grouped by a shared superset of rows (the inner block of a run and repeat, or an outer test fold), so that
    each chunk's cross products are one GEMM per group.
    """
    groups = {}
    for index, job in enumerate(jobs):
        item = items[job.item]
        if job.block < 0:
            key, superset = ("test", job.item), job.rows
        else:
            key = ("block", item.run, item.repeat, job.block)
            superset = np.flatnonzero(contexts[item.run].blocks[item.repeat] == job.block)
        entry = groups.setdefault(key, (superset, [], []))
        entry[1].append(index)
        entry[2].append(np.searchsorted(superset, job.rows))
    return [(rows, np.asarray(members), positions) for rows, members, positions in groups.values()]


def _cell_operators(context, repeat):
    """Sparse operators summing the subjects of every (fold, inner block) cell of a repeat: A (cells p x n) with the
    weighted design rows, A0 (cells x n) with the weights. Returns (A, A0, n folds).
    """
    from scipy import sparse

    folds = context.run.folds.assignment[repeat]
    blocks = context.blocks[repeat]
    n_folds = context.run.folds.n_folds(repeat)
    units = np.flatnonzero((folds >= 0) & (context.w > 0))
    cells = folds[units] * context.n_blocks + blocks[units]
    p = context.X.shape[1]
    n_cells = n_folds * context.n_blocks
    A = sparse.csr_matrix((context.Xw[units].ravel().astype(PASS_DTYPE), ((cells[:, None] * p + np.arange(p)).ravel(), np.repeat(units, p))), shape=(n_cells * p, context.X.shape[0]))
    A0 = sparse.csr_matrix((context.w[units].astype(PASS_DTYPE), (cells, units)), shape=(n_cells, context.X.shape[0]))
    return A, A0, n_folds


def _score_items(prep, contexts, items, out):
    """Cross-fitted circuit scores of the items (outer folds): an item's held-out subjects are scored with the outer
    circuit (all its training subjects) and each training subject with the inner circuit that left its block out.

    The training sums of every map come from the (fold, inner block) cell sums S of the repeat: outer circuit of
    fold f: S_total - S_f; inner circuit (f, g): S_total - S_f - (S_block g - S_(f, g)). Writes out[kind][item][rows].
    """
    Y = prep.data.maps
    n_voxels = Y.shape[1]
    kind = "slnm" if prep.circuit == "slnm" else "glm"
    jobs = []
    for index, item in enumerate(items):
        context = contexts[item.run]
        blocks = context.blocks[item.repeat]
        jobs.append(_Job(index, -1, item.train, item.test))
        for block in range(context.n_blocks):
            rows = np.flatnonzero(item.train & (blocks == block))
            if rows.size:
                jobs.append(_Job(index, block, item.train & (blocks != block), rows))
    for job in jobs:
        context = contexts[items[job.item].run]
        _job_setup(job, items[job.item], context.X, context.w)
    # Per (run, repeat): every (fold, map) cell, map 0 the outer circuit and map g + 1 the inner circuit of block g,
    # with the fits of the cells that have a job (the others stay invalid and are not used)
    p = contexts[items[0].run].X.shape[1]
    plan = {}
    for index, job in enumerate(jobs):
        item = items[job.item]
        key = (item.run, item.repeat)
        if key not in plan:
            A, A0, n_folds = _cell_operators(contexts[item.run], item.repeat)
            cells = n_folds * (contexts[item.run].n_blocks + 1)
            plan[key] = {"A": A, "A0": A0, "n_folds": n_folds, "inv": np.zeros((cells, p, p)), "df": np.ones(cells), "valid": np.zeros(cells, dtype=bool), "jobs": [], "cells": []}
        entry = plan[key]
        cell = item.fold * (contexts[item.run].n_blocks + 1) + job.block + 1
        entry["inv"][cell], entry["df"][cell], entry["valid"][cell] = job.inv, max(job.df, 1.0), job.valid
        entry["jobs"].append(index)
        entry["cells"].append(cell)
    itemsize = np.dtype(PASS_DTYPE).itemsize
    per_voxel = sum(entry["inv"].shape[0] * (2 * p + 4) * itemsize for entry in plan.values()) + len(jobs) * itemsize
    chunk = int(min(CHUNK_VOXELS, max(256, PASS_BYTES // max(per_voxel, 1))))
    accumulator = _ScoreAccumulator(prep, jobs, _row_groups(contexts, items, jobs))
    store = prep.circuit == "topk"
    stored = np.zeros((len(jobs), n_voxels), dtype=np.float32) if store else None
    voxel_mean = prep.voxel_mean.astype(PASS_DTYPE)
    for part in scores._chunks(n_voxels, chunk):
        raw = np.asarray(Y[:, part], dtype=PASS_DTYPE)
        centred = raw - voxel_mean[part]
        squares = centred * centred
        maps = np.empty((len(jobs), raw.shape[1]), dtype=PASS_DTYPE)
        for key, entry in plan.items():
            n_folds, m = entry["n_folds"], contexts[key[0]].n_blocks
            S = np.asarray(entry["A"] @ centred).reshape(n_folds, m, p, -1)
            Q = np.asarray(entry["A0"] @ squares).reshape(n_folds, m, -1)
            xty = np.empty((n_folds, m + 1) + S.shape[2:], dtype=PASS_DTYPE)
            yy = np.empty((n_folds, m + 1, S.shape[3]), dtype=PASS_DTYPE)
            np.subtract(S.sum(axis=(0, 1)), S.sum(axis=1), out=xty[:, 0])  # outer: all minus the held-out fold
            np.subtract(Q.sum(axis=(0, 1)), Q.sum(axis=1), out=yy[:, 0])
            np.add(xty[:, :1] - S.sum(axis=0)[None], S, out=xty[:, 1:])  # inner g: minus block g, the held-out part of it back
            np.add(yy[:, :1] - Q.sum(axis=0)[None], Q, out=yy[:, 1:])
            cells = _maps_from_sums(entry["inv"], entry["df"], entry["valid"], xty.reshape(-1, p, xty.shape[-1]), yy.reshape(-1, yy.shape[-1]), kind)
            maps[entry["jobs"]] = cells[entry["cells"]]
        if store:
            stored[:, part] = maps
        else:
            accumulator.add(part, raw, maps)
    if store:
        rois = np.zeros((len(jobs), n_voxels), dtype=np.float32)
        for index, job in enumerate(jobs):
            if job.valid:
                rois[index] = scores.top_pct_roi(stored[index], prep.top_pct)
        del stored
        for part in scores._chunks(n_voxels, chunk):
            accumulator.add(part, np.asarray(Y[:, part], dtype=PASS_DTYPE), rois[:, part].astype(PASS_DTYPE))
    accumulator.write(out)


def _location_items(prep, contexts, items):
    """Cross-fitted lesion-location scores (n,) per item, from the occupancy inner products.

    A map is sum_i a_i occupancy_i over its training lesions (proportional subtraction: a_i = 1 / n_cases for cases
    and -1 / n_controls for controls; scores: a_i = (y_i - mean) / n), so a lesion's damage score with it is
    sum_i a_i <occupancy_i, occupancy_j>. Held-out lesions are scored with the map of all the item's training lesions,
    training lesions with the map that leaves their inner block out (as the circuits).
    """
    out = []
    for item in items:
        context = contexts[item.run]
        run, blocks = context.run, context.blocks[item.repeat]
        maps = [(item.train, item.test)] + [(item.train & (blocks != block), np.flatnonzero(item.train & (blocks == block))) for block in range(context.n_blocks)]
        values = np.full(prep.data.n, np.nan)
        for train, rows in maps:
            if rows.size == 0:
                continue
            assert_disjoint(train, item.test)
            assert_disjoint(train, rows)
            w = np.where(train, context.w, 0.0)
            coefficients = np.zeros(prep.data.n)
            if prep.data.outcome_type == "binary":
                cases, controls = w * (run.y == 1), w * (run.y == 0)
                if cases.sum() > 0 and controls.sum() > 0:
                    coefficients = cases / cases.sum() - controls / controls.sum()
            elif w.sum() > 0:
                coefficients = w * (run.y - float(w @ run.y) / w.sum()) / w.sum()
            if np.any(coefficients[item.test] != 0) or np.any(coefficients[rows] != 0):
                raise LeakageError("A scored lesion entered its own lesion-location map")
            values[rows] = prep.gram[rows] @ coefficients
        out.append(values)
    return out


def _passes(prep, contexts, items):
    """Items split into passes over the maps of at most MAX_MAPS_PER_PASS in-fold maps (and STORE_BYTES of stored maps
    for top-x% circuits, whose threshold needs the whole map).
    """
    limit = MAX_MAPS_PER_PASS
    if prep.circuit == "topk":
        limit = max(1, min(limit, STORE_BYTES // max(4 * prep.data.maps.shape[1], 1)))
    out, current, size = [], [], 0
    for item in items:
        maps = 1 + contexts[item.run].n_blocks
        if current and size + maps > limit:
            out.append(current)
            current, size = [], 0
        current.append(item)
        size += maps
    if current:
        out.append(current)
    return out


def _cv_runs(prep, run_list, keep=False):
    """Cross-validate every run of ``run_list`` (one dict of metrics, and the predictions with ``keep``, per run)."""
    contexts = [_context(prep, run) for run in run_list]
    items = []
    for index, run in enumerate(run_list):
        w = np.asarray(run.counts, dtype=np.float64)
        for repeat, fold, test, train in run.folds.iter_folds():
            items.append(_Item(index, repeat, fold, test, train & (w > 0)))
    kinds = ["circuit"] + (["circuit_partial"] if prep.score == "similarity" and len(prep.bases) > 1 else [])
    circuit_scores = {key: [np.full(prep.data.n, np.nan) for _ in items] for key in kinds}
    position = {id(item): index for index, item in enumerate(items)}
    for group in _passes(prep, contexts, items):
        local = {key: [values[position[id(item)]] for item in group] for key, values in circuit_scores.items()}
        _score_items(prep, contexts, group, local)
    location = _location_items(prep, contexts, items) if prep.gram is not None else None
    results = []
    for index, run in enumerate(run_list):
        own = [(position[id(item)], item) for item in items if item.run == index]
        result = _models(prep, run, own, circuit_scores, location, keep)
        if keep:
            result["folds_scored"] = {
                "items": [(item.repeat, item.fold) for _, item in own],
                "blocks": contexts[index].blocks,
                "circuit": {key: [values[i] for i, _ in own] for key, values in circuit_scores.items()},
                "location": None if location is None else [location[i] for i, _ in own],
            }
        results.append(result)
    return results


def fold_map(data, outcome, train, circuit="glm", weights=None, top_pct=DEFAULT_TOP_PCT):
    """The in-fold map of one training set, computed directly (glm.fit on the training rows only): the reference
    of the batched pass, which must give the same maps (tested). Unthresholded t (glm), partial r (slnm) or the
    top-x% ROI of t (topk); never a permutation threshold.

    :param train: boolean mask or indices of the training subjects; :param weights: multiplicities (rows repeated).
    """
    from . import glm

    index = _index_set(train)
    counts = np.ones(index.size, dtype=np.int64) if weights is None else np.asarray(weights)[index].astype(np.int64)
    rows = np.repeat(index, counts)
    nuisance = [data.volume[:, None]] + ([data.covariates] if data.covariates is not None and data.covariates.size else [])
    X = np.column_stack([np.ones(data.n), np.asarray(outcome, dtype=np.float64), *nuisance])[rows]
    contrast = np.zeros(X.shape[1])
    contrast[1] = 1.0
    fitted = glm.fit(np.asarray(data.maps[rows], dtype=np.float64), X, contrast)
    t = fitted.stat
    if circuit == "slnm":
        return t / np.sqrt(t * t + fitted.df)
    if circuit == "topk":
        return scores.top_pct_roi(t, top_pct).astype(np.float64)
    return t


#: Elements (problems x subjects) of one batch of model fits
MODEL_BATCH_ELEMENTS = 4_000_000


def _standardise_batch(X, W):
    """Batched :func:`_standardise`: X (B, n, m) features (NaN allowed), W (B, n) training weights.

    Every problem's features are standardised with its training subjects' weighted mean and SD; NaN becomes 0
    (the training mean) and a feature constant or missing in training becomes 0 (the ridge gives it no weight).
    Returns (B, n, m + 1) with the intercept column first.
    """
    finite = np.isfinite(X)
    values = np.where(finite, X, 0.0)
    weights = W[:, :, None] * finite
    total = weights.sum(axis=1)
    safe = np.maximum(total, _TINY)
    mean = (weights * values).sum(axis=1) / safe
    sd = np.sqrt((weights * (values - mean[:, None, :]) ** 2).sum(axis=1) / safe)
    usable = (total > 0) & (sd > 1e-12 * np.maximum(np.abs(mean), 1.0))
    Z = np.where(finite & usable[:, None, :], (values - mean[:, None, :]) / np.where(usable, sd, 1.0)[:, None, :], 0.0)
    return np.concatenate([np.ones(X.shape[:2] + (1,)), Z], axis=2)


def _logistic_objective(Z, W, y, beta, ridge):
    eta = np.einsum("bnq,bq->bn", Z, beta)
    return np.einsum("bn,bn->b", W, y * eta - np.logaddexp(0.0, eta)) - 0.5 * ridge * np.einsum("bq,bq->b", beta, beta)


def fit_logistic_batch(Z, y, W, ridge=RIDGE, max_iter=60, tol=1e-7):
    """Logistic regressions of one outcome on B designs at once (IRLS with ridge and step halving, as :func:`fit_logistic`).

    :param Z: (B, n, q) designs with the intercept column first; :param y: (n,) 0/1; :param W: (B, n) weights.
    :return: (B, q) coefficients. A problem whose training subjects hold one class gets the intercept-only fit
      at its (clipped) prevalence.
    """
    B, _, q = Z.shape
    beta = np.zeros((B, q))
    positive = W @ (y == 1)
    negative = W @ (y == 0)
    prevalence = np.clip(positive / np.maximum(positive + negative, _TINY), 1e-6, 1 - 1e-6)
    beta[:, 0] = np.log(prevalence / (1 - prevalence))
    active = (positive > 0) & (negative > 0)
    eye = ridge * np.eye(q)
    for _ in range(max_iter):
        index = np.flatnonzero(active)
        if index.size == 0:
            break
        Zi, Wi, bi = Z[index], W[index], beta[index]
        mu = _sigmoid(np.einsum("bnq,bq->bn", Zi, bi))
        gradient = np.einsum("bnq,bn->bq", Zi, Wi * (y - mu)) - ridge * bi
        hessian = np.einsum("bnq,bnr->bqr", Zi * (Wi * mu * (1 - mu))[:, :, None], Zi) + eye
        try:
            step = np.linalg.solve(hessian, gradient[:, :, None])[:, :, 0]
        except np.linalg.LinAlgError:
            step = np.stack([np.linalg.lstsq(h, g, rcond=None)[0] for h, g in zip(hessian, gradient, strict=True)])
        current = _logistic_objective(Zi, Wi, y, bi, ridge)
        scale = np.ones(index.size)
        candidate = bi + step
        value = _logistic_objective(Zi, Wi, y, candidate, ridge)
        for _ in range(40):
            worse = value < current - 1e-9 * np.maximum(np.abs(current), 1.0)
            if not worse.any():
                break
            scale[worse] *= 0.5
            candidate[worse] = bi[worse] + scale[worse, None] * step[worse]
            value[worse] = _logistic_objective(Zi[worse], Wi[worse], y, candidate[worse], ridge)
        beta[index] = candidate
        moved = np.max(np.abs(scale[:, None] * step), axis=1)
        active[index[(moved < tol) | (np.abs(value - current) <= 1e-12 * np.maximum(np.abs(value), 1.0))]] = False
    return beta


def fit_linear_batch(Z, y, W, ridge=RIDGE):
    """Weighted ridge least squares of one outcome on B designs at once: (B, q) coefficients."""
    q = Z.shape[2]
    gram = np.einsum("bnq,bnr->bqr", Z * W[:, :, None], Z) + ridge * np.eye(q)
    rhs = np.einsum("bnq,bn->bq", Z, W * y)
    return np.linalg.solve(gram, rhs[:, :, None])[:, :, 0]


def _models(prep, run, own, circuit_scores, location, keep):
    """Out-of-fold predictions of every predictor and their metrics, for one run (all its fits batched).

    :param own: [(item index, item)] of the run's outer folds; ``circuit_scores[kind][item index]`` and
      ``location[item index]`` are the cross-fitted (n,) scores of the item.
    """
    data = prep.data
    n = data.n
    y = run.y
    w = np.asarray(run.counts, dtype=np.float64)
    repeats = run.folds.repeats
    computed = [item for item in prep.predictors if item.status == "computed"]
    predictions = {item.name: np.full((repeats, n), np.nan) for item in computed}
    held_out = {name: np.full((repeats, n), np.nan) for name in predictions if name in ("circuit", "circuit_partial", "location")}
    baseline = np.full((repeats, n), np.nan)
    for index, fold in own:
        train, test = fold.train, fold.test
        wt = w[train]
        baseline[fold.repeat, test] = float(wt @ y[train]) / wt.sum() if wt.sum() > 0 else np.nan
        for name, values in held_out.items():
            column = location[index] if name == "location" else circuit_scores[name][index]
            values[fold.repeat, test] = column[test]
    q0 = prep.nuisance.shape[1]
    group = max(1, MODEL_BATCH_ELEMENTS // max(n * len(computed), 1))
    for start in range(0, len(own), group):
        part = own[start : start + group]
        X = np.empty((len(part) * len(computed), n, q0 + 1))
        X[:, :, :q0] = prep.nuisance[None, :, :]
        W = np.zeros((X.shape[0], n))
        for f, (index, fold) in enumerate(part):
            assert_disjoint(fold.train, fold.test)
            for p, item in enumerate(computed):
                b = f * len(computed) + p
                if item.kind == "volume":
                    column = 0.0
                elif item.kind in ("circuit", "circuit_partial"):
                    column = circuit_scores[item.kind][index]
                elif item.kind == "location":
                    column = location[index]
                else:
                    column = item.fixed
                X[b, :, q0] = column
                W[b] = np.where(fold.train, w, 0.0)
        Z = _standardise_batch(X, W)
        beta = fit_logistic_batch(Z, y, W) if data.outcome_type == "binary" else fit_linear_batch(Z, y, W)
        eta = np.einsum("bnq,bq->bn", Z, beta)
        for f, (_, fold) in enumerate(part):
            for p, item in enumerate(computed):
                predictions[item.name][fold.repeat, fold.test] = eta[f * len(computed) + p, fold.test]
    metrics = _metrics_all(predictions, y, w, baseline, data.outcome_type)
    out = {"metrics": metrics}
    if keep:
        out.update({"predictions": predictions, "held_out": held_out, "baseline": baseline})
    return out


def _odds_ratios(rows, y, w):
    """Odds ratio per SD of every row of ``rows`` (B, n; NaN = not predicted), as :func:`odds_ratio_per_sd`, batched."""
    valid = np.isfinite(rows)
    W = np.where(valid, w[None, :], 0.0)
    total = W.sum(axis=1)
    values = np.where(valid, rows, 0.0)
    mean = (W * values).sum(axis=1) / np.maximum(total, _TINY)
    sd = np.sqrt((W * (values - mean[:, None]) ** 2).sum(axis=1) / np.maximum(total, _TINY))
    usable = (total > 0) & (sd > 0) & ((W * (y == 1)).sum(axis=1) > 0) & ((W * (y == 0)).sum(axis=1) > 0)
    out = np.full(rows.shape[0], np.nan)
    if not usable.any():
        return out
    z = np.where(valid[usable], (values[usable] - mean[usable, None]) / sd[usable, None], 0.0)
    Z = np.stack([np.ones_like(z), z], axis=2)
    beta = fit_logistic_batch(Z, y, W[usable])
    out[usable] = np.exp(np.minimum(beta[:, 1], 700.0))
    return out


def _metrics_all(predictions, y, w, baseline, outcome_type):
    """{predictor: metrics} of the out-of-fold predictions, per repeat then averaged over repeats (the OR on the log scale)."""
    names = list(predictions)
    out = {}
    if outcome_type == "binary":
        stacked = np.vstack([predictions[name] for name in names])
        ratios = _odds_ratios(stacked, y, w).reshape(len(names), -1)
        for index, name in enumerate(names):
            aucs = np.array([auc(row[np.isfinite(row)], y[np.isfinite(row)], w[np.isfinite(row)]) for row in predictions[name]])
            aucs, logs = aucs[np.isfinite(aucs)], np.log(ratios[index][np.isfinite(ratios[index]) & (ratios[index] > 0)])
            out[name] = {"auc": float(aucs.mean()) if aucs.size else float("nan"), "or_per_sd": float(np.exp(logs.mean())) if logs.size else float("nan")}
        return out
    for name in names:
        rs, r2s = [], []
        for repeat, row in enumerate(predictions[name]):
            valid = np.isfinite(row)
            rs.append(weighted_pearson(row[valid], y[valid], w[valid]))
            r2s.append(oos_r2(y[valid], row[valid], baseline[repeat][valid], w[valid]))
        rs, r2s = np.array(rs), np.array(r2s)
        out[name] = {"r": float(rs[np.isfinite(rs)].mean()) if np.isfinite(rs).any() else float("nan"), "r2": float(r2s[np.isfinite(r2s)].mean()) if np.isfinite(r2s).any() else float("nan")}
    return out


# Resampling


def bootstrap_counts(rng, outcome, datasets, outcome_type):
    """Multiplicities of a bootstrap sample: subjects drawn with replacement within each stratum (dataset x class)."""
    counts = np.zeros(len(outcome), dtype=np.int64)
    for members in _strata(np.asarray(outcome, dtype=np.float64), datasets, outcome_type, np.arange(len(outcome))).values():
        counts += np.bincount(rng.choice(members, size=members.size, replace=True), minlength=len(outcome))
    return counts


def permute_within(rng, outcome, datasets):
    """The outcome permuted within each dataset (freely without datasets)."""
    outcome = np.asarray(outcome, dtype=np.float64)
    out = outcome.copy()
    labels = datasets if datasets is not None else ["all"] * outcome.size
    groups = {}
    for index, label in enumerate(labels):
        groups.setdefault(label, []).append(index)
    for members in groups.values():
        members = np.asarray(members)
        out[members] = outcome[rng.permutation(members)]
    return out


def _runs_per_call(contexts_maps):
    """Reruns cross-validated per call (between progress reports and Stop checks)."""
    return max(1, MAX_MAPS_PER_PASS // max(contexts_maps, 1))


def bootstrap_interval(values, observed):
    """Whole-CV bootstrap interval: the bias-corrected (BC) percentile interval (Efron 1981), widened to contain the
    observed value, or the plain percentile interval with fewer than ``MIN_BC_BOOT`` bootstrap values.

    A bootstrap sample holds about 63% unique subjects (the copies of a subject share a fold, so nothing leaks), so
    its cross-validated performance is lower than the observed one at n, while a difference between two models run
    on the same samples (ΔAUC, ΔR²) is centred close to its observed value. The BC interval takes the percentiles
    Φ(2 z0 ± 1.96) of the bootstrap values, z0 = Φ⁻¹(share of bootstrap values below the observed one, ties
    counting half): the plain percentile interval when the bootstrap is centred on the observed value, shifted
    towards it when it is not. With few bootstrap values z0 is clipped and the BC percentiles degenerate, so the
    plain percentile interval is used; either interval is widened to contain the observed value when it does not
    (the BC interval can exclude it when |z0| > 1.96). :func:`interval_details` also reports the plain percentile
    interval, the median, z0 and the method used ("bc", "percentile", plus "+observed" when widened).

    :return: [low, high] (NaN without at least 2 finite bootstrap values).
    """
    return interval_details(values, observed)["ci"]


def interval_details(values, observed):
    from scipy.special import ndtr, ndtri

    values = np.asarray(values, dtype=np.float64)
    values = np.sort(values[np.isfinite(values)])
    nan = [float("nan"), float("nan")]
    if values.size < 2:
        return {"ci": nan, "ci_percentile": nan, "boot_median": float("nan"), "z0": None, "n_valid": int(values.size), "method": None}
    alpha = (1.0 - CI_LEVEL) / 2.0
    low, high, median = (float(value) for value in np.quantile(values, (alpha, 1.0 - alpha, 0.5)))
    z0 = None
    ci = nan
    method = None
    if observed is not None and math.isfinite(observed):
        tolerance = 1e-12 * max(abs(observed), 1.0)
        below = np.count_nonzero(values < observed - tolerance)
        ties = np.count_nonzero(np.abs(values - observed) <= tolerance)
        share = min(max((below + 0.5 * ties) / values.size, 0.5 / values.size), 1.0 - 0.5 / values.size)
        z0 = float(ndtri(share))
        levels = ndtr(2.0 * z0 + ndtri(np.array([alpha, 1.0 - alpha])))
        if values.size >= MIN_BC_BOOT:
            ci, method = [float(value) for value in np.quantile(values, levels)], "bc"
        else:
            ci, method = [low, high], "percentile"
        if not ci[0] <= observed <= ci[1]:
            ci, method = [min(ci[0], float(observed)), max(ci[1], float(observed))], method + "+observed"
    return {"ci": ci, "ci_percentile": [low, high], "boot_median": median, "z0": z0, "n_valid": int(values.size), "method": method}


def permutation_p(null, observed):
    """One-sided permutation p = (1 + #{null >= observed}) / (K + 1) over the finite null values (NaN when undefined)."""
    null = np.asarray(null, dtype=np.float64)
    null = null[np.isfinite(null)]
    if not np.isfinite(observed) or null.size == 0:
        return float("nan")
    tolerance = 1e-12 * max(abs(observed), 1.0)
    return float(1 + np.count_nonzero(null >= observed - tolerance)) / (null.size + 1)


def _format_number(value):
    return f"{value:.2f}".replace("-", "\u2212")  # the minus sign of the addendum's verdicts


def verdict_text(delta, ci, baseline_label, metric="auc", p_perm=None, subject="the circuit"):
    """Neutral verdict of a comparison with a baseline, e.g. "the circuit does not exceed the degree baseline
    (ΔAUC 0.01, 95% CI -0.04 to 0.06)", printed with minus signs. "exceeds" only when the difference and the whole CI
    are above 0, "falls below" only when both are below 0.
    """
    name = f"Δ{METRIC_NAMES.get(metric, metric)}"
    if delta is None or not np.isfinite(delta) or ci is None or not all(np.isfinite(value) for value in ci):
        shown = "" if delta is None or not np.isfinite(delta) else f" ({name} {_format_number(delta)}; no confidence interval)"
        return f"{subject} cannot be compared with the {baseline_label}{shown}"
    if ci[0] > 0 and delta > 0:
        verb = "exceeds"
    elif ci[1] < 0 and delta < 0:
        verb = "falls below"
    else:
        verb = "does not exceed"
    p_text = "" if p_perm is None or not np.isfinite(p_perm) else f"; p_perm = {p_perm:.3g}"
    return f"{subject} {verb} the {baseline_label} ({name} {_format_number(delta)}, {round(CI_LEVEL * 100)}% CI {_format_number(ci[0])} to {_format_number(ci[1])}{p_text})"


@dataclasses.dataclass
class ValidationResult:
    """:func:`validate` result. ``to_dict()`` is the JSON content of validation.json (without file fields)."""

    outcome_type: str
    primary: str
    folds: Folds
    predictors: list
    observed: dict
    boot: list
    null: list
    n_boot: int
    n_perm: int
    circuit: dict
    incremental: dict
    comparisons: list
    verdicts: list
    warnings: list
    timings: dict
    prepared: object = None

    def predictor(self, name):
        return next(item for item in self.predictors if item["name"] == name)

    def to_dict(self):
        return {
            "outcome_type": self.outcome_type,
            "primary_metric": self.primary,
            "scheme": self.folds.describe(),
            "circuit": self.circuit,
            "predictors": self.predictors,
            "incremental": self.incremental,
            "comparisons": self.comparisons,
            "verdicts": self.verdicts,
            "bootstrap": {
                "B": self.n_boot,
                "resampling": "subjects within strata (dataset x class for a binary outcome, dataset for a score); folds, circuits and models rerun; the copies of a subject share a fold",
                "interval": "bias-corrected (BC) percentile interval (Efron 1981); ci_percentile, boot_median and z0 describe the raw bootstrap distribution",
                "ci_level": CI_LEVEL,
            },
            "permutation": {"K": self.n_perm, "scheme": "outcome permuted within datasets; folds, circuits and models rerun", "tails": "one-sided (greater)", "min_attainable_p": 1.0 / (self.n_perm + 1) if self.n_perm else None},
            "model": MODEL_NOTE,
            "warnings": list(self.warnings),
            "timings": self.timings,
        }


def validate(data, circuit="glm", score=None, scheme="auto", k=DEFAULT_K, repeats=DEFAULT_REPEATS, top_pct=DEFAULT_TOP_PCT, n_boot=DEFAULT_BOOT, n_perm=DEFAULT_PERM, seed=0, cancel=None, on_progress=None):
    """Validate an in-fold circuit against the baselines (see the module docstring).

    :param data: :class:`ValidationData`.
    :param circuit: "glm" (t map), "slnm" (partial r map) or "topk" (top-x% ROI of the t map).
    :param score: "similarity" (default; "roi" for topk), "damage" or "roi".
    :param cancel: callable returning True to stop (raises ``jobs.Cancelled``).
    :param on_progress: callback(stage, done, total, seconds per rerun) after the observed run (its seconds without the
        preparation of the maps, the estimate of a rerun) and each pass.
    :return: :class:`ValidationResult`.
    """
    started = time.monotonic()
    prep = _prepare(data, circuit, score, top_pct)
    prepared = time.monotonic()
    data = prep.data
    y = data.outcome
    sequence = np.random.SeedSequence(int(seed))
    fold_seed, boot_seed, perm_seed, inner_seed = (int(child.generate_state(1)[0]) for child in sequence.spawn(4))
    folds = make_folds(scheme, y, data.datasets, fold_seed, data.outcome_type, k, repeats)
    observed = _cv_runs(prep, [_Run(y, np.ones(data.n), folds, inner_seed)], keep=True)[0]
    timings = {"prepare_s": prepared - started, "observed_s": time.monotonic() - started}
    maps_per_run = sum(1 for _ in folds.iter_folds()) * (1 + INNER_FOLDS)
    if on_progress is not None:
        on_progress("observed", 1, 1, time.monotonic() - prepared)  # a rerun repeats the cross-validation, not the preparation

    def reruns(stage, count, make_run, rng_seed):
        rng = np.random.default_rng(rng_seed)
        specs = [make_run(rng) for _ in range(count)]
        out = []
        tic = time.monotonic()
        per_call = _runs_per_call(maps_per_run)
        for start in range(0, len(specs), per_call):
            if cancel is not None and cancel():
                raise jobs.Cancelled(done=len(out), reason=f"stopped during the {stage}")
            out.extend(item["metrics"] for item in _cv_runs(prep, specs[start : start + per_call]))
            if on_progress is not None:
                on_progress(stage, len(out), count, (time.monotonic() - tic) / max(len(out), 1))
        return out

    def boot_run(rng):
        counts = bootstrap_counts(rng, y, data.datasets, data.outcome_type)
        return _Run(y, counts.astype(np.float64), rerun_folds(folds, y, data.datasets, data.outcome_type, int(rng.integers(2**63 - 1)), counts), int(rng.integers(2**63 - 1)))

    def perm_run(rng):
        permuted = permute_within(rng, y, data.datasets)
        return _Run(permuted, np.ones(data.n), rerun_folds(folds, permuted, data.datasets, data.outcome_type, int(rng.integers(2**63 - 1))), int(rng.integers(2**63 - 1)))

    tic = time.monotonic()
    boot = reruns("bootstrap", int(n_boot), boot_run, boot_seed)
    timings["bootstrap_s"] = time.monotonic() - tic
    tic = time.monotonic()
    null = reruns("permutations", int(n_perm), perm_run, perm_seed)
    timings["permutations_s"] = time.monotonic() - tic
    timings["total_s"] = time.monotonic() - started
    return _assemble(prep, folds, observed, boot, null, int(n_boot), int(n_perm), {key: round(value, 4) for key, value in timings.items()})


def _assemble(prep, folds, observed, boot, null, n_boot, n_perm, timings):
    data = prep.data
    outcome_type = data.outcome_type
    primary = PRIMARY[outcome_type]
    names = METRICS[outcome_type]
    predictors = []
    for item in prep.predictors:
        record = item.to_dict()
        if item.status == "computed":
            metrics = {}
            for metric in names:
                value = observed["metrics"][item.name][metric]
                boot_values = [run[item.name][metric] for run in boot]
                null_values = [run[item.name][metric] for run in null]
                details = interval_details(boot_values, value)
                metrics[metric] = {
                    "value": value,
                    "ci": details["ci"],
                    "p_perm": permutation_p(null_values, value) if n_perm else None,
                    "ci_percentile": details["ci_percentile"],
                    "boot_median": details["boot_median"],
                    "z0": details["z0"],
                    "ci_method": details["method"],
                    "n_boot_valid": details["n_valid"],
                }
            record["metrics"] = metrics
        else:
            record["metrics"] = {metric: {"value": None, "ci": None, "p_perm": None} for metric in names}
        predictors.append(record)

    def delta(run, a, b):
        return run[a][primary] - run[b][primary]

    observed_metrics = observed["metrics"]
    incremental = {"metric": primary, "model": "volume + circuit vs volume", "delta": None, "ci": None, "p_perm": None}
    if "circuit" in observed_metrics and "volume" in observed_metrics:
        value = delta(observed_metrics, "circuit", "volume")
        details = interval_details([delta(run, "circuit", "volume") for run in boot], value)
        ci = details["ci"]
        p = permutation_p([delta(run, "circuit", "volume") for run in null], value) if n_perm else None
        incremental.update({"delta": value, "ci": ci, "p_perm": p, "ci_percentile": details["ci_percentile"], "boot_median": details["boot_median"], "z0": details["z0"], "ci_method": details["method"], "text": verdict_text(value, ci, BASELINE_LABELS["volume"], primary, p, "volume + circuit")})
    comparisons = []
    for item in prep.predictors:
        if item.role != "baseline":
            continue
        label = BASELINE_LABELS.get(item.name) or f"reference-map baseline {item.name[4:]}"
        entry = {"baseline": item.name, "metric": primary, "delta": None, "ci": None, "status": item.status, "reason": item.reason}
        if item.status == "computed":
            value = delta(observed_metrics, "circuit", item.name)
            details = interval_details([delta(run, "circuit", item.name) for run in boot], value)
            ci = details["ci"]
            entry.update({"delta": value, "ci": ci, "ci_percentile": details["ci_percentile"], "boot_median": details["boot_median"], "z0": details["z0"], "ci_method": details["method"], "text": verdict_text(value, ci, label, primary, subject="the circuit")})
        else:
            entry["text"] = f"the {label} was not computed: {item.reason}"
        comparisons.append(entry)
    verdicts = [incremental.get("text")] + [entry["text"] for entry in comparisons if entry["baseline"] != "volume"]
    warnings = list(folds.warnings)
    if folds.message:
        warnings.insert(0, folds.message)
    circuit = {
        "kind": prep.circuit,
        "label": CIRCUIT_LABELS[prep.circuit],
        "score": prep.score,
        "top_pct": prep.top_pct if prep.circuit == "topk" else None,
        "statistic": "Fz maps (AvgFz) unless the input says T",
        "thresholding": "none inside folds" if prep.circuit != "topk" else f"top {prep.top_pct:g}% of |t| (a fixed rank rule)",
        "permutation_thresholds_in_folds": False,
        "partial": list(REFERENCE_KEYS) if len(prep.bases) > 1 else None,
    }
    return ValidationResult(outcome_type, primary, folds, predictors, observed, boot, null, n_boot, n_perm, circuit, incremental, comparisons, [text for text in verdicts if text], warnings, timings, prep)


# Frozen circuits


def frozen_metrics(values, outcome, outcome_type, datasets=None, n_boot=DEFAULT_FROZEN_BOOT, seed=0):
    """Metrics of a fixed score (no fitting: higher score = positive class or higher outcome) with a simple
    bootstrap percentile CI (subjects resampled within strata): AUC and OR per SD, or r.
    """
    values = np.asarray(values, dtype=np.float64)
    outcome = np.asarray(outcome, dtype=np.float64)

    def compute(weights):
        if outcome_type == "binary":
            return {"auc": auc(values, outcome, weights), "or_per_sd": odds_ratio_per_sd(values, outcome, weights)}
        return {"r": weighted_pearson(values, outcome, weights)}

    observed = compute(None)
    rng = np.random.default_rng(seed)
    boot = [compute(bootstrap_counts(rng, outcome, datasets, outcome_type).astype(np.float64)) for _ in range(int(n_boot))]
    return {name: {"value": value, "ci": interval_details([run[name] for run in boot], None)["ci_percentile"]} for name, value in observed.items()}


def frozen_scores(maps, circuit_vector, score="similarity", occupancy=None, top_pct=None, reference=None):
    """(n,) scores of every lesion with a frozen circuit (a top-x% ROI of it when ``top_pct`` is given), and the
    partial similarity given [μC_r, PC1] (None unless ``score`` is "similarity" and ``reference`` is given).
    """
    circuit_vector = np.asarray(circuit_vector, dtype=np.float64)
    if top_pct:
        circuit_vector = scores.top_pct_roi(circuit_vector, top_pct).astype(np.float64)
    if score == "damage":
        if occupancy is None:
            raise ValueError("Damage scores need the lesion occupancy")
        return scores.damage_matrix(occupancy, circuit_vector[None, :])[:, 0], None
    if score == "roi":
        return scores.roi_matrix(maps, circuit_vector[None, :])[:, 0], None
    covariates = None if reference is None else np.vstack([np.asarray(reference[key], dtype=np.float64) for key in REFERENCE_KEYS])
    r, partial = scores.similarity_matrix(maps, circuit_vector[None, :], covariates)
    return scores.fisher_z(r[:, 0]), None if partial is None else scores.fisher_z(partial[:, 0])


def apply_frozen(circuit_path, sha256, maps_tsv, participants, outcome, outcome_type, mask=None, score="similarity", top_pct=None, positive=None, dataset_column=None, n_boot=DEFAULT_FROZEN_BOOT, seed=0, input="Fz", bundle=None, emit=None, memmap_path=None):  # noqa: A002 (as glm's design input)
    """Apply a frozen circuit (fixed by another study) to the maps of a new cohort.

    The circuit file must have the given sha256 (it is the record of what was frozen). Returns a dict with the
    circuit sha256, the per-subject scores, the metrics of the score (and of lesion volume, for context) with
    bootstrap CIs, and the excluded subjects.

    :raises ValueError: the sha256 does not match, or the circuit is not on the grid of the maps.
    """
    from . import nifti
    from .export import file_sha256

    actual = file_sha256(circuit_path)
    if str(sha256).lower() != actual:
        raise ValueError(f"The circuit {os.path.basename(os.fspath(circuit_path))} has sha256 {actual}, not the frozen {sha256}: it is not the frozen circuit")
    loaded = load_data(maps_tsv, participants, outcome, outcome_type, mask=mask, positive=positive, dataset_column=dataset_column, input=input, bundle=bundle, need_occupancy=score == "damage", memmap_path=memmap_path, emit=emit)
    data, info = loaded["data"], loaded["info"]
    circuit_data, circuit_affine, _ = nifti.read_nifti(circuit_path)
    circuit_data = np.asarray(circuit_data, dtype=np.float64)
    if circuit_data.ndim > 3:
        circuit_data = circuit_data.reshape(circuit_data.shape[:3])
    if tuple(circuit_data.shape) != tuple(info["mask_shape"]) or not np.allclose(circuit_affine, info["mask_affine"], atol=1e-4, rtol=0):
        raise ValueError(f"The circuit {os.path.basename(os.fspath(circuit_path))} is not on the grid of the maps")
    vector = circuit_data.reshape(-1)[info["indices"]]
    if not np.all(np.isfinite(vector)):
        raise ValueError("The circuit has non-finite values inside the mask")
    values, partial = frozen_scores(data.maps, vector, score, data.occupancy, top_pct, data.reference)
    metrics = frozen_metrics(values, data.outcome, data.outcome_type, data.datasets, n_boot, seed)
    out = {
        "circuit": os.path.abspath(os.fspath(circuit_path)),
        "sha256": actual,
        "score": score,
        "top_pct": top_pct,
        "n": data.n,
        "sids": list(data.sids),
        "scores": values,
        "partial_scores": partial,
        "metrics": metrics,
        "partial_metrics": frozen_metrics(partial, data.outcome, data.outcome_type, data.datasets, n_boot, seed) if partial is not None else None,
        "volume_metrics": frozen_metrics(data.volume, data.outcome, data.outcome_type, data.datasets, n_boot, seed),
        "data": data,
        "info": info,
    }
    return out


# Loading (command line)


def _numeric(value):
    from .study import is_missing

    if is_missing(value):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def binary_codes(values, positive=None):
    """(0/1 codes with None for missing, positive label) of a binary column: ``positive`` (default: "case"
    when present, else 1 for a 0/1 column); every other value of a two-level column is 0.
    """
    from .study import is_missing

    present = sorted({str(value) for value in values if not is_missing(value)})
    if positive is None:
        if "case" in present:
            positive = "case"
        elif set(present) <= {"0", "1", "0.0", "1.0"}:
            positive = "1" if any(value in ("1", "1.0") for value in present) else present[-1]
        else:
            raise ValueError(f"Give the positive class (--positive): the column holds {', '.join(present[:5])}")
    positive = str(positive)
    matches = {value for value in present if value == positive or (_numeric(value) is not None and _numeric(value) == _numeric(positive))}
    if not matches:
        raise ValueError(f"The positive class {positive!r} does not occur in the column ({', '.join(present[:5])})")
    if len(present) - len(matches) != 1:
        raise ValueError(f"A binary outcome needs exactly two levels, the column holds {', '.join(present[:6])}")
    return [None if is_missing(value) else (1.0 if str(value) in matches else 0.0) for value in values], positive


def _read_occupancy(paths, n_voxels):
    """(n x V) scipy.sparse CSR occupancy from the weights.npz next to each map (cohortmaps cache entries), or (None, reason)."""
    from scipy import sparse

    rows, cols, vals = [], [], []
    for row, path in enumerate(paths):
        weights = os.path.join(os.path.dirname(path), "weights.npz")
        try:
            with np.load(weights) as stored:
                indices, values, stored_voxels = stored["indices"], stored["values"], int(stored["n_voxels"])
        except (OSError, KeyError, ValueError):
            return None, f"no lesion occupancy next to the map {os.path.basename(path)} (weights.npz of the cohortmaps cache)"
        if stored_voxels != n_voxels:
            return None, f"the occupancy of {os.path.basename(path)} has {stored_voxels} voxels, the mask {n_voxels}"
        rows.append(np.full(indices.size, row, dtype=np.int64))
        cols.append(np.asarray(indices, dtype=np.int64))
        vals.append(np.asarray(values, dtype=np.float64))
    matrix = sparse.csr_matrix((np.concatenate(vals) if vals else [], (np.concatenate(rows) if rows else [], np.concatenate(cols) if cols else [])), shape=(len(paths), n_voxels))
    return matrix, None


def load_data(maps_tsv, participants, outcome, outcome_type, covariates=(), mask=None, positive=None, dataset_column=None, input="Fz", bundle=None, refmaps=(), need_occupancy=False, memmap_path=None, emit=None):  # noqa: A002
    """:class:`ValidationData` of a maps.tsv and a participants table (listwise exclusion, reported) and an info dict
    (mask, connectome, cohortmaps run, reference, excluded subjects).

    ``dataset_column`` None picks the ``dataset`` column when at least one subject has a value in it (as the design
    of glm and slnm does); subjects without a value then form one dataset of their own ("n/a", with a warning). A
    column given explicitly excludes the subjects without a value; ``"none"`` (``--dataset none``) uses no dataset
    column (one cohort).
    """
    from . import glm, nifti, slnm
    from .export import file_sha256
    from .geometry import mask_indices
    from .study import NA, is_missing

    emit = emit or (lambda *args, **kwargs: None)
    maps_tsv = os.path.abspath(os.fspath(maps_tsv))
    column = glm.MAP_COLUMNS[input]
    study, in_study = glm._load_study(participants)
    explicit_dataset = dataset_column is not None
    if explicit_dataset and dataset_column.strip().lower() == NO_DATASET and NO_DATASET not in study.columns:
        dataset_column = None
    for name in [outcome, *covariates] + ([dataset_column] if dataset_column else []):
        if name not in study.columns:
            raise ValueError(f"{participants} has no column {name}")
    if not explicit_dataset and "dataset" in study.columns and any(not is_missing(row.get("dataset")) for row in study.subjects):
        dataset_column = "dataset"
    mapped = glm._map_rows(maps_tsv, column)
    cohort_json = os.path.join(os.path.dirname(maps_tsv), "cohortmaps.json")
    cohort = glm.cohortmaps_record(cohort_json)
    bundle_path = bundle or ((cohort or {}).get("bundle") or {}).get("path")
    if mask is None:
        candidate = os.path.join(bundle_path, "mask.nii.gz") if bundle_path else None
        if not candidate or not os.path.isfile(candidate):
            raise ValueError("Give the connectome mask (--mask) or its bundle (--bundle)")
        mask = candidate
    rows = {row["participant_id"]: row for row in study.subjects}
    sids, y, volume, cov, datasets, excluded = [], [], [], [], [], {}
    codes, positive_used = (None, None)
    if outcome_type == "binary":
        codes, positive_used = binary_codes([rows[sid].get(outcome) for sid in study.subject_ids], positive)
        codes = dict(zip(study.subject_ids, codes, strict=True))
    for sid in study.subject_ids:
        row = rows[sid]
        if sid not in mapped:
            excluded.setdefault("no map in maps.tsv", []).append(sid)
            continue
        value = codes[sid] if codes is not None else _numeric(row.get(outcome))
        if value is None:
            excluded.setdefault(f"missing {outcome}", []).append(sid)
            continue
        vol = _numeric(row.get("volume_ml"))
        if vol is None:
            vol = _numeric(mapped[sid][1])
        if vol is None:
            excluded.setdefault("missing volume_ml", []).append(sid)
            continue
        values = [_numeric(row.get(name)) for name in covariates]
        if any(item is None for item in values):
            excluded.setdefault("missing covariate", []).append(sid)
            continue
        label = None
        if dataset_column:
            label = None if is_missing(row.get(dataset_column)) else str(row.get(dataset_column))
            if label is None and explicit_dataset:
                excluded.setdefault(f"missing {dataset_column}", []).append(sid)
                continue
        sids.append(sid)
        y.append(value)
        volume.append(vol)
        cov.append(values)
        datasets.append(label)
    if len(sids) < 4:
        raise ValueError(f"Only {len(sids)} subject(s) have a map, an outcome, a volume and the covariates")
    for reason, members in excluded.items():
        emit("warning", code="excluded", message=f"{len(members)} subject(s) excluded: {reason}")
    no_dataset = [sid for sid, label in zip(sids, datasets, strict=True) if dataset_column and label is None]
    if no_dataset:
        datasets = [NA if label is None else label for label in datasets]
        emit(
            "warning",
            code="missing_dataset",
            message=f"{len(no_dataset)} subject(s) have no {dataset_column}: they are treated as one {dataset_column} of their own ({NA}). Fill in {dataset_column}, or choose the column with --dataset (none: one cohort)",
        )
    mask_data, mask_affine, mask_header = nifti.read_nifti(mask)
    mask_shape = tuple(mask_header["shape"][:3])
    indices = mask_indices(np.asarray(mask_data).reshape(mask_shape) != 0)
    paths = [mapped[sid][0] for sid in sids]
    if memmap_path is None:
        import tempfile

        handle, memmap_path = tempfile.mkstemp(suffix=".npy", prefix="lnm-validate-")
        os.close(handle)
        temporary = memmap_path
    else:
        temporary = None
    try:
        Y = glm._load_maps(paths, mask_shape, mask_affine, indices, memmap_path, emit)
        Y = np.array(Y) if temporary else Y
    finally:
        if temporary:
            os.remove(temporary)
    largest = np.zeros(len(sids))
    for part in scores._chunks(indices.size, CHUNK_VOXELS):
        if not np.all(np.isfinite(Y[:, part])):
            raise ValueError("The maps have non-finite values inside the mask")
        largest = np.maximum(largest, np.abs(np.asarray(Y[:, part])).max(axis=1))
    if input == "Fz" and np.any(largest > FZ_WARN):
        odd = [sid for sid, value in zip(sids, largest, strict=True) if value > FZ_WARN]
        emit("warning", code="map_range", message=f"{len(odd)} map(s) have |Fz| above {FZ_WARN:g} inside the mask ({', '.join(odd[:5])}{', ...' if len(odd) > 5 else ''}): check that they are AvgFz maps")
    occupancy, occupancy_reason = _read_occupancy(paths, int(indices.size))
    if occupancy is None and need_occupancy:
        raise ValueError(f"Damage scores need the lesion occupancy: {occupancy_reason}")
    subjects_sha = mapped[sids[0]][2][1]

    ref, ref_reason, ref_info = slnm._reference_for(types.SimpleNamespace(bundle=bundle_path), cohort, subjects_sha if isinstance(subjects_sha, str) and len(subjects_sha) == 64 else None, indices, mask_shape)
    reference = {key: np.asarray(ref.maps[key], dtype=np.float64) for key in REFERENCE_KEYS} if ref is not None else None
    loaded_refmaps = {}
    for path in refmaps:
        data, affine, _ = nifti.read_nifti(path)
        data = np.asarray(data, dtype=np.float64)
        if data.ndim > 3:
            data = data.reshape(data.shape[:3])
        if tuple(data.shape) != mask_shape or not np.allclose(affine, mask_affine, atol=1e-4, rtol=0):
            raise ValueError(f"The reference map {os.path.basename(path)} is not on the connectome grid (resample it first)")
        vector = data.reshape(-1)[indices]
        if not np.all(np.isfinite(vector)):
            raise ValueError(f"The reference map {os.path.basename(path)} has non-finite values inside the mask")
        name = os.path.basename(path)
        for extension in (".nii.gz", ".nii"):
            if name.endswith(extension):
                name = name[: -len(extension)]
        loaded_refmaps[name] = vector
    data = ValidationData(
        sids=sids,
        maps=Y,
        outcome=np.array(y),
        outcome_type=outcome_type,
        volume=np.array(volume),
        covariates=np.array(cov, dtype=np.float64).reshape(len(sids), -1) if covariates else None,
        covariate_names=tuple(covariates),
        datasets=datasets if dataset_column else None,
        occupancy=occupancy,
        reference=reference,
        refmaps=loaded_refmaps,
        occupancy_reason=occupancy_reason,
        reference_reason=ref_reason,
    ).check()
    info = {
        "maps_tsv": maps_tsv,
        "participants": os.path.abspath(os.fspath(participants)),
        "in_study": in_study,
        "study_path": study.path,
        "mask": os.path.abspath(mask),
        "mask_sha256": file_sha256(mask),
        "mask_shape": mask_shape,
        "mask_affine": mask_affine,
        "indices": indices,
        "map_column": column,
        "cohortmaps_json": cohort_json if cohort else None,
        "cohortmaps_run": (cohort or {}).get("run_id"),
        "bundle": (cohort or {}).get("bundle") or ({"path": bundle_path} if bundle_path else None),
        "subject_ids_sha256": subjects_sha,
        "dataset_column": dataset_column,
        "dataset_missing": len(no_dataset),
        "positive": positive_used,
        "excluded": excluded,
        "reference": ref_info,
        "reference_reason": ref_reason,
        "occupancy_reason": occupancy_reason,
        "refmaps": [os.path.abspath(path) for path in refmaps],
        "map_paths": paths,
    }
    return {"data": data, "info": info}


# Outputs


def _fmt(value):
    if value is None:
        return "n/a"
    if isinstance(value, float | np.floating):
        return f"{float(value):.6g}" if np.isfinite(value) else "n/a"
    return str(value).replace("\t", " ")


def _write_tsv(path, header, rows):
    lines = ["\t".join(header)] + ["\t".join(_fmt(value) for value in row) for row in rows]
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return path


def _repeat_mean(values):
    """Mean over the repeats (rows) of the finite values; NaN where a subject has none."""
    finite = np.isfinite(values)
    count = finite.sum(axis=0)
    out = np.full(values.shape[1], np.nan)
    np.divide(np.where(finite, values, 0.0).sum(axis=0), count, out=out, where=count > 0)
    return out


def write_scores_tsv(path, result):
    """scores.tsv: participant_id, dataset, outcome, volume_ml, then per predictor the held-out score (circuit and
    location: the in-fold score averaged over repeats; fixed maps: the score) and the out-of-fold prediction
    (linear predictor; averaged over repeats).
    """
    prep = result.prepared
    data = prep.data
    observed = result.observed
    computed = [item for item in prep.predictors if item.status == "computed"]
    header = ["participant_id", "dataset", "outcome", "volume_ml"]
    columns = []
    for item in computed:
        if item.kind != "volume":
            header.append(f"{item.name}_score")
            columns.append(item.fixed if item.kind == "fixed" else _repeat_mean(observed["held_out"][item.name]))
        header.append(f"{item.name}_prediction")
        columns.append(_repeat_mean(observed["predictions"][item.name]))
    rows = []
    for index, sid in enumerate(data.sids):
        rows.append([sid, data.datasets[index] if data.datasets else None, float(data.outcome[index]), float(data.volume[index]), *(float(column[index]) for column in columns)])
    return _write_tsv(path, header, rows)


def write_folds_tsv(path, folds, sids):
    """folds.tsv: participant_id, repeat, fold, fold_label (one row per subject and repeat; LODO folds are datasets)."""
    rows = []
    for repeat in range(folds.repeats):
        for index, sid in enumerate(sids):
            fold = int(folds.assignment[repeat, index])
            if fold >= 0:
                rows.append([sid, repeat + 1, fold + 1, folds.labels[repeat][fold]])
    return _write_tsv(path, ["participant_id", "repeat", "fold", "fold_label"], rows)


def write_record(path, record):
    """Write validation.json atomically with its paths relative to its folder (SPEC 9.7 rule 10)."""
    from .export import portable_record_paths

    jobs.write_json_atomic(path, portable_record_paths(record, os.path.dirname(os.path.abspath(path)), RECORD_PATH_FIELDS))


def read_record(path):
    """validation.json with its paths absolute, or None."""
    import json

    from .export import resolve_record_paths

    try:
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    return resolve_record_paths(record, os.path.dirname(os.path.abspath(path)), RECORD_PATH_FIELDS)


# Command line


def _parser():
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.validate", description="Validate an in-fold circuit against volume, μC, PC1 and lesion-location baselines (or apply a frozen circuit).")
    parser.add_argument("--maps", required=True, help="maps.tsv of a cohortmaps run (participant_id, AvgFz, T)")
    parser.add_argument("--participants", required=True, help="participants.tsv (of a study, or a table with participants.json)")
    parser.add_argument("--outcome", required=True, help="outcome column: group (binary) or score (continuous)")
    parser.add_argument("--type", required=True, choices=OUTCOME_TYPES, help="binary or continuous outcome")
    parser.add_argument("--circuit", choices=CIRCUITS, default=None, help="in-fold circuit: glm (t map), slnm (partial r map) or topk (top-x%% ROI of the t map)")
    parser.add_argument("--score", choices=SCORE_KINDS, default=None, help="lesion score: similarity (default; roi for topk), damage or roi")
    parser.add_argument("--top-pct", type=float, default=DEFAULT_TOP_PCT, help="top-x%% of the in-fold map for topk circuits (default 10)")
    parser.add_argument("--covariates", default="", help="comma-separated covariate columns besides lesion volume (always in the models)")
    parser.add_argument("--scheme", choices=SCHEMES, default="auto", help="auto (LODO when possible), lodo, kfold or loo")
    parser.add_argument("--k", type=jobs.positive_int, default=DEFAULT_K, help="folds of stratified k-fold (default 10)")
    parser.add_argument("--repeats", type=jobs.positive_int, default=DEFAULT_REPEATS, help="repeats of stratified k-fold (default 5)")
    parser.add_argument("--boot", type=int, default=None, help=f"whole-CV bootstrap resamples (default {DEFAULT_BOOT}; frozen: {DEFAULT_FROZEN_BOOT}; 0: none; at least {MIN_BC_BOOT} for bias-corrected intervals)")
    parser.add_argument("--perm", type=int, default=DEFAULT_PERM, help=f"outcome permutations, whole pipeline rerun (default {DEFAULT_PERM}; 0: none)")
    parser.add_argument("--seed", type=int, default=0, help="seed of the folds, bootstrap and permutations (default 0)")
    parser.add_argument("--positive", default=None, help="positive class of a binary outcome (default: case, or 1)")
    parser.add_argument("--dataset", default=None, help="dataset column (LODO folds, strata, permutation blocks; default: dataset when it has values; none: one cohort)")
    parser.add_argument("--input", choices=("Fz", "T"), default="Fz", help="per-lesion maps: Fz (AvgFz, default) or T")
    parser.add_argument("--bundle", default=None, help="connectome bundle of the reference maps and mask (default: the bundle recorded in cohortmaps.json)")
    parser.add_argument("--mask", default=None, help="connectome mask (default: the bundle's mask.nii.gz)")
    parser.add_argument("--refmaps", default="", help="comma-separated reference maps (NIfTI on the connectome grid) used as extra baselines")
    parser.add_argument("--frozen", default=None, help="apply this frozen circuit (NIfTI) instead of cross-validating")
    parser.add_argument("--frozen-sha256", default=None, help="sha256 of the frozen circuit file (required with --frozen)")
    parser.add_argument("--out", required=True, help="output folder (validation.json, scores.tsv, folds.tsv)")
    jobs.add_common_arguments(parser)
    return parser


def _columns(text):
    return [column.strip() for column in (text or "").split(",") if column.strip()]


def main(argv=None):
    """Command-line entry point (see the module docstring); returns the exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(argv)
    usage = None
    if args.frozen is None and args.circuit is None:
        usage = "--circuit is required (or --frozen to apply a frozen circuit)"
    elif args.frozen is not None and not args.frozen_sha256:
        usage = "--frozen needs --frozen-sha256 (the sha256 recorded when the circuit was frozen)"
    elif (args.boot is not None and args.boot < 0) or args.perm < 0 or args.seed < 0:
        usage = "--boot, --perm and --seed must not be negative"
    elif not 0.0 < args.top_pct < 100.0:
        usage = f"--top-pct must be between 0 and 100, got {args.top_pct}"
    if usage:
        raise jobs.JobError(usage, code="usage", exit_code=jobs.EXIT_USAGE)
    return _run_cli(args, argv)


def _run_cli(args, argv):
    from .export import DISCLAIMER, LIBRARY_VERSION, now_iso, versions

    emit = jobs.emit
    cancel = jobs.CancelToken(args.cancel_file)
    started = time.monotonic()
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    lock = jobs.OutputLock(os.path.join(out_dir, LOCK_NAME))
    lock.acquire()
    run_root, run_id, record = None, None, None
    memmap_path = os.path.join(out_dir, MAPS_MEMMAP)
    frozen = args.frozen is not None
    covariates = _columns(args.covariates)
    refmaps = [os.path.abspath(path) for path in _columns(args.refmaps)]
    n_boot = args.boot if args.boot is not None else (DEFAULT_FROZEN_BOOT if frozen else DEFAULT_BOOT)
    if 0 < n_boot < MIN_BC_BOOT:
        emit("warning", code="few_bootstrap", message=f"{n_boot} bootstrap resamples: the confidence intervals are rough (use at least {MIN_BC_BOOT}; default {DEFAULT_BOOT})")
    try:
        try:
            if frozen:
                applied = apply_frozen(
                    args.frozen,
                    args.frozen_sha256,
                    args.maps,
                    args.participants,
                    args.outcome,
                    args.type,
                    mask=args.mask,
                    score=args.score or ("roi" if args.circuit == "topk" else "similarity"),  # as the cross-validation
                    top_pct=args.top_pct if args.circuit == "topk" else None,
                    positive=args.positive,
                    dataset_column=args.dataset,
                    n_boot=n_boot,
                    seed=args.seed,
                    input=args.input,
                    bundle=args.bundle,
                    emit=emit,
                    memmap_path=memmap_path,
                )
                loaded = {"data": applied["data"], "info": applied["info"]}
            else:
                loaded = load_data(args.maps, args.participants, args.outcome, args.type, covariates, args.mask, args.positive, args.dataset, args.input, args.bundle, refmaps, args.score == "damage", memmap_path, emit)
        except ValueError as exc:
            raise jobs.JobError(str(exc), code="usage", exit_code=jobs.EXIT_USAGE) from None
        data, info = loaded["data"], loaded["info"]
        params = {
            "mode": "frozen" if frozen else "cv",
            "outcome": args.outcome,
            "type": args.type,
            "circuit": args.circuit,
            "score": args.score,
            "top_pct": args.top_pct,
            "covariates": covariates,
            "scheme": args.scheme,
            "k": args.k,
            "repeats": args.repeats,
            "n_boot": n_boot,
            "n_perm": 0 if frozen else args.perm,
            "seed": args.seed,
            "input": args.input,
            "sids": runs.params_sha256(list(data.sids)),
            "mask_sha256": info["mask_sha256"],
            "frozen_sha256": args.frozen_sha256,
        }
        run_root = os.path.abspath(args.run_root or (info["study_path"] if info["in_study"] else out_dir))
        parents = [run for run in [info["cohortmaps_run"], *args.parent_run] if run]
        run_id = runs.register(run_root, "validation", out_dir, parents=parents, params=params, seeds={"folds": args.seed, "bootstrap": args.seed, "permutation": args.seed})
        record = {"format": FORMAT_NAME, "version": FORMAT_VERSION, "run_id": run_id, "status": "running", "argv": list(argv), "start": now_iso(), "end": None}
        emit("start", job="validate", argv=list(argv), threads=args.threads, run_id=run_id, n=data.n, n_voxels=int(data.maps.shape[1]), mode=params["mode"], outcome=args.outcome, type=args.type, circuit=args.circuit)
        for reason in (info["reference_reason"], info["occupancy_reason"]):
            if reason and not frozen:
                emit("warning", code="baseline_missing", message=f"A baseline is not computed: {reason}")
        inputs = {
            "maps_tsv": info["maps_tsv"],
            "map_column": info["map_column"],
            "input": args.input,
            "participants": info["participants"],
            "mask": info["mask"],
            "mask_sha256": info["mask_sha256"],
            "cohortmaps_json": info["cohortmaps_json"],
            "cohortmaps_run": info["cohortmaps_run"],
            "subject_ids_sha256": info["subject_ids_sha256"],
            "refmaps": info["refmaps"],
        }
        outcome_info = {
            "column": args.outcome,
            "type": args.type,
            "positive": info["positive"],
            "n": data.n,
            "n_positive": int(np.sum(data.outcome == 1)) if args.type == "binary" else None,
            "n_negative": int(np.sum(data.outcome == 0)) if args.type == "binary" else None,
            "dataset_column": info["dataset_column"],
            "datasets": sorted(set(data.datasets)) if data.datasets else None,
            "dataset_missing": info["dataset_missing"],
            "excluded": info["excluded"],
        }
        common = {
            "library_version": LIBRARY_VERSION,
            "versions": versions(),
            "threads": args.threads,
            "inputs": inputs,
            "connectome": {"bundle": info["bundle"]},
            "reference": info["reference"],
            "reference_missing_reason": info["reference_reason"],
            "occupancy_missing_reason": info["occupancy_reason"],
            "outcome": outcome_info,
            "covariates": ["volume_ml", *covariates],
            "disclaimer": DISCLAIMER,
        }
        outputs = []
        if frozen:
            emit("stage", name="scores", index=1, total=1)
            scores_path = _write_tsv(
                os.path.join(out_dir, SCORES_TSV),
                ["participant_id", "dataset", "outcome", "volume_ml", "frozen_score"] + (["frozen_partial_score"] if applied["partial_scores"] is not None else []),
                [
                    [sid, data.datasets[index] if data.datasets else None, float(data.outcome[index]), float(data.volume[index]), float(applied["scores"][index])] + ([float(applied["partial_scores"][index])] if applied["partial_scores"] is not None else [])
                    for index, sid in enumerate(data.sids)
                ],
            )
            outputs.append(scores_path)
            record.update(common)
            record.update({
                "mode": "frozen",
                "frozen": {"circuit": applied["circuit"], "sha256": applied["sha256"], "score": applied["score"], "top_pct": applied["top_pct"], "provenance": "frozen (independent circuit, sha256 verified)"},
                "metrics": applied["metrics"],
                "partial_metrics": applied["partial_metrics"],
                "volume_metrics": applied["volume_metrics"],
                "bootstrap": {"B": n_boot, "resampling": "subjects within strata (simple bootstrap; the circuit is fixed)", "ci_level": CI_LEVEL},
            })
            summary = {"mode": "frozen", "n": data.n, "metrics": {name: item["value"] for name, item in applied["metrics"].items()}}
        else:
            stage = {"index": 1}

            def on_progress(name, done, total, seconds):
                if name == "observed":
                    eta = seconds * (n_boot + args.perm)
                    duration = f"{eta / 60:.0f} min" if eta >= 90 else f"{max(eta, 1):.0f} s"
                    estimate = f"about {duration} for {n_boot} bootstrap and {args.perm} permutation reruns of the whole cross-validation" if n_boot + args.perm else "no bootstrap or permutation reruns"
                    emit("stage", name="bootstrap", index=2, total=3, runs=n_boot, eta=round(eta, 1), estimate=estimate)
                    stage["index"] = 2
                    return
                if cancel.cancelled():
                    return
                if name == "permutations" and stage["index"] == 2:
                    stage["index"] = 3
                    emit("stage", name="permutations", index=3, total=3, runs=args.perm)
                remaining = (total - done) + (args.perm if name == "bootstrap" else 0)
                emit("progress", stage=name, done=done, total=total, elapsed=round(time.monotonic() - started, 3), eta=round(seconds * remaining, 1))

            emit("stage", name="observed", index=1, total=3)
            try:
                result = validate(data, args.circuit, args.score, args.scheme, args.k, args.repeats, args.top_pct, n_boot, args.perm, args.seed, cancel=cancel.cancelled, on_progress=on_progress)
            except (ValueError, LeakageError) as exc:
                raise jobs.JobError(str(exc), code="usage" if isinstance(exc, ValueError) else "leakage", exit_code=jobs.EXIT_USAGE if isinstance(exc, ValueError) else jobs.EXIT_ERROR) from None
            for message in result.warnings:
                emit("warning", code="scheme", message=message)
            outputs.append(write_scores_tsv(os.path.join(out_dir, SCORES_TSV), result))
            outputs.append(write_folds_tsv(os.path.join(out_dir, FOLDS_TSV), result.folds, data.sids))
            record.update(common)
            record.update({"mode": "cv", **result.to_dict()})
            circuit_row = result.predictor("circuit")
            summary = {
                "mode": "cv",
                "n": data.n,
                "scheme": result.folds.scheme,
                "primary": result.primary,
                "circuit": circuit_row["metrics"][result.primary]["value"],
                "delta_vs_volume": result.incremental.get("delta"),
                "delta_ci": result.incremental.get("ci"),
                "p_perm": result.incremental.get("p_perm"),
            }
        record.update({"status": "done", "end": now_iso(), "elapsed": round(time.monotonic() - started, 3), "outputs": [os.path.basename(path) for path in outputs]})
        json_path = os.path.join(out_dir, VALIDATION_JSON)
        write_record(json_path, record)
        outputs.append(json_path)
        for path in outputs:
            emit("result", kind="validation", path=path)
        runs.update(run_root, run_id, status="done", ended=record["end"], outputs=[os.path.basename(path) for path in outputs], summary=summary)
        emit("done", outputs=outputs, elapsed=round(time.monotonic() - started, 3), run_id=run_id)
        return jobs.EXIT_OK
    except jobs.Cancelled as exc:
        if record is not None:
            record.update({"status": "cancelled", "end": now_iso(), "stop_reason": exc.reason, "reruns_done": exc.done})
            write_record(os.path.join(out_dir, VALIDATION_JSON), record)
        if run_id:
            runs.update(run_root, run_id, status="cancelled", ended=now_iso())
        raise
    except BaseException as exc:
        if run_id:
            try:
                runs.update(run_root, run_id, status="failed", ended=now_iso(), error=f"{type(exc).__name__}: {exc}")
            except Exception:
                pass
        raise
    finally:
        try:
            if os.path.exists(memmap_path):
                os.remove(memmap_path)
        except OSError:
            pass
        lock.release()


if __name__ == "__main__":
    jobs.run_job(main, job="validate")

"""Pipeline calibration: empirical FWER of the configured design on the user's own lesions, the false-positive rate of
the sensitivity route, and planted-network recovery (numpy and scipy only; no Slicer imports).

Catches design and coding errors before publication (SPEC_LNM_ADDENDUM §3.4, §8; the corrected SR-18: the planted
truth is a *network*, not a parcel), such as the one-sample design whose FWER is close to 1 because lesion network
maps are never centred on 0 (they share the connectome's degree pattern; Petersen 2026):

- :func:`empirical_fwer`: K null datasets of the configured GLM or sLNM design. Group and score designs permute the
  tested labels or scores within the design's exchangeability scheme (free, within blocks or whole blocks); a
  one-sample design has no labels, so its null datasets are random sets of the same number of lesions drawn from
  every lesion with a map (the study's other lesions, linked control sets, or a ``pool`` of control lesions): the
  question of a one-sample LNM is "do these lesions share a network that lesions without the symptom do not?".
  Each null dataset runs the full configured pipeline (``design.build_design``, the sLNM ranks and flip, the
  Freedman-Lane permutation test of glm.py with ``inner_perm`` permutations) and counts as a family-wise error
  when any voxel reaches FWE p <= alpha. The empirical FWER is reported with its exact 95% binomial
  (Clopper-Pearson) CI; it is "inflated" (amber) when the CI's lower bound exceeds alpha. BH-FDR (any q <= alpha)
  is reported next to it.
- :func:`sensitivity_fpr`: null lesion sets (random or relocated: the overlap counts of :class:`nulls.RelocationPool`
  draws, :func:`relocated_counts`) through the sensitivity routes: the classic overlap rule (at least x% of the
  maps pass), the null-referenced max-statistic FWE of the overlap excess (nulls.py), and their conjunction. Each
  null set is in turn the "observed" set against the others. The GLM half of a sensitivity x specificity
  conjunction is calibrated by :func:`empirical_fwer`.
- :func:`planted_network`: a simulated score planted on the lesions' connectivity to a target network:
  ``score = beta * z(t) + sqrt(1 - beta^2) * noise``, with t the mean Fz of each lesion's map inside the target
  (so beta is the population correlation of the score with the lesions' target connectivity), for each effect
  size (default 0.1, 0.2, 0.3) and ``n_sims`` simulations. Each simulation runs the configured sLNM
  (slnm.slnm_design and slnm.run: covariates, blocks, Pearson or Spearman). Reported per effect size: power (any
  FWE voxel inside the target) with its CI, any FWE voxel anywhere, the distance from the peak of the partial-r map
  to the target (mm), whether the peak is inside the target, and the spatial r with the truth (the noise-free sLNM
  map of t: the voxelwise partial correlation of the lesions' maps with their target connectivity). The same
  recovery metrics are reported after partialling the reference maps μC_r and PC1 (``lnm-reference-v1``) out of
  the sLNM map (similarity.partial_spatial_correlation and residual_map): a recovery that only follows the
  connectome's degree pattern shrinks there. The voxel inference itself is not partialled.

Costs (addendum §8, measured again here): one null dataset of 500 Freedman-Lane permutations takes about 4.3 s at n = 200,
p = 4, V = 228,483 (4 cores, 3 BLAS threads), so K = 100 null datasets x 500 permutations take about 7 min at p = 4 and
11 min at p = 6 (the addendum planned about 25 min); :func:`estimate_seconds` scales the measured cost by n x p x V,
and the job's ETA follows the measured time per null dataset.

Command line (JSON events, see ``jobs``; exit codes 0 done, 1 error, 2 usage or blocked design, 3 cancelled, 75
locked)::

    python -m LesionNetworkMappingLib.calibrate --maps maps.tsv --participants participants.tsv
        [--design design.json] --mask mask.nii.gz --out DIR [--k 100] [--inner-perm 500] [--seed 0]
        [--alpha 0.05] [--pool-maps maps.tsv] [--no-fwer]
        [--target MASK | --target-lesion SID] [--target-top-pct 5] [--effects 0.1,0.2,0.3] [--sims 20] [--no-planted]
        [--bundle B] [--sensitivity [--rule JSON] [--sign both|pos|neg] [--overlap 90] [--k-pool 10]]
        [--estimate] [--threads K] [--cancel-file F] [--resume] [--run-root DIR] [--parent-run ID]

A design.json with ``"slnm": {"method": "spearman", "flip": false}`` (written by slnm.adjust_design) is calibrated as
that sLNM. ``--pool-maps`` adds control lesions (a control set's maps.tsv) to the draws of a one-sample design.
The planted target is a mask on the connectome grid (``--target``), or by default the network of one of the
lesions (the top ``--target-top-pct`` % of its Fz map; ``--target-lesion``, else the lesion of median volume).
``--bundle`` (default: the bundle recorded in cohortmaps.json) gives the reference maps for the partialled metrics
and the relocations of ``--sensitivity``. Outputs: calibration.json (no absolute path), the target mask and the
truth map (``calibration_desc-{target,truth}_map.nii.gz``, on the mask's exact header). The run is registered (kind
"calibration") with the cohortmaps run of maps.tsv as parent. ``--resume`` keeps the null datasets and simulations
a stopped run finished (each is seeded by its index, so the result is the same).
"""

import os
import sys

if __name__ == "__main__":
    from LesionNetworkMappingLib.jobs import set_blas_threads

    set_blas_threads(sys.argv[1:])  # before numpy is imported: BLAS reads these variables once

import dataclasses
import math
import time

import numpy as np

from . import design as designlib
from . import glm, jobs, nifti, runs, similarity, slnm
from .export import DISCLAIMER, LIBRARY_VERSION, file_sha256, now_iso, versions
from .geometry import mask_indices

FORMAT_NAME = "slicer-lnm-calibration"
FORMAT_VERSION = 1
CALIBRATION_JSON = "calibration.json"
LOCK_NAME = ".calibration.lock"
RESUME_NAME = ".calibration-resume.json"
MAPS_MEMMAP = ".calibration-maps.npy"
FZ_MEMMAP = ".calibration-fz.npy"
TARGET_DESC = "target"
TRUTH_DESC = "truth"
#: Paths of calibration.json (dotted keys), stored relative to its folder (SPEC 9.7 rule 10)
RECORD_PATH_FIELDS = ("inputs.maps_tsv", "inputs.participants", "inputs.design", "inputs.mask", "inputs.pool_maps", "inputs.target", "inputs.bundle",
                      "inputs.cohortmaps_json", "planted.reference.path")

ALPHA = 0.05
CI_LEVEL = 0.95
CI_METHOD = "Clopper-Pearson (exact binomial)"
DEFAULT_K = 100
DEFAULT_INNER_PERM = 500
#: Fewer inner permutations cannot reach FWE p <= 0.05 reliably (the smallest attainable p is 1 / inner_perm)
MIN_INNER_PERM = 100
DEFAULT_EFFECTS = (0.1, 0.2, 0.3)
DEFAULT_SIMS = 20
DEFAULT_TARGET_TOP_PCT = 5.0
DEFAULT_K_POOL = 10
#: Columns added to in-memory copies of the study (never written)
SCORE_COLUMN = "calibration_score"
SELECTOR_COLUMN = "calibration_selected"
SELECTED, NOT_SELECTED = "yes", "no"
POOL_LEVEL = "calibration_pool"
#: Measured cost of one Freedman-Lane permutation, overheads included, and the size it was measured at (n x p x V):
#: 4.25 s per null dataset of 500 permutations at n = 200, p = 4, V = 228,483 (4 cores, 3 BLAS threads, numpy 2.4; L-48).
#: The addendum's planning figure (28.5 ms at p = 6, so about 25 min for K = 100 x 500) was about twice as slow.
PERMUTATION_S = 0.0083
PERMUTATION_REFERENCE_SIZE = 200 * 4 * 228_483
#: Fixed cost of one null dataset or simulation (design, preparation of the residualised maps), in permutations
DATASET_OVERHEAD_PERMUTATIONS = 12
#: Verdict levels of an error rate and the chip level of each
LEVELS = {"consistent": "ok", "conservative": "ok", "inflated": "warn", "none": "neutral"}
TRUTH_DEFINITION = ("the noise-free sLNM map of the planted score: the voxelwise partial correlation of the lesions' maps with their mean Fz "
                    "in the target (the target-connectivity map in lesion space)")
NULL_SOURCES = {
    "label_permutation": "group labels permuted within the design's exchangeability scheme",
    "score_permutation": "scores permuted within the design's exchangeability scheme",
    "random_subsets": "random sets of the same number of lesions drawn from every lesion with a map (no labels to permute in a one-sample design)",
}
ONE_SAMPLE_NOTE = ("A one-sample design tests whether the mean map differs from 0; lesion network maps share the connectome's degree pattern, "
                   "so random lesions pass it too. Compare the lesions with a control group (two-group design) or with null models.")


class NotCalibratable(ValueError):
    """The calibration cannot run on these inputs (the message says why and what to do)."""


# Rates and verdicts


def clopper_pearson(count, n, level=CI_LEVEL):
    """Exact (Clopper-Pearson) binomial confidence interval [low, high] of count successes in n trials."""
    from scipy import stats

    count, n = int(count), int(n)
    if n <= 0:
        return [float("nan"), float("nan")]
    tail = (1.0 - float(level)) / 2.0
    low = 0.0 if count <= 0 else float(stats.beta.ppf(tail, count, n - count + 1))
    high = 1.0 if count >= n else float(stats.beta.ppf(1.0 - tail, count + 1, n - count))
    return [low, high]


def rate_summary(count, n, level=CI_LEVEL):
    """{"count", "n", "rate", "ci": [low, high], "ci_level", "ci_method"} of a binomial rate."""
    count, n = int(count), int(n)
    return {"count": count, "n": n, "rate": count / n if n else None, "ci": clopper_pearson(count, n, level), "ci_level": level, "ci_method": CI_METHOD}


def _percent(value):
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{100.0 * value:.0f}%" if value >= 0.1 or value == 0 else f"{100.0 * value:.1f}%"


def rate_text(summary):
    """"0.04 (95% CI 0.011-0.099; 4 of 100)" with an en dash."""
    if not summary or not summary.get("n"):
        return "not computed"
    low, high = summary["ci"]
    return f"{summary['rate']:.2f} ({100 * summary['ci_level']:.0f}% CI {low:.3f}\u2013{high:.3f}; {summary['count']} of {summary['n']})"


def fwer_verdict(summary, alpha=ALPHA, test=None):
    """(level, text) of an empirical FWER: "inflated" when the CI's lower bound exceeds alpha (amber), "conservative"
    when its upper bound is below alpha, else "consistent"; "none" without null datasets.
    """
    if not summary or not summary.get("n"):
        return "none", "No null dataset was analysed."
    low, high = summary["ci"]
    text = f"Empirical FWER {rate_text(summary)} at \u03b1 = {alpha:g}: "
    if low > alpha:
        level = "inflated"
        text += "inflated — the configured analysis finds FWE-significant voxels in null data more often than \u03b1."
        if test == "one_sample":
            text += " " + ONE_SAMPLE_NOTE
        else:
            text += " Check the design (exchangeability blocks, covariates) and compare with the PALM export."
    elif high < alpha:
        level = "conservative"
        text += "conservative (fewer false positives than \u03b1; power may be lost)."
    else:
        level = "consistent"
        text += "consistent with \u03b1."
    return level, text


# Studies in memory


def _copy_study(study, rows):
    """An in-memory study with the given rows (dicts of TSV text); new columns are added; nothing is written."""
    from .study import Study

    return Study(study.path, dict(study.metadata), study.columns, rows, study.dictionary)


def _format_number(value):
    return repr(float(value))


def _with_values(study, column, values):
    """A copy of ``study`` whose ``column`` holds ``values`` ({participant_id: text}); other subjects get "n/a"."""
    rows = []
    for row in study.subjects:
        row[column] = values.get(row["participant_id"], "n/a")
        rows.append(row)
    return _copy_study(study, rows)


def add_pool(study, pool, group_column="group"):
    """A copy of ``study`` with control lesions appended (for the draws of a one-sample design).

    :param pool: {participant_id: volume_ml or None} of lesions that are not in the study (ids already in it are
      skipped). They get ``group_column`` = :data:`POOL_LEVEL` and review_status "approved" (control-set lesions are
      reviewed in their set; the copy is never written).
    :return: (study copy, ids added).
    """
    rows = study.subjects
    present = {row["participant_id"] for row in rows}
    added = []
    for sid, volume in pool.items():
        if sid in present:
            continue
        rows.append({"participant_id": sid, group_column or "group": POOL_LEVEL, designlib.VOLUME_COLUMN: "n/a" if volume is None else str(volume), "review_status": "approved"})
        added.append(sid)
    return _copy_study(study, rows), added


def slnm_settings(spec):
    """(method, flip) of a continuous design.json written by slnm.adjust_design ("pearson", False otherwise)."""
    extra = (getattr(spec, "extra", None) or {}).get("slnm") or {}
    method = extra.get("method") if extra.get("method") in slnm.METHODS else "pearson"
    return method, bool(extra.get("flip"))


def configured_design(study, spec):
    """The design of the configured analysis: ``design.build_design``, plus the sLNM ranks and flip of a continuous
    design whose spec records them. :return: (design.Design, sLNM method or None).
    """
    spec = designlib.as_spec(spec)
    design = designlib.build_design(study, spec)
    if spec.test == "continuous" and (spec.extra or {}).get("slnm"):
        method, flip = slnm_settings(spec)
        slnm.adjust_design(design, method, flip)
        return design, method
    return design, None


def null_source(spec):
    """"label_permutation", "score_permutation" or "random_subsets" (one-sample designs)."""
    test = designlib.as_spec(spec).test
    if test == "one_sample":
        return "random_subsets"
    return "score_permutation" if test == "continuous" else "label_permutation"


def one_sample_candidates(study, spec, sids):
    """Participant ids a one-sample null dataset may draw: subjects with a map whose covariates and block are present."""
    spec = designlib.as_spec(spec)
    required = [column for column in (*spec.covariates, spec.block_column) if column and column in study.columns]
    rows, _excluded = study.analysis_rows(required)
    with_maps = set(sids)
    return [row["participant_id"] for row in rows if row["participant_id"] in with_maps]


def _tested_column(spec):
    return spec.group_column if spec.test in designlib.GROUP_TESTS else spec.score_column


def null_studies(study, spec, design, k, seed=0, candidates=None):
    """Yield (index, null study, null spec) for the K null datasets of a design (see the module docstring).

    Group and score designs: the tested column's values are permuted among the design's subjects with
    ``design.permutations`` (the design's blocks and mode; the identity is skipped; every unique relabelling once when
    there are at most K of them). One-sample designs: a selector column marks a random set of ``design.n`` subjects of
    ``candidates`` (:func:`one_sample_candidates`).

    :raises NotCalibratable: a one-sample design with no lesion outside its own.
    """
    spec = designlib.as_spec(spec)
    if spec.test == "one_sample":
        pool = list(candidates if candidates is not None else study.subject_ids)
        if len(set(pool) - set(design.sids)) < 1 or len(pool) <= design.n:
            raise NotCalibratable(
                "A one-sample design has no labels to permute: its null datasets are random sets of lesions, and this study has no lesion "
                "outside the tested ones. Link a control set to the study (Library) or give control-lesion maps (--pool-maps). " + ONE_SAMPLE_NOTE)
        null_spec = dataclasses.replace(spec, group_column=SELECTOR_COLUMN, levels=[SELECTED], extra=dict(spec.extra))
        rng = np.random.default_rng([int(seed), 7])
        for index in range(int(k)):
            chosen = set(rng.choice(len(pool), size=design.n, replace=False).tolist())
            values = {sid: (SELECTED if position in chosen else NOT_SELECTED) for position, sid in enumerate(pool)}
            yield index, _with_values(study, SELECTOR_COLUMN, values), null_spec
        return
    column = _tested_column(spec)
    original = {row["participant_id"]: row.get(column) for row in study.subjects}
    labels = np.array([original[sid] for sid in design.sids], dtype=object)
    blocks = design.blocks if design.permute != "free" else None
    mode = design.permute if blocks is not None else "free"
    codes = np.unique(labels.astype(str), return_inverse=True)[1]
    perms = designlib.permutations(design.n, blocks, mode, int(k) + 1, int(seed), codes)
    next(perms)  # the identity
    for index, perm in enumerate(perms):
        relabelled = labels[np.argsort(perm)]
        values = dict(original)
        values.update(zip(design.sids, relabelled.tolist(), strict=True))
        yield index, _with_values(study, column, values), spec


# Empirical FWER


def _rows_of(sids, index):
    return np.fromiter((index[sid] for sid in sids), dtype=np.intp, count=len(sids))


class _Maps:
    """Maps of the analysed subjects (rows of Y) gathered in design order, cached on the order (and ranked for Spearman)."""

    def __init__(self, Y, sids):
        self.Y = Y
        self.index = {sid: row for row, sid in enumerate(sids)}
        if len(self.index) != len(sids):
            raise ValueError("The participant ids of the maps are not unique")
        self._key = None
        self._value = None

    def get(self, sids, ranked=False):
        key = (tuple(sids), bool(ranked))
        if key != self._key:
            missing = [sid for sid in sids if sid not in self.index]
            if missing:
                raise ValueError(f"{len(missing)} subject(s) of the design have no map, for example {missing[0]}")
            value = np.asarray(self.Y[_rows_of(sids, self.index)], dtype=np.float32)
            if ranked:
                value = slnm.rank_maps(value)
            self._key, self._value = key, value
        return self._value


def _null_outcome(index, design, result, alpha):
    p_fwe = result.p_fwe
    q = result.q_fdr
    return {
        "index": int(index),
        "n": int(design.n),
        "fwe_voxels": int(np.count_nonzero(p_fwe <= alpha)),
        "min_p_fwe": float(p_fwe.min()) if p_fwe.size else None,
        "fdr_voxels": int(np.count_nonzero(q <= alpha)),
        "n_perm": int(result.n_perm),
    }


def empirical_fwer(study, spec, Y, sids, k=DEFAULT_K, inner_perm=DEFAULT_INNER_PERM, seed=0, alpha=ALPHA, candidates=None, design=None, cancel=None,
                   progress=None, done=None, on_dataset=None, batch=None):
    """Empirical family-wise error rate of the configured design (see the module docstring).

    :param study: ``study.Study`` (in memory is fine) holding every subject a null dataset may use; for a one-sample
      design, lesions outside the tested ones (other groups, linked control sets, :func:`add_pool`).
    :param spec: the configured ``design.DesignSpec`` (or design.json dict or path).
    :param Y: (N, V) maps (array or memmap) of the subjects ``sids``, in that order (the design's input statistic).
    :param k: null datasets; ``inner_perm``: shufflings of each null dataset's permutation test, identity included.
    :param candidates: subjects a one-sample null dataset may draw (default :func:`one_sample_candidates`).
    :param design: the observed design (default: built from ``study`` and ``spec``); for one-sample designs it must
      be built without pool lesions.
    :param progress: ``progress(done, total)`` after each null dataset; ``done``: outcomes of a previous run to
      resume from (dataset indices are seeded, so the result is the same); ``on_dataset(outcome)`` after each new one.
    :return: JSON-compatible dict: "fwe" and "fdr" (:func:`rate_summary`), "level" and "text"
      (:func:`fwer_verdict`), "null_source", "datasets" (per null dataset), "design" and timings.
    :raises NotCalibratable: a blocked design, or a one-sample design without other lesions.
    """
    spec = designlib.as_spec(spec)
    if int(inner_perm) < MIN_INNER_PERM:
        raise ValueError(f"inner_perm must be at least {MIN_INNER_PERM} (the smallest attainable p is 1 / inner_perm), got {inner_perm}")
    if design is None:
        design, _method = configured_design(study, spec)
    if design.blocking:
        raise NotCalibratable("The design is blocked: " + " | ".join(check.message for check in design.blocking))
    method, _flip = slnm_settings(spec)
    ranked = spec.test == "continuous" and method == "spearman"
    if spec.test == "one_sample" and candidates is None:
        candidates = one_sample_candidates(study, spec, sids)
    maps = _Maps(Y, sids)
    previous = {int(item["index"]): item for item in (done or []) if isinstance(item, dict) and "index" in item}
    outcomes, skipped = [], []
    started = time.monotonic()
    computed = 0
    total = int(k)
    source = null_source(spec)
    for index, null_study, null_spec in null_studies(study, spec, design, k, seed, candidates):
        if index in previous:
            outcomes.append(previous[index])
        else:
            glm._check_cancel(cancel, len(outcomes))
            try:
                null_design, _ = configured_design(null_study, null_spec)
            except designlib.DesignError as exc:
                outcome = {"index": int(index), "skipped": f"the null design cannot be built: {exc}"}
            else:
                if null_design.blocking:
                    outcome = {"index": int(index), "skipped": "the null design is blocked: " + "; ".join(check.message for check in null_design.blocking)}
                else:
                    Yd = maps.get(null_design.sids, ranked)
                    result = glm.run(null_design, Yd, n_perm=int(inner_perm), seed=int(seed) + 1 + index, tfce=False, batch=batch, cancel=cancel)
                    outcome = _null_outcome(index, null_design, result, alpha)
            outcomes.append(outcome)
            computed += 1
            if on_dataset is not None:
                on_dataset(outcome)
        if progress is not None:
            progress(len(outcomes), total)
    total = len(outcomes)
    analysed = [item for item in outcomes if not item.get("skipped")]
    skipped = [item for item in outcomes if item.get("skipped")]
    fwe = rate_summary(sum(1 for item in analysed if item["fwe_voxels"] > 0), len(analysed))
    fdr = rate_summary(sum(1 for item in analysed if item["fdr_voxels"] > 0), len(analysed))
    level, text = fwer_verdict(fwe, alpha, spec.test)
    elapsed = time.monotonic() - started
    sizes = [item["n"] for item in analysed]
    return {
        "alpha": float(alpha),
        "k_requested": int(k),
        "k": len(analysed),
        "inner_perm": int(inner_perm),
        "min_attainable_p": 1.0 / int(inner_perm),
        "seed": int(seed),
        "null_source": source,
        "null_source_text": NULL_SOURCES[source],
        "n_candidates": len(candidates) if candidates is not None else None,
        "inference": "voxel max-stat FWE (primary; TFCE is not calibrated here) and BH-FDR",
        "fwe": fwe,
        "fdr": fdr,
        "level": level,
        "chip": LEVELS[level],
        "text": text,
        "skipped": len(skipped),
        "skipped_reasons": sorted({item["skipped"] for item in skipped})[:5],
        "n_range": [min(sizes), max(sizes)] if sizes else None,
        "datasets": outcomes,
        "design": _design_info(design),
        "timings": {"elapsed_s": round(elapsed, 3), "per_dataset_s": round(elapsed / computed, 4) if computed else None, "computed": computed},
        "exhausted": total < int(k),
    }


def _design_info(design):
    spec = design.spec
    method = slnm_settings(spec)[0] if spec.test == "continuous" else None
    return {
        "test": spec.test,
        "design_type": design.design_type,
        "n": int(design.n),
        "p": int(design.p),
        "stat": design.stat,
        "tails": design.tails,
        "contrast": spec.contrast,
        "label": glm.contrast_label(design),
        "exchange": design.exchange,
        "permute": design.permute,
        "block_column": design.block_column,
        "covariates": list(spec.covariates),
        "input": spec.input,
        "method": method,
    }


# Sensitivity route


def relocated_counts(pool, k, seed=0):
    """(K, n_parts, V) overlap counts (int16 or int32) of K null lesion sets drawn from a :class:`nulls.RelocationPool`
    (one relocation per lesion per set).
    """
    sets = pool.draw(int(k), int(seed))
    n_lesions = sets.shape[1] if sets.ndim == 2 else 0
    dtype = np.int16 if n_lesions < 32768 else np.int32
    out = np.zeros((sets.shape[0], pool.store.n_parts, pool.store.n_voxels), dtype=dtype)
    for row, columns in enumerate(sets):
        out[row] = pool.overlap(columns)
    return out


def sensitivity_fpr(null_counts, n_lesions, overlap=90.0, sign="both", alpha=ALPHA, kind="rl_independent"):
    """False-positive rates of the sensitivity routes on K null lesion sets (random or relocated lesions).

    Each null set in turn is the observed set: it is a false positive of the classic route when any voxel passes the
    classic rule (at least ``overlap`` % of the maps, sign-specific as nulls.classic_pass), of the null-referenced route
    when any voxel reaches max-statistic FWE p <= alpha against the other sets (the standardised excess of nulls.py,
    with the mean and SD of all K sets: slightly conservative), and of the conjunction when one voxel passes both.

    :param null_counts: (K, n_parts, V) overlap counts of the null sets (:func:`relocated_counts`).
    :return: JSON-compatible dict with "classic", "null_fwe" and "conjunction" (:func:`rate_summary` each, plus the
      mean share of voxels passing), K and the settings.
    """
    from . import nulls

    counts = np.asarray(null_counts)
    if counts.ndim != 3 or counts.shape[0] < 2:
        raise ValueError(f"Expected (K >= 2, n_parts, V) null overlap counts, got shape {counts.shape}")
    k, _parts, n_voxels = counts.shape
    total = counts.sum(axis=1, dtype=np.float64)
    mean = total.mean(axis=0)
    sd = total.std(axis=0, ddof=1)
    inverse = 1.0 / np.maximum(sd, nulls.SD_FLOOR)
    z = (total - mean) * inverse
    max_z = z.max(axis=1)
    classic_any, null_any, both_any, classic_share, null_share = [], [], [], [], []
    for b in range(k):
        passed = nulls.classic_pass(counts[b], n_lesions, overlap, sign)
        others = np.delete(max_z, b)
        exceed = others.size - np.searchsorted(np.sort(others), z[b], side="left")
        significant = (1.0 + exceed) / (others.size + 1.0) <= alpha
        classic_any.append(bool(passed.any()))
        null_any.append(bool(significant.any()))
        both_any.append(bool(np.any(passed & significant)))
        classic_share.append(float(passed.mean()) if n_voxels else 0.0)
        null_share.append(float(significant.mean()) if n_voxels else 0.0)

    def route(flags, shares):
        out = rate_summary(sum(flags), k)
        out["mean_share_of_voxels"] = float(np.mean(shares))
        return out

    classic = route(classic_any, classic_share)
    return {
        "k": int(k),
        "n_lesions": int(n_lesions),
        "overlap_pct": float(overlap),
        "sign": sign,
        "alpha": float(alpha),
        "null_kind": kind,
        "classic": classic,
        "null_fwe": route(null_any, null_share),
        "conjunction": route(both_any, [0.0]),
        "classic_text": f"Classic rule (≥{overlap:g}% of the maps) on null lesion sets: a voxel passes in {rate_text(classic)} sets; "
                        f"on average {_percent(classic['mean_share_of_voxels'])} of the voxels pass.",
        "note": "Each null set is compared with the others; the GLM (specificity) half of a sensitivity x specificity conjunction is calibrated by the empirical FWER.",
    }


# Planted network


def target_from_map(values, top_pct=DEFAULT_TARGET_TOP_PCT, sign="pos"):
    """(V,) bool target network: the top ``top_pct`` % of a map (positive values by default; "abs" for |map|)."""
    values = np.asarray(values, dtype=np.float64)
    scores = np.abs(values) if sign == "abs" else values
    finite = np.isfinite(scores)
    if not finite.any():
        raise ValueError("The map has no finite value")
    cut = np.percentile(scores[finite], 100.0 - float(top_pct))
    return finite & (scores >= cut) & ((scores > 0) if sign == "pos" else True)


def voxel_coordinates(indices, shape, affine):
    """(V, 3) RAS mm coordinates of flat C-order mask indices."""
    ijk = np.column_stack(np.unravel_index(np.asarray(indices), tuple(shape))).astype(np.float64)
    affine = np.asarray(affine, dtype=np.float64)
    return ijk @ affine[:3, :3].T + affine[:3, 3]


def _distance(coords, target_coords, voxel):
    if coords is None or voxel is None or target_coords is None or not len(target_coords):
        return None
    delta = target_coords - coords[voxel]
    return float(np.sqrt(np.min(np.einsum("ij,ij->i", delta, delta))))


def _peak(values):
    values = np.asarray(values, dtype=np.float64)
    if not np.isfinite(values).any():
        return None
    return int(np.nanargmax(np.where(np.isfinite(values), values, -np.inf)))


def _summary(values):
    values = np.asarray([value for value in values if value is not None and math.isfinite(value)], dtype=np.float64)
    if not values.size:
        return None
    q1, median, q3 = np.percentile(values, [25, 50, 75])
    return {"mean": float(values.mean()), "sd": float(values.std(ddof=1)) if values.size > 1 else 0.0, "median": float(median), "q1": float(q1), "q3": float(q3), "n": int(values.size)}


def _recovery(r_map, truth, target, coords, target_coords, covariates):
    """Recovery metrics of one sLNM map (raw, and after partialling the covariate maps when given)."""
    peak = _peak(r_map)
    out = {
        "peak": peak,
        "peak_in_target": bool(target[peak]) if peak is not None else None,
        "peak_distance_mm": _distance(coords, target_coords, peak),
        "spatial_r": similarity.pearson(r_map, truth),
    }
    if covariates is not None:
        residual = similarity.residual_map(r_map, covariates)
        partial_peak = _peak(residual)
        out["partial"] = {
            "peak": partial_peak,
            "peak_in_target": bool(target[partial_peak]) if partial_peak is not None else None,
            "peak_distance_mm": _distance(coords, target_coords, partial_peak),
            "spatial_r": similarity.partial_spatial_correlation(r_map, truth, covariates),
        }
    return out


def _planted_spec(spec):
    """sLNM settings of the planted simulations from the configured design (covariates, blocks, method)."""
    if spec is None:
        return {"covariates": [designlib.VOLUME_COLUMN], "transforms": {}, "block_column": None, "permute": "auto", "method": "pearson", "volume_off_reason": None, "input": "Fz"}
    spec = designlib.as_spec(spec)
    method = slnm_settings(spec)[0] if spec.test == "continuous" else "pearson"
    return {
        "covariates": list(spec.covariates),
        "transforms": {name: value for name, value in spec.transforms.items() if name in spec.covariates},
        "block_column": spec.block_column,
        "permute": spec.permute if spec.test == "continuous" else "auto",
        "method": method,
        "volume_off_reason": spec.volume_off_reason,
        "input": "Fz",
    }


def _slnm_design(study, settings):
    try:
        return slnm.slnm_design(study, SCORE_COLUMN, covariates=settings["covariates"], block_column=settings["block_column"], method=settings["method"],
                                permute=settings["permute"], transforms=settings["transforms"], volume_off_reason=settings["volume_off_reason"], input=settings["input"])
    except designlib.DesignError as exc:
        raise NotCalibratable(f"The sLNM design of the planted score cannot be built: {exc}") from None


def planted_network(study, Y, sids, target, effects=DEFAULT_EFFECTS, n_sims=DEFAULT_SIMS, inner_perm=DEFAULT_INNER_PERM, seed=0, spec=None, reference=None, coords=None,
                    alpha=ALPHA, cancel=None, progress=None, done=None, on_sim=None, batch=None, truth_out=None, subjects=None):
    """Planted-network recovery of the configured sLNM (see the module docstring).

    :param study: study (in memory is fine) with the covariates of the lesions ``sids``.
    :param Y: (N, V) exact per-lesion Fz maps of ``sids`` (array or memmap), in that order.
    :param target: (V,) bool target network (a region or network mask on the maps' voxels; :func:`target_from_map`).
    :param effects: population correlations beta of the score with the target connectivity (each in (0, 1)).
    :param spec: the configured design spec: its covariates, block column and sLNM method are used (default: volume, Pearson).
    :param reference: (k, V) covariate maps [μC_r, PC1] for the partialled metrics, or None (not computed).
    :param coords: (V, 3) mm coordinates of the voxels for the peak distance (None: not computed).
    :param done: {"<effect index>:<sim>": outcome} of a previous run to resume from; ``on_sim(key, outcome)`` after each new one.
    :param truth_out: optional dict that receives the truth map ("truth", (V,) float64).
    :param subjects: participant ids to plant the score on (default: every lesion of ``sids``), for example the
      subjects of the configured design.
    :return: JSON-compatible dict: "effects" (per beta: power, any FWE, peak distance, peak in target, spatial r and
      their partialled versions), the target, the truth definition and its similarity with the reference maps.
    :raises NotCalibratable: an empty target, a design that cannot be built, or no variation of the target connectivity.
    """
    target = np.asarray(target, dtype=bool).reshape(-1)
    n_voxels = Y.shape[1]
    if target.size != n_voxels:
        raise ValueError(f"The target has {target.size} voxels, the maps have {n_voxels}")
    if not target.any():
        raise NotCalibratable("The target network has no voxel")
    effects = [float(beta) for beta in effects]
    if any(not 0.0 < beta < 1.0 for beta in effects):
        raise ValueError(f"Effect sizes are correlations between 0 and 1 (exclusive), got {effects}")
    if int(inner_perm) < MIN_INNER_PERM:
        raise ValueError(f"inner_perm must be at least {MIN_INNER_PERM}, got {inner_perm}")
    settings = _planted_spec(spec)
    maps = _Maps(Y, sids)
    keep = set(sids) & set(study.subject_ids) & (set(subjects) if subjects is not None else set(sids))
    used = [sid for sid in sids if sid in keep]
    if len(used) < 3:
        raise NotCalibratable(f"The planted network needs at least 3 lesions with a map, got {len(used)}")
    # Target connectivity of each lesion: the mean Fz of its map inside the target
    connectivity = {}
    rows = _rows_of(used, maps.index)
    for start in range(0, len(used), 64):
        block = np.asarray(Y[rows[start:start + 64]], dtype=np.float64)
        for offset, value in enumerate(block[:, target].mean(axis=1)):
            connectivity[used[start + offset]] = float(value)
    base_rows = [row for row in study.subjects if row["participant_id"] in connectivity]
    base = _with_values(_copy_study(study, base_rows), SCORE_COLUMN, {sid: _format_number(value) for sid, value in connectivity.items()})
    sdesign0 = _slnm_design(base, settings)
    design0 = sdesign0.design
    if design0.blocking:
        raise NotCalibratable("The sLNM design of the planted score is blocked: " + " | ".join(check.message for check in design0.blocking))
    order = list(design0.sids)
    t = np.array([connectivity[sid] for sid in order])
    sd = float(t.std())
    if not sd > 0:
        raise NotCalibratable("The lesions' connectivity to the target does not vary: no score can be planted on it")
    zt = (t - t.mean()) / sd
    ranked = settings["method"] == "spearman"
    Yd = maps.get(order, ranked)
    truth = slnm.partial_r_map(sdesign0.x, Yd, sdesign0.covariates)
    if truth_out is not None:
        truth_out["truth"] = truth
    covariates = None if reference is None else np.atleast_2d(np.asarray(reference, dtype=np.float64))
    target_coords = coords[target] if coords is not None else None
    n = len(order)
    previous = dict(done or {})
    total = len(effects) * int(n_sims)
    finished = 0
    started = time.monotonic()
    computed = 0
    per_effect = []
    for effect_index, beta in enumerate(effects):
        outcomes = []
        for sim in range(int(n_sims)):
            key = f"{effect_index}:{sim}"
            if key in previous:
                outcomes.append(previous[key])
            else:
                glm._check_cancel(cancel, finished)
                rng = np.random.default_rng([int(seed), 11, effect_index, sim])
                score = beta * zt + math.sqrt(1.0 - beta * beta) * rng.standard_normal(n)
                simulated = _with_values(base, SCORE_COLUMN, {sid: _format_number(value) for sid, value in zip(order, score, strict=True)})
                sdesign = _slnm_design(simulated, settings)
                if list(sdesign.design.sids) != order:
                    raise RuntimeError("The planted design changed its subjects")
                result = slnm.run(sdesign, Yd, n_perm=int(inner_perm), seed=int(seed) + 101 + 1000 * effect_index + sim, ranked=True, batch=batch, cancel=cancel)
                fwe = result.p_fwe <= alpha
                outcome = {
                    "sim": sim,
                    "fwe_in_target": bool(np.any(fwe & target)),
                    "fwe_any": bool(fwe.any()),
                    "fwe_voxels": int(np.count_nonzero(fwe)),
                    "fwe_voxels_in_target": int(np.count_nonzero(fwe & target)),
                    "sample_r": float(np.corrcoef(score, zt)[0, 1]),
                    **_recovery(result.effect, truth, target, coords, target_coords, covariates),
                }
                outcomes.append(outcome)
                computed += 1
                if on_sim is not None:
                    on_sim(key, outcome)
            finished += 1
            if progress is not None:
                progress(finished, total)
        per_effect.append(_effect_summary(beta, outcomes, covariates is not None))
    elapsed = time.monotonic() - started
    info = {
        "effects": per_effect,
        "n": n,
        "n_sims": int(n_sims),
        "inner_perm": int(inner_perm),
        "alpha": float(alpha),
        "seed": int(seed),
        "target": {"n_voxels": int(np.count_nonzero(target)), "share_of_voxels": float(target.mean())},
        "model": "score = beta * z(mean Fz in the target) + sqrt(1 - beta^2) * N(0, 1); beta is the population correlation of the score with the target connectivity",
        "truth": TRUTH_DEFINITION,
        "settings": {key: value for key, value in settings.items() if key != "input"},
        "power_definition": f"a simulation recovers the network when any voxel inside the target reaches FWE p <= {alpha:g}",
        "partialled": covariates is not None,
        "timings": {"elapsed_s": round(elapsed, 3), "per_simulation_s": round(elapsed / computed, 4) if computed else None, "computed": computed},
    }
    if covariates is not None:
        info["truth_reference_r"] = [similarity.pearson(truth, row) for row in covariates]
        info["truth_r2_explained"] = similarity.r2_explained(truth, covariates)
    return info


def _effect_summary(beta, outcomes, partialled):
    n = len(outcomes)
    out = {
        "beta": beta,
        "n_sims": n,
        "power": rate_summary(sum(1 for item in outcomes if item["fwe_in_target"]), n),
        "any_fwe": rate_summary(sum(1 for item in outcomes if item["fwe_any"]), n),
        "peak_in_target": rate_summary(sum(1 for item in outcomes if item.get("peak_in_target")), n),
        "peak_distance_mm": _summary(item.get("peak_distance_mm") for item in outcomes),
        "spatial_r": _summary(item.get("spatial_r") for item in outcomes),
        "simulations": outcomes,
    }
    if partialled:
        partials = [item.get("partial") or {} for item in outcomes]
        out["partial"] = {
            "peak_in_target": rate_summary(sum(1 for item in partials if item.get("peak_in_target")), n),
            "peak_distance_mm": _summary(item.get("peak_distance_mm") for item in partials),
            "spatial_r": _summary(item.get("spatial_r") for item in partials),
        }
    return out


# Costs


def permutation_seconds(n, p, n_voxels):
    """Estimated seconds of one Freedman-Lane permutation (8.3 ms at n = 200, p = 4, V = 228,483; see :data:`PERMUTATION_S`)."""
    return PERMUTATION_S * max(int(n), 1) * max(int(p), 1) * max(int(n_voxels), 1) / PERMUTATION_REFERENCE_SIZE


def estimate_seconds(n, n_voxels, p=6, k=DEFAULT_K, inner_perm=DEFAULT_INNER_PERM, effects=DEFAULT_EFFECTS, n_sims=DEFAULT_SIMS, fwer=True, planted=True):
    """Estimated run time (seconds) of a calibration: (K + effects x sims) x (inner_perm + overhead) permutations."""
    units = (int(k) if fwer else 0) + (len(effects) * int(n_sims) if planted else 0)
    return units * (int(inner_perm) + DATASET_OVERHEAD_PERMUTATIONS) * permutation_seconds(n, p, n_voxels)


# Checklist


def latest_record(root, run_records=None):
    """(run id, calibration.json record with absolute paths) of the newest finished calibration of a run root, or (None, None)."""
    record = runs.latest(root, "calibration", status="done", runs=run_records)
    if record is None:
        return None, None
    folder = runs.run_dir(root, record)
    data = read_record(os.path.join(folder, CALIBRATION_JSON)) if folder else None
    return record.get("id"), data


def checklist_evidence(root, run_records=None):
    """(status, value, evidence) of the newest calibration for the reporting checklist (item F8, design and
    correction): "done" when the empirical FWER is consistent with alpha, "flagged" when it is inflated or the
    calibration was partial, "not_run" without a calibration.
    """
    run_id, data = latest_record(root, run_records)
    if data is None:
        return "not_run", "no pipeline calibration", []
    fwer = data.get("fwer") or {}
    parts = []
    status = "done"
    if fwer.get("fwe"):
        parts.append("empirical FWER " + rate_text(fwer["fwe"]))
        if fwer.get("level") == "inflated":
            status = "flagged"
            parts[-1] += " (inflated)"
    else:
        status = "flagged"
        parts.append("empirical FWER not computed" + (f" ({fwer.get('reason')})" if fwer.get("reason") else ""))
    planted = data.get("planted") or {}
    for effect in planted.get("effects") or []:
        parts.append(f"power at beta={effect['beta']:g}: {rate_text(effect['power'])}")
    return status, "; ".join(parts), [run_id] if run_id else []


# Files


def write_record(path, record):
    """Write calibration.json atomically with its paths relative to its folder (SPEC 9.7 rule 10)."""
    from .export import portable_record_paths

    jobs.write_json_atomic(path, portable_record_paths(record, os.path.dirname(os.path.abspath(path)), RECORD_PATH_FIELDS))


def read_record(path):
    """calibration.json with absolute paths, or None."""
    from .export import resolve_record_paths

    record = glm._read_json(path)
    return resolve_record_paths(record, os.path.dirname(os.path.abspath(path)), RECORD_PATH_FIELDS) if record is not None else None


def map_path(out_dir, desc):
    return os.path.join(out_dir, f"calibration_desc-{desc}_map.nii.gz")


# Command line


def _parser():
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.calibrate", description="Calibrate an LNM analysis: empirical FWER of its design, sensitivity-route FPR and planted-network recovery.")
    parser.add_argument("--maps", required=True, help="maps.tsv of a cohortmaps run (participant_id, AvgFz, T)")
    parser.add_argument("--participants", required=True, help="participants.tsv (of a study, or a table with participants.json)")
    parser.add_argument("--design", default=None, help="design.json of the analysis (default: two groups, case vs control, volume covariate)")
    parser.add_argument("--mask", required=True, help="connectome mask (the maps' grid)")
    parser.add_argument("--out", required=True, help="output folder")
    parser.add_argument("--k", type=jobs.positive_int, default=DEFAULT_K, help=f"null datasets of the empirical FWER (default {DEFAULT_K})")
    parser.add_argument("--inner-perm", type=jobs.positive_int, default=DEFAULT_INNER_PERM, help=f"permutations of each null dataset and simulation, identity included (default {DEFAULT_INNER_PERM}; at least {MIN_INNER_PERM})")
    parser.add_argument("--seed", type=int, default=0, help="seed (default 0)")
    parser.add_argument("--alpha", type=float, default=ALPHA, help=f"FWE and FDR level (default {ALPHA:g})")
    parser.add_argument("--pool-maps", default=None, help="maps.tsv of control lesions added to the draws of a one-sample design (a control set's cohortmaps run)")
    parser.add_argument("--no-fwer", action="store_true", help="skip the empirical FWER")
    parser.add_argument("--target", default=None, help="target network mask on the connectome grid (non-zero voxels)")
    parser.add_argument("--target-lesion", default=None, help="participant whose Fz map's top voxels are the target network (default: the lesion of median volume)")
    parser.add_argument("--target-top-pct", type=float, default=DEFAULT_TARGET_TOP_PCT, help=f"top % of the lesion's Fz map that makes the target (default {DEFAULT_TARGET_TOP_PCT:g})")
    parser.add_argument("--effects", default=",".join(f"{beta:g}" for beta in DEFAULT_EFFECTS), help="planted effect sizes (correlations), comma-separated (default 0.1,0.2,0.3)")
    parser.add_argument("--sims", type=jobs.positive_int, default=DEFAULT_SIMS, help=f"simulations per effect size (default {DEFAULT_SIMS})")
    parser.add_argument("--no-planted", action="store_true", help="skip the planted-network recovery")
    parser.add_argument("--bundle", default=None, help="connectome bundle (reference maps μC_r and PC1; relocations of --sensitivity); default: the bundle of cohortmaps.json")
    parser.add_argument("--sensitivity", action="store_true", help="also calibrate the sensitivity route on relocated lesion sets (needs the bundle)")
    parser.add_argument("--rule", default=None, help="threshold rule of the sensitivity route (JSON; default FWE)")
    parser.add_argument("--sign", choices=("both", "pos", "neg"), default="both", help="sign of the sensitivity route (default both)")
    parser.add_argument("--overlap", type=float, default=90.0, help="classic overlap rule, % of the maps (default 90)")
    parser.add_argument("--k-pool", type=jobs.positive_int, default=DEFAULT_K_POOL, help=f"relocations per lesion of the sensitivity route (default {DEFAULT_K_POOL})")
    parser.add_argument("--estimate", action="store_true", help="only report the estimated run time (an 'estimate' event)")
    jobs.add_common_arguments(parser)
    return parser


def _floats(text):
    try:
        values = [float(item) for item in str(text).split(",") if item.strip()]
    except ValueError:
        raise jobs.JobError(f"Invalid effect sizes {text!r}", code="usage", exit_code=jobs.EXIT_USAGE) from None
    if not values or any(not 0.0 < value < 1.0 for value in values):
        raise jobs.JobError(f"Effect sizes are correlations between 0 and 1 (exclusive), got {text!r}", code="usage", exit_code=jobs.EXIT_USAGE)
    return values


def main(argv=None):
    """Command-line entry point (see the module docstring); returns the exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(argv)
    if args.seed < 0:
        raise jobs.JobError(f"--seed must not be negative, got {args.seed}", code="usage", exit_code=jobs.EXIT_USAGE)
    if args.inner_perm < MIN_INNER_PERM:
        raise jobs.JobError(f"--inner-perm must be at least {MIN_INNER_PERM}, got {args.inner_perm}", code="usage", exit_code=jobs.EXIT_USAGE)
    if not 0.0 < args.alpha < 0.5:
        raise jobs.JobError(f"--alpha must be between 0 and 0.5, got {args.alpha}", code="usage", exit_code=jobs.EXIT_USAGE)
    if not 0.0 < args.target_top_pct < 100.0:
        raise jobs.JobError(f"--target-top-pct must be between 0 and 100, got {args.target_top_pct}", code="usage", exit_code=jobs.EXIT_USAGE)
    if args.target and args.target_lesion:
        raise jobs.JobError("Give --target or --target-lesion, not both", code="usage", exit_code=jobs.EXIT_USAGE)
    if args.no_fwer and args.no_planted and not args.sensitivity:
        raise jobs.JobError("Nothing to calibrate (--no-fwer and --no-planted without --sensitivity)", code="usage", exit_code=jobs.EXIT_USAGE)
    return _run_cli(args, argv, _floats(args.effects))


def _spec_for(path):
    try:
        return designlib.DesignSpec.load(path) if path else designlib.DesignSpec()
    except (OSError, ValueError) as exc:
        raise jobs.JobError(f"The design {path} cannot be read: {exc}", code="usage", exit_code=jobs.EXIT_USAGE) from None


def _pool_rows(path, column, study_ids):
    """({participant_id: map path}, {participant_id: volume or None}) of the pool lesions not in the study."""
    mapped = glm._map_rows(os.path.abspath(path), column)
    paths = {sid: value[0] for sid, value in mapped.items() if sid not in study_ids}
    volumes = {sid: value[1] for sid, value in mapped.items() if sid not in study_ids}
    return paths, volumes


def _median_volume_lesion(study, sids):
    volumes = []
    for row in study.subjects:
        if row["participant_id"] not in sids:
            continue
        try:
            volumes.append((float(row.get(designlib.VOLUME_COLUMN)), row["participant_id"]))
        except (TypeError, ValueError):
            continue
    if not volumes:
        return sorted(sids)[len(sids) // 2] if sids else None
    volumes.sort()
    return volumes[len(volumes) // 2][1]


def _read_mask(path, mask_shape, mask_affine, indices):
    data, affine, header = nifti.read_nifti(path)
    data = np.asarray(data)
    if data.ndim > 3:
        data = data.reshape(data.shape[:3]) if int(np.prod(data.shape[3:])) == 1 else None
    if data is None or tuple(data.shape) != tuple(mask_shape) or not np.allclose(affine, mask_affine, atol=1e-4, rtol=0):
        raise jobs.JobError(f"The target {path} is not on the grid of the connectome mask (shape {tuple(header['shape'])})", code="usage", exit_code=jobs.EXIT_USAGE)
    return np.asarray(data).reshape(-1)[indices] != 0


def _reference(args, cohort, subjects_sha, indices, mask_shape):
    """(covariate maps (2, V) or None, info dict or None, reason or None) from the bundle's reference maps."""
    if not (args.bundle or ((cohort or {}).get("bundle") or {}).get("path")):
        return None, None, "no connectome bundle (--bundle)"
    ref, reason, info = slnm._reference_for(args, cohort, subjects_sha, indices, mask_shape)
    if ref is None:
        return None, None, reason
    return ref.covariates(slnm.REFERENCE_COVARIATES), info, None


def _units_progress(emit, stage, offset, total, started, first):
    def report(done, _stage_total):
        spent = time.monotonic() - started
        new = offset + done - first["done"]
        eta = spent / new * max(total - offset - done, 0) if new > 0 else None
        emit("progress", stage=stage, done=offset + done, total=total, elapsed=round(spent, 3), eta=None if eta is None else round(eta, 3))

    return report


def _run_cli(args, argv, effects):
    emit = jobs.emit
    cancel = jobs.CancelToken(args.cancel_file)
    started = time.monotonic()
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    lock = jobs.OutputLock(os.path.join(out_dir, LOCK_NAME))
    lock.acquire()
    run_root, run_id, record = None, None, None
    temporaries = []
    try:
        # Inputs and the configured design
        study, in_study = glm._load_study(args.participants)
        spec = _spec_for(args.design)
        column = glm.MAP_COLUMNS[spec.input]
        maps_tsv = os.path.abspath(args.maps)
        mapped = glm._map_rows(maps_tsv, column)
        fz_mapped = mapped if column == "AvgFz" else glm._map_rows(maps_tsv, "AvgFz")
        cohort_json = os.path.join(os.path.dirname(maps_tsv), "cohortmaps.json")
        cohort = glm.cohortmaps_record(cohort_json)
        cohort_run = (cohort or {}).get("run_id")
        with_maps = {sid for sid in study.subject_ids if sid in mapped}
        if not with_maps:
            raise jobs.JobError(f"No subject of {args.participants} has a map in {maps_tsv}", code="design_blocked", exit_code=jobs.EXIT_USAGE)
        subset, _filled = glm._subset_study(study, with_maps, {sid: values[1] for sid, values in mapped.items()})
        try:
            design, _method = configured_design(subset, spec)
        except designlib.DesignError as exc:
            raise jobs.JobError(f"The design cannot be built: {exc}", code="design_blocked", exit_code=jobs.EXIT_USAGE) from None
        pooling = glm._pooling_checks(design, mapped, cohort)
        if pooling:
            design.checks = designlib._ordered([*pooling, *design.checks])
        if design.blocking:
            messages = [check.message for check in design.blocking]
            raise jobs.JobError("The design is blocked: " + " | ".join(messages), code="design_blocked", exit_code=jobs.EXIT_USAGE, checks=[check.to_dict() for check in design.blocking])
        pool_paths, pool_volumes = ({}, {})
        if args.pool_maps:
            if spec.test == "one_sample":
                pool_paths, pool_volumes = _pool_rows(args.pool_maps, column, set(study.subject_ids))
            else:
                emit("warning", code="pool_ignored", message="--pool-maps is used by one-sample designs only; it is ignored")
        null_study, pool_ids = add_pool(subset, pool_volumes, spec.group_column) if pool_paths else (subset, [])
        all_sids = list(subset.subject_ids) + list(pool_ids)
        all_paths = [mapped[sid][0] for sid in subset.subject_ids] + [pool_paths[sid] for sid in pool_ids]

        mask_data, mask_affine, mask_header = nifti.read_nifti(args.mask)
        mask_shape = tuple(mask_header["shape"][:3])
        indices = mask_indices(np.asarray(mask_data).reshape(mask_shape) != 0)
        n_voxels = int(indices.size)
        estimate = estimate_seconds(design.n, n_voxels, design.p, args.k, args.inner_perm, effects, args.sims, fwer=not args.no_fwer, planted=not args.no_planted)
        if args.estimate:
            emit("estimate", seconds=round(estimate, 1), n=design.n, p=design.p, n_voxels=n_voxels, k=args.k, inner_perm=args.inner_perm, effects=effects, sims=args.sims)
            emit("done", outputs=[], estimate_s=round(estimate, 1))
            return jobs.EXIT_OK

        params = {
            "design": runs.params_sha256(design.to_dict(matrix=True)),
            "maps": runs.params_sha256([[sid, path, *_stat_key(path)] for sid, path in zip(all_sids, all_paths, strict=True)]),
            "mask_sha256": file_sha256(args.mask),
            "k": args.k,
            "inner_perm": args.inner_perm,
            "seed": args.seed,
            "alpha": args.alpha,
            "effects": effects,
            "sims": args.sims,
            "target": [args.target and file_sha256(args.target), args.target_lesion, args.target_top_pct],
            "fwer": not args.no_fwer,
            "planted": not args.no_planted,
            "sensitivity": [bool(args.sensitivity), args.rule, args.sign, args.overlap, args.k_pool],
        }
        run_root = os.path.abspath(args.run_root or (study.path if in_study else out_dir))
        parents = [run for run in [cohort_run, *args.parent_run] if run]
        run_id = runs.register(run_root, "calibration", out_dir, parents=parents, params={**params, "test": spec.test}, seeds={"calibration": args.seed})
        record = {"format": FORMAT_NAME, "version": FORMAT_VERSION, "run_id": run_id, "status": "running", "argv": list(argv), "start": now_iso(), "end": None}
        emit("start", job="calibration", argv=list(argv), threads=args.threads, run_id=run_id, n=design.n, p=design.p, n_voxels=n_voxels, test=spec.test,
             design_type=design.design_type, k=args.k, inner_perm=args.inner_perm, estimate_s=round(estimate, 1))
        state = jobs.ResumeState(os.path.join(out_dir, RESUME_NAME))
        if not args.resume or state.data.get("params") != params:
            if args.resume and state.data:
                emit("warning", code="resume_mismatch", message="The saved progress belongs to other inputs or settings; the calibration starts again.")
            state.clear()
        state.data.setdefault("params", params)
        state.data.setdefault("fwer", [])
        state.data.setdefault("planted", {})
        state.save()
        previous_json = os.path.join(out_dir, CALIBRATION_JSON)
        if os.path.exists(previous_json):
            os.remove(previous_json)

        # Maps
        emit("stage", name="maps", index=1, total=4)
        memmap_path = os.path.join(out_dir, MAPS_MEMMAP)
        temporaries.append(memmap_path)
        Y = glm._load_maps(all_paths, mask_shape, mask_affine, indices, memmap_path, emit)
        Yfz = Y
        if column != "AvgFz" and not args.no_planted:
            fz_path = os.path.join(out_dir, FZ_MEMMAP)
            temporaries.append(fz_path)
            fz_sids = [sid for sid in subset.subject_ids if sid in fz_mapped]
            Yfz = glm._load_maps([fz_mapped[sid][0] for sid in fz_sids], mask_shape, mask_affine, indices, fz_path, emit)
        else:
            fz_sids = all_sids
        units = (0 if args.no_fwer else args.k) + (0 if args.no_planted else len(effects) * args.sims)
        clock = time.monotonic()
        first = {"done": len(state.data["fwer"]) + len(state.data["planted"])}

        # Empirical FWER
        fwer = None
        emit("stage", name="fwer", index=2, total=4)
        if not args.no_fwer:
            def keep_dataset(outcome):
                state.data["fwer"].append(outcome)
                state.save()

            try:
                fwer = empirical_fwer(null_study, spec, Y, all_sids, k=args.k, inner_perm=args.inner_perm, seed=args.seed, alpha=args.alpha, design=design, cancel=cancel,
                                      progress=_units_progress(emit, "fwer", 0, units, clock, first), done=state.data["fwer"], on_dataset=keep_dataset)
            except NotCalibratable as exc:
                fwer = {"level": "none", "chip": LEVELS["none"], "reason": str(exc), "text": str(exc), "design": _design_info(design)}
                emit("warning", code="fwer_not_calibratable", message=str(exc))
            emit("result", kind="fwer", level=fwer.get("level"), text=fwer.get("text"))

        # Sensitivity route
        sensitivity = None
        emit("stage", name="sensitivity", index=3, total=4)
        if args.sensitivity:
            sensitivity = _sensitivity_cli(args, maps_tsv, cohort, emit, cancel)

        # Planted network
        planted = None
        emit("stage", name="planted", index=4, total=4)
        target_path = None
        truth_path = None
        if not args.no_planted:
            planted, target_vec, truth = _planted_cli(args, effects, subset, spec, design, Yfz, fz_sids, indices, mask_shape, mask_affine, cohort, state, emit, cancel,
                                                       _units_progress(emit, "planted", 0 if args.no_fwer else args.k, units, clock, first))
            if target_vec is not None:
                record_header = nifti.read_raw_header(args.mask)
                target_path = glm._write_on_mask(map_path(out_dir, TARGET_DESC), target_vec.astype(np.float32), record_header, indices, mask_shape, 0.0, "LNM calibration planted target network")
                if truth is not None:
                    truth_path = glm._write_on_mask(map_path(out_dir, TRUTH_DESC), truth, record_header, indices, mask_shape, 0.0, "LNM calibration truth (noise-free sLNM r)")
            emit("result", kind="planted", effects=[{"beta": effect["beta"], "power": effect["power"]["rate"]} for effect in (planted or {}).get("effects") or []])

        outputs = [path for path in (target_path, truth_path) if path]
        record.update({
            "status": "done",
            "end": now_iso(),
            "elapsed": round(time.monotonic() - started, 3),
            "library_version": LIBRARY_VERSION,
            "versions": versions(),
            "threads": args.threads,
            "inputs": {
                "maps_tsv": maps_tsv,
                "participants": os.path.abspath(args.participants),
                "design": os.path.abspath(args.design) if args.design else None,
                "mask": os.path.abspath(args.mask),
                "pool_maps": os.path.abspath(args.pool_maps) if args.pool_maps else None,
                "target": os.path.abspath(args.target) if args.target else None,
                "cohortmaps_json": cohort_json if cohort else None,
                "cohortmaps_run": cohort_run,
                "n_pool": len(pool_ids),
            },
            "design": _design_info(design),
            "design_warnings": [check.message for check in design.warnings],
            "estimate_s": round(estimate, 1),
            "fwer": _strip(fwer, "datasets") if fwer is not None else None,
            "sensitivity": sensitivity,
            "planted": planted,
            "outputs": {key: os.path.basename(path) for key, path in (("target", target_path), ("truth", truth_path)) if path},
            "details": {"fwer_datasets": (fwer or {}).get("datasets")},
            "disclaimer": DISCLAIMER,
        })
        json_path = os.path.join(out_dir, CALIBRATION_JSON)
        write_record(json_path, record)
        for path in [*outputs, json_path]:
            emit("result", kind="calibration", path=path)
        state.clear()
        summary = {"test": spec.test}
        if fwer and fwer.get("fwe"):
            summary.update({"fwer": fwer["fwe"]["rate"], "fwer_ci": fwer["fwe"]["ci"], "level": fwer["level"]})
        if planted:
            summary["power"] = {f"{effect['beta']:g}": effect["power"]["rate"] for effect in planted["effects"]}
        runs.update(run_root, run_id, status="done", ended=record["end"], outputs=[os.path.basename(path) for path in [*outputs, json_path]], summary=summary)
        emit("done", outputs=[*outputs, json_path], elapsed=round(time.monotonic() - started, 3), run_id=run_id)
        return jobs.EXIT_OK
    except jobs.Cancelled as exc:
        if record is not None:
            record.update({"status": "cancelled", "end": now_iso(), "stop_reason": exc.reason})
            write_record(os.path.join(out_dir, CALIBRATION_JSON), record)
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
        for path in temporaries:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        lock.release()


def _stat_key(path):
    try:
        info = os.stat(path)
    except OSError:
        raise jobs.JobError(f"The map is missing: {path}", code="map_missing") from None
    return [info.st_size, info.st_mtime_ns]


def _strip(data, *keys):
    return {key: value for key, value in data.items() if key not in keys}


def _planted_cli(args, effects, study, spec, design, Yfz, fz_sids, indices, mask_shape, mask_affine, cohort, state, emit, cancel, progress):
    """(planted dict or a not-computed dict, target vector or None, truth map or None) of the command line."""
    sids = [sid for sid in design.sids if sid in set(fz_sids)]
    target_info = {}
    try:
        if args.target:
            target = _read_mask(args.target, mask_shape, mask_affine, indices)
            target_info = {"kind": "mask", "source": os.path.basename(args.target)}
        else:
            lesion = args.target_lesion or _median_volume_lesion(study, set(sids))
            if lesion not in fz_sids:
                raise NotCalibratable(f"The target lesion {lesion} has no Fz map")
            row = list(fz_sids).index(lesion)
            target = target_from_map(np.asarray(Yfz[row], dtype=np.float64), args.target_top_pct)
            target_info = {"kind": "lesion_network", "participant_id": lesion, "top_pct": args.target_top_pct,
                           "text": f"the network of {lesion}: the top {args.target_top_pct:g}% of its Fz map"}
        subjects_sha = None
        if cohort and cohort.get("subject_ids_sha256"):
            subjects_sha = cohort["subject_ids_sha256"]
        reference, ref_info, ref_reason = _reference(args, cohort, subjects_sha, indices, mask_shape)
        if reference is None:
            emit("warning", code="no_reference", message=f"The partialled recovery (μC_r, PC1) is not computed: {ref_reason}")
        coords = voxel_coordinates(indices, mask_shape, mask_affine)
        truth_holder = {}

        def keep(key, outcome):
            state.data["planted"][key] = outcome
            state.save()

        planted = planted_network(study, Yfz, list(fz_sids), target, effects=effects, n_sims=args.sims, inner_perm=args.inner_perm, seed=args.seed, spec=spec,
                                  reference=reference, coords=coords, alpha=args.alpha, cancel=cancel, progress=progress, done=state.data["planted"], on_sim=keep,
                                  truth_out=truth_holder, subjects=sids)
        planted["target"].update(target_info)
        planted["reference"] = ref_info if ref_info else {"reason": ref_reason}
        return planted, target, truth_holder.get("truth")
    except NotCalibratable as exc:
        emit("warning", code="planted_not_calibratable", message=str(exc))
        return {"reason": str(exc), "target": target_info, "effects": []}, None, None


def _sensitivity_cli(args, maps_tsv, cohort, emit, cancel):
    """The sensitivity-route calibration on relocated lesion sets (exact pooled relocations; needs the bundle)."""
    from . import connectome, nulls, thresholds

    path = args.bundle or ((cohort or {}).get("bundle") or {}).get("path")
    if not path:
        emit("warning", code="sensitivity_not_run", message="The sensitivity route is not calibrated: no connectome bundle (--bundle)")
        return {"reason": "no connectome bundle"}
    try:
        rule = thresholds.normalize_rule(args.rule)
    except ValueError as exc:
        raise jobs.JobError(f"Invalid --rule: {exc}", code="usage", exit_code=jobs.EXIT_USAGE) from None
    bundle = connectome.ConnectomeBundle.open(path)
    try:
        participants = os.path.abspath(args.participants)
        lesions = nulls.load_cohort(maps_tsv, bundle, participants=participants, emit=emit)
        if len(lesions) < 2:
            return {"reason": "fewer than 2 lesions with weights"}
        pool = nulls.build_relocation_pool(lesions.columns, bundle, subjects=list(lesions.subject_ids) if lesions.subject_ids else None, k_pool=args.k_pool, rule=rule,
                                           sign=args.sign, seed=args.seed, emit=emit, cancel=cancel)
        counts = relocated_counts(pool, args.k, args.seed)
        out = sensitivity_fpr(counts, len(lesions), args.overlap, args.sign, args.alpha)
        out.update({"rule": rule, "k_pool": args.k_pool, "mode": "exact_pooled", "label": f"pooled exact null ({args.k_pool} per lesion)", "caption": nulls.CAPTION})
        return out
    finally:
        if hasattr(bundle, "close"):
            bundle.close()


if __name__ == "__main__":
    jobs.run_job(main, job="calibration")

"""Symptom lesion network mapping (sLNM 2.0): permutation inference, LODO, convergence and control-score maps.

numpy and scipy only; no Slicer imports. SPEC_LNM_ADDENDUM §3.5 (Siddiqi 2021, Petersen 2026, Treeratana 2026):

- :func:`slnm_design`: the GLM design (design.py, test "continuous") with the score as the effect of
  interest and covariates (lesion volume on by default). ``method="spearman"`` rank-transforms the
  score and the numeric covariates over the subjects used (and the maps per voxel, :func:`rank_maps`,
  in voxel chunks), so that the partial r is a partial Spearman correlation; ``flip`` multiplies the
  score by -1 (TMS cohorts) and is recorded.
- :func:`run`: fit and Freedman-Lane permutation through glm.py (within-block score permutation when the
  design says so; max-stat FWE p and BH-FDR q). The effect size is the partial r = t / sqrt(t² + df),
  which equals the correlation of the score and the map after both are residualised on the covariates
  (:func:`partial_r_map`). The parametric p of ``stats.symptom_correlation`` is never computed here.
- :func:`lodo`: per-dataset partial-r maps for the datasets with at least :data:`MIN_LODO_SUBJECTS`
  subjects (the others are skipped with a warning); their (n_d - 3)-weighted Fisher-z combination
  (:func:`fisher_combine`); the cross-dataset spatial-r matrix and the leave-one-dataset-out r of each
  dataset with the combination of the others; the p of the mean off-diagonal r (and of each LODO r)
  from block permutations: the scores are shuffled within each dataset and every per-dataset map is
  recomputed in batches, (B x n_d) @ (n_d x V) (:class:`ScoreNull`). Convergence (descriptive):
  Stouffer z of z_d = atanh(r_d) sqrt(n_d - 3 - k_d) (:func:`stouffer`) and the I² heterogeneity map of
  Cochran's Q on the Fisher z (:func:`i_squared`), k_d being the covariates of dataset d.
- :func:`compare`: control-score maps (the same pipeline per control column) compared with the main map:
  the spatial r, with a raw-input permutation p (the main scores are shuffled, within blocks when the
  design uses blocks, and the main map is recomputed), and the partial spatial r given the reference
  maps [mu_r, pc1] (``similarity.partial_spatial_correlation``, reference spec ``lnm-reference-v1``)
  with its own permutation p from the same shuffles. Reported as, for example,
  "r = 0.56 → partial(PC1, μC) r = 0.30, p_perm = 0.11" (:func:`comparison_text`).

Permutation p-values are (1 + #{null >= observed}) / (K + 1) over the K non-identity permutations, with
the tie tolerance of the float32 null maps; spatial comparisons are two-sided (|r|) and the convergence
tests (mean off-diagonal r, LODO r) one-sided (greater).

Command line (JSON events, see ``jobs``; exit codes 0 done, 1 error, 2 usage or blocked design, 3
cancelled, 75 locked)::

    python -m LesionNetworkMappingLib.slnm --maps maps.tsv --participants participants.tsv --score COL
        [--covariates a,b] [--block COL] [--spearman] [--flip] [--perm 5000] [--lodo]
        [--control-scores c1,c2] [--bundle B] --mask M --out DIR [--cancel-file F] [--resume]
        [--seed S] [--compare-perm 1000] [--input Fz|T] [--volume-off-reason TEXT] [--batch B]
        [--conn-label L] [--threads K] [--run-root DIR] [--parent-run ID]

``--covariates`` replaces the default (volume_ml); ``--covariates ""`` with ``--volume-off-reason`` turns
volume off. ``--bundle`` (default: the bundle path recorded in cohortmaps.json next to maps.tsv) gives
the reference maps of the connectome and subject list of the maps (never built here); without them
the partial comparison is reported as not computed. Outputs (exact mask header)::

    group_conn-<label>_desc-slnm<Score>{R, PFwe, PFdr}_map.nii.gz       main map (and per control column)
    circuit-slnm<Score>_desc-R_map.nii.gz                              the unthresholded r map as a circuit
    group_conn-<label>_desc-slnm<Score>{Ds<Dataset>R, LodoR, StoufferZ, ISq}_map.nii.gz   with --lodo
    slnm_lodo.tsv (with --lodo), slnm.json, design.json, design_matrix.tsv

The run is registered (kind "slnm") with the cohortmaps run of maps.tsv as parent. ``--resume``
continues a stopped run: finished units (main, each control, LODO, comparison) are kept and the
permutations of an unfinished GLM unit continue from their last batch.
"""

import os
import sys

if __name__ == "__main__":
    from LesionNetworkMappingLib.jobs import set_blas_threads

    set_blas_threads(sys.argv[1:])  # before numpy is imported: BLAS reads these variables once

import dataclasses
import math
import re
import time

import numpy as np

from . import design as designlib
from . import glm, jobs, nifti, runs, schema, similarity
from .export import DISCLAIMER, LIBRARY_VERSION, connectome_label, file_sha256, group_map_path, now_iso, versions
from .geometry import mask_indices

FORMAT_NAME = "slicer-lnm-slnm"
FORMAT_VERSION = 1
SLNM_JSON = "slnm.json"
LODO_TSV = "slnm_lodo.tsv"
RESUME_NAME = ".slnm-resume.json"
MAPS_MEMMAP = ".slnm-maps.npy"
LOCK_NAME = ".slnm.lock"
METHODS = ("pearson", "spearman")
#: Datasets need at least this many subjects to enter LODO (addendum §3.5)
MIN_LODO_SUBJECTS = 10
MIN_LODO_DATASETS = 2
#: Permutations (identity included) of the LODO block permutation and the control comparisons
DEFAULT_COMPARE_PERM = 1000
#: Reference maps partialled out of the control comparisons (addendum §3.7: [μC_r, PC1])
REFERENCE_COVARIATES = ("mu_r", "pc1")
#: r is clipped before the Fisher transform (as engine.RMAX)
RMAX = 0.999999
CHUNK_VOXELS = 32768
#: Bytes of the null maps of one batch (the batch size is chosen to fit, between 1 and MAX_BATCH)
BATCH_BYTES = 256 * 1024**2
MAX_BATCH = 64
#: Tie tolerance of a float32 null statistic against the float64 observed one ("null >= observed" counts)
TIE_RTOL = 1e-5
TIE_ATOL = 1e-6
#: A voxel whose residualised sum of squares is below this fraction of its raw sum of squares is constant
#: after removing the covariates: its partial r is 0
DEGENERATE_RTOL = 1e-20
CONVERGENCE_LABEL = "descriptive (Fisher-z approximations; not a permutation test)"
PARAMETRIC_NOTE = "The parametric p of a voxelwise correlation (stats.symptom_correlation) is not computed: inference is by permutation (Freedman-Lane, max-stat FWE; BH-FDR secondary)."


# Design


@dataclasses.dataclass
class SlnmDesign:
    """An sLNM design (:func:`slnm_design`).

    - ``design``: the ``design.Design`` the GLM runs (rows in ``design.sids`` order); its X holds the
      tested regressor as fitted (ranked for Spearman, times -1 for ``flip``) in column 1 and the
      covariates (ranked when numeric, for Spearman) after it;
    - ``raw_X``: the design matrix before ranking and flipping (column 1: the centred, transformed score);
    - ``ranked_columns``: the indices of the X columns that Spearman ranks (the score and numeric covariates).
    """

    design: object
    score: str
    method: str
    flip: bool
    raw_X: np.ndarray
    ranked_columns: list

    @property
    def n(self):
        return self.design.n

    @property
    def sids(self):
        return self.design.sids

    @property
    def spearman(self):
        return self.method == "spearman"

    @property
    def x(self):
        """The tested regressor as fitted (n,)."""
        return self.design.X[:, 1]

    @property
    def covariates(self):
        """The covariate columns as fitted (n x q, without the intercept)."""
        return self.design.X[:, 2:]


def _centred_ranks(values):
    ranked = similarity.ranks(np.asarray(values, dtype=np.float64))
    return ranked - ranked.mean()


def _numeric_columns(columns):
    """Indices of the covariate columns of a continuous design that hold numbers (not category dummies)."""
    return [index for index, name in enumerate(columns) if index >= 2 and "[" not in name]


def adjust_design(design, method="pearson", flip=False):
    """An :class:`SlnmDesign` of a continuous ``design.Design`` (modified in place): Spearman ranks and flip.

    :param method: "pearson" or "spearman" (the score and the numeric covariates are replaced by their
      centred ranks over the subjects of the design; ties share their average rank).
    :param flip: multiply the score by -1.
    """
    if method not in METHODS:
        raise ValueError(f"Unknown method {method!r}; expected one of {', '.join(METHODS)}")
    if design.spec.test != "continuous":
        raise ValueError(f"An sLNM design tests a continuous score, not {design.spec.test!r}")
    raw_X = np.array(design.X, dtype=np.float64)
    X = raw_X.copy()
    ranked = []
    if method == "spearman" and np.all(np.isfinite(X)):
        ranked = [1, *_numeric_columns(design.columns)]
        for index in ranked:
            X[:, index] = _centred_ranks(X[:, index])
    if flip:
        X[:, 1] = -X[:, 1]
    design.X = X
    # Ranks and the sign keep which rows are equal, so the unique permutations are unchanged
    design.row_labels = designlib._row_codes(X)
    # Kept in design.json (an unknown key for design.py), so that the saved matrix of ranks is self-describing
    design.spec.extra = {**design.spec.extra, "slnm": {"method": method, "flip": bool(flip), "ranked_columns": [design.columns[index] for index in ranked]}}
    return SlnmDesign(design=design, score=design.spec.score_column, method=method, flip=bool(flip), raw_X=raw_X, ranked_columns=ranked)


def slnm_design(
    study,
    score,
    covariates=None,
    block_column=None,
    method="pearson",
    flip=False,
    n_perm=designlib.DEFAULT_N_PERM,
    seed=0,
    permute="auto",
    transforms=None,
    volume_off_reason=None,
    levels=None,
    group_column="group",
    input="Fz",  # noqa: A002 (the design.json key)
    contrast="two_sided",
):
    """The sLNM design of a study: design.build_design with test "continuous" and the score as the effect of interest.

    :param score: score column (the regressor whose partial r is mapped).
    :param covariates: nuisance columns (default: lesion volume). Turning volume off needs ``volume_off_reason``
      (otherwise the design warns).
    :param block_column: exchangeability blocks (default: dataset, else source_type); scores are permuted within
      blocks when every block can be permuted (design.py decides, ``permute="auto"``).
    :param method: "pearson" or "spearman"; ``flip``: multiply the score by -1 (recorded).
    :param n_perm: permutations (identity included); below the design minimum (1000) the spec keeps the
      minimum and the caller overrides the count (as the CLI does for previews).
    :param levels: optional levels of ``group_column`` that select the subjects (default: everyone with a score, D-A24).
    :return: :class:`SlnmDesign`.
    :raises design.DesignError: unknown columns, no subjects left.
    """
    spec = designlib.DesignSpec(
        test="continuous",
        group_column=group_column,
        levels=levels,
        contrast=contrast,
        score_column=score,
        covariates=[designlib.VOLUME_COLUMN] if covariates is None else list(covariates),
        transforms=dict(transforms or {}),
        block_column=block_column,
        permute=permute,
        input=input,
        inference={"n_perm": max(int(n_perm), designlib.MIN_N_PERM), "seed": int(seed)},
        volume_off_reason=volume_off_reason,
    )
    return adjust_design(designlib.build_design(study, spec), method, flip)


# Maps


def rank_maps(Y, out=None, chunk=CHUNK_VOXELS, rows=None):
    """Ranks of every voxel across the subjects (rows), ties averaged, in voxel chunks (float32).

    :param Y: n x V maps (array or memmap).
    :param out: n x V float32 array or memmap to fill (default: a new array).
    :param rows: optional row indices of Y to rank among themselves.
    """
    from scipy import stats as scipy_stats

    n_rows = Y.shape[0] if rows is None else len(rows)
    n_voxels = Y.shape[1]
    if out is None:
        out = np.empty((n_rows, n_voxels), dtype=np.float32)
    for start in range(0, n_voxels, chunk):
        part = slice(start, min(start + chunk, n_voxels))
        block = np.asarray(Y[:, part] if rows is None else Y[rows, part], dtype=np.float64)
        out[:, part] = scipy_stats.rankdata(block, axis=0)
    return out


def _basis(Z, n):
    """Orthonormal basis (n x rank) of the span of [1, Z] (Z: n x q nuisance columns, or None)."""
    columns = np.ones((n, 1)) if Z is None or np.size(Z) == 0 else np.column_stack([np.ones(n), np.asarray(Z, dtype=np.float64).reshape(n, -1)])
    U, s, _ = np.linalg.svd(columns, full_matrices=False)
    rank = int(np.sum(s > 1e-10 * max(columns.shape) * s[0]))
    return U[:, :rank]


def partial_r_map(x, Y, Z=None, chunk=CHUNK_VOXELS):
    """Partial correlation of a score with every voxel given covariates, by residualisation.

    Both the score and the maps are residualised on [1, Z] (least squares), then correlated:
    r = (R x) . (R y) / (|R x| |R y|), which equals t / sqrt(t² + df) of the GLM [1, x, Z].

    :param x: (n,) score. :param Y: n x V maps. :param Z: n x q covariates (or None).
    :return: (V,) float64 (0 where a map or the score is constant after residualisation).
    """
    x = np.asarray(x, dtype=np.float64).reshape(-1)
    n = x.size
    Q = _basis(Z, n)
    xr = x - Q @ (Q.T @ x)
    x_norm = float(np.sqrt(xr @ xr))
    n_voxels = Y.shape[1]
    out = np.zeros(n_voxels)
    if x_norm <= 1e-12 * max(float(np.sqrt(x @ x)), 1e-300):
        return out
    xr /= x_norm
    for start in range(0, n_voxels, chunk):
        part = slice(start, min(start + chunk, n_voxels))
        y = np.asarray(Y[:, part], dtype=np.float64)
        yr = y - Q @ (Q.T @ y)
        ss = np.einsum("nv,nv->v", yr, yr)
        raw = np.einsum("nv,nv->v", y, y)
        valid = ss > DEGENERATE_RTOL * raw
        np.divide(xr @ yr, np.sqrt(ss), out=out[part], where=valid)
    return out


class ScoreNull:
    """Partial-r maps of a score and its permutations against fixed maps (raw-input permutation).

    The maps are residualised on [1, Z] and scaled to unit norm per voxel once (float32, n x V); each
    permuted score x[P] is residualised and scaled, and a batch of B permutations is one GEMM
    (B x n) @ (n x V). ``observed`` is the float64 partial-r map of the unpermuted score.

    :param x: (n,) score as fitted. :param Y: maps (array or memmap with at least the rows used).
    :param Z: n x q covariates (or None). :param rows: row indices of Y (default: all rows).
    :param rank: rank the maps per voxel among these rows first (Spearman within a dataset).
    """

    def __init__(self, x, Y, Z=None, rows=None, rank=False, chunk=CHUNK_VOXELS):
        from scipy import stats as scipy_stats

        self.x = np.asarray(x, dtype=np.float64).reshape(-1)
        n = self.x.size
        self.n = n
        rows = None if rows is None else np.asarray(rows, dtype=np.intp)
        if (Y.shape[0] if rows is None else rows.size) != n:
            raise ValueError(f"Expected {n} maps, got {Y.shape[0] if rows is None else rows.size}")
        self.Q = _basis(Z, n)
        self.k = self.Q.shape[1] - 1  # covariate dimensions besides the intercept
        self.df = n - self.Q.shape[1] - 1
        n_voxels = Y.shape[1]
        self.n_voxels = n_voxels
        self.Yn = np.zeros((n, n_voxels), dtype=np.float32)
        self.observed = np.zeros(n_voxels)
        ux = self._unit(self.x[np.newaxis])[0]
        self.score_degenerate = not np.any(ux)
        for start in range(0, n_voxels, chunk):
            part = slice(start, min(start + chunk, n_voxels))
            y = np.asarray(Y[:, part] if rows is None else Y[rows, part], dtype=np.float64)
            if rank:
                y = scipy_stats.rankdata(y, axis=0)
            yr = y - self.Q @ (self.Q.T @ y)
            ss = np.einsum("nv,nv->v", yr, yr)
            raw = np.einsum("nv,nv->v", y, y)
            valid = ss > DEGENERATE_RTOL * raw
            norm = np.sqrt(np.where(valid, ss, 1.0))
            yn = np.where(valid, yr / norm, 0.0)
            self.Yn[:, part] = yn
            self.observed[part] = ux @ yn

    def _unit(self, X):
        """Rows of X residualised on [1, Z] and scaled to unit norm (zero rows stay zero)."""
        R = X - (X @ self.Q) @ self.Q.T
        norms = np.sqrt(np.einsum("bn,bn->b", R, R))
        raw = np.sqrt(np.einsum("bn,bn->b", X, X))
        valid = norms > 1e-12 * np.maximum(raw, 1e-300)
        return np.where(valid[:, np.newaxis], R / np.where(valid, norms, 1.0)[:, np.newaxis], 0.0)

    def maps(self, perms):
        """Partial-r maps (B x V float32) of the permuted scores x[P] for a batch of index arrays P."""
        perms = np.asarray(perms, dtype=np.intp).reshape(-1, self.n)
        U = self._unit(self.x[perms]).astype(np.float32)
        return U @ self.Yn


def fisher_z(r):
    """atanh of r clipped to ±RMAX (float64)."""
    return np.arctanh(np.clip(np.asarray(r, dtype=np.float64), -RMAX, RMAX))


def fisher_combine(r_maps, n):
    """The (n_d - 3)-weighted mean of the Fisher z of per-dataset r maps, back-transformed to r.

    :param r_maps: D x V. :param n: (D,) subjects per dataset.
    """
    weights = np.asarray(n, dtype=np.float64) - 3.0
    return np.tanh(np.tensordot(weights, fisher_z(r_maps), axes=1) / weights.sum())


def stouffer(r_maps, n, k):
    """Stouffer z = sum_d z_d / sqrt(D), with z_d = atanh(r_d) sqrt(n_d - 3 - k_d) (descriptive)."""
    scale = np.sqrt(np.asarray(n, dtype=np.float64) - 3.0 - np.asarray(k, dtype=np.float64))
    z = fisher_z(r_maps) * scale[:, np.newaxis]
    return z.sum(axis=0) / math.sqrt(len(scale))


def i_squared(r_maps, n, k):
    """(I², Q): Cochran's Q of the Fisher z with weights w_d = n_d - 3 - k_d (inverse variances) and
    I² = max(0, (Q - (D - 1)) / Q) (0 where Q = 0), per voxel (descriptive heterogeneity).
    """
    weights = np.asarray(n, dtype=np.float64) - 3.0 - np.asarray(k, dtype=np.float64)
    z = fisher_z(r_maps)
    mean = np.tensordot(weights, z, axes=1) / weights.sum()
    q = np.tensordot(weights, (z - mean) ** 2, axes=1)
    out = np.zeros_like(q)
    np.divide(q - (len(weights) - 1), q, out=out, where=q > 0)
    return np.clip(out, 0.0, 1.0), q


def _standardize_rows(rows):
    """Rows centred and scaled to unit norm (zero rows for constant rows), float32."""
    rows = np.asarray(rows, dtype=np.float32)
    centred = rows - rows.mean(axis=-1, keepdims=True, dtype=np.float64).astype(np.float32)
    norms = np.sqrt(np.einsum("...v,...v->...", centred, centred, dtype=np.float64))
    scale = np.divide(1.0, norms, out=np.zeros_like(norms), where=norms > 0).astype(np.float32)
    centred *= scale[..., np.newaxis]
    return centred


def _exceed(null, observed, two_sided):
    """Permutation p (1 + #{null >= observed}) / (K + 1) with the float32 tie tolerance."""
    null = np.asarray(null, dtype=np.float64)
    if two_sided:
        null, observed = np.abs(null), abs(observed)
    threshold = observed - (TIE_RTOL * abs(observed) + TIE_ATOL)
    return (1.0 + np.count_nonzero(null >= threshold)) / (null.size + 1.0)


def default_batch(n_maps, n_voxels):
    """Permutations per batch: n_maps null maps of V float32 each per permutation stay within BATCH_BYTES."""
    return max(1, min(MAX_BATCH, int(BATCH_BYTES // max(4 * n_maps * max(n_voxels, 1), 1))))


def _check_cancel(cancel, done):
    if cancel is None:
        return
    if hasattr(cancel, "raise_if_cancelled"):
        cancel.raise_if_cancelled(done=done)
        return
    if cancel.is_set() if hasattr(cancel, "is_set") else cancel():
        raise jobs.Cancelled(done=done, reason="cancelled")


def _batches(iterator, batch):
    block = []
    for item in iterator:
        block.append(item)
        if len(block) == batch:
            yield block
            block = []
    if block:
        yield block


def null_permutations(n, blocks=None, mode="free", n_perm=DEFAULT_COMPARE_PERM, seed=0):
    """(non-identity index arrays, exhaustive) of a raw-input permutation: within blocks for "within", else free.

    The arrays come from ``design.permutations`` (identity dropped): every unique one once when there are
    at most n_perm, otherwise n_perm - 1 drawn at random.
    """
    blocks = None if mode == "free" else blocks
    unique = designlib.unique_count(n, blocks, None, mode=mode)
    exhaustive = unique <= min(int(n_perm), designlib.EXHAUSTIVE_CAP)
    perms = list(designlib.permutations(n, blocks, mode, int(n_perm), int(seed)))[1:]
    return perms, exhaustive


# Inference


def run(sdesign, Y, n_perm=None, seed=None, ranked=False, batch=None, dtype=None, cancel=None, on_batch=None, state=None, allow_blocked=False, engine=None, prepared=None):
    """Fit and permute an sLNM design through glm.py (Freedman-Lane; within-block score permutation when
    the design uses blocks): max-stat FWE p, BH-FDR q and the partial r (``result.effect``).

    :param Y: n x V maps in design order (raw; ranked here for Spearman unless ``ranked``).
    :return: ``glm.GlmResult``.
    """
    if sdesign.spearman and not ranked:
        Y = rank_maps(Y)
    return glm.run(sdesign.design, Y, n_perm=n_perm, seed=seed, batch=batch, dtype=dtype, cancel=cancel, on_batch=on_batch, state=state, allow_blocked=allow_blocked, engine=engine, prepared=prepared)


@dataclasses.dataclass
class LodoResult:
    """:func:`lodo` result.

    ``datasets`` (labels used), ``n``, ``k`` (covariate dimensions per dataset), ``maps`` (D x V partial r),
    ``combined`` (Fisher-z combination), ``matrix`` (D x D spatial r), ``mean_offdiag`` and its ``p_perm``,
    ``lodo_r`` / ``lodo_p`` (each dataset against the combination of the others), ``stouffer_z``,
    ``i2`` and ``q`` (descriptive convergence), ``skipped`` ({label: reason}), ``n_perm`` (identity included),
    ``exhaustive``, ``column``, ``warnings``.
    """

    column: str | None
    datasets: list
    n: list
    k: list
    maps: np.ndarray | None
    combined: np.ndarray | None
    matrix: np.ndarray | None
    mean_offdiag: float | None
    p_perm: float | None
    lodo_r: list
    lodo_p: list
    stouffer_z: np.ndarray | None
    i2: np.ndarray | None
    q: np.ndarray | None
    skipped: dict
    n_perm: int
    exhaustive: bool
    warnings: list

    @property
    def ok(self):
        return self.maps is not None

    def summary(self):
        """JSON-compatible numbers of the LODO analysis (slnm.json)."""
        out = {
            "column": self.column,
            "datasets": list(self.datasets),
            "n": [int(value) for value in self.n],
            "k": [int(value) for value in self.k],
            "weights": [int(value) - 3 for value in self.n],
            "skipped": dict(self.skipped),
            "min_subjects": MIN_LODO_SUBJECTS,
            "warnings": list(self.warnings),
            "computed": self.ok,
        }
        if not self.ok:
            return out
        out.update({
            "combination": "(n_d - 3)-weighted mean of Fisher z, back-transformed to r",
            "matrix": [[float(value) for value in row] for row in self.matrix],
            "mean_offdiag_r": float(self.mean_offdiag),
            "p_perm": float(self.p_perm),
            "lodo_r": [float(value) for value in self.lodo_r],
            "lodo_p_perm": [float(value) for value in self.lodo_p],
            "permutation": {
                "method": "block permutation: scores shuffled within each dataset, per-dataset maps recomputed",
                "n_perm": int(self.n_perm),
                "exhaustive": bool(self.exhaustive),
                "tails": 1,
                "alternative": "greater",
            },
            "convergence": {
                "label": CONVERGENCE_LABEL,
                "stouffer": "sum_d atanh(r_d) sqrt(n_d - 3 - k_d) / sqrt(D)",
                "i2": "Cochran's Q on Fisher z with weights n_d - 3 - k_d; I² = max(0, (Q - (D - 1)) / Q)",
                "stouffer_z_range": [float(np.min(self.stouffer_z)), float(np.max(self.stouffer_z))],
                "i2_median": float(np.median(self.i2)),
                "i2_fraction_above_0_5": float(np.mean(self.i2 > 0.5)),
            },
        })
        return out


def lodo(sdesign, Y, labels=None, column=None, n_perm=DEFAULT_COMPARE_PERM, seed=1, min_subjects=MIN_LODO_SUBJECTS, batch=None, cancel=None, on_batch=None):
    """Leave-one-dataset-out sLNM (see the module docstring).

    :param Y: n x V raw maps in design order (Spearman ranks within each dataset are computed here).
    :param labels: per-row dataset labels (default: the design's block labels, e.g. the dataset column).
    :param n_perm: block permutations, identity included (exhaustive when the unique ones are fewer).
    :param on_batch: ``on_batch(done, total)`` after each batch.
    :return: :class:`LodoResult` (``maps`` None when fewer than 2 datasets qualify; see ``warnings``).
    """
    design = sdesign.design
    if labels is None:
        column, labels = design.block_column, design.block_labels
    warnings, skipped = [], {}

    def empty(message):
        warnings.append(message)
        return LodoResult(column, [], [], [], None, None, None, None, None, [], [], None, None, None, skipped, 0, False, warnings)

    if labels is None:
        return empty("LODO needs a dataset column (dataset or the block column): it was skipped")
    labels = [str(label) for label in labels]
    order = list(dict.fromkeys(sorted(labels)))
    rows_of = {label: np.flatnonzero(np.array(labels) == label) for label in order}
    raw_score = sdesign.raw_X[:, 1]
    raw_cov = sdesign.raw_X[:, 2:]
    nulls, used = [], []
    for label in order:
        rows = rows_of[label]
        if rows.size < min_subjects:
            skipped[label] = f"{rows.size} subjects (fewer than {min_subjects})"
            continue
        x = raw_score[rows].copy()
        Z = raw_cov[rows].copy()
        if sdesign.spearman:
            x = _centred_ranks(x)
            for index in sdesign.ranked_columns:
                if index >= 2:
                    Z[:, index - 2] = _centred_ranks(Z[:, index - 2])
        if sdesign.flip:
            x = -x
        null = ScoreNull(x, Y, Z, rows=rows, rank=sdesign.spearman)
        if null.score_degenerate:
            skipped[label] = "the score does not vary once the covariates are removed"
            continue
        if rows.size - 3 - null.k < 1:
            skipped[label] = f"too few subjects for {null.k} covariate dimension(s)"
            continue
        nulls.append(null)
        used.append((label, rows))
    for label, reason in skipped.items():
        warnings.append(f"Dataset {label} is left out of LODO: {reason}")
    if len(used) < MIN_LODO_DATASETS:
        return empty(f"LODO needs at least {MIN_LODO_DATASETS} datasets with {min_subjects} or more subjects; {len(used)} qualify: it was skipped")

    D = len(used)
    n = [int(rows.size) for _, rows in used]
    k = [int(null.k) for null in nulls]
    weights = np.array(n, dtype=np.float64) - 3.0
    maps = np.stack([null.observed for null in nulls])
    combined = fisher_combine(maps, n)
    matrix = np.eye(D)
    for i in range(D):
        for j in range(i + 1, D):
            matrix[i, j] = matrix[j, i] = similarity.pearson(maps[i], maps[j])
    pairs = [(i, j) for i in range(D) for j in range(i + 1, D)]
    mean_offdiag = float(np.mean([matrix[i, j] for i, j in pairs]))
    z = fisher_z(maps)
    total = np.tensordot(weights, z, axes=1)
    lodo_r = [similarity.pearson(maps[d], np.tanh((total - weights[d] * z[d]) / (weights.sum() - weights[d]))) for d in range(D)]

    # Block permutation: the scores are shuffled within each dataset (rows grouped by dataset)
    sizes = np.array(n)
    starts = np.r_[0, np.cumsum(sizes)[:-1]]
    blocks = np.repeat(np.arange(1, D + 1), sizes)
    perms, exhaustive = null_permutations(int(sizes.sum()), blocks, "within", n_perm, seed)
    K = len(perms)
    batch = batch or default_batch(2 * D + 2, maps.shape[1])
    null_mean = np.empty(K)
    null_lodo = np.empty((D, K))
    done = 0
    for block in _batches(perms, batch):
        _check_cancel(cancel, done)
        block = np.asarray(block)
        B = block.shape[0]
        null_maps = np.stack([nulls[d].maps(block[:, starts[d] : starts[d] + n[d]] - starts[d]) for d in range(D)])  # D x B x V
        standard = _standardize_rows(null_maps)
        values = np.zeros(B)
        for i, j in pairs:
            values += np.einsum("bv,bv->b", standard[i], standard[j])
        null_mean[done : done + B] = values / len(pairs)
        nz = np.arctanh(np.clip(null_maps, -RMAX, RMAX, out=null_maps), out=null_maps)  # float32, in place
        null_total = np.tensordot(weights.astype(np.float32), nz, axes=1)
        for d in range(D):
            others = null_total - np.float32(weights[d]) * nz[d]
            others *= np.float32(1.0 / (weights.sum() - weights[d]))
            np.tanh(others, out=others)
            null_lodo[d, done : done + B] = np.einsum("bv,bv->b", standard[d], _standardize_rows(others))
        done += B
        if on_batch is not None:
            on_batch(done, K)
    p_perm = _exceed(null_mean, mean_offdiag, two_sided=False)
    lodo_p = [_exceed(null_lodo[d], lodo_r[d], two_sided=False) for d in range(D)]
    i2, q = i_squared(maps, n, k)
    return LodoResult(
        column=column,
        datasets=[label for label, _ in used],
        n=n,
        k=k,
        maps=maps,
        combined=combined,
        matrix=matrix,
        mean_offdiag=mean_offdiag,
        p_perm=p_perm,
        lodo_r=lodo_r,
        lodo_p=lodo_p,
        stouffer_z=stouffer(maps, n, k),
        i2=i2,
        q=q,
        skipped=skipped,
        n_perm=K + 1,
        exhaustive=exhaustive,
        warnings=warnings,
    )


def comparison_text(r, partial_r=None, p_perm=None, p_raw=None, reason=None):
    """The comparison line: "r = 0.56 → partial(PC1, μC) r = 0.30, p_perm = 0.11" (p_perm of the partial r).

    Without the reference maps: "r = 0.56, p_perm = 0.03 (partial(PC1, μC) not computed: <reason>)".
    """
    if partial_r is not None and math.isfinite(partial_r):
        return f"r = {r:.2f} → partial(PC1, μC) r = {partial_r:.2f}, p_perm = {p_perm:.2g}"
    text = f"r = {r:.2f}" + (f", p_perm = {p_raw:.2g}" if p_raw is not None else "")
    return text + f" (partial(PC1, μC) not computed: {reason or 'no reference maps'})"


def _residualize_rows(rows, basis):
    """Rows (B x V) minus their projection on an orthonormal voxel basis (V x m)."""
    return rows - (rows @ basis) @ basis.T


def compare(sdesign, Y, main_map, control_maps, covariates=None, n_perm=DEFAULT_COMPARE_PERM, seed=2, ranked=False, batch=None, cancel=None, on_batch=None, reason=None):
    """Compare control-score maps with the main sLNM map (see the module docstring).

    :param Y: n x V maps of the main design (ranked for Spearman when ``ranked``; otherwise ranked here).
    :param main_map: (V,) main partial-r map. :param control_maps: {name: (V,) partial-r map}.
    :param covariates: (k, V) reference maps (``reference.Reference.covariates(REFERENCE_COVARIATES)``) or
      None (the partial comparison is then reported as not computed, with ``reason``).
    :param n_perm: raw-input permutations, identity included: the main scores are shuffled (within blocks
      when the design uses blocks) and the main map is recomputed.
    :return: [{name, r, p_perm_r, partial_r, p_perm_partial, text, n_perm, exhaustive}] in the order of control_maps.
    """
    design = sdesign.design
    if sdesign.spearman and not ranked:
        Y = rank_maps(Y)
    main_map = np.asarray(main_map, dtype=np.float64)
    names = list(control_maps)
    controls = np.stack([np.asarray(control_maps[name], dtype=np.float64) for name in names]) if names else np.zeros((0, main_map.size))
    valid = np.isfinite(main_map) & np.all(np.isfinite(controls), axis=0)
    covs = None
    if covariates is not None:
        covs = np.atleast_2d(np.asarray(covariates, dtype=np.float64))
        valid &= np.all(np.isfinite(covs), axis=0)
    observed_r = [similarity.pearson(main_map[valid], controls[c, valid]) for c in range(len(names))]
    observed_partial = [similarity.partial_spatial_correlation(main_map[valid], controls[c, valid], covs[:, valid]) for c in range(len(names))] if covs is not None else [None] * len(names)
    basis = None
    residual_controls = None
    if covs is not None:
        basis = _basis(covs[:, valid].T, int(np.count_nonzero(valid)))
        residual_controls = np.stack([similarity.residual_map(controls[c, valid], covs[:, valid]) for c in range(len(names))]) if names else None
    mode = design.permute if design.blocks is not None else "free"
    perms, exhaustive = null_permutations(design.n, design.blocks, mode, n_perm, seed)
    null = ScoreNull(sdesign.x, Y, sdesign.covariates)
    K = len(perms)
    null_r = np.empty((len(names), K))
    null_partial = np.empty((len(names), K))
    batch = batch or default_batch(3, main_map.size)
    done = 0
    for block in _batches(perms, batch):
        _check_cancel(cancel, done)
        maps = null.maps(block)[:, valid].astype(np.float64)
        B = maps.shape[0]
        residuals = _residualize_rows(maps, basis) if basis is not None else None
        for c in range(len(names)):
            null_r[c, done : done + B] = similarity.pearson_rows(maps, controls[c, valid])
            if residuals is not None:
                null_partial[c, done : done + B] = similarity.pearson_rows(residuals, residual_controls[c])
        done += B
        if on_batch is not None:
            on_batch(done, K)
    out = []
    for c, name in enumerate(names):
        r = float(observed_r[c])
        p_r = _exceed(null_r[c], r, two_sided=True)
        partial = observed_partial[c]
        p_partial = _exceed(null_partial[c], partial, two_sided=True) if partial is not None and math.isfinite(partial) else None
        out.append({
            "name": name,
            "r": r,
            "p_perm_r": p_r,
            "partial_r": None if partial is None or not math.isfinite(partial) else float(partial),
            "p_perm_partial": p_partial,
            "text": comparison_text(r, partial, p_partial, p_r, reason),
            "n_perm": K + 1,
            "exhaustive": bool(exhaustive),
            "tails": 2,
        })
    return out


# Files


def score_label(column):
    """The "desc" stem of a score's outputs: "slnm" + its letters and digits in camel case (slnmNihss)."""
    camel = glm._camel(column)
    return "slnm" + (glm._upper_first(re.sub(r"[^A-Za-z0-9]", "", camel)) or "Score")


def _unique_labels(names, make):
    out, seen = [], set()
    for index, name in enumerate(names):
        label = make(name) or f"X{index + 1}"
        base, suffix = label, 2
        while label in seen:
            label, suffix = f"{base}{suffix}", suffix + 1
        seen.add(label)
        out.append(label)
    return out


def _write_vector(path, vector, mask_path, descrip="", fill=0.0, intent=(0, (0.0, 0.0, 0.0))):
    data, _, header = nifti.read_nifti(mask_path)
    shape = tuple(header["shape"][:3])
    indices = mask_indices(np.asarray(data).reshape(shape) != 0)
    return glm._write_on_mask(path, vector, nifti.read_raw_header(mask_path), indices, shape, fill, descrip, intent[0], intent[1])


def write_lodo_tsv(path, result):
    """slnm_lodo.tsv: one row per dataset (used or skipped) with n, k, weight, status, LODO r and p, the
    mean r with the other datasets and one r_<dataset> column per used dataset (the spatial-r matrix).
    """
    used = list(result.datasets)
    header = ["dataset", "n", "k", "weight", "status", "lodo_r", "lodo_p_perm", "mean_r_others"] + [f"r_{label}" for label in used]
    lines = ["\t".join(header)]
    for d, label in enumerate(used):
        others = [result.matrix[d, e] for e in range(len(used)) if e != d]
        values = [label, str(result.n[d]), str(result.k[d]), str(result.n[d] - 3), "used", f"{result.lodo_r[d]:.6g}", f"{result.lodo_p[d]:.6g}", f"{np.mean(others):.6g}"]
        values += [f"{result.matrix[d, e]:.6g}" for e in range(len(used))]
        lines.append("\t".join(values))
    for label, reason in result.skipped.items():
        lines.append("\t".join([label, "n/a", "n/a", "n/a", f"skipped: {reason}", "n/a", "n/a", "n/a"] + ["n/a"] * len(used)))
    schema.write_text_atomic(path, "\n".join(lines) + "\n")
    return path


# Command line


def _parser():
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.slnm", description="Symptom lesion network mapping: partial r with permutation inference, LODO, convergence and control-score maps.")
    parser.add_argument("--maps", required=True, help="maps.tsv of a cohortmaps run (participant_id, AvgFz, T)")
    parser.add_argument("--participants", required=True, help="participants.tsv (of a study, or a table with participants.json)")
    parser.add_argument("--score", required=True, help="score column (the regressor whose partial r is mapped)")
    parser.add_argument("--covariates", default=None, help="comma-separated covariate columns (default: volume_ml); an empty value turns volume off (give --volume-off-reason)")
    parser.add_argument("--volume-off-reason", default=None, help="why lesion volume is not a covariate (recorded in provenance and methods)")
    parser.add_argument("--block", default=None, help="exchangeability block / dataset column (default: dataset, else source_type)")
    parser.add_argument("--spearman", action="store_true", help="rank-transform the scores, numeric covariates and maps (partial Spearman)")
    parser.add_argument("--flip", action="store_true", help="multiply the score by -1 (TMS cohorts)")
    parser.add_argument("--perm", type=jobs.positive_int, default=designlib.DEFAULT_N_PERM, help="GLM permutations, identity included (default 5000; below 1000 is a preview)")
    parser.add_argument("--seed", type=int, default=0, help="permutation seed (default 0)")
    parser.add_argument("--lodo", action="store_true", help="leave-one-dataset-out maps, cross-dataset similarity and convergence (Stouffer, I²)")
    parser.add_argument("--control-scores", default=None, help="comma-separated control score columns mapped with the same pipeline and compared with the main map")
    parser.add_argument("--compare-perm", type=jobs.positive_int, default=DEFAULT_COMPARE_PERM, help="raw-input permutations of the LODO and control comparisons, identity included (default 1000)")
    parser.add_argument("--bundle", default=None, help="connectome bundle of the reference maps (default: the bundle recorded in cohortmaps.json)")
    parser.add_argument("--input", choices=designlib.INPUTS, default="Fz", help="per-lesion maps: Fz (AvgFz, default) or T")
    parser.add_argument("--mask", required=True, help="connectome mask (the maps' grid; outputs get its exact header)")
    parser.add_argument("--out", required=True, help="output folder")
    parser.add_argument("--batch", type=jobs.positive_int, default=None, help="GLM permutations per GEMM (default: chosen from memory)")
    parser.add_argument("--conn-label", default=None, help="connectome name of the output names (default: from cohortmaps.json next to maps.tsv)")
    jobs.add_common_arguments(parser)
    return parser


def _columns(text):
    if text is None:
        return None
    return [column.strip() for column in text.split(",") if column.strip()]


def main(argv=None):
    """Command-line entry point (see the module docstring); returns the exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(argv)
    if args.seed < 0:
        raise jobs.JobError(f"--seed must not be negative, got {args.seed}", code="usage", exit_code=jobs.EXIT_USAGE)
    controls = _columns(args.control_scores) or []
    if len(set(controls)) != len(controls):
        raise jobs.JobError(f"--control-scores lists a column twice: {args.control_scores}", code="usage", exit_code=jobs.EXIT_USAGE)
    covariates = _columns(args.covariates)
    for column in controls:
        if column == args.score or column in (covariates if covariates is not None else [designlib.VOLUME_COLUMN]):
            raise jobs.JobError(f"The control score {column} is the main score or a covariate", code="usage", exit_code=jobs.EXIT_USAGE)
    return _run_cli(args, argv, controls, covariates)


def _build(study, column, args, covariates, flip):
    try:
        return slnm_design(
            study,
            column,
            covariates=covariates,
            block_column=args.block,
            method="spearman" if args.spearman else "pearson",
            flip=flip,
            n_perm=args.perm,
            seed=args.seed,
            volume_off_reason=args.volume_off_reason,
            input=args.input,
        )
    except designlib.DesignError as exc:
        raise jobs.JobError(f"The design of {column} cannot be built: {exc}", code="design_blocked", exit_code=jobs.EXIT_USAGE) from None


def _prepare_permutations(sdesign, n_perm, what):
    design = sdesign.design
    count, exhaustive = glm.shuffling_count(design, n_perm)
    if n_perm != design.spec.n_perm:
        glm._override_permutations(design, n_perm, count, exhaustive)
    if design.blocking:
        messages = [check.message for check in design.blocking]
        raise jobs.JobError(f"The design of {what} is blocked: " + " | ".join(messages), code="design_blocked", exit_code=jobs.EXIT_USAGE, checks=[check.to_dict() for check in design.blocking])
    return count, exhaustive


def _reference_for(args, cohort, subjects_sha, indices, mask_shape):
    """(Reference or None, reason, info dict) for the partial comparison."""
    from . import connectome, reference

    path = args.bundle or ((cohort or {}).get("bundle") or {}).get("path")
    if not path:
        return None, "no connectome bundle given (--bundle)", None
    try:
        bundle = connectome.ConnectomeBundle.open(path)
    except (OSError, ValueError) as exc:
        return None, f"the bundle cannot be opened ({exc})", None
    try:
        if tuple(bundle.shape) != tuple(mask_shape) or not np.array_equal(bundle.mask_indices, indices):
            return None, "the mask is not the bundle's mask", None
        ref = reference.load_reference(bundle, subjects_sha=subjects_sha)
    finally:
        if hasattr(bundle, "close"):
            bundle.close()
    if ref is None:
        return None, "no valid reference maps for this connectome and subject list (python -m LesionNetworkMappingLib.reference build)", None
    info = {"definition": ref.definition, "path": ref.path, "sha256": ref.sha256, "subjects_sha256": ref.subjects_sha256, "covariates": list(REFERENCE_COVARIATES)}
    return ref, None, info


def _remove_previous_outputs(out_dir):
    previous = glm._read_json(os.path.join(out_dir, SLNM_JSON))
    if previous:
        for name in list((previous.get("outputs") or {}).values()):
            path = os.path.join(out_dir, os.path.basename(str(name)))
            if os.path.isfile(path):
                os.remove(path)
    glm._remove(os.path.join(out_dir, SLNM_JSON))


def _unit_file(out_dir, unit, kind):
    return os.path.join(out_dir, f".slnm-{re.sub(r'[^A-Za-z0-9]', '_', unit)}-{kind}.npz")


def _save_arrays(path, **arrays):
    tmp_path = f"{path}.{os.getpid()}.tmp.npz"
    try:
        np.savez(tmp_path, **arrays)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def _load_arrays(path):
    try:
        with np.load(path) as data:
            return {key: np.array(data[key]) for key in data.files}
    except (OSError, ValueError, KeyError, EOFError):
        return None


def _run_cli(args, argv, controls, covariates):
    emit = jobs.emit
    cancel = jobs.CancelToken(args.cancel_file)
    started = time.monotonic()
    timings = {}
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    lock = jobs.OutputLock(os.path.join(out_dir, LOCK_NAME))
    lock.acquire()
    if lock.reclaimed is not None:
        emit("warning", code="stale_lock", message=f"A stale lock of process {lock.reclaimed.get('pid')} on {out_dir} was reclaimed")
    run_root, run_id, record = None, None, None
    temporaries = []
    try:
        # Inputs and designs
        study, in_study = glm._load_study(args.participants)
        for column in [args.score, *controls]:
            if column not in study.columns:
                raise jobs.JobError(f"{args.participants} has no column {column}", code="usage", exit_code=jobs.EXIT_USAGE)
        column = glm.MAP_COLUMNS[args.input]
        maps_tsv = os.path.abspath(args.maps)
        mapped = glm._map_rows(maps_tsv, column)
        cohort_json = os.path.join(os.path.dirname(maps_tsv), "cohortmaps.json")
        cohort = glm.cohortmaps_record(cohort_json)
        cohort_run = (cohort or {}).get("run_id")
        bundle_info = (cohort or {}).get("bundle") or {}
        label = args.conn_label or bundle_info.get("name") or os.path.basename(os.path.dirname(os.path.abspath(args.mask))) or "connectome"
        with_maps = {sid for sid in study.subject_ids if sid in mapped}
        no_map = [sid for sid in study.subject_ids if sid not in mapped]
        if not with_maps:
            raise jobs.JobError(f"No subject of {args.participants} has a map in {maps_tsv}", code="design_blocked", exit_code=jobs.EXIT_USAGE)
        subset, filled = glm._subset_study(study, with_maps, {sid: values[1] for sid, values in mapped.items()})
        main = _build(subset, args.score, args, covariates, args.flip)
        design = main.design
        pooling = glm._pooling_checks(design, mapped, cohort)
        if pooling:
            design.checks = designlib._ordered([*pooling, *design.checks])
        if no_map:
            design.excluded[glm.EXCLUDED_NO_MAP] = list(no_map)
            design.checks = designlib._ordered([*design.checks, designlib.Check("info", "no_map", f"{len(no_map)} subject(s) have no map in maps.tsv and are excluded", {"sids": no_map})])
        count, exhaustive = _prepare_permutations(main, args.perm, args.score)
        # Control scores: the same pipeline on the subjects of the main design (listwise for the control)
        control_study, _ = glm._subset_study(subset, set(design.sids), {})
        control_designs = {name: _build(control_study, name, args, covariates, False) for name in controls}
        control_counts = {name: _prepare_permutations(sdesign, args.perm, name) for name, sdesign in control_designs.items()}
        for check in design.warnings:
            emit("warning", code=check.code, message=check.message)
        for name, sdesign in control_designs.items():
            for check in sdesign.design.warnings:
                emit("warning", code=check.code, message=f"{name}: {check.message}")

        # Mask, reference and run registration
        mask_data, mask_affine, mask_header = nifti.read_nifti(args.mask)
        mask_shape = tuple(mask_header["shape"][:3])
        indices = mask_indices(np.asarray(mask_data).reshape(mask_shape) != 0)
        n_voxels = int(indices.size)
        mask_sha = file_sha256(args.mask)
        subjects_sha = mapped[design.sids[0]][2][1]
        ref, ref_reason, ref_info = (None, None, None)
        if controls:
            ref, ref_reason, ref_info = _reference_for(args, cohort, subjects_sha if re.fullmatch(r"[0-9a-f]{64}", str(subjects_sha)) else None, indices, mask_shape)
            if ref is None:
                emit("warning", code="no_reference", message=f"The partial comparison with [μC_r, PC1] is not computed: {ref_reason}")
        map_paths = [mapped[sid][0] for sid in design.sids]
        maps_key = []
        for sid, path in zip(design.sids, map_paths, strict=True):
            try:
                info = os.stat(path)
            except OSError:
                raise jobs.JobError(f"The map of {sid} is missing: {path}", code="map_missing") from None
            maps_key.append([sid, path, info.st_size, info.st_mtime_ns])
        method = main.method
        params = {
            "design": runs.params_sha256(design.to_dict(matrix=True)),
            "controls": {name: runs.params_sha256(sdesign.design.to_dict(matrix=True)) for name, sdesign in control_designs.items()},
            "n_perm": int(args.perm),
            "compare_perm": int(args.compare_perm),
            "seed": int(args.seed),
            "method": method,
            "flip": bool(args.flip),
            "lodo": bool(args.lodo),
            "maps": runs.params_sha256(maps_key),
            "mask_sha256": mask_sha,
            "reference": None if ref_info is None else ref_info["sha256"],
        }
        run_root = os.path.abspath(args.run_root or (study.path if in_study else out_dir))
        parents = [run for run in [cohort_run, *args.parent_run] if run]
        run_id = runs.register(run_root, "slnm", out_dir, parents=parents, params={**params, "label": label, "score": args.score}, seeds={"permutation": int(args.seed), "lodo": int(args.seed) + 1, "compare": int(args.seed) + 2})
        record = {"format": FORMAT_NAME, "version": FORMAT_VERSION, "run_id": run_id, "status": "running", "argv": list(argv), "start": now_iso(), "end": None}
        emit(
            "start",
            job="slnm",
            argv=list(argv),
            threads=args.threads,
            run_id=run_id,
            n=design.n,
            n_voxels=n_voxels,
            score=args.score,
            method=method,
            flip=bool(args.flip),
            permute=design.permute,
            permute_reason=design.permute_reason,
            n_perm=count,
            exhaustive=exhaustive,
            controls=list(controls),
            lodo=bool(args.lodo),
        )

        # Resume state and maps
        state = jobs.ResumeState(os.path.join(out_dir, RESUME_NAME))
        if args.resume and state.data.get("params") != params and state.data:
            emit("warning", code="resume_mismatch", message="The saved progress belongs to other inputs or settings; the run starts again.")
        if not args.resume or state.data.get("params") != params:
            state.clear()
            for name in os.listdir(out_dir):
                if name.startswith(".slnm-") and name.endswith(".npz"):
                    os.remove(os.path.join(out_dir, name))
        _remove_previous_outputs(out_dir)
        state.data = {"params": params, "units": dict(state.data.get("units") or {})}
        state.save()
        units = state.data["units"]
        n_stages = 3 + len(controls) + bool(args.lodo) + bool(controls)
        stage = {"index": 0}

        def next_stage(name, **fields):
            stage["index"] += 1
            emit("stage", name=name, index=stage["index"], total=n_stages, **fields)

        next_stage("maps")
        tic = time.monotonic()
        memmap_path = os.path.join(out_dir, MAPS_MEMMAP)
        temporaries.append(memmap_path)
        Y = glm._load_maps(map_paths, mask_shape, mask_affine, indices, memmap_path, emit)
        map_checks = designlib.map_checks(design, Y)
        blocking = [check for check in map_checks if check.level == "block"]
        if blocking:
            raise jobs.JobError("The maps cannot be analysed: " + " | ".join(check.message for check in blocking), code="design_blocked", exit_code=jobs.EXIT_USAGE, checks=[check.to_dict() for check in blocking])
        design.checks = designlib._ordered(design.checks + map_checks)
        Y_used = Y
        if main.spearman:
            ranks_path = os.path.join(out_dir, ".slnm-ranks.npy")
            temporaries.append(ranks_path)
            Y_used = rank_maps(Y, out=np.lib.format.open_memmap(ranks_path, mode="w+", dtype=np.float32, shape=Y.shape))
        timings["load_s"] = time.monotonic() - tic

        def glm_unit(unit, sdesign, Yd, unit_count):
            """Permutation GLM of one design, resumable per batch; returns {r, p_fwe, q_fdr} and the summary."""
            done_path = _unit_file(out_dir, unit, "result")
            if unit in units:
                arrays = _load_arrays(done_path)
                if arrays is not None:
                    next_stage("permutations", unit=unit, resumed=unit_count[0] - 1, total_permutations=unit_count[0] - 1)
                    return arrays, units[unit]
            null_path = _unit_file(out_dir, unit, "null")
            null_state = glm.NullState.load(null_path) if args.resume else None
            if null_state is not None and (null_state.counts is None or null_state.counts.size != n_voxels):
                null_state = None
            resumed = 0 if null_state is None else max(null_state.cursor - 1, 0)
            next_stage("permutations", unit=unit, resumed=resumed, total_permutations=unit_count[0] - 1)
            session = {"started": time.monotonic(), "first": resumed}

            def on_batch(null, done, total, elapsed):
                null.save(null_path)
                spent = time.monotonic() - session["started"]
                new = done - session["first"]
                eta = spent / new * max((total or done) - done, 0) if new > 0 else None
                emit("progress", stage="permutations", unit=unit, done=done, total=total, elapsed=round(spent, 3), eta=None if eta is None else round(eta, 3))

            tic = time.monotonic()
            result = run(sdesign, Yd, n_perm=args.perm, seed=args.seed, ranked=True, batch=args.batch, dtype=glm.precision(unit_count[1]), cancel=cancel, on_batch=on_batch, state=null_state)
            if result.result.n_perm != unit_count[0]:
                raise RuntimeError(f"Expected {unit_count[0]} shufflings, the design yielded {result.result.n_perm}")
            numbers = glm.summary(result)
            numbers["permutations_s"] = time.monotonic() - tic
            numbers["per_permutation_ms"] = result.result.timings.get("per_permutation_ms")
            numbers["precision"] = result.result.precision
            numbers["resumed_from"] = resumed or None
            arrays = {"r": result.effect, "p_fwe": result.result.p_fwe, "q_fdr": result.result.q_fdr}
            _save_arrays(done_path, **arrays)
            glm._remove(null_path)
            units[unit] = numbers
            state.save()
            return arrays, numbers

        main_arrays, main_numbers = glm_unit("main", main, Y_used, (count, exhaustive))
        timings["main_s"] = main_numbers.get("permutations_s")

        control_arrays, control_numbers = {}, {}
        for name, sdesign in control_designs.items():
            rows = [design.sids.index(sid) for sid in sdesign.sids]
            if rows == list(range(design.n)):
                Yc = Y_used
            else:
                sub_path = os.path.join(out_dir, f".slnm-maps-{len(temporaries)}.npy")
                temporaries.append(sub_path)
                Yc = np.lib.format.open_memmap(sub_path, mode="w+", dtype=np.float32, shape=(len(rows), n_voxels))
                if sdesign.spearman:
                    rank_maps(Y, out=Yc, rows=np.asarray(rows))
                else:
                    for start in range(0, n_voxels, CHUNK_VOXELS):
                        part = slice(start, min(start + CHUNK_VOXELS, n_voxels))
                        Yc[:, part] = Y[rows, part]
            control_arrays[name], control_numbers[name] = glm_unit(f"control:{name}", sdesign, Yc, control_counts[name])

        # LODO
        lodo_result, lodo_summary = None, None
        if args.lodo:
            done_path = _unit_file(out_dir, "lodo", "result")
            cached = _load_arrays(done_path) if "lodo" in units else None
            next_stage("lodo", resumed=cached is not None)
            if cached is not None:
                lodo_summary = units["lodo"]
            else:
                tic = time.monotonic()
                lodo_result = lodo(
                    main,
                    Y,
                    n_perm=args.compare_perm,
                    seed=args.seed + 1,
                    cancel=cancel,
                    on_batch=lambda done, total: emit("progress", stage="lodo", done=done, total=total),
                )
                lodo_summary = lodo_result.summary()
                lodo_summary["elapsed_s"] = time.monotonic() - tic
                for message in lodo_result.warnings:
                    emit("warning", code="lodo", message=message)
                if lodo_result.ok:
                    cached = {"maps": lodo_result.maps, "combined": lodo_result.combined, "stouffer_z": lodo_result.stouffer_z, "i2": lodo_result.i2}
                else:
                    cached = {"none": np.zeros(0)}
                _save_arrays(done_path, **cached)
                units["lodo"] = lodo_summary
                state.save()
            if lodo_result is None:
                lodo_result = _lodo_from_cache(lodo_summary, cached)

        # Control comparisons
        comparisons = []
        if controls:
            next_stage("compare", resumed="compare" in units)
            if "compare" in units:
                comparisons = units["compare"]
            else:
                comparisons = compare(
                    main,
                    Y_used,
                    main_arrays["r"],
                    {name: control_arrays[name]["r"] for name in controls},
                    covariates=None if ref is None else ref.covariates(REFERENCE_COVARIATES),
                    n_perm=args.compare_perm,
                    seed=args.seed + 2,
                    ranked=True,
                    cancel=cancel,
                    on_batch=lambda done, total: emit("progress", stage="compare", done=done, total=total),
                    reason=ref_reason,
                )
                units["compare"] = comparisons
                state.save()
            for item in comparisons:
                emit("info", code="control_comparison", control=item["name"], message=item["text"])

        # Outputs
        next_stage("write")
        tic = time.monotonic()
        desc = score_label(args.score)
        fwe = bool(design.spec.inference.get("fwe", True))
        fdr = bool(design.spec.inference.get("fdr", True))
        flip_text = " (score x -1)" if args.flip else ""
        paths = {}

        def write_group(key, stem, name, vector, fill, descrip, intent):
            path = group_map_path(out_dir, label, stem + name)
            _write_vector(path, vector, args.mask, descrip, fill, intent)
            paths[key] = path

        def write_unit(prefix, stem, arrays, numbers, what):
            df = float(numbers["df"])
            write_group(f"{prefix}r", stem, "R", arrays["r"], 0.0, f"sLNM partial r {what} df={numbers['df']}", (2, (df, 0.0, 0.0)))
            if fwe:
                write_group(f"{prefix}p_fwe", stem, "PFwe", arrays["p_fwe"], 1.0, f"sLNM max-stat FWE p K={numbers['n_nonidentity']}", (22, (0.0, 0.0, 0.0)))
            if fdr:
                write_group(f"{prefix}p_fdr", stem, "PFdr", arrays["q_fdr"], 1.0, "sLNM BH-FDR q", (22, (0.0, 0.0, 0.0)))

        write_unit("", desc, main_arrays, main_numbers, f"{args.score}{flip_text} {method}")
        circuit = os.path.join(out_dir, f"circuit-{desc}_desc-R_map.nii.gz")
        _write_vector(circuit, main_arrays["r"], args.mask, f"sLNM circuit: partial r of {args.score}{flip_text}"[:80], 0.0, (2, (float(main_numbers["df"]), 0.0, 0.0)))
        paths["circuit"] = circuit
        control_labels = dict(zip(controls, _unique_labels(controls, score_label), strict=True))
        for name in controls:
            stem = control_labels[name] if control_labels[name] != desc else desc + "Control"
            write_unit(f"control:{name}:", stem, control_arrays[name], control_numbers[name], f"{name} {method}")
        if lodo_result is not None and lodo_result.ok:
            labels = _unique_labels(lodo_result.datasets, lambda text: glm._upper_first(re.sub(r"[^A-Za-z0-9]", "", glm._camel(text))))
            for d, (dataset, dataset_label) in enumerate(zip(lodo_result.datasets, labels, strict=True)):
                write_group(f"lodo:{dataset}", desc, f"Ds{dataset_label}R", lodo_result.maps[d], 0.0, f"sLNM partial r, dataset {dataset} (n={lodo_result.n[d]})", (2, (float(lodo_result.n[d] - 2 - lodo_result.k[d]), 0.0, 0.0)))
            write_group("lodo_combined", desc, "LodoR", lodo_result.combined, 0.0, "sLNM (n-3)-weighted Fisher-z combination of datasets", (0, (0.0, 0.0, 0.0)))
            write_group("stouffer_z", desc, "StoufferZ", lodo_result.stouffer_z, 0.0, "sLNM Stouffer z (descriptive)", (5, (0.0, 0.0, 0.0)))
            write_group("i2", desc, "ISq", lodo_result.i2, 0.0, "sLNM I2 heterogeneity (descriptive)", (0, (0.0, 0.0, 0.0)))
        if args.lodo:
            paths["lodo_tsv"] = write_lodo_tsv(os.path.join(out_dir, LODO_TSV), lodo_result)
        design_json, design_tsv = designlib.save_design(design, out_dir)
        timings["write_s"] = time.monotonic() - tic
        timings["total_s"] = time.monotonic() - started

        def design_record(sdesign, numbers, arrays):
            d = sdesign.design
            return {
                "score": sdesign.score,
                "n": d.n,
                "excluded": {reason: list(sids) for reason, sids in d.excluded.items()},
                "n_excluded": sum(len(sids) for sids in d.excluded.values()),
                "columns": list(d.columns),
                "ranked_columns": [d.columns[index] for index in sdesign.ranked_columns],
                "permutation": {
                    "method": "Freedman-Lane (Winkler et al. 2014): score permutation",
                    "scheme": d.permute,
                    "reason": d.permute_reason,
                    "block_column": d.block_column,
                    "n_perm": int(numbers["n_perm"]),
                    "n_perm_requested": int(args.perm),
                    "unique_perms": designlib._count_json(d.unique_perms),
                    "unique_perms_log10": designlib._log10(d.unique_perms),
                    "exhaustive": bool(d.exhaustive),
                    "seed": int(args.seed),
                    "preview": args.perm < designlib.MIN_N_PERM and not d.exhaustive,
                    "resumed_from": numbers.get("resumed_from"),
                },
                "inference": {key: numbers[key] for key in ("stat", "tails", "df", "n_voxels", "n_perm", "n_nonidentity", "min_attainable_p", "stat_range", "fwe", "fdr", "uncorrected", "precision")},
                "partial_r": {"formula": "t / sqrt(t^2 + df)", "range": [float(np.min(arrays["r"])), float(np.max(arrays["r"]))] if arrays["r"].size else None},
                "checks": [check.to_dict() for check in d.checks],
                "warnings": [check.message for check in d.warnings],
            }

        main_record = design_record(main, main_numbers, main_arrays)
        record.update({
            "status": "done",
            "end": now_iso(),
            "elapsed": round(time.monotonic() - started, 3),
            "library_version": LIBRARY_VERSION,
            "versions": versions(),
            "threads": args.threads,
            "inputs": {
                "maps_tsv": maps_tsv,
                "map_column": column,
                "input": args.input,
                "participants": os.path.abspath(args.participants),
                "mask": os.path.abspath(args.mask),
                "mask_sha256": mask_sha,
                "cohortmaps_json": cohort_json if cohort else None,
                "cohortmaps_run": cohort_run,
                "subject_ids_sha256": subjects_sha,
            },
            "connectome": {"label": connectome_label(label), "name": label, "bundle": bundle_info or None},
            "score": args.score,
            "desc": desc,
            "method": method,
            "spearman": method == "spearman",
            "flip": bool(args.flip),
            "flip_note": "the score was multiplied by -1 before mapping" if args.flip else None,
            "covariates": list(design.spec.covariates),
            "volume_off_reason": design.spec.volume_off_reason,
            "volume_filled_from_maps": filled,
            "design_type": design.design_type,
            **main_record,
            "primary_inference": "max-stat FWE p from score permutation (Freedman-Lane); BH-FDR q secondary",
            "parametric_p": None,
            "parametric_note": PARAMETRIC_NOTE,
            "controls": [
                {**design_record(control_designs[name], control_numbers[name], control_arrays[name]), "desc": control_labels[name], **next(item for item in comparisons if item["name"] == name)}
                for name in controls
            ],
            "comparison": {
                "statistic": "spatial Pearson r over the mask voxels; partial r given the reference maps [mu_r, pc1] (similarity.partial_spatial_correlation)",
                "permutation": "raw-input: the main scores are shuffled (within blocks when the design uses blocks) and the main map is recomputed; two-sided",
                "n_perm": int(args.compare_perm),
                "reference": ref_info,
                "reference_missing_reason": ref_reason,
            }
            if controls
            else None,
            "lodo": lodo_summary,
            "outputs": {key: os.path.basename(path) for key, path in paths.items()},
            "design_json": os.path.basename(design_json),
            "design_matrix": os.path.basename(design_tsv),
            "limitations": glm.limitations(design),
            "timings": {key: (None if value is None else round(float(value), 4)) for key, value in timings.items()},
            "disclaimer": DISCLAIMER,
        })
        json_path = os.path.join(out_dir, SLNM_JSON)
        glm.write_record(json_path, record)
        outputs = [*paths.values(), json_path, design_json, design_tsv]
        for path in outputs:
            emit("result", kind="slnm", path=path)
        state.clear()
        for name in os.listdir(out_dir):
            if name.startswith(".slnm-") and name.endswith(".npz"):
                os.remove(os.path.join(out_dir, name))
        del Y, Y_used
        if run_id:
            runs.update(
                run_root,
                run_id,
                status="done",
                ended=record["end"],
                outputs=[os.path.basename(path) for path in outputs],
                summary={"n": design.n, "score": args.score, "method": method, "n_perm": int(main_numbers["n_perm"]), "fwe_significant": main_numbers["fwe"]["n_significant"], "lodo_p": (lodo_summary or {}).get("p_perm")},
            )
        emit("done", outputs=outputs, elapsed=round(time.monotonic() - started, 3), run_id=run_id, n_perm=int(main_numbers["n_perm"]))
        return jobs.EXIT_OK
    except jobs.Cancelled as exc:
        if record is not None:
            record.update({"status": "cancelled", "end": now_iso(), "stop_reason": exc.reason, "permutations_done": exc.done})
            glm.write_record(os.path.join(out_dir, SLNM_JSON), record)
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
                glm._remove(path)
            except OSError:
                pass
        lock.release()


def _lodo_from_cache(summary, arrays):
    """A :class:`LodoResult` from the summary and arrays a finished LODO unit saved (for --resume)."""
    if not summary.get("computed"):
        return LodoResult(summary.get("column"), [], [], [], None, None, None, None, None, [], [], None, None, None, dict(summary.get("skipped") or {}), 0, False, list(summary.get("warnings") or []))
    return LodoResult(
        column=summary.get("column"),
        datasets=list(summary["datasets"]),
        n=list(summary["n"]),
        k=list(summary["k"]),
        maps=arrays["maps"],
        combined=arrays["combined"],
        matrix=np.asarray(summary["matrix"]),
        mean_offdiag=summary["mean_offdiag_r"],
        p_perm=summary["p_perm"],
        lodo_r=list(summary["lodo_r"]),
        lodo_p=list(summary["lodo_p_perm"]),
        stouffer_z=arrays["stouffer_z"],
        i2=arrays["i2"],
        q=None,
        skipped=dict(summary.get("skipped") or {}),
        n_perm=int(summary["permutation"]["n_perm"]),
        exhaustive=bool(summary["permutation"]["exhaustive"]),
        warnings=list(summary.get("warnings") or []),
    )


if __name__ == "__main__":
    jobs.run_job(main, job="slnm")

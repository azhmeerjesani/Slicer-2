"""Voxelwise GLM with permutation inference for lesion network mapping (numpy and scipy only; no Slicer imports).

A PALM-equivalent subset for LNM specificity and symptom mapping (SPEC_LNM_ADDENDUM §3.4, §3.5):

- :func:`fit`: ordinary least squares through the pseudoinverse; t for a one-row contrast, F for a
  multi-row contrast; df = n - rank(X).
- :class:`FreedmanLane`: the Freedman-Lane permutation scheme (Winkler et al. 2014, Table 2). The
  design is partitioned for the contrast into the effect of interest and the nuisance Z (Beckmann
  et al. 2001), Yr = Rz Y is computed once, and each permutation P refits Y* = P Yr + Hz Y. The fit
  is batched: B permutations share one GEMM ``M (B*p x n) @ Yr (n x V)`` whose rows are the
  orthonormal basis of the design (the pseudoinverse in that basis) with permuted columns, and the
  residual sum of squares comes from the projection identity SSE = ||Yr||² - ||u||², which equals
  ||Yr||² - β̃ᵀ(XᵀX)β̃ (validated against brute-force refits in the tests). Sign flipping (one-sample
  designs) scales the columns instead.
- Statistics: two-sided max |t| or one-sided max t per permutation; F for multi-row contrasts; the
  Aspin-Welch v statistic for two-group designs with variance groups (Winkler 2014, the G statistic
  with one contrast row), whose per-voxel weighted covariance is evaluated in closed form after a
  simultaneous diagonalization of the two groups' cross-products.
- Inference: FWE p = (1 + #{max_null >= obs}) / (K + 1) over the K non-identity permutations (the
  identity is the "1 +"), voxel uncorrected p from int32 exceedance counters, BH-FDR q on it, the
  minimum attainable p = 1 / (K + 1) and the FDR floor ceil(V * p_min / alpha) (the number of voxels
  that must reach p_min before BH can reach q <= alpha). Ties count as exceedances, with a relative
  tolerance that covers the float32 rounding of the batched GEMM (conservative).
- Effect sizes: Hedges g = adjusted mean difference / pooled residual SD x (1 - 3 / (4 df - 1)) for
  group contrasts (and the one-sample mean), partial r = t / sqrt(t² + df) for scores, partial
  eta² = F df1 / (F df1 + df) for F.
- Optional TFCE (tfce.py) with FWE from the maximum TFCE of each permutation.

Permutations come from the design (design.py: free, within-block or whole-block exchange, exhaustive
enumeration when the unique permutations are at most n_perm, sign flips for one-sample designs). A
shuffling P is applied to the data rows (Y* = Yr[P] + Hz Y). An exhaustive enumeration is used in the
orientation whose relabellings X[P⁻¹] are all distinct (:func:`shufflings`), so every unique
permutation is counted once whichever orientation design.py enumerates.

Command line (JSON events, see ``jobs``; exit codes 0 done, 1 error, 2 usage or blocked design, 3
cancelled, 75 locked)::

    python -m LesionNetworkMappingLib.glm --maps maps.tsv --participants participants.tsv
        --design design.json --mask mask.nii.gz --out DIR [--perm N] [--seed S] [--tfce] [--batch B]
        [--threads K] [--cancel-file F] [--resume] [--conn-label L] [--desc D]
        [--precision auto|float32|float64] [--keep-maps] [--run-root DIR] [--parent-run ID]

``maps.tsv`` comes from cohortmaps (participant_id plus the AvgFz and T map paths); the design's
``input`` chooses the column. Subjects of participants.tsv without a map are excluded (listwise,
reported). Lesion volumes missing from participants.tsv are taken from maps.tsv (in memory only;
the job never writes participants.tsv). Maps are loaded into an (n x V) float32 memmap in the
output folder. A blocked design (or blocking map checks) is an error event with the messages and
exit code 2. Progress is reported per batch with an ETA; exceedance counts, the max-null vectors
and the permutation cursor are saved after every batch, so ``--resume`` continues a stopped run and
reproduces the uninterrupted result. Outputs (exact mask header; export naming)::

    group_conn-<label>_desc-<contrast>{Tstat|Fstat, PUnc, PFwe, PFdr, TfcePFwe, HedgesG|PartialR|PartialEtaSq}_map.nii.gz
    glm.json, design.json, design_matrix.tsv

P-value maps are plain p (not -log10), 1 outside the mask. The Aspin-Welch v statistic is written
as the Tstat map (glm.json "stat": "v"). The run is registered (kind "glm") with the cohortmaps run
of maps.tsv (its sibling cohortmaps.json) as parent. The permutations run in float32, or in
float64 for an exhaustive enumeration (``--precision auto``, the default), so that exhaustive p is
exact.
"""

import os
import sys

if __name__ == "__main__":
    from LesionNetworkMappingLib.jobs import set_blas_threads

    set_blas_threads(sys.argv[1:])  # before numpy is imported: BLAS reads these variables once

import dataclasses
import itertools
import json
import math
import re
import time

import numpy as np
from scipy import linalg as scipy_linalg

from . import design as designlib
from . import jobs, nifti, runs
from . import tfce as tfcelib
from .export import DISCLAIMER, LIBRARY_VERSION, connectome_label, file_sha256, group_map_path, now_iso, versions
from .geometry import mask_indices

FORMAT_NAME = "slicer-lnm-glm"
FORMAT_VERSION = 1
GLM_JSON = "glm.json"
RESUME_NAME = ".glm-resume.json"
RESUME_ARRAYS = ".glm-resume.npz"
MAPS_MEMMAP = ".glm-maps.npy"
LOCK_NAME = ".glm.lock"
FDR_ALPHA = 0.05
FWE_ALPHA = 0.05
#: Map column of maps.tsv for each design input
MAP_COLUMNS = {"Fz": "AvgFz", "T": "T"}
EXCLUDED_NO_MAP = "no map in maps.tsv"
#: Columns of maps.tsv that must be equal for maps to be pooled (cohortmaps.COMPATIBILITY_COLUMNS)
COMPATIBILITY_COLUMNS = ("manifest_sha256", "subject_ids_sha256", "statistic", "supersample", "probabilistic", "library_version")
STATS = ("t", "F", "v")
EFFECT_NAMES = {"hedges_g": "HedgesG", "partial_r": "PartialR", "partial_eta2": "PartialEtaSq"}
#: Bytes of the GEMM output of one batch (the batch size is chosen to fit, between 1 and MAX_BATCH)
BATCH_BYTES = 128 * 1024**2
MAX_BATCH = 64
#: Voxels per chunk of the float64 preparation (observed statistics and Yr)
CHUNK_VOXELS = 32768
#: Relative tolerance of "null >= observed" (ties count; covers the rounding of the compute precision).
#: It is scaled per voxel by the conditioning kappa = ||Yr||² / SSE of the observed fit (see
#: :attr:`Prepared.kappa`): the null SSE = ||Yr||² - ||u||² loses that factor of relative precision, so a
#: relabelling that ties the observed statistic exactly (the complementary labelling of a balanced
#: two-group design, the all -1 sign flip) still counts when the effect is strong.
TIE_RTOL = {np.dtype(np.float32): 1e-5, np.dtype(np.float64): 1e-9}
TIE_ATOL = {np.dtype(np.float32): 1e-6, np.dtype(np.float64): 1e-12}
#: Largest kappa a float32 run accepts: beyond it (tie tolerance above 1%; |t| above about
#: 30 sqrt(df)) the permutations are computed in float64 instead
KAPPA_MAX_FLOAT32 = 1e3
#: SSE is floored at this fraction of ||Yr||² (cancellation in SSE = ||Yr||² - ||u||²)
SSE_RTOL = {np.dtype(np.float32): 1e-5, np.dtype(np.float64): 1e-12}
#: A voxel whose residualized sum of squares is below this fraction of its raw sum of squares is
#: constant after removing the nuisance: its statistic is 0 (p = 1) in every permutation
DEGENERATE_RTOL = 1e-20
_TINY = 1e-30


# Fit


@dataclasses.dataclass
class Fit:
    """An OLS fit (:func:`fit`): ``beta`` (p x V), ``psi`` (contrast estimates, r x V), ``sse`` (V),
    ``df`` (n - rank), ``rank``, ``kind`` ("t" or "F"), ``stat`` (V) and ``df1`` (rank of the contrast).
    """

    beta: np.ndarray
    psi: np.ndarray
    sse: np.ndarray
    df: int
    rank: int
    kind: str
    stat: np.ndarray
    df1: int


def fit(Y, X, C, chunk=CHUNK_VOXELS):
    """Ordinary least squares fit of every column of Y through the pseudoinverse of X.

    :param Y: n x V data (maps in design order).
    :param X: n x p design matrix.
    :param C: contrast, p weights or r x p rows: t for one row, F for several.
    :return: :class:`Fit` (float64). Voxels with a perfect fit (SSE = 0) get a statistic of 0.
    :raises ValueError: no residual degrees of freedom, or a contrast of the wrong size.
    """
    X = np.asarray(X, dtype=np.float64)
    C = np.atleast_2d(np.asarray(C, dtype=np.float64))
    Y = np.asarray(Y)
    if Y.ndim == 1:
        Y = Y[:, None]
    n, p = X.shape
    if Y.shape[0] != n:
        raise ValueError(f"Y has {Y.shape[0]} rows but X has {n}")
    if C.shape[1] != p:
        raise ValueError(f"The contrast has {C.shape[1]} weights but the design has {p} columns")
    pinv = np.linalg.pinv(X)
    rank = int(np.linalg.matrix_rank(X))
    df = n - rank
    if df < 1:
        raise ValueError(f"No residual degrees of freedom ({n} rows, rank {rank})")
    cov = C @ (pinv @ pinv.T) @ C.T  # C (XᵀX)⁺ Cᵀ
    kind = "t" if C.shape[0] == 1 else "F"
    df1 = int(np.linalg.matrix_rank(C))
    cov_inv = np.linalg.pinv(cov) if kind == "F" else None
    n_voxels = Y.shape[1]
    beta = np.empty((p, n_voxels))
    psi = np.empty((C.shape[0], n_voxels))
    sse = np.empty(n_voxels)
    stat = np.zeros(n_voxels)
    for start in range(0, n_voxels, chunk):
        part = slice(start, min(start + chunk, n_voxels))
        y = np.asarray(Y[:, part], dtype=np.float64)
        b = pinv @ y
        residual = y - X @ b
        beta[:, part] = b
        psi[:, part] = C @ b
        sse[part] = np.einsum("nv,nv->v", residual, residual)
    sigma2 = sse / df
    valid = sigma2 > 0
    if kind == "t":
        np.divide(psi[0], np.sqrt(sigma2 * cov[0, 0]), out=stat, where=valid)
    else:
        numerator = np.einsum("iv,ij,jv->v", psi, cov_inv, psi) / df1
        np.divide(numerator, sigma2, out=stat, where=valid)
    return Fit(beta=beta, psi=psi, sse=sse, df=df, rank=rank, kind=kind, stat=stat, df1=df1)


def nuisance_partition(X, C):
    """Beckmann partitioning of a design for a contrast: (X_effect (n x r), Z (n x (p - r))).

    X_effect = X D Cᵀ (C D Cᵀ)⁻¹ and Z = X D Cv (Cvᵀ D Cv)⁻¹ with D = (XᵀX)⁻¹, Cu a basis of the null
    space of C and Cv = Cu - Cᵀ (C D Cᵀ)⁻¹ C D Cu (Winkler et al. 2014, Appendix A). [X_effect, Z]
    spans the space of X, the contrast estimate of the full model is the coefficient of X_effect,
    and Z carries no part of it.
    """
    X = np.asarray(X, dtype=np.float64)
    C = np.atleast_2d(np.asarray(C, dtype=np.float64))
    D = np.linalg.pinv(X.T @ X)
    Cm = C.T
    middle = np.linalg.pinv(Cm.T @ D @ Cm)
    effect = X @ D @ Cm @ middle
    Cu = scipy_linalg.null_space(C)
    if Cu.shape[1] == 0:
        return effect, np.zeros((X.shape[0], 0))
    Cv = Cu - Cm @ middle @ Cm.T @ D @ Cu
    Z = X @ D @ Cv @ np.linalg.pinv(Cv.T @ D @ Cv)
    return effect, Z


# Freedman-Lane engine


class FreedmanLane:
    """Freedman-Lane permutation of a GLM, batched (see the module docstring).

    :param X: n x p design matrix (full column rank).
    :param C: contrast: one row for t and v, one or more rows for F.
    :param stat: "t", "F" or "v" (Aspin-Welch; needs two variance groups and one contrast row).
    :param variance_groups: per-row group codes for "v" (exactly two distinct values).
    :param exchange: "permutation" (index arrays) or "sign_flip" (±1 vectors).
    :param dtype: compute precision of the permutations (float32, the default, or float64); the
      observed statistics are always computed in float64.
    """

    def __init__(self, X, C, stat="t", variance_groups=None, exchange="permutation", dtype=np.float32):
        X = np.asarray(X, dtype=np.float64)
        C = np.atleast_2d(np.asarray(C, dtype=np.float64))
        if stat not in STATS:
            raise ValueError(f"Unknown statistic {stat!r}; expected one of {', '.join(STATS)}")
        if exchange not in ("permutation", "sign_flip"):
            raise ValueError(f"Unknown exchange {exchange!r}")
        n, p = X.shape
        if C.shape[1] != p:
            raise ValueError(f"The contrast has {C.shape[1]} weights but the design has {p} columns")
        if np.linalg.matrix_rank(X) < p:
            raise ValueError("The design matrix is rank-deficient")
        r = int(np.linalg.matrix_rank(C))
        if r < C.shape[0]:
            raise ValueError("The contrast rows are linearly dependent")
        if stat in ("t", "v") and r != 1:
            raise ValueError(f"The {stat} statistic needs a one-row contrast; use F for {r} rows")
        if n - p < 1:
            raise ValueError(f"No residual degrees of freedom ({n} rows, {p} columns)")
        self.X, self.C, self.stat, self.exchange = X, C, stat, exchange
        self.n, self.p, self.r = n, p, r
        self.df = n - p
        self.dtype = np.dtype(dtype)
        if self.dtype not in TIE_RTOL:
            raise ValueError(f"Unsupported precision {self.dtype}; use float32 or float64")
        _, Z = nuisance_partition(X, C)
        self.Z = Z
        self.Zpinv = np.linalg.pinv(Z) if Z.shape[1] else None
        # Orthonormal basis of span(X), rotated so that its first r columns span the contrast:
        # u = Qᵀ Y* gives t = u₀ / sqrt(SSE / df), F = ||u₀..u_r-1||² / r / (SSE / df) and SSE = ||Yr||² - ||u||²
        Q, R = np.linalg.qr(X)
        W = scipy_linalg.solve_triangular(R, C.T, trans="T")  # R⁻ᵀ Cᵀ: Var(psi) / sigma² = WᵀW
        rotation, triangle = np.linalg.qr(W, mode="complete")
        if triangle[0, 0] < 0:  # the first axis points along the contrast, so that t = +u₀ / sigma
            rotation[:, 0] = -rotation[:, 0]
        self.psi_scale = float(abs(triangle[0, 0]))  # ψ = ||W|| u₀ for one-row contrasts
        self.Q = Q @ rotation
        self.basis_T = np.ascontiguousarray(self.Q.T.astype(self.dtype))
        self.groups = None
        self.variance_groups = variance_groups
        if stat == "v":
            self._setup_variance_groups(variance_groups)

    def with_dtype(self, dtype):
        """The same engine with another compute precision."""
        return FreedmanLane(self.X, self.C, self.stat, self.variance_groups, self.exchange, dtype)

    def _setup_variance_groups(self, variance_groups):
        if variance_groups is None:
            raise ValueError("The v statistic needs variance groups")
        groups = np.asarray(variance_groups).reshape(-1)
        if groups.size != self.n:
            raise ValueError(f"Expected {self.n} variance group codes, got {groups.size}")
        levels = sorted(set(groups.tolist()))
        if len(levels) != 2:
            raise ValueError(f"The v statistic needs exactly 2 variance groups, got {len(levels)}")
        first = groups == levels[0]
        self.groups = first
        X1 = self.X[first]
        gram = self.X.T @ self.X
        lower = np.linalg.cholesky(gram)
        inner = scipy_linalg.solve_triangular(lower, scipy_linalg.solve_triangular(lower, X1.T @ X1, lower=True).T, lower=True)
        d, U = np.linalg.eigh((inner + inner.T) / 2.0)
        self.v_d = np.clip(d, 0.0, 1.0)
        e = U.T @ scipy_linalg.solve_triangular(lower, self.C[0], lower=True)
        self.v_e2 = e**2
        leverage = np.sum(self.Q**2, axis=1)
        self.v_trace = (float(np.sum(1.0 - leverage[first])), float(np.sum(1.0 - leverage[~first])))
        if min(self.v_trace) <= 1e-9:
            raise ValueError("Each variance group needs residual degrees of freedom (at least one more subject than it has parameters)")
        Q1 = self.Q[first]
        self.v_A1 = Q1.T @ Q1  # Σ over group 1 of (Q u)² = uᵀ A1 u
        mask = first.astype(np.float64)
        self.v_mask = mask
        self.v_basis1_T = np.ascontiguousarray((self.Q * mask[:, None]).T.astype(self.dtype))

    # Statistics from the basis coordinates u

    def _statistic(self, u, yy, floor, extra=None):
        """Statistic from u (..., p, V), ||Yr||² (V) and, for v, (a1, m1) of group 1."""
        ss = np.einsum("...pv,...pv->...v", u, u)
        sse = yy - ss
        np.maximum(sse, floor, out=sse)
        if self.stat == "t":
            return u[..., 0, :] / np.sqrt(sse / self.df)
        if self.stat == "F":
            head = u[..., : self.r, :]
            numerator = np.einsum("...pv,...pv->...v", head, head) / self.r
            return numerator / (sse / self.df)
        a1, m1 = extra
        b1 = np.einsum("...pv,...pv->...v", u, m1)
        c1 = np.einsum("...pv,pq,...qv->...v", u, self.v_A1.astype(u.dtype), u)
        sse1 = a1 - 2.0 * b1 + c1
        np.clip(sse1, floor, None, out=sse1)
        sse2 = np.maximum(sse - sse1, floor)
        w1 = self.v_trace[0] / sse1
        w2 = self.v_trace[1] / sse2
        denominator = np.zeros_like(w1)
        for dk, ek in zip(self.v_d, self.v_e2, strict=True):
            denominator += ek / (w1 * dk + w2 * (1.0 - dk))
        return self.psi_scale * u[..., 0, :] / np.sqrt(np.maximum(denominator, _TINY))

    def prepare(self, Y, chunk=CHUNK_VOXELS):
        """Residualize the data once: Yr = Rz Y (compute precision), ||Yr||², and the observed fit (float64).

        :param Y: n x V data (an array or memmap; read in voxel chunks).
        :return: :class:`Prepared`.
        """
        if not hasattr(Y, "shape"):
            Y = np.asarray(Y)
        if Y.ndim != 2 or Y.shape[0] != self.n:
            raise ValueError(f"Expected data of shape ({self.n}, V), got {Y.shape}")
        n_voxels = Y.shape[1]
        Yr = np.empty((self.n, n_voxels), dtype=self.dtype)
        Yr2 = np.empty((self.n, n_voxels), dtype=self.dtype) if self.stat == "v" else None
        yy = np.empty(n_voxels)
        stat = np.empty(n_voxels)
        psi = np.empty(n_voxels) if self.r == 1 else None
        sse_out = np.empty(n_voxels)
        kappa = np.empty(n_voxels)
        degenerate = np.zeros(n_voxels, dtype=bool)
        for start in range(0, n_voxels, chunk):
            part = slice(start, min(start + chunk, n_voxels))
            y = np.asarray(Y[:, part], dtype=np.float64)
            raw = np.einsum("nv,nv->v", y, y)
            yr = y - self.Z @ (self.Zpinv @ y) if self.Zpinv is not None else y.copy()
            ss = np.einsum("nv,nv->v", yr, yr)
            flat = (ss <= DEGENERATE_RTOL * raw) | (raw <= 0)
            yr[:, flat] = 0.0
            ss[flat] = 0.0
            degenerate[part] = flat
            u = self.Q.T @ yr
            floor = np.maximum(SSE_RTOL[np.dtype(np.float64)] * ss, _TINY)
            extra = None
            if self.stat == "v":
                extra = (self.v_mask @ (yr * yr), (self.Q * self.v_mask[:, None]).T @ yr)
            stat[part] = self._statistic(u, ss, floor, extra)
            sse_out[part] = np.maximum(ss - np.einsum("pv,pv->v", u, u), floor)
            smallest = sse_out[part]
            if self.stat == "v":  # the group SSEs are also differences of ||Yr||²-sized terms
                residual = yr - self.Q @ u
                sse1 = self.v_mask @ (residual * residual)
                smallest = np.maximum(np.minimum(sse1, smallest - sse1), floor)
            kappa[part] = np.maximum(ss / np.maximum(smallest, _TINY), 1.0)
            if psi is not None:
                psi[part] = self.psi_scale * u[0]
            yy[part] = ss
            Yr[:, part] = yr
            if Yr2 is not None:
                Yr2[:, part] = yr * yr
        stat[degenerate] = 0.0
        kappa[degenerate] = 1.0
        if psi is not None:
            psi[degenerate] = 0.0
        return Prepared(Yr=Yr, Yr2=Yr2, yy=yy, stat=stat, psi=psi, sse=sse_out, degenerate=degenerate, kappa=kappa)

    def null_statistics(self, prepared, shufflings, out=None):
        """Statistics of a batch of shufflings (B x V, compute precision).

        :param shufflings: B permutation index arrays (data rows Y[P]) or ±1 vectors (sign flips).
        :param out: optional work buffer of at least B * p * V elements (compute precision).
        """
        block = np.asarray(shufflings)
        if block.ndim != 2 or block.shape[1] != self.n:
            raise ValueError(f"Expected shufflings of shape (B, {self.n}), got {block.shape}")
        B = block.shape[0]
        n_voxels = prepared.Yr.shape[1]
        if self.exchange == "permutation":
            inverse = np.argsort(block, axis=1)
            M = self.basis_T[:, inverse].transpose(1, 0, 2).reshape(B * self.p, self.n)
        else:
            M = (self.basis_T[None, :, :] * block[:, None, :].astype(self.dtype)).reshape(B * self.p, self.n)
        M = np.ascontiguousarray(M, dtype=self.dtype)
        if out is None or out.size < B * self.p * n_voxels:
            out = np.empty(B * self.p * n_voxels, dtype=self.dtype)
        U = out[: B * self.p * n_voxels].reshape(B * self.p, n_voxels)
        np.matmul(M, prepared.Yr, out=U)
        U = U.reshape(B, self.p, n_voxels)
        yy = prepared.yy_compute
        floor = prepared.floor_compute
        extra = None
        if self.stat == "v":
            if self.exchange == "permutation":
                M1 = self.v_basis1_T[:, inverse].transpose(1, 0, 2).reshape(B * self.p, self.n)
                masks = self.v_mask[inverse].astype(self.dtype)
            else:
                M1 = (self.v_basis1_T[None, :, :] * block[:, None, :].astype(self.dtype)).reshape(B * self.p, self.n)
                masks = np.broadcast_to(self.v_mask.astype(self.dtype), (B, self.n))
            m1 = (np.ascontiguousarray(M1) @ prepared.Yr).reshape(B, self.p, n_voxels)
            a1 = np.ascontiguousarray(masks) @ prepared.Yr2
            extra = (a1, m1)
        return self._statistic(U, yy, floor, extra)


@dataclasses.dataclass
class Prepared:
    """Data prepared by :meth:`FreedmanLane.prepare`: Yr (n x V), Yr² (v only), ||Yr||², and the
    observed statistic, contrast estimate (one-row contrasts) and SSE (float64), plus the voxels that
    are constant after removing the nuisance (statistic 0), and ``kappa`` = ||Yr||² / SSE (>= 1; the
    smaller group SSE for v), the factor by which the null SSE loses relative precision (it scales the
    tie tolerance; None means 1).
    """

    Yr: np.ndarray
    Yr2: np.ndarray | None
    yy: np.ndarray
    stat: np.ndarray
    psi: np.ndarray | None
    sse: np.ndarray
    degenerate: np.ndarray
    kappa: np.ndarray | None = None

    def __post_init__(self):
        dtype = self.Yr.dtype
        self.yy_compute = self.yy.astype(dtype)
        self.floor_compute = np.maximum(SSE_RTOL[np.dtype(dtype)] * self.yy, _TINY).astype(dtype)


# Permutation inference


@dataclasses.dataclass
class NullState:
    """Progress of a permutation run (saved after every batch for --resume).

    ``cursor`` counts the shufflings consumed, the identity included; ``counts`` holds the per-voxel
    exceedances of the non-identity shufflings; ``max_null`` (and ``tfce_max_null``) one maximum per
    non-identity shuffling, in order.
    """

    cursor: int = 0
    counts: np.ndarray | None = None
    max_null: list = dataclasses.field(default_factory=list)
    tfce_max_null: list = dataclasses.field(default_factory=list)

    def save(self, path):
        """Write the state atomically as .npz."""
        path = os.fspath(path)
        tmp_path = f"{path}.{os.getpid()}.tmp.npz"
        try:
            np.savez(
                tmp_path,
                cursor=np.array(self.cursor, dtype=np.int64),
                counts=np.zeros(0, dtype=np.int32) if self.counts is None else self.counts,
                max_null=np.asarray(self.max_null, dtype=np.float64),
                tfce_max_null=np.asarray(self.tfce_max_null, dtype=np.float64),
            )
            os.replace(tmp_path, path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        return path

    @classmethod
    def load(cls, path):
        """A state read from .npz (None when the file is missing or unreadable)."""
        try:
            with np.load(os.fspath(path)) as data:
                return cls(
                    cursor=int(data["cursor"]),
                    counts=np.array(data["counts"], dtype=np.int32),
                    max_null=data["max_null"].tolist(),
                    tfce_max_null=data["tfce_max_null"].tolist(),
                )
        except (OSError, ValueError, KeyError, EOFError):
            return None


@dataclasses.dataclass
class PermutationResult:
    """Result of :func:`permutation_test`.

    - ``stat``: observed statistic (float64; t, F or v); ``df``, ``df1``, ``kind``, ``tails``;
    - ``p_unc``, ``p_fwe`` (max-stat), ``q_fdr`` (BH on p_unc);
    - ``tfce`` and ``tfce_p_fwe`` (None without TFCE);
    - ``n_perm`` (shufflings used, identity included, = K + 1), ``min_p`` = 1 / (K + 1),
      ``fdr_floor_voxels`` = ceil(V * min_p / alpha);
    - ``max_null``, ``tfce_max_null``, ``counts``; ``psi`` and ``sse`` of the observed fit; ``timings``;
    - ``precision``: compute precision of the permutations ("float32" or "float64"; see
      :data:`KAPPA_MAX_FLOAT32`).
    """

    stat: np.ndarray
    kind: str
    tails: int
    df: int
    df1: int
    p_unc: np.ndarray
    p_fwe: np.ndarray
    q_fdr: np.ndarray
    n_perm: int
    min_p: float
    fdr_floor_voxels: int
    max_null: np.ndarray
    counts: np.ndarray
    psi: np.ndarray | None
    sse: np.ndarray
    tfce: np.ndarray | None = None
    tfce_p_fwe: np.ndarray | None = None
    tfce_max_null: np.ndarray | None = None
    timings: dict = dataclasses.field(default_factory=dict)
    fdr_alpha: float = FDR_ALPHA
    precision: str = "float32"

    @property
    def n_voxels(self):
        return int(self.stat.size)

    @property
    def K(self):  # noqa: N802 (the paper's name)
        return self.n_perm - 1


def _is_identity(shuffling, exchange):
    shuffling = np.asarray(shuffling)
    if exchange == "sign_flip":
        return bool(np.all(shuffling == 1))
    return bool(np.array_equal(shuffling, np.arange(shuffling.size)))


def _magnitude(values, two_sided):
    return np.abs(values) if two_sided else values


def _threshold(observed, dtype, two_sided, kappa=None):
    """The values a null statistic must reach to count as an exceedance (observed minus the tie tolerance).

    :param kappa: per-voxel conditioning of the null SSE (:attr:`Prepared.kappa`) that scales the
      relative tolerance (default 1).
    """
    magnitude = _magnitude(observed, two_sided)
    dtype = np.dtype(dtype)
    rtol = TIE_RTOL[dtype] if kappa is None else TIE_RTOL[dtype] * np.maximum(np.asarray(kappa, dtype=np.float64), 1.0)
    return magnitude - (rtol * np.abs(magnitude) + TIE_ATOL[dtype])


def fdr_bh(p):
    """Benjamini-Hochberg q-values (adjusted p-values) of a vector of p-values."""
    p = np.asarray(p, dtype=np.float64).reshape(-1)
    m = p.size
    if m == 0:
        return p.copy()
    order = np.argsort(p, kind="stable")
    ranked = p[order] * m / np.arange(1, m + 1)
    ranked = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(m)
    out[order] = np.minimum(ranked, 1.0)
    return out


def fdr_floor_voxels(n_voxels, min_p, alpha=FDR_ALPHA):
    """Voxels that must reach the smallest attainable p before BH can give q <= alpha: ceil(V * p_min / alpha)."""
    return math.ceil(n_voxels * min_p / alpha - 1e-9)


def fwe_from_null(observed, max_null, dtype=np.float64, two_sided=True, kappa=None):
    """FWE p = (1 + #{max_null >= observed}) / (K + 1) for every voxel (K = len(max_null)).

    :param kappa: per-voxel conditioning that scales the tie tolerance (see :func:`_threshold`).
    """
    max_null = np.sort(np.asarray(max_null, dtype=np.float64))
    K = max_null.size
    threshold = _threshold(np.asarray(observed, dtype=np.float64), dtype, two_sided, kappa)
    exceed = K - np.searchsorted(max_null, threshold, side="left")
    return (1.0 + exceed) / (K + 1.0)


def default_batch(n_rows, n_voxels, total=None):
    """Shufflings per GEMM: the output (B * n_rows x V floats) stays within BATCH_BYTES; 1..MAX_BATCH."""
    batch = int(BATCH_BYTES // max(4 * n_rows * max(n_voxels, 1), 1))
    batch = max(1, min(batch, MAX_BATCH))
    if total is not None:
        batch = max(1, min(batch, int(total)))
    return batch


def _check_cancel(cancel, done):
    if cancel is None:
        return
    if hasattr(cancel, "raise_if_cancelled"):
        cancel.raise_if_cancelled(done=done)
        return
    stop = cancel.is_set() if hasattr(cancel, "is_set") else cancel()
    if stop:
        raise jobs.Cancelled(done=done, reason="cancelled")


def permutation_test(
    Y,
    X,
    C,
    shufflings,
    stat="t",
    tails=2,
    variance_groups=None,
    exchange="permutation",
    n_total=None,
    batch=None,
    dtype=np.float32,
    tfce_grid=None,
    tfce_params=None,
    cancel=None,
    on_batch=None,
    state=None,
    fdr_alpha=FDR_ALPHA,
    engine=None,
    prepared=None,
):
    """Freedman-Lane permutation test of every voxel with max-stat FWE, uncorrected p and BH-FDR.

    :param Y: n x V data in design order (array or memmap).
    :param X, C: design matrix and contrast (one row for t and v, one or more for F).
    :param shufflings: iterable of permutation index arrays (or ±1 vectors for sign flips); the
      first must be the identity. Each non-identity item is one of the K null samples.
    :param stat: "t", "F" or "v"; ``tails``: 2 (max |stat|) or 1 (max stat; the contrast carries
      the direction). F is always one-sided.
    :param n_total: number of shufflings expected (identity included), for progress only.
    :param batch: shufflings per GEMM (default :func:`default_batch`).
    :param dtype: compute precision of the null statistics (float32 or float64). A float32 run whose
      data have a voxel with kappa above :data:`KAPPA_MAX_FLOAT32` (a very strong effect: the null SSE
      cannot be resolved in float32) is computed in float64 instead (``result.precision``).
    :param tfce_grid: a ``tfce.TfceGrid`` of the mask (V in-mask voxels) to add TFCE and its FWE p.
    :param tfce_params: {"H", "E", "steps"} for TFCE (defaults: 2, 0.5, 100).
    :param cancel: ``jobs.CancelToken`` or any callable/Event; checked before each batch
      (``jobs.Cancelled`` is raised with the state saved by ``on_batch``).
    :param on_batch: ``on_batch(state, done, total, elapsed)`` after each batch (progress, checkpoints).
    :param state: :class:`NullState` to resume from: its first ``cursor`` shufflings are skipped.
    :return: :class:`PermutationResult`.
    """
    timings = {}
    started = time.monotonic()
    engine = engine or FreedmanLane(X, C, stat, variance_groups, exchange, dtype)
    two_sided = tails == 2 and engine.stat != "F"
    if prepared is None:
        prepared = engine.prepare(Y)
    if engine.dtype == np.float32 and prepared.kappa is not None and float(np.max(prepared.kappa, initial=1.0)) > KAPPA_MAX_FLOAT32:
        engine = engine.with_dtype(np.float64)
        prepared = engine.prepare(Y)
        timings["promoted_to_float64"] = True
    timings["prepare_s"] = time.monotonic() - started
    observed = prepared.stat
    n_voxels = observed.size
    threshold = _threshold(observed, engine.dtype, two_sided, prepared.kappa).astype(engine.dtype)
    params = {"H": tfcelib.DEFAULT_H, "E": tfcelib.DEFAULT_E, "steps": tfcelib.DEFAULT_STEPS, **(tfce_params or {})}
    tfce_observed = None
    if tfce_grid is not None:
        tic = time.monotonic()
        tfce_observed = tfce_grid.enhance(observed, params["H"], params["E"], params["steps"], two_sided)
        timings["tfce_observed_s"] = time.monotonic() - tic

    state = state if state is not None else NullState()
    if state.counts is None or state.counts.size != n_voxels:
        if state.cursor > 0:
            raise ValueError("The resume state does not match the data")
        state.counts = np.zeros(n_voxels, dtype=np.int32)
    iterator = iter(shufflings)
    first = next(iterator, None)
    if first is None or not _is_identity(first, engine.exchange):
        raise ValueError("The first shuffling must be the identity")
    if state.cursor == 0:
        state.cursor = 1
    else:
        for _ in itertools.islice(iterator, state.cursor - 1):
            pass
    rows = engine.p * (2 if engine.stat == "v" else 1)
    batch = int(batch) if batch else default_batch(rows, n_voxels, None if n_total is None else max(n_total - 1, 1))
    work = np.empty(batch * engine.p * n_voxels, dtype=engine.dtype)
    resumed = state.cursor
    permutation_started = time.monotonic()
    tfce_seconds = 0.0
    while True:
        _check_cancel(cancel, state.cursor - 1)
        block = list(itertools.islice(iterator, batch))
        if not block:
            break
        values = engine.null_statistics(prepared, block, out=work)
        magnitude = _magnitude(values, two_sided)
        state.max_null.extend(magnitude.max(axis=1).astype(np.float64).tolist())
        state.counts += np.count_nonzero(magnitude >= threshold[None, :], axis=0).astype(np.int32)
        if tfce_grid is not None:
            tic = time.monotonic()
            for row in values:
                state.tfce_max_null.append(tfce_grid.max_enhanced(row.astype(np.float64), params["H"], params["E"], params["steps"], two_sided))
            tfce_seconds += time.monotonic() - tic
        state.cursor += len(block)
        if on_batch is not None:
            on_batch(state, state.cursor - 1, None if n_total is None else n_total - 1, time.monotonic() - permutation_started)
    elapsed = time.monotonic() - permutation_started
    computed = state.cursor - resumed
    timings.update({
        "batch": batch,
        "permutations_s": elapsed,
        "permutations_computed": computed,
        "per_permutation_ms": 1000.0 * elapsed / computed if computed else None,
        "tfce_permutations_s": tfce_seconds if tfce_grid is not None else None,
    })
    K = state.cursor - 1
    n_perm = K + 1
    min_p = 1.0 / n_perm
    p_unc = (1.0 + state.counts.astype(np.float64)) / n_perm
    max_null = np.asarray(state.max_null, dtype=np.float64)
    p_fwe = fwe_from_null(observed, max_null, engine.dtype, two_sided, prepared.kappa)
    result = PermutationResult(
        stat=observed,
        kind=engine.stat,
        tails=2 if two_sided else 1,
        df=engine.df,
        df1=engine.r,
        p_unc=p_unc,
        p_fwe=p_fwe,
        q_fdr=fdr_bh(p_unc),
        n_perm=n_perm,
        min_p=min_p,
        fdr_floor_voxels=fdr_floor_voxels(n_voxels, min_p, fdr_alpha),
        max_null=max_null,
        counts=state.counts.copy(),
        psi=prepared.psi,
        sse=prepared.sse,
        timings=timings,
        fdr_alpha=fdr_alpha,
        precision=engine.dtype.name,
    )
    if tfce_grid is not None:
        tfce_null = np.asarray(state.tfce_max_null, dtype=np.float64)
        result.tfce = tfce_observed
        result.tfce_max_null = tfce_null
        result.tfce_p_fwe = fwe_from_null(tfce_observed, tfce_null, engine.dtype, two_sided)
    timings["total_s"] = time.monotonic() - started
    return result


# Designs


def shufflings(design, n_perm=None, seed=None):
    """(iterator, count, exhaustive): the shufflings of a design (identity first) and how many it yields.

    :param n_perm: permutations to draw, identity included (default: the spec's n_perm); every unique
      permutation is enumerated instead when there are at most that many (and at most 1e6).
    :param seed: random seed (default: the spec's).
    """
    n_perm = int(design.spec.n_perm if n_perm is None else n_perm)
    seed = int(design.seed if seed is None else seed)
    count, exhaustive = shuffling_count(design, n_perm)
    if design.exchange == "sign_flip":
        iterator = designlib.sign_flips(design.n, n_perm, seed, design.blocks, design.permute)
    else:
        iterator = designlib.permutations(design.n, design.blocks, design.permute, n_perm, seed, design.row_labels)
        if exhaustive:
            iterator = iter(_distinct_relabellings(list(iterator), design.row_labels))
    return iterator, count, exhaustive


def shuffling_count(design, n_perm=None):
    """(shufflings yielded, identity included; exhaustive) of a design for n_perm requested permutations."""
    n_perm = int(design.spec.n_perm if n_perm is None else n_perm)
    unique = int(design.unique_perms)
    exhaustive = unique <= min(n_perm, designlib.EXHAUSTIVE_CAP)
    return (unique if exhaustive else n_perm), exhaustive


def _distinct_relabellings(perms, row_labels):
    """Exhaustive permutations as data-row index arrays (Y[P]) that give distinct designs.

    Y[P] tests the data against the design rows X[P⁻¹], so the enumeration is complete when the
    labels of P⁻¹ are all distinct. ``design.permutations`` enumerates arrangements P whose own labels
    (labels[P]) are distinct; such a list is inverted. Either form is accepted.
    """
    labels = np.asarray(row_labels).reshape(-1)

    def distinct(arrays):
        return len({tuple(labels[array].tolist()) for array in arrays}) == len(arrays)

    inverses = [np.argsort(perm).astype(np.intp) for perm in perms]
    if distinct(inverses):
        return perms
    if distinct(perms):
        return inverses
    raise ValueError("The exhaustive permutations of the design are not distinct")


def limitations(design):
    """The design's limitations, plus the small-sample caveat of the v statistic (glm.json)."""
    out = list(design.limitations)
    if design.stat == "v" and design.variance_groups is not None:
        _, sizes = np.unique(np.asarray(design.variance_groups).reshape(-1), return_counts=True)
        if sizes.size == 2 and (sizes.min() < 20 or sizes.max() > 1.5 * sizes.min()):
            out.append(
                f"Aspin-Welch v with variance groups of {int(sizes.min())} and {int(sizes.max())} subjects: its permutation test is only "
                "approximately exact when small or unbalanced groups have very different variances (in simulations of 8 vs 16 subjects "
                "with a 4x SD ratio, 7.5% voxel-wise false positives at alpha 0.05, against 17% for t)",
            )
    return out


def effect_kind(design):
    """"hedges_g" (group contrasts and the one-sample mean), "partial_r" (scores) or "partial_eta2" (F)."""
    if design.stat == "F":
        return "partial_eta2"
    if design.spec.test == "continuous":
        return "partial_r"
    return "hedges_g"


def hedges_correction(df):
    """Small-sample correction of Hedges g: J = 1 - 3 / (4 df - 1)."""
    return 1.0 - 3.0 / (4.0 * df - 1.0)


def effect_size(kind, result):
    """Effect-size map of a :class:`PermutationResult` (float64)."""
    df = result.df
    if kind == "partial_r":
        t = result.stat
        return t / np.sqrt(t * t + df)
    if kind == "partial_eta2":
        f = result.stat * result.df1
        return f / (f + df)
    if result.psi is None:
        raise ValueError("Hedges g needs a one-row contrast")
    sd = np.sqrt(result.sse / df)
    out = np.zeros_like(sd)
    np.divide(result.psi, sd, out=out, where=sd > 0)
    return out * hedges_correction(df)


def _camel(text):
    words = [word for word in re.split(r"[^A-Za-z0-9]+", str(text)) if word]
    if not words:
        return ""
    return words[0] + "".join(word[:1].upper() + word[1:] for word in words[1:])


def _upper_first(text):
    return text[:1].upper() + text[1:]


def contrast_label(design):
    """The "desc" label of a design's outputs (letters and digits): caseGtControl, caseVsControl, groupAnova, nihssPos, oneSample, ..."""
    spec = design.spec
    if spec.test in designlib.GROUP_TESTS:
        levels = [_camel(level) or f"level{index + 1}" for index, level in enumerate(design.levels)]
        if spec.contrast == "F":
            label = (_camel(spec.group_column) or "group") + "Anova"
        elif spec.contrast == "A>B":
            label = levels[0] + "Gt" + _upper_first(levels[1])
        elif spec.contrast == "B>A":
            label = levels[1] + "Gt" + _upper_first(levels[0])
        else:
            label = levels[0] + "Vs" + _upper_first(levels[1])
    else:
        base = (_camel(spec.score_column) or "score") if spec.test == "continuous" else "oneSample"
        label = base + {"positive": "Pos", "negative": "Neg"}.get(spec.contrast, "")
    label = re.sub(r"[^A-Za-z0-9]", "", label)
    return label or "contrast"


@dataclasses.dataclass
class GlmResult:
    """:func:`run` result: the permutation result, the effect size and what the design used."""

    design: object
    result: PermutationResult
    effect_kind: str
    effect: np.ndarray
    label: str
    exhaustive: bool
    seed: int
    n_perm_requested: int

    def __getattr__(self, name):
        # The permutation result's fields (stat, p_unc, p_fwe, ...) are reachable directly
        if name in ("result", "__setstate__"):
            raise AttributeError(name)
        return getattr(self.result, name)


def precision(exhaustive):
    """Default compute precision: float64 for an exhaustive enumeration (exact p; at most n_perm
    shufflings), float32 otherwise.
    """
    return np.dtype(np.float64 if exhaustive else np.float32)


def run(design, Y, n_perm=None, seed=None, mask=None, tfce=None, tfce_params=None, batch=None, dtype=None, cancel=None, on_batch=None, state=None, fdr_alpha=FDR_ALPHA, allow_blocked=False, engine=None, prepared=None):
    """Permutation GLM of a design (design.py) on maps in design order.

    :param design: ``design.Design`` (rows in ``design.sids`` order).
    :param Y: n x V maps in design order.
    :param n_perm, seed: override the spec's (see :func:`shufflings`).
    :param mask: 3D mask of the V voxels (needed for TFCE).
    :param tfce: add TFCE (default: the spec's ``inference.tfce``).
    :param dtype: compute precision of the permutations (default: :func:`precision` of the enumeration).
    :param allow_blocked: run although the design has blocking checks (tests only).
    :param engine, prepared: a :class:`FreedmanLane` of the design and its :meth:`FreedmanLane.prepare` result, to reuse.
    :return: :class:`GlmResult`.
    :raises ValueError: a blocked design, or a mask that does not hold V voxels.
    """
    if design.blocking and not allow_blocked:
        raise ValueError("The design is blocked: " + "; ".join(check.message for check in design.blocking))
    Y_shape = getattr(Y, "shape", None)
    if Y_shape is None or len(Y_shape) != 2 or Y_shape[0] != design.n:
        raise ValueError(f"Expected {design.n} maps in design order, got shape {Y_shape}")
    iterator, count, exhaustive = shufflings(design, n_perm, seed)
    if dtype is None:
        dtype = engine.dtype if engine is not None else precision(exhaustive)
    use_tfce = bool(design.spec.inference.get("tfce")) if tfce is None else bool(tfce)
    grid = None
    if use_tfce:
        if mask is None:
            raise ValueError("TFCE needs the mask of the voxels")
        grid = mask if isinstance(mask, tfcelib.TfceGrid) else tfcelib.TfceGrid(mask, (tfce_params or {}).get("connectivity", tfcelib.DEFAULT_CONNECTIVITY))
        if grid.n_voxels != Y_shape[1]:
            raise ValueError(f"The mask holds {grid.n_voxels} voxels but the maps have {Y_shape[1]}")
    result = permutation_test(
        Y,
        design.X,
        design.C,
        iterator,
        stat=design.stat,
        tails=design.tails,
        variance_groups=design.variance_groups,
        exchange=design.exchange,
        n_total=count,
        batch=batch,
        dtype=dtype,
        tfce_grid=grid,
        tfce_params=tfce_params,
        cancel=cancel,
        on_batch=on_batch,
        state=state,
        fdr_alpha=fdr_alpha,
        engine=engine,
        prepared=prepared,
    )
    kind = effect_kind(design)
    return GlmResult(
        design=design,
        result=result,
        effect_kind=kind,
        effect=effect_size(kind, result),
        label=contrast_label(design),
        exhaustive=exhaustive,
        seed=int(design.seed if seed is None else seed),
        n_perm_requested=int(design.spec.n_perm if n_perm is None else n_perm),
    )


# Files


def _write_on_mask(path, vector, record, indices, shape, fill=0.0, descrip="", intent_code=0, intent_params=(0.0, 0.0, 0.0)):
    """Write a masked vector as float32 with the mask's exact header (geometry, codes, units, byte order)."""
    header = record.copy()
    volume = np.full(int(np.prod(shape)), fill, dtype=np.float32)
    volume[indices] = np.asarray(vector, dtype=np.float32)
    volume = volume.reshape(shape)
    dim = [1] * 8
    dim[0] = 3
    dim[1:4] = shape
    header["dim"] = dim
    header["datatype"] = 16
    header["bitpix"] = 32
    header["vox_offset"] = nifti.HEADER_SIZE + 4
    header["scl_slope"] = 1.0
    header["scl_inter"] = 0.0
    header["cal_min"] = header["cal_max"] = 0.0
    header["glmin"] = header["glmax"] = 0
    header["intent_code"] = intent_code
    header["intent_p1"], header["intent_p2"], header["intent_p3"] = intent_params
    header["intent_name"] = b""
    header["descrip"] = descrip.encode("latin-1", errors="replace")[:80]
    header["aux_file"] = b""
    header["magic"] = b"n+1\0"
    byteorder = ">" if header.dtype["sizeof_hdr"].byteorder == ">" else "<"
    payload = volume.astype(np.dtype(np.float32).newbyteorder(byteorder), copy=False).tobytes(order="F")
    nifti._write_image(path, header, payload, 1)
    return path


def write_maps(glm_result, mask_path, out_dir, label, desc=None, fwe=True, fdr=True):
    """Write the group maps of a :class:`GlmResult` on the mask's exact header.

    :param label: connectome name (or label) of "group_conn-<label>".
    :param desc: contrast label (default: :func:`contrast_label`).
    :return: {key: path} with keys stat, p_unc, p_fwe, p_fdr, tfce_p_fwe and effect (as written).
    """
    result = glm_result.result
    data, _, header = nifti.read_nifti(mask_path)
    shape = tuple(header["shape"][:3])
    mask = np.asarray(data).reshape(shape) != 0
    indices = mask_indices(mask)
    if indices.size != result.n_voxels:
        raise ValueError(f"The mask {mask_path} holds {indices.size} voxels but the result has {result.n_voxels}")
    record = nifti.read_raw_header(mask_path)
    desc = desc or glm_result.label
    stat_name = "Fstat" if result.kind == "F" else "Tstat"
    df = float(result.df)
    stat_intent = (4, (float(result.df1), df, 0.0)) if result.kind == "F" else (3, (df, 0.0, 0.0)) if result.kind == "t" else (0, (0.0, 0.0, 0.0))
    stat_text = {"t": "t", "F": "F", "v": "Aspin-Welch v"}[result.kind]
    items = [("stat", stat_name, result.stat, 0.0, f"LNM GLM {stat_text} df={result.df}", stat_intent)]
    items.append(("p_unc", "PUnc", result.p_unc, 1.0, f"LNM GLM uncorrected p K={result.K}", (22, (0.0, 0.0, 0.0))))
    if fwe:
        items.append(("p_fwe", "PFwe", result.p_fwe, 1.0, f"LNM GLM max-stat FWE p K={result.K}", (22, (0.0, 0.0, 0.0))))
    if fdr:
        items.append(("p_fdr", "PFdr", result.q_fdr, 1.0, "LNM GLM BH-FDR q", (22, (0.0, 0.0, 0.0))))
    if result.tfce_p_fwe is not None:
        items.append(("tfce_p_fwe", "TfcePFwe", result.tfce_p_fwe, 1.0, f"LNM GLM TFCE FWE p K={result.K}", (22, (0.0, 0.0, 0.0))))
    effect_name = EFFECT_NAMES[glm_result.effect_kind]
    items.append(("effect", effect_name, glm_result.effect, 0.0, f"LNM GLM {effect_name}", (0, (0.0, 0.0, 0.0))))
    paths = {}
    for key, name, vector, fill, descrip, (intent_code, intent_params) in items:
        path = group_map_path(out_dir, label, desc + name)
        _write_on_mask(path, vector, record, indices, shape, fill, descrip, intent_code, intent_params)
        paths[key] = path
    return paths


def summary(glm_result, alpha=FWE_ALPHA):
    """JSON-compatible numbers of a :class:`GlmResult` for glm.json (inference, discreteness and effect sizes)."""
    result = glm_result.result
    K = result.K
    finite = result.stat[np.isfinite(result.stat)]
    at_floor = int(np.count_nonzero(result.p_unc <= result.min_p * (1 + 1e-9)))
    out = {
        "stat": result.kind,
        "tails": result.tails,
        "df": int(result.df),
        "df1": int(result.df1),
        "n_voxels": result.n_voxels,
        "n_perm": int(result.n_perm),
        "n_nonidentity": int(K),
        "min_p": float(result.min_p),
        "min_attainable_p": float(result.min_p),
        "stat_range": [float(finite.min()), float(finite.max())] if finite.size else None,
        "fwe": {
            "alpha": alpha,
            "n_significant": int(np.count_nonzero(result.p_fwe <= alpha)),
            "min_p": float(result.p_fwe.min()) if result.p_fwe.size else None,
            "critical_value": float(np.quantile(result.max_null, 1.0 - alpha)) if result.max_null.size else None,
        },
        "fdr": {
            "alpha": result.fdr_alpha,
            "floor_voxels": int(result.fdr_floor_voxels),
            "voxels_at_floor": at_floor,
            "min_attainable_q": float(result.min_p),
            "min_q": float(result.q_fdr.min()) if result.q_fdr.size else None,
            "n_significant": int(np.count_nonzero(result.q_fdr <= result.fdr_alpha)),
            "note": (
                f"The smallest attainable p is 1/(K+1) = {result.min_p:.3g} (K = {K}); BH-FDR reaches q <= {result.fdr_alpha:g} only when at least "
                f"{result.fdr_floor_voxels} voxels reach it. Use 10,000 permutations, or Export for PALM (tail acceleration), for smaller p-values."
            ),
        },
        "uncorrected": {"n_below_0_001": int(np.count_nonzero(result.p_unc <= 0.001))},
        "effect_size": {
            "name": EFFECT_NAMES[glm_result.effect_kind],
            "kind": glm_result.effect_kind,
            "formula": {
                "hedges_g": "adjusted mean difference / pooled residual SD x (1 - 3/(4 df - 1))",
                "partial_r": "t / sqrt(t^2 + df)",
                "partial_eta2": "F df1 / (F df1 + df)",
            }[glm_result.effect_kind],
            "range": [float(np.min(glm_result.effect)), float(np.max(glm_result.effect))] if glm_result.effect.size else None,
        },
    }
    if result.tfce_p_fwe is not None:
        out["tfce_fwe"] = {"n_significant": int(np.count_nonzero(result.tfce_p_fwe <= alpha)), "min_p": float(result.tfce_p_fwe.min()) if result.tfce_p_fwe.size else None}
    return out


# Command line


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _load_study(participants):
    """(Study, is a study folder): the study holding participants.tsv, or an in-memory study of the table."""
    from .study import PARTICIPANTS_JSON, Study, _parse_dictionary, read_tsv

    participants = os.path.abspath(os.fspath(participants))
    folder = os.path.dirname(participants)
    if os.path.basename(participants) == "participants.tsv" and Study.is_study(folder):
        return Study.open(folder), True
    columns, rows = read_tsv(participants)
    if "participant_id" not in columns:
        raise jobs.JobError(f"{participants} has no participant_id column", code="usage", exit_code=jobs.EXIT_USAGE)
    dictionary = {}
    dictionary_path = os.path.join(folder, PARTICIPANTS_JSON)
    if os.path.isfile(dictionary_path):
        try:
            with open(dictionary_path, "rb") as f:
                dictionary = _parse_dictionary(f.read(), dictionary_path)
        except (OSError, ValueError):
            dictionary = {}
    columns = ["participant_id"] + [column for column in columns if column != "participant_id"]
    metadata = {"format": "slicer-studio-study", "version": 1, "name": os.path.basename(folder) or "participants"}
    return Study(folder, metadata, columns, rows, dictionary), False


def _subset_study(study, sids, volumes):
    """An in-memory copy of a study with only the given subjects, with missing lesion volumes filled in."""
    from .study import Study, is_missing

    rows, filled = [], 0
    for row in study.subjects:
        if row["participant_id"] not in sids:
            continue
        volume = volumes.get(row["participant_id"])
        if is_missing(row.get(designlib.VOLUME_COLUMN)) and volume is not None:
            row[designlib.VOLUME_COLUMN] = volume
            filled += 1
        rows.append(row)
    subset = Study(study.path, dict(study.metadata), study.columns, rows, study.dictionary)
    return subset, filled


def _map_rows(maps_tsv, column):
    """{participant_id: (absolute map path, volume_ml text or None, compatibility key)} of a maps.tsv for one map column."""
    from .study import is_missing, read_tsv

    columns, rows = read_tsv(maps_tsv)
    if "participant_id" not in columns or column not in columns:
        raise jobs.JobError(f"{maps_tsv} needs the columns participant_id and {column}", code="usage", exit_code=jobs.EXIT_USAGE)
    folder = os.path.dirname(os.path.abspath(maps_tsv))
    out = {}
    for row in rows:
        path = row.get(column)
        if is_missing(path):
            continue
        path = path if os.path.isabs(path) else os.path.normpath(os.path.join(folder, path))
        volume = row.get(designlib.VOLUME_COLUMN)
        key = tuple(row.get(name, "n/a") for name in COMPATIBILITY_COLUMNS)
        out[row["participant_id"]] = (path, None if is_missing(volume) else volume, key)
    return out


def _pooling_checks(design, mapped, cohort):
    """Blocking checks of maps that must not be pooled: an unfinished cohort maps run, or rows of maps.tsv
    whose compatibility fields (connectome manifest, subject list, statistic, resampling, library) differ.
    """
    checks = []
    status = (cohort or {}).get("status")
    if cohort is not None and status not in (None, "done"):
        checks.append(designlib.Check("block", "not_poolable", f"The cohort maps run did not finish (status {status}); run it again", {"status": status}))
    keys = {}
    for sid in design.sids:
        keys.setdefault(mapped[sid][2], []).append(sid)
    if len(keys) > 1:
        differing = [name for index, name in enumerate(COMPATIBILITY_COLUMNS) if len({key[index] for key in keys}) > 1]
        checks.append(
            designlib.Check(
                "block",
                "not_poolable",
                f"The per-lesion maps cannot be pooled: maps.tsv rows differ in {', '.join(differing)}; map every lesion with the same connectome, subject list and settings",
                {"columns": differing},
            ),
        )
    return checks


def _load_maps(paths, mask_shape, mask_affine, indices, memmap_path, emit):
    """Masked maps (n x V float32 memmap) of the given files, in order."""
    Y = np.lib.format.open_memmap(memmap_path, mode="w+", dtype=np.float32, shape=(len(paths), indices.size))
    for row, path in enumerate(paths):
        try:
            data, affine, header = nifti.read_nifti(path)
        except (OSError, ValueError, EOFError) as exc:
            raise jobs.JobError(f"The map {path} cannot be read: {exc}", code="map_unreadable") from None
        data = np.asarray(data)
        if data.ndim > 3:
            data = data.reshape(data.shape[:3]) if int(np.prod(data.shape[3:])) == 1 else None
        if data is None or tuple(data.shape) != tuple(mask_shape) or not np.allclose(affine, mask_affine, atol=1e-4, rtol=0):
            raise jobs.JobError(f"The map {path} is not on the grid of the mask (shape {tuple(header['shape'])})", code="map_grid")
        Y[row] = np.asarray(data, dtype=np.float32).reshape(-1)[indices]
        if (row + 1) % 25 == 0 or row + 1 == len(paths):
            emit("progress", stage="maps", done=row + 1, total=len(paths))
    Y.flush()
    return Y


def _override_permutations(design, n_perm, count, exhaustive):
    """Make a design's permutation fields and checks describe a run with another n_perm (--perm below the
    spec's minimum, which the spec itself cannot hold): n_perm, exhaustive, min_p and their checks.
    """
    unit = "sign flips" if design.exchange == "sign_flip" else "permutations"
    design.n_perm, design.exhaustive, design.min_p = int(count), bool(exhaustive), 1.0 / max(int(count), 1)
    checks = []
    for check in design.checks:
        if check.code == "exhaustive":
            continue
        if check.code == "permutation_scheme":
            check = designlib.Check(check.level, check.code, check.message, {**(check.detail or {}), "n_perm": int(count), "exhaustive": bool(exhaustive), "min_p": design.min_p})
        checks.append(check)
    if exhaustive and design.unique_perms >= designlib.MIN_UNIQUE_PERMS:
        checks.append(
            designlib.Check(
                "warn",
                "exhaustive",
                f"All {design.unique_perms} unique {unit} are enumerated (exhaustive; fewer than the {n_perm} requested): the smallest attainable p is {design.min_p:.3g}",
                {"unique_perms": int(design.unique_perms)},
            ),
        )
    if n_perm < designlib.MIN_N_PERM and not exhaustive:
        checks.append(designlib.Check("warn", "n_perm_below_minimum", f"{n_perm} permutations is below the minimum of {designlib.MIN_N_PERM}: a preview, not a final result", {"n_perm": int(n_perm)}))
    design.checks = designlib._ordered(checks)


def _remove_previous_outputs(out_dir):
    previous = _read_json(os.path.join(out_dir, GLM_JSON))
    if previous:
        for name in list((previous.get("outputs") or {}).values()):
            path = os.path.join(out_dir, os.path.basename(str(name)))
            if os.path.isfile(path):
                os.remove(path)
    for name in (GLM_JSON,):
        path = os.path.join(out_dir, name)
        if os.path.exists(path):
            os.remove(path)


def _remove(*paths):
    for path in paths:
        if path and os.path.exists(path):
            os.remove(path)


def _parser():
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.glm", description="Voxelwise GLM with Freedman-Lane permutation inference (FWE, FDR, optional TFCE).")
    parser.add_argument("--maps", required=True, help="maps.tsv of a cohortmaps run (participant_id, AvgFz, T)")
    parser.add_argument("--participants", required=True, help="participants.tsv (of a study, or a table with participants.json)")
    parser.add_argument("--design", default=None, help="design.json (default: two groups, case vs control, volume covariate)")
    parser.add_argument("--mask", required=True, help="connectome mask (the maps' grid; outputs get its exact header)")
    parser.add_argument("--out", required=True, help="output folder")
    parser.add_argument("--perm", type=jobs.positive_int, default=None, help="permutations, identity included (default: the design's n_perm)")
    parser.add_argument("--seed", type=int, default=None, help="permutation seed (default: the design's)")
    parser.add_argument("--tfce", action="store_true", help="add TFCE (H=2, E=0.5, 100 steps, 26-connectivity); about 1.3-1.9 s per two-sided permutation on a 2 mm MNI mask")
    parser.add_argument("--batch", type=jobs.positive_int, default=None, help="permutations per GEMM (default: chosen from the memory of one batch)")
    parser.add_argument("--conn-label", default=None, help="connectome name of the output names (default: from cohortmaps.json next to maps.tsv)")
    parser.add_argument("--desc", default=None, help="contrast label of the output names (letters and digits; default: from the design)")
    parser.add_argument("--precision", choices=("auto", "float32", "float64"), default="auto", help="precision of the permutations (default auto: float64 for an exhaustive enumeration, float32 otherwise)")
    parser.add_argument("--keep-maps", action="store_true", help="keep the n x V maps memmap after the run")
    jobs.add_common_arguments(parser)
    return parser


def main(argv=None):
    """Command-line entry point (see the module docstring); returns the exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(argv)
    if args.seed is not None and args.seed < 0:
        raise jobs.JobError(f"--seed must not be negative, got {args.seed}", code="usage", exit_code=jobs.EXIT_USAGE)
    if args.desc is not None and not re.fullmatch(r"[A-Za-z0-9]+", args.desc):
        raise jobs.JobError(f"--desc must contain only letters and digits, got {args.desc!r}", code="usage", exit_code=jobs.EXIT_USAGE)
    return _run_cli(args, argv)


def _spec_for(args):
    """The design spec with the command-line overrides, and the permutation count to run."""
    try:
        spec = designlib.DesignSpec.load(args.design) if args.design else designlib.DesignSpec()
    except (OSError, ValueError) as exc:
        raise jobs.JobError(f"The design {args.design} cannot be read: {exc}", code="usage", exit_code=jobs.EXIT_USAGE) from None
    inference = dict(spec.inference)
    if args.seed is not None:
        inference["seed"] = args.seed
    if args.tfce:
        inference["tfce"] = True
    if args.perm is not None and args.perm >= designlib.MIN_N_PERM:
        inference["n_perm"] = args.perm
    spec = dataclasses.replace(spec, inference=inference)
    n_perm = int(args.perm) if args.perm is not None else spec.n_perm
    return spec, n_perm


def _run_cli(args, argv):
    emit = jobs.emit
    cancel = jobs.CancelToken(args.cancel_file)
    started = time.monotonic()
    timings = {}
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    lock = jobs.OutputLock(os.path.join(out_dir, LOCK_NAME))
    lock.acquire()
    run_root, run_id, record = None, None, None
    try:
        # Inputs and design
        study, in_study = _load_study(args.participants)
        spec, n_perm = _spec_for(args)
        column = MAP_COLUMNS[spec.input]
        maps_tsv = os.path.abspath(args.maps)
        mapped = _map_rows(maps_tsv, column)
        cohort_json = os.path.join(os.path.dirname(maps_tsv), "cohortmaps.json")
        cohort = _read_json(cohort_json)
        cohort_run = (cohort or {}).get("run_id")
        bundle = (cohort or {}).get("bundle") or {}
        label = args.conn_label or bundle.get("name") or os.path.basename(os.path.dirname(os.path.abspath(args.mask))) or "connectome"
        with_maps = {sid for sid in study.subject_ids if sid in mapped}
        no_map = [sid for sid in study.subject_ids if sid not in mapped]
        subset, filled = _subset_study(study, with_maps, {sid: values[1] for sid, values in mapped.items()})
        if not with_maps:
            raise jobs.JobError(f"No subject of {args.participants} has a map in {maps_tsv}", code="design_blocked", exit_code=jobs.EXIT_USAGE)
        try:
            design = designlib.build_design(subset, spec)
        except designlib.DesignError as exc:
            raise jobs.JobError(f"The design cannot be built: {exc}", code="design_blocked", exit_code=jobs.EXIT_USAGE) from None
        pooling = _pooling_checks(design, mapped, cohort)
        if pooling:
            design.checks = designlib._ordered([*pooling, *design.checks])
        if no_map:
            design.excluded[EXCLUDED_NO_MAP] = list(no_map)
            design.checks = designlib._ordered([*design.checks, designlib.Check("info", "no_map", f"{len(no_map)} subject(s) have no map in maps.tsv and are excluded", {"sids": no_map})])
        count, exhaustive = shuffling_count(design, n_perm)
        if n_perm != design.spec.n_perm:
            _override_permutations(design, n_perm, count, exhaustive)
        if design.blocking:
            messages = [check.message for check in design.blocking]
            raise jobs.JobError("The design is blocked: " + " | ".join(messages), code="design_blocked", exit_code=jobs.EXIT_USAGE, checks=[check.to_dict() for check in design.blocking])
        for check in design.warnings:
            emit("warning", code=check.code, message=check.message)
        two_sided = design.tails == 2 and design.stat != "F"

        # Mask and run registration
        mask_data, mask_affine, mask_header = nifti.read_nifti(args.mask)
        mask_shape = tuple(mask_header["shape"][:3])
        mask = np.asarray(mask_data).reshape(mask_shape) != 0
        indices = mask_indices(mask)
        n_voxels = int(indices.size)
        mask_sha = file_sha256(args.mask)
        dtype = precision(exhaustive) if args.precision == "auto" else np.dtype(args.precision)
        grid = tfcelib.TfceGrid(mask) if spec.inference.get("tfce") else None
        tfce_eta = tfcelib.tfce_eta(grid, count, two_sided) if grid is not None else None
        map_paths = [mapped[sid][0] for sid in design.sids]
        maps_key = []
        for sid, path in zip(design.sids, map_paths, strict=True):
            try:
                info = os.stat(path)
            except OSError:
                raise jobs.JobError(f"The map of {sid} is missing: {path}", code="map_missing") from None
            maps_key.append([sid, path, info.st_size, info.st_mtime_ns])
        params = {
            "design": runs.params_sha256(design.to_dict(matrix=True)),
            "n_perm": n_perm,
            "seed": spec.seed,
            "count": count,
            "maps": runs.params_sha256(maps_key),
            "mask_sha256": mask_sha,
            "precision": dtype.name,
            "tfce": bool(grid is not None),
        }
        run_root = os.path.abspath(args.run_root or (study.path if in_study else out_dir))
        parents = [run for run in [cohort_run, *args.parent_run] if run]
        run_id = runs.register(run_root, "glm", out_dir, parents=parents, params={**params, "label": label, "test": spec.test, "contrast": spec.contrast}, seeds={"permutation": spec.seed})
        start_time = now_iso()
        record = {
            "format": FORMAT_NAME,
            "version": FORMAT_VERSION,
            "run_id": run_id,
            "status": "running",
            "argv": list(argv),
            "start": start_time,
            "end": None,
        }
        emit(
            "start",
            job="glm",
            argv=list(argv),
            threads=args.threads,
            run_id=run_id,
            n=design.n,
            p=design.p,
            n_voxels=n_voxels,
            stat=design.stat,
            tails=design.tails,
            design_type=design.design_type,
            permute=design.permute,
            permute_reason=design.permute_reason,
            n_perm=count,
            exhaustive=exhaustive,
            tfce=grid is not None,
            tfce_eta_s=tfce_eta,
        )

        # Resume state and maps
        state_file = jobs.ResumeState(os.path.join(out_dir, RESUME_NAME))
        arrays_path = os.path.join(out_dir, RESUME_ARRAYS)
        memmap_path = os.path.join(out_dir, MAPS_MEMMAP)
        null_state = None
        if not args.resume:
            state_file.clear()
            _remove(arrays_path)
        elif state_file.data.get("params") != params:
            if state_file.data:
                emit("warning", code="resume_mismatch", message="The saved progress belongs to other inputs or settings; the permutations start again.")
            state_file.clear()
            _remove(arrays_path)
        else:
            null_state = NullState.load(arrays_path)
            if null_state is not None and (null_state.counts is None or null_state.counts.size != n_voxels):
                null_state = None
        maps_ready = bool(args.resume and state_file.data.get("maps_loaded") and os.path.exists(memmap_path))
        _remove_previous_outputs(out_dir)
        state_file.data = {"params": params, "maps_loaded": False}
        state_file.save()
        emit("stage", name="maps", index=1, total=4)
        tic = time.monotonic()
        if maps_ready:
            Y = np.load(memmap_path, mmap_mode="r")
            if Y.shape != (design.n, n_voxels) or Y.dtype != np.float32:
                maps_ready = False
        if not maps_ready:
            Y = _load_maps(map_paths, mask_shape, mask_affine, indices, memmap_path, emit)
        state_file.data["maps_loaded"] = True
        state_file.save()
        map_checks = designlib.map_checks(design, Y)
        blocking = [check for check in map_checks if check.level == "block"]
        if blocking:
            raise jobs.JobError("The maps cannot be analysed: " + " | ".join(check.message for check in blocking), code="design_blocked", exit_code=jobs.EXIT_USAGE, checks=[check.to_dict() for check in blocking])
        for check in map_checks:
            if check.level == "warn":
                emit("warning", code=check.code, message=check.message)
        design.checks = designlib._ordered(design.checks + map_checks)
        timings["load_s"] = time.monotonic() - tic

        # Permutations
        emit("stage", name="fit", index=2, total=4)
        tic = time.monotonic()
        engine = FreedmanLane(design.X, design.C, design.stat, design.variance_groups, design.exchange, dtype)
        prepared = engine.prepare(Y)
        timings["prepare_s"] = time.monotonic() - tic
        resumed = 0 if null_state is None else max(null_state.cursor - 1, 0)
        emit("stage", name="permutations", index=3, total=4, resumed=resumed, total_permutations=count - 1)
        session = {"started": time.monotonic(), "first": resumed}

        def on_batch(state, done, total, elapsed):
            state.save(arrays_path)
            state_file.data["cursor"] = state.cursor
            state_file.save()
            spent = time.monotonic() - session["started"]
            new = done - session["first"]
            eta = spent / new * max((total or done) - done, 0) if new > 0 else None
            emit("progress", stage="permutations", done=done, total=total, elapsed=round(spent, 3), eta=None if eta is None else round(eta, 3))

        glm_result = run(
            design,
            Y,
            n_perm=n_perm,
            seed=spec.seed,
            mask=grid,
            tfce=grid is not None,
            batch=args.batch,
            dtype=dtype,
            cancel=cancel,
            on_batch=on_batch,
            state=null_state,
            engine=engine,
            prepared=prepared,
        )
        del prepared  # frees Yr before the outputs are written
        if glm_result.result.n_perm != count:
            raise RuntimeError(f"Expected {count} shufflings, the design yielded {glm_result.result.n_perm}")
        result = glm_result.result
        timings["permutations_s"] = result.timings.get("permutations_s")
        timings["per_permutation_ms"] = result.timings.get("per_permutation_ms")
        timings["tfce_s"] = result.timings.get("tfce_permutations_s")

        # Outputs
        emit("stage", name="write", index=4, total=4)
        tic = time.monotonic()
        desc = args.desc or glm_result.label
        paths = write_maps(glm_result, args.mask, out_dir, label, desc, fwe=bool(spec.inference.get("fwe", True)), fdr=bool(spec.inference.get("fdr", True)))
        design_json, design_tsv = designlib.save_design(design, out_dir)
        timings["write_s"] = time.monotonic() - tic
        timings["total_s"] = time.monotonic() - started
        numbers = summary(glm_result)
        record.update({
            "status": "done",
            "end": now_iso(),
            "elapsed": round(time.monotonic() - started, 3),
            "library_version": LIBRARY_VERSION,
            "versions": versions(),
            "threads": args.threads,
            "batch": result.timings.get("batch"),
            "precision": result.precision,
            "inputs": {
                "maps_tsv": maps_tsv,
                "map_column": column,
                "input": spec.input,
                "participants": os.path.abspath(args.participants),
                "design": os.path.abspath(args.design) if args.design else None,
                "mask": os.path.abspath(args.mask),
                "mask_sha256": mask_sha,
                "cohortmaps_json": cohort_json if cohort else None,
                "cohortmaps_run": cohort_run,
            },
            "connectome": {"label": connectome_label(label), "name": label, "bundle": bundle or None},
            "test": spec.test,
            "design_type": design.design_type,
            "contrast": {"label": desc, "spec": spec.contrast, "names": list(design.contrast_names), "matrix": design.C.tolist(), "columns": list(design.columns)},
            "n": design.n,
            "n_per_level": dict(design.n_per_level),
            "n_voxels": n_voxels,
            "excluded": {reason: list(sids) for reason, sids in design.excluded.items()},
            "n_excluded": sum(len(sids) for sids in design.excluded.values()),
            "covariates": list(spec.covariates),
            "transforms": dict(spec.transforms),
            "volume_off_reason": spec.volume_off_reason,
            "volume_filled_from_maps": filled,
            "permutation": {
                "method": "Freedman-Lane (Winkler et al. 2014) with Beckmann partitioning",
                "scheme": design.permute,
                "reason": design.permute_reason,
                "exchange": design.exchange,
                "block_column": design.block_column,
                "n_perm": int(result.n_perm),
                "n_perm_requested": int(n_perm),
                "unique_perms": designlib._count_json(design.unique_perms),
                "unique_perms_log10": designlib._log10(design.unique_perms),
                "exhaustive": bool(exhaustive),
                "seed": int(spec.seed),
                "preview": n_perm < designlib.MIN_N_PERM and not exhaustive,
                "resumed_from": resumed or None,
            },
            "seeds": {"permutation": int(spec.seed)},
            **numbers,
            "fwe": {**numbers["fwe"], "enabled": bool(spec.inference.get("fwe", True)), "primary": True},
            "fdr": {**numbers["fdr"], "enabled": bool(spec.inference.get("fdr", True))},
            "tfce": {
                "enabled": grid is not None,
                "H": tfcelib.DEFAULT_H,
                "E": tfcelib.DEFAULT_E,
                "steps": tfcelib.DEFAULT_STEPS,
                "connectivity": tfcelib.DEFAULT_CONNECTIVITY,
                "two_sided": two_sided,
                "eta_s": tfce_eta,
                **(numbers.get("tfce_fwe") or {}),
            },
            "cluster_inference": None,
            "limitations": limitations(design),
            "checks": [check.to_dict() for check in design.checks],
            "warnings": [check.message for check in design.warnings],
            "outputs": {key: os.path.basename(path) for key, path in paths.items()},
            "design_json": os.path.basename(design_json),
            "design_matrix": os.path.basename(design_tsv),
            "timings": {key: (None if value is None else round(float(value), 4)) for key, value in timings.items()},
            "disclaimer": DISCLAIMER,
        })
        record.pop("tfce_fwe", None)
        json_path = os.path.join(out_dir, GLM_JSON)
        jobs.write_json_atomic(json_path, record)
        for path in [*paths.values(), design_json, design_tsv, json_path]:
            emit("result", kind="glm", path=path)
        state_file.clear()
        _remove(arrays_path)
        del Y
        if not args.keep_maps:
            _remove(memmap_path)
        if run_id:
            runs.update(
                run_root,
                run_id,
                status="done",
                ended=record["end"],
                outputs=[os.path.basename(path) for path in [*paths.values(), json_path, design_json, design_tsv]],
                summary={"n": design.n, "n_perm": int(result.n_perm), "fwe_significant": numbers["fwe"]["n_significant"], "min_p": result.min_p, "design_type": design.design_type},
            )
        emit("done", outputs=[*paths.values(), json_path, design_json, design_tsv], elapsed=round(time.monotonic() - started, 3), run_id=run_id, n_perm=int(result.n_perm), per_permutation_ms=timings["per_permutation_ms"])
        return jobs.EXIT_OK
    except jobs.Cancelled as exc:
        if record is not None:
            record.update({"status": "cancelled", "end": now_iso(), "stop_reason": exc.reason, "permutations_done": exc.done})
            jobs.write_json_atomic(os.path.join(out_dir, GLM_JSON), record)
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
        lock.release()


if __name__ == "__main__":
    jobs.run_job(main, job="glm")

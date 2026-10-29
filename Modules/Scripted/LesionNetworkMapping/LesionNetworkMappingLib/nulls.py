"""Cohort null suite (addendum §3.3, §3.6): null-referenced sensitivity, the classic rule's false-positive rate and bias map.

The classic overlap map (the share of a cohort's lesion maps that pass a threshold rule, shown at >= 75/90/95/100 %)
is hub-inflated (Cui 2026): lesions anywhere give maps that share the connectome's degree pattern. This module puts
it next to a null-referenced version computed from null cohorts, sets of lesions that keep some property of the
observed cohort but not its locations:

==================  ============================================================  =========================================
kind                null cohort                                                   status
==================  ============================================================  =========================================
rl_independent      each lesion rigidly relocated, independently (qa.relocated_   established (Zalesky & Cash 2026; vdH)
                    lesions): keeps every lesion's shape and weights, destroys
                    the overlap between lesions
rl_cohort_shift     one shared integer shift for the whole cohort: keeps the      proposal (this addendum)
                    shapes and the pairwise Dice exactly
rl_strength         each centroid moved to one of the 100 brain voxels whose      2026 (Zalesky & Cash)
                    signed μC_r (or, with ``strength="abs"``, hubness) is
                    nearest the lesion's own
rl_overlap          simulated annealing of the centroids so that the pairwise-    single preprint (Cui 2026), experimental;
                    Dice histogram of the null cohort matches the observed one    fast mode only
empirical_pool      for each lesion a volume-matched lesion of a control set      established practice (cached exact maps)
==================  ============================================================  =========================================

Two modes, always labelled in every output:

- **exact** (``mode="exact"``, recorded as "exact_pooled"): K_pool relocations per lesion (default 20) are mapped
  exactly, at every connectome voxel, as extra seed columns of engine passes over the cohort's own connectome
  subjects (``engine.compute_network_maps(null_weights=...)``, batched by the cohortmaps memory budget). Each
  relocation map is thresholded with the same rule as the observed maps and kept only as a bit-packed binary map.
  A null cohort draws one relocation per lesion at random; its overlap is the sum of the drawn binary maps. K null
  cohorts (default 1000) are labelled "pooled exact null (K_pool per lesion)". The cohort shift has no pooling
  (its null cohorts are the K_pool shifts themselves) and the empirical pool draws the cached exact maps of the
  control lesions. The observed maps are the cohortmaps maps of the same engine and subjects; the observed lesions
  are also mapped in the null passes, and the agreement is recorded ("observed_check").
- **fast** (``mode="fast"``, recorded as "approximate"): relocations and observed lesions are mapped with the fast
  parcel index (fastmap.py; approximate z-scale), so K can reach 10,000. Only rank rules (top x %) apply, the
  observed map is recomputed in fast mode (like with like), and the parity gate of the index decides which lesions
  may take part (a parity.json, or the parity of the cohort's own exact maps computed here); its numbers are
  attached. Fast-mode maps are never final observed maps.

Statistics (per voxel v; O = observed overlap count, O_b = overlap of null cohort b, b = 1..K; for sign "both" the
count is the number of maps with |map| >= t, as overlapCount)::

    sensNullExcess  100 * (O - mean_b O_b) / L                     percentage points of the L lesions
    sensNullP       (1 + #{b: O_b >= O}) / (K + 1)                 one-sided; floor 1 / (K + 1)
    sensNullQ       Benjamini-Hochberg q of sensNullP over the V voxels
    sensNullPFwe    (1 + #{b: max_v Z_b >= Z(v)}) / (K + 1)        max statistic of the standardised excess
                    Z = (O - mean_b O_b) / max(sd_b O_b, 0.5)
    sensNullBias    #{b: the null cohort passes the classic rule at v} / K

The classic rule passes where at least ``overlap`` % of the maps pass (sign-specific for "both", as the sens<k>
map of cohort.py). Its false-positive rate for this cohort is reported as, e.g., "≥90% at the rule: 31% of brain
voxels pass; random sets: 28% (5-95%: 22-35%)" (with en dashes). The group QA percentile is the percentile of r(group map, μC_r)
among the null cohorts' group maps (qa.group_qa), for the overlap map and for the mean map of the rule's statistic
(the null cohorts' mean maps on a voxel sample are saved for other group maps of the same statistic).

Every output (maps, their .json sidecars, nulls.json) says whether it is exact-pooled or approximate, states the
minimum attainable p and q, and carries the permanent caption :data:`CAPTION` (addendum §3.6). Shared files hold
no absolute local path.

Command line (JSON events, see ``jobs``; exit codes 0 done, 1 error, 2 usage, 3 cancelled, 75 locked)::

    python -m LesionNetworkMappingLib.nulls (--maps maps.tsv | --study DIR) --bundle B --out DIR
        [--kind rl_independent|rl_cohort_shift|rl_strength|rl_overlap|empirical_pool] [--mode exact|fast]
        [--k 1000] [--k-pool 20] [--rule JSON] [--sign both|pos|neg] [--overlap 90] [--seed 0]
        [--participants participants.tsv [--group-column group]] [--strength signed|abs] [--iterations 2000]
        [--pool DIR|TSV | --pool-maps maps.tsv] [--pool-matches 10] [--index DIR] [--parity parity.json]
        [--brain-mask MASK] [--min-inside 0.95] [--budget-gb G] [--cache DIR] [--include-unreviewed]
        [--qa-voxels 30000] [--estimate] [--threads K] [--cancel-file F] [--resume] [--run-root DIR] [--parent-run ID]

The run is registered (kind "nulls") in ``<run root>/derivatives/lnm/runs.jsonl`` with the cohortmaps run (and the
parity run) as parents; the run root is ``--run-root``, else the study, else the output folder. :func:`estimate`
predicts the run time (A-04 runs the exact null only when it is short enough) and :func:`find_fast_index` finds the
fast index of a bundle and subject list.
"""

import os
import sys

if __name__ == "__main__":
    from LesionNetworkMappingLib.jobs import set_blas_threads

    set_blas_threads(sys.argv[1:])  # before numpy is imported: BLAS reads these variables once

import json
import math
import shutil
import time

import numpy as np

from . import jobs, runs, thresholds

FORMAT_NAME = "slicer-lnm-nulls"
SIDECAR_FORMAT = "slicer-lnm-nulls-map"
FORMAT_VERSION = 1
NULLS_JSON = "nulls.json"
SETS_NAME = "nullsets.npz"  # the null design: relocation shifts and the columns each null cohort drew
NULL_GROUP_FILE = "null_group_mean.f16.npy"  # (K, V') mean statistic map of each null cohort on a voxel sample
NULL_GROUP_VOXELS = "null_group_voxels.npy"  # (V',) indices of that sample into the connectome voxels
PARITY_JSON = "parity.json"
LOCK_NAME = ".nulls.lock"
WORK_DIR = ".nulls-work"
RESUME_NAME = "resume.json"

KINDS = ("rl_independent", "rl_cohort_shift", "rl_strength", "rl_overlap", "empirical_pool")
MODES = ("exact", "fast")
MODE_RECORDS = {"exact": "exact_pooled", "fast": "approximate"}
KIND_INFO = {
    "rl_independent": {
        "description": "each lesion rigidly relocated to a random place in the brain, independently of the others",
        "preserves": "shape and weights of each lesion; destroys the overlap between lesions",
        "status": "established",
        "citation": "Zalesky & Cash 2026; van den Heuvel et al. 2026",
        "modes": MODES,
    },
    "rl_cohort_shift": {
        "description": "one shared rigid shift of the whole cohort",
        "preserves": "shapes and the pairwise overlap (Dice) of the lesions",
        "status": "proposal",
        "citation": "SPEC_LNM_ADDENDUM §3.6 (proposal)",
        "modes": MODES,
    },
    "rl_strength": {
        "description": "each lesion's centroid moved to one of the 100 brain voxels with the nearest signed mean connectivity (μC_r)",
        "preserves": "local connectivity strength of each lesion",
        "status": "2026 (Zalesky & Cash)",
        "citation": "Zalesky & Cash 2026",
        "modes": MODES,
    },
    "rl_overlap": {
        "description": "lesion centroids placed by simulated annealing so that the pairwise-Dice histogram matches the observed one",
        "preserves": "size, shape and the distribution of pairwise overlap",
        "status": "single preprint (Cui 2026), experimental",
        "citation": "Cui et al. 2026 (preprint)",
        "modes": ("fast",),
    },
    "empirical_pool": {
        "description": "for each lesion, a volume-matched lesion drawn from a control set",
        "preserves": "realistic lesion shapes and volumes",
        "status": "established practice",
        "citation": "Edelman et al. 2026 (suggestion)",
        "modes": MODES,
    },
}
# Permanent caption of every null output (addendum §3.6, exact text)
CAPTION = (
    "Null models benchmark significance; they do not by themselves establish disease specificity "
    "(van den Heuvel et al. 2026b); see also Zalesky & Cash 2026; Siddiqi et al. 2026."
)
APPROX_LABEL = "approximate (fast parcel mode, approximate z-scale; rank rules only)"
OUTPUT_DESCS = ("sensNullExcess", "sensNullP", "sensNullQ", "sensNullPFwe", "sensNullBias")
DESCRIPTIONS = {
    "sensNullExcess": "overlap excess over the null cohorts (percentage points of the lesions)",
    "sensNullP": "one-sided permutation p of the overlap against the null cohorts, (1 + #{null >= observed}) / (K + 1)",
    "sensNullQ": "Benjamini-Hochberg q of sensNullP over the connectome voxels",
    "sensNullPFwe": "family-wise p of the standardised overlap excess (max statistic over the voxels)",
    "sensNullBias": "fraction of the null cohorts that pass the classic overlap rule (bias map)",
}
DEFAULT_K = 1000
MAX_K_FAST = 10_000
DEFAULT_K_POOL = 20
DEFAULT_K_POOL_FAST = 100
DEFAULT_OVERLAP = 90.0
OVERLAP_RANGE = (50.0, 100.0)
STRENGTH_CANDIDATES = 100
STRENGTH_MODES = ("signed", "abs")
STRENGTH_MAPS = {"signed": "mu_r", "abs": "hub"}
COHORT_SHIFT_ATTEMPTS = 1000  # random shared shifts tried per shift wanted
ANNEAL_ITERATIONS = 2000
ANNEAL_T0, ANNEAL_T1 = 0.05, 5e-4  # temperatures of the geometric cooling (KS distance units)
POOL_MATCHES = 10
SD_FLOOR = 0.5  # lesion counts: floor of the null standard deviation in the standardised excess
ALPHA = 0.05
QA_VOXELS = 30_000  # voxels of the group QA sample (engine.NULL_MAX_VOXELS)
DICE_BINARIZE_AT = 0.5  # lesionset.BINARIZE_AT

# Run-time model (estimate): engine cost per subject at 2 BLAS threads for V * T = 292,019 * 216 (addendum §8, A-04 E2)
ENGINE_SUBJECT_S = 0.13
ENGINE_COLUMN_S = 0.0042
ENGINE_REFERENCE_SIZE = 292_019 * 216
READ_RATE = 400e6  # bytes per second, floor of one pass over the connectome
SET_VOXEL_S = 1.3e-9  # unpacking and summing one lesion's binary map per voxel, per null cohort
COHORT_VOXEL_S = 2.5e-8  # statistics of one null cohort's overlap map (accumulation and max statistic), per voxel
COHORT_OVERHEAD_S = 1.0e-3  # fixed cost of one null cohort
RUN_OVERHEAD_S = 1.0  # imports, reading the observed maps, writing the outputs
RULE_VOXEL_S = {"top_pct": 1.6e-8, "other": 3.0e-9}  # thresholding one map (top_pct: a percentile per map), per voxel
FAST_MAP_VOXEL_S = 6.0e-9  # one approximate map (a few parcel rows), per voxel
ANNEAL_SET_S = 0.15  # one annealed null cohort (2000 iterations)

runs.register_kind("nulls")


def _now_iso():
    from .export import now_iso

    return now_iso()


def lesion_seed(seed, index, stream=1):
    """The random seed of lesion ``index`` (stream 1: relocations), derived from the run seed (independent streams)."""
    return int(np.random.SeedSequence([int(seed), int(stream), int(index)]).generate_state(1)[0])


def _rng(seed, stream, *extra):
    return np.random.default_rng(np.random.SeedSequence([int(seed), int(stream), *[int(value) for value in extra]]))


def _sparse_columns(weights, n_voxels):
    """Lesions as a list of (positions int64, weights float32) over the connectome voxels (sorted, weights > 0).

    :param weights: an (L, V) matrix, a (V,) vector, or a list of (V,) vectors or (positions, weights) pairs.
    """
    if isinstance(weights, np.ndarray) and weights.ndim in (1, 2):
        rows = weights[np.newaxis] if weights.ndim == 1 else weights
        items = list(rows)
    else:
        items = list(weights)
    out = []
    for item in items:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            positions = np.asarray(item[0], dtype=np.int64).ravel()
            values = np.asarray(item[1], dtype=np.float32).ravel()
        else:
            dense = np.asarray(item, dtype=np.float32).ravel()
            if dense.size != n_voxels:
                raise ValueError(f"A lesion has {dense.size} weights, the connectome has {n_voxels} voxels")
            positions = np.flatnonzero(dense > 0)
            values = dense[positions]
        keep = values > 0
        positions, values = positions[keep], values[keep]
        order = np.argsort(positions, kind="stable")
        out.append((positions[order], values[order]))
    return out


def _dense(column, n_voxels):
    out = np.zeros(n_voxels, dtype=np.float32)
    out[column[0]] = column[1]
    return out


#
# Relocation
#


class Relocator:
    """Rigid integer-voxel shifts of lesions on the connectome grid (the geometry of the relocation nulls).

    A lesion shifted by ``s`` keeps its shape and weights; weight landing outside the connectome mask is dropped. A
    shift is valid when at least ``min_inside`` of the lesion's weight lands on connectome voxels inside the brain
    mask (``qa.relocated_lesions``'s rule). ``centroid[l]`` is the rounded weighted centroid of lesion l (voxels).
    """

    def __init__(self, columns, bundle_or_grid, mask_indices=None, brain_mask=None, min_inside=None):
        from .qa import MIN_INSIDE, _grid_and_indices

        shape, indices = _grid_and_indices(bundle_or_grid, mask_indices)
        self.shape = shape
        self.indices = indices
        self.n_voxels = int(indices.size)
        self.min_inside = MIN_INSIDE if min_inside is None else float(min_inside)
        self.in_brain = np.ones(self.n_voxels, dtype=bool) if brain_mask is None else np.asarray(brain_mask, dtype=bool).ravel()
        if self.in_brain.size != self.n_voxels:
            raise ValueError(f"The brain mask has {self.in_brain.size} values, the connectome {self.n_voxels} voxels")
        self.position = np.full(int(np.prod(shape)), -1, dtype=np.int64)
        self.position[indices] = np.arange(self.n_voxels)
        self.brain_positions = np.flatnonzero(self.in_brain)
        self.brain_ijk = np.stack(np.unravel_index(indices[self.brain_positions], shape), axis=1)
        self._shape_array = np.array(shape, dtype=np.int64)
        self.columns = _sparse_columns(columns, self.n_voxels)
        self.ijk, self.values, self.total, self.centroid, self.radius = [], [], [], [], []
        for positions, weights in self.columns:
            ijk = np.stack(np.unravel_index(indices[positions], shape), axis=1) if positions.size else np.zeros((0, 3), np.int64)
            values = weights.astype(np.float64)
            total = float(values.sum())
            centroid = np.round((ijk * values[:, np.newaxis]).sum(axis=0) / total).astype(np.int64) if total > 0 else np.zeros(3, np.int64)
            self.ijk.append(ijk)
            self.values.append(values)
            self.total.append(total)
            self.centroid.append(centroid)
            self.radius.append(int(np.ceil(np.sqrt(((ijk - centroid) ** 2).sum(axis=1).max()))) if ijk.size else 0)
        self.centroid = np.array(self.centroid, dtype=np.int64).reshape(-1, 3)

    @property
    def n_lesions(self):
        return len(self.columns)

    def targets(self, lesion, shifts):
        """(b, m) connectome positions of the m voxels of ``lesion`` moved by each of b shifts (-1: outside the mask)."""
        shifts = np.atleast_2d(np.asarray(shifts, dtype=np.int64))
        moved = self.ijk[lesion][np.newaxis] + shifts[:, np.newaxis]
        inside = np.all((moved >= 0) & (moved < self._shape_array), axis=2)
        flat = np.ravel_multi_index(tuple(np.where(inside[..., np.newaxis], moved, 0).transpose(2, 0, 1)), self.shape)
        return np.where(inside, self.position[flat], -1)

    def inside_fraction(self, lesion, shifts):
        """(b,) fraction of the lesion's weight that each shift puts on connectome voxels inside the brain mask."""
        shifts = np.atleast_2d(np.asarray(shifts, dtype=np.int64))
        total = self.total[lesion]
        if total <= 0 or shifts.shape[0] == 0:
            return np.zeros(shifts.shape[0])
        out = np.empty(shifts.shape[0])
        step = max(1, 2_000_000 // max(self.ijk[lesion].shape[0], 1))
        for start in range(0, shifts.shape[0], step):
            target = self.targets(lesion, shifts[start:start + step])
            landed = np.where(target >= 0, self.in_brain[np.maximum(target, 0)], False)
            out[start:start + step] = (landed * self.values[lesion]).sum(axis=1) / total
        return out

    def valid(self, lesion, shifts):
        return self.inside_fraction(lesion, shifts) >= self.min_inside * (1.0 - 1e-12)

    def column(self, lesion, shift):
        """(positions, weights) of ``lesion`` moved by ``shift`` (sorted connectome positions; lost voxels dropped)."""
        target = self.targets(lesion, shift)[0]
        landed = target >= 0
        order = np.argsort(target[landed], kind="stable")
        return target[landed][order], self.columns[lesion][1][landed][order]

    def random_targets(self, rng, size):
        """``size`` brain voxels (ijk) drawn at random (with repetition)."""
        return self.brain_ijk[rng.integers(0, len(self.brain_ijk), size=int(size))]


def relocate_independent(relocator, k, seed=0):
    """``rl_independent``: k rigid relocations of each lesion, independently (``qa.relocated_lesions``, one seed per lesion).

    :return: (columns, shifts): per lesion a list of (positions, weights) and a (k_l, 3) array of shifts (k_l <= k).
    """
    from .qa import relocated_lesions

    columns, shifts = [], []
    for lesion, column in enumerate(relocator.columns):
        moved, lesion_shifts = relocated_lesions(_dense(column, relocator.n_voxels), relocator.shape, mask_indices=relocator.indices, brain_mask=relocator.in_brain,
                                                 k=k, seed=lesion_seed(seed, lesion), min_inside=relocator.min_inside, return_shifts=True, sparse=True)
        columns.append([(np.asarray(p, np.int64), np.asarray(w, np.float32)) for p, w in moved])
        shifts.append(lesion_shifts)
    return columns, shifts


def lesion_strength(relocator, strength):
    """Weighted mean of a strength map (μC_r, or hubness) over each lesion's voxels (None for an empty lesion)."""
    strength = np.asarray(strength, dtype=np.float64).ravel()
    out = []
    for positions, weights in relocator.columns:
        values = strength[positions]
        finite = np.isfinite(values)
        total = float(weights[finite].sum())
        out.append(float((values[finite] * weights[finite]).sum() / total) if total > 0 else None)
    return out


def strength_candidates(relocator, strength, n_candidates=STRENGTH_CANDIDATES):
    """Per lesion, the connectome positions of the ``n_candidates`` brain voxels whose strength is nearest the lesion's.

    Ties are broken by voxel order. Voxels with a non-finite strength are never candidates.
    """
    strength = np.asarray(strength, dtype=np.float64).ravel()
    if strength.size != relocator.n_voxels:
        raise ValueError(f"The strength map has {strength.size} voxels, the connectome {relocator.n_voxels}")
    pool = relocator.brain_positions[np.isfinite(strength[relocator.brain_positions])]
    out = []
    for value in lesion_strength(relocator, strength):
        if value is None or pool.size == 0:
            out.append(np.zeros(0, dtype=np.int64))
            continue
        order = np.argsort(np.abs(strength[pool] - value), kind="stable")[:int(n_candidates)]
        out.append(pool[order])
    return out


def relocate_strength(relocator, strength, k, seed=0, n_candidates=STRENGTH_CANDIDATES):
    """``rl_strength``: each lesion's centroid moved onto one of its strength-matched candidate voxels (valid shifts only).

    :param strength: (V,) strength map: μC_r (signed) or the hubness map (absolute).
    :return: (columns, shifts, candidates) per lesion; candidates are connectome positions.
    """
    candidates = strength_candidates(relocator, strength, n_candidates)
    columns, shifts = [], []
    for lesion, candidate in enumerate(candidates):
        rng = np.random.default_rng(lesion_seed(seed, lesion, stream=4))
        if candidate.size == 0 or relocator.total[lesion] <= 0:
            columns.append([])
            shifts.append(np.zeros((0, 3), np.int64))
            continue
        ijk = np.stack(np.unravel_index(relocator.indices[candidate], relocator.shape), axis=1)
        lesion_shifts = ijk[rng.permutation(len(ijk))] - relocator.centroid[lesion]
        lesion_shifts = lesion_shifts[np.any(lesion_shifts != 0, axis=1)]
        accepted = lesion_shifts[relocator.valid(lesion, lesion_shifts)][:int(k)]
        columns.append([relocator.column(lesion, shift) for shift in accepted])
        shifts.append(accepted)
    return columns, shifts, candidates


def cohort_centroid(relocator):
    """Rounded weighted centroid (voxels) of all the cohort's lesions together."""
    weights = np.concatenate(list(relocator.values)) if relocator.values else np.zeros(0)
    ijk = np.concatenate(relocator.ijk) if relocator.ijk else np.zeros((0, 3))
    total = float(weights.sum())
    if total <= 0:
        return np.zeros(3, np.int64)
    return np.round((ijk * weights[:, np.newaxis]).sum(axis=0) / total).astype(np.int64)


def cohort_shifts(relocator, k, seed=0, attempts_per_shift=COHORT_SHIFT_ATTEMPTS):
    """``rl_cohort_shift``: k shared integer shifts of the whole cohort.

    The cohort's centroid is moved onto brain voxels drawn at random (never the zero shift); a shift is accepted when
    at least ``min_inside`` of the cohort's total weight lands inside the brain, trying at most
    ``attempts_per_shift * k`` targets (distinct brain voxels). Every lesion moves by the same vector, so the shapes and the pairwise overlaps
    are kept exactly on the grid (weight leaving the connectome mask is dropped, at most 1 - min_inside of the total).

    :return: (shifts (k', 3), info {"attempts", "inside" (cohort fraction per shift), "min_lesion_inside"}).
    """
    rng = _rng(seed, 5)
    centre = cohort_centroid(relocator)
    total = float(sum(relocator.total))
    accepted, inside, lesion_min, attempts = [], [], [], 0
    order = rng.permutation(len(relocator.brain_ijk))[: int(attempts_per_shift) * max(int(k), 1)]  # targets without repetition
    batch = max(16, int(k))
    while len(accepted) < k and attempts < order.size and total > 0:
        shifts = relocator.brain_ijk[order[attempts:attempts + batch]] - centre
        attempts += len(shifts)
        shifts = shifts[np.any(shifts != 0, axis=1)]
        mass = np.zeros((len(shifts), relocator.n_lesions))
        for lesion in range(relocator.n_lesions):
            mass[:, lesion] = relocator.inside_fraction(lesion, shifts) * relocator.total[lesion]
        fraction = mass.sum(axis=1) / total
        good = np.flatnonzero(fraction >= relocator.min_inside * (1.0 - 1e-12))[: k - len(accepted)]
        for row in good:
            accepted.append(shifts[row])
            inside.append(float(fraction[row]))
            with np.errstate(invalid="ignore", divide="ignore"):
                per_lesion = mass[row] / np.maximum(np.asarray(relocator.total), 1e-300)
            lesion_min.append(float(per_lesion.min()) if per_lesion.size else 1.0)
        batch = min(4 * batch, 4096)
    return np.array(accepted, dtype=np.int64).reshape(-1, 3), {"attempts": attempts, "inside": inside, "min_lesion_inside": lesion_min}


def apply_shift(relocator, shift):
    """Every lesion moved by one shared shift: a list of L (positions, weights) columns."""
    return [relocator.column(lesion, shift) if relocator.total[lesion] > 0 else (np.zeros(0, np.int64), np.zeros(0, np.float32)) for lesion in range(relocator.n_lesions)]


#
# Pairwise Dice and the overlap-matched null (rl_overlap)
#


DICE_EDGES = tuple(round(0.05 * k, 2) for k in range(21))  # lesionset.DICE_EDGES


class DiceTracker:
    """Pairwise Dice of binarized lesions under moves, with sparse sets: one membership bit per lesion and voxel.

    ``cover[v]`` holds the bits of the lesions that cover connectome voxel v, so the intersections of one lesion with
    all the others are one gather of its voxels' bit words (vectorised, O(voxels x L / 64)).
    """

    def __init__(self, n_lesions, n_voxels):
        self.n_lesions = int(n_lesions)
        self.words = max(1, (self.n_lesions + 63) // 64)
        self.cover = np.zeros((int(n_voxels), self.words), dtype=np.uint64)
        self.sets = [np.zeros(0, np.int64) for _ in range(self.n_lesions)]
        self.sizes = np.zeros(self.n_lesions, dtype=np.int64)

    def _bit(self, lesion):
        return lesion // 64, np.uint64(1) << np.uint64(lesion % 64)

    def set(self, lesion, positions):
        """Place ``lesion`` on these (unique) connectome positions."""
        word, bit = self._bit(lesion)
        old = self.sets[lesion]
        if old.size:
            self.cover[old, word] &= ~bit
        positions = np.asarray(positions, dtype=np.int64)
        if positions.size:
            self.cover[positions, word] |= bit
        self.sets[lesion] = positions
        self.sizes[lesion] = positions.size

    def intersections(self, positions):
        """(L,) number of voxels each lesion shares with the voxel set ``positions``."""
        positions = np.asarray(positions, dtype=np.int64)
        if positions.size == 0:
            return np.zeros(self.n_lesions, dtype=np.int64)
        words = np.ascontiguousarray(self.cover[positions], dtype="<u8")  # little-endian words: bit i of a word is lesion 64 * word + i
        bits = np.unpackbits(words.view(np.uint8), axis=1, bitorder="little")
        return bits.sum(axis=0, dtype=np.int64)[:self.n_lesions]

    def dice_row(self, lesion, positions=None):
        """(L,) Dice of ``lesion`` (or of the voxel set ``positions`` standing for it) with every lesion; 1 on the diagonal."""
        positions = self.sets[lesion] if positions is None else np.asarray(positions, dtype=np.int64)
        shared = self.intersections(positions).astype(np.float64)
        sizes = self.sizes.astype(np.float64).copy()
        sizes[lesion] = positions.size
        shared[lesion] = positions.size
        total = positions.size + sizes
        return np.divide(2.0 * shared, total, out=np.zeros(self.n_lesions), where=total > 0)

    def matrix(self):
        return np.vstack([self.dice_row(lesion) for lesion in range(self.n_lesions)]) if self.n_lesions else np.zeros((0, 0))


def _binary_positions(column, binarize_at=DICE_BINARIZE_AT):
    positions, weights = column
    return positions[np.asarray(weights) >= binarize_at]


def pairwise_dice(columns, n_voxels, binarize_at=DICE_BINARIZE_AT):
    """Condensed pairwise Dice (pairs i < j, row-major, like lesionset.pairwise_dice) of lesions binarized at ``binarize_at``."""
    columns = _sparse_columns(columns, n_voxels)
    tracker = DiceTracker(len(columns), n_voxels)
    for lesion, column in enumerate(columns):
        tracker.set(lesion, _binary_positions(column, binarize_at))
    matrix = tracker.matrix()
    return matrix[np.triu_indices(len(columns), 1)]


def _dice_bins(values, edges=DICE_EDGES):
    """Histogram bin of each Dice value (as numpy.histogram over ``edges``: half-open bins, the last one closed)."""
    return np.searchsorted(np.asarray(edges[1:-1]), np.asarray(values, dtype=np.float64), side="right")


def dice_histogram(condensed, edges=DICE_EDGES):
    return np.bincount(_dice_bins(condensed, edges), minlength=len(edges) - 1)


def ks_distance(hist_a, hist_b):
    """Kolmogorov-Smirnov distance between two histograms over the same bins (max |CDF difference|)."""
    a = np.asarray(hist_a, dtype=np.float64)
    b = np.asarray(hist_b, dtype=np.float64)
    if a.sum() <= 0 or b.sum() <= 0:
        return 0.0
    return float(np.max(np.abs(np.cumsum(a) / a.sum() - np.cumsum(b) / b.sum())))


def anneal_overlap(relocator, iterations=ANNEAL_ITERATIONS, seed=0, edges=DICE_EDGES, t0=ANNEAL_T0, t1=ANNEAL_T1, observed=None):
    """``rl_overlap`` (experimental): place the lesions (rigid shifts of each centroid) by simulated annealing so that the
    histogram of their pairwise Dice matches the observed cohort's (minimising the KS distance between the histograms).

    The start is an independent random placement (rl_independent). Each iteration moves one lesion: half of the
    proposals put its centroid near another lesion's current place (to create overlap), half anywhere in the brain;
    a proposal must keep ``min_inside`` of the lesion's weight inside the brain. Dice rows are updated with sparse sets
    (:class:`DiceTracker`); moves are accepted by the Metropolis rule with geometric cooling from ``t0`` to ``t1``;
    the best placement is returned.

    :param observed: optional condensed observed Dice (default: of the relocator's lesions).
    :return: dict {"shifts" (L, 3), "columns", "ks_initial", "ks_final", "ks_best", "accepted", "proposed", "invalid"}.
    """
    rng = _rng(seed, 6)
    n_lesions = relocator.n_lesions
    if observed is None:
        observed = pairwise_dice(relocator.columns, relocator.n_voxels)
    hist_obs = dice_histogram(observed, edges)
    tracker = DiceTracker(n_lesions, relocator.n_voxels)
    shifts = np.zeros((n_lesions, 3), dtype=np.int64)
    for lesion in range(n_lesions):
        if relocator.total[lesion] <= 0:
            continue
        for _attempt in range(50):
            candidate = relocator.random_targets(rng, 64) - relocator.centroid[lesion]
            good = np.flatnonzero(relocator.valid(lesion, candidate))
            if good.size:
                shifts[lesion] = candidate[good[0]]
                break
        tracker.set(lesion, _binary_positions(relocator.column(lesion, shifts[lesion])))
    dice = tracker.matrix()
    upper = np.triu_indices(n_lesions, 1)
    hist = dice_histogram(dice[upper], edges)
    ks = ks_distance(hist, hist_obs)
    result = {"ks_initial": ks, "accepted": 0, "proposed": 0, "invalid": 0}
    best_ks, best_shifts = ks, shifts.copy()
    active = [lesion for lesion in range(n_lesions) if relocator.total[lesion] > 0]
    iterations = int(iterations)
    if n_lesions >= 2 and active and iterations > 0:
        ratio = (t1 / t0) ** (1.0 / max(iterations - 1, 1))
        temperature = t0
        for _iteration in range(iterations):
            lesion = active[int(rng.integers(len(active)))]
            if rng.random() < 0.5 and len(active) > 1:
                other = active[int(rng.integers(len(active)))]
                radius = max(1, relocator.radius[lesion])
                target = relocator.centroid[other] + shifts[other] + rng.integers(-radius, radius + 1, size=3)
            else:
                target = relocator.random_targets(rng, 1)[0]
            shift = target - relocator.centroid[lesion]
            result["proposed"] += 1
            if not relocator.valid(lesion, shift[np.newaxis])[0]:
                result["invalid"] += 1
                temperature *= ratio
                continue
            positions = _binary_positions(relocator.column(lesion, shift))
            new_row = tracker.dice_row(lesion, positions)
            others = np.arange(n_lesions) != lesion
            new_hist = hist - np.bincount(_dice_bins(dice[lesion, others], edges), minlength=hist.size) + np.bincount(_dice_bins(new_row[others], edges), minlength=hist.size)
            new_ks = ks_distance(new_hist, hist_obs)
            if new_ks <= ks or rng.random() < math.exp(-(new_ks - ks) / max(temperature, 1e-12)):
                tracker.set(lesion, positions)
                dice[lesion, others] = new_row[others]
                dice[others, lesion] = new_row[others]
                hist, ks = new_hist, new_ks
                shifts[lesion] = shift
                result["accepted"] += 1
                if ks < best_ks:
                    best_ks, best_shifts = ks, shifts.copy()
            temperature *= ratio
    result.update({"ks_final": ks, "ks_best": best_ks, "shifts": best_shifts})
    result["columns"] = [relocator.column(lesion, best_shifts[lesion]) if relocator.total[lesion] > 0 else (np.zeros(0, np.int64), np.zeros(0, np.float32)) for lesion in range(n_lesions)]
    return result


#
# Empirical pool
#


def volume_matched_sets(cohort_volumes, pool_volumes, k, n_match=POOL_MATCHES, seed=0):
    """``empirical_pool``: K null cohorts of control lesions, one volume-matched control lesion per cohort lesion.

    The candidates of a lesion are the ``n_match`` control lesions nearest in log volume. Within a null cohort each
    lesion (in random order) takes a random candidate that no other lesion of that cohort took, when one is left.

    :return: (sets (K, L) int indices into the pool, candidates (L, n_match')).
    """
    cohort = np.log(np.maximum(np.asarray(cohort_volumes, dtype=np.float64), 1e-6))
    pool = np.log(np.maximum(np.asarray(pool_volumes, dtype=np.float64), 1e-6))
    if pool.size == 0:
        raise ValueError("The control pool is empty")
    n_match = max(1, min(int(n_match), pool.size))
    candidates = np.vstack([np.argsort(np.abs(pool - value), kind="stable")[:n_match] for value in cohort]) if cohort.size else np.zeros((0, n_match), np.int64)
    rng = _rng(seed, 7)
    sets = np.zeros((int(k), cohort.size), dtype=np.int64)
    for b in range(int(k)):
        used = set()
        for lesion in rng.permutation(cohort.size):
            options = [c for c in candidates[lesion] if c not in used] or list(candidates[lesion])
            choice = int(options[int(rng.integers(len(options)))])
            used.add(choice)
            sets[b, lesion] = choice
    return sets, candidates


#
# Threshold rule and binary maps
#


SIGN_PARTS = {"both": ("pos", "neg"), "pos": ("pos",), "neg": ("neg",)}


def rule_statistic(rule):
    """The map a rule applies to: "T", or "Fz" (fz_abs, and top_pct on Fz); see cohort.rule_statistic."""
    from .cohort import rule_statistic as statistic

    return statistic(rule)


def resolve_rule(rule, n, n_voxels, gsr=None):
    """The rule resolved for maps of N subjects (top_pct rules are resolved per map: the result has no value).

    :raise ValueError: the rule is not allowed for N (e.g. |T| >= 7 below N = 900 without "override").
    """
    rule = thresholds.normalize_rule(rule)
    if rule["kind"] == "top_pct":
        return thresholds.resolve(rule, max(int(n), 1), n_voxels, gsr=gsr, values=np.zeros(1))
    resolved = thresholds.resolve(rule, int(n), n_voxels, gsr=gsr)
    if not (resolved.allowed or rule.get("override")):
        raise ValueError(f"The rule cannot be applied to maps with N={n}: {resolved.banner or 'not allowed'}")
    return resolved


def rule_values(rows, rule, n, n_voxels, gsr=None):
    """(L,) threshold of each map (row) under the rule: one value for N-derived rules, a percentile per map for top_pct.

    Same values as ``thresholds.resolve`` (and so as cohort.group_maps): np.percentile of the finite scores.
    """
    rule = thresholds.normalize_rule(rule)
    rows = np.atleast_2d(rows)
    if rule["kind"] != "top_pct":
        return np.full(rows.shape[0], resolve_rule(rule, n, n_voxels, gsr).value)
    sign = rule["sign"]
    out = np.full(rows.shape[0], np.nan)
    for row, values in enumerate(rows):
        scores = values.astype(np.float64)  # a copy: np.percentile may then partition it in place
        if sign == "both":
            np.abs(scores, out=scores)
        elif sign == "neg":
            np.negative(scores, out=scores)
        if np.all(np.isfinite(scores)):
            above = scores[scores > 0.0]
            cut = np.percentile(scores, 100.0 - rule["value"], overwrite_input=True)
            # As thresholds.resolve: the cut never goes below 0 (only voxels of the sign pass), NaN without any
            out[row] = (cut if cut > 0.0 else above.min()) if above.size else np.nan
        else:
            out[row] = thresholds.resolve(rule, max(int(n), 1), n_voxels, values=values).value
    return out


def binarize_parts(rows, values, sign="both", empty=None):
    """(L, n_parts, V) bool binary maps: per part ("pos": map >= t, "neg": map <= -t) as stats.binarize.

    For sign "both", a map passes where |map| >= t; its pos and neg parts never overlap. Rows whose threshold is not
    finite, and rows marked ``empty`` (a lesion without voxels), pass nowhere.
    """
    rows = np.atleast_2d(rows)
    parts = SIGN_PARTS[sign]
    out = np.zeros((rows.shape[0], len(parts), rows.shape[1]), dtype=bool)
    values = np.asarray(values, dtype=np.float64).reshape(-1, 1)
    usable = np.isfinite(values[:, 0])
    if empty is not None:
        usable &= ~np.asarray(empty, dtype=bool)
    for index, part in enumerate(parts):
        out[:, index] = (rows >= values) if part == "pos" else (rows <= -values)
    out[~usable] = False
    return out


def classic_pass(parts_counts, n_lesions, overlap_pct, sign="both"):
    """(V,) bool: the classic sensitivity rule (at least ``overlap_pct`` % of the maps), sign-specific as cohort.sensitivity."""
    passed = [100.0 * np.asarray(count, dtype=np.float64) / max(int(n_lesions), 1) >= float(overlap_pct) for count in parts_counts]
    if sign == "both":
        return passed[0] ^ passed[1]
    return passed[0]


#
# Bit-packed binary maps of the null columns
#


class ColumnStore:
    """Binary maps of null columns (relocated lesions, control lesions), bit-packed: n_columns x n_parts x ceil(V / 8)
    bytes, plus optional values on a voxel sample (exact mode, for the group QA) or parcel weights (fast mode).
    """

    def __init__(self, n_columns, n_parts, n_voxels, n_sample=0, n_parcels=0):
        self.n_columns, self.n_parts, self.n_voxels = int(n_columns), int(n_parts), int(n_voxels)
        self.bits = np.zeros((self.n_columns, self.n_parts, (self.n_voxels + 7) // 8), dtype=np.uint8)
        self.sample = np.zeros((self.n_columns, int(n_sample)), dtype=np.float16) if n_sample else None
        self.weights = np.zeros((self.n_columns, int(n_parcels)), dtype=np.float32) if n_parcels else None

    @property
    def nbytes(self):
        return self.bits.nbytes + (self.sample.nbytes if self.sample is not None else 0) + (self.weights.nbytes if self.weights is not None else 0)

    def put(self, start, parts, sample=None, weights=None):
        stop = start + parts.shape[0]
        self.bits[start:stop] = np.packbits(parts, axis=-1)
        if sample is not None and self.sample is not None:
            self.sample[start:stop] = np.clip(sample, -65504.0, 65504.0)
        if weights is not None and self.weights is not None:
            self.weights[start:stop] = weights

    def counts(self, columns):
        """(n_parts, V) overlap counts of a set of columns."""
        columns = np.asarray(columns, dtype=np.int64)
        bits = np.unpackbits(self.bits[columns], axis=-1, count=self.n_voxels)
        return bits.sum(axis=0, dtype=np.int32)

    def binary(self, column):
        return np.unpackbits(self.bits[column], axis=-1, count=self.n_voxels).astype(bool)


#
# Null statistics
#


def fdr_bh(p):
    """Benjamini-Hochberg q-values of a vector of p-values (glm.fdr_bh)."""
    from .glm import fdr_bh as bh

    return bh(p)


def fdr_floor_voxels(n_voxels, min_p, alpha=ALPHA):
    """Voxels that must reach the smallest attainable p before BH gives q <= alpha: ceil(V * p_min / alpha)."""
    return math.ceil(n_voxels * min_p / alpha - 1e-9)


def permutation_p(observed, null_ge_counts, k):
    """p = (1 + #{null >= observed}) / (K + 1): its floor is 1 / (K + 1)."""
    return (1.0 + np.asarray(null_ge_counts, dtype=np.float64)) / (int(k) + 1.0)


class NullOverlap:
    """Streaming statistics of K null overlap maps against the observed overlap (module docstring).

    ``add(parts_counts)`` takes the (n_parts, V) counts of one null cohort; :meth:`finish` returns the maps and numbers.
    The null overlap maps are kept in ``store`` (a (K, V) array, in memory or a numpy.memmap) for the max statistic.
    """

    def __init__(self, observed_parts, n_lesions, overlap_pct, sign, k, store=None):
        self.observed_parts = np.asarray(observed_parts, dtype=np.int32)
        self.observed = self.observed_parts.sum(axis=0)
        self.n_lesions = int(n_lesions)
        self.overlap_pct = float(overlap_pct)
        self.sign = sign
        self.k = int(k)
        n_voxels = self.observed.size
        self.store = store if store is not None else np.zeros((self.k, n_voxels), dtype=np.uint8 if self.n_lesions < 256 else np.uint16)
        self.sum = np.zeros(n_voxels)
        self.sumsq = np.zeros(n_voxels)
        self.ge = np.zeros(n_voxels, dtype=np.int64)
        self.bias = np.zeros(n_voxels, dtype=np.int64)
        self.fractions = []
        self.any_pass = []
        self.count = 0
        self.classic_observed = classic_pass(self.observed_parts, self.n_lesions, self.overlap_pct, sign)

    def add(self, parts_counts):
        if self.count >= self.k:
            raise ValueError(f"More than K = {self.k} null cohorts")
        parts_counts = np.asarray(parts_counts)
        total = parts_counts.sum(axis=0)
        values = total.astype(np.float64)
        self.sum += values
        self.sumsq += values * values
        self.ge += total >= self.observed
        passed = classic_pass(parts_counts, self.n_lesions, self.overlap_pct, self.sign)
        self.bias += passed
        self.fractions.append(float(passed.mean()) if passed.size else 0.0)
        self.any_pass.append(bool(passed.any()))
        self.store[self.count] = total
        self.count += 1

    def finish(self, cancel=None, chunk_voxels=10_000_000):
        """{"maps": {desc: (V,) float32}, "z", "max_null", "mean", "sd", "numbers"} over the K null cohorts added."""
        k = self.count
        if k < 1:
            raise ValueError("No null cohort was computed")
        mean = self.sum / k
        var = (self.sumsq - k * mean * mean) / max(k - 1, 1)
        sd = np.sqrt(np.maximum(var, 0.0))
        inverse = 1.0 / np.maximum(sd, SD_FLOOR)
        observed = self.observed.astype(np.float64)
        z = (observed - mean) * inverse  # the same arithmetic as the null cohorts' z below (ties compare exactly)
        max_null = np.empty(k)
        step = max(1, int(chunk_voxels) // max(observed.size, 1))
        buffer = np.empty((min(step, k), observed.size))
        for start in range(0, k, step):
            if cancel is not None:
                cancel.raise_if_cancelled(done=start)
            block = np.asarray(self.store[start:start + step])
            work = buffer[: block.shape[0]]
            np.subtract(block, mean, out=work)
            work *= inverse
            max_null[start:start + step] = work.max(axis=1)
        p = permutation_p(observed, self.ge, k)
        q = fdr_bh(p)
        ordered = np.sort(max_null)
        exceed = k - np.searchsorted(ordered, z, side="left")
        p_fwe = (1.0 + exceed) / (k + 1.0)
        excess = 100.0 * (observed - mean) / max(self.n_lesions, 1)
        maps = {
            "sensNullExcess": excess.astype(np.float32),
            "sensNullP": p.astype(np.float32),
            "sensNullQ": q.astype(np.float32),
            "sensNullPFwe": p_fwe.astype(np.float32),
            "sensNullBias": (self.bias / k).astype(np.float32),
        }
        n_voxels = observed.size
        min_p = 1.0 / (k + 1)
        fractions = np.asarray(self.fractions)
        observed_fraction = float(self.classic_observed.mean()) if n_voxels else 0.0
        numbers = {
            "k": k,
            "n_voxels": int(n_voxels),
            "min_attainable_p": min_p,
            "min_attainable_q": min_p,
            "fdr_floor_voxels": fdr_floor_voxels(n_voxels, min_p),
            "voxels_at_floor": int(np.count_nonzero(p <= min_p * (1 + 1e-9))),
            "min_p": float(p.min()) if n_voxels else None,
            "min_q": float(q.min()) if n_voxels else None,
            "min_p_fwe": float(p_fwe.min()) if n_voxels else None,
            "n_p_05": int(np.count_nonzero(p <= ALPHA)),
            "n_q_05": int(np.count_nonzero(q <= ALPHA)),
            "n_fwe_05": int(np.count_nonzero(p_fwe <= ALPHA)),
            "fwe_critical_z": float(np.quantile(max_null, 1.0 - ALPHA)),
            "sd_floor": SD_FLOOR,
            "excess_max": float(excess.max()) if n_voxels else None,
            "classic_rule": {
                "overlap_pct": self.overlap_pct,
                "sign": self.sign,
                "observed_fraction": observed_fraction,
                "null_median": float(np.median(fractions)),
                "null_p05": float(np.percentile(fractions, 5)),
                "null_p95": float(np.percentile(fractions, 95)),
                "null_mean": float(fractions.mean()),
                "null_any_pass": float(np.mean(self.any_pass)),
                "false_positive_rate_note": "fraction of in-mask voxels where the classic rule passes, in the observed cohort and in each null cohort",
            },
            "discreteness": (
                f"The smallest attainable p is 1/(K+1) = {min_p:.3g} (K = {k}); BH-FDR reaches q <= {ALPHA:g} only when at least "
                f"{fdr_floor_voxels(n_voxels, min_p)} voxels reach it."
            ),
        }
        numbers["classic_rule"]["text"] = fpr_text(self.overlap_pct, observed_fraction, fractions)
        return {"maps": maps, "z": z, "max_null": max_null, "mean": mean, "sd": sd, "numbers": numbers}


def _percent_text(fraction):
    percent = 100.0 * float(fraction)
    if percent == 0.0 or percent >= 10.0:
        return f"{percent:.0f}%"
    if percent >= 1.0:
        return f"{percent:.1f}%"
    return f"{percent:.2g}%"


def fpr_text(overlap_pct, observed_fraction, null_fractions):
    """The classic rule's false-positive-rate summary, e.g.
    "≥90% at the rule: 31% of brain voxels pass; random sets: 28% (5-95%: 22-35%)" (with en dashes).
    """
    fractions = np.asarray(null_fractions, dtype=np.float64)
    if fractions.size == 0:
        return f"≥{overlap_pct:g}% at the rule: {_percent_text(observed_fraction)} of brain voxels pass; random sets: not computed"
    p05, median, p95 = np.percentile(fractions, [5, 50, 95])
    return (f"≥{overlap_pct:g}% at the rule: {_percent_text(observed_fraction)} of brain voxels pass; "
            f"random sets: {_percent_text(median)} (5\u201395%: {_percent_text(p05)}\u2013{_percent_text(p95)})")


#
# Inputs
#


class Cohort:
    """The observed lesions of a nulls run, from a cohortmaps maps.tsv (the maps are never recomputed for the observed side).

    ``ids``, ``keys``, ``columns`` ((positions, weights) per lesion over the connectome voxels), ``maps`` ({"T",
    "AvgFz": [paths]}), ``volume_ml``, ``lesion_sha256``, ``n`` (connectome subjects), ``subject_ids``,
    ``subject_ids_sha256``, ``record`` (cohortmaps.json), ``maps_tsv``, ``run_id``, ``excluded`` [{participant_id, reason}].
    """

    def __init__(self):
        self.ids, self.keys, self.columns, self.volume_ml, self.lesion_sha256 = [], [], [], [], []
        self.maps = {"T": [], "AvgFz": []}
        self.n = None
        self.subject_ids = None
        self.subject_ids_sha256 = None
        self.record = {}
        self.maps_tsv = None
        self.run_id = None
        self.excluded = []

    def __len__(self):
        return len(self.ids)

    def subset(self, keep):
        out = Cohort()
        for attribute in ("n", "subject_ids", "subject_ids_sha256", "record", "maps_tsv", "run_id"):
            setattr(out, attribute, getattr(self, attribute))
        out.excluded = list(self.excluded)
        for index in keep:
            out.ids.append(self.ids[index])
            out.keys.append(self.keys[index])
            out.columns.append(self.columns[index])
            out.volume_ml.append(self.volume_ml[index])
            out.lesion_sha256.append(self.lesion_sha256[index])
            for key in out.maps:
                out.maps[key].append(self.maps[key][index])
        return out


def _na(value):
    return value in (None, "", "n/a")


def load_cohort(maps_tsv, bundle, participants=None, group_column="group", emit=None):
    """The cohort of a cohortmaps maps.tsv (with its cohortmaps.json): lesion weights from the cache entries.

    With ``participants`` (a participants.tsv with ``group_column``), only the cases are kept when there are any (as the
    classic overlap of cohort.py). Lesions without a voxel in the connectome mask are excluded with a warning.
    """
    from . import cohortmaps, engine
    from .cohort import read_groups

    emit = emit or jobs.emit
    maps_tsv = os.path.abspath(os.fspath(maps_tsv))
    rows = cohortmaps.read_maps_tsv(maps_tsv)
    folder = os.path.dirname(maps_tsv)
    record = cohortmaps.read_record(os.path.join(folder, cohortmaps.COHORTMAPS_JSON)) or {}
    cohort = Cohort()
    cohort.maps_tsv = maps_tsv
    cohort.record = record
    cohort.run_id = record.get("run_id")
    cohort.n = record.get("n")
    cohort.subject_ids = record.get("subject_ids")
    cohort.subject_ids_sha256 = record.get("subject_ids_sha256")
    if cohort.n is None and cohort.subject_ids:
        cohort.n = len(cohort.subject_ids)
    groups = read_groups(participants, group_column) if participants and os.path.isfile(participants) else {}
    if groups and any(str(value or "").strip().lower() == "case" for value in groups.values()):
        dropped = [row["participant_id"] for row in rows if str(groups.get(row["participant_id"]) or "").strip().lower() != "case"]
        rows = [row for row in rows if str(groups.get(row["participant_id"]) or "").strip().lower() == "case"]
        if dropped:
            cohort.excluded.extend({"participant_id": sid, "reason": "not a case (the overlap is over the cases)"} for sid in dropped)
    cache_dir = record.get("cache_dir")
    for row in rows:
        sid = row["participant_id"]
        column = None
        entry = cohortmaps.read_entry(cache_dir, row["key"]) if cache_dir and not _na(row.get("key")) else None
        if entry is not None:
            indices, values, stored = cohortmaps.read_weights(entry)
            if stored != bundle.n_voxels:
                raise ValueError(f"The cache entry of {sid} has {stored} voxels, the connectome has {bundle.n_voxels}")
            column = _sparse_columns([(indices, values)], bundle.n_voxels)[0]
        elif not _na(row.get("lesion")) and os.path.isfile(row["lesion"]):
            weights, _qc = engine.prepare_lesion(row["lesion"], bundle, supersample=int(record.get("supersample") or 2), binarize=not record.get("probabilistic"))
            column = _sparse_columns(weights, bundle.n_voxels)[0]
        if column is None:
            cohort.excluded.append({"participant_id": sid, "reason": "the lesion weights are not available (cache entry and lesion file missing)"})
            emit("warning", code="excluded", participant_id=sid, message=f"{sid}: the lesion weights are not available (cache entry and lesion file missing); the lesion is left out of the nulls.")
            continue
        if column[0].size == 0:
            cohort.excluded.append({"participant_id": sid, "reason": "no voxel in the connectome mask"})
            emit("warning", code="excluded", participant_id=sid, message=f"{sid}: the lesion has no voxel in the connectome mask; it is left out of the nulls.")
            continue
        cohort.ids.append(sid)
        cohort.keys.append(None if _na(row.get("key")) else row["key"])
        cohort.columns.append(column)
        volume = row.get("volume_ml")
        cohort.volume_ml.append(None if _na(volume) else float(volume))
        cohort.lesion_sha256.append(None if _na(row.get("lesion_sha256")) else row["lesion_sha256"])
        for key in cohort.maps:
            cohort.maps[key].append(None if _na(row.get(key)) else row[key])
    voxel_ml = bundle.grid.voxel_volume / 1000.0
    cohort.volume_ml = [v if v is not None else float(c[1].sum() * voxel_ml) for v, c in zip(cohort.volume_ml, cohort.columns, strict=True)]
    return cohort


def _load_rows(paths, bundle, what):
    from .cohort import load_masked

    missing = [index for index, path in enumerate(paths) if not path or not os.path.isfile(path)]
    if missing:
        raise jobs.JobError(f"{len(missing)} lesion map(s) ({what}) are missing, for example {paths[missing[0]]!r}", code="input")
    return load_masked(paths, bundle.mask_indices, bundle.shape, bundle.affine)


def _mapped_study(study, bundle, out_dir, cache, threads, cancel, resume, budget_bytes, include_unreviewed, run_root, parents, argv, emit):
    """Map a study's analysable lesions with cohortmaps into ``out_dir``; return (maps.tsv path, cohortmaps run id)."""
    from . import cohortmaps
    from .study import Study

    study_obj = Study.open(study)
    lesions, excluded = cohortmaps.study_lesions(study_obj, include_unreviewed)
    if not lesions:
        raise jobs.JobError("No lesion to map (check include, review and the lesion files)", code="no_lesions")

    def inner(event, **fields):
        if event == "start":
            return None
        if event == "done":
            return emit("result", kind="mapped", path=os.path.join(out_dir, cohortmaps.MAPS_TSV), n=fields.get("n"), cache_hits=fields.get("cache_hits"), computed=fields.get("computed"))
        return emit(event, **fields)

    record = cohortmaps.run(lesions, bundle, out_dir, cache_root=cache, study=study_obj, threads=threads, cancel=cancel, resume=resume, budget_bytes=budget_bytes,
                            excluded=excluded, review={"required": study_obj.review_required, "include_unreviewed": bool(include_unreviewed)}, run_root=run_root or study_obj.path,
                            parent_runs=parents, argv=argv, emit=inner)
    return os.path.join(out_dir, cohortmaps.MAPS_TSV), (record or {}).get("run_id")


def _pool_cohort(pool, pool_maps, bundle, out_dir, cohort, mode, cache, threads, cancel, budget_bytes, run_root, emit):
    """The control pool of ``empirical_pool`` as a :class:`Cohort` (mapped with cohortmaps in exact mode)."""
    from . import cohortmaps
    from .cohort import lesions_from

    if pool_maps:
        return load_cohort(pool_maps, bundle, emit=emit)
    lesions = lesions_from(pool)
    pool_dir = os.path.join(out_dir, "pool")
    if mode == "fast":
        out = Cohort()
        from . import engine

        for sid, path in lesions:
            weights, qc = engine.prepare_lesion(path, bundle)
            column = _sparse_columns(weights, bundle.n_voxels)[0]
            if column[0].size == 0:
                continue
            out.ids.append(sid)
            out.keys.append(None)
            out.columns.append(column)
            out.volume_ml.append(qc.get("volume_ml"))
            from .export import file_sha256

            out.lesion_sha256.append(file_sha256(path))
            for key in out.maps:
                out.maps[key].append(None)
        return out

    def inner(event, **fields):
        if event in ("start", "done"):
            return None
        return emit(event, **fields)

    cohortmaps.run(lesions, bundle, pool_dir, subjects=list(cohort.subject_ids), cache_root=cache or os.path.join(out_dir, "cache"), threads=threads, cancel=cancel,
                   budget_bytes=budget_bytes, run_root=run_root, emit=inner)
    return load_cohort(os.path.join(pool_dir, cohortmaps.MAPS_TSV), bundle, emit=emit)


#
# Fast index
#


def find_fast_index(bundle, subject_ids=None, verify=False):
    """Folder of the fast parcel index of a bundle and subject list (fastmap.default_out_dir), or None.

    The index.json must be a fast index of this connectome mask and of exactly these subjects (all of the bundle's by
    default), with its matrices present; ``verify`` also checks every file's sha256 (slow for large connectomes).
    """
    from . import fastmap
    from .connectome import ConnectomeBundle, subjects_sha256

    bundle = ConnectomeBundle.open(bundle) if isinstance(bundle, (str, os.PathLike)) else bundle
    ids = list(bundle.subject_ids if subject_ids is None else subject_ids)
    folder = fastmap.default_out_dir(bundle, ids)
    path = os.path.join(folder, fastmap.INDEX_JSON)
    try:
        with open(path, encoding="utf-8") as f:
            metadata = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(metadata, dict) or metadata.get("format") != fastmap.FORMAT_NAME:
        return None
    if (metadata.get("subjects") or {}).get("sha256") != subjects_sha256(ids):
        return None
    if (metadata.get("bundle") or {}).get("mask_sha256") not in (None, bundle.mask_sha256):
        return None
    files = metadata.get("files") or {}
    if any(not os.path.isfile(os.path.join(folder, item.get("file", ""))) for item in files.values()):
        return None
    if verify:
        try:
            fastmap.load_index(folder, bundle, verify=True)
        except (OSError, ValueError):
            return None
    return folder


#
# Run-time estimate
#


def estimate_details(n_lesions, bundle, k_pool=None, mode="exact", k=DEFAULT_K, kind="rl_independent", subjects=None, threads=2, budget_bytes=None, rule=None, n_pool=None):
    """Predicted run time of a nulls run (seconds) with its parts: engine passes, binarization and the null cohorts.

    Engine (exact mode): per subject and pass 0.13 s plus 0.0042 s per seed column (the lesions and their K_pool
    relocations, at full V), scaled by V * T / (292,019 * 216) and (2 / threads)^0.8, each pass at least as long as
    reading the connectome at 400 MB/s; passes = ceil(L / L_max) with L_max from the cohortmaps memory budget. Fast
    mode costs no engine pass. Thresholding costs about 16 ns per voxel and map for top-x% rules (3 ns otherwise),
    and each null cohort about 1.3 ns per lesion and voxel plus 25 ns per voxel and 1 ms (annealing: 0.15 s per cohort);
    a run adds about 1 s. On a 4-core host the estimate was within a factor of 1.5 of measured runs.

    :param bundle: a ConnectomeBundle or its folder.
    :param subjects: the connectome subjects (None: all; an int: the first n; a list of ids).
    :return: dict {"seconds", "engine_s", "rule_s", "sets_s", "anneal_s", "passes", "batch_size", "columns", "k", "k_pool", "mode"}.
    """
    from . import cohortmaps, engine
    from .connectome import ConnectomeBundle

    bundle = ConnectomeBundle.open(bundle) if isinstance(bundle, (str, os.PathLike)) else bundle
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    n_lesions = max(int(n_lesions), 0)
    k_pool = int(k_pool or (DEFAULT_K_POOL if mode == "exact" else DEFAULT_K_POOL_FAST))
    k = int(k)
    if kind == "rl_cohort_shift" and mode == "exact":
        k = k_pool
    ids = engine.resolve_subjects(bundle, subjects)
    n_voxels = bundle.n_voxels
    mean_t = float(np.mean([sum(bundle.runs(i)) for i in ids])) if ids else 0.0
    scale = n_voxels * mean_t / ENGINE_REFERENCE_SIZE * (2.0 / max(int(threads or 2), 1)) ** 0.8
    columns = n_lesions * k_pool if kind != "empirical_pool" else int(n_pool or 0)
    rule_kind = "top_pct" if thresholds.normalize_rule(rule)["kind"] == "top_pct" else "other"
    engine_s, passes, size = 0.0, 0, 0
    if mode == "exact" and n_lesions:
        budget = cohortmaps.default_budget_bytes() if budget_bytes is None else int(budget_bytes)
        per_lesion = _exact_lesion_bytes(n_voxels, k_pool if kind != "empirical_pool" else 0)
        size = int(min(cohortmaps.MAX_BATCH, max(1, budget // max(per_lesion, 1))))
        if kind == "empirical_pool":
            batches = [(start, min(start + size, columns)) for start in range(0, columns, size)]
        else:
            batches = [(start, min(start + size, n_lesions)) for start in range(0, n_lesions, size)]
        passes = len(batches)
        subject_bytes = n_voxels * mean_t * bundle.dtype.itemsize
        for start, stop in batches:
            n_columns = (stop - start) * (1 if kind == "empirical_pool" else k_pool + 1)
            per_subject = (ENGINE_SUBJECT_S + ENGINE_COLUMN_S * n_columns) * scale
            engine_s += max(per_subject, subject_bytes / READ_RATE) * len(ids)
    rule_s = columns * n_voxels * (RULE_VOXEL_S[rule_kind] + (FAST_MAP_VOXEL_S if mode == "fast" else 0.0))
    sets_s = k * (n_voxels * (n_lesions * SET_VOXEL_S + COHORT_VOXEL_S) + COHORT_OVERHEAD_S)
    anneal_s = 0.0
    if kind in ("rl_overlap", "rl_cohort_shift") and mode == "fast":
        rule_s = k * n_lesions * n_voxels * (RULE_VOXEL_S[rule_kind] + FAST_MAP_VOXEL_S)
        anneal_s = k * ANNEAL_SET_S if kind == "rl_overlap" else 0.0
    seconds = RUN_OVERHEAD_S + engine_s + rule_s + sets_s + anneal_s
    return {"seconds": float(seconds), "engine_s": float(engine_s), "rule_s": float(rule_s), "sets_s": float(sets_s), "anneal_s": float(anneal_s), "passes": passes,
            "batch_size": size, "columns": columns, "k": k, "k_pool": k_pool, "mode": mode, "kind": kind, "subjects": len(ids), "n_voxels": n_voxels}


def estimate(n_lesions, bundle, k_pool=None, mode="exact", **kwargs):
    """Predicted run time in seconds of a nulls run of ``n_lesions`` lesions (see :func:`estimate_details`)."""
    return estimate_details(n_lesions, bundle, k_pool=k_pool, mode=mode, **kwargs)["seconds"]


def _exact_lesion_bytes(n_voxels, k_pool):
    """Bytes one lesion and its K_pool relocations add to an exact engine pass (cohortmaps constants, plus the null
    mean Fz maps).
    """
    from . import cohortmaps

    return cohortmaps.BYTES_PER_LESION_VOXEL * int(n_voxels) + (cohortmaps.BYTES_PER_NULL_VOXEL + 4) * int(k_pool) * int(n_voxels)


#
# The run
#


class _CancelAdapter:
    """A cancel callable or Event with the ``raise_if_cancelled`` of jobs.CancelToken."""

    def __init__(self, cancel):
        self._cancel = cancel
        self.reason = None

    def cancelled(self):
        from .connectome import _is_cancelled

        return bool(self._cancel is not None and _is_cancelled(self._cancel))

    is_set = cancelled

    def __call__(self):
        return self.cancelled()

    def raise_if_cancelled(self, done=None):
        if self.cancelled():
            raise jobs.Cancelled(done=done, reason="cancelled")


def _cancel_token(cancel):
    return cancel if hasattr(cancel, "raise_if_cancelled") else _CancelAdapter(cancel)


class _Context:
    """Parameters of a run, resolved once."""

    def __init__(self, **values):
        self.__dict__.update(values)


def _emit_progress(emit, stage, done, total, started, **extra):
    spent = time.monotonic() - started
    eta = spent / done * (total - done) if done else None
    emit("progress", stage=stage, done=int(done), total=int(total), elapsed=round(spent, 3), eta=None if eta is None else round(eta, 3), **extra)


def _relocation_columns(context, relocator, cohort, reference, emit):
    """Per lesion, the null columns of a pooled kind and the design details: (columns, shifts, info)."""
    kind, k_pool, seed = context.kind, context.k_pool, context.seed
    info = {}
    if kind == "rl_independent":
        columns, shifts = relocate_independent(relocator, k_pool, seed)
    elif kind == "rl_strength":
        key = STRENGTH_MAPS[context.strength]
        maps = getattr(reference, "maps", reference) if reference is not None else None
        if not maps or maps.get(key) is None:
            raise jobs.JobError(f"rl_strength needs the bundle's reference maps ({key}); build them with 'python -m LesionNetworkMappingLib.reference build'", code="no_reference")
        columns, shifts, candidates = relocate_strength(relocator, maps[key], k_pool, seed, STRENGTH_CANDIDATES)
        values = lesion_strength(relocator, maps[key])
        info = {"strength": context.strength, "map": key, "candidates": STRENGTH_CANDIDATES, "lesion_strength": dict(zip(cohort.ids, values, strict=True)),
                "candidate_counts": [int(c.size) for c in candidates]}
    elif kind == "rl_cohort_shift":
        shared, shift_info = cohort_shifts(relocator, k_pool, seed)
        if len(shared) == 0:
            raise jobs.JobError("No shared shift keeps the cohort inside the brain; use rl_independent", code="no_shift")
        columns = [[relocator.column(lesion, shift) if relocator.total[lesion] > 0 else (np.zeros(0, np.int64), np.zeros(0, np.float32)) for shift in shared] for lesion in range(relocator.n_lesions)]
        shifts = [shared for _ in range(relocator.n_lesions)]
        info = {"shifts": len(shared), "attempts": shift_info["attempts"], "cohort_inside_median": float(np.median(shift_info["inside"])),
                "min_lesion_inside_median": float(np.median(shift_info["min_lesion_inside"]))}
        if len(shared) < k_pool:
            emit("warning", code="shifts", message=f"Only {len(shared)} of {k_pool} shared shifts keep the cohort inside the brain.")
    else:
        raise ValueError(kind)
    short = [(sid, len(lesion_columns)) for sid, lesion_columns in zip(cohort.ids, columns, strict=True) if 0 < len(lesion_columns) < k_pool]
    if short and kind != "rl_cohort_shift":
        where = f"of their {STRENGTH_CANDIDATES} strength-matched candidate voxels" if kind == "rl_strength" else "inside the brain"
        emit("warning", code="relocations", participants=[sid for sid, _ in short],
             message=f"{len(short)} lesion(s) have fewer than {k_pool} valid relocations {where} (fewest: {min(count for _, count in short)}); their null cohorts draw from the ones found.")
    return columns, shifts, info


def _draw_sets(context, counts, offsets):
    """(K, L) global column indices drawn for each null cohort of a pooled kind."""
    rng = _rng(context.seed, 2)
    k = context.k
    sets = np.zeros((k, len(counts)), dtype=np.int64)
    for lesion, (count, offset) in enumerate(zip(counts, offsets, strict=True)):
        sets[:, lesion] = offset + rng.integers(0, count, size=k)
    return sets


def _parts_of_rows(context, rows, empty=None):
    values = rule_values(rows, context.rule, context.n, context.n_voxels, context.gsr)
    return binarize_parts(rows, values, context.sign, empty=empty)


def _work_store(context, shape, dtype):
    """A (K, V) array for the null overlap maps: in memory up to the budget, else a memory-mapped file in the work folder."""
    nbytes = int(np.prod(shape)) * np.dtype(dtype).itemsize
    if nbytes <= max(context.budget_bytes // 2, 64 << 20):
        return np.zeros(shape, dtype=dtype)
    os.makedirs(context.work, exist_ok=True)
    return np.lib.format.open_memmap(os.path.join(context.work, "null_overlaps.npy"), mode="w+", dtype=dtype, shape=shape)


def _exact_store(context, bundle, cohort, columns, observed_rows, emit, cancel):
    """Map the null columns exactly (engine passes in budgeted batches of lesions); return (ColumnStore, observed check)."""
    from . import engine

    n_columns = sum(len(c) for c in columns)
    sample = context.sample
    store = ColumnStore(n_columns, len(SIGN_PARTS[context.sign]), bundle.n_voxels, n_sample=sample.size)
    offsets = np.concatenate(([0], np.cumsum([len(c) for c in columns])))[:-1]
    per_lesion = _exact_lesion_bytes(bundle.n_voxels, max((len(c) for c in columns), default=0))
    size = int(min(100, max(1, context.budget_bytes // max(per_lesion, 1))))
    batches = [(start, min(start + size, len(columns))) for start in range(0, len(columns), size)]
    state = jobs.ResumeState(os.path.join(context.work, RESUME_NAME))
    if not context.resume:
        state.clear()
    elif state.done and state.data.get("params") != context.params_sha256:
        emit("warning", code="resume_mismatch", message="The saved progress belongs to other parameters; the nulls are computed again.")
        state.clear()
    state.data = {"params": context.params_sha256}
    os.makedirs(context.work, exist_ok=True)
    check = []
    started = time.monotonic()
    emit("progress", stage="null maps", batch=0, batches=len(batches), batch_size=size, done=0, total=len(batches), elapsed=0.0, eta=None)
    statistic = context.statistic
    for number, (start, stop) in enumerate(batches):
        unit = f"batch-{number}"
        path = os.path.join(context.work, f"{unit}.npz")
        first, last = int(offsets[start]), int(offsets[start] + sum(len(c) for c in columns[start:stop]))
        if state.is_done(unit) and os.path.isfile(path):
            with np.load(path) as saved:
                store.bits[first:last] = saved["bits"]
                store.sample[first:last] = saved["sample"]
                check.extend(saved["check"].tolist())
            continue
        cancel.raise_if_cancelled(done=number)
        weights = np.vstack([_dense(cohort.columns[lesion], bundle.n_voxels) for lesion in range(start, stop)])
        null_columns = [column for lesion in range(start, stop) for column in columns[lesion]]

        def on_progress(done, total, elapsed, number=number):
            eta = elapsed / done * (total - done) if done else None
            emit("progress", stage="null maps", batch=number + 1, batches=len(batches), done=done, total=total, elapsed=round(elapsed, 3), eta=None if eta is None else round(eta, 3))

        maps = engine.compute_network_maps(bundle, weights, subjects=list(cohort.subject_ids), chunk=context.chunk, progress=on_progress, cancel=cancel,
                                           null_weights=null_columns or None, null_fz=statistic == "Fz" and bool(null_columns))
        if maps.cancelled:
            raise jobs.Cancelled(done=number, reason=getattr(cancel, "reason", None))
        null_rows = maps.null_t if statistic == "T" else maps.null_fz
        recomputed = maps.t if statistic == "T" else maps.avg_fz
        batch_check = []
        for row, lesion in enumerate(range(start, stop) if observed_rows is not None else ()):
            reference = observed_rows[lesion]
            scale = float(np.max(np.abs(reference))) or 1.0
            batch_check.append(float(np.max(np.abs(recomputed[row].astype(np.float64) - reference)) / scale))
        del maps
        empty = np.array([column[0].size == 0 for column in null_columns], dtype=bool)
        step = max(1, 4_000_000 // max(bundle.n_voxels, 1))
        for offset in range(0, len(null_columns), step):
            rows = null_rows[offset:offset + step]
            parts = _parts_of_rows(context, rows, empty[offset:offset + step])
            store.put(first + offset, parts, sample=rows[:, sample])
        del null_rows
        tmp = f"{path}.{os.getpid()}.tmp.npz"
        np.savez(tmp, bits=store.bits[first:last], sample=store.sample[first:last], check=np.asarray(batch_check))
        os.replace(tmp, path)
        state.mark_done(unit)
        check.extend(batch_check)
        emit("progress", stage="null maps", batch=number + 1, batches=len(batches), done=number + 1, total=len(batches), elapsed=round(time.monotonic() - started, 3), eta=None)
    return store, check


class RelocationPool:
    """The exact relocation pool of a cohort (:func:`build_relocation_pool`): K_pool relocations per lesion, mapped exactly
    and kept as bit-packed binary maps at the rule.

    ``store`` (:class:`ColumnStore`), ``columns`` (per lesion, the relocated (positions, weights)), ``shifts`` (per lesion,
    (k_l, 3)), ``counts`` and ``offsets`` (global column index of lesion l's j-th relocation: offsets[l] + j), ``info`` (design
    details) and ``check`` (per lesion, the relative difference between the observed map remapped in the null passes
    and the given observed map, or empty).
    """

    def __init__(self, store, columns, shifts, info, check):
        self.store = store
        self.columns = columns
        self.shifts = shifts
        self.info = info
        self.check = check
        self.counts = [len(c) for c in columns]
        self.offsets = np.concatenate(([0], np.cumsum(self.counts)))[:-1].astype(np.int64)

    def draw(self, k, seed=0):
        """(K, L) global column indices: one relocation per lesion, drawn at random for each null cohort."""
        return _draw_sets(_Context(k=int(k), seed=int(seed)), self.counts, self.offsets)

    def overlap(self, columns):
        """(n_parts, V) overlap counts of one null cohort (a row of :meth:`draw`)."""
        return self.store.counts(columns)


def build_relocation_pool(lesions, bundle, subjects=None, k_pool=DEFAULT_K_POOL, rule=None, sign="both", kind="rl_independent", seed=0, reference=None, strength="signed",
                          brain_mask=None, min_inside=None, budget_bytes=None, observed=None, qa_voxels=QA_VOXELS, chunk=None, gsr=None, emit=None, cancel=None):
    """The exact pooled null's relocations (addendum §3.6): K_pool relocations of each lesion mapped exactly at full V as
    extra seed columns of engine passes (batched by the cohortmaps memory budget), each kept only as its binary map at
    the rule (bit-packed). :func:`run` does the same for the cohort of a maps.tsv.

    :param lesions: (L, V) weights, or a list of (V,) vectors or (positions, weights) pairs, over the bundle voxels.
    :param subjects: the connectome subjects of the maps (None: all; an int; a list of ids), as the observed maps'.
    :param kind: "rl_independent", "rl_strength" (needs ``reference``) or "rl_cohort_shift".
    :param observed: optional (L, V) observed maps of the rule's statistic, checked against their remapping.
    :return: :class:`RelocationPool`.
    """
    import tempfile

    from . import cohortmaps, engine

    if kind not in ("rl_independent", "rl_strength", "rl_cohort_shift"):
        raise ValueError(f"The relocation pool is built for rl_independent, rl_strength or rl_cohort_shift, not {kind!r}")
    emit = emit or (lambda *args, **kwargs: None)
    rule = thresholds.normalize_rule(rule)
    ids = engine.resolve_subjects(bundle, subjects)
    pool_cohort = Cohort()
    pool_cohort.columns = _sparse_columns(lesions, bundle.n_voxels)
    pool_cohort.ids = [f"lesion-{index + 1}" for index in range(len(pool_cohort.columns))]
    pool_cohort.subject_ids = list(ids)
    sample = engine.null_voxel_sample(bundle.n_voxels, qa_voxels, seed=seed)
    with tempfile.TemporaryDirectory(prefix="lnm-nulls-") as work:
        context = _Context(kind=kind, rule=rule, sign=sign, seed=int(seed), k_pool=int(k_pool), strength=strength, n=len(ids), n_voxels=bundle.n_voxels, gsr=gsr,
                           statistic=rule_statistic(rule), budget_bytes=cohortmaps.default_budget_bytes() if budget_bytes is None else int(budget_bytes),
                           chunk=int(chunk or engine.DEFAULT_CHUNK), sample=np.arange(bundle.n_voxels) if sample is None else sample, work=work, resume=False, params_sha256=None)
        if rule["kind"] != "top_pct":
            resolve_rule(rule, len(ids), bundle.n_voxels, gsr)
        relocator = Relocator(pool_cohort.columns, bundle, brain_mask=brain_mask, min_inside=min_inside)
        columns, shifts, info = _relocation_columns(context, relocator, pool_cohort, reference, emit)
        store, check = _exact_store(context, bundle, pool_cohort, columns, observed, emit, _cancel_token(cancel))
    return RelocationPool(store, columns, shifts, info, check)


def _fast_maps(index, columns, chunk=64):
    """Approximate maps of lesion columns in chunks: yields (start, (c, V) float32 maps, (c, R) normalized parcel weights)."""
    from . import fastmap

    for start in range(0, len(columns), chunk):
        block = columns[start:start + chunk]
        m = fastmap.parcel_occupancy(block, index) if block else np.zeros((0, index.n_parcels))
        total = m.sum(axis=1, keepdims=True)
        weights = np.divide(m, total, out=np.zeros_like(m), where=total > 0).astype(np.float32)
        yield start, fastmap.approximate_maps(None, index, occupancy=m), weights


def _fast_store(context, index, columns, emit, cancel):
    n_columns = sum(len(c) for c in columns)
    flat = [column for lesion_columns in columns for column in lesion_columns]
    store = ColumnStore(n_columns, len(SIGN_PARTS[context.sign]), index.n_voxels, n_parcels=index.n_parcels)
    started = time.monotonic()
    for start, rows, weights in _fast_maps(index, flat):
        cancel.raise_if_cancelled(done=start)
        empty = np.array([column[0].size == 0 for column in flat[start:start + rows.shape[0]]], dtype=bool)
        store.put(start, _parts_of_rows(context, rows, empty), weights=weights)
        _emit_progress(emit, "null maps", start + rows.shape[0], n_columns, started)
    return store


def _group_mean_exact(store, sets, n_lesions, chunk_bytes=80 << 20):
    """(K, V') mean statistic map of each null cohort on the voxel sample (exact store)."""
    k = sets.shape[0]
    n_sample = store.sample.shape[1]
    out = np.zeros((k, n_sample), dtype=np.float32)
    step = max(1, chunk_bytes // max(n_lesions * n_sample * 4, 1))
    for start in range(0, k, step):
        block = store.sample[sets[start:start + step]].astype(np.float32)  # (c, L, V')
        out[start:start + step] = block.mean(axis=1)
    return out


def _group_qa(context, observed_overlap, observed_mean, null_overlap_sample, null_mean_sample, reference):
    """qa.group_qa of the overlap map and of the mean statistic map against the null cohorts' maps (voxel sample)."""
    from . import qa

    if reference is None:
        level, text = qa.NOT_CALIBRATED, "not calibrated (no reference maps for this connectome and subject list)"
        return {"available": False, "level": level, "text": text}
    out = {"available": True, "null_kind": context.kind, "voxels": int(context.sample.size)}
    for name, observed, nulls in (("overlap", observed_overlap, null_overlap_sample), ("mean", observed_mean, null_mean_sample)):
        try:
            out[name] = qa.group_qa(observed.astype(np.float64), reference, nulls, null_voxels=context.sample, null_kind=context.kind)
        except ValueError as exc:
            out[name] = {"level": qa.NOT_CALIBRATED, "text": f"not calibrated ({exc})"}
    out["statistic"] = "approximate Fz" if context.mode == "fast" else context.statistic
    return out


def _parity_for_fast(bundle, index, cohort, parity, reference, emit):
    """(report, source): the parity report of the fast index for this cohort (the given one, else computed here from the
    cohort's exact AvgFz maps).
    """
    from . import fastmap

    if isinstance(parity, (str, os.PathLike)):
        with open(parity, encoding="utf-8") as f:
            parity = json.load(f)
    if isinstance(parity, dict):
        recorded = (parity.get("index") or {}).get("sha256")
        if recorded not in (None, index.sha256):
            emit("warning", code="parity_index", message="The parity report belongs to another fast index; the parity is computed again from the cohort's exact maps.")
        else:
            if "gate" not in parity:
                parity = {**parity, "gate": fastmap.gate(parity)}
            return parity, "given"
    if any(path is None for path in cohort.maps["AvgFz"]):
        raise jobs.JobError("Fast mode needs a parity report (--parity) or the cohort's exact AvgFz maps to compute one", code="parity")
    exact = _load_rows(cohort.maps["AvgFz"], bundle, "AvgFz")
    approx = fastmap.approximate_maps(cohort.columns, index)
    report = fastmap.parity(exact, approx, reference, fastmap.lesion_strata(cohort.columns, index), fastmap.lesion_volumes_ml(cohort.columns, index), cohort.ids)
    report["index"] = {"sha256": index.sha256, "run_id": index.metadata.get("run_id"), "bundle": (index.metadata.get("bundle") or {}).get("name"), "subjects": index.metadata.get("subjects"), "parcels": index.n_parcels}
    report["source"] = "the cohort's exact AvgFz maps (cohortmaps)"
    report["created"] = _now_iso()
    for message in report["gate"]["messages"]:
        emit("warning", code="parity_gate", message=message)
    return report, "computed"


def _parity_summary(report, source):
    if report is None:
        return None
    gate = report.get("gate") or {}
    return {
        "source": source,
        "run_id": report.get("run_id"),
        "n": report.get("n"),
        "constants": gate.get("constants"),
        "allowed_strata": gate.get("allowed_strata"),
        "allowed_size_bins": gate.get("allowed_size_bins"),
        "strata": {key: {k: value.get(k) for k in ("n", "median_partial_r", "median_top10_dice", "passed", "reason")} for key, value in (gate.get("strata") or {}).items()},
        "size_bins": {key: {k: value.get(k) for k in ("n", "median_partial_r", "median_top10_dice", "passed", "reason")} for key, value in (gate.get("size_bins") or {}).items()},
        "file": PARITY_JSON if source == "computed" else None,
    }


def _mode_label(context):
    if context.mode == "fast":
        return APPROX_LABEL
    if context.kind == "rl_cohort_shift":
        return f"pooled exact null ({context.k} cohort shifts)"
    if context.kind == "empirical_pool":
        return f"pooled exact null ({context.n_pool} control lesions, {context.pool_matches} volume-matched per lesion)"
    return f"pooled exact null ({context.k_pool} per lesion)"


def _check_arguments(kind, mode, rule, k, k_pool, overlap, sign, strength, pool, pool_maps):
    if kind not in KINDS:
        raise ValueError(f"Unknown null kind {kind!r}; expected one of {', '.join(KINDS)}")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    if mode not in KIND_INFO[kind]["modes"]:
        raise ValueError(f"{kind} ({KIND_INFO[kind]['status']}) is available in {' or '.join(KIND_INFO[kind]['modes'])} mode only")
    if sign not in thresholds.SIGNS:
        raise ValueError(f"sign must be one of {', '.join(thresholds.SIGNS)}")
    if strength not in STRENGTH_MODES:
        raise ValueError(f"strength must be one of {', '.join(STRENGTH_MODES)}")
    low, high = OVERLAP_RANGE
    if not low <= float(overlap) <= high:
        raise ValueError(f"The overlap rule must be between {low:g} and {high:g} %, got {overlap}")
    if int(k) < 1 or int(k_pool) < 1:
        raise ValueError("K and K_pool must be at least 1")
    if mode == "fast" and int(k) > MAX_K_FAST:
        raise ValueError(f"Fast mode computes at most {MAX_K_FAST} null cohorts")
    if mode == "fast":
        if rule["kind"] != "top_pct":
            raise ValueError("Fast mode allows rank rules only: use a top-x% rule ({\"kind\": \"top_pct\", \"value\": 10})")
        if rule.get("statistic", "Fz") != "Fz":
            raise ValueError("Approximate maps have no T: the top-x% rule of fast mode ranks the approximate Fz (\"statistic\": \"Fz\")")
    if kind == "empirical_pool" and not (pool or pool_maps):
        raise ValueError("empirical_pool needs a control set: pool (a folder or lesions TSV) or pool_maps (a maps.tsv)")


def run(
    bundle,
    out_dir,
    maps_tsv=None,
    study=None,
    kind="rl_independent",
    mode="exact",
    k=DEFAULT_K,
    k_pool=None,
    rule=None,
    sign="both",
    overlap=DEFAULT_OVERLAP,
    seed=0,
    participants=None,
    group_column="group",
    strength="signed",
    iterations=ANNEAL_ITERATIONS,
    pool=None,
    pool_maps=None,
    pool_matches=POOL_MATCHES,
    index=None,
    parity=None,
    reference=None,
    brain_mask=None,
    min_inside=None,
    budget_bytes=None,
    cache=None,
    include_unreviewed=False,
    qa_voxels=QA_VOXELS,
    chunk=None,
    threads=None,
    cancel=None,
    resume=False,
    emit=None,
    run_root=None,
    parent_runs=(),
    register=True,
    argv=None,
):
    """Run the cohort null suite (module docstring) and write its outputs; return the nulls.json record.

    :param bundle: a ConnectomeBundle or its folder (the cohort's connectome).
    :param maps_tsv: the cohortmaps maps.tsv of the observed lesions; or ``study`` (mapped with cohortmaps into out_dir).
    :param kind: one of :data:`KINDS`; ``mode``: "exact" or "fast".
    :param k: null cohorts (exact cohort shift: the K_pool shifts); ``k_pool``: relocations per lesion (exact 20, fast 100).
    :param rule: threshold rule (default FWE; fast mode: a top_pct rule on Fz); ``sign`` and ``overlap`` as cohort.py.
    :param participants: participants.tsv whose ``group_column`` selects the cases (default: the study's).
    :param strength: rl_strength's map: "signed" (μC_r) or "abs" (hubness).
    :param iterations: rl_overlap's annealing iterations per null cohort.
    :param pool, pool_maps: empirical_pool's control set (lesion files, or a mapped maps.tsv); ``pool_matches`` candidates.
    :param index: fast index folder (default :func:`find_fast_index` of the cohort's subjects); ``parity``: a parity
      report (path or dict) of that index, else computed from the cohort's exact maps.
    :param reference: reference maps (default: the bundle's, for the cohort's subjects).
    :param brain_mask: NIfTI path or (V,) boolean vector the relocated lesions must stay in (default: the connectome mask).
    :param min_inside: fraction of a relocated lesion's weight that must stay inside the brain (default qa.MIN_INSIDE).
    :param budget_bytes: memory budget of one engine pass (default cohortmaps.default_budget_bytes()).
    :param qa_voxels: voxels of the group QA sample.
    :param cancel: ``jobs.CancelToken`` (or None); ``resume``: keep the engine batches of a stopped exact run.
    :param emit: event function (default ``jobs.emit``).
    :param run_root: folder whose derivatives/lnm/runs.jsonl records the run (default: the study, else ``out_dir``).
    """
    from . import cohortmaps, engine, export, fastmap
    from .connectome import ConnectomeBundle

    emit = emit or jobs.emit
    cancel = _cancel_token(cancel)
    started = time.monotonic()
    rule = thresholds.normalize_rule(rule)
    k_pool = int(k_pool or (DEFAULT_K_POOL if mode == "exact" else DEFAULT_K_POOL_FAST))
    _check_arguments(kind, mode, rule, k, k_pool, overlap, sign, strength, pool, pool_maps)
    if (maps_tsv is None) == (study is None):
        raise ValueError("Give maps_tsv or study (not both)")
    bundle = ConnectomeBundle.open(bundle) if isinstance(bundle, (str, os.PathLike)) else bundle
    out_dir = os.path.abspath(os.fspath(out_dir))
    os.makedirs(out_dir, exist_ok=True)
    budget_bytes = cohortmaps.default_budget_bytes() if budget_bytes is None else int(budget_bytes)
    on_the_fly = mode == "fast" and kind in ("rl_cohort_shift", "rl_overlap")
    stage_names = (["lesion maps"] if study is not None else []) + ["design"] + ([] if on_the_fly else ["null maps"]) + ["null cohorts", "inference", "write"]
    stage_index = {"value": 0}

    def stage(name, **fields):
        stage_index["value"] += 1
        emit("stage", name=name, index=stage_index["value"], total=len(stage_names), **fields)

    emit("start", job="nulls", argv=list(argv or []), threads=threads, kind=kind, mode=MODE_RECORDS[mode], status=KIND_INFO[kind]["status"])
    lock = jobs.OutputLock(os.path.join(out_dir, LOCK_NAME))
    lock.acquire()
    run_root = os.path.abspath(os.fspath(run_root or study or out_dir))
    run_id = None
    parents = list(parent_runs or ())
    work = os.path.join(out_dir, WORK_DIR)
    try:
        for desc in OUTPUT_DESCS:  # never leave the outputs of an earlier run next to this one's
            path = export.group_map_path(out_dir, bundle.name, desc)
            for name in (path, _sidecar_path(path)):
                if os.path.exists(name):
                    os.remove(name)
        for name in (NULLS_JSON, SETS_NAME, NULL_GROUP_FILE, NULL_GROUP_VOXELS, PARITY_JSON):
            if os.path.exists(os.path.join(out_dir, name)):
                os.remove(os.path.join(out_dir, name))
        if not resume:
            shutil.rmtree(work, ignore_errors=True)
        if study is not None:
            stage("lesion maps")
            participants = participants or os.path.join(os.fspath(study), "participants.tsv")
            maps_tsv, mapped_run = _mapped_study(study, bundle, out_dir, cache, threads, cancel, resume, budget_bytes, include_unreviewed, run_root, parents, argv, emit)
            if mapped_run:
                parents.append(mapped_run)
        cohort = load_cohort(maps_tsv, bundle, participants, group_column, emit)
        if cohort.run_id and cohort.run_id not in parents:
            parents.append(cohort.run_id)
        if len(cohort) < 2:
            raise jobs.JobError(f"The nulls need at least 2 lesions; {len(cohort)} can be used", code="no_lesions")
        if not cohort.subject_ids:
            raise jobs.JobError("cohortmaps.json does not list the connectome subjects of the maps", code="input")
        unknown = sorted(set(cohort.subject_ids) - set(bundle.subject_ids))
        if unknown:
            raise jobs.JobError(f"{len(unknown)} subject(s) of the maps are not in the connectome {bundle.name!r}, for example {unknown[0]!r}", code="input")
        n = int(cohort.n or len(cohort.subject_ids))
        statistic = rule_statistic(rule) if mode == "exact" else "Fz"
        info = export.bundle_info(bundle)
        gsr = info.get("gsr")
        try:
            resolved = resolve_rule(rule, n, bundle.n_voxels, gsr) if mode == "exact" else None
        except ValueError as exc:
            raise jobs.JobError(str(exc), code="rule") from None
        if isinstance(brain_mask, (str, os.PathLike)):
            brain_mask_vector = engine._brain_mask_vector(os.fspath(brain_mask), bundle)
        else:
            brain_mask_vector = None if brain_mask is None else np.asarray(brain_mask, dtype=bool)
        if reference is None:
            from .qa import load_reference

            reference = load_reference(bundle, subjects_sha=cohort.subject_ids_sha256) or load_reference(bundle)
        sample = engine.null_voxel_sample(bundle.n_voxels, qa_voxels, seed=seed)
        sample = np.arange(bundle.n_voxels) if sample is None else sample
        context = _Context(kind=kind, mode=mode, rule=rule, sign=sign, overlap=float(overlap), seed=int(seed), k=int(k), k_pool=k_pool, strength=strength, n=n,
                           n_voxels=bundle.n_voxels, gsr=gsr, statistic=statistic, budget_bytes=budget_bytes, chunk=int(chunk or engine.DEFAULT_CHUNK), sample=sample,
                           work=work, resume=bool(resume), n_pool=0, pool_matches=int(pool_matches))
        context.params_sha256 = runs.params_sha256({
            "kind": kind, "mode": mode, "rule": rule, "sign": sign, "k_pool": k_pool, "seed": int(seed), "strength": strength, "subjects": cohort.subject_ids_sha256,
            "lesions": [[sid, key or sha] for sid, key, sha in zip(cohort.ids, cohort.keys, cohort.lesion_sha256, strict=True)], "budget": budget_bytes,
            "brain_mask": None if brain_mask_vector is None else runs.params_sha256(np.flatnonzero(brain_mask_vector).tolist()), "min_inside": min_inside,
        })

        # Fast mode: the index, its parity gate, and the lesions it allows
        fast_index, parity_report, parity_source = None, None, None
        if mode == "fast":
            index_path = index or find_fast_index(bundle, cohort.subject_ids)
            if index_path is None:
                raise jobs.JobError("No fast index for this connectome and subject list: build it with 'python -m LesionNetworkMappingLib.fastmap build', or use exact mode", code="no_index")
            try:
                fast_index = fastmap.load_index(index_path, bundle)
            except (OSError, ValueError) as exc:
                raise jobs.JobError(f"The fast index cannot be used: {exc}", code="no_index") from None
            if (fast_index.metadata.get("subjects") or {}).get("sha256") != cohort.subject_ids_sha256:
                emit("warning", code="index_subjects", message="The fast index was built from other connectome subjects than the observed maps; the parity gate compares them.")
            parity_report, parity_source = _parity_for_fast(bundle, fast_index, cohort, parity, reference, emit)
            keep, gate_excluded = [], []
            for position, (sid, stratum, volume) in enumerate(zip(cohort.ids, fastmap.lesion_strata(cohort.columns, fast_index), cohort.volume_ml, strict=True)):
                allowed, message = fastmap.lesion_allowed(parity_report, stratum, volume)
                if allowed:
                    keep.append(position)
                else:
                    gate_excluded.append({"participant_id": sid, "stratum": stratum, "volume_ml": volume, "reason": message})
            if gate_excluded:
                sids = [item["participant_id"] for item in gate_excluded]
                emit("warning", code="parity_gate", participants=sids, message=f"{len(sids)} lesion(s) are left out of the fast-mode nulls by the parity gate "
                     f"({', '.join(sids[:10])}{' ...' if len(sids) > 10 else ''}): {gate_excluded[0]['reason']}")
            if len(keep) < 2:
                raise jobs.JobError(f"The parity gate allows fast mode for {len(keep)} of {len(cohort)} lesions: use exact mode (pooled exact null)", code="parity_gate")
            if len(keep) < len(cohort):
                cohort = cohort.subset(keep)
                cohort.excluded.extend(gate_excluded)
            if parity_report.get("run_id") and parity_report["run_id"] not in parents:
                parents.append(parity_report["run_id"])
        if register:
            run_id = runs.register(run_root, "nulls", out_dir, parents=parents,
                                   params={"kind": kind, "mode": MODE_RECORDS[mode], "k": int(k), "k_pool": k_pool, "rule": rule, "sign": sign, "overlap": float(overlap),
                                           "strength": strength if kind == "rl_strength" else None, "design_sha256": context.params_sha256, "subject_ids_sha256": cohort.subject_ids_sha256},
                                   seeds={"null": int(seed)})
        n_lesions = len(cohort)
        n_parts = len(SIGN_PARTS[sign])
        estimate_info = estimate_details(n_lesions, bundle, k_pool, mode, int(k), kind, subjects=list(cohort.subject_ids), threads=threads, budget_bytes=budget_bytes, rule=rule)
        emit("result", kind="plan", lesions=n_lesions, subjects=n, k=int(k), k_pool=k_pool, statistic=statistic, estimate_s=round(estimate_info["seconds"], 1))

        # Observed maps: the cohortmaps maps (exact) or the approximate maps of the same lesions (fast), with the same rule as the nulls
        if mode == "exact":
            column_name = "T" if statistic == "T" else "AvgFz"
            observed_rows = _load_rows(cohort.maps[column_name], bundle, column_name)
        else:
            observed_rows = fastmap.approximate_maps(cohort.columns, fast_index)
        observed_parts = _parts_of_rows(context, observed_rows).sum(axis=0, dtype=np.int32)
        observed_mean = observed_rows.mean(axis=0)
        relocator = Relocator(cohort.columns, bundle, brain_mask=brain_mask_vector, min_inside=min_inside)

        # The null design and the binary maps of the null columns
        stage("design", kind=kind)
        design = {"kind": kind}
        store, sets, observed_check, shifts, shared_shifts = None, None, None, None, None
        if kind == "empirical_pool":
            if mode == "exact":
                stage("null maps", columns="control lesions")
            pool_cohort = _pool_cohort(pool, pool_maps, bundle, out_dir, cohort, mode, cache, threads, cancel, budget_bytes, run_root, emit)
            own = {sha for sha in cohort.lesion_sha256 if sha}
            keep = [i for i, sha in enumerate(pool_cohort.lesion_sha256) if not sha or sha not in own]
            if len(keep) < len(pool_cohort):
                emit("warning", code="pool", message=f"{len(pool_cohort) - len(keep)} control lesion(s) are also cohort lesions; they are left out of the pool.")
                pool_cohort = pool_cohort.subset(keep)
            if len(pool_cohort) == 0:
                raise jobs.JobError("The control pool has no usable lesion", code="input")
            if mode == "exact" and pool_cohort.subject_ids_sha256 not in (None, cohort.subject_ids_sha256):
                raise jobs.JobError("The control maps were computed from other connectome subjects than the cohort's", code="input")
            if pool_cohort.run_id and pool_cohort.run_id not in parents:
                parents.append(pool_cohort.run_id)
                if run_id:
                    runs.update(run_root, run_id, parents=list(parents))
            context.n_pool = len(pool_cohort)
            sets, candidates = volume_matched_sets(cohort.volume_ml, pool_cohort.volume_ml, context.k, pool_matches, seed)
            design.update({"pool": len(pool_cohort), "matches": int(candidates.shape[1]), "matching": "nearest in log volume"})
            if mode == "exact":
                pool_rows = _load_rows(pool_cohort.maps["T" if statistic == "T" else "AvgFz"], bundle, "control maps")
                store = ColumnStore(len(pool_cohort), n_parts, bundle.n_voxels, n_sample=sample.size)
                store.put(0, _parts_of_rows(context, pool_rows), sample=pool_rows[:, sample])
                del pool_rows
            else:
                stage("null maps", columns=len(pool_cohort))
                store = _fast_store(context, fast_index, [[column] for column in pool_cohort.columns], emit, cancel)
        elif on_the_fly:
            if kind == "rl_cohort_shift":
                shared_shifts, shift_info = cohort_shifts(relocator, context.k, seed)
                if len(shared_shifts) == 0:
                    raise jobs.JobError("No shared shift keeps the cohort inside the brain; use rl_independent", code="no_shift")
                if len(shared_shifts) < context.k:
                    emit("warning", code="shifts", message=f"Only {len(shared_shifts)} of {context.k} shared shifts keep the cohort inside the brain: K = {len(shared_shifts)}.")
                context.k = len(shared_shifts)
                design.update({"shifts": len(shared_shifts), "attempts": shift_info["attempts"], "cohort_inside_median": float(np.median(shift_info["inside"])),
                               "min_lesion_inside_median": float(np.median(shift_info["min_lesion_inside"]))})
            else:
                design.update({"iterations": int(iterations), "edges": list(DICE_EDGES), "binarize_at": DICE_BINARIZE_AT, "temperatures": [ANNEAL_T0, ANNEAL_T1]})
        else:
            columns, shifts, columns_info = _relocation_columns(context, relocator, cohort, reference, emit)
            design.update(columns_info)
            counts = [len(c) for c in columns]
            if min(counts) == 0:
                bad = [sid for sid, count in zip(cohort.ids, counts, strict=True) if count == 0]
                raise jobs.JobError(f"No relocation of {', '.join(bad[:5])} fits inside the brain: these lesions cannot take part in the nulls", code="relocations")
            offsets = np.concatenate(([0], np.cumsum(counts)))[:-1]
            if kind == "rl_cohort_shift":
                context.k = min(counts)
                sets = np.vstack([offsets + b for b in range(context.k)])
            else:
                sets = _draw_sets(context, counts, offsets)
            design["relocations_per_lesion"] = counts
            stage("null maps", columns=int(sum(counts)))
            if mode == "exact":
                store, observed_check = _exact_store(context, bundle, cohort, columns, observed_rows, emit, cancel)
            else:
                store = _fast_store(context, fast_index, columns, emit, cancel)
            del columns

        # The null cohorts
        k_sets = context.k
        dtype = np.uint8 if n_lesions < 256 else np.uint16
        accumulator = NullOverlap(observed_parts, n_lesions, overlap, sign, k_sets, store=_work_store(context, (k_sets, bundle.n_voxels), dtype))
        stage("null cohorts", k=k_sets)
        set_started = time.monotonic()
        null_mean = np.zeros((k_sets, sample.size), dtype=np.float32)
        anneal_ks, set_shifts = [], []
        p_sample = np.asarray(fast_index.P[:, sample], dtype=np.float32) if fast_index is not None else None
        last = 0.0
        for b in range(k_sets):
            if b % 8 == 0:
                cancel.raise_if_cancelled(done=b)
            if on_the_fly:
                if kind == "rl_cohort_shift":
                    set_columns = apply_shift(relocator, shared_shifts[b])
                else:
                    annealed = anneal_overlap(relocator, iterations, seed=lesion_seed(seed, b, stream=6))
                    set_columns = annealed["columns"]
                    anneal_ks.append((annealed["ks_initial"], annealed["ks_best"]))
                    set_shifts.append(annealed["shifts"])
                parts_counts = np.zeros((n_parts, bundle.n_voxels), dtype=np.int32)
                means = np.zeros(sample.size)
                for _start, rows, weights in _fast_maps(fast_index, set_columns):
                    empty = weights.sum(axis=1) == 0
                    parts_counts += _parts_of_rows(context, rows, empty).sum(axis=0, dtype=np.int32)
                    means += weights.sum(axis=0) @ p_sample
                null_mean[b] = means / n_lesions
            else:
                parts_counts = store.counts(sets[b])
                if store.weights is not None:
                    null_mean[b] = (store.weights[sets[b]].sum(axis=0) / n_lesions) @ p_sample
            accumulator.add(parts_counts)
            now = time.monotonic()
            if now - last > 0.25 or b + 1 == k_sets:
                last = now
                _emit_progress(emit, "null cohorts", b + 1, k_sets, set_started)
        if store is not None and store.sample is not None:
            null_mean = _group_mean_exact(store, sets, n_lesions)
        stage("inference")
        result = accumulator.finish(cancel)
        null_overlap_sample = np.asarray(accumulator.store[:, sample], dtype=np.float32)
        group_qa = _group_qa(context, observed_parts.sum(axis=0), observed_mean, null_overlap_sample, null_mean, reference)
        del accumulator
        if kind == "rl_overlap":
            ks = np.asarray(anneal_ks)
            design.update({"ks_initial_median": float(np.median(ks[:, 0])), "ks_best_median": float(np.median(ks[:, 1])),
                           "observed_dice_mean": float(pairwise_dice(cohort.columns, bundle.n_voxels).mean())})
        if observed_check is not None:
            worst = max(observed_check) if observed_check else None
            design["observed_check"] = {"max_relative_difference": worst, "note": "the observed lesions remapped in the null passes against their cohortmaps maps (same engine and subjects)"}
            if worst is not None and worst > 1e-3:
                emit("warning", code="observed_check", message=f"The observed maps differ from their remapping in the null passes by up to {worst:.2g} (relative): check that maps.tsv belongs to this connectome.")

        # Outputs
        stage("write")
        record = _write_outputs(context, bundle, info, out_dir, cohort, result, group_qa, design, parity_report, parity_source, sets, shifts, shared_shifts, set_shifts,
                                null_mean, resolved, maps_tsv, run_id, parents, argv, started, fast_index, parity, pool, pool_maps, brain_mask)
        outputs = record.pop("outputs_list")
        shutil.rmtree(work, ignore_errors=True)
        numbers = result["numbers"]
        if run_id:
            runs.update(run_root, run_id, status="done", ended=record["created"], outputs=outputs,
                        summary={"kind": kind, "mode": MODE_RECORDS[mode], "k": k_sets, "n_lesions": n_lesions, "n_fwe_05": numbers["n_fwe_05"],
                                 "min_attainable_p": numbers["min_attainable_p"], "classic_rule": numbers["classic_rule"]["text"]})
        emit("done", outputs=[os.path.join(out_dir, name) for name in outputs], elapsed=round(time.monotonic() - started, 3), run_id=run_id, k=k_sets,
             mode=MODE_RECORDS[mode], classic_rule=numbers["classic_rule"]["text"], min_attainable_p=numbers["min_attainable_p"])
        return record
    except jobs.Cancelled:
        if run_id:
            runs.update(run_root, run_id, status="cancelled", ended=_now_iso())
        raise
    except BaseException as exc:
        if run_id:
            try:
                runs.update(run_root, run_id, status="failed", ended=_now_iso(), error=f"{type(exc).__name__}: {exc}")
            except Exception:
                pass
        raise
    finally:
        lock.release()


def _sidecar_path(map_path):
    return map_path[: -len(".nii.gz")] + ".json" if map_path.endswith(".nii.gz") else os.path.splitext(map_path)[0] + ".json"


def _write_outputs(context, bundle, info, out_dir, cohort, result, group_qa, design, parity_report, parity_source, sets, shifts, shared_shifts, set_shifts, null_mean,
                   resolved, maps_tsv, run_id, parents, argv, started, fast_index, parity, pool, pool_maps, brain_mask):
    """Write the maps (exact mask header), their sidecars, nulls.json, the null design and the null group maps."""
    from . import export, nifti
    from .cohort import mask_geometry
    from .glm import _write_on_mask

    mask_path = os.path.join(bundle.path, "mask.nii.gz")
    raw = nifti.read_raw_header(mask_path)
    indices, shape, _affine = mask_geometry(mask_path)
    numbers = result["numbers"]
    mode_record = MODE_RECORDS[context.mode]
    label = _mode_label(context)
    parity_summary = _parity_summary(parity_report, parity_source)
    terms = {"licence": info.get("licence"), "terms_sha256": info.get("terms_sha256"), "acknowledgement": info.get("acknowledgement")}
    common = {
        "kind": context.kind,
        "kind_status": KIND_INFO[context.kind]["status"],
        "mode": mode_record,
        "mode_label": label,
        "approximate": context.mode == "fast",
        "k": numbers["k"],
        "k_pool": context.k_pool if context.kind not in ("empirical_pool",) and not (context.mode == "fast" and context.kind in ("rl_cohort_shift", "rl_overlap")) else None,
        "min_attainable_p": numbers["min_attainable_p"],
        "min_attainable_q": numbers["min_attainable_q"],
        "fdr_floor_voxels": numbers["fdr_floor_voxels"],
        "rule": context.rule,
        "statistic": context.statistic if context.mode == "exact" else "approximate Fz",
        "sign": context.sign,
        "overlap_pct": context.overlap,
        "n_lesions": len(cohort),
        "n_subjects": context.n,
        "caption": CAPTION,
        "disclaimer": export.DISCLAIMER,
        "terms": terms,
        "run_id": run_id,
    }
    if parity_summary is not None:
        common["parity"] = {key: parity_summary[key] for key in ("source", "constants", "allowed_strata", "allowed_size_bins", "file")}
    label_text = export.connectome_label(bundle.name)
    outputs = []
    for desc in OUTPUT_DESCS:
        path = export.group_map_path(out_dir, bundle.name, desc)
        fill = 1.0 if desc in ("sensNullP", "sensNullQ", "sensNullPFwe") else 0.0
        _write_on_mask(path, result["maps"][desc], raw, indices, shape, fill=fill, descrip=f"LNM nulls {desc} {'approx' if context.mode == 'fast' else 'exact'} K={numbers['k']}")
        sidecar = {"format": SIDECAR_FORMAT, "version": FORMAT_VERSION, "map": desc, "description": DESCRIPTIONS[desc], "connectome": label_text, **common, "nulls_json": NULLS_JSON}
        jobs.write_json_atomic(_sidecar_path(path), sidecar)
        outputs += [os.path.basename(path), os.path.basename(_sidecar_path(path))]
    # The null design: the columns each null cohort drew (rows of the relocation shifts, per lesion) and the shifts
    design_arrays = {"sets": np.asarray(sets if sets is not None else np.zeros((0, 0)), dtype=np.int32)}
    if shared_shifts is not None:
        design_arrays["shared_shifts"] = np.asarray(shared_shifts, dtype=np.int16).reshape(-1, 3)
    if shifts:
        design_arrays["shifts"] = np.concatenate([np.asarray(item, dtype=np.int16).reshape(-1, 3) for item in shifts])
        design_arrays["shift_counts"] = np.asarray([len(item) for item in shifts], dtype=np.int32)
    if set_shifts:
        design_arrays["set_shifts"] = np.asarray(set_shifts, dtype=np.int16)
    sets_tmp = os.path.join(out_dir, f".{SETS_NAME}.{os.getpid()}.tmp.npz")
    np.savez_compressed(sets_tmp, **design_arrays)
    os.replace(sets_tmp, os.path.join(out_dir, SETS_NAME))
    outputs.append(SETS_NAME)
    group_tmp = os.path.join(out_dir, f".{NULL_GROUP_FILE}.{os.getpid()}.tmp.npy")
    np.save(group_tmp, np.clip(null_mean, -65504.0, 65504.0).astype(np.float16))
    os.replace(group_tmp, os.path.join(out_dir, NULL_GROUP_FILE))
    voxels_tmp = os.path.join(out_dir, f".{NULL_GROUP_VOXELS}.{os.getpid()}.tmp.npy")
    np.save(voxels_tmp, context.sample.astype(np.int64))
    os.replace(voxels_tmp, os.path.join(out_dir, NULL_GROUP_VOXELS))
    outputs += [NULL_GROUP_FILE, NULL_GROUP_VOXELS]
    if parity_source == "computed" and parity_report is not None:
        jobs.write_json_atomic(os.path.join(out_dir, PARITY_JSON), parity_report)
        outputs.append(PARITY_JSON)
    created = _now_iso()
    inputs = {
        "maps_tsv": maps_tsv,
        "cohortmaps_run": cohort.run_id,
        "subject_ids_sha256": cohort.subject_ids_sha256,
        "index": getattr(fast_index, "path", None),
        "index_sha256": getattr(fast_index, "sha256", None),
        "parity": os.fspath(parity) if isinstance(parity, (str, os.PathLike)) else None,
        "pool": os.fspath(pool) if pool else None,
        "pool_maps": os.fspath(pool_maps) if pool_maps else None,
        "brain_mask": os.fspath(brain_mask) if isinstance(brain_mask, (str, os.PathLike)) else None,
    }
    record = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "run_id": run_id,
        "created": created,
        "elapsed": round(time.monotonic() - started, 3),
        **common,
        "label": label,
        "description": KIND_INFO[context.kind]["description"],
        "preserves": KIND_INFO[context.kind]["preserves"],
        "citation": KIND_INFO[context.kind]["citation"],
        "seeds": {"seed": context.seed, "relocations": "SeedSequence([seed, 1, lesion])", "sets": "SeedSequence([seed, 2])", "qa_sample": context.seed},
        "observed": {
            "maps": "the cohortmaps maps of the same connectome subjects (exact engine)" if context.mode == "exact" else "recomputed in fast mode from the same index (like with like)",
            "same_rule": True,
            "rule_resolved": resolved.to_dict() if resolved is not None else {"kind": "top_pct", "note": "resolved per map (rank rule)"},
        },
        "participants": list(cohort.ids),
        "excluded": cohort.excluded,
        "design": design,
        "inference": {key: value for key, value in numbers.items() if key != "classic_rule"},
        "classic_rule": numbers["classic_rule"],
        "bias_map": {"desc": "sensNullBias", "mean": float(result["maps"]["sensNullBias"].mean()), "max": float(result["maps"]["sensNullBias"].max())},
        "group_qa": group_qa,
        "null_group_maps": {"file": NULL_GROUP_FILE, "voxels": NULL_GROUP_VOXELS, "statistic": common["statistic"], "shape": [int(null_mean.shape[0]), int(null_mean.shape[1])],
                            "note": "mean map of the rule's statistic of each null cohort on a voxel sample (qa.group_qa null_group_maps with null_voxels)"},
        "parity": parity_summary if parity_report is None else {**parity_summary, "excluded": (parity_report.get("gate") or {}).get("excluded"), "messages": (parity_report.get("gate") or {}).get("messages")},
        "connectome": {"bundle": info, "label": export.connectome_label(bundle.name)},
        "inputs": inputs,
        "parents": list(parents),
        "argv": list(argv or []),
        "versions": export.versions(),
        "outputs": {desc: os.path.basename(export.group_map_path(out_dir, bundle.name, desc)) for desc in OUTPUT_DESCS} | {"sets": SETS_NAME, "null_group_maps": NULL_GROUP_FILE},
    }
    if context.mode == "fast":
        record["note"] = "Fast-mode maps are approximations: never report them as final observed maps."
    portable = export.portable_record_paths(record, out_dir, ("inputs.maps_tsv", "inputs.index", "inputs.parity", "inputs.pool", "inputs.pool_maps", "inputs.brain_mask", "connectome.bundle.path"))
    jobs.write_json_atomic(os.path.join(out_dir, NULLS_JSON), portable)
    outputs.append(NULLS_JSON)
    portable["outputs_list"] = outputs
    return portable


def load_null_group_maps(out_dir):
    """(null group maps (K, V') float32, voxel indices (V',), statistic) of a nulls run folder, for qa.group_qa."""
    with open(os.path.join(out_dir, NULLS_JSON), encoding="utf-8") as f:
        record = json.load(f)
    item = record.get("null_group_maps") or {}
    maps = np.load(os.path.join(out_dir, item.get("file", NULL_GROUP_FILE)), allow_pickle=False).astype(np.float32)
    voxels = np.load(os.path.join(out_dir, item.get("voxels", NULL_GROUP_VOXELS)), allow_pickle=False)
    return maps, voxels, item.get("statistic")


#
# Command line
#


def build_parser():
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.nulls", description="Cohort null suite: null-referenced sensitivity, the classic rule's false-positive rate and bias map.")
    jobs.add_common_arguments(parser)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--maps", default=None, help="maps.tsv of a cohortmaps run (the observed lesion maps)")
    source.add_argument("--study", default=None, help="study folder: map its analysable lesions with cohortmaps first")
    parser.add_argument("--bundle", required=True, help="connectome bundle (.lnmc) of the maps")
    parser.add_argument("--out", required=True, help="output folder")
    parser.add_argument("--kind", choices=KINDS, default="rl_independent", help="null kind (default: rl_independent)")
    parser.add_argument("--mode", choices=MODES, default="exact", help="exact (pooled exact null) or fast (approximate, parcel index); default exact")
    parser.add_argument("--k", type=jobs.positive_int, default=DEFAULT_K, help=f"null cohorts (default {DEFAULT_K}; fast mode at most {MAX_K_FAST}; exact cohort shift: K_pool)")
    parser.add_argument("--k-pool", type=jobs.positive_int, default=None, help=f"relocations per lesion (default {DEFAULT_K_POOL} exact, {DEFAULT_K_POOL_FAST} fast)")
    parser.add_argument("--rule", default=None, help="threshold rule as JSON (default FWE; fast mode needs a top_pct rule)")
    parser.add_argument("--sign", choices=thresholds.SIGNS, default="both", help="both (|map| >= t), pos or neg (default: both)")
    parser.add_argument("--overlap", type=float, default=DEFAULT_OVERLAP, help=f"classic rule: percent of the maps that must pass (50-100; default {DEFAULT_OVERLAP:g})")
    parser.add_argument("--seed", type=int, default=0, help="random seed of the relocations and the null cohorts")
    parser.add_argument("--participants", default=None, help="participants.tsv with the group column (the overlap is over the cases)")
    parser.add_argument("--group-column", default="group", help="column with case / control (default: group)")
    parser.add_argument("--strength", choices=STRENGTH_MODES, default="signed", help="rl_strength: signed mean connectivity (μC_r, default) or absolute hubness")
    parser.add_argument("--iterations", type=jobs.positive_int, default=ANNEAL_ITERATIONS, help=f"rl_overlap: annealing iterations per null cohort (default {ANNEAL_ITERATIONS})")
    pool_group = parser.add_mutually_exclusive_group()
    pool_group.add_argument("--pool", default=None, help="empirical_pool: control lesions (a folder of NIfTI files or a lesions TSV)")
    pool_group.add_argument("--pool-maps", default=None, help="empirical_pool: maps.tsv of the mapped control lesions")
    parser.add_argument("--pool-matches", type=jobs.positive_int, default=POOL_MATCHES, help=f"empirical_pool: volume-matched candidates per lesion (default {POOL_MATCHES})")
    parser.add_argument("--index", default=None, help="fast mode: fast index folder (default: the bundle's index of the maps' subjects)")
    parser.add_argument("--parity", default=None, help="fast mode: parity.json of the index (default: computed from the cohort's exact maps)")
    parser.add_argument("--reference", default=None, help="reference maps folder (default: the bundle's, for the maps' subjects)")
    parser.add_argument("--brain-mask", default=None, help="brain mask NIfTI that relocated lesions must stay in (default: the connectome mask)")
    parser.add_argument("--min-inside", type=float, default=None, help="fraction of a relocated lesion's weight that must stay inside the brain (default 0.95)")
    parser.add_argument("--budget-gb", type=float, default=None, help="memory budget of one engine pass in GB")
    parser.add_argument("--cache", default=None, help="cohortmaps cache (with --study or --pool)")
    parser.add_argument("--include-unreviewed", action="store_true", help="--study: also map lesions that review has not approved")
    parser.add_argument("--qa-voxels", type=jobs.positive_int, default=QA_VOXELS, help=f"voxel sample of the group QA (default {QA_VOXELS})")
    parser.add_argument("--chunk", type=jobs.positive_int, default=None, help="voxels per matrix product")
    parser.add_argument("--estimate", action="store_true", help="only report the estimated run time (a result event) and exit")
    return parser


def _parse_rule(text):
    if not text:
        return thresholds.normalize_rule(None)
    try:
        return thresholds.rule_from_json(text)
    except ValueError as exc:
        raise jobs.JobError(f"Invalid --rule: {exc}", code="usage", exit_code=jobs.EXIT_USAGE) from None


def main(argv=None):
    """Command-line entry point (see the module docstring); returns the exit code."""
    from . import cohortmaps

    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    rule = _parse_rule(args.rule)
    if args.min_inside is not None and not 0.0 < args.min_inside <= 1.0:
        parser.error("--min-inside must be in (0, 1]")
    budget = cohortmaps._budget_bytes(args.budget_gb)
    try:
        _check_arguments(args.kind, args.mode, rule, args.k, args.k_pool or 1, args.overlap, args.sign, args.strength, args.pool, args.pool_maps)
    except ValueError as exc:
        raise jobs.JobError(str(exc), code="usage", exit_code=jobs.EXIT_USAGE) from None
    if args.estimate:
        if not args.maps:
            parser.error("--estimate needs --maps")
        rows = cohortmaps.read_maps_tsv(args.maps)
        record = cohortmaps.read_record(os.path.join(os.path.dirname(os.path.abspath(args.maps)), cohortmaps.COHORTMAPS_JSON)) or {}
        details = estimate_details(len(rows), args.bundle, args.k_pool, args.mode, args.k, args.kind, subjects=record.get("subject_ids"), threads=args.threads, budget_bytes=budget, rule=rule)
        details["null_kind"] = details.pop("kind")
        jobs.emit("result", kind="estimate", **details)
        jobs.emit("done", outputs=[], elapsed=round(jobs.elapsed(), 3), run_id=None, seconds=details["seconds"])
        return jobs.EXIT_OK
    reference = None
    if args.reference:
        from .reference import load_reference

        reference = load_reference(args.bundle, path=args.reference)
        if reference is None:
            raise jobs.JobError(f"No valid reference maps in {args.reference}", code="no_reference")
    cancel = jobs.CancelToken(args.cancel_file)
    try:
        run(args.bundle, args.out, maps_tsv=args.maps, study=args.study, kind=args.kind, mode=args.mode, k=args.k, k_pool=args.k_pool, rule=rule, sign=args.sign,
            overlap=args.overlap, seed=args.seed, participants=args.participants, group_column=args.group_column, strength=args.strength, iterations=args.iterations,
            pool=args.pool, pool_maps=args.pool_maps, pool_matches=args.pool_matches, index=args.index, parity=args.parity, reference=reference,
            brain_mask=args.brain_mask, min_inside=args.min_inside, budget_bytes=budget, cache=args.cache, include_unreviewed=args.include_unreviewed,
            qa_voxels=args.qa_voxels, chunk=args.chunk, threads=args.threads, cancel=cancel, resume=args.resume, run_root=args.run_root, parent_runs=args.parent_run, argv=argv)
    except (jobs.Cancelled, jobs.JobError):
        raise
    except (OSError, ValueError, KeyError) as exc:
        raise jobs.JobError(f"{type(exc).__name__}: {exc}", code="input") from None
    return jobs.EXIT_OK


if __name__ == "__main__":
    jobs.run_job(main, job="nulls")

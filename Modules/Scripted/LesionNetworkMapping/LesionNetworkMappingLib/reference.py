"""Connectome reference maps, specification ``lnm-reference-v1`` (addendum §3.7), with their builder CLI and loader.

The reference maps describe what every network map of a connectome shares, whatever the lesion:
the mean connectivity profile, its spread and its leading spatial modes. Connectome-confound QA
(qa.py), the nulls, validation, the fast parcel index and the methods text all use these maps, so
they are computed once per bundle and subject list under one versioned definition (``DEFINITION``).

Definitions (r is the engine's Pearson r of subject s averaged over the subject's runs; V voxels)::

    mu_r       exact     For each voxel v: the mean over subjects of the mean over runs of
                         mean_{u != v} r_run(v, u). Rows are demeaned and unit-norm per run, so
                         sum_{u != v} r_run(v, u) = x_v . g - x_v . x_v with g = sum_u x_u (one GEMV per run).
                         Voxels with zero variance in a run are left out of that run's mean (V' counts the
                         non-zero rows) and contribute 0 for that run. Label: "mean r profile (voxelwise)".
    mu_z       sampled   Mean over K sampled seed voxels u != v of the mean over subjects of atanh r_s(v, u)
                         (r clipped to +/-RMAX as in the engine).
    sigma_r    sampled   Standard deviation (ddof 1) over the K sampled seed columns u != v of the
                         group-mean r(v, u).
    hub_abs_z  sampled   "Hubness": the bundle's derived/degree.npy (mean |Fz| to its seeds) when present,
                         otherwise the mean over the same K seeds and the subjects of |atanh r_s(v, u)|.
    pcs        randomized  The k (default 3) leading left singular vectors of the column-centred group
                         r-matrix C~ = C - 1 mu^T, with C = mean_s mean_runs X_run X_run^T (V x V, never formed)
                         and mu the column means of C: the PCA scores of the voxels with their connectivity
                         profiles as features. Unit norm, signs chosen so that each correlates positively
                         with mu_r. eigvals = sigma^2 / (V - 1).
    ev1        randomized  Optional: the leading eigenvector of the uncentred C (Zaugg & Litvak "mode 1";
                         close to mu_r), unit norm, oriented like the PCs.

The sampled seeds are ``numpy.random.default_rng(seed).choice(V, K, replace=False)`` sorted (as
``connectome.compute_degree``), and each seed's time course is normalized per run as the engine does
for a one-voxel lesion. The PCs use randomized block subspace iteration (Halko et al. 2011, Alg. 4.4):
Omega = default_rng(seed + 1).standard_normal((V, k + oversample)), Y = C~ Omega, then ``power_iters``
times Q = qr(C~^T Q), Q = qr(C~ Q), and finally B = Q^T C~ and a small SVD. Matrix-free:
C Q = mean_s (1 / R_s) X_s (X_s^T Q), C~ q = C q - 1 (mu . q) and C~^T p = C p - mu (1^T p). EV1 uses
the same iteration on C with Omega_ev = default_rng(seed + 2).standard_normal((V, 1 + oversample));
its columns ride along in the same passes. The singular value estimates of every step are kept in
reference.json ("convergence") so that the accuracy of a build on real data can be judged.

Cost and memory: pass 1 ("maps") reads every subject once and computes mu_r, mu_z, sigma_r, hubness
and the first product C~ Omega; the PCs then need 2 * power_iters + 1 further passes (7 by default),
each two GEMMs with k + oversample columns per subject. The sampled maps keep the group sum of r for
the K seed columns: V * K * 4 bytes (457 MB at V = 228,483 and K = 500). When the estimated peak of
the maps pass with read-ahead (3 subject buffers) exceeds the memory budget (default 900 MiB) that
pass reads one subject at a time (one buffer): a GSP-size maps pass then peaks at about 0.7 GB
instead of about 1 GB, and costs little time because its computation outweighs the reads. The PC
passes need little memory and always read ahead. :func:`estimate` returns the passes, bytes read,
memory and a measured time estimate.

Outputs in ``<bundle>/derived/reference/`` (all subjects of the bundle) or
``<bundle>/derived/reference/subj-<sha12>/`` (any other subject list), or ``--out``::

    mu_r.npy  mu_z.npy  sigma_r.npy  hub_abs_z.npy   (V,) float32
    pcs.npy   (k, V) float32        eigvals.npy  (k,) float64        ev1.npy  (V,) float32 (optional)
    reference.json   definition, a text definition per map, inputs (manifest, data and mask sha256,
                     subjects n/sha256/list name), parameters, convergence, created, elapsed, sha256 per file

Files are written atomically; reference.json is written last, and the previous reference.json is
removed before new maps replace the old ones, so a reference.json always describes the files next to
it. Work in progress lives in ``.reference-build/`` (resume state, intermediate products): with
``--resume`` a stopped build skips the completed units ("maps" and each PC step).

Nothing builds reference maps implicitly (decision D-A20): only ``reference build`` (or :func:`build`)
does, and :func:`load_reference` returns None when they are missing or stale.

Command line (JSON events, see ``jobs``; exit codes 0 done, 1 error, 2 usage, 3 cancelled, 75 locked)::

    python -m LesionNetworkMappingLib.reference build --bundle B [--out DIR] [--subjects N | --subject-list SPEC]
        [--seeds 500] [--pcs 3] [--power-iters 3] [--oversample 7] [--ev1] [--seed 0] [--hub auto|compute]
        [--memory-gb 1] [--threads K] [--cancel-file F] [--resume] [--run-root DIR] [--parent-run ID]
    python -m LesionNetworkMappingLib.reference estimate --bundle B [--subjects N | --subject-list SPEC] [--seeds 500] ...
    python -m LesionNetworkMappingLib.reference compare --bundle B --external FILE.nii.gz [--reference DIR] [--subject-list SPEC]

``build`` registers the run (kind "reference") in ``<run root>/derivatives/lnm/runs.jsonl``; the run
root is ``--run-root``, else the bundle folder. ``compare`` resamples a user-supplied map (for
example van den Heuvel's GSP1000_degree.nii.gz, a signed row sum of a parcel FCz matrix) to the
bundle grid and reports Pearson r and Spearman rho with every reference map.
"""

import os
import sys

if __name__ == "__main__":
    from LesionNetworkMappingLib.jobs import set_blas_threads

    set_blas_threads(sys.argv[1:])  # before numpy is imported: BLAS reads these variables once

import datetime
import glob
import hashlib
import json
import shutil
import time
from typing import NamedTuple

import numpy as np

from . import jobs, runs
from .connectome import ConnectomeBundle, subjects_sha256

DEFINITION = "lnm-reference-v1"
FORMAT_NAME = "lnm-reference"
FORMAT_VERSION = 1
REFERENCE_JSON = "reference.json"
WORK_DIR = ".reference-build"
LOCK_NAME = ".reference.lock"
RESUME_NAME = "resume.json"
DEFAULT_SEEDS = 500
DEFAULT_PCS = 3
DEFAULT_POWER_ITERS = 3
DEFAULT_OVERSAMPLE = 7
DEFAULT_CHUNK = 8192
DEFAULT_MEMORY_BUDGET = 900 << 20  # the maps pass reads ahead only below this estimate (GSP size: about 0.7 GB without, 1 GB with)
RMAX = 0.999999  # as engine.RMAX: r is clipped before the Fisher transform
HUB_MODES = ("auto", "compute")
SIGMA_DDOF = 1

MAP_FILES = {
    "mu_r": "mu_r.npy",
    "mu_z": "mu_z.npy",
    "sigma_r": "sigma_r.npy",
    "hub_abs_z": "hub_abs_z.npy",
    "pcs": "pcs.npy",
    "eigvals": "eigvals.npy",
    "ev1": "ev1.npy",
}

# Normative text definitions (addendum §3.7), copied into reference.json
MAP_DEFINITIONS = {
    "mu_r": {
        "label": "mean r profile (voxelwise)",
        "symbol": "μC_r",
        "exactness": "exact",
        "definition": (
            "For each voxel v: the mean over subjects of the mean over runs of mean_{u≠v} r_run(v,u). Per run the rows are demeaned and unit-norm, "
            "so Σ_{u≠v} r(v,u) = x_v·ḡ - x_v·x_v with ḡ = Σ_u x_u (one GEMV per run). Voxels with zero variance in a run are excluded from that run's mean "
            "(V' = non-zero rows) and contribute 0 for that run."
        ),
    },
    "mu_z": {
        "label": "mean Fisher z profile (sampled)",
        "symbol": "μC_z",
        "exactness": "sampled",
        "definition": "Mean over K sampled seed voxels u≠v of the mean over subjects of atanh r_s(v,u), with r_s the subject's run-averaged r clipped to ±0.999999.",
    },
    "sigma_r": {
        "label": "spread of r over random seeds (sampled)",
        "symbol": "sigmaC_r",
        "exactness": "sampled",
        "definition": "Standard deviation (ddof 1) over the K sampled seed columns u≠v of the group-mean r(v,u) = mean_s r_s(v,u).",
    },
    "hub_abs_z": {
        "label": "hubness",
        "symbol": "hub",
        "exactness": "sampled",
        "definition": "The bundle's derived/degree.npy (mean |Fz| to its random seeds) when present and valid; otherwise the mean over the K sampled seeds u≠v and the subjects of |atanh r_s(v,u)|.",
    },
    "pcs": {
        "label": "principal components of the connectivity profiles",
        "symbol": "PC1..PCk",
        "exactness": "randomized",
        "definition": (
            "Leading left singular vectors of the column-centred voxel-by-voxel group r-matrix C̃ = C - 1μᵀ (C = mean over subjects of the run-averaged r-matrix, μ its column means), "
            "i.e. the PCA scores of the voxels with their connectivity profiles as features. "
            "Randomized block subspace iteration (rank k + oversample, power_iters iterations), matrix-free. Unit norm; signs oriented to correlate positively with mu_r."
        ),
    },
    "eigvals": {
        "label": "PC eigenvalues",
        "symbol": "lambda1..lambdak",
        "exactness": "randomized",
        "definition": "sigma_k^2 / (V - 1) for the singular values sigma_k of C̃ (variance of the voxel PCA scores along each PC).",
    },
    "ev1": {
        "label": "leading eigenvector of the uncentred r-matrix (mode 1)",
        "symbol": "EV1",
        "exactness": "randomized",
        "definition": "Leading eigenvector of the uncentred group r-matrix C (Zaugg & Litvak 'mode 1'; close to μC), same randomized iteration, unit norm, oriented to correlate positively with mu_r.",
    },
}

STAGES = ("maps", "pcs", "write")


class Reference(NamedTuple):
    """Reference maps loaded by :func:`load_reference`.

    ``maps``: {"mu_r", "mu_z", "sigma_r", "hub", "pc1".."pc<k>", and "ev1" when built}: (V,) float32
    vectors over the bundle voxels. ``eigvals``: (k,) PC eigenvalues. ``metadata``: reference.json.
    ``path``: the reference folder. ``sha256``: sha256 of reference.json (identifies the build).
    """

    maps: dict
    eigvals: np.ndarray
    metadata: dict
    path: str
    sha256: str

    @property
    def definition(self):
        return self.metadata.get("definition")

    @property
    def subjects_sha256(self):
        return (self.metadata.get("subjects") or {}).get("sha256")

    def restrict(self, voxels):
        """A Reference with every map restricted to ``voxels`` (indices or a boolean mask over the bundle voxels)."""
        return self._replace(maps={key: value[voxels] for key, value in self.maps.items()})

    def covariates(self, keys=("mu_r", "pc1")):
        """(len(keys), V) float64 matrix of the named maps (for partial similarity or residual maps)."""
        return np.vstack([np.asarray(self.maps[key], dtype=np.float64) for key in keys])


# Small helpers


def _now_iso():
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _file_sha256(path, block=1 << 20):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(block):
            digest.update(chunk)
    return digest.hexdigest()


def _save_npy(path, array):
    tmp_path = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp_path, "wb") as f:
            np.save(f, array)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def _load_npy(path):
    return np.load(path, allow_pickle=False)


def _open_bundle(bundle):
    return bundle if isinstance(bundle, ConnectomeBundle) else ConnectomeBundle.open(bundle)


def _data_sha256(bundle):
    from .export import data_sha256

    return data_sha256(bundle.metadata)


def _manifest_sha256(bundle):
    return _file_sha256(os.path.join(bundle.path, "connectome.json"))


def _peak_rss_mb():
    try:
        import resource
    except ImportError:  # Windows
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)


def select_seeds(n_voxels, n_seeds=DEFAULT_SEEDS, seed=0):
    """Sorted indices of the sampled seed voxels: ``default_rng(seed).choice(V, K, replace=False)`` (as compute_degree)."""
    n_seeds = min(int(n_seeds), int(n_voxels))
    return np.sort(np.random.default_rng(seed).choice(int(n_voxels), size=n_seeds, replace=False))


def seeds_sha256(seeds):
    """sha256 of the seed indices as little-endian int64 (recorded in reference.json)."""
    return hashlib.sha256(np.asarray(seeds, dtype="<i8").tobytes()).hexdigest()


def default_out_dir(bundle, subject_ids=None):
    """Reference folder of a subject list: ``derived/reference`` for all the bundle's subjects, else ``derived/reference/subj-<sha12>``."""
    bundle = _open_bundle(bundle)
    base = os.path.join(bundle.path, "derived", "reference")
    if subject_ids is None or list(subject_ids) == bundle.subject_ids:
        return base
    return os.path.join(base, f"subj-{subjects_sha256(subject_ids)[:12]}")


def _normalized_seed_matrix(data, runs, seeds):
    """(T_total, K) float32 seed matrix of one subject: each seed voxel's run segments demeaned and unit-normalized, / nRuns (as the engine)."""
    from .engine import _normalized_seeds

    return _normalized_seeds(data[seeds].astype(np.float64), runs)


def _omega(n_voxels, m_pc, m_ev, seed):
    """Random test matrix (V, m_pc + m_ev): default_rng(seed + 1) for the PCs, then default_rng(seed + 2) for EV1."""
    omega = np.random.default_rng(int(seed) + 1).standard_normal((n_voxels, m_pc))
    if m_ev:
        omega = np.hstack([omega, np.random.default_rng(int(seed) + 2).standard_normal((n_voxels, m_ev))])
    return omega


def _orthonormalize(matrix):
    """Q of the thin QR of ``matrix`` and its singular values (those of R)."""
    q, r = np.linalg.qr(matrix)
    return q, np.linalg.svd(r, compute_uv=False)


def _orient(vectors, target):
    """Flip the sign of each row of ``vectors`` (k, V) that correlates negatively with ``target`` (V,)."""
    target = np.asarray(target, dtype=np.float64) - np.mean(target)
    out = np.array(vectors, dtype=np.float64)
    for row in out:
        if float((row - row.mean()) @ target) < 0:
            row *= -1.0
    return out


# Per-subject computations


class _Scratch:
    """Reusable chunk buffers (float32 upcast, float64 copy, seed products)."""

    def __init__(self, chunk, n_times, n_seeds):
        self.x32 = np.empty(chunk * n_times, dtype=np.float32)
        self.x64 = np.empty(chunk * n_times, dtype=np.float64)
        self.r = np.empty(chunk * max(n_seeds, 1), dtype=np.float32)

    def upcast(self, data, v0, v1, float64=False):
        n_times = data.shape[1]
        x = self.x32[: (v1 - v0) * n_times].reshape(v1 - v0, n_times)
        np.copyto(x, data[v0:v1])
        if not float64:
            return x, None
        x64 = self.x64[: (v1 - v0) * n_times].reshape(v1 - v0, n_times)
        np.copyto(x64, x)
        return x, x64


def _maps_subject(subject, seeds, seed_column, omega32, acc_r, sums, c_omega, compute_hub, chunk, scratch):
    """Pass-1 work of one subject; adds into ``acc_r`` (V, K), ``sums`` and ``c_omega`` (V, m: its (1 / R) X (X^T Omega)).

    sums: "mu_r" (exact per-subject mean r, float64), "colsum" (C 1 per subject), "z" and "abs_z"
    (sums over the seeds of atanh r and |atanh r|, self excluded).
    """
    data, runs_ = subject.data, subject.runs
    n_voxels = data.shape[0]
    n_runs = len(runs_)
    bounds = np.cumsum((0, *runs_))
    segments = [slice(bounds[i], bounds[i + 1]) for i in range(n_runs)]
    seed_matrix = _normalized_seed_matrix(data, runs_, seeds) if seeds is not None else None
    n_seeds = 0 if seeds is None else len(seeds)
    m = omega32.shape[1]
    g = np.zeros((n_runs, data.shape[1]), dtype=np.float64)  # per run (only its segment is used): sum of the rows
    self_dot = np.zeros((n_runs, n_voxels), dtype=np.float64)
    p = np.zeros((data.shape[1], m), dtype=np.float64)  # X^T Omega
    # Sweep A: row sums, self products, X^T Omega and the seed correlations
    for v0 in range(0, n_voxels, chunk):
        v1 = min(n_voxels, v0 + chunk)
        x, x64 = scratch.upcast(data, v0, v1, float64=True)
        for run, segment in enumerate(segments):
            block = x64[:, segment]
            g[run, segment] += block.sum(axis=0)
            self_dot[run, v0:v1] = np.einsum("ij,ij->i", block, block)
        p += x.T @ omega32[v0:v1]
        if seed_matrix is not None:
            r = scratch.r[: (v1 - v0) * n_seeds].reshape(v1 - v0, n_seeds)
            np.matmul(x, seed_matrix, out=r)
            np.clip(r, -RMAX, RMAX, out=r)
            rows = np.flatnonzero(seed_column[v0:v1] >= 0)
            r[rows, seed_column[v0 + rows]] = 0.0  # self-correlations are excluded
            acc_r[v0:v1] += r
            np.arctanh(r, out=r)
            sums["z"][v0:v1] += r.sum(axis=1, dtype=np.float64)
            if compute_hub:
                np.abs(r, out=r)
                sums["abs_z"][v0:v1] += r.sum(axis=1, dtype=np.float64)
    valid = self_dot > 0
    n_valid = valid.sum(axis=1)
    p32 = p.astype(np.float32) / np.float32(n_runs)
    mu = np.zeros(n_voxels, dtype=np.float64)
    colsum = np.zeros(n_voxels, dtype=np.float64)
    # Sweep B: x_v . g (one GEMV per run) and X (X^T Omega)
    for v0 in range(0, n_voxels, chunk):
        v1 = min(n_voxels, v0 + chunk)
        x, x64 = scratch.upcast(data, v0, v1, float64=True)
        for run, segment in enumerate(segments):
            d = x64[:, segment] @ g[run, segment]
            colsum[v0:v1] += d
            if n_valid[run] > 1:
                mu[v0:v1] += np.where(valid[run, v0:v1], (d - self_dot[run, v0:v1]) / (n_valid[run] - 1), 0.0)
        c_omega[v0:v1] += x @ p32
    sums["mu_r"] += mu / n_runs
    sums["colsum"] += colsum / n_runs


def _apply_c_subject(subject, q32, chunk, scratch):
    """One subject's (1 / R) X (X^T Q) for Q (V, m) float32; returns (V, m) float64."""
    data = subject.data
    n_voxels = data.shape[0]
    m = q32.shape[1]
    p = np.zeros((data.shape[1], m), dtype=np.float64)
    for v0 in range(0, n_voxels, chunk):
        v1 = min(n_voxels, v0 + chunk)
        x, _ = scratch.upcast(data, v0, v1)
        p += x.T @ q32[v0:v1]
    p32 = p.astype(np.float32)
    out = np.empty((n_voxels, m), dtype=np.float64)
    for v0 in range(0, n_voxels, chunk):
        v1 = min(n_voxels, v0 + chunk)
        x, _ = scratch.upcast(data, v0, v1)
        out[v0:v1] = x @ p32
    out /= len(subject.runs)
    return out


def _sigma_from_sums(acc_r, n_subjects, seed_column, chunk):
    """Std (ddof 1) over the seed columns u != v of the group-mean r (acc_r / N); self entries of acc_r are 0."""
    n_voxels, n_seeds = acc_r.shape
    sigma = np.zeros(n_voxels, dtype=np.float64)
    for v0 in range(0, n_voxels, chunk):
        v1 = min(n_voxels, v0 + chunk)
        mean_r = acc_r[v0:v1].astype(np.float64) / n_subjects
        is_seed = seed_column[v0:v1] >= 0
        count = np.where(is_seed, n_seeds - 1, n_seeds).astype(np.float64)
        centre = mean_r.sum(axis=1) / count
        mean_r -= centre[:, np.newaxis]
        rows = np.flatnonzero(is_seed)
        mean_r[rows, seed_column[v0 + rows]] = 0.0
        sigma[v0:v1] = np.sqrt(np.einsum("ij,ij->i", mean_r, mean_r) / np.maximum(count - SIGMA_DDOF, 1))
    return sigma


# Planning


def _subject_selection(bundle, subjects=None, subject_list=None):
    """(ids, description dict) from subjects (None, an int or ids) or a subject list spec."""
    if subject_list is not None:
        resolved = bundle.resolve_subject_list(subject_list)
        return list(resolved.ids), {"list_name": resolved.name, "list_source": resolved.source, "list_file": resolved.path}
    if subjects is None:
        return bundle.subject_ids, {"list_name": None, "list_source": "all", "list_file": None}
    if isinstance(subjects, (int, np.integer)):
        ids = bundle.select_subjects(int(subjects))
        return ids, {"list_name": None, "list_source": "all" if len(ids) == bundle.n_subjects else f"first {len(ids)}", "list_file": None}
    resolved = bundle.resolve_subject_list(list(subjects))
    return list(resolved.ids), {"list_name": None, "list_source": "ids", "list_file": None}


def _n_steps(power_iters):
    return 2 * int(power_iters) + 1


_BASELINE_BYTES = 64 << 20  # Python, numpy and the library (34 MB measured) with a margin


def _memory_bytes(n_voxels, n_seeds, max_times, itemsize, n_runs, m, chunk, buffers):
    """Peak memory of the maps pass (the largest; PC passes need about buffers * subject + 4 * V * m * 8)."""
    chunk = min(chunk, n_voxels)
    accumulators = n_voxels * n_seeds * 4 + n_voxels * m * (8 + 4) + n_voxels * 8 * (6 + 2 * n_runs) + n_voxels * n_runs
    temporaries = chunk * (max_times * 12 + n_seeds * 4 + 4 * 8) + max_times * (n_seeds + m) * 8
    return int(_BASELINE_BYTES + accumulators + temporaries + buffers * n_voxels * max_times * itemsize)


def estimate(bundle, subjects=None, subject_list=None, n_seeds=DEFAULT_SEEDS, k=DEFAULT_PCS, power_iters=DEFAULT_POWER_ITERS, oversample=DEFAULT_OVERSAMPLE, ev1=False, chunk=DEFAULT_CHUNK, memory_budget=DEFAULT_MEMORY_BUDGET, measure=True):
    """Work, memory and time of a build (for the "Build reference maps (~N min)?" prompt and the start event).

    The time is measured on the first subject (its read time and one chunk of each kind of work,
    scaled to the bundle) unless ``measure`` is False; read-ahead overlaps reading and computing, so a
    subject costs about the larger of the two. Page-cache effects make it a rough figure.
    :return: dict with n_subjects, n_voxels, n_seeds, passes, read_bytes, memory_bytes, prefetch,
      output_bytes, seconds_per_subject {"maps", "pcs"}, seconds (None when not measured).
    """
    bundle = _open_bundle(bundle)
    ids, _ = _subject_selection(bundle, subjects, subject_list)
    n_voxels = bundle.n_voxels
    n_seeds = min(int(n_seeds), n_voxels)
    m = int(k) + int(oversample) + ((1 + int(oversample)) if ev1 else 0)
    max_times = max(sum(bundle.runs(i)) for i in ids)
    max_runs = max(len(bundle.runs(i)) for i in ids)
    itemsize = bundle.dtype.itemsize
    subject_bytes = sum(n_voxels * sum(bundle.runs(i)) * itemsize for i in ids)
    passes = 1 + _n_steps(power_iters)
    memory_prefetch = _memory_bytes(n_voxels, n_seeds, max_times, itemsize, max_runs, m, chunk, 3)
    prefetch = memory_budget is None or memory_prefetch <= memory_budget
    info = {
        "n_subjects": len(ids),
        "n_voxels": n_voxels,
        "n_seeds": n_seeds,
        "columns": m,
        "passes": passes,
        "read_bytes": int(subject_bytes * passes),
        "memory_bytes": memory_prefetch if prefetch else _memory_bytes(n_voxels, n_seeds, max_times, itemsize, max_runs, m, chunk, 1),
        "memory_budget": memory_budget,
        "prefetch": prefetch,
        "output_bytes": int(n_voxels * 4 * (4 + int(k) + (1 if ev1 else 0)) + 8 * int(k)),
        "seconds_per_subject": None,
        "seconds": None,
    }
    if not measure or not ids:
        return info
    started = time.perf_counter()
    subject = bundle.load_subject(ids[0])
    read_s = time.perf_counter() - started
    subject_size = subject.data.nbytes
    data = subject.data[: min(chunk, n_voxels)]
    sample = subject._replace(data=data)
    seeds = select_seeds(data.shape[0], min(n_seeds, data.shape[0]), 0)
    seed_column = np.full(data.shape[0], -1, dtype=np.int64)
    seed_column[seeds] = np.arange(len(seeds))
    scratch = _Scratch(data.shape[0], data.shape[1], len(seeds))
    omega32 = np.ones((data.shape[0], m), dtype=np.float32)
    acc = np.zeros((data.shape[0], len(seeds)), dtype=np.float32)
    sums = {key: np.zeros(data.shape[0]) for key in ("mu_r", "colsum", "z", "abs_z")}
    c_omega = np.zeros((data.shape[0], m))

    def timed(function, *args):
        function(*args)  # warm-up: first-touch page faults and BLAS thread start
        started = time.perf_counter()
        function(*args)
        return (time.perf_counter() - started) * n_voxels / data.shape[0]

    # One chunk of each kind of work, scaled to V (a chunk of the maps pass multiplies by all K seed columns)
    maps_s = timed(_maps_subject, sample, seeds, seed_column, omega32, acc, sums, c_omega, True, chunk, scratch)
    pcs_s = timed(_apply_c_subject, sample, omega32, chunk, scratch)
    # Read time scales with each subject's size; read-ahead overlaps it with the computation
    read_rate = read_s / max(subject_size, 1)
    mean_bytes = subject_bytes / len(ids)
    per_maps = max(read_rate * mean_bytes, maps_s) if prefetch else read_rate * mean_bytes + maps_s
    per_pcs = max(read_rate * mean_bytes, pcs_s)
    info["seconds_per_subject"] = {"maps": round(per_maps, 4), "pcs": round(per_pcs, 4), "read": round(read_rate * mean_bytes, 4)}
    info["seconds"] = round(len(ids) * (per_maps + _n_steps(power_iters) * per_pcs), 1)
    return info


# Hubness source


def _hub_from_degree(bundle, ids, hub):
    """(vector or None, description): the bundle's degree.npy when ``hub`` is "auto" and it is a valid |Fz| hubness map."""
    from .qa import read_degree_metadata

    path = os.path.join(bundle.path, "derived", "degree.npy")
    if hub != "auto" or not os.path.isfile(path):
        return None, {"source": "computed", "reason": "requested" if hub != "auto" else "no derived/degree.npy"}
    metadata = read_degree_metadata(bundle.path)
    try:
        degree = bundle.degree
    except (OSError, ValueError) as exc:
        return None, {"source": "computed", "reason": f"derived/degree.npy is unreadable: {exc}"}
    if metadata is not None and metadata.get("measure", "abs_fz") != "abs_fz":
        return None, {"source": "computed", "reason": f"derived/degree.npy holds measure {metadata.get('measure')!r}, not mean |Fz|"}
    if degree is None or not np.all(np.isfinite(degree)) or float(degree.min()) < 0:
        return None, {"source": "computed", "reason": "derived/degree.npy has negative or non-finite values (not mean |Fz|)"}
    description = {
        "source": "bundle derived/degree.npy",
        "sha256": _file_sha256(path),
        "degree_json": metadata,  # K, subjects, subject_ids_sha256, seed, measure (None when degree.json is missing or stale)
        "same_subjects": None if metadata is None else metadata.get("subject_ids_sha256") == subjects_sha256(ids),
    }
    return degree.astype(np.float32), description


# Build


class _Progress:
    """Progress events over the whole job (units = subject passes) with an ETA from measured per-subject times.

    The first subject of a pass (no read-ahead yet, first-touch page faults) is left out of the measured
    rate while the estimate or later subjects give one.
    """

    def __init__(self, emit, n_subjects, n_steps, estimate_info, done_units=0):
        self.emit = emit
        self.n = n_subjects
        self.total = n_subjects * (1 + n_steps)
        self.done = done_units
        self.started = time.monotonic()
        rates = (estimate_info or {}).get("seconds_per_subject") or {}
        self.estimated = {"maps": rates.get("maps"), "pcs": rates.get("pcs")}
        self.measured = {"maps": [0.0, 0], "pcs": [0.0, 0]}
        self.first = {"maps": None, "pcs": None}
        self.last_emit = 0.0

    def rate(self, stage):
        spent, count = self.measured[stage]
        if count:
            return spent / count
        other = "pcs" if stage == "maps" else "maps"
        if self.measured[other][1] and self.estimated[stage] and self.estimated[other]:
            # Scale the estimate by how far the measured other stage is from its estimate
            return self.estimated[stage] * (self.measured[other][0] / self.measured[other][1]) / self.estimated[other]
        return self.estimated[stage] if self.estimated[stage] is not None else self.first[stage]

    def step(self, stage, seconds, remaining_maps, remaining_pcs, force=False, first=False, **extra):
        self.done += 1
        if first:
            self.first[stage] = seconds
        else:
            self.measured[stage][0] += seconds
            self.measured[stage][1] += 1
        now = time.monotonic()
        if not force and now - self.last_emit < 0.25:
            return
        self.last_emit = now
        eta = None
        if remaining_maps == 0 or self.rate("maps") is not None:
            if remaining_pcs == 0 or self.rate("pcs") is not None:
                eta = round(remaining_maps * (self.rate("maps") or 0.0) + remaining_pcs * (self.rate("pcs") or 0.0), 1)
        self.emit("progress", done=self.done, total=self.total, elapsed=round(now - self.started, 3), eta=eta, stage=stage, **extra)


def _iterate(bundle, ids, prefetch, cancel, done_units):
    """Subjects of one pass; stops the read-ahead thread when the pass ends or is cancelled."""
    iterator = bundle.iter_subjects(ids, prefetch=prefetch, reuse_buffers=True)
    try:
        for subject in iterator:
            if cancel is not None and cancel():
                raise jobs.Cancelled(done=done_units(), reason=getattr(cancel, "reason", None))
            yield subject
    finally:
        iterator.close()


def build(
    bundle,
    out_dir=None,
    subjects=None,
    subject_list=None,
    n_seeds=DEFAULT_SEEDS,
    k=DEFAULT_PCS,
    power_iters=DEFAULT_POWER_ITERS,
    oversample=DEFAULT_OVERSAMPLE,
    ev1=False,
    seed=0,
    hub="auto",
    chunk=DEFAULT_CHUNK,
    memory_budget=DEFAULT_MEMORY_BUDGET,
    resume=False,
    cancel=None,
    emit=None,
    run_root=None,
    parent_runs=(),
    register=True,
    argv=None,
    threads=None,
):
    """Build the reference maps of a bundle and subject list (see the module docstring); return the reference.json dict.

    :param subjects: None (all), an int (the first n) or a list of ids; or give ``subject_list``
      (a subset name, a file of ids or comma-separated ids, see ConnectomeBundle.resolve_subject_list).
    :param out_dir: output folder (default :func:`default_out_dir`).
    :param hub: "auto" (copy a valid derived/degree.npy, else compute) or "compute".
    :param memory_budget: bytes; above it the maps pass reads subjects without read-ahead (None: no limit).
    :param resume: keep the completed units of a stopped build with the same inputs and parameters.
    :param cancel: callable (e.g. jobs.CancelToken) polled between subjects; raises jobs.Cancelled.
    :param emit: event function (default jobs.emit, JSON lines on standard output).
    :param run_root: folder whose derivatives/lnm/runs.jsonl records the run (default: the bundle).
    """
    emit = emit or jobs.emit
    bundle = _open_bundle(bundle)
    if bundle.kind != "timeseries":
        raise jobs.JobError(f"Reference maps need a time-series connectome; {bundle.name!r} is of kind {bundle.kind!r}", code="input")
    if hub not in HUB_MODES:
        raise ValueError(f"hub must be one of {HUB_MODES}")
    ids, list_info = _subject_selection(bundle, subjects, subject_list)
    n_voxels = bundle.n_voxels
    n_seeds = min(int(n_seeds), n_voxels)
    k, power_iters, oversample, chunk = int(k), int(power_iters), int(oversample), max(1, int(chunk))
    if n_seeds < 3:
        raise ValueError("At least 3 seed voxels are needed")
    if k < 1 or oversample < 0 or power_iters < 0 or k + oversample > n_voxels:
        raise ValueError("Invalid PCA parameters: need k >= 1, oversample >= 0, power_iters >= 0 and k + oversample <= V")
    if out_dir is None and not os.access(bundle.path, os.W_OK):
        raise jobs.JobError(f"The connectome folder is read-only ({bundle.path}): give an output folder (--out) for the reference maps", code="read_only")
    out_dir = os.path.abspath(os.fspath(out_dir or default_out_dir(bundle, ids)))
    os.makedirs(out_dir, exist_ok=True)
    work = os.path.join(out_dir, WORK_DIR)
    os.makedirs(work, exist_ok=True)
    n_steps = _n_steps(power_iters)
    subjects_sha = subjects_sha256(ids)
    data_sha = _data_sha256(bundle)
    mask_sha = bundle.mask_sha256
    manifest_sha = _manifest_sha256(bundle)
    seeds = select_seeds(n_voxels, n_seeds, seed)
    seed_column = np.full(n_voxels, -1, dtype=np.int64)
    seed_column[seeds] = np.arange(n_seeds)
    hub_vector, hub_info = _hub_from_degree(bundle, ids, hub)
    compute_hub = hub_vector is None
    params = {
        "definition": DEFINITION,
        "data_sha256": data_sha,
        "mask_sha256": mask_sha,
        "subjects_sha256": subjects_sha,
        "K_seeds": n_seeds,
        "seed": int(seed),
        "k": k,
        "power_iters": power_iters,
        "oversample": oversample,
        "ev1": bool(ev1),
        "hub": hub,
        "chunk": chunk,
    }
    info = estimate(bundle, ids, None, n_seeds, k, power_iters, oversample, ev1, chunk, memory_budget, measure=True)
    lock = jobs.OutputLock(os.path.join(out_dir, LOCK_NAME))
    lock.acquire()
    run_id = None
    run_root = os.path.abspath(os.fspath(run_root)) if run_root else (bundle.path if os.access(bundle.path, os.W_OK) else out_dir)
    try:
        if lock.reclaimed is not None:
            emit("warning", code="stale_lock", message=f"A stale lock of process {lock.reclaimed.get('pid')} on {out_dir} was reclaimed")
        state = jobs.ResumeState(os.path.join(work, RESUME_NAME))
        if not resume:
            state.clear()
        elif state.done and state.data.get("params") != params:
            emit("warning", code="resume_mismatch", message="The saved progress belongs to other inputs or parameters; the build starts again.")
            state.clear()
        if not state.done:
            for name in os.listdir(work):
                if name != RESUME_NAME:
                    os.remove(os.path.join(work, name))
        state.data = {"params": params, "elapsed": float(state.data.get("elapsed", 0.0)) if state.done else 0.0}
        emit(
            "start",
            job="reference",
            argv=list(argv or []),
            threads=threads,
            definition=DEFINITION,
            bundle=bundle.name,
            out=out_dir,
            n_subjects=len(ids),
            n_voxels=n_voxels,
            seeds=n_seeds,
            k=k,
            power_iters=power_iters,
            oversample=oversample,
            ev1=bool(ev1),
            hub=hub_info["source"],
            resumed=list(state.done),
            estimate=info,
        )
        if hub_vector is None and hub == "auto" and hub_info.get("reason", "").startswith("derived/degree.npy"):
            emit("warning", code="hub_computed", message=f"Hubness is computed from the sampled seeds: {hub_info['reason']}")
        if register:
            run_id = runs.register(run_root, "reference", out_dir, parents=list(parent_runs or ()), params={**params, "n_subjects": len(ids), "bundle": bundle.path}, seeds={"seeds": int(seed), "omega": int(seed) + 1})
        started = time.monotonic()
        progress = _Progress(emit, len(ids), n_steps, info, done_units=len(ids) * (int(state.is_done("maps")) + sum(state.is_done(f"pc-step-{i}") for i in range(1, n_steps + 1))))
        done_units = lambda: progress.done  # noqa: E731
        m_pc = k + oversample
        m_ev = 1 + oversample if ev1 else 0
        prefetch = info["prefetch"]
        scratch = None

        def scratch_for(n_columns):
            nonlocal scratch
            if scratch is None or scratch.r.size < min(chunk, n_voxels) * max(n_columns, 1):
                scratch = _Scratch(min(chunk, n_voxels), max(sum(bundle.runs(i)) for i in ids), n_columns)
            return scratch

        # Stage 1: exact mu_r, sampled maps and the first product C~ Omega
        emit("stage", name="maps", index=1, total=len(STAGES))
        if not state.is_done("maps"):
            omega32 = _omega(n_voxels, m_pc, m_ev, seed).astype(np.float32)  # float64 Omega is drawn again for mu^T Omega
            acc_r = np.zeros((n_voxels, n_seeds), dtype=np.float32)
            sums = {key: np.zeros(n_voxels, dtype=np.float64) for key in ("mu_r", "colsum", "z", "abs_z")}
            c_omega = np.zeros(omega32.shape, dtype=np.float64)
            buffers = scratch_for(n_seeds)
            last = time.monotonic()
            for index, subject in enumerate(_iterate(bundle, ids, prefetch, cancel, done_units)):
                _maps_subject(subject, seeds, seed_column, omega32, acc_r, sums, c_omega, compute_hub, chunk, buffers)
                now = time.monotonic()  # wall time per subject, reading included
                progress.step("maps", now - last, len(ids) - index - 1, len(ids) * n_steps, force=index == len(ids) - 1, first=index == 0)
                last = now
            n = len(ids)
            counts = np.where(seed_column >= 0, n_seeds - 1, n_seeds).astype(np.float64)
            maps = {
                "mu_r": (sums["mu_r"] / n).astype(np.float32),
                "mu_z": (sums["z"] / counts / n).astype(np.float32),
                "sigma_r": _sigma_from_sums(acc_r, n, seed_column, chunk).astype(np.float32),
                "hub_abs_z": hub_vector if hub_vector is not None else (sums["abs_z"] / counts / n).astype(np.float32),
            }
            del acc_r, omega32
            mu_col = sums["colsum"] / n / n_voxels  # column means of C (C is symmetric)
            c_omega /= n
            products = c_omega[:, :m_pc] - np.outer(np.ones(n_voxels), mu_col @ _omega(n_voxels, m_pc, 0, seed))  # C~ Omega = C Omega - 1 (mu^T Omega)
            q_pc, sv_pc = _orthonormalize(products)
            for key, vector in maps.items():
                _save_npy(os.path.join(work, MAP_FILES[key]), vector)
            _save_npy(os.path.join(work, "mu_col.npy"), mu_col)
            _save_npy(os.path.join(work, "q_pc.npy"), q_pc)
            history = {"pcs": [sv_pc[:k].tolist()], "ev1": []}
            if ev1:
                q_ev, sv_ev = _orthonormalize(c_omega[:, m_pc:])
                _save_npy(os.path.join(work, "q_ev.npy"), q_ev)
                history["ev1"].append(sv_ev[:1].tolist())
            state.data["history"] = history
            state.data["elapsed"] = state.data.get("elapsed", 0.0) + (time.monotonic() - started)
            state.mark_done("maps")
        mu_r = _load_npy(os.path.join(work, MAP_FILES["mu_r"]))
        mu_col = _load_npy(os.path.join(work, "mu_col.npy"))
        q_pc = _load_npy(os.path.join(work, "q_pc.npy"))
        q_ev = _load_npy(os.path.join(work, "q_ev.npy")) if ev1 else None
        history = state.data.get("history") or {"pcs": [], "ev1": []}

        # Stage 2: subspace iteration; odd steps apply C~^T, even steps C~, the last step gives B^T = C~^T Q
        emit("stage", name="pcs", index=2, total=len(STAGES))
        ones = np.ones(n_voxels)
        for step in range(1, n_steps + 1):
            unit = f"pc-step-{step}"
            if state.is_done(unit):
                continue
            step_started = time.monotonic()
            block = np.hstack([q_pc, q_ev]) if ev1 else q_pc
            block32 = block.astype(np.float32)
            product = np.zeros(block.shape, dtype=np.float64)
            buffers = scratch_for(0)
            last = time.monotonic()
            for index, subject in enumerate(_iterate(bundle, ids, True, cancel, done_units)):
                product += _apply_c_subject(subject, block32, chunk, buffers)
                now = time.monotonic()
                progress.step("pcs", now - last, 0, len(ids) * (n_steps - step + 1) - index - 1, force=index == len(ids) - 1, first=index == 0, step=step, steps=n_steps)
                last = now
            product /= len(ids)
            c_q = product[:, :m_pc]
            if step % 2:  # C~^T Q = C Q - mu (1^T Q)
                c_q = c_q - np.outer(mu_col, ones @ q_pc)
            else:  # C~ Q = C Q - 1 (mu^T Q)
                c_q = c_q - np.outer(ones, mu_col @ q_pc)
            if step < n_steps:
                q_pc, sv = _orthonormalize(c_q)
                history["pcs"].append(sv[:k].tolist())
                if ev1:
                    q_ev, sv_ev = _orthonormalize(product[:, m_pc:])
                    history["ev1"].append(sv_ev[:1].tolist())
            else:
                bt_pc = c_q  # B^T = C~^T Q, (V, m)
                _save_npy(os.path.join(work, "bt_pc.npy"), bt_pc)
                if ev1:
                    _save_npy(os.path.join(work, "bt_ev.npy"), product[:, m_pc:])
            _save_npy(os.path.join(work, "q_pc.npy"), q_pc)
            if ev1:
                _save_npy(os.path.join(work, "q_ev.npy"), q_ev)
            state.data["history"] = history
            state.data["elapsed"] = state.data.get("elapsed", 0.0) + (time.monotonic() - step_started)
            state.mark_done(unit)

        # Stage 3: small SVDs, orientation and the outputs
        emit("stage", name="write", index=3, total=len(STAGES))
        write_started = time.monotonic()
        q_pc = _load_npy(os.path.join(work, "q_pc.npy"))
        bt_pc = _load_npy(os.path.join(work, "bt_pc.npy"))
        u_b, singular, _ = np.linalg.svd(bt_pc.T, full_matrices=False)  # B = Q^T C~ (m, V)
        pcs = _orient((q_pc @ u_b[:, :k]).T, mu_r)
        pcs /= np.linalg.norm(pcs, axis=1, keepdims=True)
        eigvals = singular[:k] ** 2 / (n_voxels - 1)
        history["pcs"].append(singular[:k].tolist())
        outputs = {"pcs": pcs.astype(np.float32), "eigvals": eigvals.astype(np.float64)}
        ev1_info = None
        if ev1:
            q_ev = _load_npy(os.path.join(work, "q_ev.npy"))
            u_e, singular_ev, _ = np.linalg.svd(_load_npy(os.path.join(work, "bt_ev.npy")).T, full_matrices=False)
            vector = _orient((q_ev @ u_e[:, :1]).T, mu_r)[0]
            outputs["ev1"] = (vector / np.linalg.norm(vector)).astype(np.float32)
            history["ev1"].append(singular_ev[:1].tolist())
            ev1_info = {"eigenvalue": float(singular_ev[0]), "convergence": _convergence(history["ev1"])}
        for key, array in outputs.items():
            _save_npy(os.path.join(work, MAP_FILES[key]), array)
        elapsed = round(float(state.data.get("elapsed", 0.0)) + time.monotonic() - write_started, 3)
        keys = [key for key in MAP_FILES if key != "ev1" or ev1]
        record = {
            "format": FORMAT_NAME,
            "version": FORMAT_VERSION,
            "definition": DEFINITION,
            "maps": {},
            "bundle": {"name": bundle.name, "path": bundle.path, "n_voxels": n_voxels, "shape": list(bundle.shape), "data_sha256": data_sha},
            "manifest_sha256": manifest_sha,
            "data_sha256": data_sha,
            "mask_sha256": mask_sha,
            "subjects": {"n": len(ids), "sha256": subjects_sha, **list_info, "ids": list(ids)},
            "K_seeds": n_seeds,
            "seed": int(seed),
            "k": k,
            "power_iters": power_iters,
            "oversample": oversample,
            "ev1": ev1_info,
            "seeds": {"sha256": seeds_sha256(seeds), "rule": "numpy.random.default_rng(seed).choice(V, K, replace=False), sorted; each seed normalized per run as an engine one-voxel lesion"},
            "omega": {"pcs": "numpy.random.default_rng(seed + 1).standard_normal((V, k + oversample))", "ev1": "numpy.random.default_rng(seed + 2).standard_normal((V, 1 + oversample))" if ev1 else None},
            "pca": {"singular_values": singular[:k].tolist(), "eigvals": eigvals.tolist(), "convergence": _convergence(history["pcs"]), "sign": "each PC correlates positively with mu_r"},
            "hub": hub_info,
            "rmax": RMAX,
            "sigma_ddof": SIGMA_DDOF,
            "chunk": chunk,
            "created": _now_iso(),
            "elapsed": elapsed,
            "run_id": run_id,
            "argv": list(argv or []),
            "software": {"library_version": jobs.library_version(), "numpy": np.__version__},
        }
        # Publish: remove the old description first, so that reference.json never describes other files
        json_path = os.path.join(out_dir, REFERENCE_JSON)
        if os.path.exists(json_path):
            os.remove(json_path)
        for key, name in MAP_FILES.items():
            target = os.path.join(out_dir, name)
            if key in keys:
                os.replace(os.path.join(work, name), target)
                shape = list(np.load(target, mmap_mode="r", allow_pickle=False).shape)
                record["maps"][key] = {"file": name, "sha256": _file_sha256(target), "shape": shape, **MAP_DEFINITIONS[key]}
            elif os.path.exists(target):
                os.remove(target)  # an ev1.npy of an earlier build
        record["maps"]["hub_abs_z"]["source"] = hub_info["source"]
        jobs.write_json_atomic(json_path, record)
        shutil.rmtree(work, ignore_errors=True)
        emit("result", kind="reference", path=json_path)
        if run_id:
            runs.update(run_root, run_id, status="done", ended=record["created"], outputs=[REFERENCE_JSON, *(MAP_FILES[key] for key in keys)], summary={"n_subjects": len(ids), "K_seeds": n_seeds, "eigvals": eigvals.tolist(), "elapsed": elapsed})
        emit("done", outputs=[json_path, *(os.path.join(out_dir, MAP_FILES[key]) for key in keys)], elapsed=elapsed, run_id=run_id, peak_rss_mb=_peak_rss_mb())
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


def _convergence(history):
    """Singular value estimates per step and the relative change of the last step (a small change means converged)."""
    if len(history) < 2:
        return {"history": history, "max_rel_change": None}
    last, previous = np.asarray(history[-1], dtype=np.float64), np.asarray(history[-2], dtype=np.float64)
    n = min(len(last), len(previous))
    change = np.abs(last[:n] - previous[:n]) / np.maximum(np.abs(last[:n]), np.finfo(np.float64).tiny)
    return {"history": history, "max_rel_change": float(change.max()) if n else None}


# Loading


def _candidates(bundle, path=None):
    if path is not None:
        path = os.path.abspath(os.fspath(path))
        return [os.path.dirname(path) if os.path.basename(path) == REFERENCE_JSON else path]
    base = os.path.join(bundle.path, "derived", "reference")
    return [base, *sorted(glob.glob(os.path.join(base, "subj-*")))]


def reference_status(bundle, subjects_sha=None, path=None, strict=False, verify=True):
    """Status of every reference folder of a bundle: a list of {path, status, reason, metadata}.

    status is "ok", "missing", "stale" (connectome data, mask or, with ``strict``, the manifest
    changed; or another definition), "other_subjects" (with ``subjects_sha``) or "corrupt"
    (a file is missing, unreadable or does not match its sha256 with ``verify``).
    """
    bundle = _open_bundle(bundle)
    statuses = []
    try:
        mask_sha = bundle.mask_sha256
    except (OSError, ValueError):
        mask_sha = None
    data_sha = _data_sha256(bundle)
    manifest_sha = _manifest_sha256(bundle) if strict else None
    for folder in _candidates(bundle, path):
        entry = {"path": folder, "status": "ok", "reason": None, "metadata": None}
        statuses.append(entry)
        json_path = os.path.join(folder, REFERENCE_JSON)
        if not os.path.isfile(json_path):
            entry.update(status="missing", reason="no reference.json")
            continue
        try:
            with open(json_path, encoding="utf-8") as f:
                metadata = json.load(f)
        except (OSError, ValueError) as exc:
            entry.update(status="corrupt", reason=f"reference.json is unreadable: {exc}")
            continue
        entry["metadata"] = metadata
        if not isinstance(metadata, dict) or metadata.get("definition") != DEFINITION:
            entry.update(status="stale", reason=f"definition {metadata.get('definition') if isinstance(metadata, dict) else None!r} is not {DEFINITION!r}")
        elif metadata.get("data_sha256") != data_sha:
            entry.update(status="stale", reason="the connectome data changed (connectome.json grid, subjects or dtype)")
        elif mask_sha is None or metadata.get("mask_sha256") != mask_sha:
            entry.update(status="stale", reason="the connectome mask changed")
        elif strict and metadata.get("manifest_sha256") != manifest_sha:
            entry.update(status="stale", reason="connectome.json changed")
        elif subjects_sha is not None and (metadata.get("subjects") or {}).get("sha256") != subjects_sha:
            entry.update(status="other_subjects", reason="built for another subject list")
        elif verify:
            for key, item in (metadata.get("maps") or {}).items():
                file_path = os.path.join(folder, str(item.get("file", "")))
                if not os.path.isfile(file_path) or _file_sha256(file_path) != item.get("sha256"):
                    entry.update(status="corrupt", reason=f"{item.get('file')} is missing or does not match its sha256")
                    break
    return statuses


def load_reference(bundle, subjects_sha=None, path=None, strict=False, verify=True):
    """The reference maps of a bundle, or None when they are missing or stale (never builds them, D-A20).

    :param subjects_sha: sha256 of the subject list (``connectome.subjects_sha256``) the reference must
      have been built from. Without it the reference of all the bundle's subjects is preferred, else
      the valid one built from the most subjects.
    :param path: a reference folder (or its reference.json) instead of the bundle's derived/reference.
    :param strict: also require the exact connectome.json (by default only the data-defining keys and
      the mask must match, so editing a description, a DOI or a user subset keeps the reference valid).
    :param verify: check every file against the sha256 recorded in reference.json.
    """
    try:
        bundle = _open_bundle(bundle)
        valid = [entry for entry in reference_status(bundle, subjects_sha, path, strict, verify) if entry["status"] == "ok"]
    except (OSError, ValueError):
        return None
    if not valid:
        return None
    if path is None and subjects_sha is None and valid[0]["path"] != _candidates(bundle)[0]:
        valid.sort(key=lambda entry: ((entry["metadata"].get("subjects") or {}).get("n") or 0, entry["metadata"].get("created") or ""), reverse=True)
    entry = valid[0]
    folder, metadata = entry["path"], entry["metadata"]
    try:
        files = {key: _load_npy(os.path.join(folder, item["file"])) for key, item in metadata["maps"].items()}
    except (OSError, ValueError, KeyError):
        return None
    n_voxels = bundle.n_voxels
    pcs = files.get("pcs")
    if any(files[key].shape != (n_voxels,) for key in ("mu_r", "mu_z", "sigma_r", "hub_abs_z") if key in files) or pcs is None or pcs.ndim != 2 or pcs.shape[1] != n_voxels:
        return None
    maps = {"mu_r": files["mu_r"], "mu_z": files["mu_z"], "sigma_r": files["sigma_r"], "hub": files["hub_abs_z"]}
    for index in range(pcs.shape[0]):
        maps[f"pc{index + 1}"] = pcs[index]
    if "ev1" in files:
        maps["ev1"] = files["ev1"]
    return Reference(maps, files.get("eigvals"), metadata, folder, _file_sha256(os.path.join(folder, REFERENCE_JSON)))


# External maps


def external_on_grid(nifti_path, bundle, min_coverage=0.5):
    """A user-supplied map on the bundle voxels: (V,) float64, NaN where it does not cover a voxel.

    The map is resampled with geometry.resample_to_grid in "fraction" mode (volume-weighted average)
    over its finite non-zero voxels only (zero is taken as "outside the map"); a bundle voxel needs at
    least ``min_coverage`` of its volume covered by such voxels.
    """
    from .geometry import resample_to_grid, to_masked_vector
    from .nifti import read_nifti

    bundle = _open_bundle(bundle)
    data, affine, _ = read_nifti(os.fspath(nifti_path))
    data = np.asarray(data, dtype=np.float64)
    if data.ndim > 3:
        if int(np.prod(data.shape[3:])) != 1:
            raise ValueError(f"{nifti_path} has {int(np.prod(data.shape[3:]))} volumes; give a single 3D map")
        data = data.reshape(data.shape[:3])
    inside = np.isfinite(data) & (data != 0)
    values = np.where(inside, data, 0.0)
    total = resample_to_grid(values, affine, bundle.grid, mode="fraction")
    coverage = resample_to_grid(inside.astype(np.float64), affine, bundle.grid, mode="fraction")
    total = to_masked_vector(total, bundle.mask_indices).astype(np.float64)
    coverage = to_masked_vector(coverage, bundle.mask_indices).astype(np.float64)
    out = np.full(bundle.n_voxels, np.nan)
    covered = coverage >= min_coverage
    out[covered] = total[covered] / coverage[covered]
    return out


def compare_to_external(vec, nifti_path, bundle, min_coverage=0.5):
    """(Pearson r, Spearman rho) between a (V,) map and a user-supplied NIfTI map on the bundle grid (see external_on_grid)."""
    from .qa import degree_similarity

    return degree_similarity(vec, external_on_grid(nifti_path, bundle, min_coverage))


# Command line


def _add_selection_arguments(parser):
    parser.add_argument("--bundle", required=True, help="connectome bundle directory (.lnmc)")
    parser.add_argument("--subjects", type=jobs.positive_int, default=None, help="use the first N connectome subjects (default: all)")
    parser.add_argument("--subject-list", default=None, metavar="SPEC", help="a named subset of the bundle, a text file of subject ids or a comma-separated list of ids")


def _add_build_arguments(parser):
    parser.add_argument("--seeds", type=jobs.positive_int, default=DEFAULT_SEEDS, help=f"sampled seed voxels K for mu_z, sigma_r and hubness (default: {DEFAULT_SEEDS})")
    parser.add_argument("--pcs", type=jobs.positive_int, default=DEFAULT_PCS, help=f"number of principal components k (default: {DEFAULT_PCS})")
    parser.add_argument("--power-iters", type=int, default=DEFAULT_POWER_ITERS, help=f"power iterations (default: {DEFAULT_POWER_ITERS})")
    parser.add_argument("--oversample", type=int, default=DEFAULT_OVERSAMPLE, help=f"extra random columns (default: {DEFAULT_OVERSAMPLE})")
    parser.add_argument("--ev1", action="store_true", help="also compute EV1, the leading eigenvector of the uncentred r-matrix")
    parser.add_argument("--seed", type=int, default=0, help="random seed of the seed voxels (and seed + 1, seed + 2 of the random projections)")
    parser.add_argument("--chunk", type=jobs.positive_int, default=DEFAULT_CHUNK, help=f"voxels per matrix product (default: {DEFAULT_CHUNK})")
    parser.add_argument("--memory-gb", type=float, default=DEFAULT_MEMORY_BUDGET / 1024**3, help=f"memory budget in GB; above it the maps pass reads without read-ahead (default: {DEFAULT_MEMORY_BUDGET / 1024**3:.3g})")


def _check_selection(parser, args):
    if args.subject_list is not None and args.subjects is not None:
        parser.error("--subject-list cannot be combined with --subjects")
    if getattr(args, "power_iters", 0) < 0 or getattr(args, "oversample", 0) < 0:
        parser.error("--power-iters and --oversample must be at least 0")
    if getattr(args, "memory_gb", 1.0) <= 0:
        parser.error("--memory-gb must be positive")


def _build_main(argv):
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.reference build", description=f"Build the connectome reference maps ({DEFINITION}).")
    jobs.add_common_arguments(parser)
    _add_selection_arguments(parser)
    _add_build_arguments(parser)
    parser.add_argument("--out", default=None, help="output folder (default: <bundle>/derived/reference[/subj-<sha12>])")
    parser.add_argument("--hub", choices=HUB_MODES, default="auto", help="hubness: copy a valid derived/degree.npy (auto, default) or compute it from the seeds")
    args = parser.parse_args(argv)
    _check_selection(parser, args)
    cancel = jobs.CancelToken(args.cancel_file)
    try:
        build(
            args.bundle,
            out_dir=args.out,
            subjects=args.subjects,
            subject_list=args.subject_list,
            n_seeds=args.seeds,
            k=args.pcs,
            power_iters=args.power_iters,
            oversample=args.oversample,
            ev1=args.ev1,
            seed=args.seed,
            hub=args.hub,
            chunk=args.chunk,
            memory_budget=int(args.memory_gb * 1024**3),
            resume=args.resume,
            cancel=cancel,
            run_root=args.run_root,
            parent_runs=args.parent_run,
            argv=argv,
            threads=args.threads,
        )
    except (jobs.Cancelled, jobs.JobError):
        raise
    except (OSError, ValueError, KeyError) as exc:
        raise jobs.JobError(f"{type(exc).__name__}: {exc}", code="input") from None
    return jobs.EXIT_OK


def _estimate_main(argv):
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.reference estimate", description="Estimate the time, memory and reads of a reference build.")
    parser.add_argument("--threads", type=jobs.positive_int, default=2, help="BLAS threads (default: 2)")
    _add_selection_arguments(parser)
    _add_build_arguments(parser)
    args = parser.parse_args(argv)
    _check_selection(parser, args)
    try:
        bundle = ConnectomeBundle.open(args.bundle)
        jobs.emit("start", job="reference-estimate", argv=argv, threads=args.threads)
        info = estimate(bundle, args.subjects, args.subject_list, args.seeds, args.pcs, args.power_iters, args.oversample, args.ev1, args.chunk, int(args.memory_gb * 1024**3))
    except (OSError, ValueError, KeyError) as exc:
        raise jobs.JobError(f"{type(exc).__name__}: {exc}", code="input") from None
    jobs.emit("result", kind="estimate", **info)
    jobs.emit("done", outputs=[], elapsed=round(jobs.elapsed(), 3), run_id=None, seconds=info["seconds"])
    return jobs.EXIT_OK


def _compare_main(argv):
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.reference compare", description="Compare a user-supplied map (e.g. GSP1000_degree.nii.gz) with the reference maps.")
    parser.add_argument("--bundle", required=True, help="connectome bundle directory (.lnmc)")
    parser.add_argument("--external", required=True, help="NIfTI map to compare (resampled to the bundle grid)")
    parser.add_argument("--reference", default=None, help="reference folder (default: the bundle's)")
    parser.add_argument("--subject-list", default=None, metavar="SPEC", help="use the reference built from this subject list")
    parser.add_argument("--min-coverage", type=float, default=0.5, help="fraction of a voxel the external map must cover (default: 0.5)")
    args = parser.parse_args(argv)
    try:
        bundle = ConnectomeBundle.open(args.bundle)
        jobs.emit("start", job="reference-compare", argv=argv)
        subjects_sha = bundle.resolve_subject_list(args.subject_list).sha256 if args.subject_list else None
        reference = load_reference(bundle, subjects_sha=subjects_sha, path=args.reference)
        if reference is None:
            raise jobs.JobError("No valid reference maps for this connectome and subject list: build them with 'python -m LesionNetworkMappingLib.reference build'", code="no_reference")
        external = external_on_grid(args.external, bundle, args.min_coverage)
    except (OSError, ValueError, KeyError) as exc:
        raise jobs.JobError(f"{type(exc).__name__}: {exc}", code="input") from None
    from .qa import degree_similarity

    comparisons = {}
    for key, vector in reference.maps.items():
        r, rho = degree_similarity(vector, external)
        comparisons[key] = {"r": r, "rho": rho}
    jobs.emit("result", kind="comparison", external=os.path.abspath(args.external), reference=reference.path, reference_sha256=reference.sha256, n_voxels=int(np.count_nonzero(np.isfinite(external))), comparisons=comparisons)
    jobs.emit("done", outputs=[], elapsed=round(jobs.elapsed(), 3), run_id=None)
    return jobs.EXIT_OK


_COMMANDS = {"build": _build_main, "estimate": _estimate_main, "compare": _compare_main}


def main(argv=None):
    """Command-line entry point (see the module docstring)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in _COMMANDS:
        parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.reference", description=f"Connectome reference maps ({DEFINITION}).")
        parser.add_argument("command", choices=list(_COMMANDS))
        parser.parse_args(argv[:1])
        return jobs.EXIT_USAGE
    return _COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    jobs.run_job(main, job="reference")

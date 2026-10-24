"""Voxel-based lesion-symptom mapping (VLSM) of a study's lesions, and circuits seeded from its clusters.

VLSM asks where lesion *location* relates to a group or a score; lesion network mapping asks the same of the lesions'
connectivity. Running both on the same cohort answers the reviewer's question "does the network add anything beyond
lesion location?" (Salvalaggio et al. 2021, Brain 144:e36; Moore et al. 2024, Cortex 170:38), and a VLSM cluster can
seed a network (VLSM-seeded LNM: Boes 2021, Brain 144:e5; Bowren 2022).

- :func:`load_lesions` loads the lesions of a study onto one grid (``lesionset.load_occupancies``; the 2 mm template
  grid of the study's family by default), binarized at occupancy 0.5 like the overlap maps.
- :func:`lesion_matrix` restricts the binary lesion matrix (n lesions x grid voxels) to the voxels lesioned in at least
  the minimum number of lesions (:func:`min_lesion_count`: the larger of 5 lesions and 10% of the lesions by default,
  the usual VLSM minimum-overlap mask) and not in every lesion (no variance). The coverage map (lesions per voxel) of
  the analysed lesions is kept.
- :func:`vlsm` fits the design of ``design.py`` (two groups or ANOVA: ``group_column``; a score: ``continuous``)
  voxel by voxel through ``glm.run`` on the binary lesion values: Freedman-Lane permutations, voxel max-statistic FWE
  (primary) and BH-FDR (secondary), optional TFCE. Total lesion volume is a covariate by default (direct total-lesion-
  volume control, Moore et al. 2026, Hum Brain Mapp, doi:10.1002/hbm.70619); turning it off needs a reason, recorded like the GLM's. With lesion
  presence as the outcome and the score (or group) as the regressor, the t of the score equals the t of the classic
  VLSM regression "score ~ lesion + volume" (the partial correlation of the two given the covariates is symmetric, and
  both models have n - p degrees of freedom), so the maps are the usual VLSM t maps, with permutation inference.
- :func:`clusters` lists the connected clusters (26-connected, per sign) of the voxels that pass FWE (or FDR) at
  ``alpha``; :func:`seed_cluster` writes one cluster (or all significant voxels of a sign) as a binary ROI and maps it
  as a circuit with ``circuits.seed_this``, linked to the VLSM run and to the lesions that made it (lesions.tsv), so
  that damage scores of the same lesions are recognised as same-cohort (double dipping).

Outputs (``<out>``; default ``<study>/derivatives/lnm/vlsm/<contrast>``; maps on the exact header of the template mask,
else of the grid)::

    group_space-<sp>_desc-vlsm<Contrast>{Tstat|Fstat, PUnc, PFwe, PFdr, TfcePFwe, HedgesG|PartialR|PartialEtaSq}_map.nii.gz
    group_space-<sp>_desc-vlsmCoverage_map.nii.gz     int16: analysed lesions covering each voxel
    group_space-<sp>_desc-vlsmTested_mask.nii.gz      uint8: the voxels tested (minimum overlap)
    vlsm.json, design.json, design_matrix.tsv, lesions.tsv (participant_id, lesion_sha256, volume_ml; no paths)

P-value maps are plain p, 1 outside the tested voxels. vlsm.json stores its paths relative to its folder (SPEC 9.7
rule 10). The run is registered (kind "vlsm") in ``<run root>/derivatives/lnm/runs.jsonl`` (run root: --run-root,
else the study).

Command line (JSON events, see ``jobs``; exit codes 0 done, 1 error, 2 usage or blocked design, 3 cancelled, 75 locked)::

    python -m LesionNetworkMappingLib.vlsm run --study DIR [--design design.json] [--out DIR] [--grid template2mm|GRID|BUNDLE]
        [--space SPACE] [--min-lesions 5] [--min-percent 10] [--perm N] [--seed S] [--tfce] [--batch B]
        [--precision auto|float32|float64] [--threads K] [--cancel-file F] [--run-root DIR] [--parent-run ID]
    python -m LesionNetworkMappingLib.vlsm seed --vlsm DIR --bundle B --out DIR [--name NAME] [--cluster K|all]
        [--alpha 0.05] [--inference fwe|fdr] [--sign pos|neg] [--subjects N | --subject-list SPEC]
        [--threads K] [--cancel-file F] [--run-root DIR] [--parent-run ID]

This module imports numpy and scipy only (no Slicer, VTK or Qt).
"""

import os
import sys

if __name__ == "__main__":
    from LesionNetworkMappingLib.jobs import set_blas_threads

    set_blas_threads(sys.argv[1:])  # before numpy is imported: BLAS reads these variables once

import dataclasses
import json
import math
import re
import time

import numpy as np

from . import design as designlib
from . import glm, jobs, lesionset, nifti, runs
from . import tfce as tfcelib

FORMAT_NAME = "slicer-lnm-vlsm"
FORMAT_VERSION = 1
VLSM_JSON = "vlsm.json"
LESIONS_TSV = "lesions.tsv"
CLUSTER_JSON = "vlsm_cluster.json"
KIND = runs.register_kind("vlsm")
LOCK_NAME = ".vlsm.lock"
VLSM_FOLDER = ("derivatives", "lnm", "vlsm")
#: Tests VLSM runs: a group factor or a score (a one-sample test has nothing to compare lesion location with)
TESTS = ("two_group", "anova", "continuous")
#: The minimum-overlap mask: voxels lesioned in at least max(DEFAULT_MIN_LESIONS, DEFAULT_MIN_PERCENT % of the lesions)
DEFAULT_MIN_LESIONS = 5
DEFAULT_MIN_PERCENT = 10.0
BINARIZE_AT = lesionset.BINARIZE_AT
CLUSTER_ALPHA = 0.05
INFERENCES = ("fwe", "fdr")
CLUSTER_SIGNS = ("pos", "neg")
CONNECTIVITY = 26
#: Paths of vlsm.json (dotted keys), stored relative to its folder
RECORD_PATH_FIELDS = ("inputs.study", "inputs.design")
#: Output keys of glm.write_maps and their "desc" suffixes
STAT_SUFFIX = {"t": "Tstat", "v": "Tstat", "F": "Fstat"}
INTERPRETATION = (
    "VLSM maps where lesion location relates to the group or score after total lesion volume; it tests location, not "
    "the network. Compare it with the network map of the same cohort to ask whether the network adds anything beyond "
    "lesion location (Salvalaggio et al. 2021; Moore et al. 2024)."
)


def _quiet(*args, **kwargs):
    return None


def _space_label(space):
    return lesionset._space_label(space)


def contrast_desc(design):
    """The "desc" stem of a design's VLSM outputs: "vlsm" + the GLM contrast label (vlsmCaseGtControl, vlsmNihssPos...)."""
    label = glm.contrast_label(design)
    return "vlsm" + label[:1].upper() + label[1:]


def output_path(out_dir, space, desc, suffix="map"):
    """``<out_dir>/group_space-<sp>_desc-<desc>_<suffix>.nii.gz`` (letters and digits only in ``desc``)."""
    if not re.fullmatch(r"[A-Za-z0-9]+", desc):
        raise ValueError(f"A desc label must contain only letters and digits: {desc!r}")
    return os.path.join(os.fspath(out_dir), f"group_space-{_space_label(space)}_desc-{desc}_{suffix}.nii.gz")


def default_out_dir(study_path, design):
    """``<study>/derivatives/lnm/vlsm/<contrast label>``."""
    return os.path.join(os.fspath(study_path), *VLSM_FOLDER, glm.contrast_label(design))


def vlsm_root(study_path):
    """``<study>/derivatives/lnm/vlsm``: the folder of the study's VLSM results."""
    return os.path.join(os.fspath(study_path), *VLSM_FOLDER)


#
# Lesion matrix
#


def min_lesion_count(n, min_lesions=DEFAULT_MIN_LESIONS, min_percent=DEFAULT_MIN_PERCENT):
    """The minimum number of lesions a voxel must be in to be tested: max(min_lesions, ceil(min_percent % of n)), at least 1."""
    count = int(min_lesions or 0)
    if min_percent:
        count = max(count, math.ceil(float(min_percent) / 100.0 * int(n) - 1e-9))
    return max(count, 1)


def min_lesion_rule(min_lesions=DEFAULT_MIN_LESIONS, min_percent=DEFAULT_MIN_PERCENT):
    """The minimum-overlap rule as read: "max(5, 10%)", "5 lesions", "10%"."""
    parts = []
    if min_lesions:
        parts.append(f"{int(min_lesions)}")
    if min_percent:
        parts.append(f"{float(min_percent):g}%")
    if len(parts) == 2:
        return f"max({parts[0]}, {parts[1]})"
    if parts:
        return parts[0] + (" lesions" if min_lesions else "")
    return "1 lesion"


@dataclasses.dataclass
class LesionMatrix:
    """Binary lesion values of the tested voxels.

    :ivar Y: n x V float32 (1 where the lesion covers the voxel), rows in ``sids`` order.
    :ivar indices: flat C-order indices of the V tested voxels in the grid's [i, j, k] array.
    :ivar coverage: int array (grid shape): analysed lesions covering each voxel.
    :ivar min_count: minimum lesions per tested voxel; ``n_all_lesioned``: voxels dropped because every lesion covers them.
    """

    Y: np.ndarray
    indices: np.ndarray
    coverage: np.ndarray
    sids: list
    min_count: int
    n_all_lesioned: int
    grid: object

    @property
    def n(self):
        return len(self.sids)

    @property
    def n_voxels(self):
        return int(self.indices.size)

    def tested_mask(self):
        """Boolean volume (grid shape) of the tested voxels."""
        mask = np.zeros(int(np.prod(self.grid.shape)), dtype=bool)
        mask[self.indices] = True
        return mask.reshape(self.grid.shape)


def lesion_matrix(occ, sids=None, min_count=None, binarize_at=BINARIZE_AT, memmap_path=None, chunk=65536):
    """The binary lesion matrix of a :class:`lesionset.LesionSet` restricted to the minimum-overlap voxels.

    :param sids: participant ids in the order of the rows (default: the set's order).
    :param min_count: minimum lesions per voxel (default: :func:`min_lesion_count` of n).
    :param memmap_path: write Y to this .npy memmap (large cohorts) instead of memory.
    :return: :class:`LesionMatrix`.
    """
    sids = list(occ.sids if sids is None else sids)
    position = {sid: index for index, sid in enumerate(occ.sids)}
    missing = [sid for sid in sids if sid not in position]
    if missing:
        raise ValueError(f"No lesion loaded for {', '.join(missing[:10])}")
    binary = occ.binary(binarize_at)[[position[sid] for sid in sids]].tocsc()
    n = len(sids)
    counts = np.diff(binary.indptr).astype(np.int64)  # lesions per grid voxel (CSC: one column per voxel)
    min_count = min_lesion_count(n) if min_count is None else max(int(min_count), 1)
    keep = (counts >= min_count) & (counts < n)
    all_lesioned = int(np.count_nonzero(counts >= max(n, 1))) if n else 0
    indices = np.flatnonzero(keep)
    if memmap_path:
        Y = np.lib.format.open_memmap(memmap_path, mode="w+", dtype=np.float32, shape=(n, indices.size))
    else:
        Y = np.zeros((n, indices.size), dtype=np.float32)
    for start in range(0, indices.size, chunk):
        columns = indices[start:start + chunk]
        Y[:, start:start + columns.size] = binary[:, columns].toarray()
    if memmap_path:
        Y.flush()
    dtype = np.int16 if n <= np.iinfo(np.int16).max else np.int32
    coverage = counts.astype(dtype).reshape(occ.grid.shape)
    return LesionMatrix(Y, indices, coverage, sids, min_count, all_lesioned, occ.grid)


def load_lesions(study, grid=None, space=None, template_dirs=(), cancel=None, progress=None):
    """The study's lesions on one grid (``lesionset.load_occupancies``: included rows, listwise exclusion, one template
    family; the 2 mm template grid of the study's family by default).
    """
    return lesionset.load_occupancies(study, grid=grid, space=space, template_dirs=template_dirs, cancel=cancel, progress=progress)


#
# Design
#


@dataclasses.dataclass
class Prepared:
    """What :func:`prepare` returns: the design, the lesions, the lesion matrix and how volumes were filled."""

    study: object
    design: object
    occ: object
    matrix: LesionMatrix
    volume_from_masks: int
    min_lesions: int
    min_percent: float
    n_perm: int
    count: int
    exhaustive: bool


def _check(level, code, message, detail=None):
    return designlib.Check(level, code, message, detail)


def prepare(study, spec=None, grid=None, space=None, min_lesions=DEFAULT_MIN_LESIONS, min_percent=DEFAULT_MIN_PERCENT, n_perm=None, occ=None, binarize_at=BINARIZE_AT,
            template_dirs=(), cancel=None, progress=None, memmap_path=None):
    """Build the design of a VLSM on a study's lesions and the lesion matrix of its subjects.

    :param study: ``study.Study`` or its folder.
    :param spec: ``design.DesignSpec`` (dict or design.json path; default: two groups, case vs control, volume covariate).
    :param n_perm: permutations to run (default: the spec's; fewer than ``design.MIN_N_PERM`` is a preview).
    :param occ: lesions already loaded (:func:`load_lesions`), else they are loaded.
    :return: :class:`Prepared`; problems of the data are design checks (``design.blocking``).
    :raises design.DesignError: an invalid spec, a test VLSM cannot run (one sample), or no subject left.
    """
    from .study import Study

    if not isinstance(study, Study):
        study = Study.open(study)
    spec = designlib.as_spec(spec)
    if spec.test not in TESTS:
        raise designlib.DesignError(f"VLSM needs a group or a score to compare lesion locations with; a {spec.test} test is not available")
    if occ is None:
        occ = load_lesions(study, grid=grid, space=space, template_dirs=template_dirs, cancel=cancel, progress=progress)
    if not len(occ):
        excluded = "; ".join(f"{reason}: {len(sids)}" for reason, sids in occ.excluded.items())
        raise designlib.DesignError(f"No lesion could be loaded ({excluded or 'the study has no lesions'})")
    volumes = {sid: f"{float(volume):.6g}" for sid, volume in zip(occ.sids, occ.volumes_ml, strict=True)}
    subset, filled = glm._subset_study(study, set(occ.sids), volumes)
    design = designlib.build_design(subset, spec)
    not_loaded = {reason: list(sids) for reason, sids in occ.excluded.items() if sids}
    known = {sid for sids in design.excluded.values() for sid in sids}
    for reason, sids in not_loaded.items():
        sids = [sid for sid in sids if sid not in known]
        if sids:
            design.excluded.setdefault(reason, []).extend(sids)
    checks = list(design.checks)
    dropped = sum(len(sids) for sids in not_loaded.values())
    if dropped:
        checks.append(_check("info", "lesions_not_loaded", f"{dropped} subject(s) have no usable lesion and are excluded", {reason: len(sids) for reason, sids in not_loaded.items()}))
    count_min = min_lesion_count(design.n, min_lesions, min_percent)
    matrix = lesion_matrix(occ, design.sids, count_min, binarize_at=binarize_at, memmap_path=memmap_path)
    rule = min_lesion_rule(min_lesions, min_percent)
    if matrix.n_voxels == 0:
        checks.append(_check("block", "no_tested_voxels", f"No voxel is lesioned in at least {count_min} of the {design.n} lesions ({rule}): lower the minimum overlap, or add lesions",
                             {"min_count": count_min, "n": design.n}))
    else:
        checks.append(_check("info", "min_overlap", f"{matrix.n_voxels:,} voxels are lesioned in at least {count_min} of the {design.n} lesions ({rule}) and are tested",
                             {"min_count": count_min, "n_voxels": matrix.n_voxels}))
    if matrix.n_all_lesioned:
        checks.append(_check("info", "all_lesioned", f"{matrix.n_all_lesioned} voxel(s) lesioned in every lesion are not tested (no variance)", {"n_voxels": matrix.n_all_lesioned}))
    design.checks = designlib._ordered(checks)
    requested = int(spec.n_perm if n_perm is None else n_perm)
    count, exhaustive = glm.shuffling_count(design, requested)
    if requested != design.spec.n_perm:
        glm._override_permutations(design, requested, count, exhaustive)
    return Prepared(subset, design, occ, matrix, filled, int(min_lesions or 0), float(min_percent or 0.0), requested, count, exhaustive)


def lesion_rows(prepared):
    """Rows of lesions.tsv: participant_id, lesion_sha256 (of the lesion file), group (when the study has the column) and volume_ml."""
    from .export import file_sha256

    occ, design = prepared.occ, prepared.design
    position = {sid: index for index, sid in enumerate(occ.sids)}
    used = {row["participant_id"]: row for row in prepared.study.subjects}
    rows = []
    for sid in design.sids:
        row = occ.rows.get(sid) or used.get(sid) or {}
        path = prepared.study.abspath(row.get("lesion")) if row.get("lesion") else None
        sha = file_sha256(path) if path and os.path.isfile(path) else "n/a"
        volume = (used.get(sid) or {}).get(designlib.VOLUME_COLUMN)
        rows.append({"participant_id": sid, "lesion_sha256": sha, "group": row.get("group") or "n/a",
                     "volume_ml": volume if volume not in (None, "") else f"{float(occ.volumes_ml[position[sid]]):.6g}"})
    return rows


def write_lesions_tsv(path, rows):
    columns = ("participant_id", "lesion_sha256", "group", "volume_ml")
    from .schema import write_text_atomic

    lines = ["\t".join(columns)] + ["\t".join(str(row.get(column, "n/a")) for column in columns) for row in rows]

    write_text_atomic(path, "\n".join(lines) + "\n")
    return path


#
# Run
#


def _reference_header(prepared, out_dir, space):
    """Write the coverage map and the tested mask on the template's exact header (else the grid's); return their paths."""
    occ, matrix = prepared.occ, prepared.matrix
    coverage_path = output_path(out_dir, space, "vlsmCoverage")
    lesionset._write_map(coverage_path, matrix.coverage, occ.grid, occ.reference, f"VLSM coverage: lesions per voxel of {matrix.n}")
    tested_path = output_path(out_dir, space, "vlsmTested", "mask")
    lesionset._write_map(tested_path, matrix.tested_mask().astype(np.uint8), occ.grid, occ.reference, f"VLSM tested voxels: lesioned in >= {matrix.min_count}")
    return coverage_path, tested_path


def write_maps(glm_result, prepared, out_dir, space=None, fwe=True, fdr=True):
    """Write the VLSM maps (statistic, p maps, effect size), the coverage map and the tested mask.

    :return: {key: path} with keys stat, p_unc, p_fwe, p_fdr, tfce_p_fwe, effect, coverage and tested (as written).
    """
    space = space or prepared.occ.space
    result = glm_result.result
    coverage_path, tested_path = _reference_header(prepared, out_dir, space)
    record = nifti.read_raw_header(coverage_path)
    shape = tuple(prepared.occ.grid.shape)
    indices = prepared.matrix.indices
    desc = contrast_desc(prepared.design)
    df = float(result.df)
    stat_intent = (4, (float(result.df1), df, 0.0)) if result.kind == "F" else (3, (df, 0.0, 0.0)) if result.kind == "t" else (0, (0.0, 0.0, 0.0))
    stat_text = {"t": "t", "F": "F", "v": "Aspin-Welch v"}[result.kind]
    p_intent = (22, (0.0, 0.0, 0.0))
    items = [("stat", STAT_SUFFIX[result.kind], result.stat, 0.0, f"VLSM {stat_text} df={result.df}", stat_intent),
             ("p_unc", "PUnc", result.p_unc, 1.0, f"VLSM uncorrected p K={result.K}", p_intent)]
    if fwe:
        items.append(("p_fwe", "PFwe", result.p_fwe, 1.0, f"VLSM max-stat FWE p K={result.K}", p_intent))
    if fdr:
        items.append(("p_fdr", "PFdr", result.q_fdr, 1.0, "VLSM BH-FDR q", p_intent))
    if result.tfce_p_fwe is not None:
        items.append(("tfce_p_fwe", "TfcePFwe", result.tfce_p_fwe, 1.0, f"VLSM TFCE FWE p K={result.K}", p_intent))
    effect_name = glm.EFFECT_NAMES[glm_result.effect_kind]
    items.append(("effect", effect_name, glm_result.effect, 0.0, f"VLSM {effect_name}", (0, (0.0, 0.0, 0.0))))
    paths = {}
    for key, name, vector, fill, descrip, (intent_code, intent_params) in items:
        path = output_path(out_dir, space, desc + name)
        glm._write_on_mask(path, vector, record, indices, shape, fill, descrip, intent_code, intent_params)
        paths[key] = path
    paths["coverage"] = coverage_path
    paths["tested"] = tested_path
    return paths


def write_record(path, record):
    """Write vlsm.json atomically with its paths relative to its folder (SPEC 9.7 rule 10)."""
    from .export import portable_record_paths

    jobs.write_json_atomic(path, portable_record_paths(record, os.path.dirname(os.path.abspath(path)), RECORD_PATH_FIELDS))


def read_record(path):
    """vlsm.json (a file or its folder) with its paths absolute, or None."""
    from .export import resolve_record_paths

    path = os.fspath(path)
    if os.path.isdir(path):
        path = os.path.join(path, VLSM_JSON)
    try:
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("format") != FORMAT_NAME:
        return None
    return resolve_record_paths(record, os.path.dirname(os.path.abspath(path)), RECORD_PATH_FIELDS)


def find_results(study_path):
    """Folders of the study's finished VLSM results (``<study>/derivatives/lnm/vlsm/*/vlsm.json``), oldest first."""
    root = vlsm_root(study_path)
    if not os.path.isdir(root):
        return []
    found = []
    for name in sorted(os.listdir(root)):
        folder = os.path.join(root, name)
        record = read_record(folder)
        if record is not None and record.get("status") == "done":
            found.append((os.path.getmtime(os.path.join(folder, VLSM_JSON)), folder))
    return [folder for _stamp, folder in sorted(found)]


def _remove_previous_outputs(out_dir):
    previous = read_record(out_dir)
    if previous:
        for name in list((previous.get("outputs") or {}).values()):
            path = os.path.join(out_dir, os.path.basename(str(name)))
            if os.path.isfile(path):
                os.remove(path)
    path = os.path.join(out_dir, VLSM_JSON)
    if os.path.exists(path):
        os.remove(path)


def vlsm(study, spec=None, grid=None, space=None, min_lesions=DEFAULT_MIN_LESIONS, min_percent=DEFAULT_MIN_PERCENT, n_perm=None, seed=None, out_dir=None, tfce=None,
         batch=None, dtype=None, run_root=None, parent_runs=(), register=True, template_dirs=(), cancel=None, emit=None, argv=None, design_path=None, threads=None, keep_matrix=False):
    """Run a VLSM on a study's lesions and write its outputs (see the module docstring).

    :param study: ``study.Study`` or its folder.
    :param spec: ``design.DesignSpec``, dict or design.json path (default: two groups case vs control, volume covariate).
    :param grid: the grid the lesions are loaded onto (``lesionset.resolve_grid``; default the 2 mm template grid).
    :param min_lesions, min_percent: the minimum-overlap mask (default: lesioned in at least max(5, 10%) of the lesions).
    :param n_perm, seed: override the spec's.
    :param out_dir: output folder (default: ``<study>/derivatives/lnm/vlsm/<contrast>``).
    :param emit: ``emit(event, **fields)`` for job events (default: none).
    :param design_path: the design.json the spec was read from (recorded).
    :return: vlsm.json content with absolute paths (``record`` is its path).
    :raises design.DesignError: an invalid spec; ``jobs.JobError`` (exit code 2) for a blocked design.
    """
    from .export import DISCLAIMER, LIBRARY_VERSION, now_iso, versions
    from .study import Study

    emit = emit or _quiet
    started = time.monotonic()
    timings = {}
    if not isinstance(study, Study):
        study = Study.open(study)
    spec = designlib.as_spec(spec)
    inference = dict(spec.inference)
    if seed is not None:
        inference["seed"] = int(seed)
    if tfce is not None:
        inference["tfce"] = bool(tfce)
    if n_perm is not None and n_perm >= designlib.MIN_N_PERM:
        inference["n_perm"] = int(n_perm)
    spec = dataclasses.replace(spec, inference=inference)
    n_perm = int(spec.n_perm if n_perm is None else n_perm)

    emit("start", job="vlsm", argv=list(argv or []), threads=threads, test=spec.test, contrast=spec.contrast)
    emit("stage", name="lesions", index=1, total=3)
    tic = time.monotonic()

    def progress(done, total):
        emit("progress", stage="lesions", done=done, total=total)

    occ = load_lesions(study, grid=grid, space=space, template_dirs=template_dirs, cancel=cancel, progress=progress)
    timings["load_s"] = time.monotonic() - tic
    try:
        prepared = prepare(study, spec, occ=occ, min_lesions=min_lesions, min_percent=min_percent, n_perm=n_perm)
    except designlib.DesignError as exc:
        raise jobs.JobError(f"The design cannot be built: {exc}", code="design_blocked", exit_code=jobs.EXIT_USAGE) from None
    design = prepared.design
    if design.blocking:
        messages = [check.message for check in design.blocking]
        raise jobs.JobError("The VLSM is blocked: " + " | ".join(messages), code="design_blocked", exit_code=jobs.EXIT_USAGE, checks=[check.to_dict() for check in design.blocking])
    for check in design.warnings:
        emit("warning", code=check.code, message=check.message)
    out_dir = os.path.abspath(os.fspath(out_dir or default_out_dir(study.path, design)))
    os.makedirs(out_dir, exist_ok=True)
    lock = jobs.OutputLock(os.path.join(out_dir, LOCK_NAME))
    lock.acquire()
    run_root = os.path.abspath(os.fspath(run_root or study.path))
    run_id, record = None, None
    space_label = prepared.occ.space
    desc = contrast_desc(design)
    matrix = prepared.matrix
    try:
        params = {
            "design": runs.params_sha256(design.to_dict(matrix=True)),
            "n_perm": n_perm,
            "seed": spec.seed,
            "count": prepared.count,
            "min_count": matrix.min_count,
            "min_lesions": prepared.min_lesions,
            "min_percent": prepared.min_percent,
            "grid": {"label": prepared.occ.label, "shape": list(prepared.occ.grid.shape)},
            "space": space_label,
            "tfce": bool(spec.inference.get("tfce")),
        }
        if register:
            run_id = runs.register(run_root, KIND, out_dir, parents=[run for run in parent_runs or () if run],
                                   params={**params, "test": spec.test, "contrast": spec.contrast, "desc": desc}, seeds={"permutation": spec.seed})
        record = {"format": FORMAT_NAME, "version": FORMAT_VERSION, "run_id": run_id, "status": "running", "argv": list(argv or []), "start": now_iso(), "end": None}
        two_sided = design.tails == 2 and design.stat != "F"
        tfce_grid = tfcelib.TfceGrid(matrix.tested_mask()) if spec.inference.get("tfce") else None
        _remove_previous_outputs(out_dir)
        emit("stage", name="permutations", index=2, total=3, total_permutations=prepared.count - 1, run_id=run_id, n=design.n, p=design.p, n_voxels=matrix.n_voxels, min_count=matrix.min_count, stat=design.stat,
             tails=design.tails, design_type=design.design_type, permute=design.permute, permute_reason=design.permute_reason, n_perm=prepared.count, exhaustive=prepared.exhaustive,
             tfce=tfce_grid is not None, tfce_eta_s=tfcelib.tfce_eta(tfce_grid, prepared.count, two_sided) if tfce_grid is not None else None)
        session = {"started": time.monotonic()}

        def on_batch(state, done, total, elapsed):
            spent = time.monotonic() - session["started"]
            eta = spent / done * max((total or done) - done, 0) if done > 0 else None
            emit("progress", stage="permutations", done=done, total=total, elapsed=round(spent, 3), eta=None if eta is None else round(eta, 3))

        glm_result = glm.run(design, matrix.Y, n_perm=n_perm, seed=spec.seed, mask=tfce_grid, tfce=tfce_grid is not None, batch=batch, dtype=dtype, cancel=cancel, on_batch=on_batch)
        result = glm_result.result
        if result.n_perm != prepared.count:
            raise RuntimeError(f"Expected {prepared.count} shufflings, the design yielded {result.n_perm}")
        timings["permutations_s"] = result.timings.get("permutations_s")
        timings["per_permutation_ms"] = result.timings.get("per_permutation_ms")

        emit("stage", name="write", index=3, total=3)
        tic = time.monotonic()
        paths = write_maps(glm_result, prepared, out_dir, space_label, fwe=bool(spec.inference.get("fwe", True)), fdr=bool(spec.inference.get("fdr", True)))
        design_json, design_tsv = designlib.save_design(design, out_dir)
        lesions_tsv = write_lesions_tsv(os.path.join(out_dir, LESIONS_TSV), lesion_rows(prepared))
        timings["write_s"] = time.monotonic() - tic
        numbers = glm.summary(glm_result)
        found = clusters_of_result(glm_result, prepared)
        coverage = matrix.coverage
        record.update({
            "status": "done",
            "end": now_iso(),
            "elapsed": round(time.monotonic() - started, 3),
            "library_version": LIBRARY_VERSION,
            "versions": versions(),
            "precision": result.precision,
            "inputs": {
                "study": study.path,
                "design": os.path.abspath(os.fspath(design_path)) if design_path else None,
                "lesions": "lesion column of participants.tsv (binarized at occupancy %g)" % BINARIZE_AT,
            },
            "space": space_label,
            "grid": {"label": prepared.occ.label, "shape": [int(v) for v in prepared.occ.grid.shape], "voxel_mm3": float(prepared.occ.grid.voxel_volume)},
            "test": spec.test,
            "design_type": design.design_type,
            "contrast": {"label": glm_result.label, "desc": desc, "spec": spec.contrast, "names": list(design.contrast_names), "matrix": design.C.tolist(), "columns": list(design.columns)},
            "n": design.n,
            "n_per_level": dict(design.n_per_level),
            "excluded": {reason: list(sids) for reason, sids in design.excluded.items()},
            "n_excluded": sum(len(sids) for sids in design.excluded.values()),
            "covariates": list(spec.covariates),
            "transforms": dict(spec.transforms),
            "volume_covariate": designlib.VOLUME_COLUMN in spec.covariates,
            "volume_off_reason": spec.volume_off_reason,
            "volume_from_masks": prepared.volume_from_masks,
            "min_overlap": {"rule": min_lesion_rule(prepared.min_lesions, prepared.min_percent), "min_lesions": prepared.min_lesions, "min_percent": prepared.min_percent,
                            "min_count": matrix.min_count, "n_voxels_tested": matrix.n_voxels, "n_voxels_all_lesioned": matrix.n_all_lesioned,
                            "max_coverage": int(coverage.max()) if coverage.size else 0, "binarize_at": BINARIZE_AT},
            "n_voxels": matrix.n_voxels,
            "permutation": {
                "method": "Freedman-Lane (Winkler et al. 2014) with Beckmann partitioning, on binary lesion values",
                "scheme": design.permute,
                "reason": design.permute_reason,
                "exchange": design.exchange,
                "block_column": design.block_column,
                "n_perm": int(result.n_perm),
                "n_perm_requested": int(n_perm),
                "unique_perms": designlib._count_json(design.unique_perms),
                "exhaustive": bool(prepared.exhaustive),
                "seed": int(spec.seed),
                "preview": n_perm < designlib.MIN_N_PERM and not prepared.exhaustive,
            },
            "seeds": {"permutation": int(spec.seed)},
            **numbers,
            "fwe": {**numbers["fwe"], "enabled": bool(spec.inference.get("fwe", True)), "primary": True},
            "fdr": {**numbers["fdr"], "enabled": bool(spec.inference.get("fdr", True))},
            "tfce": {"enabled": tfce_grid is not None, **(numbers.get("tfce_fwe") or {})},
            "clusters": {"alpha": CLUSTER_ALPHA, "inference": "fwe", "connectivity": CONNECTIVITY, "n": len(found), "table": found[:50],
                         "note": "Clusters of the voxels with FWE p <= 0.05 are descriptive (no cluster-extent inference)."},
            "cluster_inference": None,
            "interpretation": INTERPRETATION,
            "limitations": glm.limitations(design),
            "checks": [check.to_dict() for check in design.checks],
            "warnings": [check.message for check in design.warnings],
            "outputs": {key: os.path.basename(path) for key, path in paths.items()},
            "design_json": os.path.basename(design_json),
            "design_matrix": os.path.basename(design_tsv),
            "lesions_tsv": os.path.basename(lesions_tsv),
            "timings": {key: (None if value is None else round(float(value), 4)) for key, value in timings.items()},
            "disclaimer": DISCLAIMER,
        })
        record.pop("tfce_fwe", None)
        json_path = os.path.join(out_dir, VLSM_JSON)
        write_record(json_path, record)
        outputs = [*paths.values(), design_json, design_tsv, lesions_tsv, json_path]
        for path in outputs:
            emit("result", kind="vlsm", path=path)
        if run_id:
            runs.update(run_root, run_id, status="done", ended=record["end"], outputs=[os.path.basename(path) for path in outputs],
                        summary={"n": design.n, "n_voxels": matrix.n_voxels, "n_perm": int(result.n_perm), "fwe_significant": numbers["fwe"]["n_significant"], "min_p": result.min_p,
                                 "design_type": design.design_type})
        emit("done", outputs=outputs, elapsed=round(time.monotonic() - started, 3), run_id=run_id, n_perm=int(result.n_perm), n_voxels=matrix.n_voxels)
        out = read_record(json_path)
        out["record"] = json_path
        if keep_matrix:
            out["_prepared"] = prepared
            out["_result"] = glm_result
        return out
    except jobs.Cancelled as exc:
        if record is not None:
            record.update({"status": "cancelled", "end": now_iso(), "stop_reason": exc.reason, "permutations_done": exc.done})
            write_record(os.path.join(out_dir, VLSM_JSON), record)
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


#
# Clusters
#


def _label_clusters(significant, stat3d, affine, voxel_ml, sign):
    from scipy import ndimage

    structure = ndimage.generate_binary_structure(3, 3)
    labels, count = ndimage.label(significant, structure=structure)
    out = []
    for index in range(1, count + 1):
        voxels = np.argwhere(labels == index)
        values = stat3d[tuple(voxels.T)]
        peak = int(np.argmax(values) if sign == "pos" else np.argmin(values))
        world = _world(voxels, affine)
        out.append({"sign": sign, "voxels": len(voxels), "volume_ml": round(len(voxels) * voxel_ml, 4), "peak_stat": float(values[peak]),
                    "peak_ijk": [int(v) for v in voxels[peak]], "peak_xyz": [round(float(v), 2) for v in world[peak]],
                    "centroid_xyz": [round(float(v), 2) for v in world.mean(axis=0)], "_label": labels == index})
    return out


def _world(ijk, affine):
    """World (RAS mm) coordinates of voxel indices (n x 3)."""
    ijk = np.asarray(ijk, dtype=np.float64).reshape(-1, 3)
    return ijk @ np.asarray(affine, dtype=np.float64)[:3, :3].T + np.asarray(affine, dtype=np.float64)[:3, 3]


def _passing(p, alpha):
    """p <= alpha, also for p stored in float32: a permutation p of exactly alpha (for example 50/1000) is read back as
    float32(0.05), slightly above the float64 0.05.
    """
    p = np.asarray(p, dtype=np.float64)
    return np.isfinite(p) & (p <= float(alpha) * (1.0 + 1e-6))


def _cluster_list(stat3d, p3d, affine, alpha, sign, voxel_ml, min_voxels=1, masks=False):
    signs = CLUSTER_SIGNS if sign in (None, "both") else (sign,)
    found = []
    passing = _passing(p3d, alpha)
    for part in signs:
        significant = passing & (stat3d > 0 if part == "pos" else stat3d < 0)
        found += [item for item in _label_clusters(significant, stat3d, affine, voxel_ml, part) if item["voxels"] >= int(min_voxels)]
    found.sort(key=lambda item: (-item["voxels"], -abs(item["peak_stat"])))
    for number, item in enumerate(found, start=1):
        item["id"] = number
        if not masks:
            item.pop("_label")
    return found


def clusters_of_result(glm_result, prepared, alpha=CLUSTER_ALPHA, sign="both", min_voxels=1):
    """The FWE clusters of a VLSM result in memory (vlsm.json's table): id, sign, voxels, volume_ml, peak_stat, peak_ijk, peak_xyz, centroid_xyz."""
    grid = prepared.occ.grid
    size = int(np.prod(grid.shape))
    stat = np.zeros(size)
    p = np.ones(size)
    stat[prepared.matrix.indices] = glm_result.result.stat
    p[prepared.matrix.indices] = glm_result.result.p_fwe
    return _cluster_list(stat.reshape(grid.shape), p.reshape(grid.shape), grid.affine, alpha, sign, grid.voxel_volume / 1000.0, min_voxels)


def _result_maps(record, inference):
    folder = os.path.dirname(record["record"]) if record.get("record") else None
    outputs = record.get("outputs") or {}
    key = {"fwe": "p_fwe", "fdr": "p_fdr"}[inference]
    if not outputs.get("stat") or not outputs.get(key):
        raise ValueError(f"The VLSM result has no {inference.upper()} map")
    return os.path.join(folder, outputs["stat"]), os.path.join(folder, outputs[key])


def _load_record(vlsm_result):
    if isinstance(vlsm_result, dict):
        return vlsm_result
    path = os.fspath(vlsm_result)
    record = read_record(path)
    if record is None:
        raise ValueError(f"No finished VLSM result in {path}")
    record["record"] = path if path.endswith(".json") else os.path.join(path, VLSM_JSON)
    return record


def clusters(vlsm_result, alpha=CLUSTER_ALPHA, inference="fwe", sign="both", min_voxels=1, masks=False):
    """Connected clusters (26-connected, per sign) of the voxels of a VLSM result with p <= ``alpha``.

    :param vlsm_result: a VLSM folder, its vlsm.json, or :func:`vlsm`'s record.
    :param inference: "fwe" (max-statistic, primary) or "fdr".
    :param sign: "both", "pos" (positive statistic) or "neg".
    :param masks: keep each cluster's boolean volume under "_label".
    :return: [{"id", "sign", "voxels", "volume_ml", "peak_stat", "peak_ijk", "peak_xyz", "centroid_xyz"}], largest first.
    """
    if inference not in INFERENCES:
        raise ValueError(f"inference must be one of {', '.join(INFERENCES)}, got {inference!r}")
    if sign not in ("both", *CLUSTER_SIGNS):
        raise ValueError(f"sign must be both, pos or neg, got {sign!r}")
    record = _load_record(vlsm_result)
    stat_path, p_path = _result_maps(record, inference)
    stat, affine, _ = nifti.read_nifti(stat_path)
    p = nifti.read_nifti(p_path)[0]
    stat = np.asarray(stat, dtype=np.float64)
    voxel_ml = abs(float(np.linalg.det(np.asarray(affine)[:3, :3]))) / 1000.0
    return _cluster_list(stat, np.asarray(p, dtype=np.float64), affine, alpha, sign, voxel_ml, min_voxels, masks)


def cluster_roi(vlsm_result, cluster=1, alpha=CLUSTER_ALPHA, inference="fwe", sign="pos"):
    """(uint8 ROI [i, j, k], affine, info) of one cluster (its id) or of every significant voxel of ``sign`` ("all").

    :raises ValueError: no such cluster, or no significant voxel.
    """
    record = _load_record(vlsm_result)
    stat_path, p_path = _result_maps(record, inference)
    stat, affine, _ = nifti.read_nifti(stat_path)
    stat = np.asarray(stat, dtype=np.float64)
    p = np.asarray(nifti.read_nifti(p_path)[0], dtype=np.float64)
    voxel_ml = abs(float(np.linalg.det(np.asarray(affine)[:3, :3]))) / 1000.0
    if str(cluster) == "all":
        if sign not in CLUSTER_SIGNS:
            raise ValueError("All significant voxels need one sign: pos or neg")
        roi = _passing(p, alpha) & (stat > 0 if sign == "pos" else stat < 0)
        if not roi.any():
            raise ValueError(f"No voxel has {inference.upper()} p <= {alpha:g} with a {'positive' if sign == 'pos' else 'negative'} statistic")
        voxels = np.argwhere(roi)
        info = {"id": "all", "sign": sign, "voxels": int(roi.sum()), "volume_ml": round(int(roi.sum()) * voxel_ml, 4),
                "centroid_xyz": [round(float(v), 2) for v in _world(voxels, affine).mean(axis=0)]}
    else:
        found = _cluster_list(stat, p, affine, alpha, "both", voxel_ml, masks=True)
        match = [item for item in found if item["id"] == int(cluster)]
        if not match:
            raise ValueError(f"No cluster {cluster} with {inference.upper()} p <= {alpha:g} (the result has {len(found)})")
        info = dict(match[0])
        roi = info.pop("_label")
    info.update({"alpha": float(alpha), "inference": inference})
    return roi.astype(np.uint8), np.asarray(affine, dtype=np.float64), info


def seed_cluster(vlsm_result, bundle, out_dir, name=None, cluster=1, alpha=CLUSTER_ALPHA, inference="fwe", sign="pos", subjects=None, subject_list=None, run_root=None,
                 parent_runs=(), register=True, cancel=None, emit=None, argv=None):
    """Map a VLSM cluster as a circuit (``circuits.seed_this``): the cluster is written as a binary ROI in ``out_dir``, the
    circuit is linked to the VLSM statistic map, its run and the lesions that made it (lesions.tsv).

    :param cluster: a cluster id of :func:`clusters`, or "all" (every significant voxel of ``sign``).
    :return: the circuit.json record (absolute paths), with "vlsm_cluster" (the cluster's description).
    """
    from . import circuits
    from .export import write_json_atomic

    record = _load_record(vlsm_result)
    folder = os.path.dirname(record["record"])
    roi, affine, info = cluster_roi(record, cluster, alpha, inference, sign)
    desc = (record.get("contrast") or {}).get("desc") or "vlsm"
    name = circuits.circuit_name(name or f"{desc[:1].upper()}{desc[1:]}C{info['id'] if info['id'] != 'all' else 'All' + sign.capitalize()}")
    out_dir = os.path.abspath(os.fspath(out_dir))
    os.makedirs(out_dir, exist_ok=True)
    roi_path = os.path.join(out_dir, "vlsm_cluster_mask.nii.gz")
    nifti.write_nifti(roi_path, roi, affine, descrip=f"VLSM cluster {info['id']} ({inference.upper()} p <= {alpha:g})")
    stat_path = _result_maps(record, inference)[0]
    lesions_tsv = os.path.join(folder, record.get("lesions_tsv") or LESIONS_TSV)
    parents = [run for run in [record.get("run_id"), *(parent_runs or ())] if run]
    study_path = (record.get("inputs") or {}).get("study")
    (emit or _quiet)("result", kind="vlsm_cluster", cluster={key: value for key, value in info.items() if key != "peak_ijk"}, roi=roi_path)
    circuit = circuits.seed_this(bundle, out_dir, name, roi=roi_path, subjects=subjects, subject_list=subject_list, parent=stat_path, parent_runs=parents,
                                 source_maps=lesions_tsv if os.path.isfile(lesions_tsv) else None, run_root=run_root or study_path, register=register, cancel=cancel, emit=emit,
                                 argv=argv)
    described = {"format": "slicer-lnm-vlsm-cluster", "version": 1, "vlsm": os.path.relpath(record["record"], out_dir).replace(os.sep, "/"), "vlsm_run": record.get("run_id"),
                 "contrast": desc, "cluster": {key: value for key, value in info.items() if key != "peak_ijk"}, "circuit": circuits.CIRCUIT_JSON}
    write_json_atomic(os.path.join(out_dir, CLUSTER_JSON), described)
    circuit["vlsm_cluster"] = described
    return circuit


#
# Command line
#


def _parser():
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.vlsm", description="Voxel-based lesion-symptom mapping with permutation inference, and circuits from its clusters.")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="VLSM of a study's lesions (binary masks, minimum overlap, lesion volume covariate)")
    jobs.add_common_arguments(run)
    run.add_argument("--study", required=True, help="study folder (participants.tsv with the lesion column)")
    run.add_argument("--design", default=None, help="design.json (default: two groups, case vs control, volume covariate)")
    run.add_argument("--out", default=None, help="output folder (default: <study>/derivatives/lnm/vlsm/<contrast>)")
    run.add_argument("--grid", default=None, help="template2mm (default), a registry grid id, a NIfTI grid or a connectome bundle")
    run.add_argument("--space", default=None, help="space of --grid when it is not a standard template grid")
    run.add_argument("--min-lesions", type=int, default=DEFAULT_MIN_LESIONS, help=f"test voxels lesioned in at least this many lesions (default {DEFAULT_MIN_LESIONS})")
    run.add_argument("--min-percent", type=float, default=DEFAULT_MIN_PERCENT, help=f"... and in at least this percent of the lesions (default {DEFAULT_MIN_PERCENT:g}; 0: off)")
    run.add_argument("--perm", type=jobs.positive_int, default=None, help="permutations, identity included (default: the design's n_perm)")
    run.add_argument("--seed", type=int, default=None, help="permutation seed (default: the design's)")
    run.add_argument("--tfce", action="store_true", help="add TFCE (H=2, E=0.5, 100 steps, 26-connectivity) on the tested voxels")
    run.add_argument("--batch", type=jobs.positive_int, default=None, help="permutations per GEMM (default: from the memory of one batch)")
    run.add_argument("--precision", choices=("auto", "float32", "float64"), default="auto", help="precision of the permutations (default auto)")
    run.add_argument("--template-dir", action="append", default=[], help="folder with downloaded templates (repeatable)")

    seed = commands.add_parser("seed", help="map a VLSM cluster as a circuit (circuits.seed_this)")
    jobs.add_common_arguments(seed)
    seed.add_argument("--vlsm", required=True, help="VLSM output folder (vlsm.json)")
    seed.add_argument("--bundle", required=True, help="connectome bundle (.lnmc)")
    seed.add_argument("--out", required=True, help="output folder of the circuit")
    seed.add_argument("--name", default=None, help="circuit name (letters and digits; default from the contrast and the cluster)")
    seed.add_argument("--cluster", default="1", help="cluster id (largest first), or all (every significant voxel of --sign)")
    seed.add_argument("--alpha", type=float, default=CLUSTER_ALPHA, help=f"significance of the cluster voxels (default {CLUSTER_ALPHA:g})")
    seed.add_argument("--inference", choices=INFERENCES, default="fwe", help="FWE (default, primary) or FDR p map")
    seed.add_argument("--sign", choices=CLUSTER_SIGNS, default="pos", help="sign of the statistic with --cluster all (default pos)")
    subjects = seed.add_mutually_exclusive_group()
    subjects.add_argument("--subjects", type=jobs.positive_int, default=None, help="use the first N connectome subjects")
    subjects.add_argument("--subject-list", default=None, help="a named subset, a file of subject ids or comma-separated ids")
    return parser


def main(argv=None):
    """Command-line entry point (see the module docstring); returns the exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(argv)
    cancel = jobs.CancelToken(args.cancel_file)
    try:
        if args.command == "run":
            if args.seed is not None and args.seed < 0:
                raise jobs.JobError(f"--seed must not be negative, got {args.seed}", code="usage", exit_code=jobs.EXIT_USAGE)
            if args.min_lesions < 0 or not 0 <= args.min_percent <= 100:
                raise jobs.JobError("--min-lesions must not be negative and --min-percent must be in [0, 100]", code="usage", exit_code=jobs.EXIT_USAGE)
            try:
                spec = designlib.DesignSpec.load(args.design) if args.design else designlib.DesignSpec()
            except (OSError, ValueError) as exc:
                raise jobs.JobError(f"The design {args.design} cannot be read: {exc}", code="usage", exit_code=jobs.EXIT_USAGE) from None
            dtype = None if args.precision == "auto" else np.dtype(args.precision)
            record = vlsm(args.study, spec, grid=args.grid, space=args.space, min_lesions=args.min_lesions, min_percent=args.min_percent, n_perm=args.perm, seed=args.seed,
                          out_dir=args.out, tfce=True if args.tfce else None, batch=args.batch, dtype=dtype, run_root=args.run_root, parent_runs=args.parent_run,
                          template_dirs=tuple(args.template_dir), cancel=cancel, emit=jobs.emit, argv=argv, design_path=args.design, threads=args.threads)
            return jobs.EXIT_OK if record.get("status") == "done" else jobs.EXIT_ERROR
        seed_cluster(args.vlsm, args.bundle, args.out, name=args.name, cluster=args.cluster, alpha=args.alpha, inference=args.inference, sign=args.sign,
                     subjects=args.subjects, subject_list=args.subject_list, run_root=args.run_root, parent_runs=args.parent_run, cancel=cancel, emit=jobs.emit, argv=argv)
        return jobs.EXIT_OK
    except (jobs.Cancelled, jobs.JobError):
        raise
    except designlib.DesignError as exc:
        raise jobs.JobError(f"The design cannot be built: {exc}", code="design_blocked", exit_code=jobs.EXIT_USAGE) from None
    except (OSError, ValueError, KeyError) as exc:
        raise jobs.JobError(f"{type(exc).__name__}: {exc}", code="input") from None


if __name__ == "__main__":
    jobs.run_job(main, job="vlsm")

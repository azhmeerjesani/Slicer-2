"""Fast (parcel) mode of lesion network mapping (addendum §3.12): a complete tiling, the parcel index and its parity gate.

The parcel index holds, for every parcel i of a complete tiling of the connectome mask, the exact group
mean Fz map of the parcel as a seed (row i of P, R x V) and the parcel-to-parcel matrix C. The map of
any lesion is then approximated without touching the connectome::

    Fz~ = (m @ P) / sum(m)        m = the lesion's occupancy of each parcel (sum of its weights there)

a weighted average of parcel-seed maps. It is labelled **approximate z-scale**: it is not on the exact
Fz scale (the seed of a lesion is the sum of its voxels' time courses, not the average of its parcels'
maps), so only rank rules (top-x%) apply to it, and fast-mode maps are never reported as final
observed maps. Thousands of nulls, permutations and previews cost one small matrix product each.

Tiling (:func:`build_tiling`), on the connectome grid, first come first served:

- Schaefer-400 7 networks (FSL 2 mm), Tian S2 (nearest neighbour), Buckner 2011 cerebellum 7 networks
  (1 mm, resampled to the grid by majority: the label, or background, covering most of each voxel) and
  the brain stem label of the Harvard-Oxford subcortical atlas (optionally the Harvard AAN nuclei first);
- then compact supervoxels over every mask voxel no atlas covers (white matter included): the uncovered
  voxels intersected with a regular cell grid (~10 mm), split into connected components (faces), and
  pieces under ``min_piece`` voxels merged into the touching supervoxel they share most neighbours with.

Every mask voxel therefore belongs to exactly one parcel, so a pure white-matter lesion has non-zero
parcel occupancy. labels.tsv lists id, name, source (atlas id or "supervoxel"), network, stratum
(cortical, subcortical, brainstem, cerebellar, white-matter-supervoxel), voxels and the atlas value.
Atlases come from the fetch cache only (never downloaded here; ``python -m LesionNetworkMappingLib.fetch``).
On a connectome of another template family the default atlases are replaced by their catalog versions in
that family (e.g. the -2009c Schaefer and Tian atlases); those without one are left out with a notice. A
tiling written on its own (:meth:`Tiling.write`) keeps its atlas records and statistics in tiling.json.

Index (:func:`build_index`): one engine pass with label-sum seeds (each parcel's seed is the sum of its
voxels' time courses) and the float32 mean accumulator (``engine.compute_network_maps(labels=...,
accumulate="mean")``), in checkpointed batches of subjects so that a stopped build resumes. Files::

    P_fz.f16.npy   (R, V) float16  row i: mean over subjects of atanh(r) between parcel i's seed and each voxel
    C_fz.npy       (R, R) float64  mean over subjects of atanh(r) between the parcel seeds
    labels.nii.gz  parcel id (1..R) of every mask voxel on the connectome grid (0 outside the mask)
    labels.tsv     the parcel table
    index.json     format, label ("approximate z-scale"), bundle identity, subjects (n, sha256, list name),
                   the atlases and their sha256, coverage statistics, the data use terms tag inherited from
                   the bundle, build time (seconds per subject) and versions; no absolute paths

Apply: :func:`parcel_occupancy` (L x R), :func:`approximate_maps` and :func:`coverage` (fraction of a
lesion's occupancy in atlas parcels vs supervoxels).

Parity (:func:`parity`, :func:`gate`): on >= 10 lesions mapped exactly, per lesion the spatial r, the
partial r with the reference maps mu_r and PC1 partialled out (``similarity.partial_spatial_correlation``;
the shared degree component cannot carry the gate), the top-10% rank Dice, the location stratum (from
the parcel at the lesion's centroid) and a size bin. The gate (:data:`FASTMODE_GATE`) requires the median
partial r and the median Dice of a group to reach the constants, per stratum and per size bin; lesions
in failing (or too small) groups are excluded from fast-mode statistics with a message. The constants
are to be calibrated on GSP1000 and reported, not promised; every value goes to parity.json.

Command line (JSON events, see ``jobs``; exit codes 0 done, 1 error, 2 usage, 3 cancelled, 75 locked)::

    python -m LesionNetworkMappingLib.fastmap build --bundle B [--out DIR] [--subjects N | --subject-list SPEC]
        [--tiling DIR | --atlas ID ... [--aan] [--cache DIR] [--cell-mm 10] [--min-piece 5]]
        [--checkpoint-every 100] [--threads K] [--cancel-file F] [--resume] [--run-root DIR] [--parent-run ID]
    python -m LesionNetworkMappingLib.fastmap parity --bundle B --index DIR --out DIR
        (--pair LESION.nii.gz EXACT_AvgRFz.nii.gz ... | --engine-run run.json ...) [--reference DIR] [--top-pct 10]
    python -m LesionNetworkMappingLib.fastmap apply --index DIR --lesion L.nii.gz [...] --out DIR [--parity parity.json]

``build`` registers the run (kind "fastindex") and ``parity`` its report (kind "parity") in
``<run root>/derivatives/lnm/runs.jsonl``; the run root is ``--run-root``, else the bundle folder
(``build``) or the output folder.
"""

import os
import sys

if __name__ == "__main__":
    from LesionNetworkMappingLib.jobs import set_blas_threads

    set_blas_threads(sys.argv[1:])  # before numpy is imported: BLAS reads these variables once

import csv
import datetime
import hashlib
import json
import re
import time
import types

import numpy as np

from . import fetch, jobs, runs
from .geometry import Grid, from_masked_vector, resample_to_grid, to_masked_vector

FORMAT_NAME = "slicer-lnm-fastindex"
FORMAT_VERSION = 1
PARITY_FORMAT = "slicer-lnm-fastparity"
APPLY_FORMAT = "slicer-lnm-fastmaps"
FASTMAP_VERSION = "1.0"
APPROX_LABEL = "approximate z-scale"
RULES_LABEL = "rank rules only (top-x%)"
NOT_FINAL_NOTE = "Fast-mode maps are approximations: never report them as final observed maps."
APPROX_DESC = "FastApproxFz"  # "desc" label of an approximate map written by apply

INDEX_JSON = "index.json"
P_NAME = "P_fz.f16.npy"
C_NAME = "C_fz.npy"
LABELS_NII = "labels.nii.gz"
LABELS_TSV = "labels.tsv"
TILING_JSON = "tiling.json"  # provenance of a tiling written on its own (atlases, coverage statistics, notices, parameters)
TILING_FORMAT = "slicer-lnm-tiling"
PARITY_JSON = "parity.json"
WORK_DIR = ".fastindex-build"
LOCK_NAME = ".fastindex.lock"
RESUME_NAME = "resume.json"
CHECKPOINT_FILES = ("sums-{}.npy", "c_sum-{}.npy")  # running sums after batch k, named by k (see build_index)

DEFAULT_CELL_MM = 10.0
DEFAULT_MIN_PIECE = 5
DEFAULT_CHECKPOINT = 100  # subjects per batch; the sums are saved after each batch but the last
SUPERVOXEL_SOURCE = "supervoxel"
NOT_AVAILABLE = "n/a"
STRATA = ("cortical", "subcortical", "brainstem", "cerebellar", "white-matter-supervoxel")
SUPERVOXEL_STRATUM = "white-matter-supervoxel"
TSV_COLUMNS = ("id", "name", "source", "network", "stratum", "voxels", "atlas_value")

# The default tiling atlases (catalog ids), in priority order: a voxel keeps the first label it gets
DEFAULT_TILING_ATLASES = (
    {"id": "schaefer2018-400-7n-2mm", "stratum": "cortical"},
    {"id": "tian-s2-2mm", "stratum": "subcortical", "resample": "nearest"},
    {"id": "buckner2011-cerebellum-7n-1mm", "stratum": "cerebellar", "resample": "majority"},
    {"id": "harvard-oxford-subcortical-th25-2mm", "stratum": "brainstem", "labels": ["Brain-Stem"], "network": "Brainstem"},
)
# Optional Harvard Ascending Arousal Network nuclei (CC0), placed before the Harvard-Oxford brain stem
AAN_ENTRY = {"id": "harvard-aan-v2", "stratum": "brainstem", "network": "AAN", "optional": True}

# Parity gate (addendum §3.12): medians per stratum and per size bin; to be calibrated on GSP1000 and reported
FASTMODE_GATE = {"partial_r": 0.80, "top10_dice": 0.60}
PARITY_MIN_LESIONS = 10  # exact lesions the gate needs at all
GATE_MIN_GROUP = 3  # lesions a stratum or size bin needs to be evaluated (fewer: not allowed, "insufficient")
PARITY_COVARIATES = ("mu_r", "pc1")
DEFAULT_TOP_PCT = 10.0
ON_DEMAND_ROWS = 16  # approximate_maps converts at most this many (or R / 8) touched rows of P per call, else keeps float32 P
SIZE_BINS_ML = (1.0, 10.0, 50.0)  # bin edges in ml: < 1, 1-10, 10-50, >= 50
SIZE_BIN_NAMES = ("lt1ml", "1to10ml", "10to50ml", "ge50ml")

GSP_PARCEL_MATRIX = "GSP1000_FCz_Schaefer1000Melbourne54.mat"


# Small helpers


def _now_iso():
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _file_sha256(path, block=1 << 20):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(block), b""):
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


def _open_bundle(bundle):
    from .connectome import ConnectomeBundle

    return ConnectomeBundle.open(os.fspath(bundle)) if isinstance(bundle, (str, os.PathLike)) else bundle


def _geometry(source):
    """(Grid, mask indices) of a bundle, an index, a tiling or an object with ``grid`` and ``mask_indices``."""
    return source.grid, np.asarray(source.mask_indices, dtype=np.int64)


def _norm_name(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _finite(value):
    value = float(value)
    return value if np.isfinite(value) else None


def _median(values):
    values = [v for v in values if v is not None and np.isfinite(v)]
    return float(np.median(values)) if values else None


# Tiling


class Tiling:
    """A complete tiling of the connectome mask: every mask voxel belongs to exactly one parcel.

    :ivar labels: (V,) int32 parcel index (0..R-1) of every mask voxel.
    :ivar table: list of R dicts (one per parcel, in index order): id (1..R, the value in labels.nii.gz),
      name, source (atlas id or "supervoxel"), network, stratum, voxels, atlas_value (None for supervoxels).
    :ivar grid: the connectome :class:`Grid`; ``mask_indices``: flat C-order indices of the V mask voxels.
    :ivar atlases: records of the atlases used (id, title, sha256, licence, stratum, resample, parcels, voxels).
    :ivar stats: coverage statistics (see :func:`build_tiling`); ``notices``: skipped atlases and the like.
    """

    def __init__(self, labels, table, grid, mask_indices, atlases=(), stats=None, notices=(), params=None):
        self.labels = np.asarray(labels, dtype=np.int32)
        self.table = [dict(row) for row in table]
        self.grid = grid
        self.mask_indices = np.asarray(mask_indices, dtype=np.int64)
        self.atlases = [dict(a) for a in atlases]
        self.stats = dict(stats or {})
        self.notices = list(notices)
        self.params = dict(params or {})
        if self.labels.shape != (self.mask_indices.size,):
            raise ValueError(f"The tiling has {self.labels.size} labels for {self.mask_indices.size} mask voxels")
        if self.labels.size and (self.labels.min() < 0 or self.labels.max() >= len(self.table)):
            raise ValueError("The tiling is not complete: every mask voxel needs a parcel of the table")

    @property
    def n_parcels(self):
        return len(self.table)

    @property
    def n_voxels(self):
        return int(self.labels.size)

    @property
    def sources(self):
        """(R,) array of the source of each parcel (atlas id or "supervoxel")."""
        return np.array([row["source"] for row in self.table], dtype=object)

    @property
    def strata(self):
        """(R,) array of the stratum of each parcel."""
        return np.array([row["stratum"] for row in self.table], dtype=object)

    @property
    def is_supervoxel(self):
        """(R,) boolean: the parcel is a supervoxel (not an atlas parcel)."""
        return np.array([row["source"] == SUPERVOXEL_SOURCE for row in self.table], dtype=bool)

    @property
    def shape(self):
        return self.grid.shape

    @property
    def affine(self):
        return self.grid.affine

    def sha256(self):
        """sha256 of the parcel of every voxel and of the table (identifies the tiling)."""
        digest = hashlib.sha256()
        digest.update(np.ascontiguousarray(self.labels, dtype="<i4").tobytes())
        digest.update(np.ascontiguousarray(self.mask_indices, dtype="<i8").tobytes())
        digest.update(json.dumps([[row.get(key) for key in TSV_COLUMNS] for row in self.table], sort_keys=True).encode("utf-8"))
        return digest.hexdigest()

    def volume(self):
        """Parcel ids (1..R) on the connectome grid, 0 outside the mask; int16 or int32 indexed [i, j, k]."""
        dtype = np.int16 if self.n_parcels < np.iinfo(np.int16).max else np.int32
        return from_masked_vector((self.labels + 1).astype(dtype), self.mask_indices, self.grid.shape, fill=0, dtype=dtype)

    def provenance(self):
        """The tiling's provenance (tiling.json): atlases with their sha256, coverage statistics, notices and
        parameters; base names only, no local path.
        """
        return {"format": TILING_FORMAT, "version": 1, "sha256": self.sha256(), "atlases": self.atlases, "stats": self.stats, "notices": self.notices, "params": self.params}

    def write(self, out_dir, provenance=True):
        """Write labels.nii.gz and labels.tsv (and, with ``provenance``, tiling.json) into ``out_dir``; return the
        paths of labels.nii.gz and labels.tsv. An index folder leaves tiling.json out: index.json holds the same.
        """
        from .nifti import write_nifti

        os.makedirs(out_dir, exist_ok=True)
        nii_path = os.path.join(out_dir, LABELS_NII)
        tsv_path = os.path.join(out_dir, LABELS_TSV)
        tmp = f"{nii_path}.{os.getpid()}.tmp.nii.gz"
        try:
            write_nifti(tmp, self.volume(), self.grid.affine, descrip=f"LNM fast index tiling, {self.n_parcels} parcels")
            os.replace(tmp, nii_path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        tmp = f"{tsv_path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f, delimiter="\t", lineterminator="\n")
                writer.writerow(TSV_COLUMNS)
                for row in self.table:
                    writer.writerow([NOT_AVAILABLE if row.get(key) in (None, "") else row.get(key) for key in TSV_COLUMNS])
            os.replace(tmp, tsv_path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        if provenance:
            jobs.write_json_atomic(os.path.join(out_dir, TILING_JSON), self.provenance())
        return [nii_path, tsv_path]

    @classmethod
    def load(cls, folder):
        """Read labels.nii.gz and labels.tsv of a folder (an index or a written tiling).

        The mask is the set of labelled voxels (the tiling is complete, so it is the connectome mask). The
        atlases, notices and parameters come from tiling.json (a written tiling) or index.json (an index) when
        they describe these labels (same tiling sha256); the coverage statistics are recomputed from the table
        when missing, so an index built from a loaded tiling records them all.
        """
        from .nifti import read_nifti

        data, affine, _header = read_nifti(os.path.join(folder, LABELS_NII))
        data = np.asarray(data).reshape(np.asarray(data).shape[:3])
        values = np.rint(data).astype(np.int64).reshape(-1)
        indices = np.flatnonzero(values > 0)
        table = read_labels_tsv(os.path.join(folder, LABELS_TSV))
        labels = values[indices] - 1
        if labels.size and labels.max() >= len(table):
            raise ValueError(f"{LABELS_NII} holds parcel ids that {LABELS_TSV} does not list")
        tiling = cls(labels, table, Grid(data.shape, affine), indices)
        saved = _saved_provenance(folder)
        if saved is not None and saved.get("sha256") not in (None, tiling.sha256()):
            tiling.notices.append({"code": "provenance_mismatch", "message": f"The atlas records next to {LABELS_TSV} describe another tiling; they are left out."})
            saved = None
        if saved is not None:
            tiling.atlases = [dict(a) for a in saved.get("atlases") or []]
            tiling.stats = dict(saved.get("stats") or {})
            tiling.notices = [dict(n) for n in saved.get("notices") or []] + tiling.notices
            tiling.params = dict(saved.get("params") or {})
        if not tiling.atlases and any(row["source"] != SUPERVOXEL_SOURCE for row in table):
            tiling.notices.append({"code": "atlases_unknown", "message": f"{os.path.basename(os.path.normpath(folder))} has no record of its atlases (no {TILING_JSON}); their sha256 cannot be reported."})
        tiling.stats = {**_coverage_stats(table, tiling.n_voxels, [a.get("id") for a in tiling.atlases]), **{k: v for k, v in tiling.stats.items() if v is not None}}
        return tiling


def _saved_provenance(folder):
    """The provenance of a tiling folder: tiling.json, else the "atlases" and "tiling" keys of index.json (None without either)."""
    tiling_path = os.path.join(folder, TILING_JSON)
    if os.path.isfile(tiling_path):
        with open(tiling_path, encoding="utf-8") as f:
            saved = json.load(f)
        if saved.get("format") != TILING_FORMAT:
            raise ValueError(f"{tiling_path} is not a tiling record ({TILING_FORMAT})")
        return saved
    index_path = os.path.join(folder, INDEX_JSON)
    if os.path.isfile(index_path):
        with open(index_path, encoding="utf-8") as f:
            record = json.load(f)
        tiling = dict(record.get("tiling") or {})
        notices, params, sha = tiling.pop("notices", []), tiling.pop("params", {}), tiling.pop("sha256", None)
        return {"sha256": sha, "atlases": record.get("atlases") or [], "stats": tiling, "notices": notices, "params": params}
    return None


def _coverage_stats(table, n_voxels, atlas_ids=()):
    """Coverage statistics of a parcel table: parcel counts, atlas and supervoxel voxel fractions and the fraction by atlas."""
    n_voxels = int(n_voxels)
    voxels = np.array([row.get("voxels") or 0 for row in table], dtype=np.int64)
    supervoxel = np.array([row.get("source") == SUPERVOXEL_SOURCE for row in table], dtype=bool)
    counts = {str(atlas_id): 0 for atlas_id in atlas_ids if atlas_id is not None}
    for row, count in zip(table, voxels, strict=True):
        if row.get("source") != SUPERVOXEL_SOURCE:
            counts[str(row.get("source"))] = counts.get(str(row.get("source")), 0) + int(count)
    by_source = {source: count / max(n_voxels, 1) for source, count in counts.items()}
    return {
        "n_voxels": n_voxels,
        "n_parcels": len(table),
        "n_atlas_parcels": int(np.count_nonzero(~supervoxel)),
        "n_supervoxels": int(np.count_nonzero(supervoxel)),
        "atlas_fraction": float(voxels[~supervoxel].sum() / max(n_voxels, 1)),
        "supervoxel_fraction": float(voxels[supervoxel].sum() / max(n_voxels, 1)),
        "by_source": by_source,
    }


def read_labels_tsv(path):
    """Rows of a labels.tsv as dicts (id and voxels as int, atlas_value as int or None, "n/a" as None)."""
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for record in reader:
            row = {key: (None if record.get(key) in (None, "", NOT_AVAILABLE) else record.get(key)) for key in TSV_COLUMNS}
            row["id"] = int(row["id"])
            row["voxels"] = int(row["voxels"]) if row["voxels"] is not None else None
            row["atlas_value"] = int(row["atlas_value"]) if row["atlas_value"] is not None else None
            rows.append(row)
    for position, row in enumerate(rows):
        if row["id"] != position + 1:
            raise ValueError(f"{os.path.basename(path)}: parcel ids must be 1..R in order (row {position + 1} has id {row['id']})")
    return rows


def _resolve_entries(atlas_entries, aan):
    entries = [dict(e) if isinstance(e, dict) else {"id": e} for e in (DEFAULT_TILING_ATLASES if atlas_entries is None else atlas_entries)]
    if aan and not any(e.get("id") == AAN_ENTRY["id"] for e in entries):
        position = next((i for i, e in enumerate(entries) if e.get("stratum") == "brainstem"), len(entries))
        entries.insert(position, dict(AAN_ENTRY))
    return entries


def _default_entries_for_family(entries, family, allow_cross_family):
    """(entries, notices): the default atlases for a connectome of ``family``.

    Each default atlas of another family is replaced by its catalog version in this family (an id with a suffix,
    e.g. "tian-s2-2mm-2009c"). One without such a version is left out with a notice (unless ``allow_cross_family``),
    so that it is neither required in the cache nor loaded only to be dropped.
    """
    if family in ("unknown", "native"):
        return entries, []
    catalog = fetch.load_catalog("atlases")
    out, notices = [], []
    for entry in entries:
        atlas_id = entry.get("id")
        resource = catalog.get(atlas_id) if atlas_id in catalog else None
        atlas_family = resource.get("family") if resource is not None else None
        if resource is None or atlas_family in (None, "unknown", "native", family) or allow_cross_family:
            out.append(entry)
            continue
        variant = next((r for r in catalog if r.id.startswith(f"{atlas_id}-") and r.get("family") == family), None)
        if variant is not None:
            out.append({**entry, "id": variant.id})
            notices.append({"code": "family_variant", "atlas": variant.id, "message": f"{variant.title} is used instead of {resource.title} (the connectome is in the {family} family)."})
            continue
        notices.append({"code": "family_mismatch", "atlas": atlas_id, "message": f"{resource.title} is in the {atlas_family} family and the connectome in the {family} family, and the catalog has no {family} version: it is left out, and supervoxels cover its voxels."})
    return out, notices


def _load_entry_atlas(entry, cache):
    """(Atlas, sha256 record) of a tiling entry: an Atlas object ({"atlas": Atlas}) or a catalog id read from the cache."""
    from .atlases import Atlas

    atlas = entry.get("atlas")
    if isinstance(atlas, Atlas):
        digest = hashlib.sha256(np.ascontiguousarray(atlas.data, dtype="<i4").tobytes() + np.ascontiguousarray(atlas.affine, dtype="<f8").tobytes()).hexdigest()
        return atlas, {"sha256": digest, "sha256_of": "content", "file": None}
    resource = fetch.find_resource(entry["id"], ("atlases",))
    atlas = Atlas.load(resource, cache)
    path = fetch.cached_path(resource, cache)
    return atlas, {"sha256": _file_sha256(path) if path else None, "sha256_of": "file", "file": os.path.basename(path) if path else None}


def _majority_on_grid(data, affine, grid, indices, values):
    """Label of each mask voxel by majority: the label (or background) covering most of the voxel (fraction resampling)."""
    best = np.zeros(indices.size, dtype=np.float64)
    out = np.zeros(indices.size, dtype=np.int64)
    covered = np.zeros(indices.size, dtype=np.float64)
    for value in values:
        fraction = to_masked_vector(resample_to_grid((data == value).astype(np.float64), affine, grid, mode="fraction"), indices)
        covered += fraction
        better = fraction > best
        best[better] = fraction[better]
        out[better] = value
    out[best <= 1.0 - covered] = 0  # background covers more of the voxel than the leading label
    return out


def _atlas_on_grid(atlas, grid, indices, method, values):
    """(V,) int64 atlas label value of every mask voxel (0 where none of ``values``)."""
    data = atlas.data
    if method == "majority":
        return _majority_on_grid(data, atlas.affine, grid, indices, values)
    if atlas.grid.matches(grid):
        on_grid = data.reshape(-1)[indices].astype(np.int64)
    else:
        on_grid = np.rint(to_masked_vector(resample_to_grid(data, atlas.affine, grid, mode="nearest"), indices)).astype(np.int64)
    on_grid[~np.isin(on_grid, values)] = 0
    return on_grid


def _selected_values(atlas, wanted):
    """Label values of an atlas selected by names or values (all non-zero values when ``wanted`` is None)."""
    present = np.unique(atlas.data[atlas.data != 0])
    if wanted is None:
        return present
    names = {_norm_name(name): value for value, name in atlas.labels.items()}
    selected = []
    for item in wanted:
        if isinstance(item, (int, np.integer)):
            selected.append(int(item))
        elif _norm_name(item) in names:
            selected.append(names[_norm_name(item)])
    return np.intersect1d(present, np.array(selected, dtype=np.int64))


def _supervoxels(free, grid, indices, cell_mm, min_piece):
    """Supervoxels of the uncovered mask voxels: (V,) int64 piece number (1..n, 0 where covered) and statistics.

    The uncovered voxels are cut by a regular cell grid (cells of round(cell_mm / spacing) voxels per
    axis) and split into face-connected components; pieces under ``min_piece`` voxels are merged into
    the touching supervoxel they share most of their 26 neighbours with (isolated small pieces stay).
    """
    from scipy import ndimage

    shape = grid.shape
    cells = np.maximum(1, np.rint(cell_mm / grid.spacing)).astype(np.int64)
    volume = np.zeros(int(np.prod(shape)), dtype=bool)
    volume[indices[free]] = True
    volume = volume.reshape(shape)
    # Insert an empty plane after every cell along each axis, so that no component crosses a cell boundary
    expanded_shape = tuple(int(n + (n - 1) // c) for n, c in zip(shape, cells, strict=True))
    position = [np.arange(n) + np.arange(n) // c for n, c in zip(shape, cells, strict=True)]
    expanded = np.zeros(expanded_shape, dtype=bool)
    expanded[np.ix_(*position)] = volume
    pieces_expanded, n_pieces = ndimage.label(expanded)
    pieces = pieces_expanded[np.ix_(*position)].astype(np.int64)  # (ni, nj, nk) piece per voxel, 0 elsewhere
    del expanded, pieces_expanded
    flat = pieces.reshape(-1)
    sizes = np.bincount(flat, minlength=n_pieces + 1)
    n_initial = int(n_pieces)
    merged = isolated = 0
    small = [p for p in np.argsort(sizes[1:], kind="stable") + 1 if 0 < sizes[p] < min_piece]
    if small:
        members = {}
        positions = np.flatnonzero(flat)
        order = np.argsort(flat[positions], kind="stable")
        positions = positions[order]
        starts = np.searchsorted(flat[positions], np.arange(n_pieces + 2))
        for p in range(1, n_pieces + 1):
            members[p] = positions[starts[p]:starts[p + 1]]
        offsets = np.array([(a, b, c) for a in (-1, 0, 1) for b in (-1, 0, 1) for c in (-1, 0, 1) if (a, b, c) != (0, 0, 0)], dtype=np.int64)
        shape_array = np.array(shape, dtype=np.int64)
        for p in small:
            if not 0 < sizes[p] < min_piece:
                continue  # merged away already, or grown by a merge
            ijk = np.stack(np.unravel_index(members[p], shape), axis=1)
            around = (ijk[:, np.newaxis, :] + offsets[np.newaxis]).reshape(-1, 3)
            around = around[np.all((around >= 0) & (around < shape_array), axis=1)]
            neighbours = flat[np.ravel_multi_index(tuple(around.T), shape)]
            neighbours = neighbours[(neighbours != 0) & (neighbours != p)]
            if neighbours.size == 0:
                isolated += 1
                continue
            candidates, contacts = np.unique(neighbours, return_counts=True)
            # Most contacts, then the larger piece, then the lower number (deterministic)
            target = int(candidates[np.lexsort((candidates, -sizes[candidates], -contacts))[0]])
            flat[members[p]] = target
            members[target] = np.concatenate((members[target], members[p]))
            sizes[target] += sizes[p]
            sizes[p] = 0
            members[p] = members[p][:0]
            merged += 1
    # Number the remaining pieces 1..n in the order of their first voxel (C order)
    on_mask = flat[indices]
    present = np.flatnonzero(sizes[1:]) + 1
    first = np.full(n_pieces + 1, np.iinfo(np.int64).max, dtype=np.int64)
    np.minimum.at(first, on_mask[on_mask > 0], np.flatnonzero(on_mask > 0))
    renumber = np.zeros(n_pieces + 1, dtype=np.int64)
    renumber[present[np.argsort(first[present], kind="stable")]] = np.arange(1, present.size + 1)
    final = renumber[on_mask]
    final_sizes = np.bincount(final, minlength=present.size + 1)[1:]
    stats = {
        "cell_voxels": [int(c) for c in cells],
        "pieces_initial": n_initial,
        "merged_small": merged,
        "isolated_small": isolated,
        "n": int(present.size),
        "voxels_min": int(final_sizes.min()) if final_sizes.size else 0,
        "voxels_median": float(np.median(final_sizes)) if final_sizes.size else 0.0,
        "voxels_max": int(final_sizes.max()) if final_sizes.size else 0,
    }
    return final, stats


def build_tiling(bundle, atlas_entries=None, cache=None, cell_mm=DEFAULT_CELL_MM, min_piece=DEFAULT_MIN_PIECE, aan=False, allow_cross_family=False):
    """Build the complete tiling of a connectome mask (see the module docstring); return a :class:`Tiling`.

    :param bundle: a ConnectomeBundle (or its path), or any object with ``grid`` and ``mask_indices``.
    :param atlas_entries: tiling atlases in priority order (default :data:`DEFAULT_TILING_ATLASES`). Each is a
      catalog id or a dict: "id" (catalog id) or "atlas" (an :class:`atlases.Atlas`), "stratum" (one of
      :data:`STRATA`), optional "labels" (names or values to keep; default all), "resample" ("nearest" or
      "majority"; default majority for atlases with voxels under half the grid's), "network" (a network name
      for every label instead of the atlas's naming) and "optional" (skip when not cached).
    :param cache: fetch cache folder; atlases are read from it only (never downloaded).
    :param aan: also use the Harvard AAN nuclei (:data:`AAN_ENTRY`, optional) before the brain stem label.
    :param allow_cross_family: use atlases of another template family than the grid (recorded in notices).
    :raise fetch.NotCachedError: a required atlas is not cached (the message names every missing one).
    """
    from .atlases import mask_family

    bundle = _open_bundle(bundle)
    grid, indices = _geometry(bundle)
    n_voxels = indices.size
    family, _space = mask_family(grid.shape, grid.affine)
    assigned = np.full(n_voxels, -1, dtype=np.int64)
    table, records, notices, missing, unknown = [], [], [], [], []
    loaded = []
    entries = _resolve_entries(atlas_entries, aan)
    if atlas_entries is None:
        entries, family_notices = _default_entries_for_family(entries, family, allow_cross_family)
        notices.extend(family_notices)
    for entry in entries:
        stratum = entry.get("stratum")
        if stratum not in STRATA or stratum == SUPERVOXEL_STRATUM:
            raise ValueError(f"Tiling atlas {entry.get('id') or entry.get('atlas')!r}: stratum must be one of {', '.join(STRATA[:-1])}")
        try:
            atlas, digest = _load_entry_atlas(entry, cache)
        except fetch.UnknownResourceError:
            unknown.append(str(entry.get("id")))
            continue
        except fetch.FetchError as exc:
            if entry.get("optional"):
                notices.append({"code": "optional_missing", "atlas": entry.get("id"), "message": f"{exc} The optional atlas is left out."})
                continue
            missing.append(entry.get("id"))
            continue
        loaded.append((entry, atlas, digest))
    if unknown:
        raise ValueError(f"Unknown tiling atlas id(s): {', '.join(unknown)}; they are not in the atlas catalog (python -m LesionNetworkMappingLib.fetch list --catalog atlases).")
    if missing:
        raise fetch.NotCachedError(f"The fast index needs these atlases, which are not cached: {', '.join(missing)}. Download them first (python -m LesionNetworkMappingLib.fetch get --catalog atlases --id <id>).", resource_id=missing[0])
    for entry, atlas, digest in loaded:
        stratum = entry["stratum"]
        known = family not in ("unknown", "native") and atlas.family not in ("unknown", "native")
        if known and family != atlas.family:
            if not allow_cross_family:
                notices.append({"code": "family_mismatch", "atlas": atlas.id, "message": f"{atlas.title} is in the {atlas.family} family and the connectome in the {family} family: it is left out, and supervoxels cover its voxels."})
                continue
            notices.append({"code": "cross_family_allowed", "atlas": atlas.id, "message": f"{atlas.title} ({atlas.family} family) is used on a {family} connectome on request."})
        values = _selected_values(atlas, entry.get("labels"))
        if values.size == 0:
            notices.append({"code": "no_labels", "atlas": atlas.id, "message": f"{atlas.title} has none of the requested labels {entry.get('labels')}; it is left out."})
            continue
        method = entry.get("resample") or ("majority" if atlas.voxel_volume < 0.5 * grid.voxel_volume else "nearest")
        if method not in ("nearest", "majority"):
            raise ValueError(f"Unknown resampling {method!r} for {atlas.id}: use nearest or majority")
        on_grid = _atlas_on_grid(atlas, grid, indices, method, values)
        claim = (assigned < 0) & (on_grid != 0)
        present = np.unique(on_grid[claim])
        first = len(table)
        lookup = np.full(int(present.max()) + 1 if present.size else 1, -1, dtype=np.int64)
        lookup[present] = first + np.arange(present.size)
        assigned[claim] = lookup[on_grid[claim]]
        counts = np.bincount(assigned[claim] - first, minlength=present.size)
        for offset, value in enumerate(present):
            network = entry.get("network") or atlas.network(int(value))
            table.append({"name": atlas.label_name(int(value)), "source": atlas.id, "network": network, "stratum": stratum, "voxels": int(counts[offset]), "atlas_value": int(value)})
        records.append({
            "id": atlas.id,
            "title": atlas.title,
            "licence": atlas.licence,
            "family": atlas.family,
            "stratum": stratum,
            "resample": method,
            "labels": None if entry.get("labels") is None else [str(item) for item in entry["labels"]],
            "parcels": int(present.size),
            "voxels": int(claim.sum()),
            **digest,
        })
    if atlas_entries is None and not records and any(notice["code"] == "family_mismatch" for notice in notices):
        raise ValueError(f"None of the default tiling atlases can be used on this connectome ({family} family); give atlases of its family (--atlas ID:STRATUM) or allow other families (--allow-cross-family).")
    free = assigned < 0
    pieces, sv_stats = _supervoxels(free, grid, indices, float(cell_mm), int(min_piece))
    if sv_stats["isolated_small"]:
        notices.append({"code": "isolated_supervoxels", "message": f"{sv_stats['isolated_small']} pocket(s) of uncovered voxels smaller than {int(min_piece)} voxels touch no other supervoxel and stay parcels of their own; their rows of P are noisy."})
    n_atlas = len(table)
    assigned[free] = n_atlas + pieces[free] - 1
    counts = np.bincount(pieces[free], minlength=sv_stats["n"] + 1)[1:]
    for number in range(sv_stats["n"]):
        table.append({"name": f"supervoxel-{number + 1:04d}", "source": SUPERVOXEL_SOURCE, "network": None, "stratum": SUPERVOXEL_STRATUM, "voxels": int(counts[number]), "atlas_value": None})
    for position, row in enumerate(table):
        row["id"] = position + 1
    stats = {**_coverage_stats(table, n_voxels, [record["id"] for record in records]), "supervoxels": sv_stats}
    params = {"cell_mm": float(cell_mm), "min_piece": int(min_piece), "aan": bool(aan), "allow_cross_family": bool(allow_cross_family)}
    return Tiling(assigned.astype(np.int32), table, grid, indices, records, stats, notices, params)


# Index


class FastIndex:
    """A parcel index read by :func:`load_index` (or returned by :func:`build_index`).

    ``P`` (R, V) float16, ``C`` (R, R) float64, ``tiling`` (:class:`Tiling`), ``metadata`` (index.json),
    ``path`` (folder), ``sha256`` (of index.json). ``grid`` and ``mask_indices`` are the connectome's.
    """

    def __init__(self, P, C, tiling, metadata, path=None, sha256=None):  # noqa: N803 (the matrix names of the addendum)
        self.P = P
        self.C = C
        self.tiling = tiling
        self.metadata = metadata
        self.path = path
        self.sha256 = sha256
        self._p32 = None

    @property
    def grid(self):
        return self.tiling.grid

    @property
    def mask_indices(self):
        return self.tiling.mask_indices

    @property
    def n_parcels(self):
        return self.tiling.n_parcels

    @property
    def n_voxels(self):
        return self.tiling.n_voxels

    @property
    def terms(self):
        return self.metadata.get("terms")

    def rows32(self, rows=None):
        """float32 rows of P: all (computed once and kept, 4 * R * V bytes: 0.5 GB at R = 549, V = 228k) or the given parcel indices.

        :func:`approximate_maps` keeps the full copy as soon as its lesions touch more than
        max(:data:`ON_DEMAND_ROWS`, R / 8) parcels (a null set of 50 lesions: 84 ms instead of 336 ms per set).
        """
        if rows is None:
            if self._p32 is None:
                self._p32 = np.asarray(self.P, dtype=np.float32)
            return self._p32
        if self._p32 is not None:
            return self._p32[rows]
        return np.asarray(self.P[rows], dtype=np.float32)

    def approximate_maps(self, occ):
        return approximate_maps(occ, self)

    def coverage(self, occ):
        return coverage(occ, self)


def default_out_dir(bundle, subject_ids=None):
    """<bundle>/derived/fastindex for all subjects, <bundle>/derived/fastindex/subj-<sha12> for another subject list."""
    from .connectome import subjects_sha256

    base = os.path.join(bundle.path, "derived", "fastindex")
    if subject_ids is None or list(subject_ids) == list(bundle.subject_ids):
        return base
    return os.path.join(base, f"subj-{subjects_sha256(subject_ids)[:12]}")


def _subject_selection(bundle, subjects, subject_list):
    if subject_list is not None:
        if subjects is not None:
            raise ValueError("Give subjects or subject_list, not both")
        selection = bundle.resolve_subject_list(subject_list)
        return selection.ids, {"list_name": selection.name, "list_source": selection.source}
    from .engine import resolve_subjects

    return resolve_subjects(bundle, subjects), {"list_name": None, "list_source": "all" if subjects is None else "first_n" if isinstance(subjects, (int, np.integer)) else "ids"}


def _bundle_identity(bundle):
    from .export import data_sha256, file_sha256

    manifest = os.path.join(bundle.path, "connectome.json")
    return {
        "name": bundle.name,
        "manifest_sha256": file_sha256(manifest) if os.path.isfile(manifest) else None,
        "data_sha256": data_sha256(bundle.metadata),
        "mask_sha256": bundle.mask_sha256,
        "n_voxels": int(bundle.n_voxels),
        "shape": [int(n) for n in bundle.shape],
        "affine": np.asarray(bundle.affine, dtype=np.float64).round(6).tolist(),
    }


def _terms(bundle):
    """(terms tag, terms sha256, acknowledgement) inherited from the bundle's provenance."""
    from .export import GSP_ACKNOWLEDGEMENT, GSP_LICENCE

    provenance = bundle.provenance
    licence = provenance.get("licence")
    licence = licence if licence and licence != "unknown" else None
    acknowledgement = provenance.get("acknowledgement") or (GSP_ACKNOWLEDGEMENT if licence == GSP_LICENCE else None)
    return licence, provenance.get("termsSha256"), acknowledgement


def _write_p(sums_vr, n, out_path):
    """Write P = sums / n as an (R, V) float16 .npy, transposing in voxel blocks (no float32 copy of all rows)."""
    n_voxels, n_parcels = sums_vr.shape
    p = np.empty((n_parcels, n_voxels), dtype=np.float16)
    scale = np.float32(1.0 / max(n, 1))
    block = 32768
    for v0 in range(0, n_voxels, block):
        p[:, v0:v0 + block] = (sums_vr[v0:v0 + block] * scale).T
    _save_npy(out_path, p)
    return p


def _load_checkpoint(work, state, batches, n_voxels, n_parcels):
    """(sums, c_sum, n_done) of the checkpoint recorded in resume.json; ValueError when it is not consistent with it.

    The batches done must be batch-0..batch-k, the checkpoint must be k and n_done the subjects of those batches.
    """
    checkpoint = state.data.get("checkpoint")
    expected_units = [f"batch-{k}" for k in range(len(state.done))]
    if not isinstance(checkpoint, int) or state.done != expected_units or checkpoint != len(state.done) - 1 or checkpoint >= len(batches) - 1:
        raise ValueError("the record of the batches done is not consistent")
    n_done = int(state.data.get("n_done", -1))
    if n_done != sum(len(batch) for batch in batches[:checkpoint + 1]):
        raise ValueError(f"{n_done} subjects recorded for {checkpoint + 1} batch(es)")
    sums_name, c_name = (name.format(checkpoint) for name in CHECKPOINT_FILES)
    sums = np.load(os.path.join(work, sums_name), allow_pickle=False)
    c_sum = np.load(os.path.join(work, c_name), allow_pickle=False)
    if sums.shape != (n_voxels, n_parcels) or c_sum.shape != (n_parcels, n_parcels):
        raise ValueError("the saved sums do not match the tiling")
    return sums, c_sum, n_done


def build_index(
    bundle,
    tiling=None,
    subjects=None,
    subject_list=None,
    out_dir=None,
    atlas_entries=None,
    cache=None,
    cell_mm=DEFAULT_CELL_MM,
    min_piece=DEFAULT_MIN_PIECE,
    aan=False,
    allow_cross_family=False,
    checkpoint_every=DEFAULT_CHECKPOINT,
    chunk=None,
    resume=False,
    cancel=None,
    emit=None,
    run_root=None,
    parent_runs=(),
    register=True,
    argv=None,
    threads=None,
):
    """Build the parcel index of a bundle and subject list (see the module docstring); return a :class:`FastIndex`.

    :param tiling: a :class:`Tiling` of this bundle's mask (default: :func:`build_tiling` with
      ``atlas_entries``, ``cache``, ``cell_mm``, ``min_piece``, ``aan`` and ``allow_cross_family``).
    :param subjects: None (all), an int (the first n) or a list of ids; or give ``subject_list`` (a subset
      name, a file of ids or comma-separated ids).
    :param checkpoint_every: subjects per batch. After each batch but the last, the running sums are saved
      (sums-<k>.npy and c_sum-<k>.npy, then resume.json names batch k) so that ``resume`` continues from there.
      Memory: about 2 * 4 * V * R bytes for the running and batch sums plus P in float16 (2 * R * V): about
      3.2 GB at V = 228k, R = 1400 with several batches (one batch: about 1.3 GB less).
    :param cancel: callable (e.g. jobs.CancelToken) polled between subjects; raises jobs.Cancelled.
    :param emit: event function (default jobs.emit).
    :param run_root: folder whose derivatives/lnm/runs.jsonl records the run (default: the bundle folder,
      or ``out_dir`` when the bundle is read-only).
    """
    from . import engine
    from .connectome import _is_cancelled, subjects_sha256
    from .reference import subjects_data_sha256

    emit = emit or jobs.emit
    bundle = _open_bundle(bundle)
    if bundle.kind != "timeseries":
        raise jobs.JobError(f"The fast index needs a time-series connectome; {bundle.name!r} is of kind {bundle.kind!r}", code="input")
    chunk = int(chunk or engine.DEFAULT_CHUNK)
    checkpoint_every = max(1, int(checkpoint_every))
    ids, list_info = _subject_selection(bundle, subjects, subject_list)
    if tiling is None:
        tiling = build_tiling(bundle, atlas_entries, cache, cell_mm, min_piece, aan, allow_cross_family)
    if tiling.n_voxels != bundle.n_voxels or not np.array_equal(tiling.mask_indices, bundle.mask_indices) or not tiling.grid.matches(bundle.grid):
        raise ValueError("The tiling is not on this connectome's grid and mask")
    if out_dir is None and not os.access(bundle.path, os.W_OK):
        raise jobs.JobError(f"The connectome folder is read-only ({bundle.path}): give an output folder for the fast index", code="read_only")
    out_dir = os.path.abspath(os.fspath(out_dir or default_out_dir(bundle, ids)))
    os.makedirs(out_dir, exist_ok=True)
    work = os.path.join(out_dir, WORK_DIR)
    n_parcels = tiling.n_parcels
    tiling_sha = tiling.sha256()
    subjects_sha = subjects_sha256(ids)
    identity = _bundle_identity(bundle)
    batches = [ids[i:i + checkpoint_every] for i in range(0, len(ids), checkpoint_every)]
    params = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "data_sha256": identity["data_sha256"],
        "mask_sha256": identity["mask_sha256"],
        "subjects_sha256": subjects_sha,
        "subjects_data_sha256": subjects_data_sha256(bundle, ids),
        "tiling_sha256": tiling_sha,
        "checkpoint_every": checkpoint_every,
    }
    lock = jobs.OutputLock(os.path.join(out_dir, LOCK_NAME))
    lock.acquire()
    run_id = None
    run_root = os.path.abspath(os.fspath(run_root)) if run_root else (bundle.path if os.access(bundle.path, os.W_OK) else out_dir)
    try:
        if lock.reclaimed is not None:
            emit("warning", code="stale_lock", message=f"A stale lock of process {lock.reclaimed.get('pid')} on the fast index folder was reclaimed")
        os.makedirs(work, exist_ok=True)
        state = jobs.ResumeState(os.path.join(work, RESUME_NAME))
        if not resume:
            state.clear()
        elif state.done and state.data.get("params") != params:
            emit("warning", code="resume_mismatch", message="The saved progress belongs to other inputs or parameters; the build starts again.")
            state.clear()
        sums = c_sum = None
        n_done = 0
        if state.done:
            try:
                sums, c_sum, n_done = _load_checkpoint(work, state, batches, bundle.n_voxels, n_parcels)
            except (OSError, ValueError, EOFError) as exc:
                emit("warning", code="resume_unreadable", message=f"The saved progress cannot be used ({exc}); the build starts again.")
                state.clear()
                sums = c_sum = None
                n_done = 0
        # Keep only resume.json and the checkpoint it names (a stop between two saves can leave a newer, unrecorded one)
        keep = {RESUME_NAME, *(name.format(state.data["checkpoint"]) for name in CHECKPOINT_FILES)} if state.done else {RESUME_NAME}
        for name in os.listdir(work):
            if name not in keep:
                os.remove(os.path.join(work, name))
        if not state.done:
            state.data = {"params": params, "elapsed": 0.0, "subject_seconds": []}
        resumed = list(state.done)
        emit("start", job="fastindex", argv=list(argv or []), threads=threads, bundle=bundle.name, out=out_dir, n_subjects=len(ids), n_voxels=int(bundle.n_voxels), parcels=n_parcels,
             atlas_parcels=tiling.stats.get("n_atlas_parcels"), supervoxels=tiling.stats.get("n_supervoxels"), batches=len(batches), resumed=resumed)
        for notice in tiling.notices:
            emit("warning", code=notice.get("code"), message=notice.get("message"))
        if register:
            run_id = runs.register(run_root, "fastindex", out_dir, parents=list(parent_runs or ()), params={**params, "n_subjects": len(ids), "bundle": bundle.name, "parcels": n_parcels})
        started = time.perf_counter()
        elapsed_before = float(state.data.get("elapsed", 0.0))
        subject_seconds = [float(s) for s in state.data.get("subject_seconds", [])]
        n_start = n_done
        for number, batch in enumerate(batches):
            unit = f"batch-{number}"
            if state.is_done(unit):
                continue
            last = {"t": 0.0}

            def on_progress(done, total, elapsed, offset=n_done, last=last):
                subject_seconds.append(round(elapsed - last["t"], 4))
                last["t"] = elapsed
                spent = time.perf_counter() - started
                finished = offset + done
                eta = spent / max(finished - n_start, 1) * (len(ids) - finished)
                emit("progress", done=finished, total=len(ids), elapsed=round(elapsed_before + spent, 3), eta=round(eta, 3))

            maps = engine.compute_network_maps(bundle, None, subjects=batch, chunk=chunk, progress=on_progress, cancel=cancel, labels=tiling.labels, n_labels=n_parcels, accumulate="mean", keep_r=False)
            if maps.cancelled:
                if _is_cancelled(cancel):
                    raise jobs.Cancelled(done=n_done + maps.n, reason=getattr(cancel, "reason", None) or "stopped")
                raise jobs.JobError("The connectome was closed while the fast index was being built", code="input")
            batch_sums = maps.avg_fz.T  # (V, R) float32: the accumulator's own array, scaled to the batch mean
            batch_sums *= np.float32(maps.n)
            batch_c = maps.c_fz * maps.n
            del maps
            if sums is None:
                sums, c_sum = batch_sums, batch_c
            else:
                sums += batch_sums
                c_sum += batch_c
            del batch_sums
            n_done += len(batch)
            if number < len(batches) - 1:  # the last batch goes straight to the outputs
                # New files named by the batch, then resume.json names them in one atomic save; the previous
                # checkpoint is removed only after that, so a stop at any point leaves a consistent pair
                previous = state.data.get("checkpoint")
                for name, array in zip(CHECKPOINT_FILES, (sums, c_sum), strict=True):
                    _save_npy(os.path.join(work, name.format(number)), array)
                state.data.update({"checkpoint": number, "n_done": n_done, "elapsed": elapsed_before + time.perf_counter() - started, "subject_seconds": subject_seconds})
                state.mark_done(unit)
                if previous is not None and previous != number:
                    for name in CHECKPOINT_FILES:
                        path = os.path.join(work, name.format(previous))
                        if os.path.exists(path):
                            os.remove(path)
        elapsed = round(elapsed_before + time.perf_counter() - started, 3)

        # Outputs: index.json is removed first and written last, so that it always describes the files next to it
        json_path = os.path.join(out_dir, INDEX_JSON)
        if os.path.exists(json_path):
            os.remove(json_path)
        p_path = os.path.join(out_dir, P_NAME)
        c_path = os.path.join(out_dir, C_NAME)
        p = _write_p(sums, n_done, p_path)
        del sums
        c = c_sum / max(n_done, 1)
        _save_npy(c_path, c)
        nii_path, tsv_path = tiling.write(out_dir, provenance=False)
        licence, terms_sha, acknowledgement = _terms(bundle)
        from .export import LIBRARY_VERSION

        record = {
            "format": FORMAT_NAME,
            "version": FORMAT_VERSION,
            "fastmap_version": FASTMAP_VERSION,
            "library_version": LIBRARY_VERSION,
            "label": APPROX_LABEL,
            "rules": RULES_LABEL,
            "note": NOT_FINAL_NOTE,
            "definition": "Approximate map of a lesion: Fz~ = (m @ P) / sum(m), with m the lesion's occupancy of each parcel; "
            "a weighted average of parcel-seed mean Fz maps, not on the exact Fz scale.",
            "bundle": identity,
            "subjects": {"n": n_done, "sha256": subjects_sha, "data_sha256": params["subjects_data_sha256"], **list_info},
            "terms": licence,
            "terms_sha256": terms_sha,
            "acknowledgement": acknowledgement,
            "seeds": "label-sum: each parcel's seed is the sum of its voxels' time courses (unit weights)",
            "accumulate": "mean",
            "rmax": engine.RMAX,
            "atlases": tiling.atlases,
            "tiling": {"sha256": tiling_sha, "params": tiling.params, "notices": tiling.notices, **tiling.stats},
            "coverage": {"atlas_fraction": tiling.stats.get("atlas_fraction"), "supervoxel_fraction": tiling.stats.get("supervoxel_fraction"), "by_source": tiling.stats.get("by_source")},
            "reference_note": "The fast index defines no reference maps of its own; parity uses the bundle's lnm-reference-v1 maps.",
            "files": {
                "P": {"file": P_NAME, "shape": [n_parcels, int(bundle.n_voxels)], "dtype": "float16", "sha256": _file_sha256(p_path),
                      "definition": "row i: mean over subjects of atanh(r) between parcel i's seed and each connectome voxel; r is the mean over runs of the Pearson r, clipped to +/-RMAX"},
                "C": {"file": C_NAME, "shape": [n_parcels, n_parcels], "dtype": "float64", "sha256": _file_sha256(c_path),
                      "definition": "mean over subjects of atanh(r) between the parcel seed time courses; r is the mean over runs of the Pearson r, clipped to +/-RMAX"},
                "labels": {"file": LABELS_NII, "sha256": _file_sha256(nii_path), "definition": "parcel id (1..R) of every connectome mask voxel; 0 outside the mask"},
                "table": {"file": LABELS_TSV, "sha256": _file_sha256(tsv_path), "columns": list(TSV_COLUMNS)},
            },
            "build": {
                "created": _now_iso(),
                "elapsed": elapsed,
                "seconds_per_subject_median": _median(subject_seconds),
                "seconds_per_subject_mean": float(np.mean(subject_seconds)) if subject_seconds else None,
                "checkpoint_every": checkpoint_every,
                "resumed": resumed,
                "threads": threads,
            },
            "run_id": run_id,
        }
        jobs.write_json_atomic(json_path, record)
        import shutil

        shutil.rmtree(work, ignore_errors=True)
        emit("result", kind="fastindex", path=json_path)
        if run_id:
            runs.update(run_root, run_id, status="done", ended=record["build"]["created"], outputs=[INDEX_JSON, P_NAME, C_NAME, LABELS_NII, LABELS_TSV],
                        summary={"n_subjects": n_done, "parcels": n_parcels, "supervoxels": tiling.stats.get("n_supervoxels"), "elapsed": elapsed, "seconds_per_subject": record["build"]["seconds_per_subject_median"]})
        emit("done", outputs=[json_path, p_path, c_path, nii_path, tsv_path], elapsed=elapsed, run_id=run_id)
        return FastIndex(p, c, tiling, record, out_dir, _file_sha256(json_path))
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


def load_index(path, bundle=None, verify=True):
    """Read a parcel index folder (or its index.json); return a :class:`FastIndex`.

    :param bundle: when given, the index must belong to this connectome (same mask and voxels).
    :param verify: check every file against the sha256 recorded in index.json.
    :raise ValueError: not an index, a file that does not match, or another connectome.
    """
    folder = os.path.dirname(path) if os.path.basename(os.fspath(path)) == INDEX_JSON else os.fspath(path)
    json_path = os.path.join(folder, INDEX_JSON)
    with open(json_path, encoding="utf-8") as f:
        metadata = json.load(f)
    if metadata.get("format") != FORMAT_NAME:
        raise ValueError(f"{json_path} is not a fast index ({FORMAT_NAME})")
    if int(metadata.get("version", 0)) > FORMAT_VERSION:
        raise ValueError(f"The fast index has version {metadata.get('version')}; this library reads version {FORMAT_VERSION}")
    files = metadata.get("files") or {}
    if verify:
        for item in files.values():
            if _file_sha256(os.path.join(folder, item["file"])) != item.get("sha256"):
                raise ValueError(f"{item['file']} does not match the sha256 recorded in {INDEX_JSON}")
    tiling = Tiling.load(folder)
    p = np.load(os.path.join(folder, files.get("P", {}).get("file", P_NAME)), allow_pickle=False)
    c = np.load(os.path.join(folder, files.get("C", {}).get("file", C_NAME)), allow_pickle=False)
    if p.shape != (tiling.n_parcels, tiling.n_voxels) or c.shape != (tiling.n_parcels, tiling.n_parcels):
        raise ValueError("The index matrices do not match its tiling")
    if bundle is not None:
        bundle = _open_bundle(bundle)
        if bundle.n_voxels != tiling.n_voxels or not np.array_equal(bundle.mask_indices, tiling.mask_indices) or (metadata.get("bundle") or {}).get("mask_sha256") not in (None, bundle.mask_sha256):
            raise ValueError(f"The fast index belongs to another connectome ({(metadata.get('bundle') or {}).get('name')!r}) than {bundle.name!r}")
    return FastIndex(p, c, tiling, metadata, folder, _file_sha256(json_path))


# Apply


def _labels_of(index):
    """(labels (V,) int, n_parcels) of a FastIndex, a Tiling or a labels vector."""
    if isinstance(index, FastIndex):
        return index.tiling.labels, index.n_parcels
    if isinstance(index, Tiling):
        return index.labels, index.n_parcels
    labels = np.asarray(index, dtype=np.int64)
    return labels, int(labels.max()) + 1 if labels.size else 0


def _lesion_columns(occ, n_voxels):
    """Sparse (positions, weights) pairs of lesions given as a (V,) vector, an (L, V) matrix, one (positions, weights)
    tuple or a list of such pairs (or of (V,) vectors).

    A tuple of two (V,) vectors is two dense lesions, not one (positions, weights) pair, even when the first is of
    integers: a sparse pair of all V voxels must be given in a list, [(positions, weights)].
    """
    if (
        isinstance(occ, tuple)
        and len(occ) == 2
        and np.ndim(occ[0]) == 1
        and np.ndim(occ[1]) == 1
        and np.issubdtype(np.asarray(occ[0]).dtype, np.integer)
        and not (np.size(occ[0]) == n_voxels and np.size(occ[1]) == n_voxels)
    ):
        occ = [occ]  # one lesion as a (positions, weights) tuple
    if isinstance(occ, (list, tuple)):
        columns = []
        for item in occ:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                positions = np.asarray(item[0], dtype=np.int64).ravel()
                weights = np.asarray(item[1], dtype=np.float64).ravel()
            else:
                dense = np.asarray(item, dtype=np.float64).ravel()
                if dense.size != n_voxels:
                    raise ValueError(f"A lesion has {dense.size} weights, the connectome has {n_voxels} voxels")
                positions = np.flatnonzero(dense > 0)
                weights = dense[positions]
            if positions.size and (positions.min() < 0 or positions.max() >= n_voxels):
                raise ValueError("Lesion voxel positions must index the connectome voxels")
            keep = weights > 0
            columns.append((positions[keep], weights[keep]))
        return columns
    dense = np.asarray(occ, dtype=np.float64)
    dense = dense[np.newaxis] if dense.ndim == 1 else dense
    if dense.ndim != 2 or dense.shape[1] != n_voxels:
        raise ValueError(f"Lesion occupancy has shape {dense.shape}, expected (L, {n_voxels})")
    columns = []
    for row in dense:
        positions = np.flatnonzero(row > 0)
        columns.append((positions, row[positions]))
    return columns


def parcel_occupancy(occ_sparse, labels, n_parcels=None):
    """Occupancy of every parcel by every lesion: m[l, i] = sum of lesion l's weights on parcel i's voxels.

    :param occ_sparse: lesions as a list of (voxel positions, weights) pairs (``qa.relocated_lesions(sparse=True)``),
      an (L, V) matrix or one (V,) vector of weights over the connectome voxels.
    :param labels: a :class:`FastIndex`, a :class:`Tiling` or a (V,) parcel index vector (0..R-1).
    :return: (L, R) float64.
    """
    labels, count = _labels_of(labels)
    n_parcels = count if n_parcels is None else int(n_parcels)
    columns = _lesion_columns(occ_sparse, labels.size)
    lengths = np.array([positions.size for positions, _ in columns], dtype=np.int64)
    out = np.zeros((len(columns), n_parcels), dtype=np.float64)
    if lengths.sum() == 0:
        return out
    rows = np.repeat(np.arange(len(columns), dtype=np.int64), lengths)
    positions = np.concatenate([positions for positions, _ in columns])
    weights = np.concatenate([weights for _, weights in columns])
    parcel = labels[positions].astype(np.int64)
    inside = parcel >= 0
    flat = rows[inside] * n_parcels + parcel[inside]
    out.reshape(-1)[:] = np.bincount(flat, weights=weights[inside], minlength=out.size)
    return out


def approximate_maps(occ, index, occupancy=None):
    """Approximate maps Fz~ = (m @ P) / sum(m) of lesions (labelled "approximate z-scale"; rank rules only).

    :param occ: lesions (see :func:`parcel_occupancy`); ignored when ``occupancy`` gives m directly.
    :param index: a :class:`FastIndex`.
    :param occupancy: optional (L, R) parcel occupancy.
    :return: (L, V) float32; all zeros for a lesion without voxels in the connectome mask.
    """
    m = parcel_occupancy(occ, index) if occupancy is None else np.atleast_2d(np.asarray(occupancy, dtype=np.float64))
    if m.shape[1] != index.n_parcels:
        raise ValueError(f"The occupancy has {m.shape[1]} parcels, the index {index.n_parcels}")
    total = m.sum(axis=1, keepdims=True)
    weights = np.divide(m, total, out=np.zeros_like(m), where=total > 0).astype(np.float32)
    used = np.flatnonzero(np.any(weights != 0, axis=0))
    if used.size == 0:
        return np.zeros((m.shape[0], index.n_voxels), dtype=np.float32)
    if index._p32 is None and used.size <= max(ON_DEMAND_ROWS, index.n_parcels // 8):
        return weights[:, used] @ index.rows32(used)  # a few lesions: convert only the rows they touch
    return weights @ index.rows32()  # null sets and cohorts: keep a float32 copy of P (4 * R * V bytes)


def coverage(occ, index, occupancy=None):
    """Per lesion, how its occupancy splits between atlas parcels and supervoxels.

    :return: list of dicts {"total" (sum of weights in the mask), "atlas", "supervoxel" (fractions of the
      total; None for an empty lesion) and "by_source" ({atlas id or "supervoxel": fraction})}.
    """
    tiling = index.tiling if isinstance(index, FastIndex) else index
    m = parcel_occupancy(occ, tiling) if occupancy is None else np.atleast_2d(np.asarray(occupancy, dtype=np.float64))
    sources = tiling.sources
    names = list(dict.fromkeys(sources.tolist()))
    supervoxel = tiling.is_supervoxel
    out = []
    for row in m:
        total = float(row.sum())
        if total <= 0:
            out.append({"total": 0.0, "atlas": None, "supervoxel": None, "by_source": {}})
            continue
        by_source = {name: float(row[sources == name].sum() / total) for name in names}
        out.append({"total": total, "atlas": float(row[~supervoxel].sum() / total), "supervoxel": float(row[supervoxel].sum() / total), "by_source": {k: v for k, v in by_source.items() if v > 0}})
    return out


def lesion_strata(occ, index):
    """Location stratum of each lesion: the stratum of the parcel at the lesion voxel nearest its weighted centroid.

    :return: list of strata (None for an empty lesion).
    """
    tiling = index.tiling if isinstance(index, FastIndex) else index
    grid, indices = _geometry(tiling)
    strata = tiling.strata
    out = []
    for positions, weights in _lesion_columns(occ, tiling.n_voxels):
        if positions.size == 0:
            out.append(None)
            continue
        ijk = np.stack(np.unravel_index(indices[positions], grid.shape), axis=1).astype(np.float64)
        world = grid.voxel_to_world(ijk)
        centroid = (world * weights[:, np.newaxis]).sum(axis=0) / weights.sum()
        nearest = int(np.argmin(((world - centroid) ** 2).sum(axis=1)))
        out.append(str(strata[tiling.labels[positions[nearest]]]))
    return out


def lesion_volumes_ml(occ, index):
    """Volume (ml) of each lesion inside the connectome mask: the sum of its weights times the voxel volume."""
    tiling = index.tiling if isinstance(index, FastIndex) else index
    voxel_ml = tiling.grid.voxel_volume / 1000.0
    return [float(weights.sum() * voxel_ml) for _, weights in _lesion_columns(occ, tiling.n_voxels)]


def size_bin(volume_ml, edges=SIZE_BINS_ML, names=SIZE_BIN_NAMES):
    """Size bin name of a lesion volume in ml (None for an unknown volume)."""
    if volume_ml is None or not np.isfinite(volume_ml):
        return None
    return names[int(np.searchsorted(np.asarray(edges, dtype=np.float64), float(volume_ml), side="right"))]


# Parity


def top_set(values, top_pct=DEFAULT_TOP_PCT, sign="both"):
    """Boolean mask of the top ``top_pct`` percent of voxels of a map by rank: |value| ("both"), value ("pos") or -value ("neg").

    Exactly ceil(top_pct / 100 * n) finite voxels are selected (ties broken by voxel order).
    """
    values = np.asarray(values, dtype=np.float64).ravel()
    scores = {"both": np.abs(values), "pos": values, "neg": -values}[sign]
    scores = np.where(np.isfinite(scores), scores, -np.inf)
    n_finite = int(np.count_nonzero(np.isfinite(values)))
    k = min(n_finite, int(np.ceil(float(top_pct) / 100.0 * n_finite)))
    out = np.zeros(values.size, dtype=bool)
    if k > 0:
        out[np.argsort(-scores, kind="stable")[:k]] = True
    return out


def top_dice(a, b, top_pct=DEFAULT_TOP_PCT, sign="both"):
    """Rank Dice: the Dice overlap of the top ``top_pct`` percent voxels of two maps (NaN when both sets are empty)."""
    first, second = top_set(a, top_pct, sign), top_set(b, top_pct, sign)
    total = int(first.sum() + second.sum())
    return float(2.0 * np.count_nonzero(first & second) / total) if total else float("nan")


def _covariates(reference, keys, n_voxels):
    """(k, V) covariate rows from a reference.Reference, a dict of maps or an array; None without a reference."""
    if reference is None:
        return None
    if hasattr(reference, "covariates"):
        rows = reference.covariates(keys)
    elif isinstance(reference, dict):
        rows = np.vstack([np.asarray(reference[key], dtype=np.float64) for key in keys])
    else:
        rows = np.atleast_2d(np.asarray(reference, dtype=np.float64))
    if rows.shape[1] != n_voxels:
        raise ValueError(f"The reference maps have {rows.shape[1]} voxels, the maps {n_voxels}")
    return rows


def _reference_record(reference):
    if reference is None:
        return None
    if hasattr(reference, "sha256") and hasattr(reference, "definition"):
        return {"definition": reference.definition, "sha256": reference.sha256, "subjects_sha256": reference.subjects_sha256}
    return {"definition": "supplied", "sha256": None}


def parity(exact_fz, approx, reference, strata, sizes_ml=None, names=None, top_pct=DEFAULT_TOP_PCT, sign="both", covariates=PARITY_COVARIATES):
    """Per-lesion agreement of approximate maps with exact maps; return the parity report (a dict, see :func:`gate`).

    :param exact_fz: (L, V) exact mean Fz maps (engine AvgFz) over the connectome voxels.
    :param approx: (L, V) approximate maps (:func:`approximate_maps`) of the same lesions.
    :param reference: the bundle's reference maps (``reference.Reference``, a dict with ``covariates`` keys,
      or a (k, V) array): mu_r and PC1 are partialled out. None: the partial r is not computed and the gate fails.
    :param strata: the location stratum of each lesion (:func:`lesion_strata`).
    :param sizes_ml: lesion volumes in ml (:func:`lesion_volumes_ml`), for the size bins. None: the gate is
      applied per stratum only (the report's "size_axis" is false).
    :return: {"format", "version", "label", "gate_constants", "top_pct", "sign", "covariates", "reference",
      "n", "lesions": [{"name", "stratum", "volume_ml", "size_bin", "r", "partial_r", "top10_dice"}], "gate": ...}.
    """
    from .similarity import partial_spatial_correlation, pearson

    exact_fz = np.atleast_2d(np.asarray(exact_fz, dtype=np.float64))
    approx = np.atleast_2d(np.asarray(approx, dtype=np.float64))
    if exact_fz.shape != approx.shape:
        raise ValueError(f"The exact maps have shape {exact_fz.shape} and the approximate maps {approx.shape}")
    n_lesions, n_voxels = exact_fz.shape
    strata = list(strata)
    if len(strata) != n_lesions:
        raise ValueError(f"{len(strata)} strata for {n_lesions} lesions")
    sizes = [None] * n_lesions if sizes_ml is None else [None if v is None else float(v) for v in sizes_ml]
    names = [f"lesion-{i + 1}" for i in range(n_lesions)] if names is None else [str(n) for n in names]
    rows = _covariates(reference, covariates, n_voxels)
    lesions = []
    for index in range(n_lesions):
        partial = partial_spatial_correlation(exact_fz[index], approx[index], rows) if rows is not None else float("nan")
        lesions.append({
            "name": names[index],
            "stratum": strata[index],
            "volume_ml": sizes[index],
            "size_bin": size_bin(sizes[index]),
            "r": _finite(pearson(exact_fz[index], approx[index])),
            "partial_r": _finite(partial),
            "top10_dice": _finite(top_dice(exact_fz[index], approx[index], top_pct, sign)),
        })
    report = {
        "format": PARITY_FORMAT,
        "version": 1,
        "label": APPROX_LABEL,
        "rules": RULES_LABEL,
        "gate_constants": dict(FASTMODE_GATE),
        "min_lesions": PARITY_MIN_LESIONS,
        "min_group": GATE_MIN_GROUP,
        "top_pct": float(top_pct),
        "sign": sign,
        "covariates": list(covariates),
        "reference": _reference_record(reference),
        "size_bins_ml": list(SIZE_BINS_ML),
        "size_axis": sizes_ml is not None,
        "n": n_lesions,
        "lesions": lesions,
    }
    report["gate"] = gate(report)
    return report


def _group_verdict(members, constants, min_group):
    n = len(members)
    partial = _median([m["partial_r"] for m in members])
    dice = _median([m["top10_dice"] for m in members])
    verdict = {"n": n, "median_partial_r": partial, "median_top10_dice": dice, "passed": False}
    if n < min_group:
        verdict["reason"] = f"only {n} exact lesion(s); the gate needs at least {min_group}"
    elif partial is None:
        verdict["reason"] = "the partial r is not available (no reference maps)"
    elif partial < constants["partial_r"]:
        verdict["reason"] = f"median partial r {partial:.2f} < {constants['partial_r']:.2f}"
    elif dice is None or dice < constants["top10_dice"]:
        verdict["reason"] = f"median top-10% Dice {'n/a' if dice is None else f'{dice:.2f}'} < {constants['top10_dice']:.2f}"
    else:
        verdict["passed"] = True
        verdict["reason"] = "passed"
    return verdict


def gate(report, constants=None, min_lesions=None, min_group=None):
    """Apply the parity gate to a parity report (per stratum and per size bin); return the gate dict.

    A stratum (or size bin) passes when it has at least ``min_group`` exact lesions, its median partial r
    reaches ``constants["partial_r"]`` and its median top-10% Dice ``constants["top10_dice"]``. Fast-mode
    statistics may use a lesion only when both its stratum and its size bin passed (and the report holds at
    least ``min_lesions`` lesions); the other lesions are listed in "excluded" with a message. A report made
    without lesion volumes ("size_axis" false) is gated per stratum only.

    :return: {"constants", "min_lesions", "min_group", "n", "enough_lesions", "size_axis", "strata": {stratum:
      verdict}, "size_bins": {bin: verdict}, "allowed_strata", "allowed_size_bins", "excluded": [{"name",
      "stratum", "size_bin", "message"}], "messages"}.
    """
    constants = dict(report.get("gate_constants") or FASTMODE_GATE) if constants is None else dict(constants)
    min_lesions = int(report.get("min_lesions", PARITY_MIN_LESIONS) if min_lesions is None else min_lesions)
    min_group = int(report.get("min_group", GATE_MIN_GROUP) if min_group is None else min_group)
    lesions = report.get("lesions") or []
    enough = len(lesions) >= min_lesions
    size_axis = bool(report.get("size_axis", any(m.get("size_bin") is not None for m in lesions)))
    strata = {}
    for stratum in [*STRATA, *sorted({str(m["stratum"]) for m in lesions if m.get("stratum") and m["stratum"] not in STRATA})]:
        members = [m for m in lesions if m.get("stratum") == stratum]
        if members:
            strata[stratum] = _group_verdict(members, constants, min_group)
    bins = {}
    for name in SIZE_BIN_NAMES if size_axis else ():
        members = [m for m in lesions if m.get("size_bin") == name]
        if members:
            bins[name] = _group_verdict(members, constants, min_group)
    messages = []
    if not enough:
        messages.append(f"The parity gate needs at least {min_lesions} lesions mapped exactly; {len(lesions)} were given. Fast mode is not allowed for any lesion.")
    if not size_axis:
        messages.append("No lesion volumes were given: the gate is applied per stratum only.")
    if report.get("reference") is None:
        messages.append("No reference maps: the partial r (mu_r and PC1 partialled out) cannot be computed, so no stratum passes. Build them with 'python -m LesionNetworkMappingLib.reference build'.")
    allowed_strata = sorted(s for s, v in strata.items() if v["passed"] and enough)
    allowed_bins = [b for b in SIZE_BIN_NAMES if b in bins and bins[b]["passed"] and enough]
    excluded = []
    for m in lesions:
        reasons = []
        if not enough:
            reasons.append("too few exact lesions for the gate")
        else:
            verdict = strata.get(m.get("stratum"))
            if m.get("stratum") is None:
                reasons.append("the lesion has no voxel in the connectome mask")
            elif not verdict or not verdict["passed"]:
                reasons.append(f"the {m.get('stratum')} stratum failed the parity gate ({verdict['reason'] if verdict else 'not evaluated'})")
            bin_verdict = bins.get(m.get("size_bin"))
            if size_axis and m.get("size_bin") is None:
                reasons.append("the lesion volume is unknown")
            elif size_axis and (not bin_verdict or not bin_verdict["passed"]):
                reasons.append(f"the {m.get('size_bin')} size bin failed the parity gate ({bin_verdict['reason'] if bin_verdict else 'not evaluated'})")
        if reasons:
            excluded.append({"name": m.get("name"), "stratum": m.get("stratum"), "size_bin": m.get("size_bin"), "message": "Excluded from fast-mode statistics: " + "; ".join(reasons) + "."})
    for stratum, verdict in strata.items():
        if not verdict["passed"]:
            messages.append(f"Fast mode is not allowed for {stratum} lesions: {verdict['reason']}.")
    for name, verdict in bins.items():
        if not verdict["passed"]:
            messages.append(f"Fast mode is not allowed for lesions of size {name}: {verdict['reason']}.")
    return {
        "constants": constants,
        "min_lesions": min_lesions,
        "min_group": min_group,
        "n": len(lesions),
        "enough_lesions": enough,
        "size_axis": size_axis,
        "strata": strata,
        "size_bins": bins,
        "allowed_strata": allowed_strata,
        "allowed_size_bins": allowed_bins,
        "excluded": excluded,
        "messages": messages,
    }


def allowed_strata(report):
    """Strata in which fast-mode statistics are allowed by a parity report (its gate, recomputed when missing)."""
    return list((report.get("gate") or gate(report))["allowed_strata"])


def lesion_allowed(report, stratum, volume_ml=None, size=None):
    """(allowed, message): whether fast mode may be used for a lesion of this stratum and size under a parity report."""
    result = report.get("gate") or gate(report)
    size = size or size_bin(volume_ml)
    if not result["enough_lesions"]:
        return False, f"Fast mode is not allowed: the parity gate needs at least {result['min_lesions']} exact lesions."
    if stratum is None:
        return False, "Fast mode is not allowed for this lesion: it has no voxel in the connectome mask, so its location stratum is unknown."
    if stratum not in result["allowed_strata"]:
        verdict = result["strata"].get(stratum)
        return False, f"Fast mode is not allowed for {stratum} lesions: {verdict['reason'] if verdict else 'no exact lesion of this stratum was checked'}."
    if not result.get("size_axis", True):
        return True, "allowed by the parity gate (per stratum; no lesion volumes were given)"
    if size not in result["allowed_size_bins"]:
        if size is None:
            return False, "Fast mode is not allowed for this lesion: its volume is unknown, so its size bin cannot be checked."
        verdict = result["size_bins"].get(size)
        return False, f"Fast mode is not allowed for lesions of size {size}: {verdict['reason'] if verdict else 'no exact lesion of this size was checked'}."
    return True, "allowed by the parity gate"


# External parcel matrix (C parity only)


def import_parcel_matrix(mat_path):
    """Read a user-downloaded parcel FCz matrix (e.g. GSP1000_FCz_Schaefer1000Melbourne54.mat) for a C-parity comparison only.

    The largest square 2D numeric variable is taken. The file is never redistributed: the result carries the
    GSP data use terms tag ("GSP-DUT-2014") and is meant for :func:`compare_parcel_matrices`.

    :return: {"matrix" (R, R) float64, "variable", "variables" (all names), "file" (base name), "sha256", "terms",
      "acknowledgement"}.
    :raise ValueError: no square matrix in the file, or a MATLAB v7.3 file without h5py.
    """
    from .export import GSP_ACKNOWLEDGEMENT, GSP_LICENCE

    candidates = {}
    try:
        from scipy import io

        contents = io.loadmat(mat_path)
        for key, value in contents.items():
            if not key.startswith("__") and isinstance(value, np.ndarray) and value.ndim == 2 and value.shape[0] == value.shape[1] and value.dtype.kind in "fiu":
                candidates[key] = value
    except NotImplementedError:  # MATLAB v7.3 (HDF5)
        try:
            import h5py
        except ImportError:
            raise ValueError(f"{os.path.basename(mat_path)} is a MATLAB v7.3 file, which needs h5py; save it again with -v7 or install h5py") from None
        with h5py.File(mat_path, "r") as f:
            for key, value in f.items():
                if hasattr(value, "shape") and len(value.shape) == 2 and value.shape[0] == value.shape[1]:
                    candidates[key] = np.asarray(value).T
    if not candidates:
        raise ValueError(f"{os.path.basename(mat_path)} holds no square 2D numeric matrix")
    variable = max(candidates, key=lambda key: candidates[key].shape[0])
    return {
        "matrix": np.asarray(candidates[variable], dtype=np.float64),
        "variable": variable,
        "variables": sorted(candidates),
        "file": os.path.basename(mat_path),
        "sha256": _file_sha256(mat_path),
        "terms": GSP_LICENCE,
        "acknowledgement": GSP_ACKNOWLEDGEMENT,
        "use": "C-parity comparison only",
    }


def compare_parcel_matrices(a, b, order=None):
    """Spatial agreement of two parcel matrices: Pearson r and Spearman rho of their upper-triangle entries.

    :param order: optional parcel indices into ``b`` giving, for each parcel of ``a``, the same parcel in ``b``
      (the parcel orders of the two matrices must agree; this function cannot tell).
    """
    from .similarity import pearson_spearman

    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if order is not None:
        order = np.asarray(order, dtype=np.int64)
        b = b[np.ix_(order, order)]
    if a.shape != b.shape or a.ndim != 2 or a.shape[0] != a.shape[1]:
        raise ValueError(f"The parcel matrices have shapes {a.shape} and {b.shape}; they must be the same square shape")
    upper = np.triu_indices(a.shape[0], k=1)
    r, rho = pearson_spearman(a[upper], b[upper])
    return {"r": _finite(r), "rho": _finite(rho), "n_pairs": int(upper[0].size)}


def atlas_block(index, sources):
    """(C sub-matrix, parcel indices) of the parcels of the given atlas sources, in tiling order (for C parity)."""
    sources = [sources] if isinstance(sources, str) else list(sources)
    rows = np.flatnonzero(np.isin(index.tiling.sources, sources))
    return index.C[np.ix_(rows, rows)], rows


# Command line


def _grid_source(index):
    return types.SimpleNamespace(grid=index.grid, mask_indices=index.mask_indices)


def _load_lesions(paths, index):
    """(names, (L, V) float32 weights, qc list) of lesion files resampled to the index grid (engine.prepare_lesion)."""
    from .engine import _lesion_name, _unique_name, prepare_lesion

    names, weights, qc = [], [], []
    source = _grid_source(index)
    for path in paths:
        vector, values = prepare_lesion(path, source)
        name = _unique_name(_lesion_name(path), names)
        names.append(name)
        weights.append(vector)
        qc.append({"name": name, "file": os.path.basename(path), "sha256": _file_sha256(path), **values})
    return names, np.stack(weights) if weights else np.zeros((0, index.n_voxels), np.float32), qc


def _map_vector(path, index):
    """A map NIfTI on the connectome grid as a (V,) float64 vector over the index voxels."""
    from .nifti import read_nifti

    data, affine, _header = read_nifti(path)
    data = np.asarray(data, dtype=np.float64)
    data = data.reshape(data.shape[:3])
    if not Grid(data.shape, affine).matches(index.grid):
        data = resample_to_grid(data, affine, index.grid, mode="linear")
    return to_masked_vector(data, index.mask_indices)


def _engine_run_pairs(path):
    """(lesion file, exact AvgFz map) pairs of an engine run.json."""
    with open(path, encoding="utf-8") as f:
        run = json.load(f)
    folder = os.path.dirname(os.path.abspath(path))
    pairs = []
    for lesion in run.get("lesions") or []:
        maps = (run.get("maps") or {}).get(lesion.get("name")) or {}
        if "AvgFz" not in maps:
            raise ValueError(f"{os.path.basename(path)} has no AvgFz map for lesion {lesion.get('name')!r}")
        pairs.append((os.path.join(folder, lesion["file"]), os.path.join(folder, maps["AvgFz"])))
    if not pairs:
        raise ValueError(f"{os.path.basename(path)} lists no lesions")
    return pairs


def _build_main(argv):
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.fastmap build", description="Build the fast parcel index of a connectome (approximate z-scale). "
                                     "Memory: about 8 * V * R bytes for the sums plus 2 * R * V for P (about 3.2 GB at V = 228k voxels and R = 1400 parcels); "
                                     "applying the index to many lesions keeps a float32 copy of P (4 * R * V bytes).")
    jobs.add_common_arguments(parser)
    parser.add_argument("--bundle", required=True, help="connectome bundle directory (.lnmc)")
    parser.add_argument("--subjects", type=jobs.positive_int, default=None, help="use the first N connectome subjects (default: all)")
    parser.add_argument("--subject-list", default=None, metavar="SPEC", help="a named subset of the bundle, a text file of subject ids or a comma-separated list of ids")
    parser.add_argument("--out", default=None, help="output folder (default: <bundle>/derived/fastindex[/subj-<sha12>])")
    parser.add_argument("--tiling", default=None, metavar="DIR", help="use the tiling of this folder (labels.nii.gz, labels.tsv and tiling.json, or an index folder) instead of building one")
    parser.add_argument("--atlas", action="append", default=None, metavar="ID[:STRATUM]", help="tiling atlas catalog id in priority order (repeatable; default: Schaefer-400 7N, Tian S2, Buckner 7N cerebellum, Harvard-Oxford brain stem); other atlases need a stratum")
    parser.add_argument("--aan", action="store_true", help="also use the Harvard AAN brain stem nuclei (when cached)")
    parser.add_argument("--cache", default=None, help="atlas cache folder (default: the fetch cache)")
    parser.add_argument("--cell-mm", type=float, default=DEFAULT_CELL_MM, help=f"supervoxel cell size in mm (default: {DEFAULT_CELL_MM:g})")
    parser.add_argument("--min-piece", type=jobs.positive_int, default=DEFAULT_MIN_PIECE, help=f"supervoxel pieces under this many voxels are merged into a neighbour (default: {DEFAULT_MIN_PIECE})")
    parser.add_argument("--allow-cross-family", action="store_true", help="use atlases of another template family than the connectome")
    parser.add_argument("--checkpoint-every", type=jobs.positive_int, default=DEFAULT_CHECKPOINT, help=f"save the progress every this many subjects (default: {DEFAULT_CHECKPOINT})")
    parser.add_argument("--chunk", type=jobs.positive_int, default=None, help="voxels per matrix product")
    args = parser.parse_args(argv)
    if args.subject_list is not None and args.subjects is not None:
        parser.error("--subject-list cannot be combined with --subjects")
    if args.tiling is not None and args.atlas is not None:
        parser.error("--tiling cannot be combined with --atlas")
    if args.cell_mm <= 0:
        parser.error("--cell-mm must be positive")
    cancel = jobs.CancelToken(args.cancel_file)
    try:
        bundle = _open_bundle(args.bundle)
        if args.tiling is not None:
            tiling = Tiling.load(args.tiling)
        else:
            entries = None
            if args.atlas is not None:
                defaults = {entry["id"]: entry for entry in DEFAULT_TILING_ATLASES}
                entries = []
                for text in args.atlas:
                    atlas_id, _, stratum = text.partition(":")
                    entry = dict(defaults.get(atlas_id) or {"id": atlas_id})
                    if stratum:
                        entry["stratum"] = stratum
                    if entry.get("stratum") not in STRATA[:-1]:
                        parser.error(f"--atlas {text}: give the stratum of this atlas as ID:STRATUM ({', '.join(STRATA[:-1])})")
                    entries.append(entry)
            tiling = build_tiling(bundle, entries, args.cache, args.cell_mm, args.min_piece, args.aan, args.allow_cross_family)
        build_index(bundle, tiling, subjects=args.subjects, subject_list=args.subject_list, out_dir=args.out, checkpoint_every=args.checkpoint_every, chunk=args.chunk,
                    resume=args.resume, cancel=cancel, run_root=args.run_root, parent_runs=args.parent_run, argv=argv, threads=args.threads)
    except (jobs.Cancelled, jobs.JobError):
        raise
    except fetch.NotCachedError as exc:
        raise jobs.JobError(str(exc), code="not_cached") from None
    except (OSError, ValueError, KeyError, fetch.FetchError) as exc:
        raise jobs.JobError(f"{type(exc).__name__}: {exc}", code="input") from None
    return jobs.EXIT_OK


def _parity_main(argv):
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.fastmap parity", description="Parity of the fast index with exact lesion maps, and its gate.")
    jobs.add_common_arguments(parser)
    parser.add_argument("--bundle", required=True, help="connectome bundle directory (.lnmc) of the index")
    parser.add_argument("--index", required=True, help="fast index folder")
    parser.add_argument("--out", required=True, help="output folder (parity.json)")
    parser.add_argument("--pair", nargs=2, action="append", default=[], metavar=("LESION", "EXACT_FZ"), help="a lesion file and its exact mean Fz map (engine desc-AvgRFz; repeatable)")
    parser.add_argument("--engine-run", action="append", default=[], metavar="RUN_JSON", help="an engine run.json whose lesions and AvgFz maps are used (repeatable)")
    parser.add_argument("--reference", default=None, help="reference folder (default: the bundle's reference of the index subjects, else of all subjects)")
    parser.add_argument("--top-pct", type=float, default=DEFAULT_TOP_PCT, help=f"rank Dice on the top this percent of voxels (default: {DEFAULT_TOP_PCT:g})")
    parser.add_argument("--sign", choices=("both", "pos", "neg"), default="both", help="rank by |value| (both, default), value (pos) or -value (neg)")
    args = parser.parse_args(argv)
    if not args.pair and not args.engine_run:
        parser.error("give --pair LESION EXACT_FZ or --engine-run run.json")
    if not 0 < args.top_pct <= 100:
        parser.error("--top-pct must be in (0, 100]")
    from .reference import load_reference

    cancel = jobs.CancelToken(args.cancel_file)
    try:
        bundle = _open_bundle(args.bundle)
        index = load_index(args.index, bundle)
        pairs = [tuple(pair) for pair in args.pair]
        for path in args.engine_run:
            pairs.extend(_engine_run_pairs(path))
        jobs.emit("start", job="parity", argv=argv, threads=args.threads, index=os.path.basename(os.path.abspath(args.index)), lesions=len(pairs))
        reference = load_reference(bundle, subjects_sha=(index.metadata.get("subjects") or {}).get("sha256"), path=args.reference)
        if reference is None and args.reference is None:
            reference = load_reference(bundle)
        if reference is None:
            jobs.emit("warning", code="no_reference", message="No valid reference maps: the partial r cannot be computed and no stratum passes the gate.")
        names, weights, qc = _load_lesions([lesion for lesion, _ in pairs], index)
        cancel.raise_if_cancelled(done=0)
        exact = np.stack([_map_vector(path, index) for _, path in pairs])
        approx = approximate_maps(weights, index)
        report = parity(exact, approx, reference, lesion_strata(weights, index), lesion_volumes_ml(weights, index), names, args.top_pct, args.sign)
    except (jobs.Cancelled, jobs.JobError):
        raise
    except (OSError, ValueError, KeyError) as exc:
        raise jobs.JobError(f"{type(exc).__name__}: {exc}", code="input") from None
    cover = coverage(weights, index)
    for lesion, lesion_qc, lesion_cover, (_, exact_path) in zip(report["lesions"], qc, cover, pairs, strict=True):
        lesion.update({"file": lesion_qc["file"], "sha256": lesion_qc["sha256"], "exact_map": os.path.basename(exact_path), "coverage": lesion_cover})
    report["index"] = {"sha256": index.sha256, "run_id": index.metadata.get("run_id"), "bundle": (index.metadata.get("bundle") or {}).get("name"), "subjects": index.metadata.get("subjects"), "parcels": index.n_parcels}
    report["terms"] = index.terms
    report["acknowledgement"] = index.metadata.get("acknowledgement")
    report["created"] = _now_iso()
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    run_root = os.path.abspath(args.run_root) if args.run_root else out_dir
    parents = list(args.parent_run) + ([index.metadata["run_id"]] if index.metadata.get("run_id") else [])
    run_id = runs.register(run_root, "parity", out_dir, parents=parents, params={"index_sha256": index.sha256, "top_pct": args.top_pct, "sign": args.sign, "gate": FASTMODE_GATE, "n_lesions": len(names)})
    report["run_id"] = run_id
    path = os.path.join(out_dir, PARITY_JSON)
    jobs.write_json_atomic(path, report)
    for message in report["gate"]["messages"]:
        jobs.emit("warning", code="parity_gate", message=message)
    result = report["gate"]
    runs.update(run_root, run_id, status="done", ended=report["created"], outputs=[PARITY_JSON], summary={"n": report["n"], "allowed_strata": result["allowed_strata"], "allowed_size_bins": result["allowed_size_bins"], "excluded": len(result["excluded"])})
    jobs.emit("result", kind="parity", path=path, allowed_strata=result["allowed_strata"], allowed_size_bins=result["allowed_size_bins"])
    jobs.emit("done", outputs=[path], elapsed=round(jobs.elapsed(), 3), run_id=run_id)
    return jobs.EXIT_OK


def approximate_map_path(out_dir, lesion_name, bundle_name):
    """File name of a lesion's approximate map: <lesion>_conn-<label>_desc-FastApproxFz_funcmap.nii.gz."""
    from .engine import connectome_label

    return os.path.join(out_dir, f"{lesion_name}_conn-{connectome_label(bundle_name)}_desc-{APPROX_DESC}_funcmap.nii.gz")


def _apply_main(argv):
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.fastmap apply", description="Approximate lesion maps from the fast index (approximate z-scale; rank rules only).")
    parser.add_argument("--threads", type=jobs.positive_int, default=2, help="BLAS threads (default: 2)")
    parser.add_argument("--index", required=True, help="fast index folder")
    parser.add_argument("--lesion", action="append", required=True, help="lesion NIfTI file (any grid; repeatable)")
    parser.add_argument("--out", required=True, help="output folder")
    parser.add_argument("--parity", default=None, help="parity.json of this index: each lesion is marked allowed or excluded by its gate")
    args = parser.parse_args(argv)
    from .nifti import write_nifti

    try:
        index = load_index(args.index)
        jobs.emit("start", job="fastmap-apply", argv=argv, threads=args.threads, lesions=len(args.lesion))
        names, weights, qc = _load_lesions(args.lesion, index)
        report = None
        if args.parity:
            with open(args.parity, encoding="utf-8") as f:
                report = json.load(f)
            if (report.get("index") or {}).get("sha256") not in (None, index.sha256):
                jobs.emit("warning", code="parity_index", message="The parity report belongs to another fast index; its gate is not applied.")
                report = None
        maps = approximate_maps(weights, index)
    except (OSError, ValueError, KeyError) as exc:
        raise jobs.JobError(f"{type(exc).__name__}: {exc}", code="input") from None
    os.makedirs(args.out, exist_ok=True)
    bundle_name = (index.metadata.get("bundle") or {}).get("name") or "connectome"
    strata = lesion_strata(weights, index)
    volumes = lesion_volumes_ml(weights, index)
    cover = coverage(weights, index)
    outputs, lesions = [], []
    for row, name in enumerate(names):
        path = approximate_map_path(args.out, name, bundle_name)
        write_nifti(path, from_masked_vector(maps[row], index.mask_indices, index.grid.shape, fill=0.0, dtype=np.float32), index.grid.affine, descrip=f"LNM fast mode, {APPROX_LABEL}")
        outputs.append(path)
        entry = {"name": name, "file": qc[row]["file"], "sha256": qc[row]["sha256"], "map": os.path.basename(path), "stratum": strata[row], "volume_ml": volumes[row], "size_bin": size_bin(volumes[row]), "coverage": cover[row]}
        if report is not None:
            entry["allowed"], entry["gate_message"] = lesion_allowed(report, strata[row], volumes[row])
        lesions.append(entry)
    record = {
        "format": APPLY_FORMAT,
        "version": 1,
        "label": APPROX_LABEL,
        "statistic": "approximate Fz (weighted average of parcel-seed mean Fz maps)",
        "rules": RULES_LABEL,
        "note": NOT_FINAL_NOTE,
        "index": {"sha256": index.sha256, "run_id": index.metadata.get("run_id"), "bundle": bundle_name, "subjects": index.metadata.get("subjects"), "parcels": index.n_parcels},
        "parity": None if report is None else {"run_id": report.get("run_id"), "gate": {key: report["gate"][key] for key in ("constants", "allowed_strata", "allowed_size_bins")}},
        "terms": index.terms,
        "acknowledgement": index.metadata.get("acknowledgement"),
        "lesions": lesions,
        "created": _now_iso(),
    }
    json_path = os.path.join(args.out, "fastmaps.json")
    jobs.write_json_atomic(json_path, record)
    jobs.emit("done", outputs=[*outputs, json_path], elapsed=round(jobs.elapsed(), 3), run_id=None)
    return jobs.EXIT_OK


_COMMANDS = {"build": _build_main, "parity": _parity_main, "apply": _apply_main}


def main(argv=None):
    """Command-line entry point (see the module docstring)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in _COMMANDS:
        parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.fastmap", description="Fast (parcel) mode: index, parity and approximate maps.")
        parser.add_argument("command", choices=list(_COMMANDS))
        parser.parse_args(argv[:1])
        return jobs.EXIT_USAGE
    return _COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    jobs.run_job(main, job="fastmap")

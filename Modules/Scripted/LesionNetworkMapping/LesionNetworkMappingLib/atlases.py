"""Space-aware multi-atlas labelling: label tables, network names and labels of points, masks and clusters.

An :class:`Atlas` is an integer label volume with its affine, its label table and its space family
(``geometry.space_family``). Atlases come from the catalog ``catalog/atlases.json`` through
:meth:`Atlas.load`, which reads the cache of :mod:`fetch` only: it never downloads (the labelling pipeline must
not touch the network; a download is an explicit click that runs ``python -m LesionNetworkMappingLib.fetch``).

Labels are computed through the affines, never by array index:

- :func:`label_point` maps a world coordinate (RAS mm) to the voxel of each atlas (``world_to_voxel``, nearest voxel);
- :func:`label_mask` resamples the occupancy (a binary or fractional image on any grid) onto each atlas grid with
  ``geometry.resample_to_grid`` in fraction mode, so volumes are conserved, and reports for every label the
  percentage of the mask inside it and the percentage of the label region covered by the mask;
- :func:`label_cluster` does the same for a cluster of a connectome grid (a flat or masked vector).

Space families are never mixed silently. An atlas of another family than the mask (or a family that cannot be
told) is skipped with a notice, because the templates of different families differ by a few millimetres in
subcortical structures. ``allow_cross_family=True`` labels anyway and records the decision in
``result.allowed_cross_family`` so that exports can state it. The family of the mask is ``space`` when given,
else it is recognized from the grid (``geometry.detect_grid_candidates``).

Network names: Schaefer parcels carry the Yeo network in their names (``7Networks_LH_Vis_1`` is "Vis"), Tian
regions belong to a structure ("aHIP-rh" is "HIP") and Buckner and Yeo volumes number the seven Yeo networks.
"""

import csv
import re
from typing import NamedTuple

import numpy as np

from . import fetch
from .geometry import Grid, detect_grid_candidates, from_masked_vector, resample_to_grid, space_family, world_to_voxel
from .nifti import read_nifti

YEO7_NAMES = {1: "Vis", 2: "SomMot", 3: "DorsAttn", 4: "SalVentAttn", 5: "Limbic", 6: "Cont", 7: "Default"}
TIAN_STRUCTURES = ("HIP", "AMY", "THA", "NAc", "GP", "PUT", "CAU")
NETWORK_SCHEMES = ("schaefer", "yeo7", "tian")
UNLABELLED = "unlabelled"

_SCHAEFER_NAME = re.compile(r"^(?:\d+Networks_)?(?:LH|RH)_([A-Za-z]+)(?:_|$)")
_TRAILING_NUMBER = re.compile(r"(\d+)\s*$")
_INTEGER = re.compile(r"[-+]?\d+")


# Network names


def network_of(name, scheme=None):
    """Network (or structure) a label name belongs to, or None.

    Without ``scheme``, Schaefer names are parsed ("7Networks_LH_Vis_1" -> "Vis", "17Networks_RH_DefaultA_PFCd_2"
    -> "DefaultA") and then Tian names ("aHIP-rh" -> "HIP", "THA-DP-lh" -> "THA", "NAc-shell-rh" -> "NAc").
    ``scheme`` "schaefer", "tian" or "yeo7" restricts the parse; "yeo7" takes the number at the end of the name
    ("Network3", "Network_3") as a Yeo 2011 network number (Buckner2011 cerebellum and Yeo2011 labels).
    """
    if not name:
        return None
    text = str(name).strip()
    if scheme in (None, "schaefer"):
        match = _SCHAEFER_NAME.match(text)
        if match:
            return match.group(1)
    if scheme in (None, "tian"):
        head = re.sub(r"-(?:lh|rh)$", "", text).split("-")[0]
        if head in TIAN_STRUCTURES:
            return head
        if len(head) > 1 and head[0] in "apml" and head[1:] in TIAN_STRUCTURES:
            return head[1:]
    if scheme == "yeo7":
        match = _TRAILING_NUMBER.search(text)
        if match:
            return YEO7_NAMES.get(int(match.group(1)))
    return None


def to_yeo7(network):
    """The Yeo 7-network name of a Schaefer 7- or 17-network name ("DefaultB" -> "Default", "VisCent" -> "Vis"); None if unknown."""
    if not network:
        return None
    text = str(network)
    if text == "TempPar":
        return "Default"
    if text in ("VisCent", "VisPeri"):
        return "Vis"
    base = re.sub(r"[ABC]$", "", text)
    return base if base in YEO7_NAMES.values() else None


# Label tables


def _read_lines(path):
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return [line.rstrip("\r\n") for line in f]


def read_label_table(path, fmt="tsv", id_offset=0, id_column="auto", values=None):
    """Read a label table file into {label value: name}.

    :param fmt: "tsv" (TemplateFlow: a header with ``index`` and ``name`` columns), "lines" (one name per
      line, label 1 first, as in Tian's label files) or "lut" (text lines holding an integer and a name, as in
      FreeSurfer colour tables and AAL: ``1 L_V1_ROI`` or ``1 Name 120 18 134 0`` or ``Name 1 2001``).
    :param id_offset: added to every index of a "tsv" or "lines" table (the Harvard-Oxford volumes store index + 1).
    :param id_column: for "lut" files, which integer of a line is the label value: "first", "last" or "auto"
      (the one that matches the values present in the image, ``values``).
    :param values: label values present in the image (used by id_column "auto").
    """
    lines = _read_lines(path)
    if fmt == "lines":
        names = [line.strip() for line in lines if line.strip()]
        return {index + 1 + int(id_offset): name for index, name in enumerate(names)}
    if fmt == "tsv":
        return _read_tsv(lines, id_offset)
    if fmt == "lut":
        return _read_lut(lines, id_column, values)
    raise ValueError(f"Unknown label table format: {fmt}")


def _read_tsv(lines, id_offset):
    rows = list(csv.reader([line for line in lines if line.strip()], delimiter="\t"))
    if not rows:
        return {}
    header = [cell.strip().lower() for cell in rows[0]]
    if "name" in header:
        name_column = header.index("name")
        id_column = next((header.index(key) for key in ("index", "id", "label", "value") if key in header), 0)
        body = rows[1:]
    else:
        id_column, name_column, body = 0, 1, rows
    labels = {}
    for row in body:
        if len(row) <= max(id_column, name_column):
            continue
        try:
            value = int(float(row[id_column]))
        except ValueError:
            continue
        labels[value + int(id_offset)] = row[name_column].strip()
    return labels


def _read_lut(lines, id_column, values):
    entries = []  # (name, first integer, last integer, integer before the name)
    for line in lines:
        text = line.strip()
        if not text or text.startswith(("#", "%", "//")):
            continue
        tokens = text.split()
        integers = [(index, int(token)) for index, token in enumerate(tokens) if _INTEGER.fullmatch(token)]
        name_index = next((index for index, token in enumerate(tokens) if not _INTEGER.fullmatch(token) and not re.fullmatch(r"[-+]?\d*\.\d+", token)), None)
        if name_index is None or not integers:
            continue
        before = integers[0][0] < name_index
        entries.append((tokens[name_index], integers[0][1], integers[-1][1], before))
    if id_column == "first":
        return {first: name for name, first, _last, _before in entries}
    if id_column == "last":
        return {last: name for name, _first, last, _before in entries}
    by_first = {first: name for name, first, _last, _before in entries}
    by_last = {last: name for name, _first, last, _before in entries}
    if values is not None:
        present = {int(value) for value in values}
        if len(present & set(by_last)) > len(present & set(by_first)):
            return by_last
        return by_first
    return by_first if entries and entries[0][3] else by_last


# Atlases


class Atlas:
    """An integer label volume with its affine, label table and space family.

    :ivar id: catalog id (or any name for atlases built in memory).
    :ivar data: int32 array indexed [i, j, k]; 0 is background.
    :ivar affine: 4x4 voxel-to-RAS matrix.
    :ivar labels: {label value: name}; values without a name are called "label N".
    :ivar family: space family ("fsl6", "2009", ...) or "unknown".
    :ivar space: space label ("MNI152NLin6Asym", ...) or None.
    :ivar networks: network scheme of the label names (see :func:`network_of`) or None.
    """

    def __init__(self, atlas_id, data, affine, labels=None, family="unknown", space=None, title=None, licence=None, badge=None, networks=None, warning=None, entry=None):
        data = np.asarray(data)
        if data.ndim == 4 and data.shape[3] == 1:
            data = data[..., 0]
        if data.ndim != 3:
            raise ValueError(f"An atlas is a 3D label volume, got shape {data.shape}")
        values = np.nan_to_num(data) if data.dtype.kind == "f" else data
        if data.dtype.kind == "f" and not np.array_equal(values, np.rint(values)):
            raise ValueError(f"{atlas_id} is not a label volume (non-integer values); probability maps are not labelled")
        self.id = atlas_id
        self.data = values.astype(np.int32, copy=False)
        self.affine = np.array(affine, dtype=np.float64).reshape(4, 4)
        self.labels = {int(key): str(value) for key, value in (labels or {}).items()}
        self.family = family or "unknown"
        self.space = space
        self.title = title or atlas_id
        self.licence = licence
        self.badge = badge
        self.networks = networks
        self.warning = warning
        self.entry = entry
        self._counts = None

    @property
    def shape(self):
        return self.data.shape

    @property
    def grid(self):
        return Grid(self.data.shape, self.affine)

    @property
    def voxel_volume(self):
        """Voxel volume in mm^3."""
        return float(abs(np.linalg.det(self.affine[:3, :3])))

    def __repr__(self):
        return f"Atlas({self.id!r}, family={self.family!r}, shape={self.shape}, labels={len(self.labels)})"

    def label_name(self, value):
        """Name of a label value (None for 0 and for values outside the atlas)."""
        value = int(value)
        if value == 0:
            return None
        return self.labels.get(value, f"label {value}")

    def network(self, value):
        """Network (or structure) of a label value, by this atlas's naming scheme; None if the atlas has none."""
        value = int(value)
        if value == 0 or self.networks is None:
            return None
        if self.networks == "yeo7":
            return YEO7_NAMES.get(value)
        return network_of(self.labels.get(value), self.networks)

    def region_counts(self):
        """{label value: number of voxels} of the non-zero labels (computed once)."""
        if self._counts is None:
            values, counts = np.unique(self.data[self.data != 0], return_counts=True)
            self._counts = {int(v): int(c) for v, c in zip(values, counts, strict=True)}
        return self._counts

    def values_at(self, xyz):
        """Label values at world coordinates (N, 3), nearest voxel through the affine; 0 outside the volume."""
        points = np.asarray(xyz, dtype=np.float64).reshape(-1, 3)
        ijk = np.floor(world_to_voxel(points, self.affine) + 0.5).astype(np.int64)
        shape = np.array(self.data.shape)
        inside = np.all((ijk >= 0) & (ijk < shape), axis=1)
        out = np.zeros(len(points), dtype=np.int32)
        out[inside] = self.data[ijk[inside, 0], ijk[inside, 1], ijk[inside, 2]]
        return out

    @classmethod
    def load(cls, entry, cache=None):
        """Load a catalog atlas from the cache (never downloads).

        :param entry: a catalog entry (:class:`fetch.Resource` or dictionary) or the id of an atlas.
        :raises fetch.NotCachedError: the atlas file or its label table is not cached (download it first with the
          fetch command; ``fetch.fetch_set`` or ``python -m LesionNetworkMappingLib.fetch get``).
        """
        resource = fetch.find_resource(entry, ("atlases", "templates")) if isinstance(entry, str) else fetch.Resource.coerce(entry)
        path = fetch.cached_path(resource, cache)
        if path is None:
            raise fetch.NotCachedError(f"{resource.title} is not cached: download it first.", resource_id=resource.id)
        data, affine, _header = read_nifti(path)
        if data.ndim == 4:
            data = data[..., 0]
        values = np.unique(data[data != 0]) if data.dtype.kind in "iuf" else None
        labels = cls._load_labels(resource, cache, values)
        family = resource.get("family")
        space = resource.get("space")
        if not family:
            match = detect_grid_candidates(data.shape, affine)
            family = match.family if match else "unknown"
            space = match.candidates[0] if match else space
        atlas = cls(resource.id, data, affine, labels, family, space, resource.title, resource.licence, resource.badge, resource.get("networks"), resource.get("warning"), resource)
        split = resource.get("hemisphere_split")
        if split:
            atlas._split_hemispheres(split)
        return atlas

    @staticmethod
    def _load_labels(resource, cache, values):
        table = resource.get("label_table")
        if not table:
            return {}
        if table.get("format") == "inline":
            return {int(key): name for key, name in (table.get("names") or {}).items()}
        label_resource = resource.label_resource()
        if label_resource is None:
            return {}
        path = fetch.cached_path(label_resource, cache)
        if path is None:
            raise fetch.NotCachedError(f"The label table of {resource.title} is not cached: download it first.", resource_id=label_resource.id)
        labels = read_label_table(path, table.get("format", "tsv"), table.get("id_offset", 0), table.get("id_column", "auto"), values)
        complete = table.get("complete")
        if complete:  # a published table that lists only some of the labels: the rest follows a known order
            sides = complete.get("sides") or [""]
            for area, name in enumerate(complete.get("names", ())):
                for side_index, side in enumerate(sides):
                    labels.setdefault(len(sides) * area + side_index + int(table.get("id_offset", 0)), f"{side} {name}".strip())
        return labels

    def _split_hemispheres(self, split):
        """Give the voxels right of the midline (x > 0) their own label values (value + offset) and R_ names.

        Some volumes number the areas of both hemispheres alike and tell them apart by the names only (HCP-MMP1).
        """
        offset = int(split.get("offset", 1000))
        left, right = split.get("left_prefix", "L_"), split.get("right_prefix", "R_")
        used = np.argwhere(self.data != 0)
        x = used @ self.affine[0, :3] + self.affine[0, 3]
        right_voxels = used[x > 0]
        values = np.unique(self.data[tuple(right_voxels.T)]) if len(right_voxels) else np.array([], dtype=np.int32)
        self.data[tuple(right_voxels.T)] += offset
        new_labels = {}
        for value, name in self.labels.items():
            base = name[len(left) :] if name.startswith(left) else name
            new_labels[value] = left + base
            if value in values:
                new_labels[value + offset] = right + base
        self.labels = new_labels
        self._counts = None


# Results


class LabelRow(NamedTuple):
    """One row of :func:`label_mask`: the atlas id, the label name, the percentage of the mask inside the label and
    the percentage of the label region covered by the mask.
    """

    atlas: str
    label: str
    pct_of_mask: float
    pct_of_region: float


class LabelResult(list):
    """A list of rows (:class:`LabelRow` or (atlas, label, network) tuples) with what the labelling decided.

    :ivar notices: dicts {"code", "atlas", "message"} for atlases that were skipped (family mismatch or unknown
      family) and for an empty mask.
    :ivar allowed_cross_family: dicts of the atlases used across families on request (recorded in provenance).
    :ivar details: for :func:`label_mask`, a dict per row with label_id, network, volume_mm3 and region_mm3 too.
    :ivar unlabelled: {atlas id: percentage of the mask outside every label of that atlas}.
    :ivar mask_family: family of the labelled mask ("unknown" when it could not be told).
    :ivar peak: for :func:`label_cluster` with ``peak``, the (atlas, label, network) rows of the peak coordinate.
    """

    def __init__(self, rows=()):
        super().__init__(rows)
        self.notices = []
        self.allowed_cross_family = []
        self.details = []
        self.unlabelled = {}
        self.mask_family = "unknown"
        self.mask_space = None
        self.peak = None

    @property
    def refused(self):
        """Ids of the atlases skipped because of their family."""
        return [notice["atlas"] for notice in self.notices if notice["code"] in ("family_mismatch", "family_unknown")]


def mask_family(shape=None, affine=None, space=None):
    """(family, space) of a mask: from the ``space`` label when given, else recognized from its grid ("unknown", None if neither)."""
    if space:
        return space_family(space), space
    if shape is not None and affine is not None:
        match = detect_grid_candidates(tuple(shape)[:3], np.asarray(affine, dtype=np.float64))
        if match:
            return match.family, match.candidates[0]
    return "unknown", None


def _usable(atlas, family, space, allow_cross_family, result):
    """Whether ``atlas`` may label something of ``family``; refusals and permissions are recorded on ``result``."""
    known = family not in ("unknown", "native") and atlas.family not in ("unknown", "native")
    if known and family == atlas.family:
        return True
    code = "family_mismatch" if known else "family_unknown"
    if known:
        reason = f"{atlas.title} is in the {atlas.family} space family, but the mask is in the {family} family."
    elif family in ("unknown", "native"):
        reason = f"The space of the mask could not be told, so it cannot be compared with {atlas.title} ({atlas.family} family). Say which template space it is in."
    else:
        reason = f"The space family of {atlas.title} is not known, so it cannot be compared with the {family} family of the mask."
    if allow_cross_family:
        result.allowed_cross_family.append({"atlas": atlas.id, "atlas_family": atlas.family, "mask_family": family, "mask_space": space, "reason": code})
        return True
    result.notices.append(
        {
            "code": code,
            "atlas": atlas.id,
            "message": reason + " Templates of different families differ by a few millimetres, so the label is not computed. Warp the mask to this family, or allow the comparison explicitly.",
        },
    )
    return False


def _as_atlases(atlases):
    return [atlases] if isinstance(atlases, Atlas) else list(atlases)


# Labelling


def label_points(xyz, atlases, space=None, allow_cross_family=False):
    """Labels of several world coordinates (N, 3): a list of :func:`label_point` results, one per point."""
    points = np.asarray(xyz, dtype=np.float64).reshape(-1, 3)
    results = [LabelResult() for _ in points]
    family, space_label = (space_family(space), space) if space else (None, None)
    for atlas in _as_atlases(atlases):
        if family is not None:
            gate = LabelResult()
            if not _usable(atlas, family, space_label, allow_cross_family, gate):
                for result in results:
                    result.notices.extend(gate.notices)
                continue
            for result in results:
                result.allowed_cross_family.extend(gate.allowed_cross_family)
        values = atlas.values_at(points)
        for result, value in zip(results, values, strict=True):
            result.append((atlas.id, atlas.label_name(value), atlas.network(value)))
            result.mask_family = family or "unknown"
    return results


def label_point(xyz, atlases, space=None, allow_cross_family=False):
    """Labels of one world coordinate (RAS mm): [(atlas id, label name, network)] for each atlas.

    The nearest voxel of each atlas is found through its affine. A point outside an atlas or on background
    gives (atlas id, None, None). With ``space`` (the template space of the coordinate) atlases of another family
    are skipped with a notice like in :func:`label_mask`; without it nothing is known about the coordinate and
    every atlas answers.
    """
    return label_points(np.asarray(xyz, dtype=np.float64).reshape(1, 3), atlases, space, allow_cross_family)[0]


def label_mask(occupancy, affine, atlases, allow_cross_family=False, space=None, top=None, min_pct=0.0):
    """Atlas labels overlapped by a mask: [(atlas id, label name, pct_of_mask, pct_of_region)].

    :param occupancy: 3D array [i, j, k] of 0/1 or fractional occupancy (a lesion mask or a thresholded map).
    :param affine: its 4x4 voxel-to-RAS matrix.
    :param atlases: an :class:`Atlas` or a list of them.
    :param allow_cross_family: label with atlases of another space family anyway (recorded in the result).
    :param space: template space of the mask; by default recognized from its grid.
    :param top: keep only the ``top`` largest rows of each atlas.
    :param min_pct: drop rows whose pct_of_mask is below this.
    :return: :class:`LabelResult`, rows sorted by atlas (in the order given) and then by decreasing pct_of_mask.
      ``pct_of_mask`` is the share of the mask volume inside the label (the rest is in ``result.unlabelled``),
      ``pct_of_region`` the share of the label's volume covered by the mask (a fractional mask counts by its
      fraction). The occupancy is resampled onto each atlas grid in fraction mode, so a single mask voxel
      survives a coarser atlas and volumes are conserved.
    """
    occ = np.nan_to_num(np.asarray(occupancy, dtype=np.float64))
    if occ.ndim == 4 and occ.shape[3] == 1:
        occ = occ[..., 0]
    if occ.ndim != 3:
        raise ValueError(f"label_mask expects a 3D occupancy array, got shape {occ.shape}")
    occ = np.clip(occ, 0.0, 1.0)  # 0/255 and multi-label masks count as occupied; fractions stay within 0..1
    affine = np.asarray(affine, dtype=np.float64)
    result = LabelResult()
    family, space_label = mask_family(occ.shape, affine, space)
    result.mask_family, result.mask_space = family, space_label
    total = float(occ.sum()) * abs(float(np.linalg.det(affine[:3, :3])))
    if total <= 0:
        result.notices.append({"code": "empty_mask", "atlas": None, "message": "The mask is empty: nothing to label."})
        return result
    for atlas in _as_atlases(atlases):
        if not _usable(atlas, family, space_label, allow_cross_family, result):
            continue
        rows = _label_atlas(occ, affine, total, atlas, top, min_pct, result)
        result.extend(rows)
    return result


def _label_atlas(occ, affine, total, atlas, top, min_pct, result):
    fraction = resample_to_grid(occ, affine, atlas.grid, mode="fraction")
    voxel = atlas.voxel_volume
    flat = fraction.reshape(-1)
    used = np.flatnonzero(flat)
    labels = atlas.data.reshape(-1)[used]
    weights = flat[used]
    inside = labels != 0
    values, inverse = np.unique(labels[inside], return_inverse=True)
    volumes = np.bincount(inverse, weights=weights[inside], minlength=len(values)) * voxel
    counts = atlas.region_counts()
    result.unlabelled[atlas.id] = max(0.0, 100.0 * (1.0 - float(volumes.sum()) / total))
    order = sorted(range(len(values)), key=lambda index: (-volumes[index], int(values[index])))
    rows = []
    for index in order:
        value = int(values[index])
        pct_mask = 100.0 * float(volumes[index]) / total
        if pct_mask < min_pct or volumes[index] <= 0:
            continue
        region = counts[value] * voxel
        name = atlas.label_name(value)
        row = LabelRow(atlas.id, name, pct_mask, 100.0 * float(volumes[index]) / region)
        rows.append(row)
        result.details.append(
            {
                "atlas": atlas.id,
                "atlas_title": atlas.title,
                "label_id": value,
                "label": name,
                "network": atlas.network(value),
                "pct_of_mask": row.pct_of_mask,
                "pct_of_region": row.pct_of_region,
                "volume_mm3": float(volumes[index]),
                "region_mm3": float(region),
            },
        )
        if top is not None and len(rows) >= top:
            break
    return rows


def label_cluster(mask_vec, grid, atlases, indices=None, allow_cross_family=False, space=None, peak=None, top=3, min_pct=0.0):
    """Atlas labels of a cluster of a connectome grid (the cluster table of L-19).

    :param mask_vec: the cluster as a flat vector of ``grid.n_voxels`` entries (C order), an array of ``grid.shape``,
      or, with ``indices``, a masked vector (see ``geometry.mask_indices``). Non-zero entries are the cluster.
    :param grid: the :class:`Grid` of the connectome (its family is recognized from it unless ``space`` is given).
    :param peak: world coordinate (RAS mm) of the cluster's peak: its labels go to ``result.peak``.
    :param top: rows kept per atlas (the largest overlaps).
    :return: :class:`LabelResult` as of :func:`label_mask`.
    """
    vector = np.asarray(mask_vec)
    if indices is not None:
        volume = from_masked_vector(vector, np.asarray(indices), grid.shape, fill=0, dtype=vector.dtype)
    elif vector.size == grid.n_voxels:
        volume = vector.reshape(grid.shape)
    else:
        raise ValueError(f"The cluster has {vector.size} entries but the grid {grid.shape} has {grid.n_voxels} voxels (pass indices for a masked vector)")
    occupancy = (np.nan_to_num(volume.astype(np.float64)) != 0).astype(np.float64)
    result = label_mask(occupancy, grid.affine, atlases, allow_cross_family=allow_cross_family, space=space, top=top, min_pct=min_pct)
    if peak is not None:  # only the atlases that labelled the cluster answer for its peak
        result.peak = label_point(peak, [atlas for atlas in _as_atlases(atlases) if atlas.id not in result.refused])
    return result


# Cached catalog atlases


def load_cached(cache=None, ids=None, family=None, catalog="atlases"):
    """Every cached, readable atlas of a catalog as an :class:`Atlas` list (optionally of some ids or one family).

    Atlases that are not cached are skipped: this never downloads, and is what the labelling pipeline calls.
    """
    atlases = []
    for resource in fetch.load_catalog(catalog):
        if ids is not None and resource.id not in ids:
            continue
        if family is not None and resource.get("family") != family:
            continue
        try:
            atlases.append(Atlas.load(resource, cache))
        except fetch.FetchError:
            continue
    return atlases


def cache_status(cache=None, catalog="atlases"):
    """[(entry, available)] of a catalog: which atlases (file and label table) are cached, so usable offline."""
    status = []
    for resource in fetch.load_catalog(catalog):
        labels = resource.label_resource()
        status.append((resource, fetch.is_cached(resource, cache) and (labels is None or fetch.is_cached(labels, cache))))
    return status


# Region names of peaks (the Peak region column of Map's Result card)

#: Atlases that name the region of a peak, in order of preference: anatomical names first (Harvard-Oxford subcortical,
#: then cortical), then the finer and the network atlases. Only cached atlases are used (nothing is downloaded).
PEAK_REGION_ATLASES = (
    "harvard-oxford-subcortical-th25-2mm",
    "harvard-oxford-cortical-th25-2mm",
    "mial67-thalamic-nuclei-1mm",
    "massp20-1mm",
    "aal3v1",
    "harvard-aan-v2",
    "hcp-mmp1-volumetric",
    "tian-s1-2mm",
    "tian-s1-2mm-2009c",
    "schaefer2018-400-7n-2mm",
    "schaefer2018-400-7n-2mm-2009c",
    "yeo2011-7n",
)
#: The region of a peak that no usable atlas labels
OUTSIDE_ATLAS = "Outside atlas"
#: Labels that say nothing about where a peak is: the next atlas names it
_GENERIC_REGION = re.compile(r"(?i)^(left |right )?(cerebral (white matter|cortex)|lateral ventric(le|al)|brain-?stem)$")
_SIDE_PREFIX = re.compile(r"(?i)^(left|right|lh|rh|l|r)[ _-]+")
_SIDE_SUFFIX = re.compile(r"(?i)[ _-]+(left|right|lh|rh|l|r)$")
#: {(cache folder, atlas id): Atlas} of the atlases loaded for peak regions (an atlas file is read once)
_region_atlases = {}


def region_atlases(cache=None):
    """The cached atlases of :data:`PEAK_REGION_ATLASES`, in that order (read once per cache folder; never downloads)."""
    folder = fetch.cache_dir(cache)
    found = []
    for atlas_id in PEAK_REGION_ATLASES:
        key = (folder, atlas_id)
        atlas = _region_atlases.get(key)
        if atlas is None:
            try:
                resource = fetch.find_resource(atlas_id, ("atlases",))
            except Exception:
                continue
            if not fetch.is_cached(resource, folder):
                continue
            try:
                atlas = Atlas.load(resource, folder)
            except Exception:
                continue
            _region_atlases[key] = atlas
        found.append(atlas)
    return found


def region_text(label, x=None):
    """A short region name for a table: "Left Caudate" -> "L caudate", "Angular Gyrus" at x < 0 -> "L angular gyrus".

    The side comes from the label when it has one, else from ``x`` (the RAS x of the point: negative is left). Words in
    title case are lowered ("Crus I" keeps its numeral).
    """
    raw = str(label or "").strip()
    schaefer = re.match(r"^(?:\d+Networks_)?(LH|RH)_(.+)$", raw)
    if schaefer:
        # "7Networks_LH_Vis_1" -> "L Vis 1": the network and the parcel
        return f"{'L' if schaefer.group(1) == 'LH' else 'R'} {schaefer.group(2).replace('_', ' ')}"
    text = raw.replace("_", " ").strip()
    side = None
    match = _SIDE_PREFIX.match(text) or _SIDE_SUFFIX.search(text)
    if match:
        side = "L" if match.group(1).lower() in ("left", "lh", "l") else "R"
        text = (text[match.end():] if match.start() == 0 else text[:match.start()]).strip()
    elif x is not None and abs(float(x)) >= 1.0:
        side = "L" if float(x) < 0 else "R"
    words = [word.lower() if len(word) > 1 and word[0].isupper() and word[1:].islower() else word for word in text.split()]
    text = " ".join(words)
    if side and text:
        return f"{side} {text}"
    return text[:1].upper() + text[1:]


def peak_regions(points, space=None, cache=None, atlases=None):
    """Region names of peaks (world RAS mm, (N, 3)) for the Peak region column.

    Each point gets the label of the first usable atlas (in :data:`PEAK_REGION_ATLASES` order) that names a region there
    other than a generic one (cerebral cortex or white matter), as :func:`region_text`; :data:`OUTSIDE_ATLAS` when the
    usable atlases name nothing there. ``space`` (the template space of the map) skips the atlases of another space family.

    :return: a list of texts, one per point, or None when no usable atlas is cached (nothing can be said).
    """
    points = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    candidates = region_atlases(cache) if atlases is None else _as_atlases(atlases)
    if space:
        family = space_family(space)
        candidates = [atlas for atlas in candidates if family in ("unknown", "native") or atlas.family in ("unknown", "native") or atlas.family == family]
    if not candidates:
        return None
    names = [None] * len(points)
    for atlas in candidates:
        missing = [index for index, name in enumerate(names) if name is None]
        if not missing:
            break
        values = atlas.values_at(points[missing])
        for index, value in zip(missing, values, strict=True):
            label = atlas.label_name(value)
            if label and not _GENERIC_REGION.match(str(label).replace("_", " ").strip()):
                names[index] = region_text(label, points[index][0])
    return [name or OUTSIDE_ATLAS for name in names]

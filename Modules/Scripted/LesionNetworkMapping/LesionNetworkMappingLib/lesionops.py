"""Per-lesion intake checks and recorded lesion operations (addendum §3.10; lesion-input research §1, §4).

Fast, deterministic checks that catch the classic intake errors (radiological L/R flips, tracings that
include the ventricles, the same case reported twice, non-binary warped masks, wrong grids) and the
lesion operations whose semantics must be recorded:

- :func:`detect_dimensionality`: 2D (one slice along an axis or a figure plane), multi2D (several
  single-slice drawings on different levels or planes) or 3D, and the number of slices per plane;
- :func:`extrude`: thickens a 2D tracing to an exact total thickness centred on the drawn plane,
  either fractional (edge slices weighted; written as ``desc-lesionProb`` and mapped with the engine's
  ``--probabilistic``) or binary (± a whole number of voxels); the returned record states which;
- :func:`mirror_world`: world x -> -x, labelled **approximate** (:data:`MIRROR_NOTE`);
- :func:`hygiene`: values outside {0, 1}, empty masks, lesions smaller than one connectome voxel,
  % outside the brain, sform/qform disagreement, a grid that is not the study template, several parts;
- :func:`side_check`: traced hemisphere (>= 80 % rule) against the reported side;
- :func:`csf_share`: % of the lesion in CSF or the ventricles, refused across template families;
- :class:`LesionIndex`: Dice-based duplicate candidates on a 2 mm grid (``.studio/cache/lesion_index.npz``);
- :func:`qc_flags`: the ``qc_flags`` column codes of these results.

Volumes are numpy arrays indexed [i, j, k] in file order with a 4x4 voxel-to-RAS affine (see nifti.py);
voxel axes are mapped to anatomical axes through the affine, never by array index, so radiological
(left-handed) files are handled. Everything is numpy/scipy only, without Slicer, VTK or Qt, and keeps no
global state, so the functions are safe to call from worker threads.
"""

import math
import os
import threading
import zipfile
from typing import NamedTuple

import numpy as np

from . import geometry, nifti

# Anatomical planes, indexed by the world (RAS) axis they are perpendicular to
PLANES = ("axial", "coronal", "sagittal")
PLANE_WORLD_AXIS = {"sagittal": 0, "coronal": 1, "axial": 2}

DIM_2D = "2D"
DIM_MULTI2D = "multi2D"
DIM_3D = "3D"

EXTRUDE_FRACTIONAL = "fractional"
EXTRUDE_BINARY_PM = "binary_pm"
EXTRUDE_MODES = (EXTRUDE_FRACTIONAL, EXTRUDE_BINARY_PM)
EXTRUDE_SEMANTICS = "total, centred on drawn plane"
DEFAULT_EXTRUDE_MM = 2.0
OBLIQUE_SUBSAMPLES = 4  # sub-samples per axis per voxel when extruding along an oblique normal

LESION_THRESHOLD = 0.5  # fraction at which a voxel counts as lesion (parts, duplicate index)
SIDE_MAJORITY = 0.8  # share of the lesion on one side for a one-sided lesion
MIDLINE_MM = 2.0  # |x| of the centroid below which a lesion with no majority side may be "midline"
MIDLINE_BAND_MM = 10.0  # a "midline" lesion has at least half of its volume within this |x| of the midline
CSF_THRESHOLD = 0.5  # CSF probability above which a voxel counts as CSF
CSF_PCT_MAX = 20.0  # % of the lesion in CSF above which the tracing probably includes sulci or ventricles
OUTSIDE_PCT_MAX = 5.0  # % of the lesion outside the brain mask (as normqc.THRESHOLDS)
XFORM_MM_MAX = nifti.XFORM_WARNING_MM
DUPLICATE_DICE = 0.6  # pairwise Dice (2 mm grid) above which two lesions may be the same case
INDEX_GRID_ID = "NLin6-2mm"

MIRROR_NOTE = (
    "Mirror L-R is approximate: it maps world x to -x, taking the plane x = 0 as the midsagittal plane, "
    "and templates are asymmetric, so the mirrored lesion does not sit on exactly homologous anatomy. "
    "For a figure traced on the wrong side, retracing on the correct side is recommended."
)

# qc_flags codes (study column ``qc_flags``, ';'-joined) in reporting order, with their explanation
QC_FLAGS = {
    "side_mismatch": "The traced hemisphere is the opposite of the reported side (radiological figure?)",
    "csf": f"More than {CSF_PCT_MAX:g}% of the lesion is in CSF or the ventricles",
    "nonbinary": "The lesion has values other than 0 and 1 (threshold at 0.5?)",
    "empty": "The lesion is empty",
    "subvoxel": "The lesion is smaller than one connectome voxel",
    "outside": f"More than {OUTSIDE_PCT_MAX:g}% of the lesion is outside the brain",
    "xform": f"The sform and qform of the file disagree by more than {XFORM_MM_MAX:g} mm",
    "grid": "The lesion grid is not the study template grid",
    "multipart": "The lesion has several separate parts but is not marked multifocal",
    "duplicate": f"The lesion overlaps another lesion of the study with Dice >= {DUPLICATE_DICE:g} (same case reported twice?)",
}


# Helpers


def _values(mask):
    """Lesion values as a float64 copy (booleans become 0/1; non-finite values become 0)."""
    array = np.asarray(mask)
    values = array.astype(np.float64)
    if not (array.dtype == bool or np.issubdtype(array.dtype, np.integer)):
        values[~np.isfinite(values)] = 0.0
    return values


def _fraction(mask):
    """Fractional lesion (0..1, float64 copy)."""
    values = _values(mask)
    if np.asarray(mask).dtype != bool:
        np.clip(values, 0.0, 1.0, out=values)
    return values


def _lesion_voxels(mask, threshold=LESION_THRESHOLD):
    """Boolean mask of the voxels counted as lesion: >= threshold, or > 0 when no voxel reaches it."""
    values = _values(mask)
    occupied = values >= threshold
    if not occupied.any():
        occupied = values > 0
    return occupied


def _support(array):
    """Sorted flat C-order indices of the non-zero (or NaN) elements of an array.

    Scans the bytes 8 at a time, then checks only the elements of the non-zero words: several times
    faster than np.flatnonzero on uint8 lesion images, where most of the volume is zero.
    """
    flat = np.ascontiguousarray(array).reshape(-1)
    itemsize = flat.dtype.itemsize
    if itemsize not in (1, 2, 4, 8):
        return np.flatnonzero(flat)
    per_word = 8 // itemsize
    bulk = flat.size - flat.size % per_word
    words = np.flatnonzero(flat[:bulk].view(np.uint64))
    candidates = (words[:, np.newaxis] * per_word + np.arange(per_word)).reshape(-1)
    found = candidates[flat[candidates] != 0]
    tail = np.flatnonzero(flat[bulk:]) + bulk
    return np.concatenate([found, tail]) if tail.size else found


def _as_grid(spec):
    """A :class:`geometry.Grid` from a Grid, a GridSpec, a registry grid id, a (shape, affine) pair,
    a mapping or object with ``shape`` and ``affine``, or a NIfTI path (header only).
    """
    if spec is None:
        return None
    if isinstance(spec, geometry.Grid):
        return spec
    if isinstance(spec, str) and spec in geometry.GRID_BY_ID:
        return geometry.GRID_BY_ID[spec].grid
    if isinstance(spec, geometry.GridSpec):
        return spec.grid
    shape, affine = geometry._shape_and_affine(spec)
    return geometry.Grid(shape, affine)


def _unit(vector):
    vector = np.asarray(vector, dtype=np.float64).reshape(3)
    norm = float(np.linalg.norm(vector))
    if not np.isfinite(norm) or norm == 0:
        raise ValueError(f"Invalid plane normal: {vector.tolist()}")
    return vector / norm


def _aligned_voxel_axis(affine, normal, tolerance=1e-6):
    """Voxel axis whose direction is the normal (either sign), or None for an oblique normal."""
    columns = np.asarray(affine, dtype=np.float64)[:3, :3]
    directions = columns / np.linalg.norm(columns, axis=0)
    cosines = np.abs(directions.T @ normal)
    axis = int(np.argmax(cosines))
    return axis if cosines[axis] >= 1.0 - tolerance else None


def _layer_step(affine, normal):
    """Extent of one voxel along the normal (mm): the spacing for a normal along a voxel axis."""
    return float(np.abs(np.asarray(affine, dtype=np.float64)[:3, :3].T @ normal).sum())


def _count_levels(ijk, affine, normal):
    """Number of voxel layers along a normal occupied by the voxels ``ijk`` (N, 3)."""
    if len(ijk) == 0:
        return 0
    axis = _aligned_voxel_axis(affine, normal)
    if axis is not None:
        return int(np.unique(ijk[:, axis]).size)
    projection = geometry.voxel_to_world(ijk, affine) @ normal
    spread = float(projection.max() - projection.min())
    return math.floor(spread / _layer_step(affine, normal) + 1e-6) + 1


def _plane_normal(plane):
    normal = np.zeros(3)
    normal[PLANE_WORLD_AXIS[plane]] = 1.0
    return normal


def _plane_of_normal(normal, tolerance=1e-6):
    """Anatomical plane perpendicular to a world normal, or "oblique"."""
    axis = int(np.argmax(np.abs(normal)))
    if abs(normal[axis]) < 1.0 - tolerance:
        return "oblique"
    return next(plane for plane, world_axis in PLANE_WORLD_AXIS.items() if world_axis == axis)


def _components(occupied):
    """26-connected components of a boolean volume: list of (N, 3) voxel index arrays, largest first."""
    from scipy import ndimage

    found = np.argwhere(occupied)
    if len(found) == 0:
        return []
    lo, hi = found.min(axis=0), found.max(axis=0) + 1
    labels, count = ndimage.label(occupied[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]], structure=np.ones((3, 3, 3), dtype=bool))
    local = np.argwhere(labels)
    order = np.argsort(labels[tuple(local.T)], kind="stable")
    local = local[order]
    sizes = np.bincount(labels[tuple(local.T)], minlength=count + 1)[1:]
    parts = np.split(local + lo, np.cumsum(sizes)[:-1])
    return sorted(parts, key=lambda part: (-len(part), tuple(part[0])))


def _single_planes(ijk, affine, plane_normal=None):
    """Planes (or ["normal"]) along which the voxels occupy at most one layer."""
    if plane_normal is not None:
        return ["normal"] if _count_levels(ijk, affine, plane_normal) <= 1 else []
    return [plane for plane in PLANES if _count_levels(ijk, affine, _plane_normal(plane)) <= 1]


# Dimensionality


def detect_dimensionality(mask, affine, plane_normal=None, threshold=0.0):
    """Dimensionality of a traced lesion.

    - "2D": at most one occupied slice along an anatomical axis (or along ``plane_normal``);
    - "multi2D": every 26-connected component is single-slice, but on different levels or planes;
    - "3D": anything else.

    Voxel axes are mapped to the anatomical axes through the affine; slices along an anatomical axis
    that no voxel axis follows (oblique grids) and along ``plane_normal`` (figure tracing on a tilted
    plane, a world RAS vector) are counted by projecting the voxel centres onto the normal, one layer
    being the extent of a voxel along it.

    :param threshold: voxels with a value above it are occupied (default: any non-zero value).
    :return: {"dimensionality": "2D" | "multi2D" | "3D" | None (empty), "planes": drawn planes (2D and
      multi2D; "oblique" for a tilted ``plane_normal``), "n_slices": {"axial", "coronal", "sagittal":
      slices per plane}, "drawn_axis": plane of a 2D lesion (None otherwise), "n_components": number of
      26-connected components}
    """
    affine = np.asarray(affine, dtype=np.float64)
    occupied = _values(mask) > threshold
    ijk = np.argwhere(occupied)
    n_slices = {plane: _count_levels(ijk, affine, _plane_normal(plane)) for plane in PLANES}
    result = {"dimensionality": None, "planes": [], "n_slices": n_slices, "drawn_axis": None, "n_components": 0}
    if len(ijk) == 0:
        return result
    normal = None if plane_normal is None else _unit(plane_normal)
    normal_plane = None if normal is None else _plane_of_normal(normal)
    if normal is not None:
        result["n_slices_normal"] = _count_levels(ijk, affine, normal)
    components = _components(occupied)
    result["n_components"] = len(components)

    single = _single_planes(ijk, affine, normal)
    if single:
        plane = normal_plane if normal is not None else single[0]
        result.update(dimensionality=DIM_2D, planes=[plane], drawn_axis=plane)
        return result

    # multi2D: every component is single-slice; each takes a plane already used by a larger one if it can
    planes = []
    for part in components:
        candidates = _single_planes(part, affine, normal)
        if not candidates:
            result["dimensionality"] = DIM_3D
            return result
        candidates = [normal_plane] if normal is not None else candidates
        plane = next((p for p in planes if p in candidates), candidates[0])
        if plane not in planes:
            planes.append(plane)
    result.update(dimensionality=DIM_MULTI2D, planes=planes)
    return result


# Extrusion


def _round_half_down(value):
    """Nearest integer, halves rounded down (0.5 -> 0, 1.5 -> 1)."""
    return math.ceil(value - 0.5 - 1e-9)


def _slab_weights(total_mm, spacing, mode):
    """{slice offset: weight} of a slab of total thickness ``total_mm`` centred on the drawn slice.

    fractional: weight = the overlap of the slice with the slab / spacing, so sum(weights) * spacing ==
    total_mm exactly; binary_pm: weight 1 for offsets -n..n, n = round((total / 2) / spacing) (halves
    rounded down, so 2 mm at 2 mm keeps the drawn slice only).
    """
    half = total_mm / 2.0
    if mode == EXTRUDE_BINARY_PM:
        n = _round_half_down(half / spacing)
        return dict.fromkeys(range(-n, n + 1), 1.0)
    n = max(0, math.ceil((half - spacing / 2.0) / spacing - 1e-9))
    weights = {}
    for offset in range(-n, n + 1):
        low = max(offset * spacing - spacing / 2.0, -half)
        high = min(offset * spacing + spacing / 2.0, half)
        if high > low:
            weights[offset] = min(1.0, (high - low) / spacing)
    return weights


def _extrude_axis(values, part, axis, weights, out):
    """Copy the drawn slice of one component to the slab slices along a voxel axis; True if clipped."""
    level = int(part[0, axis])
    drawn = np.zeros(tuple(np.delete(values.shape, axis)), dtype=np.float64)
    other = [a for a in range(3) if a != axis]
    drawn[part[:, other[0]], part[:, other[1]]] = values[tuple(part.T)]
    clipped = False
    for offset, weight in weights.items():
        target = level + offset
        if not 0 <= target < values.shape[axis]:
            clipped = True
            continue
        index = [slice(None)] * 3
        index[axis] = target
        np.maximum(out[tuple(index)], weight * drawn, out=out[tuple(index)])
    return clipped


def _extrude_oblique(values, part, affine, normal, half, out, subsamples=OBLIQUE_SUBSAMPLES, batch=2_000_000):
    """Supersampled slab along an oblique normal; True if clipped by the field of view.

    Each output voxel gets the mean, over ``subsamples``^3 points, of the drawn value at the point's
    foot on the drawn plane (looked up at offsets along the normal, in steps of half the smallest
    spacing, through the drawn layer) for the points within ``half`` mm of the drawn plane.
    """
    shape = np.array(values.shape)
    spacing = np.sqrt((affine[:3, :3] ** 2).sum(axis=0))
    centres = geometry.voxel_to_world(part, affine)
    projection = centres @ normal
    level = float(projection.mean())
    layer_half = (projection.max() - projection.min()) / 2.0 + _layer_step(affine, normal) / 2.0
    step = spacing.min() / 2.0
    lookups = np.arange(-layer_half, layer_half + 1e-9, step)
    lookups = lookups - (lookups[0] + lookups[-1]) / 2.0  # symmetric about the plane
    drawn = np.zeros(values.shape, dtype=np.float64)
    drawn[tuple(part.T)] = values[tuple(part.T)]

    reach = np.ceil((half + layer_half) / spacing).astype(np.int64) + 1
    lo = part.min(axis=0) - reach
    hi = part.max(axis=0) + reach + 1
    clipped = bool(np.any(lo < 0) or np.any(hi > shape))
    lo, hi = np.maximum(lo, 0), np.minimum(hi, shape)
    box_shape = tuple(int(n) for n in hi - lo)
    offsets = np.stack(np.meshgrid(*[(np.arange(subsamples) + 0.5) / subsamples - 0.5] * 3, indexing="ij"), axis=-1).reshape(-1, 3)
    inverse = np.linalg.inv(affine)
    box = np.zeros(int(np.prod(box_shape)), dtype=np.float64)
    chunk = max(1, batch // (len(offsets) * len(lookups)))
    for start in range(0, box.size, chunk):
        stop = min(box.size, start + chunk)
        ijk = np.stack(np.unravel_index(np.arange(start, stop), box_shape), axis=1) + lo
        points = geometry.voxel_to_world((ijk[:, np.newaxis, :] + offsets[np.newaxis]).reshape(-1, 3), affine)
        distance = points @ normal - level
        inside = np.abs(distance) <= half + 1e-9
        feet = points[inside] - distance[inside, np.newaxis] * normal
        sampled = np.zeros(len(feet))
        for shift in lookups:
            index = np.floor(geometry.voxel_to_world(feet + shift * normal, inverse) + 0.5).astype(np.int64)
            valid = np.all((index >= 0) & (index < shape), axis=1)
            found = np.zeros(len(feet))
            found[valid] = drawn[tuple(index[valid].T)]
            np.maximum(sampled, found, out=sampled)
        samples = np.zeros(len(points))
        samples[inside] = sampled
        box[start:stop] = samples.reshape(stop - start, len(offsets)).mean(axis=1)
    region = tuple(slice(a, b) for a, b in zip(lo, hi, strict=True))
    np.maximum(out[region], box.reshape(box_shape), out=out[region])
    return clipped


def extrude(mask, affine, total_mm=DEFAULT_EXTRUDE_MM, normal=None, mode=EXTRUDE_FRACTIONAL, threshold=0.0):
    """Extrude a 2D (or multi2D) tracing to a slab of total thickness ``total_mm`` centred on the drawn plane.

    - "fractional": the slab is exactly ``total_mm`` thick; slices only partly inside it get the
      fraction inside as their weight (2 mm at 1 mm: 0.5, 1, 0.5), so the summed thickness times the
      drawn area is exact. Written as ``desc-lesionProb`` and mapped with the engine's ``--probabilistic``.
    - "binary_pm": ± round((total / 2) / spacing) whole slices (2 mm at 1 mm: 3 voxels, ± 1 mm).

    Each component is extruded through its own drawn plane (multi2D lesions), along ``normal`` when it
    is given (a world RAS vector, figure tracing on a tilted plane). A normal along a voxel axis shifts
    whole slices; an oblique normal offsets the drawn plane along the normal in steps of half a voxel and
    rasterises the slab by supersampling (approximate, recorded). Drawn values are kept (multiplied by
    the slice weights); overlapping slabs take the maximum.

    :return: (array, record). The array is float64 (0..1) on the input grid; record = {"mode",
      "total_mm", "semantics": "total, centred on drawn plane", "voxels_per_side", "spacing_mm",
      "effective_mm", "description", "planes", "normal", "oblique", "clipped", "probabilistic",
      "output_desc"}. ``effective_mm`` is the thickness produced (``total_mm`` for fractional,
      (2 n + 1) * spacing for binary_pm); ``clipped`` is True when the field of view cut the slab.
    :raises ValueError: empty or 3D lesion, unknown mode, invalid thickness.
    """
    if mode not in EXTRUDE_MODES:
        raise ValueError(f"Unknown extrusion mode: {mode} (use one of {', '.join(EXTRUDE_MODES)})")
    total_mm = float(total_mm)
    if not np.isfinite(total_mm) or total_mm <= 0:
        raise ValueError(f"The extrusion thickness must be positive, got {total_mm}")
    affine = np.asarray(affine, dtype=np.float64)
    values = _fraction(mask)
    values[_values(mask) <= threshold] = 0.0
    unit_normal = None if normal is None else _unit(normal)
    info = detect_dimensionality(values, affine, unit_normal, threshold=0.0)
    if info["dimensionality"] is None:
        raise ValueError("The lesion is empty: nothing to extrude")
    if info["dimensionality"] == DIM_3D:
        raise ValueError("The lesion is 3D: only single-slice (2D or multi2D) tracings are extruded")

    occupied = values > 0
    parts = [np.argwhere(occupied)] if info["dimensionality"] == DIM_2D else _components(occupied)
    out = np.zeros(values.shape, dtype=np.float64)
    spacing = np.sqrt((affine[:3, :3] ** 2).sum(axis=0))
    clipped = oblique = False
    per_side, spacings, edge_weights = set(), set(), set()
    for part in parts:
        if unit_normal is not None:
            direction = unit_normal
        else:
            plane = info["drawn_axis"] or next(p for p in info["planes"] if p in _single_planes(part, affine))
            direction = _plane_normal(plane)
        axis = _aligned_voxel_axis(affine, direction)
        step = float(spacing[axis]) if axis is not None else float(spacing.min())
        weights = _slab_weights(total_mm, step, mode)
        per_side.add(max(weights))
        spacings.add(round(step, 6))
        edge_weights.add(round(weights[max(weights)], 6))
        if axis is not None:
            clipped |= _extrude_axis(values, part, axis, weights, out)
        else:
            oblique = True
            half = total_mm / 2.0 if mode == EXTRUDE_FRACTIONAL else (max(weights) + 0.5) * step
            clipped |= _extrude_oblique(values, part, affine, direction, half, out)
    if oblique and mode == EXTRUDE_BINARY_PM:
        out = (out >= 0.5).astype(np.float64)

    n = max(per_side)
    step = min(spacings)
    if mode == EXTRUDE_FRACTIONAL:
        effective = total_mm
        edge = min(edge_weights)
        description = f"fractional, exact total thickness {total_mm:g} mm centred on the drawn plane ({2 * n + 1} slices at {step:g} mm, edge weight {edge:g})"
    else:
        effective = (2 * n + 1) * step
        description = f"binary ±{n * step:g} mm ({2 * n + 1} voxel{'s' if n else ''} at {step:g} mm)"
    if oblique:
        description += f"; oblique normal, supersampled {OBLIQUE_SUBSAMPLES}^3 per voxel (approximate)"
    record = {
        "mode": mode,
        "total_mm": total_mm,
        "semantics": EXTRUDE_SEMANTICS,
        "voxels_per_side": int(n),
        "spacing_mm": step if len(spacings) == 1 else sorted(spacings),
        "effective_mm": effective,
        "description": description,
        "planes": list(info["planes"]),
        "dimensionality": info["dimensionality"],
        "normal": None if unit_normal is None else [float(v) for v in unit_normal],
        "oblique": oblique,
        "clipped": clipped,
        "probabilistic": mode == EXTRUDE_FRACTIONAL,
        "output_desc": "lesionProb" if mode == EXTRUDE_FRACTIONAL else "lesion",
    }
    return out, record


# Mirror


def mirror_world(mask, affine):
    """Mirror a lesion left-right in world coordinates (x -> -x) onto its own grid.

    The mirrored image is resampled with geometry.resample_to_grid ("fraction" mode, exact voxel
    overlaps), so fractional values are kept. This is **approximate** (:data:`MIRROR_NOTE`): the
    midsagittal plane is taken as x = 0, templates are asymmetric, and on grids whose voxel centres are
    not symmetric about x = 0 (the 2009c 2 mm grid lands half a voxel off) a binary lesion becomes
    fractional.

    :return: (fractional float64 array, binary uint8 array (fraction >= 0.5), note). The note is
      :data:`MIRROR_NOTE` plus the voxel offset on grids that are not symmetric about x = 0.
    """
    affine = np.asarray(affine, dtype=np.float64)
    values = _fraction(mask)
    flip = np.diag([-1.0, 1.0, 1.0, 1.0])
    grid = geometry.Grid(values.shape, affine)
    fractional = geometry.resample_to_grid(values, flip @ affine, grid, mode="fraction")
    binary = (fractional >= LESION_THRESHOLD).astype(np.uint8)
    note = MIRROR_NOTE
    mapping = np.linalg.inv(affine) @ flip @ affine  # voxel -> voxel of the mirrored centre
    spacing = np.sqrt((affine[:3, :3] ** 2).sum(axis=0))
    shift = np.abs(mapping[:3, 3] - np.round(mapping[:3, 3])) * spacing
    off = mapping[:3, :3] - np.round(mapping[:3, :3])
    if np.abs(off).max() > 1e-6:
        note += " The grid is oblique, so mirrored voxel centres do not land on voxel centres."
    elif shift.max() > 1e-6:
        voxels = float((shift / spacing).max())
        note += f" On this grid mirrored voxel centres land {shift.max():g} mm ({voxels:g} voxel) off the voxel centres, so the mirrored lesion is fractional and its binary version (>= 0.5) is not volume-preserving."
    lost = float(values.sum() - fractional.sum())
    if lost > 1e-6 * max(1.0, float(values.sum())):
        note += f" {lost:g} voxels of lesion fell outside the field of view."
    return fractional, binary, note


# Hygiene


def _fraction_on(values, affine, grid):
    """Fractional lesion resampled onto a grid (the same array when the grids match)."""
    if geometry.Grid(values.shape, affine).matches(grid):
        return values
    found = np.argwhere(values > 0)
    if len(found) == 0:
        return np.zeros(grid.shape)
    lo, hi = found.min(axis=0), found.max(axis=0) + 1
    shift = np.eye(4)
    shift[:3, 3] = lo
    crop = values[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]]
    return geometry.resample_to_grid(crop, np.asarray(affine, dtype=np.float64) @ shift, grid, mode="fraction")


def _image_and_affine(image, default_affine):
    """(array, affine) from an array on the lesion grid or an (array, affine[, ...]) tuple."""
    if isinstance(image, (tuple, list)):
        return np.asarray(image[0]), np.asarray(image[1], dtype=np.float64)
    return np.asarray(image), np.asarray(default_affine, dtype=np.float64)


def hygiene(mask, affine, header=None, template_grid=None, connectome_grid=None, brain_mask=None, multifocal=False):
    """Mask hygiene checks of one lesion.

    :param header: NIfTI header dictionary of the lesion file (nifti.read_nifti_header) for the
      sform/qform check; skipped when None.
    :param template_grid: the study template grid (a geometry.Grid, a registry grid id such as
      "NLin6-1mm", a (shape, affine) pair, ...); skipped when None.
    :param connectome_grid: the connectome grid, for the "smaller than one connectome voxel" check.
    :param brain_mask: brain mask on the lesion grid, or an (array, affine) pair on another grid
      (values >= 0.5 are brain).
    :param multifocal: the study row marks the lesion multifocal (several parts are then expected).
    :return: dict with a boolean per qc flag ("nonbinary", "empty", "subvoxel", "outside", "xform",
      "grid", "multipart"), the measured values ("n_nonbinary", "value_range", "volume_ml", "n_parts",
      "connectome_sum", "outside_pct", "xform_mm", "grid_matches", "left_handed") and "messages", a
      list of {"code", "level" ("amber" or "info"), "text"}. Values that were not checked are None.
      A lesion on the template grid stored in another voxel order (e.g. RAS against FSL's LAS) matches
      the template ("grid_order" info message, no "grid" flag).
    """
    affine = np.asarray(affine, dtype=np.float64)
    raw = np.asarray(mask)
    if raw.ndim != 3:
        raise ValueError(f"A lesion must be a 3D array, got shape {raw.shape}")
    as_float = np.asarray(raw, dtype=np.float64)
    finite = np.isfinite(as_float)
    n_nonbinary = int(np.count_nonzero(~finite | ((as_float != 0) & (as_float != 1))))
    values = _fraction(raw)
    voxel_ml = abs(float(np.linalg.det(affine[:3, :3]))) / 1000.0
    result = {
        "nonbinary": n_nonbinary > 0,
        "n_nonbinary": n_nonbinary,
        "value_range": [float(as_float[finite].min()), float(as_float[finite].max())] if finite.any() else None,
        "empty": not bool((values > 0).any()),
        "volume_ml": float(values.sum()) * voxel_ml,
        "n_parts": 0,
        "multipart": False,
        "connectome_sum": None,
        "subvoxel": False,
        "outside_pct": None,
        "outside": False,
        "xform_mm": None,
        "xform": False,
        "grid_matches": None,
        "grid": False,
        "left_handed": bool(np.linalg.det(affine[:3, :3]) < 0),
        "messages": [],
    }
    messages = result["messages"]

    def add(code, text, level="amber"):
        messages.append({"code": code, "level": level, "text": text})

    if result["nonbinary"]:
        add("nonbinary", f"{n_nonbinary} voxels have values other than 0 and 1 (range {result['value_range']}): threshold at 0.5 for a binary lesion")
    if result["empty"]:
        add("empty", "The lesion is empty")
    else:
        result["n_parts"] = len(_components(_lesion_voxels(values)))
        result["multipart"] = result["n_parts"] > 1 and not multifocal
        if result["multipart"]:
            add("multipart", f"The lesion has {result['n_parts']} separate parts (26-connected) but is not marked multifocal")
        grid = _as_grid(connectome_grid)
        if grid is not None:
            result["connectome_sum"] = float(_fraction_on(values, affine, grid).sum())
            result["subvoxel"] = result["connectome_sum"] < 1.0
            if result["subvoxel"]:
                add("subvoxel", f"The lesion fills {result['connectome_sum']:.3g} connectome voxels (less than one)")
        if brain_mask is not None:
            brain, brain_affine = _image_and_affine(brain_mask, affine)
            on_brain = _fraction_on(values, affine, geometry.Grid(brain.shape, brain_affine))
            total = float(on_brain.sum())
            if total > 0:
                result["outside_pct"] = 100.0 * float(on_brain[~(_values(brain) >= 0.5)].sum()) / total
            else:
                result["outside_pct"] = 100.0  # the lesion lies outside the brain mask's field of view
            result["outside"] = result["outside_pct"] > OUTSIDE_PCT_MAX
            if result["outside"]:
                add("outside", f"{result['outside_pct']:.1f}% of the lesion is outside the brain")
    if header is not None:
        result["xform_mm"] = nifti.xform_disagreement(header)
        result["xform"] = result["xform_mm"] > XFORM_MM_MAX
        if result["xform"]:
            add("xform", f"The sform and qform disagree by {result['xform_mm']:.2f} mm: programs may place the lesion differently")
    if result["left_handed"]:
        add("left_handed", "The file is stored with a left-handed (radiological) voxel order; it is read through its affine", level="info")
    template = _as_grid(template_grid)
    if template is not None:
        lesion_grid = geometry.Grid(raw.shape, affine)
        exact = lesion_grid.matches(template)
        reordered = not exact and _same_voxels(lesion_grid, template)
        result["grid_matches"] = exact or reordered
        result["grid"] = not result["grid_matches"]
        if reordered:
            add("grid_order", "The lesion is on the study template grid, stored in a different voxel order than the template; it is read through its affine", level="info")
        elif result["grid"]:
            add("grid", f"The lesion grid ({_grid_text(lesion_grid)}) is not the study template grid ({_grid_text(template)})")
    return result


def _same_voxels(grid, other):
    """True when two grids hold the same voxel centres and extent, in any storage order."""
    permutation = geometry.signed_permutation(grid.affine, other.affine)
    if permutation is None:
        return False
    for axis in range(3):
        target = permutation.axes[axis]
        n = grid.shape[axis]
        first = permutation.offsets[target]
        last = first + permutation.signs[axis] * (n - 1)
        if n != other.shape[target] or min(first, last) != 0 or max(first, last) != n - 1:
            return False
    return True


def _grid_text(grid):
    """Shape, voxel size and world position of a grid's first voxel, for messages."""
    origin = np.round(grid.affine[:3, 3], 2).tolist()
    return f"shape {grid.shape}, voxel size {np.round(grid.spacing, 3).tolist()} mm, voxel (0, 0, 0) at {origin} mm, axes {''.join(_axis_codes(grid.affine))}"


def _axis_codes(affine):
    """Anatomical direction (R/L/A/P/S/I) of each voxel axis."""
    codes = []
    for axis in range(3):
        column = np.asarray(affine, dtype=np.float64)[:3, axis]
        world = int(np.argmax(np.abs(column)))
        codes.append(("RL", "AP", "SI")[world][0 if column[world] > 0 else 1])
    return codes


# Side check


def normalize_side(side):
    """"L", "R", "bilateral", "midline" or None from a reported side (any case: "left", "lh", "left hemisphere", "bilat", "L+R", ...)."""
    if side is None:
        return None
    text = " ".join(str(side).strip().lower().replace("_", " ").replace("-", " ").split())
    exact = {"l": "L", "lh": "L", "r": "R", "rh": "R", "b": "bilateral", "both": "bilateral", "l+r": "bilateral", "r+l": "bilateral", "l/r": "bilateral", "r/l": "bilateral", "lr": "bilateral", "m": "midline", "mid": "midline"}
    if text in exact:
        return exact[text]
    for prefix, value in (("left", "L"), ("right", "R"), ("bilat", "bilateral"), ("both", "bilateral"), ("midline", "midline")):
        if text.startswith(prefix):
            return value
    return None


_SIDE_WORDS = {"L": "LEFT", "R": "RIGHT", "bilateral": "BILATERAL", "midline": "MIDLINE"}


def side_check(mask, affine, reported_side, majority=SIDE_MAJORITY, midline_mm=MIDLINE_MM, midline_band_mm=MIDLINE_BAND_MM):
    """Traced hemisphere of a lesion against the side reported by the source.

    Each voxel is split between the hemispheres by the part of its x extent on each side of x = 0
    (the midsagittal plane), weighted by its lesion fraction. The traced side is "L" or "R" when that
    side holds at least ``majority`` of the lesion, otherwise "midline" when the lesion straddles the
    midline (centroid within ``midline_mm`` of x = 0 and at least half of the lesion within
    ``midline_band_mm`` of it), else "bilateral" (for example two symmetric lesions far from the midline).

    :return: {"traced_side", "reported_side" (normalized), "left_frac", "right_frac", "centroid_x_mm",
      "mismatch" (one side traced, the other reported), "level" ("ok", "amber" or "info"), "message"}.
    """
    affine = np.asarray(affine, dtype=np.float64)
    values = _fraction(mask)
    reported = normalize_side(reported_side)
    result = {"traced_side": None, "reported_side": reported, "left_frac": None, "right_frac": None, "centroid_x_mm": None, "mismatch": False, "level": "ok", "message": None}
    found = np.argwhere(values > 0)
    if len(found) == 0:
        result["message"] = "The lesion is empty"
        return result
    weights = values[tuple(found.T)]
    x = geometry.voxel_to_world(found, affine)[:, 0]
    extent = float(np.abs(affine[0, :3]).sum())  # x extent of a voxel
    left_share = np.clip((0.0 - (x - extent / 2.0)) / extent, 0.0, 1.0)
    total = float(weights.sum())
    left = float((weights * left_share).sum()) / total
    centroid = float((weights * x).sum()) / total
    near = float(weights[np.abs(x) <= midline_band_mm].sum()) / total
    if left >= majority:
        traced = "L"
    elif 1.0 - left >= majority:
        traced = "R"
    elif abs(centroid) <= midline_mm and near >= 0.5:
        traced = "midline"
    else:
        traced = "bilateral"
    result.update(traced_side=traced, left_frac=left, right_frac=1.0 - left, centroid_x_mm=centroid)
    if reported is None or reported == traced:
        return result
    if reported in ("L", "R") and traced in ("L", "R"):
        result.update(
            mismatch=True,
            level="amber",
            message=f"Lesion is in the {_SIDE_WORDS[traced]} hemisphere but the source says {_SIDE_WORDS[reported]}. Radiological figure?",
        )
    else:
        result.update(level="info", message=f"Traced {_SIDE_WORDS[traced].lower()} ({100 * left:.0f}% left); the source says {_SIDE_WORDS[reported].lower()}")
    return result


# CSF share


class CsfShare(NamedTuple):
    """Result of :func:`csf_share`: ``pct`` (None when not computed) and the ``reason`` it was not."""

    pct: object
    reason: object = None
    source: object = None

    @property
    def flagged(self):
        return self.pct is not None and self.pct > CSF_PCT_MAX


def _family(label):
    """Template family of a space label, or the label itself when it already names a family."""
    if label in geometry.SPACE_FAMILIES:
        return label
    return geometry.space_family(label)


def csf_share(mask, affine, csf=None, ventricle_labels=None, lesion_space=None, csf_threshold=CSF_THRESHOLD):
    """Percentage of the lesion (fraction-weighted) in CSF or in the ventricles.

    :param csf: (probability array, affine, space or family) of a CSF prior: voxels with a
      probability above ``csf_threshold`` count.
    :param ventricle_labels: (label array, affine, space or family[, labels]) instead: voxels with one of
      ``labels`` (default: any non-zero label) count.
    :param lesion_space: the lesion's space label or family; by default the family of its grid (see
      geometry.detect_grid_candidates).
    :return: :class:`CsfShare` (pct, reason, source). ``pct`` is None, with the ``reason``, when the
      prior is of another template family than the lesion (geometry.same_family: a prior of another
      family is not aligned with the lesion), the lesion's family is unknown, or the lesion is empty.
    """
    if (csf is None) == (ventricle_labels is None):
        raise ValueError("Pass either a CSF prior or ventricle labels")
    prior = csf if csf is not None else ventricle_labels
    source = "csf" if csf is not None else "ventricles"
    image, prior_affine = np.asarray(prior[0]), np.asarray(prior[1], dtype=np.float64)
    prior_family = _family(prior[2] if len(prior) > 2 else None)
    affine = np.asarray(affine, dtype=np.float64)
    values = _fraction(mask)
    if lesion_space is not None:
        lesion_family = _family(lesion_space)
    else:
        match = geometry.detect_grid_candidates(values.shape, affine)
        lesion_family = match.family if match is not None else "unknown"
    if lesion_family in ("native", "unknown"):
        return CsfShare(None, f"The lesion's template family is {lesion_family}: the {source} prior cannot be compared", source)
    if prior_family != lesion_family:
        return CsfShare(None, f"The {source} prior is in the {prior_family} template family and the lesion in {lesion_family}: a prior of another family is not aligned with the lesion", source)
    if csf is not None:
        region = _values(image) > csf_threshold
    else:
        labels = ventricle_labels[3] if len(ventricle_labels) > 3 and ventricle_labels[3] is not None else None
        region = np.isin(image, list(labels)) if labels is not None else image != 0
    on_prior = _fraction_on(values, affine, geometry.Grid(image.shape, prior_affine))
    total = float(on_prior.sum())
    if total <= 0:
        return CsfShare(None, "The lesion is empty (or outside the prior's field of view)", source)
    return CsfShare(100.0 * float(on_prior[region].sum()) / total, None, source)


# Duplicate index


def index_path(study_root):
    """Path of a study's duplicate index: ``<study>/.studio/cache/lesion_index.npz``."""
    from .study import PRIVATE_DIR

    return os.path.join(study_root, PRIVATE_DIR, "cache", "lesion_index.npz")


def file_key(path):
    """(mtime in ns, size in bytes) of a lesion file, the key of its index entry."""
    stat = os.stat(path)
    return (int(stat.st_mtime_ns), int(stat.st_size))


class LesionIndex:
    """Occupied 2 mm voxels of the lesions of a study, for Dice-based duplicate candidates.

    Each lesion is stored as the sorted flat indices (int32) of the 2 mm voxels it fills to at least
    0.5 (fractional occupancy, geometry.resample_to_grid; a lesion smaller than that keeps its fullest
    voxels) and their bounding box. :meth:`candidates` prefilters by bounding box and by the Dice
    upper bound 2 min(a, b) / (a + b), then intersects the index sets (np.intersect1d).
    """

    FORMAT = "slicer-lnm-lesion-index"
    VERSION = 1

    def __init__(self, grid2mm=INDEX_GRID_ID):
        self.grid = _as_grid(grid2mm)
        self._entries = {}  # sid -> (indices, bbox (6,), key or None)
        self._table = None  # cached (sids, bboxes, sizes) for the prefilter

    def __len__(self):
        return len(self._entries)

    def __contains__(self, sid):
        return str(sid) in self._entries

    def sids(self):
        return list(self._entries)

    def indices(self, sid):
        """Sorted flat 2 mm voxel indices (int32) of a lesion."""
        return self._entries[str(sid)][0]

    def key(self, sid):
        """The (mtime_ns, size) key stored with a lesion, or None."""
        entry = self._entries.get(str(sid))
        return None if entry is None else entry[2]

    def is_current(self, sid, path):
        """True when the lesion is indexed with the current mtime and size of its file."""
        entry = self._entries.get(str(sid))
        if entry is None or entry[2] is None:
            return False
        try:
            return entry[2] == file_key(path)
        except OSError:
            return False

    def _occupied(self, mask, affine):
        """Sorted flat index-grid voxels filled to >= 0.5 (the fullest voxels of a smaller lesion).

        Only the non-zero voxels (or their bounding box) are converted, so a full-size lesion image costs
        one pass over its voxels.
        """
        array = np.asarray(mask)
        if array.ndim != 3:
            raise ValueError(f"A lesion must be a 3D array, got shape {array.shape}")
        affine = np.asarray(affine, dtype=np.float64)
        support = _support(array)
        if support.size == 0:
            return np.zeros(0, dtype=np.int32)
        if geometry.Grid(array.shape, affine).matches(self.grid):
            flat, positions = _fraction(array.reshape(-1)[support]), support
        else:
            ijk = np.unravel_index(support, array.shape)
            lo = np.array([int(a.min()) for a in ijk])
            hi = np.array([int(a.max()) + 1 for a in ijk])
            shift = np.eye(4)
            shift[:3, 3] = lo
            crop = _fraction(array[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]])
            flat = geometry.resample_to_grid(crop, affine @ shift, self.grid, mode="fraction").reshape(-1)
            positions = np.flatnonzero(flat)
            flat = flat[positions]
        keep = flat >= LESION_THRESHOLD
        if not keep.any() and flat.size and flat.max() > 0:
            keep = flat == flat.max()
        return positions[keep].astype(np.int32)

    def _bbox(self, indices):
        if indices.size == 0:
            return np.array([0, 0, 0, -1, -1, -1], dtype=np.int32)
        ijk = np.unravel_index(indices, self.grid.shape)
        return np.array([*(int(a.min()) for a in ijk), *(int(a.max()) for a in ijk)], dtype=np.int32)

    def update(self, sid, mask, affine, grid2mm=None, key=None):
        """Index (or re-index) a lesion.

        :param grid2mm: the index grid the caller expects; ValueError when it is not this index's grid.
        :param key: (mtime_ns, size) of the lesion file (see :func:`file_key`), stored for :meth:`is_current`.
        """
        if grid2mm is not None and not _as_grid(grid2mm).matches(self.grid):
            raise ValueError(f"The index grid is {self.grid}, not {_as_grid(grid2mm)}")
        indices = self._occupied(mask, affine)
        self.set_indices(sid, indices, key)

    def set_indices(self, sid, indices, key=None):
        """Store precomputed sorted flat indices (int32) of a lesion on the index grid."""
        indices = np.asarray(indices, dtype=np.int32)
        self._entries[str(sid)] = (indices, self._bbox(indices), None if key is None else (int(key[0]), int(key[1])))
        self._table = None

    def remove(self, sid):
        if self._entries.pop(str(sid), None) is not None:
            self._table = None

    def _prefilter_table(self):
        if self._table is None:
            sids = list(self._entries)
            bboxes = np.array([self._entries[s][1] for s in sids], dtype=np.int32).reshape(-1, 6)
            sizes = np.array([self._entries[s][0].size for s in sids], dtype=np.int64)
            self._table = (sids, bboxes, sizes)
        return self._table

    def candidates(self, sid, threshold=DUPLICATE_DICE):
        """Other lesions with Dice >= threshold with this one: [(other sid, dice)], highest Dice first."""
        sid = str(sid)
        indices, bbox, _key = self._entries[sid]
        if indices.size == 0:
            return []
        sids, bboxes, sizes = self._prefilter_table()
        overlap = np.all(bboxes[:, :3] <= bbox[3:], axis=1) & np.all(bboxes[:, 3:] >= bbox[:3], axis=1)
        bound = 2.0 * np.minimum(sizes, indices.size) / np.maximum(sizes + indices.size, 1)
        keep = np.flatnonzero(overlap & (sizes > 0) & (bound >= threshold - 1e-12))
        found = []
        for position in keep:
            other = sids[position]
            if other == sid:
                continue
            common = np.intersect1d(indices, self._entries[other][0], assume_unique=True).size
            dice = 2.0 * common / (indices.size + sizes[position])
            if dice >= threshold - 1e-12:
                found.append((other, float(dice)))
        return sorted(found, key=lambda item: (-item[1], item[0]))

    def all_pairs(self, threshold=DUPLICATE_DICE):
        """Every candidate pair once: [(sid a, sid b, dice)] with a before b in index order."""
        order = {sid: position for position, sid in enumerate(self._entries)}
        return [(sid, other, dice) for sid in self._entries for other, dice in self.candidates(sid, threshold) if order[sid] < order[other]]

    def save(self, path):
        """Write the index to an .npz file (atomically; the folders are created)."""
        sids = list(self._entries)
        sizes = np.array([self._entries[s][0].size for s in sids], dtype=np.int64)
        keys = np.array([self._entries[s][2] or (-1, -1) for s in sids], dtype=np.int64).reshape(-1, 2)
        arrays = {
            "format": np.array(self.FORMAT),
            "version": np.array(self.VERSION),
            "grid_shape": np.array(self.grid.shape, dtype=np.int64),
            "grid_affine": self.grid.affine,
            "sids": np.array(sids, dtype=str),
            "offsets": np.concatenate([[0], np.cumsum(sizes)]).astype(np.int64),
            "indices": np.concatenate([self._entries[s][0] for s in sids]).astype(np.int32) if sids else np.zeros(0, dtype=np.int32),
            "keys": keys,
        }
        folder = os.path.dirname(os.path.abspath(path))
        os.makedirs(folder, exist_ok=True)
        temporary = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"  # unique per process and thread
        try:
            with open(temporary, "wb") as f:
                np.savez(f, **arrays)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)

    @classmethod
    def load(cls, path, grid2mm=INDEX_GRID_ID):
        """Read an index written by :meth:`save`.

        The index is a cache: a missing, empty, truncated, unreadable or older-format file, or one on
        another grid than ``grid2mm``, gives an empty index (the lesions are then indexed again).
        """
        index = cls(grid2mm)
        try:
            with open(path, "rb") as f, np.load(f, allow_pickle=False) as data:
                if str(data["format"]) != cls.FORMAT or int(data["version"]) != cls.VERSION:
                    return index
                if not geometry.Grid(data["grid_shape"], data["grid_affine"]).matches(index.grid):
                    return index
                sids, offsets, indices, keys = data["sids"], data["offsets"], data["indices"], data["keys"]
                for position, sid in enumerate(sids.tolist()):
                    key = tuple(int(v) for v in keys[position])
                    index.set_indices(sid, indices[offsets[position]:offsets[position + 1]], None if key[0] < 0 else key)
        except (OSError, KeyError, ValueError, TypeError, AttributeError, IndexError, EOFError, zipfile.BadZipFile):
            return cls(grid2mm)
        return index


# QC flags


def qc_flags(results):
    """``qc_flags`` codes (see :data:`QC_FLAGS`, in that order) of a lesion's check results.

    :param results: mapping with any of "hygiene" (:func:`hygiene`), "side" (:func:`side_check`),
      "csf" (:func:`csf_share`) and "duplicates" (:meth:`LesionIndex.candidates`) results; missing
      checks raise no flag.
    :return: list of codes; ``";".join(...)`` gives the column value.
    """
    raised = set()
    side = results.get("side")
    if side and side.get("mismatch"):
        raised.add("side_mismatch")
    csf = results.get("csf")
    if csf is not None:
        pct = csf.pct if isinstance(csf, CsfShare) else csf
        if pct is not None and pct > CSF_PCT_MAX:
            raised.add("csf")
    checks = results.get("hygiene") or {}
    for code in ("nonbinary", "empty", "subvoxel", "outside", "xform", "grid", "multipart"):
        if checks.get(code):
            raised.add(code)
    if results.get("duplicates"):
        raised.add("duplicate")
    return [code for code in QC_FLAGS if code in raised]

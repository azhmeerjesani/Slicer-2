"""Threshold-free cluster enhancement (TFCE) of statistic maps (numpy and scipy only; no Slicer imports).

TFCE (Smith & Nichols 2009) replaces each voxel's statistic by the integral, over the heights h up
to that statistic, of the extent e(h) of the cluster that holds the voxel at height h:

    TFCE(v) = sum over k = 1..steps with h_k = k * dh <= stat(v) of  e(h_k)^E * h_k^H * dh,

with dh = max(stat) / steps (per map, as PALM and FSL do) and clusters of the voxels with stat >= h_k,
connected by faces (6), faces and edges (18) or faces, edges and corners (26). Extents are counted in
voxels. Defaults are H = 2, E = 0.5, 100 steps and 26-connectivity (SPEC_LNM_ADDENDUM §3.4).

Positive and negative parts are enhanced separately (the negative part from -stat); a two-sided
result is TFCE(stat+) - TFCE(stat-). The labelling runs on the bounding box of the mask with a
structure built once (:class:`TfceGrid`), which is what the GLM permutation loop uses: TFCE FWE p
values come from the maximum TFCE of each permutation (glm.py). Each height labels only the box of
the voxels still above it, which gives the same clusters as labelling the whole box.

Cost on the reference host (4 cores): 0.7-0.95 s per one-sided map on the 91 x 109 x 91 box of a
2 mm MNI mask (up to 100 ``scipy.ndimage.label`` calls on shrinking boxes), so 1.3-1.9 s per
two-sided permutation (the addendum's 1.3 s per one-sided map labelled the whole box at every
height); :func:`tfce_eta` scales the upper value (measured under shared load) by the size of the box.
"""

import math
import time

import numpy as np
from scipy import ndimage

DEFAULT_H = 2.0
DEFAULT_E = 0.5
DEFAULT_STEPS = 100
DEFAULT_CONNECTIVITY = 26
CONNECTIVITIES = {6: 1, 18: 2, 26: 3}
#: Measured seconds per one-sided map per voxel of the bounding box (0.95 s on 91 x 109 x 91 with 3 BLAS
#: threads, smooth maps, under shared load; the upper end of the L-07 measurements and its review)
SECONDS_PER_BOX_VOXEL = 0.95 / (91 * 109 * 91)
REFERENCE_BOX = (91, 109, 91)


def structure(connectivity=DEFAULT_CONNECTIVITY):
    """The 3x3x3 connectivity structure of ``scipy.ndimage.label`` for 6, 18 or 26 neighbours."""
    try:
        rank = CONNECTIVITIES[int(connectivity)]
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"connectivity must be 6, 18 or 26, got {connectivity!r}") from None
    return ndimage.generate_binary_structure(3, rank)


def bounding_box(mask):
    """Tuple of slices of the smallest box holding every non-zero voxel of a 3D mask (an empty mask gives an empty box)."""
    mask = np.asarray(mask) != 0
    if mask.ndim != 3:
        raise ValueError(f"The mask must be 3-dimensional, got shape {mask.shape}")
    if not mask.any():
        return (slice(0, 0),) * 3
    out = []
    for axis in range(3):
        other = tuple(index for index in range(3) if index != axis)
        hits = np.flatnonzero(mask.any(axis=other))
        out.append(slice(int(hits[0]), int(hits[-1]) + 1))
    return tuple(out)


def _box_size(box):
    return tuple(max(int(item.stop) - int(item.start), 0) for item in box)


class TfceGrid:
    """A mask's bounding box, its in-mask voxels and the connectivity structure, built once.

    :param mask: 3D array; non-zero voxels are the in-mask voxels. Masked vectors (as everywhere in
      this library) list them in flat C order of the full volume (``geometry.mask_indices``).
    :param connectivity: 6, 18 or 26.
    """

    def __init__(self, mask, connectivity=DEFAULT_CONNECTIVITY):
        mask = np.asarray(mask) != 0
        self.shape = mask.shape
        self.box = bounding_box(mask)
        self.box_shape = _box_size(self.box)
        self.structure = structure(connectivity)
        self.connectivity = int(connectivity)
        self.box_mask = np.ascontiguousarray(mask[self.box])
        self.box_indices = np.flatnonzero(self.box_mask.reshape(-1))  # in-mask voxels in the box, C order
        self.box_coords = np.stack(np.unravel_index(self.box_indices, self.box_shape), axis=1) if self.box_indices.size else np.zeros((0, 3), dtype=np.intp)
        self.n_voxels = int(self.box_indices.size)

    @property
    def box_voxels(self):
        return int(np.prod(self.box_shape))

    def box_volume(self, vector):
        """A masked vector (V values) scattered into a zero-filled volume of the bounding box."""
        vector = np.asarray(vector, dtype=np.float64).reshape(-1)
        if vector.size != self.n_voxels:
            raise ValueError(f"Expected {self.n_voxels} in-mask values, got {vector.size}")
        out = np.zeros(self.box_voxels, dtype=np.float64)
        out[self.box_indices] = vector
        return out.reshape(self.box_shape)

    def enhance(self, vector, H=DEFAULT_H, E=DEFAULT_E, steps=DEFAULT_STEPS, two_sided=True):  # noqa: N803 (TFCE parameter names)
        """TFCE of a masked statistic vector (V values): signed when two-sided, else of the positive part only."""
        volume = self.box_volume(vector)
        out = _enhance_positive(volume, self.box_coords, self.structure, H, E, steps)
        if two_sided:
            out -= _enhance_positive(-volume, self.box_coords, self.structure, H, E, steps)
        return out

    def max_enhanced(self, vector, H=DEFAULT_H, E=DEFAULT_E, steps=DEFAULT_STEPS, two_sided=True):  # noqa: N803
        """The maximum TFCE value of a map (of |TFCE| over both signs when two-sided): one permutation's null value."""
        enhanced = self.enhance(vector, H, E, steps, two_sided)
        if enhanced.size == 0:
            return 0.0
        return float(np.max(np.abs(enhanced)) if two_sided else np.max(enhanced))


def _enhance_positive(volume, coords, struct, H, E, steps):  # noqa: N803
    """TFCE of the positive part of a box volume, returned at the in-mask voxels (``coords``: their box indices, C order).

    Each height labels only the box of the voxels still above it (they shrink as the height grows),
    which gives the same clusters as labelling the whole box.
    """
    out = np.zeros(len(coords), dtype=np.float64)
    if len(coords) == 0:
        return out
    values = volume[coords[:, 0], coords[:, 1], coords[:, 2]]
    top = float(values.max())
    if not top > 0 or not math.isfinite(top):
        return out
    steps = int(steps)
    if steps < 1:
        raise ValueError(f"steps must be at least 1, got {steps}")
    dh = top / steps
    active = np.flatnonzero(values > 0)  # voxels that can still be above the next height
    for k in range(1, steps + 1):
        h = top if k == steps else k * dh  # the last height is the maximum itself (k * dh may round below it)
        active = active[values[active] >= h]
        if active.size == 0:
            break
        local = coords[active]
        lo = local.min(axis=0)
        hi = local.max(axis=0) + 1
        sub = volume[lo[0] : hi[0], lo[1] : hi[1], lo[2] : hi[2]]
        labels, count = ndimage.label(sub >= h, structure=struct)
        sizes = np.bincount(labels.reshape(-1), minlength=count + 1).astype(np.float64)
        local = local - lo
        out[active] += sizes[labels[local[:, 0], local[:, 1], local[:, 2]]] ** E * (h**H * dh)
    return out


def tfce(vol, mask_bbox=None, H=DEFAULT_H, E=DEFAULT_E, steps=DEFAULT_STEPS, connectivity=DEFAULT_CONNECTIVITY, two_sided=True):  # noqa: N803
    """TFCE of a 3D statistic volume.

    :param vol: 3D array (a t, F or other statistic map).
    :param mask_bbox: None (the whole volume), a 3D mask (voxels outside it are set to 0 and the
      labelling runs on its bounding box), or a tuple of three slices (the box; every voxel in it counts).
    :param H, E, steps: height and extent exponents and number of height steps (dh = max / steps per sign).
    :param connectivity: 6, 18 or 26.
    :param two_sided: return TFCE(stat+) - TFCE(stat-); else only the positive part is enhanced.
    :return: float64 volume of the shape of ``vol`` (0 outside the mask or box).
    """
    vol = np.asarray(vol, dtype=np.float64)
    if vol.ndim != 3:
        raise ValueError(f"tfce expects a 3D volume, got shape {vol.shape}")
    if mask_bbox is None:
        mask = np.ones(vol.shape, dtype=bool)
    elif isinstance(mask_bbox, tuple) and len(mask_bbox) == 3 and all(isinstance(item, slice) for item in mask_bbox):
        mask = np.zeros(vol.shape, dtype=bool)
        mask[mask_bbox] = True
    else:
        mask = np.asarray(mask_bbox) != 0
        if mask.shape != vol.shape:
            raise ValueError(f"The mask has shape {mask.shape} but the volume has {vol.shape}")
    grid = TfceGrid(mask, connectivity)
    vector = vol.reshape(-1)[np.flatnonzero(mask.reshape(-1))]
    enhanced = grid.enhance(vector, H, E, steps, two_sided)
    out = np.zeros(vol.size, dtype=np.float64)
    out[np.flatnonzero(mask.reshape(-1))] = enhanced
    return out.reshape(vol.shape)


def tfce_eta(V, n_perm, two_sided=True, seconds_per_voxel=None, box_voxels=None):  # noqa: N803 (V: voxels, as in the spec)
    """Estimated seconds of TFCE for a permutation run.

    The cost depends on the bounding box of the mask, not on the in-mask voxel count, so ``V`` must
    describe the box; a bare voxel count is refused (it would usually be the in-mask count, which
    underestimates about 4x on an MNI mask): pass ``box_voxels`` for an explicit box size.

    :param V: the mask: a :class:`TfceGrid`, a 3D mask, or the box shape (a 3-tuple); None with ``box_voxels``.
    :param n_perm: number of maps enhanced (permutations, identity included).
    :param two_sided: two enhancements (positive and negative) per map.
    :param seconds_per_voxel: measured cost per box voxel and one-sided map (default: this host's
      :data:`SECONDS_PER_BOX_VOXEL`, about 0.95 s for a 91 x 109 x 91 box); see :func:`measure_seconds_per_voxel`.
    :param box_voxels: voxels of the bounding box, instead of ``V``.
    :return: seconds.
    :raises TypeError: ``V`` is a bare number.
    """
    if box_voxels is not None:
        voxels = int(box_voxels)
    elif isinstance(V, TfceGrid):
        voxels = V.box_voxels
    elif isinstance(V, (tuple, list)) and len(V) == 3 and all(isinstance(item, (int, np.integer)) for item in V):
        voxels = int(np.prod(V))
    elif isinstance(V, np.ndarray) and V.ndim == 3:
        voxels = int(np.prod(_box_size(bounding_box(V))))
    else:
        raise TypeError("tfce_eta needs the mask, its TfceGrid or the box shape (or box_voxels=); a voxel count is ambiguous")
    rate = SECONDS_PER_BOX_VOXEL if seconds_per_voxel is None else float(seconds_per_voxel)
    return float(voxels) * rate * (2 if two_sided else 1) * max(int(n_perm), 0)


def measure_seconds_per_voxel(grid, repeats=1, seed=0, H=DEFAULT_H, E=DEFAULT_E, steps=DEFAULT_STEPS):  # noqa: N803
    """Measured seconds per box voxel of one one-sided enhancement of a smooth random map on ``grid``."""
    rng = np.random.default_rng(seed)
    noise = ndimage.gaussian_filter(rng.standard_normal(grid.box_shape), 2.0)
    vector = noise.reshape(-1)[grid.box_indices]
    vector = vector / (vector.std() or 1.0) * 2.0
    started = time.perf_counter()
    for _ in range(max(int(repeats), 1)):
        grid.enhance(vector, H, E, steps, two_sided=False)
    return (time.perf_counter() - started) / max(int(repeats), 1) / max(grid.box_voxels, 1)

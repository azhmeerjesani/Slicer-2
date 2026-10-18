"""Voxel grids, affine-based resampling and masked-vector conversions.

All functions map voxels through the affines (voxel -> RAS world -> voxel) and never by array
index, so grids with any orientation (including radiological, left-handed grids) are handled.
Volumes are indexed [i, j, k] in file order (see nifti.py).
"""

import numpy as np

from .nifti import read_nifti_header


class Grid:
    """A 3D voxel grid: shape (ni, nj, nk) and a 4x4 affine mapping 0-based (i, j, k) to RAS mm."""

    def __init__(self, shape, affine):
        self.shape = tuple(int(n) for n in shape[:3])
        if len(self.shape) != 3:
            raise ValueError(f"A grid needs a 3D shape, got {shape}")
        self.affine = np.array(affine, dtype=np.float64).reshape(4, 4)

    @classmethod
    def from_nifti(cls, path):
        """Return the grid of a NIfTI image (reads only the header)."""
        header = read_nifti_header(path)
        return cls(header["shape"][:3], header["affine"])

    @property
    def n_voxels(self):
        return int(np.prod(self.shape))

    @property
    def spacing(self):
        """Voxel size along i, j, k in mm."""
        return np.sqrt((self.affine[:3, :3] ** 2).sum(axis=0))

    @property
    def voxel_volume(self):
        """Voxel volume in mm^3."""
        return float(abs(np.linalg.det(self.affine[:3, :3])))

    def voxel_to_world(self, ijk):
        return voxel_to_world(ijk, self.affine)

    def world_to_voxel(self, xyz):
        return world_to_voxel(xyz, self.affine)

    def matches(self, other, atol=1e-3):
        """True if the other grid has the same shape and (nearly) the same affine."""
        return self.shape == tuple(other.shape) and np.allclose(self.affine, other.affine, atol=atol, rtol=0)

    def __repr__(self):
        return f"Grid(shape={self.shape}, spacing={np.round(self.spacing, 4).tolist()})"


def voxel_to_world(ijk, affine):
    """Map (N, 3) voxel coordinates to (N, 3) world coordinates."""
    ijk = np.asarray(ijk, dtype=np.float64)
    affine = np.asarray(affine, dtype=np.float64)
    return ijk @ affine[:3, :3].T + affine[:3, 3]


def world_to_voxel(xyz, affine):
    """Map (N, 3) world coordinates to (N, 3) continuous voxel coordinates."""
    xyz = np.asarray(xyz, dtype=np.float64)
    inverse = np.linalg.inv(np.asarray(affine, dtype=np.float64))
    return xyz @ inverse[:3, :3].T + inverse[:3, 3]


def affine_from_spm_mat(mat):
    """Convert an SPM/MATLAB voxel-to-world matrix (1-based indices) to a 0-based affine."""
    shift = np.eye(4)
    shift[:3, 3] = 1.0
    return np.asarray(mat, dtype=np.float64) @ shift


def resample_to_grid(src_data, src_affine, dst_grid, mode="fraction", supersample=2, max_points=4_000_000):
    """Resample a 3D image onto another grid through the affines.

    :param src_data: source array indexed [i, j, k].
    :param src_affine: 4x4 source voxel-to-RAS matrix.
    :param dst_grid: destination :class:`Grid`.
    :param mode:
      - "fraction": volume-weighted average of the source values in each destination voxel, which
        for a binary lesion is the fractional occupancy (0..1); even a single source voxel survives
        downsampling. Axis-aligned grid pairs (any voxel sizes, flips and axis permutations, e.g.
        1.5 mm or radiological images to a 2 mm MNI grid) use the exact voxel overlap volumes, so
        the lesion volume inside the destination field of view is conserved exactly. Oblique pairs
        average the source values at regularly spread sub-samples of each destination voxel.
      - "nearest": nearest-neighbour value at each destination voxel center.
      - "linear": trilinear interpolation at each destination voxel center.
    :param supersample: minimum sub-samples per axis per destination voxel for "fraction" on
      oblique grid pairs (1..8). The count is increased automatically (up to MAX_SUBSAMPLES) so
      that the sub-sample spacing is at most half the source voxel size.
    :param max_points: sub-sample batch size (bounds memory use).
    :return: float array with shape ``dst_grid.shape``; voxels outside the source field of view are 0.
    """
    src = np.asarray(src_data)
    if src.ndim != 3:
        raise ValueError(f"resample_to_grid expects a 3D array, got shape {src.shape}")
    src_affine = np.asarray(src_affine, dtype=np.float64)
    if mode == "fraction":
        return _resample_fraction(src, src_affine, dst_grid, int(supersample), int(max_points))
    if mode not in ("nearest", "linear"):
        raise ValueError(f"Unknown resampling mode: {mode}")

    # Pull: destination voxel centers -> continuous source voxel coordinates, one i-slab at a time
    from scipy import ndimage

    dst_to_src = np.linalg.inv(src_affine) @ dst_grid.affine
    values = np.nan_to_num(src.astype(np.float64, copy=False))
    out = np.zeros(dst_grid.shape, dtype=np.float64)
    ni, nj, nk = dst_grid.shape
    slab = max(1, max_points // max(1, nj * nk))
    jk = np.indices((nj, nk), dtype=np.float64).reshape(2, -1)
    src_shape = np.array(src.shape)[:, np.newaxis]
    for i0 in range(0, ni, slab):
        i1 = min(ni, i0 + slab)
        ijk = np.vstack([np.repeat(np.arange(i0, i1, dtype=np.float64), nj * nk), np.tile(jk, i1 - i0)])
        coords = dst_to_src[:3, :3] @ ijk + dst_to_src[:3, 3:4]
        if mode == "nearest":
            index = np.floor(coords + 0.5).astype(np.int64)
            inside = np.all((index >= 0) & (index < src_shape), axis=0)
            slab_values = np.zeros(index.shape[1], dtype=np.float64)
            slab_values[inside] = values[index[0, inside], index[1, inside], index[2, inside]]
        else:
            slab_values = ndimage.map_coordinates(values, coords, order=1, mode="constant", cval=0.0)
        out[i0:i1] = slab_values.reshape(i1 - i0, nj, nk)
    return out


MAX_SUBSAMPLES = 16  # cap on the sub-samples per axis of oblique fraction resampling


def _resample_fraction(src, src_affine, dst_grid, supersample, max_points):
    if not 1 <= supersample <= 8:
        raise ValueError("supersample must be between 1 and 8")
    values = np.nan_to_num(src.astype(np.float64, copy=False))
    out = np.zeros(dst_grid.shape, dtype=np.float64)
    # Bounding box [lo, hi) of the non-zero source voxels
    lo, hi = [], []
    for axis in range(3):
        used = np.flatnonzero(values.any(axis=tuple(a for a in range(3) if a != axis)))
        if used.size == 0:
            return out
        lo.append(int(used[0]))
        hi.append(int(used[-1]) + 1)
    crop = values[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]]
    src_to_dst = np.linalg.inv(dst_grid.affine) @ src_affine
    permutation = _axis_permutation(src_to_dst[:3, :3], src.shape)
    if permutation is not None:
        _fraction_axis_aligned(crop, lo, src_to_dst, permutation, out)
    else:
        _fraction_supersampled(crop, lo, src_affine, dst_grid, supersample, max_points, out)
    return out


def _axis_permutation(linear, src_shape, tolerance=1e-3):
    """Destination axis of each source axis if ``linear`` (source to destination voxel matrix) is a
    signed permutation of a diagonal matrix, else None.

    Off-diagonal terms are ignored when they move no source voxel by more than ``tolerance``
    destination voxels (float32 rounding of the affines).
    """
    dst_axes = np.argmax(np.abs(linear), axis=0)
    if len(set(dst_axes.tolist())) != 3:
        return None
    residual = linear.copy()
    residual[dst_axes, np.arange(3)] = 0.0
    if (np.abs(residual) @ np.asarray(src_shape, dtype=np.float64)).max() > tolerance:
        return None
    return dst_axes


def _overlap_matrix(first, count, scale, offset, n_dst):
    """1D overlap lengths, in destination voxels, of source voxels first..first+count-1 with the
    destination voxels along one axis.

    :return: (index q0 of the first destination voxel, (nq, count) matrix), or (q0, None) if no overlap.
    """
    s = np.arange(first, first + count, dtype=np.float64)
    ends = np.stack([scale * (s - 0.5) + offset, scale * (s + 0.5) + offset])
    start, stop = ends.min(axis=0), ends.max(axis=0)
    q0 = max(0, int(np.floor(start.min() + 0.5)))
    q1 = min(n_dst, int(np.floor(stop.max() + 0.5)) + 1)
    if q1 <= q0:
        return q0, None
    q = np.arange(q0, q1, dtype=np.float64)[:, np.newaxis]
    return q0, np.clip(np.minimum(stop, q + 0.5) - np.maximum(start, q - 0.5), 0.0, None)


def _fraction_axis_aligned(crop, lo, src_to_dst, dst_axes, out):
    """Exact box-filter resampling between axis-aligned grids (voxels are boxes on parallel axes).

    The overlap volume of a source and a destination voxel is the product of their 1D overlaps, so
    the result is three separable matrix products. Volume is conserved exactly (inside the
    destination field of view) for any voxel size ratio and any flips or axis permutations.
    """
    result = crop
    starts = [0, 0, 0]
    for axis in range(3):
        dst_axis = dst_axes[axis]
        q0, matrix = _overlap_matrix(lo[axis], crop.shape[axis], src_to_dst[dst_axis, axis], src_to_dst[dst_axis, 3], out.shape[dst_axis])
        if matrix is None:
            return  # outside the destination field of view
        result = np.moveaxis(np.tensordot(matrix, result, axes=([1], [axis])), 0, axis)
        starts[dst_axis] = q0
    result = np.transpose(result, np.argsort(dst_axes))  # result axis order: source -> destination
    out[tuple(slice(q0, q0 + n) for q0, n in zip(starts, result.shape, strict=True))] += result


def _fraction_supersampled(crop, lo, src_affine, dst_grid, supersample, max_points, out):
    """Fractional occupancy on oblique grid pairs: mean of the nearest source values at n^3 points
    regularly spread in each destination voxel (only voxels near the non-zero source box are visited).

    The sub-sample spacing is at most half the smallest width of a source voxel, so every source
    voxel holds at least one sub-sample (a single source voxel survives downsampling), interior
    voxels are exactly 1 for a binary lesion and the total volume is unbiased.
    """
    dst_to_src = np.linalg.inv(src_affine) @ dst_grid.affine
    src_to_dst = np.linalg.inv(dst_to_src)
    lo = np.asarray(lo)
    hi = lo + np.array(crop.shape)
    # Destination voxels overlapping the source box (its 8 corners mapped into destination voxels)
    corners = np.array(np.meshgrid(*zip(lo - 0.5, hi - 0.5, strict=True), indexing="ij")).reshape(3, -1).T
    corners = corners @ src_to_dst[:3, :3].T + src_to_dst[:3, 3]
    d0 = np.maximum(np.floor(corners.min(axis=0) + 0.5).astype(np.int64), 0)
    d1 = np.minimum(np.floor(corners.max(axis=0) + 0.5).astype(np.int64) + 1, dst_grid.shape)
    if np.any(d1 <= d0):
        return
    # Smallest face-to-face width of a source voxel (its edge length unless the affine is sheared)
    edges = np.asarray(src_affine, dtype=np.float64)[:3, :3]
    volume = abs(np.linalg.det(edges))
    width = min(volume / np.linalg.norm(np.cross(edges[:, a], edges[:, b])) for a, b in ((1, 2), (2, 0), (0, 1)))
    counts = [min(MAX_SUBSAMPLES, max(supersample, int(np.ceil(2.0 * spacing / width - 1e-9)))) for spacing in dst_grid.spacing]
    offsets = np.stack(np.meshgrid(*[(np.arange(n) + 0.5) / n - 0.5 for n in counts], indexing="ij"), axis=-1).reshape(-1, 3)
    offsets = offsets @ dst_to_src[:3, :3].T - lo  # in voxels of the cropped source
    n_sub = len(offsets)

    box_shape = tuple(int(n) for n in d1 - d0)
    box = np.zeros(int(np.prod(box_shape)), dtype=np.float64)
    flat_crop = crop.reshape(-1)
    batch = max(1, max_points // n_sub)
    for start in range(0, box.size, batch):
        stop = min(box.size, start + batch)
        ijk = np.stack(np.unravel_index(np.arange(start, stop), box_shape), axis=1) + d0
        centers = ijk @ dst_to_src[:3, :3].T + dst_to_src[:3, 3]
        flat = np.zeros((stop - start, n_sub), dtype=np.int64)
        inside = np.ones((stop - start, n_sub), dtype=bool)
        for axis in range(3):
            index = np.floor(centers[:, axis, np.newaxis] + offsets[np.newaxis, :, axis] + 0.5).astype(np.int64)
            inside &= (index >= 0) & (index < crop.shape[axis])
            flat = flat * crop.shape[axis] + index
        samples = np.zeros(flat.shape, dtype=np.float64)
        samples[inside] = flat_crop[flat[inside]]
        box[start:stop] = samples.mean(axis=1)
    out[tuple(slice(a, b) for a, b in zip(d0, d1, strict=True))] += box.reshape(box_shape)


def mask_indices(mask):
    """Flat C-order indices of the non-zero voxels of a mask indexed [i, j, k]."""
    return np.flatnonzero(np.asarray(mask).reshape(-1))


def to_masked_vector(volume, indices):
    """Return the values of a volume (or a stack of volumes, shape (..., ni, nj, nk)) at the mask voxels.

    :param indices: flat C-order mask indices (see :func:`mask_indices`).
    :return: array of shape (..., V).
    """
    volume = np.asarray(volume)
    lead = volume.shape[:-3]
    return volume.reshape(*lead, -1)[..., indices]


def from_masked_vector(vector, indices, shape, fill=0.0, dtype=None):
    """Scatter a masked vector (shape (..., V)) back into volume(s) of the given 3D grid shape."""
    vector = np.asarray(vector)
    lead = vector.shape[:-1]
    out = np.full((*lead, int(np.prod(shape))), fill, dtype=dtype or vector.dtype)
    out[..., indices] = vector
    return out.reshape(*lead, *shape)

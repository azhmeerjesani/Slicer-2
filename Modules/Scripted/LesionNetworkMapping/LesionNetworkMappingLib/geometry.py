"""Voxel grids, affine-based resampling and masked-vector conversions.

All functions map voxels through the affines (voxel -> RAS world -> voxel) and never by array
index, so grids with any orientation (including radiological, left-handed grids) are handled.
Volumes are indexed [i, j, k] in file order (see nifti.py).
"""

import os
import re
from typing import NamedTuple

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


class SignedPermutation(NamedTuple):
    """Voxel index mapping ``dst[axes[a]] = signs[a] * src[a] + offsets[axes[a]]`` (all integers)."""

    axes: tuple
    signs: tuple
    offsets: tuple


def signed_permutation(src_affine, dst_affine, tolerance=1e-3):
    """Express the source voxel grid in destination voxel indices, if this is exact.

    ``M = inv(dst_affine) @ src_affine`` maps source voxel indices to destination voxel indices.
    When its linear part is a signed permutation matrix (same voxel size, axes only swapped or
    flipped) and its offsets are integers, both grids share their voxel centers.

    :param tolerance: maximum deviation of every matrix entry and offset from an integer.
    :return: :class:`SignedPermutation`, or None if the grids do not share voxel centers.
    """
    matrix = np.linalg.inv(np.asarray(dst_affine, dtype=np.float64)) @ np.asarray(src_affine, dtype=np.float64)
    rounded = np.round(matrix)
    if np.abs(matrix - rounded).max() > tolerance or np.any(rounded[3] != [0, 0, 0, 1]):
        return None
    linear = rounded[:3, :3]
    if not np.array_equal(np.abs(linear).sum(axis=0), [1, 1, 1]) or not np.array_equal(np.abs(linear).sum(axis=1), [1, 1, 1]):
        return None
    axes = tuple(int(a) for a in np.argmax(np.abs(linear), axis=0))
    signs = tuple(int(linear[axes[a], a]) for a in range(3))
    return SignedPermutation(axes, signs, tuple(int(v) for v in rounded[:3, 3]))


# Template grids and spaces
#
# A grid (shape, voxel size and position) does not name a space: TemplateFlow resamples
# MNI152NLin2009cAsym, MNI152NLin2009aSym and MNI152NLin2009cSym onto the same 1 mm and 2 mm grids,
# and the SPM/ICBM 1 mm box also holds Colin27 images. GRID_REGISTRY therefore lists, for each
# grid, the candidate spaces (the first is the default) and whether the grid is ambiguous, so a
# study can ask once instead of silently choosing.


class GridSpec(NamedTuple):
    """A standard voxel grid.

    :ivar grid_id: registry key, e.g. "TF2009-1mm".
    :ivar shape: number of voxels along the world x, y and z axes.
    :ivar spacing: voxel size (mm), the same along every axis.
    :ivar lowest_center: RAS world coordinates (mm) of the voxel center with the smallest x, y and z.
    :ivar candidates: spaces stored on this grid; the first is the default.
    :ivar family: space family of the grid (see :func:`space_family`).
    :ivar ambiguous: True when the grid alone does not tell the candidates apart.
    :ivar x_flipped: True when the reference files store x from right to left (LAS, FSL's
      radiological layout); the grid is recognized in any storage order.
    """

    grid_id: str
    shape: tuple
    spacing: float
    lowest_center: tuple
    candidates: tuple
    family: str
    ambiguous: bool = False
    x_flipped: bool = False

    @property
    def space(self):
        """Default space of the grid (the first candidate)."""
        return self.candidates[0]

    @property
    def resolution(self):
        """Voxel size as an int when it is a whole number of mm (the TemplateFlow ``res`` label), else a float."""
        return int(self.spacing) if float(self.spacing).is_integer() else float(self.spacing)

    @property
    def affine(self):
        """4x4 voxel-to-RAS matrix of the grid as its reference files store it (LAS when ``x_flipped``)."""
        affine = np.diag([self.spacing, self.spacing, self.spacing, 1.0])
        affine[:3, 3] = self.lowest_center
        if self.x_flipped:
            affine[0, 0] = -self.spacing
            affine[0, 3] = self.lowest_center[0] + self.spacing * (self.shape[0] - 1)
        return affine

    @property
    def grid(self):
        """The :class:`Grid` of :attr:`affine`."""
        return Grid(self.shape, self.affine)


_TF2009_CANDIDATES = ("MNI152NLin2009cAsym", "MNI152NLin2009aSym", "MNI152NLin2009cSym")

GRID_REGISTRY = (
    # FSL's MNI152 (MNI152_T1_1mm/2mm and TemplateFlow tpl-MNI152NLin6Asym res-01 are stored LAS; TemplateFlow's res-02 is RAS)
    GridSpec("NLin6-1mm", (182, 218, 182), 1.0, (-91.0, -126.0, -72.0), ("MNI152NLin6Asym",), "fsl6", x_flipped=True),
    GridSpec("NLin6-2mm", (91, 109, 91), 2.0, (-90.0, -126.0, -72.0), ("MNI152NLin6Asym",), "fsl6", x_flipped=True),
    # TemplateFlow's resampled MNI ICBM 2009 grids, shared by the 2009c and 2009a templates
    GridSpec("TF2009-1mm", (193, 229, 193), 1.0, (-96.0, -132.0, -78.0), _TF2009_CANDIDATES, "2009", ambiguous=True),
    GridSpec("TF2009-2mm", (97, 115, 97), 2.0, (-96.5, -132.5, -78.5), _TF2009_CANDIDATES, "2009", ambiguous=True),
    # The original McGill 2009a grid (ATLAS v2.0 lesions; header verified on NeuroVault collection 11940)
    GridSpec("McGill2009a-1mm", (197, 233, 189), 1.0, (-98.0, -134.0, -72.0), ("MNI152NLin2009aSym",), "2009"),
    # SPM's default bounding box [-90 -126 -72; 90 90 108] at 1 mm (the published value: no header was available offline)
    GridSpec("SPM-1mm", (181, 217, 181), 1.0, (-90.0, -126.0, -72.0), ("MNI152Lin", "MNIColin27"), "spm", ambiguous=True, x_flipped=True),
)
GRID_BY_ID = {spec.grid_id: spec for spec in GRID_REGISTRY}

# Spaces of the connectome grids that detect_grid and detect_space report (the other registry
# grids are reported as "native" by them, as before; use detect_grid_candidates to recognize them)
CONNECTOME_SPACES = ("MNI152NLin6Asym", "MNI152NLin2009cAsym")

# (space, resolution, shape, spacing, lowest voxel center) of the grids detect_grid recognizes
KNOWN_GRIDS = tuple((spec.space, spec.resolution, spec.shape, spec.spacing, spec.lowest_center) for spec in GRID_REGISTRY if spec.space in CONNECTOME_SPACES)
NATIVE_SPACE = "native"


class GridMatch(NamedTuple):
    """Result of :func:`detect_grid_candidates`: the registry grid and the spaces it may hold (the first is the default)."""

    grid_id: str
    candidates: tuple
    family: str
    resolution: object
    ambiguous: bool

    @property
    def space(self):
        """Default space (the first candidate)."""
        return self.candidates[0]


def _world_box(shape, affine, spacing_tolerance):
    """Shape, voxel size and lowest voxel center of a grid along the world x, y and z axes.

    :return: (world_shape tuple, world_spacing array, lowest array), or None when the voxel axes
      are not aligned with the world axes.
    """
    shape = tuple(int(n) for n in tuple(shape)[:3])
    affine = np.asarray(affine, dtype=np.float64)
    if len(shape) != 3 or affine.shape != (4, 4):
        return None
    linear = affine[:3, :3]
    world_axes = np.argmax(np.abs(linear), axis=0)  # world axis of each voxel axis
    if len(set(world_axes.tolist())) != 3:
        return None
    steps = linear[world_axes, np.arange(3)]
    off_axis = linear.copy()
    off_axis[world_axes, np.arange(3)] = 0.0
    if np.abs(off_axis).max() > spacing_tolerance * np.abs(steps).max():
        return None
    world_shape = np.zeros(3, dtype=np.int64)
    world_spacing = np.zeros(3)
    lowest = affine[:3, 3].copy()
    for axis in range(3):
        world_axis = world_axes[axis]
        world_shape[world_axis] = shape[axis]
        world_spacing[world_axis] = abs(steps[axis])
        if steps[axis] < 0:
            lowest[world_axis] += steps[axis] * (shape[axis] - 1)
    return tuple(int(n) for n in world_shape), world_spacing, lowest


def _box_matches(box, shape, spacing, lowest_center, origin_tolerance, spacing_tolerance):
    world_shape, world_spacing, lowest = box
    return (
        world_shape == tuple(shape)
        and bool(np.all(np.abs(world_spacing - spacing) <= spacing_tolerance * spacing))
        and bool(np.all(np.abs(lowest - np.asarray(lowest_center)) <= origin_tolerance))
    )


def detect_grid(shape, affine, origin_tolerance=1.0, spacing_tolerance=1e-3):
    """Identify a connectome template grid (see :data:`KNOWN_GRIDS`).

    The voxel axes must be aligned with the RAS axes (any order and directions), the voxel size and
    the number of voxels along each world axis must match, and the lowest voxel center must be
    within ``origin_tolerance`` mm of the template's. Ambiguous grids report their default space
    (193x229x193 is "MNI152NLin2009cAsym"); :func:`detect_grid_candidates` lists the alternatives
    and recognizes more grids.

    :return: (space, resolution), e.g. ("MNI152NLin2009cAsym", 2), or ("native", None).
    """
    box = _world_box(shape, affine, spacing_tolerance)
    if box is None:
        return NATIVE_SPACE, None
    for space, resolution, grid_shape, spacing, origin in KNOWN_GRIDS:
        if _box_matches(box, grid_shape, spacing, origin, origin_tolerance, spacing_tolerance):
            return space, resolution
    return NATIVE_SPACE, None


def detect_grid_candidates(shape, affine, origin_tolerance=1.0, spacing_tolerance=1e-3):
    """Identify any registry grid (see :data:`GRID_REGISTRY`) and the spaces it may hold.

    Same matching rules as :func:`detect_grid` (any axis order and directions, e.g. radiological
    storage). Header-only and numpy-only.

    :return: :class:`GridMatch` (candidates, the first being the default; ``ambiguous`` when the
      grid alone does not identify the space, so the user should be asked once), or None.
    """
    box = _world_box(shape, affine, spacing_tolerance)
    if box is None:
        return None
    for spec in GRID_REGISTRY + tuple(_RUNTIME_GRIDS):
        if _box_matches(box, spec.shape, spec.spacing, spec.lowest_center, origin_tolerance, spacing_tolerance):
            return GridMatch(spec.grid_id, spec.candidates, spec.family, spec.resolution, spec.ambiguous)
    return None


# Runtime grids: the grids of the user's own MNI templates (Slicer Studio, A-10). They are matched after the
# built-in registry grids (a built-in grid always wins) by detect_grid_candidates and detect_space(path_or_geometry),
# and GRID_BY_ID resolves their ids ("user:<id>"); detect_grid (connectome grids only) is unchanged.
_RUNTIME_GRIDS = []


def register_grid(spec):
    """Add a runtime grid (a :class:`GridSpec`, e.g. from :func:`grid_spec_from_affine`); a grid with the same id is
    replaced. Built-in registry ids cannot be replaced.

    :raises ValueError: for the id of a built-in registry grid.
    """
    if spec.grid_id in {built_in.grid_id for built_in in GRID_REGISTRY}:
        raise ValueError(f"{spec.grid_id!r} is a built-in grid")
    unregister_grid(spec.grid_id)
    _RUNTIME_GRIDS.append(spec)
    GRID_BY_ID[spec.grid_id] = spec
    return spec


def unregister_grid(grid_id):
    """Remove a runtime grid added with :func:`register_grid`; return whether it was registered."""
    found = [spec for spec in _RUNTIME_GRIDS if spec.grid_id == grid_id]
    for spec in found:
        _RUNTIME_GRIDS.remove(spec)
    if found and grid_id not in {built_in.grid_id for built_in in GRID_REGISTRY}:
        GRID_BY_ID.pop(grid_id, None)
    return bool(found)


def registered_grids():
    """The runtime grids, in registration order."""
    return tuple(_RUNTIME_GRIDS)


def grid_spec_from_affine(grid_id, shape, affine, space, family=None, spacing_tolerance=1e-3):
    """A :class:`GridSpec` of an axis-aligned grid with isotropic voxels (any storage order, e.g. LAS), or None when
    the voxel axes are not aligned with the world axes or the voxels are not cubic (such a grid cannot be registered).

    :param space: the space the grid holds (its single candidate).
    :param family: its family (default :func:`space_family` of ``space``).
    """
    box = _world_box(shape, affine, spacing_tolerance)
    if box is None:
        return None
    world_shape, world_spacing, lowest = box
    spacing = float(world_spacing.mean())
    if np.abs(world_spacing - spacing).max() > spacing_tolerance * spacing:
        return None
    x_flipped = bool(np.asarray(affine, dtype=np.float64)[0, :3][int(np.argmax(np.abs(np.asarray(affine)[0, :3])))] < 0)
    return GridSpec(str(grid_id), world_shape, spacing, tuple(round(float(v), 6) for v in lowest), (str(space),),
                    family or space_family(space), x_flipped=x_flipped)


def _shape_and_affine(source):
    """(shape, affine) of a NIfTI path, a :class:`Grid`, a (shape, affine) pair, a mapping or object
    with ``shape`` and ``affine``, a Slicer volume node or a vtkOrientedImageData.
    """
    if isinstance(source, (str, os.PathLike)):
        header = read_nifti_header(source)
        return header["shape"][:3], header["affine"]
    if isinstance(source, dict):
        return source["shape"], source["affine"]
    if isinstance(source, (tuple, list)) and len(source) == 2:
        return source
    if hasattr(source, "shape") and hasattr(source, "affine"):
        return source.shape, source.affine
    if hasattr(source, "GetIJKToRASMatrix") and hasattr(source, "GetImageData"):  # vtkMRMLVolumeNode
        import vtk

        matrix = vtk.vtkMatrix4x4()
        source.GetIJKToRASMatrix(matrix)
        image = source.GetImageData()
        if image is None:
            raise ValueError("The volume has no image data")
        return image.GetDimensions(), _vtk_matrix_to_array(matrix)
    if hasattr(source, "GetImageToWorldMatrix") and hasattr(source, "GetDimensions"):  # vtkOrientedImageData
        import vtk

        matrix = vtk.vtkMatrix4x4()
        source.GetImageToWorldMatrix(matrix)
        extent = source.GetExtent()
        shift = np.eye(4)
        shift[:3, 3] = extent[0::2]  # voxel (0, 0, 0) of the array is the first voxel of the extent
        return source.GetDimensions(), _vtk_matrix_to_array(matrix) @ shift
    raise TypeError(f"Cannot read a voxel grid from {type(source).__name__}")


def _vtk_matrix_to_array(matrix):
    return np.array([[matrix.GetElement(row, column) for column in range(4)] for row in range(4)])


def detect_space(shape, affine=None, origin_tolerance=1.0):
    """Template space of a grid.

    Two forms:

    - ``detect_space(shape, affine)``: "MNI152NLin6Asym", "MNI152NLin2009cAsym" or "native"
      (see :func:`detect_grid`; unchanged).
    - ``detect_space(path_or_geometry)``: (space, resolution) of any registry grid, ambiguous grids
      giving their default space, or None when the grid is not a standard one (native space).
      The argument is a NIfTI path (only the header is read), a :class:`Grid`, a (shape, affine)
      pair, a mapping or object with ``shape`` and ``affine``, a Slicer volume node or a
      vtkOrientedImageData (e.g. a segmentation's reference geometry).
    """
    if affine is None:
        shape, affine = _shape_and_affine(shape)
        match = detect_grid_candidates(shape, affine, origin_tolerance)
        return None if match is None else (match.space, match.resolution)
    return detect_grid(shape, affine, origin_tolerance)[0]


# Space families: templates of one family share their coordinate convention closely enough that
# images are compared without a warp; across families (e.g. 2009c and NLin6, which differ by a few
# millimetres in subcortical structures) a template-family transform is needed.
_FAMILY_PATTERNS = (
    ("fsl6", re.compile(r"^(mni152nlin6(asym|sym)?|fsl(mni152)?|mni152fsl)$")),
    ("2009b", re.compile(r"^mni152nlin2009b(asym|sym)$")),
    ("2009", re.compile(r"^(mni152nlin2009[ac](asym|sym)|mni2009[ac](asym|sym)?|icbm2009[ac](asym|sym)?)$")),
    ("spm", re.compile(r"^(mni152lin|spm|spmmni|icbm152lin|avg152)$")),
    ("colin", re.compile(r"^(mnicolin27|colin27|colin|ch2)$")),
    ("pediatric", re.compile(r"^(mnipediatric(asym|sym)?|nihpd.*|mniinfant|uncinfant.*)$")),
    ("native", re.compile(r"^(native|scanner|orig|individual|acpc|t1w|t2w|flair|pd|pdw|t2starw|ct|dwi|bold|anat)$")),
)
SPACE_FAMILIES = ("fsl6", "2009", "2009b", "spm", "colin", "pediatric", "native", "unknown")


def space_family(space):
    """Family of a space label: "fsl6", "2009", "2009b", "spm", "colin", "pediatric", "native" or "unknown".

    Labels are matched without case and without a "tpl-" or "space-" prefix, e.g.
    "MNI152NLin6Asym" -> "fsl6", "MNI152NLin2009aSym" -> "2009", "MNIColin27" -> "colin",
    "T1w" or "native" -> "native" (subject spaces), anything else (including None) -> "unknown".
    """
    if not space:
        return "unknown"
    label = str(space).strip().lower()
    for prefix in ("tpl-", "space-"):
        if label.startswith(prefix):
            label = label[len(prefix):]
    label = label.replace("_", "").replace(" ", "")
    for family, pattern in _FAMILY_PATTERNS:
        if pattern.match(label):
            return family
    return "unknown"


def same_family(a, b):
    """True when two space labels belong to the same template family (images are comparable without
    a template-family transform). Native (subject) and unknown spaces never share a family.
    """
    family = space_family(a)
    return family not in ("native", "unknown") and family == space_family(b)


def family_notice(seed_space, connectome_space):
    """The note for a lesion (seed) on a template of another MNI family than the connectome, or "" when
    the families match, either space is not known or the connectome's family is not known (the text
    recorded in run.json, qa.json, the sidecars and methods; the Map module shows the same note).
    "MNI" (an MNI template of no known family, e.g. one of the user's own templates) is "MNI (other)".
    """
    if not seed_space or not connectome_space:
        return ""
    connectome_family = space_family(connectome_space)
    if connectome_family in ("native", "unknown") or space_family(seed_space) == connectome_family:
        return ""
    shown = "MNI (other)" if seed_space == "MNI" else seed_space
    return (f"The lesion is on {shown}, the connectome on {connectome_space}: the template families are a few millimetres apart, "
            "so the map is approximate near subcortical structures.")


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

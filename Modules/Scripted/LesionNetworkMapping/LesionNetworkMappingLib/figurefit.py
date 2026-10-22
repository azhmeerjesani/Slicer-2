"""Figure-fit numerics for "Trace from published figure": landmark fits, cut planes and fit files.

numpy and the standard library only; no Slicer, VTK or Qt imports. SPEC_LNM_ADDENDUM §3.10 (figure
L/R convention, affine fits need at least 4 landmark pairs, RMS and leave-one-out RMS always shown);
lesion-input.md §1.5-1.7 (QuickNII-style cut planes, orbitomeatal tilt).

Coordinates
-----------

- **Figure pixels** ``(u, v)``: ``u`` is the column (0 at the left), ``v`` the row (0 at the top) of
  the cropped figure, pixel centres at integers, as in numpy/Pillow arrays. They are also the
  ``(i, j)`` voxel indices of the figure volume (``k = 0``).
- **Convention** (:func:`convention_matrix`): a radiological figure (patient left on the image right,
  the default of clinical figures) is mirrored, ``u -> -u``, before the fit; a neurological figure is
  not. Fits never contain a reflection (:func:`umeyama_similarity` and :func:`affine_2d` refuse
  them, :func:`validate_record` refuses a recorded one), so the L/R convention is always an explicit, recorded choice and never something a fit decides silently. For
  sagittal figures "radiological" means anterior on the image left.
- **Cut plane** (:func:`cut_plane`): the template plane the figure is fitted to, as the N, T and P
  arguments of ``vtkMRMLSliceNode.SetSliceToRASByNTP(..., CutPlane.ORIENTATION)``. Pitch tilts the top
  edge of the view towards +Z of Slicer's standard slice (superior for axial, anterior for coronal,
  left for sagittal); roll tilts the right edge of the view the same way. Both are limited to
  ±:data:`MAX_TILT_DEG` and turn about the point at ``z_mm`` on the plane's axis.
- **Plane coordinates** ``(a, b)`` in mm: along ``e_u = -x_axis`` and ``e_v = -y_axis`` of the slice,
  that is, the directions of the figure's image right and image down when a neurological figure lies
  on the plane without rotation (:meth:`CutPlane.to_plane`, :meth:`CutPlane.to_ras`).
- **Fit**: a 3x3 homogeneous 2D matrix from mirrored figure pixels to plane coordinates:
  :func:`umeyama_similarity` (4 DOF, at least 2 pairs) or :func:`affine_2d` (6 DOF, at least
  :data:`MIN_AFFINE_PAIRS` = 4 pairs, because 3 pairs fit exactly with RMS 0 and prove nothing).
- :func:`figure_ijk_to_ras` composes plane frame, fit and convention into the figure volume's 4x4
  IJK-to-RAS matrix (the parent transform when the volume's own IJKToRAS is the identity, see
  :func:`parent_transform`).

Residuals (:func:`rms`, :func:`loo_rms`, :func:`residual_level`) are in plane mm. They measure landmark
consistency, not lesion accuracy (:data:`RESIDUAL_NOTE`); the user interface says so.

Fit files (:func:`make_record`, :func:`validate_record`, :func:`write_fit_json`,
:func:`read_fit_json`): JSON format :data:`FORMAT` version :data:`VERSION`, stored next to the crop as
``sources/<record_id>/fig-<k>_fit.json``. Unknown keys are preserved; a newer version is refused.
"""

import datetime
import hashlib
import json
import math
import os
import re
from typing import NamedTuple

import numpy as np

FORMAT = "slicer-studio-figure-fit"
VERSION = 1
PLANES = ("axial", "coronal", "sagittal")
CONVENTIONS = ("radiological", "neurological")
DEFAULT_CONVENTION = "radiological"
MODELS = ("similarity", "affine")
#: Minimum landmark pairs per model (an affine fit of 3 pairs is exact and has RMS 0)
MIN_PAIRS = {"similarity": 2, "affine": 4}
MIN_AFFINE_PAIRS = MIN_PAIRS["affine"]
#: Pitch and roll of the cut plane are limited to this many degrees
MAX_TILT_DEG = 20.0
#: Typical brain width in mm used for the initial pixel size
BRAIN_WIDTH_MM = 140.0
#: Residual levels: green below the first value, amber up to the second, red above (mm)
RESIDUAL_GREEN_MM = 2.0
RESIDUAL_RED_MM = 4.0
RESIDUAL_NOTE = "measures landmark consistency, not lesion accuracy"
#: A mirrored fit with at most this fraction of the RMS suggests the wrong L/R convention
MIRROR_SUSPECT_RATIO = 0.5

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_TOLERANCE = 1e-6
_RESIDUAL_TOLERANCE_MM = 1e-4

#: Slicer's standard slice axes per plane (vtkMRMLSliceNode::Get*SliceToRASMatrix): x (screen right),
#: y (screen up), z, and the RAS axis that z_mm is measured along
_STANDARD_AXES = {
    "axial": ((-1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0), 2),
    "coronal": ((-1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, 1.0, 0.0), 1),
    "sagittal": ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0), (-1.0, 0.0, 0.0), 0),
}

_SUGGESTED_LANDMARKS = {
    "axial": (
        "midline anterior",
        "midline posterior",
        "left frontal-horn tip",
        "right frontal-horn tip",
        "4th ventricle",
        "left sylvian fissure",
        "right sylvian fissure",
    ),
    "coronal": (
        "midline superior",
        "midline inferior",
        "left lateral ventricle roof",
        "right lateral ventricle roof",
        "left sylvian fissure",
        "right sylvian fissure",
        "left temporal lobe base",
        "right temporal lobe base",
    ),
    "sagittal": (
        "frontal pole",
        "occipital pole",
        "corpus callosum genu",
        "corpus callosum splenium",
        "vertex",
        "4th ventricle",
        "pons anterior",
    ),
}


class FigureFitError(ValueError):
    """Invalid fit record; ``errors`` lists every problem found."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


# Conventions, plane and suggestions


def _check_plane(plane):
    if plane not in PLANES:
        raise ValueError(f"plane must be one of {', '.join(PLANES)}, not {plane!r}")


def _check_convention(convention):
    if convention not in CONVENTIONS:
        raise ValueError(f"convention must be one of {', '.join(CONVENTIONS)}, not {convention!r}")


def convention_matrix(convention):
    """3x3 homogeneous 2D matrix applied to figure pixels before the fit.

    "radiological" mirrors the pixel u axis (u -> -u); "neurological" is the identity.
    """
    _check_convention(convention)
    return np.diag([-1.0 if convention == "radiological" else 1.0, 1.0, 1.0])


def suggested_landmarks(plane):
    """Landmark names suggested for a figure plane (anatomy usually visible on such a figure)."""
    _check_plane(plane)
    return list(_SUGGESTED_LANDMARKS[plane])


def initial_pixel_size(brain_width_px):
    """Initial mm per pixel before any landmark: a brain about :data:`BRAIN_WIDTH_MM` wide."""
    width = float(brain_width_px)
    if not math.isfinite(width) or width <= 0:
        raise ValueError(f"brain width must be a positive number of pixels, not {brain_width_px!r}")
    return BRAIN_WIDTH_MM / width


# Fits


def _pairs(src, dst, min_pairs, what):
    src = np.asarray(src, dtype=float)
    dst = np.asarray(dst, dtype=float)
    if src.ndim != 2 or src.shape[1] != 2 or dst.shape != src.shape:
        raise ValueError(f"{what}: source and destination must be matching (n, 2) arrays, not {src.shape} and {dst.shape}")
    if not (np.isfinite(src).all() and np.isfinite(dst).all()):
        raise ValueError(f"{what}: landmark coordinates must be finite")
    if len(src) < min_pairs:
        raise ValueError(f"{what} needs at least {min_pairs} landmark pairs, got {len(src)}")
    return src, dst


def umeyama_similarity(src_xy, dst_xy):
    """Least-squares 2D similarity (scale, rotation, translation; 4 DOF) from src to dst (Umeyama 1991).

    Reflections are refused: the rotation always has determinant +1 (the L/R convention is applied to
    the figure before the fit, :func:`convention_matrix`). Needs at least 2 distinct source points.

    :return: 3x3 homogeneous matrix.
    """
    src, dst = _pairs(src_xy, dst_xy, MIN_PAIRS["similarity"], "similarity fit")
    mean_src = src.mean(axis=0)
    mean_dst = dst.mean(axis=0)
    src_c = src - mean_src
    dst_c = dst - mean_dst
    variance = float((src_c**2).sum()) / len(src)
    if variance <= 1e-12 * max(1.0, float(np.abs(src).max())) ** 2:
        raise ValueError("similarity fit: the figure landmarks coincide")
    covariance = dst_c.T @ src_c / len(src)
    u, singular, vt = np.linalg.svd(covariance)
    sign = np.diag([1.0, 1.0 if np.linalg.det(u) * np.linalg.det(vt) >= 0 else -1.0])
    rotation = u @ sign @ vt
    scale = float(np.trace(np.diag(singular) @ sign)) / variance
    if scale <= 0:
        raise ValueError("similarity fit: no proper rotation matches the landmarks (check the L/R convention)")
    matrix = np.eye(3)
    matrix[:2, :2] = scale * rotation
    matrix[:2, 2] = mean_dst - scale * rotation @ mean_src
    return matrix


def affine_2d(src, dst):
    """Least-squares 2D affine (6 DOF) from src to dst.

    Raises ValueError with fewer than :data:`MIN_AFFINE_PAIRS` (4) pairs, because 3 pairs fit exactly
    with RMS 0, when the source points are collinear, or when the best fit contains a reflection
    (determinant <= 0): like :func:`umeyama_similarity`, the fit never decides the L/R convention.

    :return: 3x3 homogeneous matrix.
    """
    return _affine_lstsq(src, dst, MIN_AFFINE_PAIRS)


def _affine_lstsq(src, dst, min_pairs):
    """Affine least squares with at least ``min_pairs`` pairs (3 only for the folds of :func:`loo_rms`)."""
    src, dst = _pairs(src, dst, min_pairs, "affine fit")
    design = np.column_stack([src, np.ones(len(src))])
    solution, _residuals, rank, _singular = np.linalg.lstsq(design, dst, rcond=None)
    if rank < 3:
        raise ValueError("affine fit: the figure landmarks are collinear")
    matrix = np.eye(3)
    matrix[:2, :] = solution.T
    if np.linalg.det(matrix[:2, :2]) <= 0:
        raise ValueError("affine fit: the landmarks need a reflection (check the L/R convention)")
    return matrix


def fit_2d(kind, src, dst):
    """Fit of ``kind`` ("similarity" or "affine") from src to dst; see the fit functions."""
    if kind == "similarity":
        return umeyama_similarity(src, dst)
    if kind == "affine":
        return affine_2d(src, dst)
    raise ValueError(f"model must be one of {', '.join(MODELS)}, not {kind!r}")


def apply_2d(matrix, points):
    """Apply a 3x3 homogeneous 2D matrix to (n, 2) points."""
    points = np.asarray(points, dtype=float).reshape(-1, 2)
    matrix = np.asarray(matrix, dtype=float)
    return points @ matrix[:2, :2].T + matrix[:2, 2]


def residuals(model, src, dst):
    """Distance from each fitted source point to its destination (same units as dst)."""
    src = np.asarray(src, dtype=float).reshape(-1, 2)
    dst = np.asarray(dst, dtype=float).reshape(-1, 2)
    return np.linalg.norm(apply_2d(model, src) - dst, axis=1)


def rms(model, src, dst):
    """Root-mean-square landmark residual of the 3x3 fit ``model``."""
    distances = residuals(model, src, dst)
    if not len(distances):
        raise ValueError("rms needs at least one landmark pair")
    return float(math.sqrt(float(np.mean(distances**2))))


def loo_rms(kind, src, dst):
    """Leave-one-out RMS: each pair's residual under the fit of all the other pairs.

    Affine folds may have 3 pairs (exact fits that still predict the held-out pair). Returns None when
    a fold cannot be fitted (similarity with fewer than 3 pairs, affine with fewer than 4, or
    degenerate or reflected folds). This algorithm is part of fit-file version :data:`VERSION`
    (:func:`validate_record` recomputes it); changing it needs a new version.
    """
    src = np.asarray(src, dtype=float).reshape(-1, 2)
    dst = np.asarray(dst, dtype=float).reshape(-1, 2)
    if kind not in MODELS:
        raise ValueError(f"model must be one of {', '.join(MODELS)}, not {kind!r}")
    if len(src) < MIN_PAIRS[kind] + (1 if kind == "similarity" else 0):
        return None
    held_out = []
    for index in range(len(src)):
        keep = np.arange(len(src)) != index
        try:
            if kind == "similarity":
                model = umeyama_similarity(src[keep], dst[keep])
            else:
                model = _affine_lstsq(src[keep], dst[keep], 3)
        except ValueError:
            return None
        held_out.append(residuals(model, src[index : index + 1], dst[index : index + 1])[0])
    return float(math.sqrt(float(np.mean(np.square(held_out)))))


def mirror_suspected(src, dst):
    """True when the mirrored figure fits clearly better: the L/R convention is probably wrong.

    Compares the similarity RMS with that of the u-mirrored source (needs at least 3 pairs).
    """
    src = np.asarray(src, dtype=float).reshape(-1, 2)
    dst = np.asarray(dst, dtype=float).reshape(-1, 2)
    if len(src) < 3:
        return False
    try:
        direct = rms(umeyama_similarity(src, dst), src, dst)
        mirrored_src = src * [-1.0, 1.0]
        mirrored = rms(umeyama_similarity(mirrored_src, dst), mirrored_src, dst)
    except ValueError:
        return False
    return mirrored < MIRROR_SUSPECT_RATIO * direct


class ResidualLevel(NamedTuple):
    """Traffic-light level of a landmark residual and its explanatory text."""

    level: str  # "green", "amber", "red" or "none"
    text: str


def residual_level(rms_mm):
    """Green below 2 mm, amber 2 to 4 mm, red above 4 mm ("none" when no residual is available)."""
    if rms_mm is None or not math.isfinite(float(rms_mm)):
        return ResidualLevel("none", f"No residual yet; the residual {RESIDUAL_NOTE}.")
    # classify the value as displayed (one decimal), so that "2.0 mm" is never green
    value = round(float(rms_mm), 1)
    if value < RESIDUAL_GREEN_MM:
        level = "green"
    elif value <= RESIDUAL_RED_MM:
        level = "amber"
    else:
        level = "red"
    return ResidualLevel(level, f"RMS {value:.1f} mm — the residual {RESIDUAL_NOTE}.")


def residual_chip(rms_mm, loo_rms_mm, n_pairs):
    """Chip text and level, for example ("RMS 1.6 mm · LOO 2.3 mm (5 pairs)", ResidualLevel).

    The level follows the larger of RMS and LOO RMS, because the LOO RMS is the honest estimate.
    """
    parts = ["RMS n/a" if rms_mm is None else f"RMS {float(rms_mm):.1f} mm", "LOO n/a" if loo_rms_mm is None else f"LOO {float(loo_rms_mm):.1f} mm"]
    known = [float(value) for value in (rms_mm, loo_rms_mm) if value is not None]
    level = residual_level(max(known) if known else None)
    return f"{' · '.join(parts)} ({int(n_pairs)} pair{'' if int(n_pairs) == 1 else 's'})", level


# Cut planes


class CutPlane(NamedTuple):
    """Template plane for ``vtkMRMLSliceNode.SetSliceToRASByNTP(*normal, *transverse, *origin, CutPlane.ORIENTATION)``.

    ``normal`` is the slice z axis (x_axis cross y_axis) and ``transverse`` the slice x axis (screen
    right); both are unit RAS vectors. ``origin`` is the RAS point the plane turns about.
    """

    normal: np.ndarray
    transverse: np.ndarray
    origin: np.ndarray

    #: Orientation argument of SetSliceToRASByNTP that gives x = T, y = N x T, z = N
    ORIENTATION = 0

    @property
    def x_axis(self):
        """Slice x axis (screen right)."""
        return np.asarray(self.transverse, dtype=float)

    @property
    def y_axis(self):
        """Slice y axis (screen up)."""
        return np.cross(self.normal, self.transverse)

    @property
    def e_u(self):
        """RAS direction of plane coordinate a (image right of an unrotated neurological figure)."""
        return -self.x_axis

    @property
    def e_v(self):
        """RAS direction of plane coordinate b (image down of an unrotated figure)."""
        return -self.y_axis

    def slice_to_ras(self):
        """4x4 SliceToRAS that SetSliceToRASByNTP sets (see :func:`ntp_slice_to_ras`)."""
        return ntp_slice_to_ras(self.normal, self.transverse, self.origin, self.ORIENTATION)

    def to_plane(self, ras_points):
        """(n, 3) RAS points to (n, 2) plane coordinates (a, b) and their (n,) signed distances to the plane."""
        offsets = np.asarray(ras_points, dtype=float).reshape(-1, 3) - self.origin
        return np.column_stack([offsets @ self.e_u, offsets @ self.e_v]), offsets @ np.asarray(self.normal, dtype=float)

    def to_ras(self, plane_points):
        """(n, 2) plane coordinates (a, b) to (n, 3) RAS points on the plane."""
        points = np.asarray(plane_points, dtype=float).reshape(-1, 2)
        return self.origin + np.outer(points[:, 0], self.e_u) + np.outer(points[:, 1], self.e_v)


def cut_plane(plane, z_mm=0.0, pitch_deg=0.0, roll_deg=0.0):
    """QuickNII-style template cut plane: a standard slice at ``z_mm`` tilted by pitch and roll.

    ``z_mm`` is the RAS coordinate along the plane's axis (S for axial, A for coronal, R for sagittal).
    With zero pitch and roll the slice axes equal Slicer's standard orientation (the slice normal may
    point the other way, which does not change the displayed plane). Pitch turns the plane about the
    slice x axis, then roll about the tilted slice y axis, both through the origin; positive values
    move the top (pitch) or right (roll) edge of the view towards +Z of the standard slice.

    :raises ValueError: unknown plane, non-finite values, or pitch or roll beyond ±:data:`MAX_TILT_DEG`.
    """
    _check_plane(plane)
    values = {"z_mm": z_mm, "pitch_deg": pitch_deg, "roll_deg": roll_deg}
    for name, value in values.items():
        if not math.isfinite(float(value)):
            raise ValueError(f"{name} must be finite")
    for name in ("pitch_deg", "roll_deg"):
        if abs(float(values[name])) > MAX_TILT_DEG + 1e-9:
            raise ValueError(f"{name} must be within ±{MAX_TILT_DEG:g}°, not {float(values[name]):g}")
    x_axis, y_axis, z_axis, ras_index = _STANDARD_AXES[plane]
    x_axis, y_axis, z_axis = (np.asarray(axis, dtype=float) for axis in (x_axis, y_axis, z_axis))
    # pitch: about x; the top edge (y) moves towards z
    cos_p, sin_p = math.cos(math.radians(float(pitch_deg))), math.sin(math.radians(float(pitch_deg)))
    y_axis, z_axis = cos_p * y_axis + sin_p * z_axis, cos_p * z_axis - sin_p * y_axis
    # roll: about the tilted y; the right edge (x) moves towards z
    cos_r, sin_r = math.cos(math.radians(float(roll_deg))), math.sin(math.radians(float(roll_deg)))
    x_axis = cos_r * x_axis + sin_r * z_axis
    origin = np.zeros(3)
    origin[ras_index] = float(z_mm)
    normal = np.cross(x_axis, y_axis)
    return CutPlane(normal / np.linalg.norm(normal), x_axis / np.linalg.norm(x_axis), origin)


def ntp_slice_to_ras(normal, transverse, position, orientation=0):
    """numpy copy of ``vtkMRMLSliceNode::SetSliceToRASByNTP`` (Libs/MRML/Core/vtkMRMLSliceNode.cxx)."""
    n = np.asarray(normal, dtype=float)
    t = np.asarray(transverse, dtype=float)
    c = np.cross(n, t)
    t = np.cross(c, n)
    n, t, c = (vector / np.linalg.norm(vector) for vector in (n, t, c))
    columns = {0: (t, c, n), 1: (-c, -n, t), 2: (t, -n, c)}
    if orientation not in columns:
        raise ValueError(f"orientation must be 0 (axial), 1 (sagittal) or 2 (coronal), not {orientation!r}")
    matrix = np.eye(4)
    matrix[:3, :3] = np.column_stack(columns[orientation])
    matrix[:3, 3] = np.asarray(position, dtype=float)
    return matrix


# Figure placement


class FigureFit(NamedTuple):
    """A landmark fit of one figure panel to its cut plane (plane coordinates in mm)."""

    model: str
    matrix: np.ndarray  # 3x3: mirrored figure pixels -> plane (a, b) mm
    rms_mm: float
    loo_rms_mm: float | None
    residuals_mm: np.ndarray
    out_of_plane_mm: np.ndarray  # distance of each template landmark to the cut plane
    mirror_suspected: bool

    @property
    def n_pairs(self):
        """Number of landmark pairs."""
        return len(self.residuals_mm)


def fit_figure(fig_px, tpl_ras, plane_frame, convention=DEFAULT_CONVENTION, model="similarity"):
    """Fit figure landmarks (pixels) to template landmarks (RAS, on the cut plane).

    The figure points are mirrored according to ``convention`` first; the template points are
    projected on ``plane_frame`` (a :class:`CutPlane`), and their distances to it are returned so that
    a caller can warn about landmarks placed off the plane.
    """
    if model not in MODELS:
        raise ValueError(f"model must be one of {', '.join(MODELS)}, not {model!r}")
    mirrored = apply_2d(convention_matrix(convention), fig_px)
    plane_points, distances = plane_frame.to_plane(tpl_ras)
    if len(plane_points) != len(mirrored):
        raise ValueError(f"{len(mirrored)} figure landmarks but {len(plane_points)} template landmarks")
    matrix = fit_2d(model, mirrored, plane_points)
    return FigureFit(
        model=model,
        matrix=matrix,
        rms_mm=rms(matrix, mirrored, plane_points),
        loo_rms_mm=loo_rms(model, mirrored, plane_points),
        residuals_mm=residuals(matrix, mirrored, plane_points),
        out_of_plane_mm=np.abs(distances),
        mirror_suspected=mirror_suspected(mirrored, plane_points),
    )


def initial_fit(size_px, pixel_spacing, convention=DEFAULT_CONVENTION):
    """3x3 placement before any landmark: ``pixel_spacing`` mm per pixel, crop centre at the plane origin.

    Like a fit, it applies to the pixels mirrored according to ``convention``.
    """
    width, height = (float(value) for value in size_px)
    spacing = float(pixel_spacing)
    if not (width > 0 and height > 0 and math.isfinite(spacing) and spacing > 0):
        raise ValueError("initial placement needs a positive crop size and pixel spacing")
    centre = apply_2d(convention_matrix(convention), [(width - 1) / 2.0, (height - 1) / 2.0])[0]
    matrix = np.diag([spacing, spacing, 1.0])
    matrix[:2, 2] = -spacing * centre
    return matrix


def figure_ijk_to_ras(fit, plane_frame, pixel_spacing=None, convention=DEFAULT_CONVENTION, size_px=None):
    """4x4 matrix from the figure volume's IJK (i = column, j = row from the top, k) to RAS.

    :param fit: a :class:`FigureFit`, a 3x3 matrix (mirrored pixels to plane mm) or None for the
      initial placement (``pixel_spacing`` mm per pixel, see :func:`initial_pixel_size`, centred with
      ``size_px`` = (width, height)).
    :param plane_frame: the :class:`CutPlane` the figure lies on.
    :param convention: "radiological" (mirrors u before the fit) or "neurological".

    The k column is the plane normal scaled to the fit's mean pixel size, so that the single-slice
    volume has square-ish voxels. When the figure volume's own IJKToRAS is the identity this matrix
    is its parent transform; otherwise use :func:`parent_transform`.
    """
    if fit is None:
        if pixel_spacing is None or size_px is None:
            raise ValueError("the initial placement needs pixel_spacing and size_px")
        matrix_2d = initial_fit(size_px, pixel_spacing, convention)
    else:
        matrix_2d = np.asarray(fit.matrix if isinstance(fit, FigureFit) else fit, dtype=float)
    if matrix_2d.shape != (3, 3) or not np.isfinite(matrix_2d).all():
        raise ValueError("the fit must be a finite 3x3 matrix")
    linear = matrix_2d @ convention_matrix(convention)
    in_plane = np.column_stack([plane_frame.e_u, plane_frame.e_v])  # 3x2
    thickness = math.sqrt(abs(float(np.linalg.det(linear[:2, :2]))))
    if thickness <= 0:
        raise ValueError("the fit is degenerate (zero area)")
    out = np.eye(4)
    out[:3, :2] = in_plane @ linear[:2, :2]
    out[:3, 2] = np.asarray(plane_frame.normal, dtype=float) * thickness
    out[:3, 3] = np.asarray(plane_frame.origin, dtype=float) + in_plane @ linear[:2, 2]
    return out


def parent_transform(ijk_to_ras, volume_ijk_to_ras):
    """Parent transform P with P @ volume_ijk_to_ras == ijk_to_ras (for a volume whose IJKToRAS is not the identity)."""
    return np.asarray(ijk_to_ras, dtype=float) @ np.linalg.inv(np.asarray(volume_ijk_to_ras, dtype=float))


# Fit files


def image_sha256(path, block=1 << 20):
    """Hex sha256 of an image file's bytes (the saved crop)."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(block):
            digest.update(chunk)
    return digest.hexdigest()


def _now_iso():
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _floats(values):
    return [float(value) for value in np.asarray(values, dtype=float).ravel()]


def _matrix_rows(matrix):
    return [_floats(row) for row in np.asarray(matrix, dtype=float)]


def make_record(
    fit,
    plane,
    convention,
    landmarks,
    cut,
    *,
    image_sha256,
    crop_px,
    template,
    tracer,
    z_mm=0.0,
    pitch_deg=0.0,
    roll_deg=0.0,
    created=None,
):
    """Fit record (format :data:`FORMAT` v:data:`VERSION`) of one figure panel; validated before return.

    :param fit: the :class:`FigureFit`.
    :param landmarks: [{"name", "fig_px": [u, v], "tpl_ras": [r, a, s]}] in fit order.
    :param cut: the :class:`CutPlane` from ``cut_plane(plane, z_mm, pitch_deg, roll_deg)``.
    :param crop_px: [x, y, width, height] of the crop in the original figure, in pixels.
    :param template: {"id", "res" (mm), "sha256" (or None)}.
    """
    record = {
        "format": FORMAT,
        "version": VERSION,
        "image_sha256": image_sha256,
        "crop_px": [int(value) for value in crop_px],
        "plane": plane,
        "convention": convention,
        "landmarks": [{"name": str(item["name"]), "fig_px": _floats(item["fig_px"]), "tpl_ras": _floats(item["tpl_ras"])} for item in landmarks],
        "model": fit.model,
        "fit_2d": _matrix_rows(fit.matrix),
        "matrix": _matrix_rows(figure_ijk_to_ras(fit, cut, convention=convention)),
        "rms_mm": float(fit.rms_mm),
        "loo_rms_mm": None if fit.loo_rms_mm is None else float(fit.loo_rms_mm),
        "cut_plane": {
            "origin_ras": _floats(cut.origin),
            "normal": _floats(cut.normal),
            "transverse": _floats(cut.transverse),
            "z_mm": float(z_mm),
            "pitch_deg": float(pitch_deg),
            "roll_deg": float(roll_deg),
        },
        "template": {"id": template.get("id"), "res": template.get("res"), "sha256": template.get("sha256")},
        "tracer": tracer,
        "created": created or _now_iso(),
    }
    validate_record(record)
    return record


def _is_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an integer too large for a float
        return False


def _vector(value, length):
    return isinstance(value, list) and len(value) == length and all(_is_number(item) for item in value)


def _matrix(value, rows, columns):
    return isinstance(value, list) and len(value) == rows and all(_vector(row, columns) for row in value)


def validate_record(record):
    """Check a fit record; raises :class:`FigureFitError` listing every problem.

    Besides types and ranges, the record must be self-consistent: the cut plane equals
    ``cut_plane(plane, z_mm, pitch_deg, roll_deg)``, ``matrix`` equals the matrix rebuilt from
    ``fit_2d``, the cut plane and the convention, ``fit_2d`` has no reflection (and is a scaled
    rotation for a similarity), the model has enough landmark pairs, and ``rms_mm`` and ``loo_rms_mm``
    equal the residuals recomputed from the landmarks.
    """
    if not isinstance(record, dict):
        raise FigureFitError(["the fit record must be a JSON object"])
    errors = []
    if record.get("format") != FORMAT:
        errors.append(f"format must be {FORMAT!r}")
    version = record.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        errors.append("version must be a positive integer")
    elif version > VERSION:
        errors.append(f"version {version} was written by a newer Slicer Studio (this one reads version {VERSION})")
    if errors:
        raise FigureFitError(errors)
    if not (isinstance(record.get("image_sha256"), str) and _HEX64.match(record["image_sha256"])):
        errors.append("image_sha256 must be 64 lower-case hex digits")
    crop = record.get("crop_px")
    if not (isinstance(crop, list) and len(crop) == 4 and all(isinstance(v, int) and not isinstance(v, bool) for v in crop) and crop[0] >= 0 and crop[1] >= 0 and crop[2] > 0 and crop[3] > 0):
        errors.append("crop_px must be [x, y, width, height] integers with a positive size")
    plane = record.get("plane")
    if plane not in PLANES:
        errors.append(f"plane must be one of {', '.join(PLANES)}")
    convention = record.get("convention")
    if convention not in CONVENTIONS:
        errors.append(f"convention must be one of {', '.join(CONVENTIONS)}")
    landmarks = record.get("landmarks")
    if not isinstance(landmarks, list):
        errors.append("landmarks must be a list")
        landmarks = []
    for index, item in enumerate(landmarks):
        if not (isinstance(item, dict) and isinstance(item.get("name"), str) and _vector(item.get("fig_px"), 2) and _vector(item.get("tpl_ras"), 3)):
            errors.append(f"landmark {index + 1} must have a name, fig_px [u, v] and tpl_ras [r, a, s] (finite numbers)")
    model = record.get("model")
    if model not in MODELS:
        errors.append(f"model must be one of {', '.join(MODELS)}")
    elif len(landmarks) < MIN_PAIRS[model]:
        errors.append(f"a {model} fit needs at least {MIN_PAIRS[model]} landmark pairs, the record has {len(landmarks)}")
    if not _matrix(record.get("fit_2d"), 3, 3):
        errors.append("fit_2d must be a 3x3 matrix of finite numbers")
    else:
        fit_2d = np.asarray(record["fit_2d"], dtype=float)
        linear = fit_2d[:2, :2]
        if not np.allclose(fit_2d[2], [0, 0, 1]):
            errors.append("fit_2d must have the last row [0, 0, 1]")
        if np.linalg.det(linear) <= 0:
            errors.append("fit_2d must not contain a reflection (the L/R convention is recorded in convention)")
        elif model == "similarity" and not np.allclose([linear[0, 0], linear[0, 1]], [linear[1, 1], -linear[1, 0]], rtol=1e-6, atol=1e-9 * float(np.abs(linear).max())):
            errors.append("fit_2d of a similarity must be a scaled rotation")
    if not _matrix(record.get("matrix"), 4, 4):
        errors.append("matrix must be a 4x4 matrix of finite numbers")
    elif not np.allclose(np.asarray(record["matrix"])[3], [0, 0, 0, 1]):
        errors.append("matrix must have the last row [0, 0, 0, 1]")
    if not (_is_number(record.get("rms_mm")) and record["rms_mm"] >= 0):
        errors.append("rms_mm must be a non-negative number")
    loo = record.get("loo_rms_mm")
    if loo is not None and not (_is_number(loo) and loo >= 0):
        errors.append("loo_rms_mm must be a non-negative number or null")
    cut = record.get("cut_plane")
    cut_ok = isinstance(cut, dict)
    if not cut_ok:
        errors.append("cut_plane must be an object")
    else:
        for key in ("origin_ras", "normal", "transverse"):
            if not _vector(cut.get(key), 3):
                errors.append(f"cut_plane.{key} must be 3 finite numbers")
                cut_ok = False
        for key in ("z_mm", "pitch_deg", "roll_deg"):
            if not _is_number(cut.get(key)):
                errors.append(f"cut_plane.{key} must be a finite number")
                cut_ok = False
        for key in ("pitch_deg", "roll_deg"):
            if _is_number(cut.get(key)) and abs(cut[key]) > MAX_TILT_DEG + 1e-9:
                errors.append(f"cut_plane.{key} must be within ±{MAX_TILT_DEG:g}°")
                cut_ok = False
    template = record.get("template")
    if not (isinstance(template, dict) and isinstance(template.get("id"), str) and template["id"] and _is_number(template.get("res")) and template["res"] > 0):
        errors.append("template must have a non-empty id and a positive res (mm)")
    elif template.get("sha256") is not None and not (isinstance(template["sha256"], str) and _HEX64.match(template["sha256"])):
        errors.append("template.sha256 must be 64 lower-case hex digits or null")
    if not isinstance(record.get("tracer"), str):
        errors.append("tracer must be text")
    created = record.get("created")
    try:
        created_ok = "T" in created and datetime.datetime.fromisoformat(created).tzinfo is not None
    except (TypeError, ValueError):
        created_ok = False
    if not created_ok:
        errors.append("created must be an ISO 8601 date and time with a UTC offset")
    if not errors and cut_ok:
        expected = cut_plane(plane, cut["z_mm"], cut["pitch_deg"], cut["roll_deg"])
        for key, value in (("origin_ras", expected.origin), ("normal", expected.normal), ("transverse", expected.transverse)):
            if not np.allclose(cut[key], value, atol=_TOLERANCE):
                errors.append(f"cut_plane.{key} does not match plane, z_mm, pitch_deg and roll_deg")
        if not errors:
            try:
                rebuilt = figure_ijk_to_ras(np.asarray(record["fit_2d"]), expected, convention=convention)
            except ValueError as error:
                errors.append(f"fit_2d: {error}")
            else:
                if not np.allclose(record["matrix"], rebuilt, atol=1e-5):
                    errors.append("matrix does not match fit_2d, cut_plane and convention")
        if not errors:
            errors.extend(_residual_errors(record, expected))
    if errors:
        raise FigureFitError(errors)
    return record


def _residual_errors(record, cut):
    """rms_mm and loo_rms_mm must be the residuals of the recorded landmarks (auditable, not just stated)."""
    mirrored = apply_2d(convention_matrix(record["convention"]), [item["fig_px"] for item in record["landmarks"]])
    plane_points = cut.to_plane([item["tpl_ras"] for item in record["landmarks"]])[0]
    errors = []
    expected_rms = rms(np.asarray(record["fit_2d"], dtype=float), mirrored, plane_points)
    if not math.isclose(record["rms_mm"], expected_rms, rel_tol=1e-6, abs_tol=_RESIDUAL_TOLERANCE_MM):
        errors.append(f"rms_mm {record['rms_mm']:g} does not match the landmarks and fit_2d ({expected_rms:g} mm)")
    expected_loo = loo_rms(record["model"], mirrored, plane_points)
    loo = record["loo_rms_mm"]
    if (loo is None) != (expected_loo is None) or (loo is not None and not math.isclose(loo, expected_loo, rel_tol=1e-6, abs_tol=_RESIDUAL_TOLERANCE_MM)):
        errors.append(f"loo_rms_mm {loo} does not match the landmarks ({expected_loo})")
    return errors


def record_cut_plane(record):
    """The :class:`CutPlane` of a validated record."""
    cut = record["cut_plane"]
    return cut_plane(record["plane"], cut["z_mm"], cut["pitch_deg"], cut["roll_deg"])


def write_fit_json(path, record):
    """Validate and write a fit record (UTF-8 JSON) through a temporary file and an atomic rename."""
    validate_record(record)
    text = json.dumps(record, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    path = os.fspath(path)
    tmp_path = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def read_fit_json(path):
    """Read and validate a fit record; raises :class:`FigureFitError` (a ValueError) when invalid."""
    with open(path, encoding="utf-8") as f:
        try:
            record = json.load(f)
        except UnicodeDecodeError as error:
            raise FigureFitError([f"not UTF-8 text: {error}"]) from error
        except (json.JSONDecodeError, RecursionError) as error:
            raise FigureFitError([f"not valid JSON: {error}"]) from error
    return validate_record(record)

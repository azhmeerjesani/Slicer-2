"""Synthetic native-space scans and lesions for the normalization tests (A-03, A-03b, A-03c).

The bundled MNI152NLin2009cAsym 2 mm brain is the truth. A native scan is that template seen
through a known similarity transform (10 degrees about z, 5 degrees about x, translation
(8, -6, 4) mm, scale 1.08) on a 1.5 mm grid. CSF-like lesions (a core at 0.15 x white matter
with a 2 mm rim at 0.45 x white matter, like chronic infarcts on T1w) are painted in template
space and pushed to the native grid through the same transform, with exact partial-volume
fractions (3 x 3 x 3 sub-samples per voxel), so the template-space truth is known on the 1 mm grid.

Variants: a whole-head scan (when the whole-head template is found), a header rotation (the
voxels stay, the head is tilted in world space), and a FLAIR-like contrast moved by a rigid motion
(a second scan of the same subject for the two-step path).

Usage::

    from lnm_normalize_fixture import make_subject
    subject = make_subject(folder, "sub-01")   # {"scan", "lesion", "truth", "truth_ml", ...}
"""

import math
import os
import sys

import numpy as np

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

from LesionNetworkMappingLib import nifti, normalize  # noqa: E402
from LesionNetworkMappingLib.geometry import voxel_to_world, world_to_voxel  # noqa: E402

TEMPLATE = "MNI152NLin2009cAsym"
SPACING_MM = 1.5
SCALE = 1.08
ROTATION_DEG = (5.0, 0.0, 10.0)  # about x, y, z (RAS)
TRANSLATION_MM = (8.0, -6.0, 4.0)
# Lesions in template space: (centre RAS mm, radius mm)
LESIONS = {
    "large": ((-26.0, -10.0, 24.0), 12.0),  # left corona radiata
    "small": ((-22.0, 2.0, 4.0), 5.0),  # left deep grey/white matter
}
CORE_FACTOR = 0.15
RIM_FACTOR = 0.45
RIM_MM = 2.0
SUBSAMPLES = 3


def rotation(degrees_xyz):
    """3x3 rotation about x, then y, then z (RAS, degrees)."""
    out = np.eye(3)
    for axis, degrees in enumerate(degrees_xyz):
        angle = math.radians(degrees)
        c, s = math.cos(angle), math.sin(angle)
        i, j = [(1, 2), (2, 0), (0, 1)][axis]
        step = np.eye(3)
        step[i, i], step[i, j], step[j, i], step[j, j] = c, -s, s, c
        out = step @ out
    return out


def template_to_native(scale=SCALE, rotation_deg=ROTATION_DEG, translation=TRANSLATION_MM):
    """4x4 RAS matrix mapping template points to native points (the known truth)."""
    matrix = np.eye(4)
    matrix[:3, :3] = scale * rotation(rotation_deg)
    matrix[:3, 3] = translation
    return matrix


def rigid(rotation_deg, translation):
    matrix = np.eye(4)
    matrix[:3, :3] = rotation(rotation_deg)
    matrix[:3, 3] = translation
    return matrix


def template_files(head=False, dirs=()):
    """(image path, mask path) of the bundled template; with ``head`` the whole-head image (None when not found)."""
    found = normalize.find_template(TEMPLATE, dirs=dirs)
    image = found["files"]["head"] if head else found["files"]["fixed"]
    return image, found["files"]["mask"]


def _distance_to_lesions(points_tpl, lesions):
    """Signed distance (mm) of template points to the nearest lesion sphere surface (negative inside)."""
    distance = np.full(points_tpl.shape[0], np.inf)
    for centre, radius in lesions:
        distance = np.minimum(distance, np.linalg.norm(points_tpl - np.asarray(centre), axis=1) - radius)
    return distance


def _fractions(shape, affine, to_template, lesions):
    """Partial-volume fraction of the lesion spheres on a grid (``to_template`` maps grid world points to template points)."""
    out = np.zeros(shape, dtype=np.float32)
    offsets = (np.arange(SUBSAMPLES) + 0.5) / SUBSAMPLES - 0.5
    sub = np.stack(np.meshgrid(offsets, offsets, offsets, indexing="ij"), -1).reshape(-1, 3)
    from_template = np.linalg.inv(to_template)
    for centre, radius in lesions:
        # bounding box of the sphere on the grid
        corners = np.array([[centre[0] + sx * radius, centre[1] + sy * radius, centre[2] + sz * radius] for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)])
        ijk = world_to_voxel(voxel_to_world(corners, from_template), affine)
        lo = np.maximum(np.floor(ijk.min(0)).astype(int) - 2, 0)
        hi = np.minimum(np.ceil(ijk.max(0)).astype(int) + 3, shape)
        if np.any(hi <= lo):
            continue
        grid = np.stack(np.meshgrid(*[np.arange(lo[a], hi[a]) for a in range(3)], indexing="ij"), -1).reshape(-1, 3).astype(np.float64)
        samples = (grid[:, None, :] + sub[None, :, :]).reshape(-1, 3)
        points = voxel_to_world(voxel_to_world(samples, affine), to_template)
        inside = (np.linalg.norm(points - np.asarray(centre), axis=1) <= radius).reshape(len(grid), -1).mean(axis=1)
        block = out[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]]
        np.maximum(block, inside.reshape(tuple(hi - lo)).astype(np.float32), out=block)
    return out


def _native_grid(template_shape, template_affine, to_native, spacing):
    corners = np.array([[i, j, k] for i in (0, template_shape[0] - 1) for j in (0, template_shape[1] - 1) for k in (0, template_shape[2] - 1)], dtype=np.float64)
    world = voxel_to_world(voxel_to_world(corners, template_affine), to_native)
    lo, hi = world.min(0) - 6.0, world.max(0) + 6.0
    shape = tuple(int(n) for n in np.ceil((hi - lo) / spacing) + 1)
    affine = np.diag([spacing, spacing, spacing, 1.0])
    affine[:3, 3] = lo
    return shape, affine


def _contrast(values, white_matter, kind):
    if kind == "T1w":
        return values
    # FLAIR-like: CSF stays dark, grey matter brighter than white matter, a gliotic rim bright
    u = values / white_matter
    flair = np.where(u < 0.35, u * 0.5, 1.4 - 0.8 * u)
    return np.clip(flair, 0.0, None) * white_matter


def make_native(folder, name, lesions=("large",), head=False, contrast="T1w", motion=None, header_rotation_deg=None, spacing=SPACING_MM, template_dirs=()):
    """Write a native scan and its fractional lesion; return their paths and the transform.

    :param lesions: keys of :data:`LESIONS` painted into the scan and the lesion file.
    :param head: use the whole-head template (None is returned when it is not found).
    :param contrast: "T1w" or "FLAIR" (FLAIR-like intensity mapping).
    :param motion: optional (rotation_deg xyz, translation mm) rigid motion after the known transform.
    :param header_rotation_deg: optional (about x, about z) rotation of the image headers about the
      image centre (the head is tilted in world space; the voxels are unchanged).
    :return: {"scan", "lesion", "to_native" (template -> native world, before the header rotation),
      "native_ml"} or None.
    """
    from scipy import ndimage

    image_path, _ = template_files(head, template_dirs)
    if image_path is None:
        return None
    template, template_affine, _ = nifti.read_nifti(image_path)
    template = np.asarray(template, dtype=np.float64)
    to_native = template_to_native()
    if motion is not None:
        to_native = rigid(*motion) @ to_native
    shape, affine = _native_grid(template.shape, template_affine, to_native, spacing)
    to_template = np.linalg.inv(to_native)
    selected = [LESIONS[key] for key in lesions]

    # Template intensities at the native voxel centres (linear), with the lesion painted in template space
    ijk = np.indices(shape, dtype=np.float64).reshape(3, -1).T
    points = voxel_to_world(voxel_to_world(ijk, affine), to_template)
    values = ndimage.map_coordinates(template, world_to_voxel(points, template_affine).T, order=1, mode="constant", cval=0.0)
    brain = values > 0
    white_matter = float(np.percentile(template[template > 0], 85))
    if selected:
        distance = _distance_to_lesions(points, selected)
        weight_in = np.clip(-distance / RIM_MM, 0.0, 1.0)
        target = RIM_FACTOR * white_matter * (1 - weight_in) + CORE_FACTOR * white_matter * weight_in
        blend = np.clip(0.5 - distance, 0.0, 1.0) * brain
        values = values * (1 - blend) + target * blend
    values = _contrast(values, white_matter, contrast)
    scan = np.round(values).astype(np.int16).reshape(shape)
    fraction = _fractions(shape, affine, to_template, selected) if selected else np.zeros(shape, dtype=np.float32)

    if header_rotation_deg is not None:
        centre = voxel_to_world((np.asarray(shape) - 1) / 2.0, affine)
        turn = np.eye(4)
        turn[:3, :3] = rotation((header_rotation_deg[0], 0.0, header_rotation_deg[1]))
        shift, back = np.eye(4), np.eye(4)
        shift[:3, 3] = centre
        back[:3, 3] = -centre
        affine = shift @ turn @ back @ affine

    os.makedirs(folder, exist_ok=True)
    modality = "T1w" if contrast == "T1w" else "FLAIR"
    scan_path = os.path.join(folder, f"{name}_{modality}.nii.gz")
    lesion_path = os.path.join(folder, f"{name}_space-{modality}_desc-lesion_mask.nii.gz")
    nifti.write_nifti(scan_path, scan, affine, sform_code=nifti.XFORM_SCANNER, qform_code=nifti.XFORM_SCANNER)
    nifti.write_nifti(lesion_path, fraction, affine, sform_code=nifti.XFORM_SCANNER, qform_code=nifti.XFORM_SCANNER)
    voxel_ml = abs(float(np.linalg.det(affine[:3, :3]))) / 1000.0
    return {"scan": scan_path, "lesion": lesion_path, "to_native": to_native, "native_ml": float(fraction.sum()) * voxel_ml, "shape": shape}


def truth(lesions=("large",), template_id=TEMPLATE):
    """Template-space truth on the template's 1 mm grid: (fraction array [i, j, k], affine)."""
    grid = normalize.template_grid(template_id)
    return _fractions(tuple(grid.shape), grid.affine, np.eye(4), [LESIONS[key] for key in lesions]), grid.affine


def make_subject(folder, name="sub-01", lesions=("large",), **options):
    """A native subject plus its truth: make_native(...) with "truth" (fraction on the 1 mm grid) and "truth_ml"."""
    subject = make_native(folder, name, lesions, **options)
    if subject is None:
        return None
    fraction, affine = truth(lesions)
    subject["truth"] = fraction
    subject["truth_affine"] = affine
    subject["truth_ml"] = float((fraction >= 0.5).sum()) / 1000.0
    return subject


def dice(a, b):
    """Dice of two masks (arrays; values >= 0.5 count as inside)."""
    a = np.asarray(a) >= 0.5
    b = np.asarray(b) >= 0.5
    total = int(a.sum()) + int(b.sum())
    return 2.0 * int((a & b).sum()) / total if total else float("nan")

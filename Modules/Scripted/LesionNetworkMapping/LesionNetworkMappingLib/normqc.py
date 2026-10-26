"""Quality-control numerics for the lesion-aware normalization of native scans to a template.

Registration of a lesioned brain to a template can shrink, move or smear the lesion, and image
similarity alone does not show it: in the measurements behind A-03 the normalized cross-correlation
of a small-lesion collapse was 0.774, as good as a successful run. The lesion volume and Jacobian
checks below are therefore needed together with the similarity:

- :func:`ncc_xl` / :func:`nmi_xl`: similarity of the template and the warped scan inside the template
  brain mask minus the dilated lesion;
- :func:`brain_mask_dice`: Dice of the warped brain mask and the template brain mask;
- :func:`lesion_volume_change`: the MNI/native lesion volume ratio *divided by the global brain-volume
  ratio*, so that small (atrophic) or large heads are not flagged only because the template is a
  different size (the raw ratio of a head scaled by 0.85 is 1.63, its normalized ratio 1.0);
- :func:`jacobian_from_displacement`, :func:`log_jacobian_lesion_minus_brain` and
  :func:`folding_pct`: local volume change in the lesion relative to the brain, and folding;
- :func:`pct_outside_brain`, :func:`pct_in_ventricles`: where the warped lesion lies;
- :func:`qc_mosaic_png`: a 3 x 3 PNG (axial, coronal and sagittal rows through the lesion centroid) in
  neurological orientation (the patient's left on the image left) with L/R, A/P and S/I stamps;
- :func:`evaluate` and :func:`normqc_json`: levels, a suggestion ("review" or "looks-ok", never a
  verdict) and the ``sub-X_desc-normqc.json`` content.

Volumes are numpy arrays indexed [i, j, k] in file order with a 4x4 voxel-to-RAS affine (see nifti.py);
all images passed to one function share the template grid. Masks may be boolean, binary or fractional
(fractions >= 0.5 count as inside). The thresholds (:data:`THRESHOLDS`) were calibrated with the A-03
measurements on MRHead with synthetic chronic-stroke lesions (:data:`CALIBRATION`); thresholds without a
measurement are marked as such in :data:`UNCALIBRATED`.
"""

import math

import numpy as np

from . import export, geometry

NORMQC_FORMAT = "slicer-lnm-normqc"
NORMQC_VERSION = 1

LEVEL_OK = "ok"
LEVEL_AMBER = "amber"
LEVEL_NA = "not_available"
SUGGEST_REVIEW = "review"
SUGGEST_OK = "looks-ok"

# Suggestion rules (A-03 measurements; "review" when any metric is outside its range)
THRESHOLDS = {
    "ncc_xl_min_affine": 0.40,
    "ncc_xl_min_bspline": 0.50,
    "normalized_ratio_min": 0.70,
    "normalized_ratio_max": 1.40,
    "abs_logJ_lesion_minus_brain_max": round(math.log(1.4), 4),
    "folding_pct_max": 0.25,
    "outside_brain_pct_max": 5.0,
    "brain_dice_min": 0.80,
    "ventricle_pct_max": 10.0,
}
# Ranges measured by A-03 (successful vs failed registrations)
CALIBRATION = {
    "ncc_xl": {"good": [0.60, 0.80], "failed": [0.02, 0.37]},
    "normalized_ratio": {"good": [0.83, 1.19], "collapse": [0.09, 0.23]},
    "abs_logJ_lesion_minus_brain": {"good": [0.0, 0.13], "collapse": [1.26, 9.4]},
    "folding_pct": {"good": [0.0, 0.04], "failed": [1.5, 4.6]},
    "outside_brain_pct": {"good": [0.0, 0.1], "misaligned": [6.6, 16.7]},
}
UNCALIBRATED = ("brain_dice_min", "ventricle_pct_max")
SUGGESTION_NOTE = "The suggestion is a prompt to look at the QC images, not a verdict; the reviewer decides pass or fail."

LESION_THRESHOLD = 0.5  # fraction at which a voxel counts as lesion (binary lesion, outlines)
JACOBIAN_LESION_THRESHOLD = 0.25  # lesion fraction used for the mean log-Jacobian (2 mm grid)
SIMILARITY_DILATE_VOXELS = 2  # lesion dilation excluded from the similarity region
NMI_BINS = 64
MIDLINE_MM = 2.0  # |x| of the lesion centroid below which the side is "midline"

# Mosaic colours (RGB)
COLOR_LESION = (255, 0, 0)
COLOR_BRAIN_EDGE = (0, 255, 255)
COLOR_SCAN_EDGE = (255, 176, 0)
COLOR_TEXT = (255, 255, 255)
COLOR_BACKGROUND = (0, 0, 0)
PLANES = ("axial", "coronal", "sagittal")
# Stamps on each cell's (left, right, top, bottom) edge: neurological orientation, Slicer's sagittal view
PLANE_STAMPS = {
    "axial": ("L", "R", "A", "P"),
    "coronal": ("L", "R", "S", "I"),
    "sagittal": ("A", "P", "S", "I"),
}
TITLE_HEIGHT = 16
CELL_GAP = 12  # keeps the stamps of neighbouring cells apart
COLOR_SEPARATOR = (72, 72, 72)


# Masks and volumes


def _as_mask(image, threshold=LESION_THRESHOLD):
    """Boolean mask of an image: booleans as they are, other values >= threshold."""
    image = np.asarray(image)
    if image.dtype == bool:
        return image
    return image >= threshold


def _as_fraction(image):
    """Fractional (0..1, float64) version of a lesion image; booleans become 0/1."""
    return np.clip(np.asarray(image, dtype=np.float64), 0.0, 1.0)


def _check_same_shape(*images):
    shapes = {tuple(np.shape(image)[:3]) for image in images if image is not None}
    if len(shapes) > 1:
        raise ValueError(f"Images must share the template grid, got shapes {sorted(shapes)}")


def _voxel_ml(affine=None, voxel_volume_mm3=None):
    if voxel_volume_mm3 is None:
        if affine is None:
            raise ValueError("Pass an affine or a voxel volume")
        voxel_volume_mm3 = abs(float(np.linalg.det(np.asarray(affine, dtype=np.float64)[:3, :3])))
    return float(voxel_volume_mm3) / 1000.0


def volume_ml(image, affine=None, voxel_volume_mm3=None, mode="fraction", threshold=LESION_THRESHOLD):
    """Volume of a mask in mL: the sum of fractions ("fraction") or the count of voxels >= threshold ("binary")."""
    if mode == "fraction":
        total = float(_as_fraction(image).sum())
    elif mode == "binary":
        total = float(np.count_nonzero(_as_mask(image, threshold)))
    else:
        raise ValueError(f"Unknown volume mode: {mode}")
    return total * _voxel_ml(affine, voxel_volume_mm3)


def affine_scale(affine):
    """|det| of the linear part of an affine: the volume scale factor of the mapping."""
    return abs(float(np.linalg.det(np.asarray(affine, dtype=np.float64)[:3, :3])))


def native_brain_ml_from_affine(template_brain_ml, template_to_scan_affine):
    """Brain volume of the native scan estimated from the global (affine) registration.

    The affine maps template points to scan points (the direction of BRAINSFit's fixed-to-moving
    output transform), so a template region of volume V covers V x |det| in the scan.
    """
    return float(template_brain_ml) * affine_scale(template_to_scan_affine)


# Metrics


def brain_mask_dice(warped_brain_mask, template_mask):
    """Dice coefficient of the warped brain mask and the template brain mask (same grid); NaN when both are empty."""
    _check_same_shape(warped_brain_mask, template_mask)
    a = _as_mask(warped_brain_mask)
    b = _as_mask(template_mask)
    total = int(np.count_nonzero(a)) + int(np.count_nonzero(b))
    if total == 0:
        return float("nan")
    return 2.0 * int(np.count_nonzero(a & b)) / total


def similarity_region(template_mask, lesion=None, dilate_voxels=SIMILARITY_DILATE_VOXELS):
    """Template brain mask minus the lesion (any fraction > 0) dilated by ``dilate_voxels`` (6-connected)."""
    region = _as_mask(template_mask).copy()
    if lesion is not None:
        _check_same_shape(template_mask, lesion)
        excluded = np.asarray(lesion) > 0
        if dilate_voxels > 0 and excluded.any():
            from scipy import ndimage

            excluded = ndimage.binary_dilation(excluded, structure=ndimage.generate_binary_structure(excluded.ndim, 1), iterations=int(dilate_voxels))
        region &= ~excluded
    return region


def ncc(a, b, mask=None):
    """Normalized cross-correlation (Pearson r) of two images inside a mask; NaN when either is constant."""
    _check_same_shape(a, b, mask)
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if mask is not None:
        region = _as_mask(mask)
        a, b = a[region], b[region]
    else:
        a, b = a.ravel(), b.ravel()
    if a.size < 2:
        return float("nan")
    a = a - a.mean()
    b = b - b.mean()
    denominator = math.sqrt(float(np.dot(a, a)) * float(np.dot(b, b)))
    if denominator == 0:
        return float("nan")
    return float(np.dot(a, b) / denominator)


def nmi(a, b, mask=None, bins=NMI_BINS):
    """Normalized mutual information (H(A) + H(B)) / H(A, B) (Studholme 1999; 1 = independent, 2 = identical).

    Each image is binned in ``bins`` equal bins between its minimum and maximum inside the mask.
    """
    _check_same_shape(a, b, mask)
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if mask is not None:
        region = _as_mask(mask)
        a, b = a[region], b[region]
    else:
        a, b = a.ravel(), b.ravel()
    if a.size < 2:
        return float("nan")
    joint, _, _ = np.histogram2d(a, b, bins=int(bins))
    p_joint = joint / joint.sum()

    def entropy(p):
        p = p[p > 0]
        return float(-(p * np.log(p)).sum())

    h_joint = entropy(p_joint)
    if h_joint == 0:
        return float("nan")
    return (entropy(p_joint.sum(axis=1)) + entropy(p_joint.sum(axis=0))) / h_joint


def ncc_xl(template, warped, template_mask, lesion=None, dilate_voxels=SIMILARITY_DILATE_VOXELS):
    """NCC of the template and the warped scan inside the template brain minus the dilated lesion."""
    return ncc(template, warped, similarity_region(template_mask, lesion, dilate_voxels))


def nmi_xl(template, warped, template_mask, lesion=None, dilate_voxels=SIMILARITY_DILATE_VOXELS, bins=NMI_BINS):
    """NMI of the template and the warped scan inside the template brain minus the dilated lesion."""
    return nmi(template, warped, similarity_region(template_mask, lesion, dilate_voxels), bins)


def lesion_volume_change(native_lesion_ml, mni_lesion_ml, native_brain_ml, mni_brain_ml, thresholds=None):
    """Lesion volume change through the normalization, corrected for the global head-size scaling.

    - raw_ratio = MNI lesion / native lesion volume;
    - scale_ratio = MNI brain / native brain volume (the global scaling of the registration);
    - normalized_ratio = raw_ratio / scale_ratio: 1 when the lesion scaled like the brain.

    With the affine of the registration, pass ``native_brain_ml = template_brain_ml x |det(affine)|``
    (:func:`native_brain_ml_from_affine`) and ``mni_brain_ml = template_brain_ml``.

    :return: dict with raw_ratio, scale_ratio, normalized_ratio (NaN when undefined) and level
      ("amber" when normalized_ratio is outside the thresholds' 0.70-1.40, "not_available" when NaN).
    """
    thresholds = THRESHOLDS if thresholds is None else {**THRESHOLDS, **thresholds}
    values = [float(v) for v in (native_lesion_ml, mni_lesion_ml, native_brain_ml, mni_brain_ml)]
    native_lesion, mni_lesion, native_brain, mni_brain = values
    raw = mni_lesion / native_lesion if native_lesion > 0 else float("nan")
    scale = mni_brain / native_brain if native_brain > 0 and mni_brain > 0 else float("nan")
    normalized = raw / scale if math.isfinite(raw) and math.isfinite(scale) else float("nan")
    return {
        "raw_ratio": raw,
        "scale_ratio": scale,
        "normalized_ratio": normalized,
        "level": _range_level(normalized, thresholds["normalized_ratio_min"], thresholds["normalized_ratio_max"]),
    }


def jacobian_from_displacement(field, spacing=None, affine=None, chunk=16):
    """Jacobian determinant det(I + du/dx) of a displacement field u (mm) by central differences.

    :param field: (X, Y, Z, 3) displacement in mm. With ``spacing``, the components are along the voxel
      axes; with ``affine``, they are in the world frame of the affine (for example RAS for a RAS
      affine; ITK fields are LPS and need an LPS affine).
    :param spacing: voxel size along the three axes in mm (axis-aligned grids).
    :param affine: 4x4 voxel-to-world affine (any orientation); only its linear part is used.
    :param chunk: slices along the first axis processed at a time (bounds the memory use).
    :return: float64 array (X, Y, Z); J <= 0 means folding.
    """
    field = np.asarray(field)
    if field.ndim != 4 or field.shape[3] != 3:
        raise ValueError(f"A displacement field must have shape (X, Y, Z, 3), got {field.shape}")
    if min(field.shape[:3]) < 2:
        raise ValueError("A displacement field needs at least 2 voxels along every axis")
    if affine is not None:
        linear = np.asarray(affine, dtype=np.float64)[:3, :3]
    elif spacing is not None:
        linear = np.diag(np.asarray(spacing, dtype=np.float64).reshape(3))
    else:
        raise ValueError("Pass the spacing or the affine of the displacement field")
    inverse = np.linalg.inv(linear)  # d(index)/d(world)
    n0 = field.shape[0]
    jac = np.empty(field.shape[:3], dtype=np.float64)
    for start in range(0, n0, int(chunk)):
        end = min(n0, start + int(chunk))
        lo, hi = max(0, start - 1), min(n0, end + 1)
        sub = np.asarray(field[lo:hi], dtype=np.float64)
        keep = slice(start - lo, start - lo + (end - start))
        # grad[c][a] = du_c / d(index_a); M[c][d] = sum_a grad[c][a] * inverse[a][d] = du_c / dx_d
        grad = [[g[keep] for g in np.gradient(sub[..., c])] for c in range(3)]
        m = [[sum(grad[c][a] * inverse[a, d] for a in range(3)) + (1.0 if c == d else 0.0) for d in range(3)] for c in range(3)]
        jac[start:end] = (
            m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
            - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
        )
    return jac


def log_jacobian_lesion_minus_brain(jac, lesion, brain, lesion_threshold=JACOBIAN_LESION_THRESHOLD):
    """Mean log J inside the lesion (fraction >= lesion_threshold) minus the mean log J inside the brain.

    Voxels with J <= 0 (folding, see :func:`folding_pct`) have no logarithm and are left out of both
    means. Negative: the lesion shrank more than the brain on average. NaN when a region is empty.
    """
    _check_same_shape(jac, lesion, brain)
    jac = np.asarray(jac, dtype=np.float64)
    valid = np.isfinite(jac) & (jac > 0)
    in_lesion = _as_mask(lesion, lesion_threshold) & valid
    in_brain = _as_mask(brain) & valid
    if not in_lesion.any() or not in_brain.any():
        return float("nan")
    return float(np.log(jac[in_lesion]).mean() - np.log(jac[in_brain]).mean())


def folding_pct(jac, brain):
    """Percentage of brain voxels with J <= 0 (folded deformation)."""
    _check_same_shape(jac, brain)
    region = _as_mask(brain)
    count = int(np.count_nonzero(region))
    if count == 0:
        return float("nan")
    return 100.0 * int(np.count_nonzero(np.asarray(jac)[region] <= 0)) / count


def pct_outside_brain(lesion, template_mask):
    """Percentage of the lesion (fraction-weighted) outside the template brain mask; NaN for an empty lesion."""
    _check_same_shape(lesion, template_mask)
    fraction = _as_fraction(lesion)
    total = float(fraction.sum())
    if total <= 0:
        return float("nan")
    return 100.0 * float(fraction[~_as_mask(template_mask)].sum()) / total


def pct_in_ventricles(lesion, ventricles, labels=None, lesion_space=None, ventricle_space=None):
    """Percentage of the lesion (fraction-weighted) inside a ventricle prior of the same template family.

    :param ventricles: a label image (with ``labels``, the ventricle label values; without, every
      non-zero label) or a probability/binary map (>= 0.5 counts as ventricle).
    :param lesion_space, ventricle_space: space labels; when both are given they must belong to the same
      template family (geometry.same_family), otherwise ValueError: a prior of another family is not
      aligned with the lesion.
    """
    if lesion_space is not None and ventricle_space is not None and not geometry.same_family(lesion_space, ventricle_space):
        raise ValueError(f"The ventricle prior ({ventricle_space}) is not in the lesion's template family ({lesion_space})")
    _check_same_shape(lesion, ventricles)
    ventricles = np.asarray(ventricles)
    if labels is not None:
        region = np.isin(ventricles, list(labels))
    elif ventricles.dtype == bool or np.issubdtype(ventricles.dtype, np.integer):
        region = ventricles != 0
    else:
        region = ventricles >= 0.5
    fraction = _as_fraction(lesion)
    total = float(fraction.sum())
    if total <= 0:
        return float("nan")
    return 100.0 * float(fraction[region].sum()) / total


def outline(mask, connectivity=1):
    """Outline of a mask (any dimension): the mask XOR its erosion (voxels outside count as background).

    The result is boolean and can be shown as a labelmap outline.
    """
    from scipy import ndimage

    mask = _as_mask(mask)
    if not mask.any():
        return np.zeros(mask.shape, dtype=bool)
    eroded = ndimage.binary_erosion(mask, structure=ndimage.generate_binary_structure(mask.ndim, connectivity), border_value=0)
    return mask ^ eroded


def centroid_mm(lesion, affine):
    """Fraction-weighted centroid of the lesion in world mm (None for an empty lesion)."""
    fraction = _as_fraction(lesion)
    total = float(fraction.sum())
    if total <= 0:
        return None
    indices = np.indices(fraction.shape, dtype=np.float64).reshape(3, -1)
    ijk = (indices * fraction.reshape(-1)).sum(axis=1) / total
    return [float(v) for v in geometry.voxel_to_world(ijk[None, :], affine)[0]]


def lesion_side(centroid, midline_mm=MIDLINE_MM):
    """Side of a lesion from its RAS centroid: "L" (x < -midline_mm), "R" (x > midline_mm) or "midline"."""
    if centroid is None:
        return None
    x = float(centroid[0])
    if x < -midline_mm:
        return "L"
    if x > midline_mm:
        return "R"
    return "midline"


# Levels and suggestion


def _finite(value):
    return value is not None and isinstance(value, (int, float, np.integer, np.floating)) and math.isfinite(float(value))


def _range_level(value, low=None, high=None):
    if not _finite(value):
        return LEVEL_NA
    value = float(value)
    if (low is not None and value < low) or (high is not None and value > high):
        return LEVEL_AMBER
    return LEVEL_OK


def evaluate(metrics, method=None, thresholds=None):
    """Levels of the metrics and a suggestion ("review" or "looks-ok"), with the reasons.

    :param metrics: dict with any of ncc_xl, normalized_ratio, logJ_lesion_minus_brain, folding_pct,
      outside_brain_pct, brain_dice, ventricle_pct (missing or NaN metrics are "not_available").
    :param method: optional dict: stage ("affine" or "bspline"; default "bspline" when a log-Jacobian is
      given), dilate_mm (< 0 means cost-function masking disabled) or masking (False), cross_modality
      and two_step, scan_only, attempts (count or list). These add reasons without a metric.
    :return: {"levels": {metric: level}, "suggestion": ..., "reasons": [text, ...]}. The suggestion is
      never a verdict (:data:`SUGGESTION_NOTE`).
    """
    thresholds = THRESHOLDS if thresholds is None else {**THRESHOLDS, **thresholds}
    method = method or {}
    stage = method.get("stage") or ("bspline" if _finite(metrics.get("logJ_lesion_minus_brain")) else "affine")
    ncc_min = thresholds["ncc_xl_min_bspline" if stage == "bspline" else "ncc_xl_min_affine"]
    logj_max = thresholds["abs_logJ_lesion_minus_brain_max"]
    rules = (
        ("ncc_xl", None, ncc_min, None, "image similarity (NCC) {value:.2f} below {low:.2f}"),
        ("normalized_ratio", None, thresholds["normalized_ratio_min"], thresholds["normalized_ratio_max"], "lesion volume ratio (normalized for brain size) {value:.2f} outside {low:.2f}-{high:.2f}"),
        ("logJ_lesion_minus_brain", abs, None, logj_max, "log-Jacobian in the lesion minus the brain {value:+.2f} beyond ±{high:.2f}"),
        ("folding_pct", None, None, thresholds["folding_pct_max"], "folding in {value:.2f}% of the brain (above {high:.2f}%)"),
        ("outside_brain_pct", None, None, thresholds["outside_brain_pct_max"], "{value:.1f}% of the lesion outside the brain (above {high:.1f}%)"),
        ("brain_dice", None, thresholds["brain_dice_min"], None, "brain mask Dice {value:.2f} below {low:.2f}"),
        ("ventricle_pct", None, None, thresholds["ventricle_pct_max"], "{value:.1f}% of the lesion in the ventricles (above {high:.1f}%)"),
    )
    levels = {}
    reasons = []
    for key, transform, low, high, text in rules:
        value = metrics.get(key)
        level = _range_level(transform(value) if transform and _finite(value) else value, low, high)
        levels[key] = level
        if level == LEVEL_AMBER:
            reasons.append(text.format(value=float(value), low=low or 0.0, high=high or 0.0))
    if method.get("masking") is False or (_finite(method.get("dilate_mm")) and float(method["dilate_mm"]) < 0):
        reasons.append("cost-function masking was disabled")
    if method.get("cross_modality") and not method.get("two_step"):
        reasons.append("cross-modality registration without a T1-weighted scan (--anat)")
    if method.get("scan_only"):
        reasons.append("scan-only mode (no lesion mask during registration)")
    attempts = method.get("attempts")
    n_attempts = len(attempts) if isinstance(attempts, (list, tuple)) else attempts
    if _finite(n_attempts) and n_attempts > 1:
        reasons.append(f"alignment needed {int(n_attempts)} attempts")
    return {"levels": levels, "suggestion": SUGGEST_REVIEW if reasons else SUGGEST_OK, "reasons": reasons}


def compute_metrics(
    template,
    warped,
    template_mask,
    lesion,
    affine,
    native_lesion_ml=None,
    template_to_scan_affine=None,
    warped_brain_mask=None,
    jac=None,
    ventricles=None,
    ventricle_labels=None,
    lesion_space=None,
    ventricle_space=None,
):
    """All metrics available from template-grid images (missing inputs give missing metrics).

    :param template, warped, template_mask, lesion: template-grid arrays (lesion fractional).
    :param affine: the template grid's voxel-to-RAS affine.
    :param native_lesion_ml: lesion volume on the native grid (fraction-weighted), for the volume ratios.
    :param template_to_scan_affine: the affine part of the registration (template -> scan points).
    :param jac: Jacobian determinant on the template grid (for logJ_lesion_minus_brain and folding_pct).
    :return: dict of plain floats, lists and strings (NaN where undefined).
    """
    _check_same_shape(template, warped, template_mask, lesion, warped_brain_mask, jac, ventricles)
    metrics = {}
    if template is not None and warped is not None:
        metrics["ncc_xl"] = ncc_xl(template, warped, template_mask, lesion)
        metrics["nmi_xl"] = nmi_xl(template, warped, template_mask, lesion)
    if warped_brain_mask is not None:
        metrics["brain_dice"] = brain_mask_dice(warped_brain_mask, template_mask)
    metrics["mni_ml"] = volume_ml(lesion, affine, mode="binary")
    metrics["mni_ml_frac"] = volume_ml(lesion, affine, mode="fraction")
    template_brain_ml = volume_ml(template_mask, affine, mode="binary")
    metrics["template_brain_ml"] = template_brain_ml
    if native_lesion_ml is not None:
        metrics["native_ml"] = float(native_lesion_ml)
        if template_to_scan_affine is not None:
            metrics["affine_scale"] = affine_scale(template_to_scan_affine)
            change = lesion_volume_change(native_lesion_ml, metrics["mni_ml_frac"], native_brain_ml_from_affine(template_brain_ml, template_to_scan_affine), template_brain_ml)
            metrics.update({key: change[key] for key in ("raw_ratio", "scale_ratio", "normalized_ratio")})
    if jac is not None:
        metrics["logJ_lesion_minus_brain"] = log_jacobian_lesion_minus_brain(jac, lesion, template_mask)
        metrics["folding_pct"] = folding_pct(jac, template_mask)
    metrics["outside_brain_pct"] = pct_outside_brain(lesion, template_mask)
    if ventricles is not None:
        metrics["ventricle_pct"] = pct_in_ventricles(lesion, ventricles, ventricle_labels, lesion_space, ventricle_space)
    centroid = centroid_mm(lesion, affine)
    metrics["centroid_mni"] = centroid
    metrics["side"] = lesion_side(centroid)
    return metrics


def _plain(value):
    """JSON-safe copy: numpy scalars and arrays to Python, NaN and infinity to None."""
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_plain(item) for item in value]
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return float(value) if math.isfinite(float(value)) else None
    return value


def normqc_json(metrics, levels, method=None, versions=None, thresholds=None, suggestion=None, reasons=None, subject=None, template=None):
    """Content of ``sub-X_desc-normqc.json`` (write it with export.write_json_atomic).

    :param levels: the levels from :func:`evaluate` (or the whole :func:`evaluate` result, whose
      suggestion and reasons are then used).
    :param method: registration method record (tool, version, quality, dilate_mm, attempts, ...).
    :param versions: library versions (default export.versions()).
    :param thresholds: the thresholds used (default :data:`THRESHOLDS`).
    """
    if isinstance(levels, dict) and "levels" in levels:
        suggestion = levels.get("suggestion") if suggestion is None else suggestion
        reasons = levels.get("reasons") if reasons is None else reasons
        levels = levels["levels"]
    if suggestion is None:
        suggestion = SUGGEST_REVIEW if reasons or LEVEL_AMBER in (levels or {}).values() else SUGGEST_OK
    out = {
        "format": NORMQC_FORMAT,
        "version": NORMQC_VERSION,
        "created": export.now_iso(),
        "subject": subject,
        "template": template,
        "metrics": _plain(metrics or {}),
        "levels": _plain(levels or {}),
        "suggestion": suggestion,
        "reasons": list(reasons or []),
        "note": SUGGESTION_NOTE,
        "method": _plain(method or {}),
        "thresholds": _plain(THRESHOLDS if thresholds is None else thresholds),
        "calibration": _plain(CALIBRATION),
        "uncalibrated": list(UNCALIBRATED),
        "versions": _plain(export.versions() if versions is None else versions),
    }
    return out


# QC mosaic


def _split_image(image):
    """(array, affine or None) from an array or an (array, affine) pair."""
    if isinstance(image, (tuple, list)) and len(image) == 2 and np.ndim(image[1]) == 2:
        return np.asarray(image[0]), np.asarray(image[1], dtype=np.float64)
    return (None, None) if image is None else (np.asarray(image), None)


def _ras_reorientation(affine, shape):
    """Axis order, flips and new affine that make voxel axes 0, 1, 2 run toward +x (R), +y (A), +z (S).

    Each voxel axis is assigned to the world axis it is closest to (exact for axis-aligned grids).
    """
    linear = np.asarray(affine, dtype=np.float64)[:3, :3]
    world_axis = np.argmax(np.abs(linear), axis=0)  # world axis of each voxel axis
    if sorted(world_axis.tolist()) != [0, 1, 2]:
        raise ValueError("Cannot orient an oblique grid whose axes do not map to distinct world axes")
    order = [int(np.where(world_axis == w)[0][0]) for w in range(3)]  # voxel axis for each world axis
    flips = [bool(linear[w, order[w]] < 0) for w in range(3)]
    # new index n_w -> old index along order[w]: n or N - 1 - n
    permutation = np.zeros((4, 4))
    permutation[3, 3] = 1.0
    for w in range(3):
        a = order[w]
        permutation[a, w] = -1.0 if flips[w] else 1.0
        permutation[a, 3] = shape[a] - 1 if flips[w] else 0.0
    return order, flips, np.asarray(affine, dtype=np.float64) @ permutation


def _reorient(volume, order, flips):
    if volume is None:
        return None
    out = np.transpose(volume, order)
    for w in range(3):
        if flips[w]:
            out = np.flip(out, axis=w)
    return out


def _display_slice(volume, plane, index):
    """2D slice in display orientation (rows top to bottom, columns left to right) of a RAS-ordered volume."""
    if plane == "axial":  # columns x ascending (patient left on the left), rows y descending (anterior up)
        return volume[:, ::-1, index].T
    if plane == "coronal":  # columns x ascending, rows z descending
        return volume[:, index, ::-1].T
    return volume[index, ::-1, ::-1].T  # sagittal: columns y descending (anterior left), rows z descending


def _plane_axis(plane):
    return {"axial": 2, "coronal": 1, "sagittal": 0}[plane]


def _plane_spacing(spacing, plane):
    """(column, row) voxel size in mm of a display slice."""
    if plane == "axial":
        return spacing[0], spacing[1]
    if plane == "coronal":
        return spacing[0], spacing[2]
    return spacing[1], spacing[2]


def _window(volume, mask):
    """Grey levels 0..1 from the 1st-99th percentiles of the volume inside the mask (or the non-zero voxels)."""
    volume = np.asarray(volume, dtype=np.float64)
    values = volume[mask] if mask is not None and mask.any() else volume[volume != 0]
    if values.size == 0:
        values = volume.ravel()
    low, high = np.percentile(values, [1.0, 99.0])
    if high <= low:
        high = low + 1.0
    return np.clip((volume - low) / (high - low), 0.0, 1.0)


def _edges(grey, region, percentile=85.0):
    """Strong edges of a 2D grey image inside a region (gradient magnitude above a percentile)."""
    from scipy import ndimage

    magnitude = np.hypot(ndimage.sobel(grey, axis=0), ndimage.sobel(grey, axis=1))
    inside = magnitude[region]
    if inside.size == 0 or not np.any(inside > 0):
        return np.zeros(grey.shape, dtype=bool)
    return region & (magnitude > np.percentile(inside[inside > 0], percentile))


def _resize(array, size, smooth):
    """Resize a 2D array to size (width, height) with Pillow (bilinear for grey, nearest for masks)."""
    from PIL import Image

    if array.dtype == bool:
        image = Image.fromarray(array.astype(np.uint8) * 255)
        return np.asarray(image.resize(size, Image.Resampling.NEAREST)) > 127
    image = Image.fromarray(np.asarray(array, dtype=np.float32), mode="F")
    return np.asarray(image.resize(size, Image.Resampling.BILINEAR if smooth else Image.Resampling.NEAREST), dtype=np.float64)


def _paint(rgb, mask, color, alpha=1.0):
    if alpha >= 1.0:
        rgb[mask] = color
    else:
        rgb[mask] = (1.0 - alpha) * rgb[mask] + alpha * np.asarray(color, dtype=np.float64)


def qc_mosaic_png(
    template,
    warped,
    template_mask,
    lesion,
    out_png,
    checkerboard=True,
    affine=None,
    title=None,
    pixels_per_mm=1.0,
    checker_mm=16.0,
    offset_mm=8.0,
    lesion_alpha=0.5,
):
    """Write a 3 x 3 normalization QC mosaic PNG and return its layout.

    Rows are axial, coronal and sagittal slices through the lesion centroid (the brain-mask centroid
    for an empty lesion), shown in neurological orientation (the patient's left on the image left,
    anterior up in axial views, anterior left in sagittal views as in Slicer) with L/R, A/P and S/I
    stamps on the cell edges. Columns:

    - ``checkerboard=True`` (needs the template): (1) the template in grey with the warped scan's edges
      in amber (inside the template brain mask dilated by 3 voxels) and the lesion outline in red;
      (2) a ``checker_mm`` checkerboard of the template and the warped scan; (3) the warped scan with
      the template brain edge in cyan and the lesion filled in red (alpha ``lesion_alpha`` x fraction);
    - ``checkerboard=False``: the warped scan at the centroid - ``offset_mm``, the centroid and the
      centroid + ``offset_mm``, with the template brain edge in cyan and the lesion outline in red.

    :param template, warped, template_mask, lesion: arrays on the template grid, or (array, affine)
      pairs; ``template`` may be None when ``checkerboard`` is False.
    :param affine: the grid's voxel-to-RAS affine when no (array, affine) pair carries it.
    :param title: ASCII title line, for example "<template> | <subject> | research use only".
    :param pixels_per_mm: image scale (1.0 gives about 600 x 700 pixels for MNI templates at any resolution).
    :return: dict with "size" (width, height), "centroid_mm", "slices" and "cells": per cell its row,
      column, plane, slice index, "box" (x0, y0, x1, y1) and "stamps" {letter: (x, y)}.
    """
    from PIL import Image, ImageDraw, ImageFont
    from scipy import ndimage

    images = {}
    for name, value in (("template", template), ("warped", warped), ("template_mask", template_mask), ("lesion", lesion)):
        array, image_affine = _split_image(value)
        images[name] = array
        if image_affine is not None:
            if affine is None:
                affine = image_affine
            elif not np.allclose(affine, image_affine, atol=1e-3):
                raise ValueError(f"The {name} image is not on the template grid")
    if affine is None:
        raise ValueError("Pass the template grid affine (affine= or (array, affine) pairs)")
    if images["warped"] is None or images["template_mask"] is None or images["lesion"] is None:
        raise ValueError("The warped scan, the template mask and the lesion are required")
    if checkerboard and images["template"] is None:
        raise ValueError("The checkerboard mosaic needs the template image")
    _check_same_shape(*images.values())
    shape = images["warped"].shape[:3]

    order, flips, ras_affine = _ras_reorientation(affine, shape)
    brain = _reorient(_as_mask(images["template_mask"]), order, flips)
    lesion_fraction = _reorient(_as_fraction(images["lesion"]), order, flips)
    warped_grey = _reorient(_window(images["warped"], _as_mask(images["template_mask"])), order, flips)
    template_grey = None
    if images["template"] is not None:
        template_grey = _reorient(_window(images["template"], _as_mask(images["template_mask"])), order, flips)
    edge_region = ndimage.binary_dilation(brain, structure=ndimage.generate_binary_structure(3, 1), iterations=3) if checkerboard else None
    spacing = np.sqrt((ras_affine[:3, :3] ** 2).sum(axis=0))
    ras_shape = brain.shape

    # Centroid in RAS-ordered voxel indices
    weights = lesion_fraction if lesion_fraction.sum() > 0 else brain.astype(np.float64)
    if weights.sum() > 0:
        center = np.array([float((np.indices(ras_shape)[w] * weights).sum() / weights.sum()) for w in range(3)])
    else:
        center = (np.array(ras_shape) - 1) / 2.0
    center_index = [int(np.clip(round(center[w]), 0, ras_shape[w] - 1)) for w in range(3)]
    centroid_world = [float(v) for v in geometry.voxel_to_world(np.array(center_index, dtype=np.float64)[None, :], ras_affine)[0]]

    # Cell size: the largest display extent of the three planes, in pixels
    sizes = {}
    for plane in PLANES:
        rows, cols = _display_slice(brain, plane, 0).shape
        col_mm, row_mm = _plane_spacing(spacing, plane)
        sizes[plane] = (max(1, round(cols * col_mm * pixels_per_mm)), max(1, round(rows * row_mm * pixels_per_mm)))
    cell_w = max(size[0] for size in sizes.values())
    cell_h = max(size[1] for size in sizes.values())
    title_h = TITLE_HEIGHT if title else 0
    width = 3 * cell_w + 2 * CELL_GAP
    height = title_h + 3 * cell_h + 2 * CELL_GAP
    canvas = Image.new("RGB", (width, height), COLOR_BACKGROUND)
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()

    def plane_slices(plane, index):
        return {name: (None if volume is None else _display_slice(volume, plane, index)) for name, volume in (("brain", brain), ("lesion", lesion_fraction), ("warped", warped_grey), ("template", template_grey), ("edge_region", edge_region))}

    def checker(plane, index, slice_shape):
        # World coordinates of the in-plane voxel centres, in display orientation
        grids = np.meshgrid(*[np.arange(n, dtype=np.float64) for n in ras_shape], indexing="ij", sparse=True)
        axis = _plane_axis(plane)
        ijk = [np.full(slice_shape, float(index)) for _ in range(3)]
        for w in range(3):
            if w != axis:
                ijk[w] = _display_slice(np.broadcast_to(grids[w], ras_shape), plane, index).astype(np.float64)
        world = [ras_affine[r, 0] * ijk[0] + ras_affine[r, 1] * ijk[1] + ras_affine[r, 2] * ijk[2] + ras_affine[r, 3] for r in range(3)]
        in_plane = [world[r] for r in range(3) if r != axis]
        return ((np.floor(in_plane[0] / checker_mm) + np.floor(in_plane[1] / checker_mm)) % 2).astype(bool)

    cells = []
    slices = {}
    for row, plane in enumerate(PLANES):
        axis = _plane_axis(plane)
        if checkerboard:
            indices = [center_index[axis]] * 3
        else:
            step = max(1, round(offset_mm / spacing[axis]))
            indices = [int(np.clip(center_index[axis] + d * step, 0, ras_shape[axis] - 1)) for d in (-1, 0, 1)]
        slices[plane] = indices
        size = sizes[plane]
        for col, index in enumerate(indices):
            data = plane_slices(plane, index)
            lesion_mask = _resize(data["lesion"] >= LESION_THRESHOLD, size, False)
            lesion_edge = outline(lesion_mask)
            brain_edge = outline(_resize(data["brain"], size, False))
            if checkerboard and col == 0:
                grey = _resize(data["template"], size, True)
                rgb = np.repeat((grey * 255.0)[..., None], 3, axis=2)
                region = _resize(data["edge_region"], size, False)
                _paint(rgb, _edges(_resize(data["warped"], size, True), region), COLOR_SCAN_EDGE)
                _paint(rgb, lesion_edge, COLOR_LESION)
            elif checkerboard and col == 1:
                board = _resize(checker(plane, index, data["warped"].shape), size, False)
                grey = np.where(board, _resize(data["template"], size, True), _resize(data["warped"], size, True))
                rgb = np.repeat((grey * 255.0)[..., None], 3, axis=2)
            elif checkerboard:
                grey = _resize(data["warped"], size, True)
                rgb = np.repeat((grey * 255.0)[..., None], 3, axis=2)
                _paint(rgb, brain_edge, COLOR_BRAIN_EDGE)
                fraction = _resize(data["lesion"], size, True)
                inside = fraction > 0
                rgb[inside] = (1.0 - lesion_alpha * fraction[inside, None]) * rgb[inside] + lesion_alpha * fraction[inside, None] * np.asarray(COLOR_LESION, dtype=np.float64)
            else:
                grey = _resize(data["warped"], size, True)
                rgb = np.repeat((grey * 255.0)[..., None], 3, axis=2)
                _paint(rgb, brain_edge, COLOR_BRAIN_EDGE)
                _paint(rgb, lesion_edge, COLOR_LESION)
            tile = Image.fromarray(np.clip(np.rint(rgb), 0, 255).astype(np.uint8), mode="RGB")
            x0 = col * (cell_w + CELL_GAP)
            y0 = title_h + row * (cell_h + CELL_GAP)
            ox, oy = x0 + (cell_w - size[0]) // 2, y0 + (cell_h - size[1]) // 2
            canvas.paste(tile, (ox, oy))
            left, right, top, bottom = PLANE_STAMPS[plane]
            stamps = {
                left: (x0 + 3, y0 + cell_h // 2 - 5),
                right: (x0 + cell_w - 9, y0 + cell_h // 2 - 5),
                top: (x0 + cell_w // 2 - 3, y0 + 2),
                bottom: (x0 + cell_w // 2 - 3, y0 + cell_h - 12),
            }
            for letter, position in stamps.items():
                draw.text(position, letter, fill=COLOR_TEXT, font=font)
            cells.append({"row": row, "col": col, "plane": plane, "index": index, "box": (x0, y0, x0 + cell_w, y0 + cell_h), "image_box": (ox, oy, ox + size[0], oy + size[1]), "stamps": stamps})
    for i in (1, 2):
        x = i * (cell_w + CELL_GAP) - CELL_GAP // 2 - 1
        y = title_h + i * (cell_h + CELL_GAP) - CELL_GAP // 2 - 1
        draw.line([(x, title_h), (x, height - 1)], fill=COLOR_SEPARATOR)
        draw.line([(0, y), (width - 1, y)], fill=COLOR_SEPARATOR)
    if title:
        draw.text((4, 2), str(title).encode("ascii", "replace").decode("ascii"), fill=COLOR_TEXT, font=font)
    canvas.save(out_png, format="PNG")
    return {"size": (width, height), "centroid_mm": centroid_world, "slices": slices, "cells": cells}

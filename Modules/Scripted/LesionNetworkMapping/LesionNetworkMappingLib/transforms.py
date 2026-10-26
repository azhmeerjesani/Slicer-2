"""Template-family transforms: warp images and transform points through ITK transform files.

ITK transform files (``.h5``, ``.tfm``, ``.txt``, ``.mat``) are read with SimpleITK, which is
bundled with Slicer's Python and imported only when a transform is applied: importing this module,
parsing transform names and the numpy-only modules (``geometry``, ``nifti``) never import it.
Accepted files: TemplateFlow/ANTs composites (affine + displacement field), BRAINSFit composites
(affine + BSpline) and flattened two-step composites (nested composites are flattened on reading).

Direction rule (enforced from the file name)::

    tpl-B_from-A_mode-image_xfm.h5        resamples A-space IMAGES onto the B grid
    <sub>_from-A_to-B_mode-image_xfm.h5   same meaning (BIDS derivatives naming)
    <name>_Inverse.h5                     BRAINSFit's inverse output: the opposite direction

An image transform maps target (B) points to source (A) points, which is what resampling needs.
So POINTS (coordinates) go from A to B with the opposite file, ``tpl-A_from-B_mode-image_xfm.h5``.
:func:`warp_image` and :func:`transform_points` raise :class:`DirectionError` when the requested
spaces do not match the file name, and :func:`warp_image` also when the source or reference image
lies on a standard grid of another template family than the name says.

World coordinates are RAS (as in Slicer and NIfTI); ITK works in LPS, so x and y change sign
around every ``TransformPoint`` call.

Command line (prints one JSON event per line, see ``jobs``)::

    python -m LesionNetworkMappingLib.transforms warp --input SRC --transform XFM --reference REF --output OUT
        [--interpolation linear|nearest] [--binarize-at T] [--source-space A] [--target-space B] [--threads N] [--cancel-file F]
    python -m LesionNetworkMappingLib.transforms points --transform XFM (--point X,Y,Z ... | --input TSV)
        [--output TSV] [--source-space A] [--target-space B]
    python -m LesionNetworkMappingLib.transforms check --transform XFM [--inverse XFM2] [--point X,Y,Z ...]
        [--tolerance MM] [--sha256]

``--point`` accepts negative coordinates (``--point -40,-20,10``) as well as ``--point=-40,-20,10``.
"""

import os
import sys

if __name__ == "__main__":
    from LesionNetworkMappingLib.jobs import set_blas_threads

    set_blas_threads(sys.argv[1:])  # before numpy is imported: BLAS reads these variables once

import csv
import re
import time
from typing import NamedTuple

import numpy as np

from .geometry import SignedPermutation, detect_grid_candidates, signed_permutation, space_family
from .nifti import read_nifti_header, write_like, xform_disagreement

INTERPOLATIONS = ("linear", "nearest")
TRANSFORM_EXTENSIONS = (".h5", ".hdf5", ".tfm", ".txt", ".mat")
INVERSE_SUFFIX = "_Inverse"  # BRAINSFit's name for the inverse of an output transform
# Landmarks (RAS mm) of `check` when none are given: a 3x3x3 lattice inside an MNI brain
DEFAULT_LANDMARKS = tuple((x, y, z) for x in (-50.0, 0.0, 50.0) for y in (-80.0, -20.0, 40.0) for z in (-30.0, 15.0, 60.0))
_IDENTITY = SignedPermutation((0, 1, 2), (1, 1, 1), (0, 0, 0))
_ENTITY = re.compile(r"^([A-Za-z0-9]+)-(.+)$")
_LPS = np.array([-1.0, -1.0, 1.0])  # RAS <-> LPS sign flip
# Standard grids that another template family also writes images on, for the family check only:
# SPM12's 2 mm avg152 box (MNI152Lin) is the FSL 2 mm grid (91x109x91 at -90, -126, -72)
_SHARED_GRID_FAMILIES = {"NLin6-2mm": ("spm",)}


class DirectionError(ValueError):
    """A transform file was requested for the opposite direction (or the wrong spaces)."""


class TransformDirection(NamedTuple):
    """Spaces of an image transform: it resamples ``image_from`` images onto the ``image_to`` grid."""

    image_from: str
    image_to: str

    @property
    def points_from(self):
        """Space of the points this file maps (the opposite of the images)."""
        return self.image_to

    @property
    def points_to(self):
        return self.image_from

    def describe(self):
        return {"images": {"from": self.image_from, "to": self.image_to}, "points": {"from": self.points_from, "to": self.points_to}}


def _strip_extension(name):
    lower = name.lower()
    for extension in TRANSFORM_EXTENSIONS:
        if lower.endswith(extension):
            return name[: -len(extension)]
    return name


def parse_transform_name(path):
    """Direction of a transform file from its name (see the module documentation).

    :return: :class:`TransformDirection`, or None when the name does not say (no ``from-`` entity,
      or neither ``tpl-`` nor ``to-``).
    :raises ValueError: when the name contradicts itself (``tpl-`` and ``to-`` differ) or its
      ``mode-`` is not ``image``.
    """
    name = _strip_extension(os.path.basename(os.fspath(path)))
    inverse = name.endswith(INVERSE_SUFFIX)
    if inverse:
        name = name[: -len(INVERSE_SUFFIX)]
    entities = {}
    for part in name.split("_"):
        match = _ENTITY.match(part)
        if match:
            entities.setdefault(match.group(1), match.group(2))
    mode = entities.get("mode")
    if mode is not None and mode != "image":
        raise ValueError(f"Unsupported transform mode '{mode}' in {os.path.basename(os.fspath(path))}: only mode-image transforms are supported")
    target, template = entities.get("to"), entities.get("tpl")
    if target and template and _label(target) != _label(template):
        raise ValueError(f"{os.path.basename(os.fspath(path))} names two target spaces (tpl-{template} and to-{target})")
    target = target or template
    source = entities.get("from")
    if not source or not target:
        return None
    return TransformDirection(target, source) if inverse else TransformDirection(source, target)


def _label(space):
    label = str(space).strip()
    for prefix in ("tpl-", "space-"):
        if label.lower().startswith(prefix):
            label = label[len(prefix):]
    return label.lower()


def transform_direction(xfm_path, operation, source_space=None, target_space=None):
    """Direction of a transform file for an operation, checked against the requested spaces.

    :param operation: "image" (resample images of ``source_space`` onto a ``target_space`` grid)
      or "points" (map ``source_space`` coordinates to ``target_space`` coordinates).
    :param source_space, target_space: requested spaces (optional when the name tells the
      direction; both are required when it does not, and then declare it).
    :return: :class:`TransformDirection` of the file.
    :raises DirectionError: when the file goes the other way (the message names the file to use),
      or when its name does not tell the direction and the spaces are not both given.
    """
    if operation not in ("image", "points"):
        raise ValueError(f"Unknown operation: {operation}")
    name = os.path.basename(os.fspath(xfm_path))
    named = parse_transform_name(xfm_path)
    if named is None:
        if not (source_space and target_space):
            raise DirectionError(
                f"The name of {name} does not tell the transform direction: name it tpl-<target>_from-<source>_mode-image_xfm.h5 "
                "(it resamples <source> images onto the <target> grid) or give the source and target spaces",
            )
        return TransformDirection(source_space, target_space) if operation == "image" else TransformDirection(target_space, source_space)
    if operation == "image":
        have_from, have_to = named.image_from, named.image_to
    else:
        have_from, have_to = named.points_from, named.points_to
    wrong = (source_space and _label(source_space) != _label(have_from)) or (target_space and _label(target_space) != _label(have_to))
    if wrong:
        source = source_space or have_from
        target = target_space or have_to
        if operation == "image":
            raise DirectionError(
                f"{name} resamples {named.image_from} images onto the {named.image_to} grid (it maps {named.points_from} points to {named.points_to} points); "
                f"to resample {source} images onto the {target} grid use tpl-{target}_from-{source}_mode-image_xfm.h5",
            )
        raise DirectionError(
            f"{name} maps {named.points_from} points to {named.points_to} points (it resamples {named.image_from} images onto the {named.image_to} grid); "
            f"to map {source} points to {target} points use tpl-{source}_from-{target}_mode-image_xfm.h5",
        )
    return named


def _check_grid_family(shape, affine, space, what, path):
    """Raise DirectionError when an image lies on a standard grid of another template family than ``space``."""
    family = space_family(space)
    if family in ("native", "unknown"):
        return
    match = detect_grid_candidates(shape, affine)
    if match is None:
        return
    families = {match.family} | {space_family(candidate) for candidate in match.candidates} | set(_SHARED_GRID_FAMILIES.get(match.grid_id, ()))
    if family not in families:
        raise DirectionError(
            f"{os.path.basename(os.fspath(path))} is on the {match.grid_id} grid ({', '.join(match.candidates)}), "
            f"but the transform expects {what} in {space}: the transform file goes the other way or is for another template",
        )


# SimpleITK (imported lazily)


def _sitk():
    import SimpleITK as sitk

    return sitk


def set_threads(threads):
    """Set the default number of threads of SimpleITK filters (process-wide).

    :return: the previous default (to restore it), or None when SimpleITK does not tell it.
    """
    sitk = _sitk()
    process_object = getattr(sitk, "ProcessObject", None)
    getter = getattr(process_object, "GetGlobalDefaultNumberOfThreads", None) or getattr(sitk, "ProcessObject_GetGlobalDefaultNumberOfThreads", None)
    setter = getattr(process_object, "SetGlobalDefaultNumberOfThreads", None) or getattr(sitk, "ProcessObject_SetGlobalDefaultNumberOfThreads", None)
    previous = int(getter()) if getter is not None else None
    if setter is not None:
        setter(int(threads))
    return previous


def read_transform(path):
    """Read an ITK transform file with SimpleITK; nested composite transforms are flattened."""
    sitk = _sitk()
    transform = sitk.ReadTransform(os.fspath(path))
    if transform.GetName() == "CompositeTransform":
        composite = sitk.CompositeTransform(transform)
        if any(composite.GetNthTransform(n).GetName() == "CompositeTransform" for n in range(composite.GetNumberOfTransforms())):
            composite.FlattenTransform()
        transform = composite
    return transform


def transform_kinds(transform):
    """Names of the transforms applied, in the order they are stored (ITK applies the last one first)."""
    if transform.GetName() == "CompositeTransform":
        composite = _sitk().CompositeTransform(transform)
        return [composite.GetNthTransform(n).GetName() for n in range(composite.GetNumberOfTransforms())]
    return [transform.GetName()]


def file_sha256(path):
    from .export import file_sha256 as sha256

    return sha256(path)


def ras_affine(origin, spacing, direction):
    """4x4 voxel-to-RAS matrix of an ITK image geometry (LPS origin, spacing, 3x3 direction)."""
    direction = np.asarray(direction, dtype=np.float64).reshape(3, 3)
    affine = np.eye(4)
    affine[:3, :3] = _LPS[:, np.newaxis] * direction * np.asarray(spacing, dtype=np.float64)[np.newaxis, :]
    affine[:3, 3] = _LPS * np.asarray(origin, dtype=np.float64)
    return affine


def _ascii(text):
    """``text`` with characters outside ASCII replaced by "?" (NIfTI header text fields are bytes)."""
    return text.encode("ascii", "replace").decode("ascii")


def _geometry_3d(size, origin, spacing, direction):
    """Drop a trailing singleton 4th dimension (3D NIfTI files written with dim[0] = 4)."""
    dimension = len(size)
    if dimension == 3:
        return tuple(size), tuple(origin), tuple(spacing), tuple(direction)
    if dimension == 4 and size[3] == 1:
        matrix = np.asarray(direction, dtype=np.float64).reshape(4, 4)[:3, :3]
        return tuple(size[:3]), tuple(origin[:3]), tuple(spacing[:3]), tuple(matrix.ravel())
    raise ValueError(f"Expected a 3D image, got size {tuple(size)}")


def _check_placement(affine, path):
    """Raise unless ITK placed a NIfTI file's voxels exactly where its header's sform (or qform) says."""
    if not os.fspath(path).lower().endswith((".nii", ".nii.gz")):
        return
    header = read_nifti_header(path)
    if signed_permutation(affine, header["affine"], tolerance=1e-3) != _IDENTITY:
        raise ValueError(
            f"SimpleITK places {os.path.basename(os.fspath(path))} differently from its NIfTI header "
            f"(the sform and qform disagree by {xform_disagreement(header):.3g} mm); make them agree before warping",
        )


def _read_source(sitk, path):
    image = sitk.ReadImage(os.fspath(path))
    if image.GetNumberOfComponentsPerPixel() != 1:
        raise ValueError(f"{os.path.basename(os.fspath(path))} has {image.GetNumberOfComponentsPerPixel()} components per voxel; only scalar images can be warped")
    size = image.GetSize()
    if len(size) == 4 and size[3] == 1:
        image = sitk.Extract(image, [*size[:3], 0], [0, 0, 0, 0])
    elif len(size) != 3:
        raise ValueError(f"{os.path.basename(os.fspath(path))} is not a 3D image (size {tuple(size)})")
    return image


def warp_image(src_path, xfm_h5, reference_path, out_path, interpolation="linear", binarize_at=None, source_space=None, target_space=None, threads=None):
    """Resample an image through a transform file onto the grid of a reference NIfTI image.

    The output has the reference's exact geometry header (sform, qform, pixdim, codes and byte
    order are copied byte for byte, see ``nifti.write_like``) after checking that SimpleITK placed
    the reference's voxel centers where its header says.

    :param src_path: image in the transform's source space (any format SimpleITK reads).
    :param xfm_h5: transform file whose name tells the direction (``tpl-B_from-A_mode-image_xfm.h5``
      resamples A images onto the B grid); see :func:`transform_direction`.
    :param reference_path: NIfTI file defining the output grid (in the transform's target space).
    :param out_path: output .nii or .nii.gz file.
    :param interpolation: "linear" (float32 output, e.g. a fractional lesion) or "nearest" (the
      source data type).
    :param binarize_at: when given, voxels >= this value become 1 and the output is uint8.
    :param source_space, target_space: optional requested spaces, checked against the name (or
      declaring the direction of a file whose name does not tell it).
    :param threads: SimpleITK threads for this call (the process-wide default is restored after it;
      unchanged when None).
    :return: provenance for the lesion sidecar: {transform, transform_sha256, interpolation,
      direction: "image", source_space, target_space, binarize_at, reference}.
    """
    if interpolation not in INTERPOLATIONS:
        raise ValueError(f"interpolation must be one of {INTERPOLATIONS}, got {interpolation!r}")
    direction = transform_direction(xfm_h5, "image", source_space, target_space)
    reference_header = read_nifti_header(reference_path)
    _check_grid_family(reference_header["shape"][:3], reference_header["affine"], direction.image_to, "the output grid", reference_path)
    sitk = _sitk()
    previous_threads = set_threads(threads) if threads is not None else None
    try:
        return _warp_image(sitk, src_path, xfm_h5, reference_path, reference_header, out_path, interpolation, binarize_at, direction)
    finally:
        if previous_threads is not None:
            set_threads(previous_threads)


def _warp_image(sitk, src_path, xfm_h5, reference_path, reference_header, out_path, interpolation, binarize_at, direction):
    reader = sitk.ImageFileReader()
    reader.SetFileName(os.fspath(reference_path))
    reader.ReadImageInformation()
    size, origin, spacing, matrix = _geometry_3d(reader.GetSize(), reader.GetOrigin(), reader.GetSpacing(), reader.GetDirection())
    _check_placement(ras_affine(origin, spacing, matrix), reference_path)

    source = _read_source(sitk, src_path)
    source_affine = ras_affine(source.GetOrigin(), source.GetSpacing(), source.GetDirection())
    _check_placement(source_affine, src_path)
    _check_grid_family(source.GetSize(), source_affine, direction.image_from, "images", src_path)

    transform = read_transform(xfm_h5)
    if interpolation == "linear":
        interpolator, pixel_type = sitk.sitkLinear, sitk.sitkFloat32
    else:
        interpolator, pixel_type = sitk.sitkNearestNeighbor, source.GetPixelID()
    warped = sitk.Resample(source, size, transform, interpolator, origin, spacing, matrix, 0.0, pixel_type)
    array_kji = sitk.GetArrayFromImage(warped)
    if binarize_at is not None:
        array_kji = (array_kji >= binarize_at).astype(np.uint8)
    elif interpolation == "linear" or array_kji.dtype.kind not in "uif" or array_kji.dtype.itemsize > 8:
        array_kji = array_kji.astype(np.float32)
    write_like(out_path, array_kji, reference_header["affine"], reference_path, dtype=array_kji.dtype, descrip=_ascii(f"warped ({interpolation}) from {direction.image_from}")[:80])
    return {
        "transform": os.path.basename(os.fspath(xfm_h5)),
        "transform_sha256": file_sha256(xfm_h5),
        "interpolation": interpolation,
        "direction": "image",
        "source_space": direction.image_from,
        "target_space": direction.image_to,
        "binarize_at": binarize_at,
        "reference": os.path.basename(os.fspath(reference_path)),
    }


def transform_points(points_ras, xfm_h5, source_space=None, target_space=None, transform=None):
    """Map RAS coordinates (mm) through a transform file, from its target space to its source space.

    For points A -> B use ``tpl-A_from-B_mode-image_xfm.h5`` (see the module documentation).

    :param points_ras: (N, 3) array, or a single (3,) point.
    :param source_space, target_space: optional requested spaces, checked against the name (or
      declaring the direction of a file whose name does not tell it).
    :param transform: the already read transform of ``xfm_h5`` (:func:`read_transform`), to map
      many batches without reading a large file again; the name still gives the direction.
    :return: array of the same shape. Points outside a displacement field's domain move by the
      other transforms only (ITK uses a zero displacement there).
    """
    transform_direction(xfm_h5, "points", source_space, target_space)
    points = np.asarray(points_ras, dtype=np.float64)
    single = points.ndim == 1
    points = np.atleast_2d(points)
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError(f"Expected (N, 3) points, got shape {np.shape(points_ras)}")
    if transform is None:
        transform = read_transform(xfm_h5)
    out = np.empty_like(points)
    for n, point in enumerate(points):
        out[n] = _LPS * np.asarray(transform.TransformPoint(tuple(float(v) for v in _LPS * point)))
    return out[0] if single else out


def round_trip(points_ras, xfm_h5, inverse_h5, transform=None, inverse=None):
    """Map points through a transform file and back through its inverse file.

    :return: (mapped points, distances in mm between each point and its round trip).
    :raises DirectionError: when both names tell their direction and the files are not opposite.
    """
    forward, backward = parse_transform_name(xfm_h5), parse_transform_name(inverse_h5)
    if forward is not None and backward is not None and (_label(forward.image_from), _label(forward.image_to)) != (_label(backward.image_to), _label(backward.image_from)):
        raise DirectionError(f"{os.path.basename(os.fspath(inverse_h5))} is not the inverse of {os.path.basename(os.fspath(xfm_h5))} by its name")
    points = np.atleast_2d(np.asarray(points_ras, dtype=np.float64))
    if forward is None:  # declare the direction of an unnamed file: opposite to its inverse, or any opposite labels
        forward = TransformDirection("source", "target") if backward is None else TransformDirection(backward.image_to, backward.image_from)
    mapped = transform_points(points, xfm_h5, forward.points_from, forward.points_to, transform=transform)
    back = transform_points(mapped, inverse_h5, forward.points_to, forward.points_from, transform=inverse)
    return mapped, np.linalg.norm(back - points, axis=1)


# Command line


def _point(text):
    import argparse

    try:
        values = [float(value) for value in text.replace(";", ",").split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid point {text!r}: expected X,Y,Z") from None
    if len(values) != 3:
        raise argparse.ArgumentTypeError(f"invalid point {text!r}: expected X,Y,Z")
    return values


def read_points_table(path):
    """Read points (RAS mm) from a TSV or CSV file with x, y and z columns (or three unnamed columns)."""
    with open(path, newline="", encoding="utf-8") as f:
        text = f.read()
    if not text.strip():
        return np.zeros((0, 3))
    delimiter = "\t" if "\t" in text.splitlines()[0] else ","
    rows = [row for row in csv.reader(text.splitlines(), delimiter=delimiter) if row and any(cell.strip() for cell in row)]
    if not rows:
        return np.zeros((0, 3))
    header = [cell.strip().lower() for cell in rows[0]]
    if {"x", "y", "z"} <= set(header):
        columns = [header.index(axis) for axis in ("x", "y", "z")]
        rows = rows[1:]
    else:
        columns = [0, 1, 2]
    return np.array([[float(row[c]) for c in columns] for row in rows], dtype=np.float64).reshape(-1, 3)


def _write_points_table(path, points):
    tmp_path = f"{path}.{os.getpid()}.tmp"
    with open(tmp_path, "w", newline="", encoding="utf-8") as f:
        f.write("x\ty\tz\n")
        for x, y, z in points:
            f.write(f"{x:.6f}\t{y:.6f}\t{z:.6f}\n")
    os.replace(tmp_path, path)


def _parser():
    from . import jobs

    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.transforms", description="Warp images and transform points through template-family transform files.")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=jobs.JsonArgumentParser)

    warp = commands.add_parser("warp", help="resample an image onto a reference grid (tpl-B_from-A resamples A images onto B)")
    warp.add_argument("--input", required=True, help="image in the transform's source space")
    warp.add_argument("--transform", required=True, help="transform file (tpl-B_from-A_mode-image_xfm.h5)")
    warp.add_argument("--reference", required=True, help="NIfTI image defining the output grid")
    warp.add_argument("--output", required=True, help="output .nii or .nii.gz")
    warp.add_argument("--interpolation", choices=INTERPOLATIONS, default="linear")
    warp.add_argument("--binarize-at", type=float, default=None, help="write voxels >= this value as 1 (uint8)")

    points = commands.add_parser("points", help="map coordinates (tpl-A_from-B maps A points to B points)")
    points.add_argument("--transform", required=True, help="transform file (tpl-A_from-B_mode-image_xfm.h5 for A -> B points)")
    points.add_argument("--point", type=_point, action="append", default=[], help="X,Y,Z in RAS mm (repeatable)")
    points.add_argument("--input", default=None, help="TSV or CSV file with x, y and z columns (RAS mm)")
    points.add_argument("--output", default=None, help="TSV file of the mapped points (x, y, z)")

    check = commands.add_parser("check", help="report a transform file's direction and, with --inverse, the landmark round trip")
    check.add_argument("--transform", required=True)
    check.add_argument("--inverse", default=None, help="the opposite-direction file: points go through --transform, then back through it")
    check.add_argument("--point", type=_point, action="append", default=[], help="landmark X,Y,Z in RAS mm (repeatable; default: a 3x3x3 lattice in an MNI brain)")
    check.add_argument("--tolerance", type=float, default=0.5, help="largest round-trip distance in mm (default: 0.5)")
    check.add_argument("--sha256", action="store_true", help="also report the sha256 of the files")

    for sub in (warp, points):
        sub.add_argument("--source-space", default=None, help="requested source space, checked against the file name")
        sub.add_argument("--target-space", default=None, help="requested target space, checked against the file name")
    for sub in (warp, points, check):
        sub.add_argument("--threads", type=jobs.positive_int, default=2, help="SimpleITK threads (default: 2)")
        sub.add_argument("--cancel-file", default=None, help="stop (exit code 3) if this file exists when the work starts (a running resample is not interrupted)")
    return parser


def _join_point_values(argv):
    """Write ``--point X,Y,Z`` as ``--point=X,Y,Z``, so that argparse accepts negative coordinates.

    argparse takes ``-40,20,10`` for an option because only plain numbers like ``-40`` count as
    negative values; a value that starts with ``--`` is left for argparse to report.
    """
    joined = []
    args = iter(argv)
    for arg in args:
        if arg == "--point":
            value = next(args, None)
            if value is None or value.startswith("--"):
                joined.append(arg)
                if value is not None:
                    joined.append(value)
                continue
            arg = f"--point={value}"
        joined.append(arg)
    return joined


def main(argv):
    """Run one command (see the module documentation); events go to standard output."""
    from . import jobs

    args = _parser().parse_args(_join_point_values(argv))
    cancel = jobs.CancelToken(args.cancel_file)
    jobs.emit("start", job="transforms", command=args.command, argv=list(argv), threads=args.threads)
    try:
        if args.command == "warp":
            return _main_warp(args, cancel)
        if args.command == "points":
            return _main_points(args, cancel)
        return _main_check(args, cancel)
    except DirectionError as exc:
        raise jobs.JobError(str(exc), code="direction") from None


def _main_warp(args, cancel):
    from . import jobs

    cancel.raise_if_cancelled(done=0)
    jobs.emit("stage", name="warp", index=1, total=1)
    provenance = warp_image(args.input, args.transform, args.reference, args.output, args.interpolation, args.binarize_at, args.source_space, args.target_space, threads=args.threads)
    jobs.emit("result", kind="image", path=os.path.abspath(args.output), **provenance)
    jobs.emit("done", outputs=[os.path.abspath(args.output)], elapsed=round(jobs.elapsed(), 3), run_id=None, provenance=provenance)


def _main_points(args, cancel):
    from . import jobs

    points = [np.asarray(args.point, dtype=np.float64).reshape(-1, 3)]
    if args.input:
        points.append(read_points_table(args.input))
    points = np.concatenate(points)
    if not len(points):
        raise jobs.JobError("No points: give --point X,Y,Z or --input TABLE", code="usage", exit_code=jobs.EXIT_USAGE)
    direction = transform_direction(args.transform, "points", args.source_space, args.target_space)
    set_threads(args.threads)
    cancel.raise_if_cancelled(done=0)
    mapped = transform_points(points, args.transform, args.source_space, args.target_space)
    outputs = []
    fields = {"source_space": direction.points_from, "target_space": direction.points_to, "direction": "points", "n": len(mapped)}
    if args.output:
        _write_points_table(args.output, mapped)
        outputs.append(os.path.abspath(args.output))
        jobs.emit("result", kind="points", path=outputs[0], **fields)
    else:
        jobs.emit("result", kind="points", points=np.round(mapped, 6).tolist(), **fields)
    jobs.emit("done", outputs=outputs, elapsed=round(jobs.elapsed(), 3), run_id=None)


def _main_check(args, cancel):
    from . import jobs

    named = parse_transform_name(args.transform)
    started = time.monotonic()
    transform = read_transform(args.transform)
    report = {
        "transform": os.path.basename(args.transform),
        "named_direction": None if named is None else named.describe(),
        "kinds": transform_kinds(transform),
    }
    if args.sha256:
        report["transform_sha256"] = file_sha256(args.transform)
    failed = None
    if args.inverse:
        cancel.raise_if_cancelled(done=0)
        inverse = read_transform(args.inverse)
        report["inverse"] = os.path.basename(args.inverse)
        report["inverse_kinds"] = transform_kinds(inverse)
        if args.sha256:
            report["inverse_sha256"] = file_sha256(args.inverse)
        landmarks = np.asarray(args.point or DEFAULT_LANDMARKS, dtype=np.float64)
        set_threads(args.threads)
        _, distances = round_trip(landmarks, args.transform, args.inverse, transform=transform, inverse=inverse)
        report["round_trip"] = {"n": len(distances), "max_mm": float(distances.max()), "mean_mm": float(distances.mean()), "tolerance_mm": args.tolerance}
        if distances.max() > args.tolerance:
            failed = f"The landmark round trip moves points by up to {distances.max():.3g} mm (tolerance {args.tolerance} mm)"
    report["seconds"] = round(time.monotonic() - started, 3)
    jobs.emit("result", kind="check", **report)
    if failed:
        raise jobs.JobError(failed, code="round_trip")
    jobs.emit("done", outputs=[], elapsed=round(jobs.elapsed(), 3), run_id=None)


if __name__ == "__main__":
    from LesionNetworkMappingLib import jobs as _jobs

    _jobs.run_job(main, job="transforms")

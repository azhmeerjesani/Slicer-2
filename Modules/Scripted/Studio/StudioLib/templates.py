"""Your own MNI templates (A-10, decision D-T2): add, check, name and use your own MNI brain images.

Users who prefer another MNI brain (FSL MNI152 1 mm, ICBM 2009a/b, a 0.5 mm or 7T template, a skull-on head, a
study-specific MNI-space average) add it once and use it like the built-in one: Start on template, Draw, Map,
Normalize and studies. Nothing is downloaded or bundled here: the user brings the files.

Where they live (the Slicer Studio folder of A-09, ``projects.root()``)::

  Slicer Studio/
    Templates/<name>/          one folder per template, created on the first add
      template.json            the record below (relative file names only, never a path)
      <image>, <mask>, <head>  copies of the user's files (the originals are never changed)
      surfaces/<file>          optional 3D brain surfaces (meshes in MNI coordinates)
      .thumbnail.png           the mid-axial slice shown in the lists (cache, safe to delete)

template.json: ``{"schema": "slicer-studio-template", "version": 1, "id", "name", "space", "space_label",
"resolution_mm", "shape", "contrast", "files": {"image", "mask"?, "head"?, "surfaces": [...]}, "sha256": {role or
"surfaces/<file>": hex}, "grid": {"shape", "affine"}, "notes", "added", "default"}``. ``space`` is a space id
(``SPACES``; "MNI" is "MNI (other)", with the user's own label in ``space_label``).

The built-in templates (the bundled MNI152NLin2009cAsym 2 mm brain, and the 2009c 1 mm T1w and the MNI152NLin6Asym
2 mm T1w when they were downloaded) are listed by the same API as read-only entries (``Template.builtin``).

Ids: a user template is "user:<slug>" (``Template.id``; template.json stores the slug), a built-in one
"builtin:<name>". Studies refer to a user template by id, name, space and sha256 (``studyRecord``), never by path.

Checking a file before adding it (``identify``, header only, no node): a known template grid (both LAS and RAS
storage) proposes its space; otherwise the image is accepted as MNI only when its field of view looks like MNI space
(the MNI brain box inside the image, the centre within ``CENTER_TOLERANCE_MM`` of (0, -18, 18)); anything else is
refused with ``REFUSAL``. Masks and heads must share the image's grid; surfaces must lie in the MNI box.

Settings (fork-only, SPEC 9.7 rule 5): ``Studio/Templates/Default`` (a template id), and the add dialog's
``Studio/Templates/LastDirectory`` (the folder its file dialogs open in, a local convenience of this computer, like
Slicer's own last-directory settings). No path is ever stored in template.json or study files.

Threads: ``identify``, ``copyFiles`` and ``thumbnailArray`` use only files and numpy (worker-safe); everything else
runs on the main thread.
"""

import contextlib
import datetime
import hashlib
import json
import logging
import math
import os
import re
import shutil
import sys
import threading

import qt
import slicer
from slicer.i18n import tr as _

SCHEMA = "slicer-studio-template"
VERSION = 1
TEMPLATES_DIR = "Templates"
TEMPLATE_JSON = "template.json"
SURFACES_DIR = "surfaces"
THUMBNAIL_FILE = ".thumbnail.png"
#: Fork-only setting: the id of the default template ("Start on template")
DEFAULT_KEY = "Studio/Templates/Default"
#: Fork-only settings that hold a template id in a field of their JSON value, which follow a moved template's new id
#: (``followRenamedIds``): "Remember these options" of Normalize to MNI (``normalize_controller.LAST_OPTIONS_SETTING``)
REMEMBERED_TEMPLATE_SETTINGS = (("Studio/LNM/Normalize/LastOptions", "templateId"),)
USER_PREFIX = "user:"
BUILTIN_PREFIX = "builtin:"
#: The space id of "MNI (other)" (an unknown MNI family: Map shows the family notice)
OTHER_SPACE = "MNI"
#: Space ids and their display names (the Space list of the add dialog)
SPACES = (
    ("MNI152NLin2009cAsym", "MNI152NLin2009cAsym (ICBM 2009c)"),
    ("MNI152NLin2009aSym", "MNI152NLin2009aSym (ICBM 2009a)"),
    ("MNI152NLin2009bAsym", "MNI152NLin2009bAsym (ICBM 2009b)"),
    ("MNI152NLin6Asym", "MNI152NLin6Asym (FSL)"),
    ("MNI152Lin", "MNI152Lin (SPM, ICBM 152 linear)"),
    ("MNIColin27", "MNIColin27 (Colin 27)"),
    ("MNI305", "MNI305"),
    (OTHER_SPACE, "MNI (other)"),
)
SPACE_IDS = tuple(spaceId for spaceId, _label in SPACES)
CONTRASTS = ("T1w", "T2w", "PD", "FLAIR", "CT", "other")
IMAGE_EXTENSIONS = (".nii.gz", ".nii", ".nrrd")
SURFACE_EXTENSIONS = (".vtk", ".vtp", ".stl", ".obj", ".ply", ".gii")
#: The MNI brain (mm, RAS): an MNI template's field of view contains it ...
MNI_BRAIN_BOX = ((-70.0, 70.0), (-100.0, 70.0), (-50.0, 80.0))
#: ... and its centre is near this point (the centre of the usual MNI fields of view)
MNI_CENTER = (0.0, -18.0, 18.0)
CENTER_TOLERANCE_MM = 20.0
#: Surfaces must lie inside the brain box widened by this much
SURFACE_TOLERANCE_MM = 20.0
#: Voxel size ratio above which a template is reported as anisotropic
ANISOTROPY_WARNING = 1.5
#: Grid comparison (mm): masks and heads on the image's grid, the fingerprint of a template grid
GRID_TOLERANCE_MM = 0.01
COPY_CHUNK = 1 << 20
REFUSAL = "This image is not in MNI space. Open it as a scan instead, or normalize it to MNI first."
#: A NIfTI file whose sform and qform disagree by more than this (mm) is refused: the check reads the sform, while
#: Slicer (ITK) loads such a file at the qform, so drawn lesions would not be where the template says
XFORM_REFUSAL_MM = 0.1
XFORM_REFUSAL = ("The sform and qform of this image disagree by up to {distance:.3g} mm, so programs place it differently "
                 "(Slicer uses the qform, FSL the sform). Make them agree (for example with fslorient -copysform2qform) "
                 "and add it again.")
#: A NIfTI file with an sform and no qform whose voxel sizes (pixdim) do not match the sform is refused as well: Slicer
#: (ITK) loads it with the sform's directions and origin but the pixdim voxel sizes, away from the grid that was checked
PIXDIM_REFUSAL = ("The voxel size in the header of this image (pixdim) does not match its sform, so programs place it differently, "
                  "up to {distance:.3g} mm apart (Slicer uses the voxel size, FSL the sform). Set the voxel size to the sform's "
                  "({spacing} mm) and add it again.")
#: Node attribute: the template id of a template volume ("user:<slug>" or "builtin:<name>"); a portable id, never a path
TEMPLATE_ID_ATTRIBUTE = "Studio.TemplateId"
#: ``Studio.Role`` of the 3D brain surfaces of a template
ROLE_SURFACE = "template-surface"
SURFACE_COLOR = (0.82, 0.82, 0.82)
SURFACE_OPACITY = 0.3

#: Built-in templates: (id, display name, space, resolution, contrast, SampleData name of the image, of the mask)
_BUILTINS = (
    ("builtin:mni2009c-2mm", "MNI152 2009c", "MNI152NLin2009cAsym", 2, "T1w brain", "MNI152NLin2009cAsym 2mm brain", "MNI152NLin2009cAsym 2mm brain mask"),
    ("builtin:mni2009c-1mm", "MNI152 2009c 1 mm", "MNI152NLin2009cAsym", 1, "T1w", "MNI152NLin2009cAsym 1mm T1w", None),
    ("builtin:mni6-2mm", "FSL MNI152 2 mm", "MNI152NLin6Asym", 2, "T1w", "MNI152NLin6Asym 2mm T1w (FSL terms)", None),
)
BUILTIN_DEFAULT = _BUILTINS[0][0]

_listeners = []
_renameListeners = []
_registered = {"root": None, "stamp": None, "ids": set(), "others": []}
_thumbnails = {}
_unwritableCopies = set()  # copied template folders whose template.json could not be given their own id


#
# Small helpers (worker-safe)
#


def _np():
    import numpy as np

    return np


def _lowerExtension(path):
    name = os.path.basename(str(path)).lower()
    for extension in (".nii.gz", ".seg.nrrd"):
        if name.endswith(extension):
            return extension
    return os.path.splitext(name)[1]


def isImageFile(path):
    return _lowerExtension(path) in IMAGE_EXTENSIONS


def isSurfaceFile(path):
    return _lowerExtension(path) in SURFACE_EXTENSIONS


def stemOf(path):
    name = os.path.basename(str(path))
    extension = _lowerExtension(name)
    return name[: -len(extension)] if extension and name.lower().endswith(extension) else name


def proposedName(path):
    """A template name from a file name: "tpl-MNI152NLin6Asym_res-01_T1w.nii.gz" -> "MNI152NLin6Asym res-01 T1w"."""
    stem = stemOf(path)
    if stem.lower().startswith("tpl-"):
        stem = stem[4:]
    text = re.sub(r"[_]+", " ", stem).strip()
    return text[:60] or _("My template")


def slugOf(name):
    """The id of a template name: lower case letters, digits and hyphens ("ICBM 2009b 0.5 mm" -> "icbm-2009b-0-5-mm")."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")
    return slug[:48].strip("-") or "template"


def spaceDisplayName(spaceId, spaceLabel=None):
    if spaceId == OTHER_SPACE:
        return spaceLabel or _("MNI (other)")
    return str(spaceId or "")


def resolutionText(resolution):
    if resolution is None:
        return ""
    value = float(resolution)
    return f"{value:g} mm"


def _isoNow():
    return datetime.date.today().isoformat()


def fileSha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(COPY_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


#
# Headers (worker-safe, header only)
#


# The signs that turn the coordinates of a NRRD "space" into RAS, as 3D Slicer reads a scalar volume (ITK's NrrdImageIO):
# only RAS and LAS coordinates are converted; any other space, a missing one included ("3D-right-handed", "scanner-xyz",
# no "space" field), is taken unchanged as LPS (see _nrrdSpaceSigns).
_NRRD_SPACES = {
    "right-anterior-superior": (1, 1, 1), "ras": (1, 1, 1),
    "left-posterior-superior": (-1, -1, 1), "lps": (-1, -1, 1),
    "left-anterior-superior": (-1, 1, 1), "las": (-1, 1, 1),
}
_NRRD_OTHER_SPACE = (-1, -1, 1)


def _nrrdSpaceSigns(fields):
    """The RAS signs of the coordinates of a NRRD file: what 3D Slicer does, so the grid checked is the grid loaded."""
    return _NRRD_SPACES.get(fields.get("space", "").strip().lower(), _NRRD_OTHER_SPACE)


def _nrrdVector(text):
    text = text.strip()
    if text == "none":
        return None
    return [float(value) for value in text.strip("()").split(",")]


def _readNrrdFields(f):
    """The ``{lowercase key: value}`` header fields of an open NRRD file, left positioned at the first data byte; raises
    ValueError.
    """
    fields = {}
    magic = f.readline()
    if not magic.startswith(b"NRRD"):
        raise ValueError(_("Not a NRRD file"))
    for _line in range(200):
        line = f.readline()
        if not line or not line.strip():
            break
        text = line.decode("latin-1").rstrip("\r\n")
        if text.startswith("#") or ":" not in text:
            continue
        key, _separator, value = text.partition(":")
        fields[key.strip().lower()] = value.lstrip("=").strip()
    return fields


def _nrrdGeometry(fields):
    """``(sizes, vectors, spatial, affine)`` from NRRD header fields: the axis sizes, the space direction of every axis (None
    for a non-spatial one), the indices of the three spatial axes and the RAS affine of their voxel grid; raises ValueError.
    """
    np = _np()
    sizes = [int(value) for value in fields.get("sizes", "").split()]
    directions = re.findall(r"\([^)]*\)|none", fields.get("space directions", ""))
    if not sizes or not directions:
        raise ValueError(_("The NRRD header has no space directions"))
    vectors = [_nrrdVector(item) for item in directions]
    spatial = [index for index, vector in enumerate(vectors) if vector is not None]
    if len(spatial) != 3:
        raise ValueError(_("Not a 3D image"))
    signs = np.array(_nrrdSpaceSigns(fields), dtype=float)
    affine = np.eye(4)
    for column, index in enumerate(spatial):
        affine[:3, column] = np.array(vectors[index]) * signs
    origin = _nrrdVector(fields.get("space origin", "(0,0,0)")) or [0.0, 0.0, 0.0]
    affine[:3, 3] = np.array(origin) * signs
    return sizes, vectors, spatial, affine


def readNrrdHeader(path):
    """``{"shape", "affine" (RAS), "codes": None}`` of a NRRD file (header only); raises ValueError."""
    with open(path, "rb") as f:
        fields = _readNrrdFields(f)
    sizes, vectors, spatial, affine = _nrrdGeometry(fields)
    shape = tuple(sizes[index] for index in spatial)
    frames = 1
    for index, vector in enumerate(vectors):
        if vector is None:
            frames *= sizes[index]
    if len(sizes) > len(vectors):  # a "dimension" larger than the directions (non-spatial axis first)
        for size in sizes[: len(sizes) - len(vectors)]:
            frames *= size
    return {"shape": shape, "frames": frames, "affine": affine, "codes": None, "format": "NRRD"}


_NRRD_TYPES = {
    "int8": "i1", "signed char": "i1", "uint8": "u1", "uchar": "u1", "unsigned char": "u1",
    "int16": "i2", "short": "i2", "short int": "i2", "signed short": "i2", "signed short int": "i2",
    "uint16": "u2", "ushort": "u2", "unsigned short": "u2", "unsigned short int": "u2",
    "int32": "i4", "int": "i4", "signed int": "i4", "uint32": "u4", "uint": "u4", "unsigned int": "u4",
    "int64": "i8", "longlong": "i8", "long long": "i8", "signed long long": "i8",
    "uint64": "u8", "ulonglong": "u8", "unsigned long long": "u8",
    "float": "f4", "float32": "f4", "double": "f8", "float64": "f8",
}


def readNrrdData(path):
    """``(data, affine)`` of a NRRD file: the voxels of its first frame as an array indexed by the three spatial axes in header
    order (the columns of the RAS ``affine``). Attached raw, gzip and ascii data only; raises ValueError. Worker-safe.
    """
    np = _np()
    with open(path, "rb") as f:
        fields = _readNrrdFields(f)
        body = f.read()
    sizes, vectors, spatial, affine = _nrrdGeometry(fields)
    if len(sizes) != len(vectors) or "data file" in fields or "datafile" in fields:
        raise ValueError(_("Unsupported NRRD layout"))
    typeName = fields.get("type", "").lower()
    if typeName not in _NRRD_TYPES:
        raise ValueError(_("Unsupported NRRD voxel type"))
    dtype = np.dtype(_NRRD_TYPES[typeName])
    encoding = fields.get("encoding", "raw").lower()
    count = int(np.prod(sizes))
    if encoding in ("gzip", "gz"):
        import zlib

        body = zlib.decompress(body, 16 + zlib.MAX_WBITS)
    elif encoding in ("ascii", "text", "txt"):
        values = np.array(body.split(), dtype=np.float64)
        if values.size != count:
            raise ValueError(_("The NRRD data is incomplete"))
        data = values.reshape(sizes[::-1])
        encoding = None
    elif encoding != "raw":
        raise ValueError(_("Unsupported NRRD encoding"))
    if encoding is not None:
        if fields.get("endian", "little").lower() == "big":
            dtype = dtype.newbyteorder(">")
        if len(body) < count * dtype.itemsize:
            raise ValueError(_("The NRRD data is incomplete"))
        # raw data may carry trailing bytes; the voxels are the last ones when "byte skip: -1"
        start = len(body) - count * dtype.itemsize if fields.get("byte skip", "0").strip() == "-1" else int(fields.get("byte skip", "0"))
        data = np.frombuffer(body, dtype=dtype, count=count, offset=start).reshape(sizes[::-1])
    data = data.transpose(range(data.ndim)[::-1])  # axes in header order (the first one varies fastest in the file)
    index = tuple(slice(None) if axis in spatial else 0 for axis in range(data.ndim))
    return data[index], affine


def readHeader(path):
    """``{"shape" (3 voxel counts), "frames", "affine" (RAS 4x4), "codes" ((sform, qform) or None), "format", "xformMm",
    "pixdimMm"}``.

    NIfTI-1 through ``LesionNetworkMappingLib.nifti`` (header only), NRRD with a small reader. Raises ValueError for
    other files. ``xformMm`` is how far (mm) the sform and qform of a NIfTI file disagree, ``pixdimMm`` how far Slicer
    places a NIfTI file with an sform and no qform from its sform because the voxel sizes (pixdim) differ (both 0.0 for
    NRRD). See :func:`xformRefusal`.
    """
    extension = _lowerExtension(path)
    if extension in (".nii", ".nii.gz"):
        from LesionNetworkMappingLib import nifti

        header = nifti.read_nifti_header(path)
        shape = tuple(int(value) for value in header["shape"])
        frames = 1
        for value in shape[3:]:
            frames *= int(value)
        return {"shape": shape[:3], "frames": frames, "affine": header["affine"], "codes": (header["sform_code"], header["qform_code"]),
                "format": "NIfTI", "xformMm": float(nifti.xform_disagreement(header)), "pixdimMm": float(nifti.pixdim_disagreement(header))}
    if extension == ".nrrd":
        return dict(readNrrdHeader(path), xformMm=0.0, pixdimMm=0.0)
    raise ValueError(_("Use a NIfTI (.nii, .nii.gz) or NRRD (.nrrd) file"))


def xformRefusal(header):
    """The plain reason a file with the :func:`readHeader` ``header`` is refused, "" when Slicer loads it at its checked
    matrix (the sform of a NIfTI file): refused when the sform and qform disagree (Slicer loads the qform) or when the
    voxel sizes of a file without a qform do not match its sform (Slicer uses the voxel sizes).
    """
    if header.get("xformMm", 0.0) > XFORM_REFUSAL_MM:
        return _(XFORM_REFUSAL).format(distance=header["xformMm"])
    if header.get("pixdimMm", 0.0) > XFORM_REFUSAL_MM:
        spacing = voxelSpacing(header["affine"])
        return _(PIXDIM_REFUSAL).format(distance=header["pixdimMm"], spacing=" × ".join(f"{value:.4g}" for value in spacing))  # noqa: RUF001
    return ""


def worldBox(shape, affine, tolerance=1e-3):
    """``(worldShape, spacing, lowest)`` along R, A, S of an axis-aligned grid, None when it is oblique."""
    np = _np()
    affine = np.asarray(affine, dtype=float)
    linear = affine[:3, :3]
    axes = np.argmax(np.abs(linear), axis=0)
    if len(set(axes.tolist())) != 3:
        return None
    steps = linear[axes, np.arange(3)]
    offAxis = linear.copy()
    offAxis[axes, np.arange(3)] = 0.0
    if np.abs(offAxis).max() > tolerance * np.abs(steps).max():
        return None
    worldShape = [0, 0, 0]
    spacing = np.zeros(3)
    lowest = affine[:3, 3].copy()
    for axis in range(3):
        worldAxis = int(axes[axis])
        worldShape[worldAxis] = int(shape[axis])
        spacing[worldAxis] = abs(steps[axis])
        if steps[axis] < 0:
            lowest[worldAxis] += steps[axis] * (int(shape[axis]) - 1)
    return tuple(worldShape), spacing, lowest


def worldBounds(shape, affine):
    """``(low, high)`` RAS corners (mm) of the voxel centres of a grid (any orientation)."""
    np = _np()
    corners = np.array([[i, j, k, 1.0] for i in (0, shape[0] - 1) for j in (0, shape[1] - 1) for k in (0, shape[2] - 1)])
    world = (np.asarray(affine, dtype=float) @ corners.T).T[:, :3]
    return world.min(axis=0), world.max(axis=0)


def orientationCode(affine):
    """"RAS", "LAS", "LPS"... : the world direction of each voxel axis."""
    np = _np()
    letters = (("L", "R"), ("P", "A"), ("I", "S"))
    linear = np.asarray(affine, dtype=float)[:3, :3]
    code = ""
    for column in range(3):
        axis = int(np.argmax(np.abs(linear[:, column])))
        code += letters[axis][1 if linear[axis, column] > 0 else 0]
    return code


def voxelSpacing(affine):
    np = _np()
    return np.sqrt((np.asarray(affine, dtype=float)[:3, :3] ** 2).sum(axis=0))


def sameGrid(firstShape, firstAffine, secondShape, secondAffine, tolerance=GRID_TOLERANCE_MM):
    """True when two grids hold the same voxel centres (any storage order: RAS and LAS copies match)."""
    np = _np()
    first, second = worldBox(firstShape, firstAffine), worldBox(secondShape, secondAffine)
    if first is not None and second is not None:
        return (first[0] == second[0] and bool(np.all(np.abs(first[1] - second[1]) <= tolerance))
                and bool(np.all(np.abs(first[2] - second[2]) <= tolerance)))
    return tuple(firstShape) == tuple(secondShape) and np.allclose(firstAffine, secondAffine, atol=tolerance, rtol=0)


#
# Identify (worker-safe)
#


class Detection:
    """What ``identify()`` found in a file's header.

    :ivar ok: the file can be added as an MNI template.
    :ivar status: "known" (a known template grid), "mni" (looks like MNI space), "refused" or "error".
    :ivar message: the detection line ("Looks like MNI space", or the refusal).
    :ivar facts: "394 x 466 x 378 · 0.5 mm isotropic · RAS · header says MNI (sform 4)".
    :ivar spaceGuess: a space id (``SPACES``) or None; ``candidates``: the spaces a known grid may hold.
    :ivar warnings: lines shown under the facts (anisotropic voxels, a 4D file, an unusual field of view).
    :ivar privacyNote: shown when the image may be a person's scan rather than a template (not blocking).
    :ivar frames: volumes in the file (a 4D file is added with its first volume only, ``firstVolume``).
    """

    def __init__(self, path):
        self.path = path
        self.ok = False
        self.status = "error"
        self.message = ""
        self.facts = ""
        self.shape = None
        self.frames = 1
        self.affine = None
        self.spacing = None
        self.orientation = ""
        self.spaceGuess = None
        self.candidates = ()
        self.resolution = None
        self.warnings = []
        self.privacyNote = None
        self.codes = None
        self.format = ""

    @property
    def is4d(self):
        return self.frames > 1

    def __repr__(self):
        return f"Detection({os.path.basename(str(self.path))!r}, status={self.status!r}, space={self.spaceGuess!r})"


def _codeText(codes):
    if codes is None:
        return _("no MNI code in the header (NRRD)")
    sform, qform = codes
    code, which = (sform, "sform") if sform > 0 else (qform, "qform")
    names = {0: _("no space code"), 1: _("scanner"), 2: _("aligned"), 3: _("Talairach"), 4: _("MNI"), 5: _("template")}
    if code == 4:
        return _("header says MNI ({which} 4)").format(which=which)
    return _("header says {name} ({which} {code})").format(name=names.get(code, str(code)), which=which, code=code)


def _looksMni(shape, affine):
    """``(boxContains, centreDistanceMm)`` of a grid compared with the MNI brain box and centre."""
    np = _np()
    low, high = worldBounds(shape, affine)
    contains = all(low[axis] <= MNI_BRAIN_BOX[axis][0] and high[axis] >= MNI_BRAIN_BOX[axis][1] for axis in range(3))
    center = (low + high) / 2.0
    return contains, float(np.linalg.norm(center - np.asarray(MNI_CENTER)))


def identify(path):
    """Check a file (header only) before it is added as a template; return a :class:`Detection`."""
    np = _np()
    detection = Detection(path)
    try:
        header = readHeader(path)
    except Exception as error:
        detection.status = "error"
        detection.message = _("This file cannot be read as an image: {error}").format(error=error)
        return detection
    shape, affine = tuple(header["shape"]), np.asarray(header["affine"], dtype=float)
    detection.shape, detection.frames, detection.affine, detection.codes, detection.format = shape, header["frames"], affine, header["codes"], header["format"]
    detection.spacing = voxelSpacing(affine)
    detection.orientation = orientationCode(affine)
    spacing = detection.spacing
    isotropic = float(spacing.max()) <= float(spacing.min()) * 1.001
    sizeText = " × ".join(str(n) for n in shape)  # noqa: RUF001
    spacingText = _("{size:g} mm isotropic").format(size=float(spacing.mean())) if isotropic else " × ".join(f"{value:g}" for value in spacing) + " mm"  # noqa: RUF001
    detection.resolution = round(float(spacing.mean()), 3) if isotropic else round(float(spacing.min()), 3)
    facts = [sizeText, spacingText, detection.orientation, _codeText(detection.codes)]
    if float(spacing.max()) > ANISOTROPY_WARNING * float(spacing.min()):
        detection.warnings.append(_("The voxels are not cubic ({spacing} mm): drawing and maps work best on near-isotropic templates.").format(
            spacing=" × ".join(f"{value:g}" for value in spacing)))  # noqa: RUF001
    if detection.frames > 1:
        detection.warnings.append(_("The file holds {count} volumes: only the first is used.").format(count=detection.frames))
        if header["format"] != "NIfTI":
            detection.status, detection.message = "refused", _("A NRRD file with several volumes cannot be used: save its first volume as NIfTI.")
            detection.facts = " · ".join(facts)
            return detection
    refusal = xformRefusal(header)
    if refusal:
        # The checked matrix (the sform) would not be the one the scene uses (Slicer loads the qform, or the pixdim voxel sizes)
        detection.status, detection.message = "refused", refusal
        detection.facts = " · ".join(facts)
        return detection
    match = None
    try:
        from LesionNetworkMappingLib import geometry

        match = geometry.detect_grid_candidates(shape, affine, origin_tolerance=GRID_TOLERANCE_MM)
    except ImportError:
        match = None
    contains, distance = _looksMni(shape, affine)
    sform = (detection.codes[0] if detection.codes[0] > 0 else detection.codes[1]) if detection.codes is not None else None
    if match is not None:
        detection.ok, detection.status = True, "known"
        detection.candidates = tuple(match.candidates)
        detection.spaceGuess = match.space if match.space in SPACE_IDS else None
        detection.resolution = match.resolution
        facts[-1] = _("known grid of {space}").format(space=match.space)
        detection.message = _("Looks like MNI space")
    elif contains and distance <= CENTER_TOLERANCE_MM:
        detection.ok, detection.status = True, "mni"
        detection.message = _("Looks like MNI space")
        if sform != 4:
            detection.privacyNote = _("The header does not say MNI. If this is a person's scan rather than a template, open it as a scan "
                                      "instead: templates are listed and shared by name.")
    elif sform == 4 and distance <= 2 * CENTER_TOLERANCE_MM:
        detection.ok, detection.status = True, "mni"
        detection.message = _("Header says MNI")
        detection.warnings.append(_("Its field of view is unusual for an MNI brain (a cropped template?): check the sides after adding it."))
    else:
        detection.status = "refused"
        detection.message = _(REFUSAL)
    if detection.ok:
        # Drawing and maps use any MNI grid; Normalize registers to the image's own grid, which needs more
        problem = normalizeTargetProblem(path, shape, affine)
        if problem:
            detection.warnings.append(problem)
    detection.facts = " · ".join(facts)
    return detection


def normalizeTargetProblem(imagePath, shape, affine):
    """Why a template image cannot be a normalization target (a plain sentence), or None when it can: Normalize needs a
    NIfTI image whose voxels are cubic and aligned with the axes (it registers scans to that grid). Worker-safe.
    """
    if _lowerExtension(imagePath) not in (".nii", ".nii.gz"):
        return _("Normalize cannot use it as a target: save the image as NIfTI (.nii.gz) to normalize scans to this template.")
    try:
        from LesionNetworkMappingLib import geometry
    except ImportError:
        return None
    try:
        spec = geometry.grid_spec_from_affine("check", tuple(int(n) for n in shape)[:3], _np().asarray(affine, dtype=float), OTHER_SPACE)
    except Exception:
        spec = None
    if spec is None:
        return _("Normalize cannot use it as a target: that needs cubic voxels aligned with the axes. Drawing and maps work.")
    return None


def checkCompanion(detection, path):
    """A brain mask or head image: ``(ok, reason)``; it must share the template image's grid."""
    try:
        header = readHeader(path)
    except Exception as error:
        return False, _("This file cannot be read as an image: {error}").format(error=error)
    if detection is None or detection.shape is None:
        return False, _("Choose the template image first.")
    refusal = xformRefusal(header)
    if refusal:
        return False, os.path.basename(path) + ": " + refusal
    if not sameGrid(header["shape"], header["affine"], detection.shape, detection.affine):
        return False, _("{name} is not on the grid of the template image ({size}).").format(
            name=os.path.basename(path), size=" × ".join(str(n) for n in detection.shape))  # noqa: RUF001
    return True, ""


#
# Surfaces (main thread: VTK readers through Slicer's model storage node)
#


# Template surfaces are meshes in MNI coordinates, which are RAS: files that state no coordinate system are read as RAS.
# A SPACE=RAS/LPS header (every file Slicer writes has one) still decides.
SURFACE_COORDINATE_SYSTEM = slicer.vtkMRMLStorageNode.CoordinateSystemRAS


def surfaceReaderAvailable(path):
    """True if Slicer can read this surface file (GIfTI only with a reader, e.g. an extension)."""
    storage = slicer.vtkMRMLModelStorageNode()
    return bool(storage.SupportedFileType(path)) if hasattr(storage, "SupportedFileType") else _lowerExtension(path) != ".gii"


def _readSurfaceBounds(path):
    storage = slicer.vtkMRMLModelStorageNode()
    # MNI coordinates are RAS: a file without a SPACE= header (FreeSurfer, BrainNet, nibabel exports) is RAS, not Slicer's LPS default.
    storage.SetCoordinateSystem(SURFACE_COORDINATE_SYSTEM)
    storage.SetFileName(path)
    model = slicer.vtkMRMLModelNode()
    if not storage.ReadData(model):
        raise ValueError(_("The surface could not be read"))
    polyData = model.GetPolyData()
    if polyData is None or polyData.GetNumberOfPoints() == 0:
        raise ValueError(_("The surface is empty"))
    return polyData.GetBounds()


def checkSurface(path):
    """``(ok, reason)`` of a 3D brain surface: readable, and its bounds within the MNI box (± ``SURFACE_TOLERANCE_MM``)."""
    if not isSurfaceFile(path):
        return False, _("Use a .vtk, .vtp, .stl, .obj, .ply or .gii surface")
    if not surfaceReaderAvailable(path):
        return False, _("This Slicer cannot read {extension} surfaces (a GIfTI reader comes with extensions such as SlicerFreeSurfer).").format(
            extension=_lowerExtension(path))
    try:
        bounds = _readSurfaceBounds(path)
    except Exception as error:
        return False, str(error)
    allowed = [(low - SURFACE_TOLERANCE_MM, high + SURFACE_TOLERANCE_MM) for low, high in MNI_BRAIN_BOX]
    inside = all(allowed[axis][0] <= bounds[2 * axis] and bounds[2 * axis + 1] <= allowed[axis][1] for axis in range(3))
    large = all(bounds[2 * axis + 1] - bounds[2 * axis] >= 20.0 for axis in range(3))
    if not inside or not large:
        return False, _("{name} does not lie in MNI space (bounds {bounds} mm).").format(
            name=os.path.basename(path), bounds=", ".join(f"{value:.0f}" for value in bounds))
    return True, ""


#
# Templates
#


class Template:
    """A template of the registry (built-in or yours). Read its fields; change it through this module only."""

    def __init__(self, record, folder=None, builtin=False, files=None):
        self.record = dict(record)
        self.folder = folder
        self.builtin = builtin
        self._files = dict(files or {})
        slug = str(record.get("id") or "")
        self.id = slug if builtin else USER_PREFIX + slug
        self.slug = slug
        self.name = str(record.get("name") or slug)
        self.space = str(record.get("space") or OTHER_SPACE)
        self.spaceLabel = record.get("space_label") or None
        resolution = record.get("resolution_mm")
        self.resolution = (int(resolution) if float(resolution).is_integer() else float(resolution)) if resolution is not None else None
        self.shape = tuple(record.get("shape") or ())
        self.contrast = str(record.get("contrast") or "")
        self.notes = str(record.get("notes") or "")
        self.added = record.get("added")
        self.sha256 = dict(record.get("sha256") or {})
        self.grid = record.get("grid") or {}

    def __repr__(self):
        return f"Template({self.id!r}, {self.name!r})"

    @property
    def isUser(self):
        return not self.builtin

    def path(self, role):
        """Absolute path of "image", "mask" or "head" (None when the template has none, or a built-in one is missing)."""
        if self.builtin:
            return self._files.get(role)
        name = (self.record.get("files") or {}).get(role)
        return os.path.join(self.folder, *str(name).split("/")) if name and self.folder else None

    @property
    def imagePath(self):
        return self.path("image")

    @property
    def maskPath(self):
        return self.path("mask")

    @property
    def headPath(self):
        return self.path("head")

    @property
    def surfacePaths(self):
        if self.builtin or not self.folder:
            return []
        return [os.path.join(self.folder, *str(name).split("/")) for name in (self.record.get("files") or {}).get("surfaces") or []]

    @property
    def imageSha256(self):
        return self.sha256.get("image")

    def spaceDisplay(self):
        return spaceDisplayName(self.space, self.spaceLabel)

    def metaLine(self):
        """"MNI152NLin2009bAsym · 0.5 mm · T1w" (plus "with brain mask" for a template you added)."""
        parts = [self.spaceDisplay(), resolutionText(self.resolution), self.contrast]
        if self.isUser and self.maskPath:
            parts.append(_("with brain mask"))
        return " · ".join(part for part in parts if part)

    def studioSpaceValue(self):
        """The ``Studio.Space`` attribute of its volume: "MNI152NLin2009bAsym 0.5"."""
        return f"{self.space} {self.resolution:g}" if self.resolution is not None else self.space

    def isAvailable(self):
        image = self.imagePath
        return bool(image) and os.path.isfile(image)

    def canNormalize(self):
        """True when it can be a normalization target: a brain mask, and a NIfTI image whose recorded grid has cubic voxels
        aligned with the axes (``normalizeTargetProblem``; Normalize refuses other grids).
        """
        image, mask = self.imagePath, self.maskPath
        if not (image and mask and os.path.isfile(image) and os.path.isfile(mask)):
            return False
        grid = self.grid
        if grid.get("shape") and grid.get("affine"):
            return normalizeTargetProblem(image, grid["shape"], grid["affine"]) is None
        return _lowerExtension(image) in (".nii", ".nii.gz")


def _sampleFile(sampleName):
    """The local file of a SampleData template: bundled with SampleData, else downloaded before; None otherwise."""
    try:
        import SampleData

        source = SampleData.SampleDataLogic().sourceForSampleName(sampleName)
    except Exception:
        source = None
    if source is not None:
        for uri in source.uris:
            uri = str(uri)
            if uri.startswith("file://"):
                path = qt.QUrl(uri).toLocalFile()
                if os.path.isfile(path):
                    return path
        fileName = source.fileNames[0] if source.fileNames else None
    else:
        fileName = None
    if not fileName:
        return None
    folders = []
    try:
        from . import downloads

        folders.append(downloads.cacheDirectory())
    except Exception:
        pass
    cacheManager = slicer.mrmlScene.GetCacheManager()
    if cacheManager is not None:
        folders.append(cacheManager.GetRemoteCacheDirectory())
    for folder in folders:
        path = os.path.join(folder or "", fileName)
        if folder and os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
    return None


def builtinTemplates(includeMissing=False):
    """The built-in templates: the bundled 2 mm brain always, the others when they were downloaded (``includeMissing``: all)."""
    found = []
    for templateId, name, spaceId, resolution, contrast, imageSample, maskSample in _BUILTINS:
        image = _sampleFile(imageSample)
        if image is None and not includeMissing and templateId != BUILTIN_DEFAULT:
            continue
        files = {"image": image, "mask": _sampleFile(maskSample) if maskSample else None, "sample": imageSample}
        record = {"id": templateId, "name": name, "space": spaceId, "resolution_mm": resolution, "contrast": contrast}
        found.append(Template(record, builtin=True, files=files))
    return found


def sampleNameOf(template):
    """The SampleData name of a built-in template's image (None for yours)."""
    return template._files.get("sample") if template is not None and template.builtin else None


def templatesFolder(rootPath=None):
    """<Slicer Studio folder>/Templates (not created here)."""
    if rootPath is None:
        from . import projects

        rootPath = projects.root()
    return os.path.join(rootPath, TEMPLATES_DIR)


def _isNumber(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _isNumberList(value, allowNested=False):
    if not isinstance(value, (list, tuple)):
        return False
    return all(_isNumberList(item) if allowNested else _isNumber(item) for item in value)


def _recordIsUsable(record):
    """True when the fields ``Template`` and its users read have the types they expect. A template.json edited by hand (or
    written by another tool) with, say, ``"resolution_mm": "2 mm"`` is then skipped like an unreadable one, instead of
    making every listing of the templates fail.
    """
    resolution = record.get("resolution_mm")
    if resolution is not None and not _isNumber(resolution):
        return False
    shape = record.get("shape")
    if shape is not None and not (isinstance(shape, (list, tuple)) and all(isinstance(item, int) and not isinstance(item, bool) for item in shape)):
        return False
    sha256 = record.get("sha256")
    if sha256 is not None and not isinstance(sha256, dict):
        return False
    grid = record.get("grid")
    if grid is not None:
        if not isinstance(grid, dict):
            return False
        if grid.get("shape") is not None and not _isNumberList(grid["shape"]):
            return False
        if grid.get("affine") is not None and not _isNumberList(grid["affine"], allowNested=True):
            return False
    files = record.get("files")
    if files is not None:
        if not isinstance(files, dict):
            return False
        if any(files.get(role) is not None and not isinstance(files[role], str) for role in ("image", "mask", "head")):
            return False
        surfaces = files.get("surfaces")
        if surfaces is not None and not (isinstance(surfaces, (list, tuple)) and all(isinstance(item, str) for item in surfaces)):
            return False
    return True


def readRecord(folder):
    """The template.json of a template folder, or None (unreadable, not a template, or with a field of the wrong type)."""
    try:
        with open(os.path.join(folder, TEMPLATE_JSON), encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("schema") != SCHEMA or not record.get("id"):
        return None
    if not _recordIsUsable(record):
        return None
    return record


def userTemplates(rootPath=None):
    """Your templates (folders of Templates/ with a template.json), sorted by name. Each has its own id: a folder copied
    in by hand with the id of another one gets its own first (``_makeCopiedIdsUnique``).
    """
    folder = templatesFolder(rootPath)
    entries = []
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    for name in names:
        path = os.path.join(folder, name)
        if name.startswith(".") or not os.path.isdir(path):
            continue
        record = readRecord(path)
        if record is not None:
            entries.append((name, path, record))
    _makeCopiedIdsUnique(entries)
    found = []
    for name, path, record in entries:
        try:
            found.append(Template(record, folder=path))
        except (TypeError, ValueError, AttributeError):
            logging.warning(f"Slicer Studio: the template folder {name} could not be read and is skipped")
    found.sort(key=lambda template: template.name.lower())
    return found


def _arrivalRank(name, path, record):
    """Sort key of the template folders that share an id: the one that was there first keeps it. That is the folder an add
    named after the template ("Lab ICBM", or "Lab ICBM 2" when that name was taken), then the oldest template.json (a
    copy is a new file: its creation, or inode change, time is that of the copy), then the folder name.
    """
    from . import projects

    base = projects.sanitizeName(str(record.get("name") or record.get("id")), fallback="Template").lower()
    lower = name.lower()
    named = lower == base or (lower.startswith(base + " ") and lower[len(base) + 1:].isdigit())
    try:
        info = os.stat(os.path.join(path, TEMPLATE_JSON))
        arrived = getattr(info, "st_birthtime", None) or info.st_ctime
    except OSError:
        arrived = float("inf")
    return (0 if named else 1, arrived, name)


def _makeCopiedIdsUnique(entries):
    """Give each template folder of ``entries`` (``(folder name, path, record)`` of one Templates/ folder) whose id another
    one there already has its own id and name, written into its template.json: a folder copied into Templates/ by hand
    (a colleague's template, a duplicate made in the file manager) keeps everything but its id. The folder that was there
    first keeps the id (``_arrivalRank``), so what refers to it (the default, scans, studies) stays its; the copy gets
    "My template 2" ("my-template-2") as an add would name it (the rule of ``makeMovedIdsUnique`` after a move). The
    records of ``entries`` are replaced. Worker-safe (files only).
    """
    groups = {}
    for index, (name, path, record) in enumerate(entries):
        groups.setdefault(str(record.get("id")), []).append(index)
    duplicates = [indexes for indexes in groups.values() if len(indexes) > 1]
    if not duplicates:
        return {}

    def nameOf(record):
        return str(record.get("name") or record.get("id")).strip().lower()

    takenSlugs = set(groups)
    changed = {}
    for indexes in duplicates:
        indexes = sorted(indexes, key=lambda index: _arrivalRank(*entries[index]))
        for index in indexes[1:]:
            name, path, record = entries[index]
            slug = str(record.get("id"))
            baseName = str(record.get("name") or slug)
            # A name another template has (without case) becomes "<name> 2"; a name it alone has stays
            takenNames = {nameOf(other[2]) for position, other in enumerate(entries) if position != index}
            newName, counter = baseName, 2
            while newName.strip().lower() in takenNames:
                newName = f"{baseName} {counter}"
                counter += 1
            base = slugOf(newName)
            newSlug, counter = base, 2
            while newSlug in takenSlugs:
                newSlug = f"{base}-{counter}"
                counter += 1
            updated = dict(record, id=newSlug, name=newName)
            if not _writeRecord(path, updated):
                # A folder that cannot be written (read-only) still gets its own id while it is listed
                if path not in _unwritableCopies:
                    _unwritableCopies.add(path)
                    logging.warning(f"Slicer Studio: the copied template {baseName} could not be given its own id in its template.json")
            entries[index] = (name, path, updated)
            takenSlugs.add(newSlug)
            changed[USER_PREFIX + slug] = USER_PREFIX + newSlug
            # The cached picture of the shared id may be the copy's
            forgetThumbnails(USER_PREFIX + slug)
    return changed


def _writeRecord(folder, record):
    """Replace the template.json of ``folder`` with ``record`` (atomically); return True when it was written."""
    target = os.path.join(folder, TEMPLATE_JSON)
    staging = f"{target}.renaming-{os.getpid()}-{threading.get_ident()}"
    try:
        with open(staging, "w", encoding="utf-8", newline="\n") as f:
            json.dump(record, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(staging, target)
        return True
    except OSError:
        with contextlib.suppress(OSError):
            os.remove(staging)
        return False


def listTemplates(rootPath=None):
    """Every template: the built-in ones first, then yours."""
    return builtinTemplates() + userTemplates(rootPath)


def templateById(templateId, rootPath=None):
    """The template with this id ("user:<slug>", a bare slug, or "builtin:<name>"), or None."""
    if not templateId:
        return None
    templateId = str(templateId)
    if templateId.startswith(BUILTIN_PREFIX):
        return next((template for template in builtinTemplates(includeMissing=True) if template.id == templateId), None)
    slug = templateId[len(USER_PREFIX):] if templateId.startswith(USER_PREFIX) else templateId
    return next((template for template in userTemplates(rootPath) if template.slug == slug), None)


def templateByChecksum(sha256, rootPath=None):
    """Your template whose image has this sha256, or None."""
    if not sha256:
        return None
    return next((template for template in userTemplates(rootPath) if template.imageSha256 == sha256), None)


def templateForGrid(shape, affine, rootPath=None):
    """Your template on this grid (shape and affine, any storage order), or None."""
    for template in userTemplates(rootPath):
        grid = template.grid
        if grid.get("shape") and grid.get("affine") and sameGrid(grid["shape"], grid["affine"], shape, affine):
            return template
    return None


def templateOfNode(node):
    """The template of a volume tagged ``Studio.TemplateId`` (yours or built-in), or None (also when the volume is not on
    the grid of your template with that id: the volume of a template you removed, then added again under the same name).
    """
    if node is None or not hasattr(node, "GetAttribute"):
        return None
    templateId = node.GetAttribute(TEMPLATE_ID_ATTRIBUTE)
    template = templateById(templateId) if templateId else None
    return template if template is not None and isNodeOnTemplateGrid(node, template) else None


def isNodeOnTemplateGrid(node, template):
    """True when the volume ``node`` holds the grid recorded for your template ``template`` (always True for a built-in
    template, a template without a recorded grid, and a node that is not a volume with an image).

    The id of your template is the slug of its name, so a template removed and added again under the same name has the
    same id: the grid tells its volume apart from the volume of the removed one. The tolerance allows the sform and qform
    disagreement that an added template may have (Slicer places the image at the qform, the recorded grid is the sform).
    """
    if template is None or template.builtin or node is None or not hasattr(node, "IsA") or not node.IsA("vtkMRMLVolumeNode"):
        return True
    grid = template.grid
    imageData = node.GetImageData()
    if not grid.get("shape") or not grid.get("affine") or imageData is None:
        return True
    import vtk

    matrix = vtk.vtkMatrix4x4()
    node.GetIJKToRASMatrix(matrix)
    affine = [[matrix.GetElement(row, column) for column in range(4)] for row in range(4)]
    try:
        return sameGrid(grid["shape"], grid["affine"], imageData.GetDimensions(), affine, tolerance=XFORM_REFUSAL_MM + GRID_TOLERANCE_MM)
    except Exception:
        logging.debug("Slicer Studio: template grid comparison failed", exc_info=True)
        return True


def forgetTemplateNodes(templateId, nodes=None):
    """Remove the ``Studio.TemplateId`` = ``templateId`` tag of scene volumes and template surfaces (or of ``nodes``): they no
    longer show that template (it was removed, or a template added again under the same name has another image). Their
    ``Studio.Role`` and ``Studio.Space`` stay, so lesions drawn on them keep their space. Return the nodes untagged.
    """
    if not templateId:
        return []
    if nodes is None:
        nodes = list(slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")) + list(slicer.util.getNodesByClass("vtkMRMLModelNode"))
    forgotten = []
    for node in nodes:
        if node is not None and node.GetAttribute(TEMPLATE_ID_ATTRIBUTE) == templateId:
            node.RemoveAttribute(TEMPLATE_ID_ATTRIBUTE)
            forgotten.append(node)
    return forgotten


def userTemplateOfNode(node):
    template = templateOfNode(node)
    return template if template is not None and template.isUser else None


#
# Default
#


def defaultTemplateId():
    """The id of the default template: ``Studio/Templates/Default`` when that template's image is on this computer, else
    the bundled 2 mm brain (a downloaded built-in whose cache was cleared is never downloaded again without a click).
    """
    value = qt.QSettings().value(DEFAULT_KEY)
    template = templateById(str(value)) if value else None
    if template is not None and template.isAvailable():
        return str(value)
    return BUILTIN_DEFAULT


def defaultTemplate():
    return templateById(defaultTemplateId())


def setDefault(templateId):
    """Make a template the default of "Start on template" (writes only ``Studio/Templates/Default``)."""
    template = templateById(templateId)
    if template is None:
        raise ValueError(_("No template {id}").format(id=templateId))
    qt.QSettings().setValue(DEFAULT_KEY, template.id)
    _notify()
    return template


#
# Grids of your templates (geometry extension point; space.spaceOf recognizes them)
#


def registerGrids(rootPath=None, force=False):
    """Register the grids of your templates with ``LesionNetworkMappingLib.geometry`` (isotropic, axis-aligned ones), so
    that ``detect_space`` and the Map and Library space checks recognize them. Cheap when nothing changed.
    """
    try:
        from LesionNetworkMappingLib import geometry
    except ImportError:
        return False
    root = templatesFolder(rootPath)
    # A template folder added or removed outside the app changes the folder's time stamp
    stamp = _folderStamp(root)
    if not force and _registered["root"] == root and _registered["stamp"] == stamp:
        return True
    for gridId in list(_registered["ids"]):
        geometry.unregister_grid(gridId)
    _registered["ids"] = set()
    _registered["others"] = []
    for template in userTemplates(rootPath):
        grid = template.grid
        if not grid.get("shape") or not grid.get("affine"):
            continue
        try:
            spec = geometry.grid_spec_from_affine(template.id, grid["shape"], grid["affine"], template.space)
            if spec is not None:
                geometry.register_grid(spec)
                _registered["ids"].add(template.id)
            else:
                # Not cubic or oblique: recognized by its exact grid (gridSpace)
                _registered["others"].append((template.space, template.resolution, tuple(grid["shape"]), grid["affine"]))
        except Exception:
            logging.warning(f"Slicer Studio: the grid of template {template.name} could not be registered")
    _registered["root"] = root
    _registered["stamp"] = stamp
    return True


def _folderStamp(folder):
    """The modification time of a folder (None when it does not exist)."""
    try:
        return os.stat(folder).st_mtime_ns
    except OSError:
        return None


def gridSpace(shape, affine):
    """``(space, resolution)`` of one of your templates whose grid geometry cannot be registered (anisotropic or oblique
    voxels) and that has exactly this grid, else None (registered grids are found by ``geometry.detect_space``).
    """
    for spaceId, resolution, gridShape, gridAffine in _registered["others"]:
        if sameGrid(gridShape, gridAffine, shape, affine):
            return spaceId, resolution
    return None


def ensureGridsRegistered():
    """``registerGrids()`` once per Slicer Studio folder (the folder can change in Settings)."""
    try:
        return registerGrids()
    except Exception:
        logging.debug("Slicer Studio: template grids not registered", exc_info=True)
        return False


#
# Adding
#


class AddPlan:
    """A checked request to add a template (``prepareAdd``); the copy runs in ``copyFiles`` (worker-safe)."""

    def __init__(self):
        self.detection = None
        self.items = []  # (source path, name in the folder, role)
        self.record = {}
        self.folder = None
        self.firstVolume = False
        self.makeDefault = False
        self.totalBytes = 0
        self.progress = 0  # bytes copied (read by the main thread)
        self.cancelled = False
        self.requiredSha256 = None  # the image must have this checksum (re-adding the template of a study)


class TemplateError(Exception):
    pass


def _uniqueFolderName(parent, name):
    from . import projects

    return projects.uniqueName(parent, projects.sanitizeName(name, fallback="Template"), isFile=False)


def _uniqueSlug(name, rootPath=None):
    taken = {template.slug for template in userTemplates(rootPath)}
    base = slugOf(name)
    slug, counter = base, 2
    while slug in taken:
        slug = f"{base}-{counter}"
        counter += 1
    return slug


def makeMovedIdsUnique(folder, movedNames, recordId=None):
    """Give each template of ``folder`` (a Templates/ folder) whose folder name is in ``movedNames`` (the templates that a
    move of the Slicer Studio folder merged in) its own id and name when another template there already uses its id or
    its name (without case): "My template" -> "My template 2" ("my-template-2"), as an add would name it. The templates
    that were there keep theirs, so what refers to them (scans, studies, the default) stays theirs. Return
    ``{old id: new id}`` ("user:<slug>") of the templates that changed. ``recordId(old id, new id)`` is called before a
    template gets its new id (the journal of the move, ``projects.MoveJournal``). Worker-safe (files only).
    """
    entries = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return {}
    for name in names:
        path = os.path.join(folder, name)
        if name.startswith(".") or not os.path.isdir(path):
            continue
        record = readRecord(path)
        if record is not None:
            entries.append((name, path, record))
    moved = set(movedNames)
    kept = [record for name, path, record in entries if name not in moved]
    takenSlugs = {str(record.get("id")) for record in kept}
    takenNames = {str(record.get("name") or record.get("id")).strip().lower() for record in kept}
    # A new id or name never takes one that another moved template keeps
    reservedSlugs = {str(record.get("id")) for name, path, record in entries if name in moved}
    reservedNames = {str(record.get("name") or record.get("id")).strip().lower() for name, path, record in entries if name in moved}
    changed = {}
    for name, path, record in entries:
        if name not in moved:
            continue
        slug = str(record.get("id"))
        baseName = str(record.get("name") or slug)
        newName, counter = baseName, 2
        if newName.strip().lower() in takenNames:
            while newName.strip().lower() in takenNames or newName.strip().lower() in reservedNames:
                newName = f"{baseName} {counter}"
                counter += 1
        newSlug = slug
        if newSlug in takenSlugs:
            base = slugOf(newName)
            newSlug, counter = base, 2
            while newSlug in takenSlugs or newSlug in reservedSlugs:
                newSlug = f"{base}-{counter}"
                counter += 1
        if newSlug != slug or newName != baseName:
            updated = dict(record, id=newSlug, name=newName)
            if recordId is not None and newSlug != slug:
                recordId(USER_PREFIX + slug, USER_PREFIX + newSlug)
            if not _writeRecord(path, updated):
                logging.warning(f"Slicer Studio: the moved template {baseName} could not be renamed")
                newSlug, newName = slug, baseName
            if newSlug != slug:
                changed[USER_PREFIX + slug] = USER_PREFIX + newSlug
        takenSlugs.add(newSlug)
        takenNames.add(newName.strip().lower())
    return changed


def followRenamedIds(renamedIds):
    """Main thread, after a move gave moved templates new ids (``makeMovedIdsUnique``): the default, the remembered
    Normalize options (``REMEMBERED_TEMPLATE_SETTINGS``), the ids held by rename listeners (the Map card's chosen
    template) and the scene's volumes and surfaces of those templates follow them (they came from the moved folder), and
    the lists refresh.
    """
    if not renamedIds:
        return
    settings = qt.QSettings()
    value = str(settings.value(DEFAULT_KEY) or "")
    if value in renamedIds:
        settings.setValue(DEFAULT_KEY, renamedIds[value])
    for key, field in REMEMBERED_TEMPLATE_SETTINGS:
        _followRenamedIdInSetting(settings, key, field, renamedIds)
    nodes = list(slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")) + list(slicer.util.getNodesByClass("vtkMRMLModelNode"))
    tagged = [(node, node.GetAttribute(TEMPLATE_ID_ATTRIBUTE)) for node in nodes if node is not None]
    for node, templateId in tagged:
        if templateId in renamedIds:
            node.SetAttribute(TEMPLATE_ID_ATTRIBUTE, renamedIds[templateId])
    for oldId, newId in renamedIds.items():
        forgetThumbnails(oldId)
        forgetThumbnails(newId)
    for callback in list(_renameListeners):
        try:
            callback(dict(renamedIds))
        except Exception:
            logging.error("Slicer Studio: a listener of renamed template ids failed", exc_info=True)
    _notify()


def _followRenamedIdInSetting(settings, key, field, renamedIds):
    """The template id in ``field`` of the JSON setting ``key`` follows ``renamedIds`` (an unreadable value is left alone).

    Without this, the remembered id would name the template of the target folder that kept it: another image and space.
    """
    if not settings.contains(key):
        return
    try:
        stored = json.loads(str(settings.value(key) or ""))
    except ValueError:
        return
    if not isinstance(stored, dict):
        return
    templateId = stored.get(field)
    if isinstance(templateId, str) and templateId in renamedIds:
        stored[field] = renamedIds[templateId]
        settings.setValue(key, json.dumps(stored, sort_keys=True))


def nameTaken(name, rootPath=None):
    """True if a template (yours or built-in) already has this name (without case)."""
    wanted = str(name or "").strip().lower()
    return any(template.name.strip().lower() == wanted for template in listTemplates(rootPath))


def prepareAdd(imagePath, mask=None, head=None, surfaces=(), name=None, space=None, contrast=None, notes=None, spaceLabel=None,
               makeDefault=False, rootPath=None, requiredSha256=None):
    """Check the files of a new template (headers, grids, surfaces, name) and return an :class:`AddPlan`.

    :raises TemplateError: with a plain message (the image is refused, a mask is on another grid, the name is taken...).
    """
    plan = AddPlan()
    if not imagePath or not os.path.isfile(imagePath):
        raise TemplateError(_("Choose the template image."))
    if not isImageFile(imagePath):
        raise TemplateError(_("Use a NIfTI (.nii, .nii.gz) or NRRD (.nrrd) file"))
    detection = identify(imagePath)
    if not detection.ok:
        raise TemplateError(detection.message or _(REFUSAL))
    plan.detection = detection
    plan.requiredSha256 = requiredSha256 or None
    plan.firstVolume = detection.is4d
    name = (name or proposedName(imagePath)).strip()
    if not name:
        raise TemplateError(_("Give the template a name."))
    if nameTaken(name, rootPath):
        raise TemplateError(_("A template named {name} exists already: choose another name.").format(name=name))
    space = space or detection.spaceGuess or OTHER_SPACE
    if space not in SPACE_IDS:
        raise TemplateError(_("Unknown space {space}").format(space=space))
    contrast = contrast or "T1w"
    if contrast not in CONTRASTS:
        raise TemplateError(_("Unknown contrast {contrast}").format(contrast=contrast))
    parent = templatesFolder(rootPath)
    plan.folder = os.path.join(parent, _uniqueFolderName(parent, name))
    imageName = os.path.basename(imagePath) if not plan.firstVolume else stemOf(imagePath) + ".nii.gz"
    plan.items.append((imagePath, imageName, "image"))
    files = {"image": imageName, "surfaces": []}
    for role, path in (("mask", mask), ("head", head)):
        if not path:
            continue
        ok, reason = checkCompanion(detection, path)
        if not ok:
            raise TemplateError(reason)
        target = os.path.basename(path)
        if target in {item[1] for item in plan.items}:
            target = f"{role}_{target}"
        plan.items.append((path, target, role))
        files[role] = target
    for path in surfaces or ():
        ok, reason = checkSurface(path)
        if not ok:
            raise TemplateError(reason)
        target = f"{SURFACES_DIR}/{os.path.basename(path)}"
        if target in {item[1] for item in plan.items}:
            raise TemplateError(_("Two surfaces are named {name}.").format(name=os.path.basename(path)))
        plan.items.append((path, target, "surface"))
        files["surfaces"].append(target)
    resolution = detection.resolution
    plan.record = {
        "schema": SCHEMA,
        "version": VERSION,
        "id": _uniqueSlug(name, rootPath),
        "name": name,
        "space": space,
        "space_label": (spaceLabel or None) if space == OTHER_SPACE else None,
        "resolution_mm": resolution,
        "shape": list(detection.shape),
        "contrast": contrast,
        "files": files,
        "sha256": {},
        "grid": {"shape": list(detection.shape), "affine": [[round(float(value), 6) for value in row] for row in detection.affine]},
        "notes": notes or "",
        "added": _isoNow(),
        "default": bool(makeDefault),
    }
    plan.makeDefault = bool(makeDefault)
    plan.totalBytes = sum(os.path.getsize(item[0]) for item in plan.items)
    return plan


def _copyHashed(source, target, plan):
    digest = hashlib.sha256()
    with open(source, "rb") as reader, open(target, "wb") as writer:
        for chunk in iter(lambda: reader.read(COPY_CHUNK), b""):
            _checkCancelled(plan)
            digest.update(chunk)
            writer.write(chunk)
            plan.progress += len(chunk)
    shutil.copystat(source, target)
    return digest.hexdigest()


def _checkCancelled(plan):
    if plan.cancelled:
        raise TemplateError(_("Cancelled"))


def _writeFirstVolume(source, target, plan):
    """The first volume of a 4D NIfTI file as a 3D NIfTI file (same header geometry and codes).

    Cancel (``plan.cancelled``) is checked while the file is read and before and after the volume is written; the progress
    moves while the file is read (most of the time) and reaches the size of the file when the volume is written.
    """
    from LesionNetworkMappingLib import nifti

    start = plan.progress
    size = os.path.getsize(source)
    readShare = 0.9 * size

    def onBlock(done, total):
        _checkCancelled(plan)
        plan.progress = start + int(readShare * done / max(1, total))

    data, affine, header = nifti.read_nifti(source, apply_scaling=False, on_block=onBlock)
    _checkCancelled(plan)
    first = data.reshape(data.shape[:3] + (-1,))[..., 0]
    nifti.write_nifti(target, first, affine, sform_code=max(1, header["sform_code"]), qform_code=max(1, header["qform_code"]),
                      dtype=first.dtype, scl_slope=header["scl_slope"] or None, scl_inter=header["scl_inter"] if header["scl_slope"] else None,
                      descrip=header.get("descrip", ""))
    _checkCancelled(plan)
    plan.progress = start + size
    return fileSha256(target)


def copyFiles(plan):
    """Copy the files of a plan into a new template folder and write its template.json; return the record. Worker-safe.

    The files are written into a hidden ".adding-..." folder that is renamed when everything is copied, so a failed or
    cancelled add leaves nothing behind. The user's files are only read.

    :raises TemplateError: "Cancelled" when ``plan.cancelled`` is set before the folder is renamed (a cancel after that
      is handled by the caller, which removes ``plan.folder``: see ``templates_ui.AddTemplateDialog``).
    """
    parent = os.path.dirname(plan.folder)
    os.makedirs(parent, exist_ok=True)
    staging = os.path.join(parent, f".adding-{plan.record['id']}-{os.getpid()}")
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    try:
        record = dict(plan.record)
        record["sha256"] = {}
        for source, name, role in plan.items:
            target = os.path.join(staging, *name.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if role == "image" and plan.firstVolume:
                digest = _writeFirstVolume(source, target, plan)
            else:
                digest = _copyHashed(source, target, plan)
            record["sha256"][name if role == "surface" else role] = digest
            if role == "image" and plan.requiredSha256 and digest != plan.requiredSha256:
                raise TemplateError(_("This is not the template of the study: its checksum differs."))
        with open(os.path.join(staging, TEMPLATE_JSON), "w", encoding="utf-8", newline="\n") as f:
            json.dump(record, f, indent=2, ensure_ascii=False)
            f.write("\n")
        # A cancel that came while the last file was written: nothing is added
        _checkCancelled(plan)
        folder = plan.folder
        if os.path.exists(folder):
            base, counter = folder, 2
            while os.path.exists(folder):
                folder = f"{base} {counter}"
                counter += 1
            plan.folder = folder
        os.rename(staging, folder)
        return record
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def finishAdd(plan, record):
    """Main thread, after ``copyFiles``: register the grid, set the default when asked, tell the listeners."""
    template = Template(record, folder=plan.folder)
    registerGrids(force=True)
    if plan.makeDefault:
        qt.QSettings().setValue(DEFAULT_KEY, template.id)
    _notify()
    return template


def addTemplate(imagePath, mask=None, head=None, surfaces=(), name=None, space=None, contrast=None, notes=None, copy=True, spaceLabel=None,
                makeDefault=False, rootPath=None, requiredSha256=None):
    """Add a template synchronously (the add dialog copies in the background writer instead); return the :class:`Template`.

    ``copy`` must be True: the files are always copied into the Slicer Studio folder (the originals are never changed, and
    a template never refers to a file outside its folder). A folder you put into Templates/ yourself with a template.json
    is listed as it is (with its own id when another template there has its id: ``userTemplates``).

    :raises TemplateError: see ``prepareAdd``.
    """
    if not copy:
        raise TemplateError(_("Templates are always copied into the Slicer Studio folder."))
    plan = prepareAdd(imagePath, mask, head, surfaces, name, space, contrast, notes, spaceLabel, makeDefault, rootPath, requiredSha256)
    record = copyFiles(plan)
    return finishAdd(plan, record)


def removeTemplate(templateId, confirmDelete=None):
    """Remove one of your templates: its folder goes to the trash (deleted after ``confirmDelete()`` where there is none).

    Return True if it is gone. Built-in templates cannot be removed (TemplateError).
    """
    from . import projects

    template = templateById(templateId)
    if template is None:
        return False
    if template.builtin:
        raise TemplateError(_("Built-in templates cannot be removed."))
    folder = template.folder
    if not projects.moveToTrash(folder):
        if confirmDelete is not None and not confirmDelete():
            return False
        shutil.rmtree(folder)
    if str(qt.QSettings().value(DEFAULT_KEY) or "") == template.id:
        qt.QSettings().remove(DEFAULT_KEY)
    registerGrids(force=True)
    forgetThumbnails(template.id)
    # Its volume and surfaces stay in the scene (lesions drawn on it keep working) but no longer carry its id: a template
    # added later under the same name (the same id) is loaded from its own image, not taken from them
    forgetTemplateNodes(template.id)
    _notify()
    return not os.path.exists(folder)


#
# Studies (study.json "template": never a path)
#


def studyRecord(template):
    """study.json ``template`` of a study drawn on ``template``.

    A built-in template keeps the existing form ({"id": space, "res", "source": "sampledata:<name>"}); yours adds its
    identity: {"id": space, "res", "source": "user:<slug>", "name", "space", "sha256"} ("id" stays the space, as every
    study reader expects).
    """
    if template is None:
        return None
    if template.builtin:
        return {"id": template.space, "res": template.resolution, "source": "sampledata:" + sampleNameOf(template)}
    return {"id": template.space, "res": template.resolution, "source": template.id, "name": template.name, "space": template.space,
            "sha256": template.imageSha256}


def templateForStudyRecord(record, rootPath=None):
    """Your template of a study.json ``template`` record ("source" "user:<slug>"): by id when its checksum matches, else
    any template with that checksum; None when it is not on this computer (or the record is not a user template).
    """
    if not isinstance(record, dict):
        return None
    source = str(record.get("source") or "")
    if not source.startswith(USER_PREFIX):
        return None
    wanted = record.get("sha256")
    template = templateById(source, rootPath)
    if template is not None and (not wanted or template.imageSha256 == wanted):
        return template
    return templateByChecksum(wanted, rootPath) if wanted else None


def isUserStudyRecord(record):
    return isinstance(record, dict) and str(record.get("source") or "").startswith(USER_PREFIX)


#
# Thumbnails (mid-axial slice)
#


def thumbnailArray(path, size=96):
    """The mid-axial slice of an image as a uint8 array (rows top = anterior, columns left = right of the patient, as
    radiological axial views), windowed at the 1st-99th percentiles. Worker-safe; None when it cannot be read.
    """
    np = _np()
    extension = _lowerExtension(path)
    if extension == ".nrrd":
        data, affine = readNrrdData(path)
    elif extension in (".nii", ".nii.gz"):
        from LesionNetworkMappingLib import nifti

        data, affine, _header = nifti.read_nifti(path, apply_scaling=True)
        data = data.reshape(data.shape[:3] + (-1,))[..., 0] if data.ndim > 3 else data
    else:
        return None
    linear = np.asarray(affine, dtype=float)[:3, :3]
    axes = np.argmax(np.abs(linear), axis=1)  # voxel axis of world x, y, z
    if len(set(axes.tolist())) != 3:
        return None
    volume = np.transpose(data, axes)  # world x, y, z order
    for worldAxis in range(3):
        if linear[worldAxis, axes[worldAxis]] < 0:
            volume = np.flip(volume, worldAxis)
    sliceXY = np.asarray(volume[:, :, volume.shape[2] // 2], dtype=np.float32)
    image = np.flipud(sliceXY.T)  # rows: anterior first; columns: x increases to the right (neurological)
    image = np.fliplr(image)  # radiological: the patient's right on the left
    finite = image[np.isfinite(image)]
    nonzero = finite[finite != 0]
    if nonzero.size == 0:
        return np.zeros((size, size), dtype=np.uint8)
    low, high = np.percentile(nonzero, (1, 99))
    scaled = np.clip((image - low) / max(high - low, 1e-6), 0, 1)
    step = max(1, int(max(scaled.shape) / size))
    return (scaled[::step, ::step] * 255).astype(np.uint8)


def forgetThumbnails(templateId):
    """Drop the in-memory thumbnails of a template at every size (the cache keys are ``(id, size)``): a template added later
    under the same name has the same id and must not show the removed one's picture.
    """
    for key in [key for key in list(_thumbnails) if key[0] == templateId]:
        _thumbnails.pop(key, None)


def rememberThumbnail(template, size, pixmap):
    """Keep a thumbnail computed elsewhere (in the background writer) for ``thumbnailPixmap``; built-in templates have no
    folder to cache it in.
    """
    if template is not None and pixmap is not None and not pixmap.isNull():
        _thumbnails[(template.id, size)] = pixmap


def thumbnailPixmap(template, size=48, compute=True):
    """A QPixmap of the template's mid-axial slice (cached in memory and in the template folder), or None."""
    if template is None:
        return None
    key = (template.id, size)
    if key in _thumbnails:
        return _thumbnails[key]
    cached = os.path.join(template.folder, THUMBNAIL_FILE) if template.folder else None
    image = None
    if cached and os.path.isfile(cached):
        image = qt.QImage(cached)
    if (image is None or image.isNull()) and compute and template.imagePath and os.path.isfile(template.imagePath):
        try:
            array = thumbnailArray(template.imagePath)
        except Exception:
            logging.debug(f"Slicer Studio: no thumbnail for {template.name}", exc_info=True)
            array = None
        if array is not None:
            image = arrayToImage(array)
            if cached:
                try:
                    image.save(cached, "PNG")
                except Exception:
                    pass
    if image is None or image.isNull():
        return None
    pixmap = qt.QPixmap.fromImage(image.scaled(size, size, qt.Qt.KeepAspectRatio, qt.Qt.SmoothTransformation))
    _thumbnails[key] = pixmap
    return pixmap


def grayPng(array):
    """PNG bytes of a 2D uint8 array (grayscale, zlib only). Worker-safe."""
    import struct
    import zlib

    np = _np()
    array = np.ascontiguousarray(array, dtype=np.uint8)
    height, width = array.shape
    raw = np.zeros((height, 1 + width), dtype=np.uint8)  # filter byte 0 (None) per row
    raw[:, 1:] = array

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw.tobytes(), 6)) + chunk(b"IEND", b"")


def arrayToImage(array):
    """A grayscale QImage of a 2D uint8 array (main thread)."""
    image = qt.QImage()
    image.loadFromData(qt.QByteArray(grayPng(array)), "PNG")
    return image


#
# Listeners
#


def addListener(callback):
    if callback not in _listeners:
        _listeners.append(callback)


def removeListener(callback):
    while callback in _listeners:
        _listeners.remove(callback)


def addRenameListener(callback):
    """``callback({old id: new id})`` after a move gave moved templates other ids (``followRenamedIds``): an id held in
    memory (a card's chosen template) follows it.
    """
    if callback not in _renameListeners:
        _renameListeners.append(callback)


def removeRenameListener(callback):
    while callback in _renameListeners:
        _renameListeners.remove(callback)


def _notify():
    for callback in list(_listeners):
        try:
            callback()
        except Exception:
            logging.error("Slicer Studio: a template listener failed", exc_info=True)


def isStudioTemplateModuleLoaded():
    return "StudioLib.templates" in sys.modules

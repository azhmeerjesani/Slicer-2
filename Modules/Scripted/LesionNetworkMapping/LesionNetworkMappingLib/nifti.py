"""Minimal NIfTI-1 reader and writer using only numpy, gzip and struct-like parsing.

Images are returned in file voxel order: ``data[i, j, k, ...]`` together with the 4x4
affine that maps 0-based ``(i, j, k)`` to RAS world coordinates (mm). No reorientation
is ever applied, so radiological (left-handed, qfac = -1) files keep their exact voxel
layout and headers survive a read/write round trip.

Slicer arrays from ``slicer.util.arrayFromVolume`` are indexed ``[k, j, i]``; transpose
them with ``array.transpose(2, 1, 0)`` and use the IJKToRAS matrix as the affine.
"""

import gzip
import os

import numpy as np

# NIfTI-1 header (348 bytes). Field names and offsets follow nifti1.h.
_HEADER_FIELDS = [
    ("sizeof_hdr", "i4"),
    ("data_type", "S10"),
    ("db_name", "S18"),
    ("extents", "i4"),
    ("session_error", "i2"),
    ("regular", "S1"),
    ("dim_info", "u1"),
    ("dim", "i2", (8,)),
    ("intent_p1", "f4"),
    ("intent_p2", "f4"),
    ("intent_p3", "f4"),
    ("intent_code", "i2"),
    ("datatype", "i2"),
    ("bitpix", "i2"),
    ("slice_start", "i2"),
    ("pixdim", "f4", (8,)),
    ("vox_offset", "f4"),
    ("scl_slope", "f4"),
    ("scl_inter", "f4"),
    ("slice_end", "i2"),
    ("slice_code", "u1"),
    ("xyzt_units", "u1"),
    ("cal_max", "f4"),
    ("cal_min", "f4"),
    ("slice_duration", "f4"),
    ("toffset", "f4"),
    ("glmax", "i4"),
    ("glmin", "i4"),
    ("descrip", "S80"),
    ("aux_file", "S24"),
    ("qform_code", "i2"),
    ("sform_code", "i2"),
    ("quatern_b", "f4"),
    ("quatern_c", "f4"),
    ("quatern_d", "f4"),
    ("qoffset_x", "f4"),
    ("qoffset_y", "f4"),
    ("qoffset_z", "f4"),
    ("srow_x", "f4", (4,)),
    ("srow_y", "f4", (4,)),
    ("srow_z", "f4", (4,)),
    ("intent_name", "S16"),
    ("magic", "S4"),
]
HEADER_SIZE = 348
_HEADER_DTYPE = np.dtype([(f[0], "<" + f[1], *f[2:]) for f in _HEADER_FIELDS])
assert _HEADER_DTYPE.itemsize == HEADER_SIZE

# Datatype codes from nifti1.h
DATATYPES = {
    2: np.dtype(np.uint8),
    4: np.dtype(np.int16),
    8: np.dtype(np.int32),
    16: np.dtype(np.float32),
    64: np.dtype(np.float64),
    256: np.dtype(np.int8),
    512: np.dtype(np.uint16),
    768: np.dtype(np.uint32),
    1024: np.dtype(np.int64),
    1280: np.dtype(np.uint64),
}
_DATATYPE_CODES = {dtype: code for code, dtype in DATATYPES.items()}

_READ_BLOCK = 1 << 24

# xform codes
XFORM_UNKNOWN = 0
XFORM_SCANNER = 1
XFORM_ALIGNED = 2
XFORM_TALAIRACH = 3
XFORM_MNI = 4


def _is_gzip(path):
    return os.fspath(path).lower().endswith(".gz")


def _check_extension(path):
    lower = os.fspath(path).lower()
    if not (lower.endswith(".nii") or lower.endswith(".nii.gz")):
        raise ValueError(f"Only single-file NIfTI-1 images (.nii or .nii.gz) are supported: {path}")


def _open(path, mode):
    return gzip.open(path, mode) if _is_gzip(path) else open(path, mode)


def _decode(value):
    return value.split(b"\0", 1)[0].decode("latin-1").strip()


def quaternion_to_affine(quatern, qoffset, pixdim, qfac):
    """Return the 4x4 qform affine (nifti_quatern_to_mat44 semantics).

    :param quatern: quaternion (b, c, d); a is implied non-negative.
    :param qoffset: translation (x, y, z).
    :param pixdim: voxel sizes (dx, dy, dz); non-positive values are treated as 1.
    :param qfac: -1 for left-handed grids (the k axis is flipped), otherwise 1.
    """
    b, c, d = (float(v) for v in quatern)
    a = 1.0 - (b * b + c * c + d * d)
    if a < 1e-7:
        # 180 degree rotation: renormalize (b, c, d)
        norm = np.sqrt(b * b + c * c + d * d)
        b, c, d = b / norm, c / norm, d / norm
        a = 0.0
    else:
        a = np.sqrt(a)
    rotation = np.array([
        [a * a + b * b - c * c - d * d, 2 * (b * c - a * d), 2 * (b * d + a * c)],
        [2 * (b * c + a * d), a * a + c * c - b * b - d * d, 2 * (c * d - a * b)],
        [2 * (b * d - a * c), 2 * (c * d + a * b), a * a + d * d - c * c - b * b],
    ])
    zooms = np.array([float(v) if v > 0 else 1.0 for v in pixdim[:3]])
    if qfac < 0:
        zooms[2] = -zooms[2]
    affine = np.eye(4)
    affine[:3, :3] = rotation * zooms[np.newaxis, :]
    affine[:3, 3] = qoffset
    return affine


def affine_to_quaternion(affine):
    """Decompose an affine into qform parameters (nifti_mat44_to_quatern semantics).

    :return: (quatern (b, c, d), qoffset (x, y, z), zooms (dx, dy, dz), qfac).
      Any shear is discarded (the closest rotation is used); the sform keeps the exact affine.
    """
    affine = np.asarray(affine, dtype=np.float64)
    matrix = affine[:3, :3]
    zooms = np.sqrt((matrix**2).sum(axis=0))
    zooms[zooms == 0] = 1.0
    rotation = matrix / zooms[np.newaxis, :]
    # Closest orthogonal matrix (polar decomposition) to absorb rounding and shear
    u, _, vt = np.linalg.svd(rotation)
    rotation = u @ vt
    qfac = 1.0
    if np.linalg.det(rotation) < 0:
        qfac = -1.0
        rotation[:, 2] = -rotation[:, 2]
    (r11, r12, r13), (r21, r22, r23), (r31, r32, r33) = rotation
    a = r11 + r22 + r33 + 1.0
    if a > 0.5:
        a = 0.5 * np.sqrt(a)
        b = 0.25 * (r32 - r23) / a
        c = 0.25 * (r13 - r31) / a
        d = 0.25 * (r21 - r12) / a
    else:
        xd = 1.0 + r11 - (r22 + r33)
        yd = 1.0 + r22 - (r11 + r33)
        zd = 1.0 + r33 - (r11 + r22)
        if xd > 1.0:
            b = 0.5 * np.sqrt(xd)
            c = 0.25 * (r12 + r21) / b
            d = 0.25 * (r13 + r31) / b
            a = 0.25 * (r32 - r23) / b
        elif yd > 1.0:
            c = 0.5 * np.sqrt(yd)
            b = 0.25 * (r12 + r21) / c
            d = 0.25 * (r23 + r32) / c
            a = 0.25 * (r13 - r31) / c
        else:
            d = 0.5 * np.sqrt(zd)
            b = 0.25 * (r13 + r31) / d
            c = 0.25 * (r23 + r32) / d
            a = 0.25 * (r21 - r12) / d
        if a < 0.0:
            b, c, d = -b, -c, -d
    return (b, c, d), tuple(affine[:3, 3]), tuple(zooms), qfac


def _parse_header(raw, path):
    if len(raw) < HEADER_SIZE:
        raise ValueError(f"File too short to be a NIfTI-1 image: {path}")
    byteorder = None
    for candidate in ("<", ">"):
        if int(np.frombuffer(raw[:4], dtype=candidate + "i4")[0]) == HEADER_SIZE:
            byteorder = candidate
            break
    if byteorder is None:
        sizes = {int(np.frombuffer(raw[:4], dtype=bo + "i4")[0]) for bo in "<>"}
        if 540 in sizes:
            raise ValueError(f"NIfTI-2 images are not supported: {path}")
        raise ValueError(f"Not a NIfTI-1 image (bad sizeof_hdr): {path}")
    fields = np.frombuffer(raw[:HEADER_SIZE], dtype=_HEADER_DTYPE.newbyteorder(byteorder))[0]
    magic = bytes(fields["magic"])
    if magic not in (b"n+1", b"n+1\0"):
        raise ValueError(f"Not a single-file NIfTI-1 image (magic {magic!r}): {path}")

    dim = [int(v) for v in fields["dim"]]
    ndim = dim[0]
    if not 1 <= ndim <= 7:
        raise ValueError(f"Invalid number of dimensions ({ndim}) in {path}")
    shape = [max(v, 1) for v in dim[1:ndim + 1]]
    while len(shape) > 3 and shape[-1] == 1:
        shape.pop()
    while len(shape) < 3:
        shape.append(1)
    pixdim = [float(v) for v in fields["pixdim"]]
    qfac = -1.0 if pixdim[0] < 0 else 1.0

    header = {
        "byteorder": byteorder,
        "dim": dim,
        "shape": tuple(shape),
        "pixdim": pixdim,
        "qfac": qfac,
        "datatype": int(fields["datatype"]),
        "bitpix": int(fields["bitpix"]),
        "vox_offset": float(fields["vox_offset"]),
        "scl_slope": float(fields["scl_slope"]),
        "scl_inter": float(fields["scl_inter"]),
        "qform_code": int(fields["qform_code"]),
        "sform_code": int(fields["sform_code"]),
        "quatern": tuple(float(fields[name]) for name in ("quatern_b", "quatern_c", "quatern_d")),
        "qoffset": tuple(float(fields[name]) for name in ("qoffset_x", "qoffset_y", "qoffset_z")),
        "srow": np.array([fields["srow_x"], fields["srow_y"], fields["srow_z"]], dtype=np.float64),
        "xyzt_units": int(fields["xyzt_units"]),
        "intent_code": int(fields["intent_code"]),
        "intent_params": tuple(float(fields[name]) for name in ("intent_p1", "intent_p2", "intent_p3")),
        "intent_name": _decode(bytes(fields["intent_name"])),
        "descrip": _decode(bytes(fields["descrip"])),
        "aux_file": _decode(bytes(fields["aux_file"])),
        "cal_min": float(fields["cal_min"]),
        "cal_max": float(fields["cal_max"]),
        "toffset": float(fields["toffset"]),
        "dim_info": int(fields["dim_info"]),
    }
    header["qform"] = quaternion_to_affine(header["quatern"], header["qoffset"], pixdim[1:4], qfac)
    header["sform"] = np.vstack([header["srow"], [0.0, 0.0, 0.0, 1.0]])
    if header["sform_code"] > 0:
        header["affine"], header["affine_source"] = header["sform"].copy(), "sform"
    elif header["qform_code"] > 0:
        header["affine"], header["affine_source"] = header["qform"].copy(), "qform"
    else:
        # "Method 1" (ANALYZE 7.5 compatibility): scaling only
        header["affine"] = np.diag([*(abs(v) if v else 1.0 for v in pixdim[1:4]), 1.0])
        header["affine_source"] = "pixdim"
    header["dtype"] = _data_dtype(header, path)
    return header


def _data_dtype(header, path):
    code, bitpix = header["datatype"], header["bitpix"]
    if code == 16 and bitpix == 16:
        # Not part of NIfTI-1: some writers store half floats as FLOAT32 with bitpix 16.
        dtype = np.dtype(np.float16)
    elif code in DATATYPES:
        dtype = DATATYPES[code]
    else:
        raise ValueError(f"Unsupported NIfTI datatype code {code} in {path}")
    return dtype.newbyteorder(header["byteorder"]) if dtype.itemsize > 1 else dtype


def read_nifti_header(path):
    """Read only the header of a .nii or .nii.gz file.

    :return: header dictionary. ``header["affine"]`` is the 4x4 voxel-to-RAS matrix (sform when
      sform_code > 0, else qform when qform_code > 0, else pixdim scaling), ``header["shape"]``
      the image shape in file order (trailing singleton dimensions beyond the third are dropped).
    """
    _check_extension(path)
    with _open(path, "rb") as f:
        raw = f.read(HEADER_SIZE)
    return _parse_header(raw, path)


def read_nifti(path, apply_scaling=True):
    """Read a NIfTI-1 image.

    :param path: .nii or .nii.gz file.
    :param apply_scaling: apply scl_slope/scl_inter (when slope is non-zero and not an identity);
      the result is then floating point.
    :return: (data, affine, header): data indexed [i, j, k, (t)] in file voxel order,
      4x4 float64 affine mapping voxel indices to RAS world mm, header dictionary.
    """
    _check_extension(path)
    with _open(path, "rb") as f:
        header = _parse_header(f.read(HEADER_SIZE), path)
        dtype = header["dtype"]
        count = int(np.prod(header["shape"], dtype=np.int64))
        offset = int(header["vox_offset"])
        if offset < HEADER_SIZE:
            offset = HEADER_SIZE + 4
        f.seek(offset)
        flat = np.empty(count, dtype=dtype)
        view = memoryview(flat.view(np.uint8))
        filled = 0
        while filled < len(view):
            # Bounded reads: GzipFile.readinto allocates a temporary bytes object of the requested size
            n = f.readinto(view[filled:filled + _READ_BLOCK])
            if not n:
                raise ValueError(f"Truncated NIfTI image data ({filled} of {len(view)} bytes): {path}")
            filled += n
    if not dtype.isnative:
        flat = flat.astype(dtype.newbyteorder("="))
    data = flat.reshape(header["shape"], order="F")
    slope, inter = header["scl_slope"], header["scl_inter"]
    if apply_scaling and np.isfinite(slope) and slope != 0 and (slope != 1 or (np.isfinite(inter) and inter != 0)):
        float_type = np.result_type(data.dtype, np.float32)
        data = data.astype(float_type) * float_type.type(slope) + float_type.type(inter if np.isfinite(inter) else 0.0)
    return data, header["affine"].copy(), header


def write_nifti(
    path,
    data,
    affine,
    sform_code=XFORM_MNI,
    qform_code=XFORM_MNI,
    dtype=None,
    scl_slope=None,
    scl_inter=None,
    descrip="",
    intent_name="",
    intent_code=0,
    intent_params=(0.0, 0.0, 0.0),
    time_step=None,
    xyzt_units=10,
    byteorder="<",
    compresslevel=1,
):
    """Write a NIfTI-1 image (gzip-compressed when the name ends with .gz).

    Both sform and qform are written and are consistent with ``affine``: the qform uses qfac = -1
    for left-handed (radiological) affines. The sform holds the exact affine (rounded to float32).

    :param data: array indexed [i, j, k, (t)]. bool is stored as uint8, float16 as float32.
    :param affine: 4x4 voxel-to-RAS matrix.
    :param dtype: optional on-disk dtype (data is cast to it).
    :param scl_slope: optional scaling slope stored in the header; data is written as given
      (readers compute ``stored * slope + inter``).
    :param intent_code: NIfTI intent (e.g. 3 for a t statistic) with ``intent_params`` (p1, p2, p3),
      e.g. the degrees of freedom of a t map.
    :param time_step: pixdim[4] for 4D images (seconds with the default units).
    :param xyzt_units: units code (default 10: mm and seconds).
    :param byteorder: "<" (default) or ">" for big-endian files.
    :param compresslevel: gzip level; outputs are written with mtime 0 so they are reproducible.
    """
    _check_extension(path)
    data = np.asarray(data)
    if dtype is not None:
        data = data.astype(dtype)
    elif data.dtype == np.bool_:
        data = data.astype(np.uint8)
    elif data.dtype == np.float16:
        data = data.astype(np.float32)
    native = data.dtype.newbyteorder("=")
    if native not in _DATATYPE_CODES:
        raise ValueError(f"Unsupported data type for NIfTI-1: {data.dtype}")
    if not 1 <= data.ndim <= 7:
        raise ValueError(f"NIfTI-1 supports 1 to 7 dimensions, got {data.ndim}")
    affine = np.asarray(affine, dtype=np.float64)
    if affine.shape != (4, 4):
        raise ValueError("affine must be a 4x4 matrix")

    quatern, qoffset, zooms, qfac = affine_to_quaternion(affine)
    header = np.zeros((), dtype=_HEADER_DTYPE.newbyteorder(byteorder))
    header["sizeof_hdr"] = HEADER_SIZE
    header["regular"] = b"r"
    dim = [1] * 8
    dim[0] = data.ndim
    dim[1:data.ndim + 1] = data.shape
    header["dim"] = dim
    pixdim = [qfac, *zooms, 1.0, 1.0, 1.0, 1.0]
    if data.ndim > 3 and time_step is not None:
        pixdim[4] = float(time_step)
    header["pixdim"] = pixdim
    header["datatype"] = _DATATYPE_CODES[native]
    header["bitpix"] = native.itemsize * 8
    header["vox_offset"] = HEADER_SIZE + 4
    header["scl_slope"] = 1.0 if scl_slope is None else scl_slope
    header["scl_inter"] = 0.0 if scl_inter is None else scl_inter
    header["xyzt_units"] = xyzt_units
    header["intent_code"] = intent_code
    header["intent_p1"], header["intent_p2"], header["intent_p3"] = intent_params
    header["intent_name"] = intent_name.encode("latin-1")[:16]
    header["descrip"] = descrip.encode("latin-1")[:80]
    header["qform_code"] = qform_code
    header["sform_code"] = sform_code
    header["quatern_b"], header["quatern_c"], header["quatern_d"] = quatern
    header["qoffset_x"], header["qoffset_y"], header["qoffset_z"] = qoffset
    header["srow_x"], header["srow_y"], header["srow_z"] = affine[0], affine[1], affine[2]
    header["magic"] = b"n+1\0"

    disk_dtype = native.newbyteorder(byteorder) if native.itemsize > 1 else native
    _write_image(path, header, data.astype(disk_dtype, copy=False).tobytes(order="F"), compresslevel)


def _write_image(path, header, payload, compresslevel):
    # Write to a temporary file then rename, so readers never see a partially written image.
    tmp_path = f"{os.fspath(path)}.{os.getpid()}.tmp"
    try:
        with open(tmp_path, "wb") as raw:
            if _is_gzip(path):
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=compresslevel, mtime=0) as f:
                    _write_payload(f, header, payload)
            else:
                _write_payload(raw, header, payload)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def _write_payload(f, header, payload):
    f.write(header.tobytes())
    f.write(b"\0\0\0\0")  # no extensions
    f.write(payload)


# Header fields that define the voxel grid; write_like copies them unchanged from the reference image.
GEOMETRY_FIELDS = (
    "pixdim",
    "xyzt_units",
    "qform_code",
    "sform_code",
    "quatern_b",
    "quatern_c",
    "quatern_d",
    "qoffset_x",
    "qoffset_y",
    "qoffset_z",
    "srow_x",
    "srow_y",
    "srow_z",
)


def read_raw_header(path):
    """Return the 348-byte header of a NIfTI-1 file as a numpy record in the file's byte order."""
    _check_extension(path)
    with _open(path, "rb") as f:
        raw = f.read(HEADER_SIZE)
    byteorder = _parse_header(raw, path)["byteorder"]
    return np.frombuffer(raw[:HEADER_SIZE], dtype=_HEADER_DTYPE.newbyteorder(byteorder))[0].copy()


def write_like(path, array_kji, ijk_to_ras, reference_path, dtype=np.uint8, descrip="", compresslevel=1):
    """Write a Slicer array on the voxel grid and with the exact geometry header of a reference image.

    This is how lesions drawn in Slicer are saved: the result has the reference's voxel order,
    sform, qform, pixdim, codes and byte order, so it overlays the reference exactly in any NIfTI
    reader (also for radiological, qfac = -1 references, which Slicer's own writer stores with a
    flipped k axis).

    :param array_kji: 3D array indexed [k, j, i] as returned by ``slicer.util.arrayFromVolume`` or
      ``arrayFromSegmentBinaryLabelmap``. It may cover only part of the reference grid (e.g. a
      segment's extent); voxels outside it are written as 0.
    :param ijk_to_ras: 4x4 IJK to RAS matrix of the array (e.g. ``volumeNode.GetIJKToRASMatrix``).
    :param reference_path: NIfTI file defining the output grid (the scan or template the lesion was drawn on).
    :param dtype: on-disk data type; values must fit in it.
    :raises ValueError: if the array grid is not the reference grid up to axis permutations, flips
      and integer voxel shifts (``inv(ref_affine) @ ijk_to_ras`` must be a signed permutation with
      integer offsets within 1e-3), or if non-zero voxels fall outside the reference grid.
    :return: ``path``.
    """
    from .geometry import signed_permutation

    _check_extension(path)
    record = read_raw_header(reference_path)
    reference = _parse_header(record.tobytes(), reference_path)
    array = np.asarray(array_kji)
    if array.ndim != 3:
        raise ValueError(f"write_like expects a 3D [k, j, i] array, got shape {array.shape}")
    mapping = signed_permutation(np.asarray(ijk_to_ras, dtype=np.float64), reference["affine"], tolerance=1e-3)
    if mapping is None:
        raise ValueError(
            f"The image grid does not match the voxel grid of {reference_path} (it must only differ by axis order, flips and whole-voxel shifts); resample it to the reference first",
        )
    dtype = np.dtype(dtype)
    if dtype.newbyteorder("=") not in _DATATYPE_CODES:
        raise ValueError(f"Unsupported data type for NIfTI-1: {dtype}")
    if array.size and np.issubdtype(dtype, np.integer) and not np.issubdtype(array.dtype, np.bool_):
        info = np.iinfo(dtype)
        if array.min() < info.min or array.max() > info.max:
            raise ValueError(f"Values {array.min()}..{array.max()} do not fit in {dtype.name}")

    # Move the source axes (i, j, k) to the reference axes, flip reversed axes, then paste the block
    block = np.transpose(array, (2, 1, 0))  # [i, j, k]
    order = np.argsort(mapping.axes)  # source axis of each reference axis
    block = np.transpose(block, order)
    out_shape = reference["shape"][:3]
    source_slices, target_slices = [], []
    for axis in range(3):
        source_axis = order[axis]
        size = block.shape[axis]
        first = mapping.offsets[axis]
        if mapping.signs[source_axis] < 0:
            block = np.flip(block, axis)
            first -= size - 1
        lo, hi = max(first, 0), min(first + size, out_shape[axis])
        source_slices.append(slice(lo - first, max(hi - first, lo - first)))
        target_slices.append(slice(lo, max(hi, lo)))
    out = np.zeros(out_shape, dtype=dtype)
    kept = block[tuple(source_slices)]
    if np.count_nonzero(kept) != np.count_nonzero(block):
        raise ValueError(f"Non-zero voxels lie outside the grid of {reference_path}")
    out[tuple(target_slices)] = kept

    header = record.copy()
    native = dtype.newbyteorder("=")
    dim = [1] * 8
    dim[0] = 3
    dim[1:4] = out_shape
    header["dim"] = dim
    header["datatype"] = _DATATYPE_CODES[native]
    header["bitpix"] = native.itemsize * 8
    header["vox_offset"] = HEADER_SIZE + 4
    header["scl_slope"] = 1.0
    header["scl_inter"] = 0.0
    header["cal_min"] = header["cal_max"] = 0.0
    header["glmin"] = header["glmax"] = 0
    header["intent_code"] = 0
    header["intent_p1"] = header["intent_p2"] = header["intent_p3"] = 0.0
    header["intent_name"] = b""
    header["descrip"] = descrip.encode("latin-1")[:80]
    header["aux_file"] = b""
    header["slice_start"] = header["slice_end"] = 0
    header["slice_code"] = 0
    header["slice_duration"] = header["toffset"] = 0.0
    header["magic"] = b"n+1\0"
    disk_dtype = native.newbyteorder(reference["byteorder"]) if native.itemsize > 1 else native
    _write_image(path, header, out.astype(disk_dtype, copy=False).tobytes(order="F"), compresslevel)
    return path

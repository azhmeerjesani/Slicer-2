"""Connectome manager jobs and helpers: conversion into a bundle, disk estimates, bundle summaries (SPEC 7.9).

Standard library, numpy and scipy only (no Qt): the Map module's connectome manager dialog
(``LesionNetworkMapping.ConnectomeManagerDialog``) runs these commands as jobs and reads these helpers.
Connectomes are never downloaded here (SPEC 7.1 rule 6): the sources are files the user already has.

- :func:`bundle_summary`: one row of the manager (name, subjects, time points, voxels, size, space, GSR,
  the provenance line, derived maps present).
- :func:`estimate_conversion`: the size of the bundle a conversion writes (float16 time series plus the
  run norms) and the free space of the destination (``shutil.disk_usage``), before anything is written.
- ``convert``: ``connectome.from_nifti_timeseries`` (a folder or glob of preprocessed 4D NIfTI runs on
  one mask) or ``connectome.from_leaddbs`` (a Lead-DBS fMRI connectome folder), as a resumable job: a
  stopped conversion keeps the subjects already written and ``--resume`` continues. MATLAB v7.3 (HDF5)
  run files need h5py: the job reads them with h5py when it can be imported (``--python-packages DIR``
  adds the folder where the application installed it after the user agreed); without it the job fails
  with an explanation (the dialog then offers the installation, never silently).

Command line (JSON events, see ``jobs``; exit codes 0 done, 1 error, 2 usage, 3 cancelled, 75 locked)::

    python -m LesionNetworkMappingLib.manager convert --source nifti --input DIR|GLOB --mask MASK --out B.lnmc
        [--runs-grouping REGEX] [--participants participants.tsv] [--name NAME] [--description TEXT]
        [--citation TEXT] [--terms-file FILE] [--gsr true|false|unknown] [--space SPACE] [--dtype float16|float32]
        [--resume] [--overwrite] [--cancel-file F] [--threads K]
    python -m LesionNetworkMappingLib.manager convert --source leaddbs --input DATASET_DIR --out B.lnmc
        [--max-subjects N] [--python-packages DIR] [same metadata options]
    python -m LesionNetworkMappingLib.manager estimate --source nifti|leaddbs --input ... [--mask MASK] [--out B.lnmc]
"""

import os
import sys

if __name__ == "__main__":
    from LesionNetworkMappingLib.jobs import set_blas_threads

    set_blas_threads(sys.argv[1:])  # before numpy is imported: BLAS reads these variables once

import glob
import shutil
import time

from . import jobs

SOURCES = ("nifti", "leaddbs")
SOURCE_LABELS = {"nifti": "NIfTI time series folder", "leaddbs": "Lead-DBS connectome folder"}
#: Bytes per voxel and time point of a converted bundle (float16), and per voxel and run of the norms (float32)
BYTES_PER_SAMPLE = {"float16": 2, "float32": 4}
BYTES_PER_NORM = 4
#: Free space kept beyond the estimate before a conversion is allowed to start
DISK_MARGIN = 1.05
NIFTI_PATTERNS = ("*.nii.gz", "*.nii")
LOCK_NAME = ".manager-convert.lock"


#
# Bundle summaries
#


def _format_bytes(count):
    for unit, size in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if count >= size:
            return f"{count / size:.3g} {unit}"
    return f"{int(count)} B"


def bundle_summary(path):
    """One row of the connectome manager for the bundle at ``path`` (a dict; ``error`` is set when it cannot be opened).

    Keys: path, name, n_subjects, time_points (total over subjects), runs, n_voxels, size_bytes, size,
    voxel_mm, space, gsr (True/False/None), kind, licence, doi, version, has_degree, has_reference,
    synthetic, error.
    """
    from .connectome import ConnectomeBundle

    row = {"path": os.path.abspath(os.fspath(path)), "name": os.path.basename(os.fspath(path).rstrip("/\\")), "error": None}
    try:
        bundle = ConnectomeBundle.open(path)
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        return row
    metadata = getattr(bundle, "metadata", {}) or {}
    try:
        provenance = bundle.provenance or {}
    except ValueError:
        provenance = {}
    source = provenance.get("source") or {}
    gsr = (provenance.get("preprocessing") or {}).get("gsr")
    subjects = metadata.get("subjects") or []
    spacing = [abs(float(value)) for value in bundle.grid.spacing]
    reference_dir = os.path.join(bundle.path, "derived", "reference")
    row.update({
        "name": bundle.name,
        "n_subjects": bundle.n_subjects,
        "time_points": int(sum(sum(entry.get("runs") or []) for entry in subjects)),
        "runs": int(sum(len(entry.get("runs") or []) for entry in subjects)),
        "n_voxels": bundle.n_voxels,
        "size_bytes": int(bundle.data_size_bytes),
        "size": _format_bytes(bundle.data_size_bytes),
        "voxel_mm": spacing[0] if max(spacing) - min(spacing) < 1e-3 else spacing,
        "space": metadata.get("space"),
        "gsr": gsr if gsr in (True, False) else None,
        "kind": provenance.get("kind") or "timeseries",
        "licence": provenance.get("licence"),
        "doi": source.get("doi"),
        "version": source.get("version"),
        "has_degree": os.path.isfile(os.path.join(bundle.path, "derived", "degree.npy")),
        "has_reference": os.path.isfile(os.path.join(reference_dir, "reference.json")),
        "synthetic": "synthetic" in f"{bundle.name} {bundle.description}".lower(),
    })
    return row


def provenance_line(row):
    """The provenance line of a :func:`bundle_summary` row: "GSP1000 v3.0 (doi:…) · N=1000 · GSR · 292,019 vox · 2 mm · 126 GB"."""
    if row.get("error"):
        return f"{row['name']} · cannot be opened: {row['error']}"
    name = row["name"] + (f" v{row['version']}" if row.get("version") else "") + (f" (doi:{row['doi']})" if row.get("doi") else "")
    parts = [name, f"N={row['n_subjects']:,}"]
    if row.get("gsr") is True:
        parts.append("GSR")
    elif row.get("gsr") is False:
        parts.append("no GSR")
    voxel = row.get("voxel_mm")
    parts += [f"{row['n_voxels']:,} vox", f"{voxel:g} mm" if isinstance(voxel, float) else " × ".join(f"{value:g}" for value in voxel) + " mm", row["size"]]  # noqa: RUF001
    return " · ".join(parts)


#
# Estimates
#


def nifti_inputs(input_path):
    """Sorted 4D NIfTI run files of a folder (``*.nii.gz``, ``*.nii``) or of a glob pattern."""
    input_path = os.fspath(input_path)
    if os.path.isdir(input_path):
        files = []
        for pattern in NIFTI_PATTERNS:
            files += glob.glob(os.path.join(input_path, pattern))
        return sorted(set(files))
    return sorted(glob.glob(input_path))


def _leaddbs_layout(dataset_dir):
    """(n_voxels, [run file paths], n_subjects) of a Lead-DBS connectome folder, without reading the time series."""
    import numpy as np

    from . import connectome

    vol = connectome._loadmat(os.path.join(dataset_dir, "dataset_volsurf.mat"))["vol"]
    n_voxels = int(np.asarray(vol["outidx"]).size)
    entries = connectome._leaddbs_subject_entries(vol["subIDs"])
    files = []
    for entry in entries:
        for run_file in entry[1:]:
            path = os.path.join(dataset_dir, "vol", run_file)
            if not os.path.exists(path) and os.path.exists(path + ".mat"):
                path += ".mat"
            files.append(path)
    return n_voxels, files, len(entries)


def _is_v73(path):
    with open(path, "rb") as f:
        head = f.read(520)
    return b"MATLAB 7.3" in head[:116] or head[512:520] == b"\x89HDF\r\n\x1a\n"


def _mat_time_points(path, n_voxels):
    """Time points of one Lead-DBS run file (the other dimension of its gmtc), read from its header only.

    MATLAB v5/v7 files are read with scipy's ``whosmat``, v7.3 (HDF5) files with h5py when it can be
    imported; a v7.3 file without h5py raises ``connectome.UnsupportedFormatError``.
    """
    from scipy import io as sio

    from .connectome import UnsupportedFormatError

    if _is_v73(path):
        try:
            import h5py
        except ImportError:
            raise UnsupportedFormatError(f"{path} is a MATLAB v7.3 (HDF5) file, which scipy cannot read. Install h5py, or re-save it in MATLAB with save(filename, '-struct', 's', '-v7').") from None
        with h5py.File(path, "r") as f:
            shape = tuple(reversed(f["gmtc"].shape)) if "gmtc" in f else ()
    else:
        try:
            shape = next((shape for name, shape, _kind in sio.whosmat(path) if name == "gmtc"), ())
        except Exception:
            shape = ()
    if len(shape) == 2:
        return shape[1] if shape[0] == n_voxels else shape[0]
    return None


def estimate_conversion(source, input_path, mask=None, out=None, dtype="float16", sample_runs=8):
    """Size of the bundle that a conversion would write, and the free space where it goes.

    :return: dict with source, subjects (or None), runs, time_points (exact or extrapolated from
      ``sample_runs`` run files), n_voxels, bytes, free_bytes (None when ``out`` is not given),
      enough (bool or None) and exact (whether every run was measured).
    """
    import numpy as np

    from .nifti import read_nifti, read_nifti_header

    if source not in SOURCES:
        raise ValueError(f"Unknown source {source!r} (use {', '.join(SOURCES)})")
    if source == "nifti":
        files = nifti_inputs(input_path)
        if not files:
            raise FileNotFoundError(f"No NIfTI time series in {input_path}")
        if mask is None:
            raise ValueError("A NIfTI conversion needs the mask that defines the connectome grid")
        n_voxels = int(np.count_nonzero(read_nifti(mask)[0]))
        time_points = 0
        for path in files:
            shape = read_nifti_header(path)["shape"]
            time_points += int(shape[3]) if len(shape) > 3 else 1
        runs, subjects, exact = len(files), None, True
    else:
        n_voxels, files, subjects = _leaddbs_layout(input_path)
        runs = len(files)
        measured = [count for count in (_mat_time_points(path, n_voxels) for path in files[:sample_runs]) if count]
        if not measured:
            raise ValueError(f"The run files of {input_path} cannot be read (no gmtc variable)")
        exact = len(measured) == runs
        time_points = int(sum(measured) if exact else round(np.mean(measured) * runs))
    total = n_voxels * time_points * BYTES_PER_SAMPLE[dtype] + n_voxels * runs * BYTES_PER_NORM
    free = None
    if out:
        folder = os.path.abspath(os.fspath(out))
        while folder and not os.path.isdir(folder):
            parent = os.path.dirname(folder)
            if parent == folder:
                break
            folder = parent
        try:
            free = shutil.disk_usage(folder).free
        except OSError:
            free = None
    return {
        "source": source,
        "subjects": subjects,
        "runs": runs,
        "time_points": time_points,
        "n_voxels": n_voxels,
        "bytes": int(total),
        "size": _format_bytes(total),
        "free_bytes": free,
        "free": None if free is None else _format_bytes(free),
        "enough": None if free is None else bool(free >= total * DISK_MARGIN),
        "exact": exact,
    }


#
# MATLAB v7.3 run files (HDF5) through h5py
#


def h5py_available(packages=None):
    """Whether h5py can be imported (after adding ``packages``, a folder of user-installed packages, to sys.path)."""
    if packages and os.path.isdir(packages) and packages not in sys.path:
        sys.path.append(packages)  # last: the application's own numpy always wins over a copy installed with h5py
    try:
        import h5py  # noqa: F401
    except ImportError:
        return False
    return True


def _h5_value(item, h5py):
    """A MATLAB value of a v7.3 file as scipy's ``loadmat(simplify_cells=True)`` gives it (numbers, text, structs, cells)."""
    import numpy as np

    if isinstance(item, h5py.Group):
        return {key: _h5_value(item[key], h5py) for key in item if not key.startswith("#")}
    matlab_class = item.attrs.get("MATLAB_class", b"")
    matlab_class = matlab_class.decode() if isinstance(matlab_class, bytes) else str(matlab_class)
    data = item[()]
    if matlab_class == "char":
        return "".join(chr(code) for code in np.asarray(data).ravel(order="F"))
    if matlab_class == "cell":
        cells = np.empty(data.shape[::-1], dtype=object)
        for index, reference in np.ndenumerate(np.asarray(data).T):
            cells[index] = _h5_value(item.file[reference], h5py)
        return cells.squeeze() if cells.size != 1 else cells.reshape(1)
    array = np.asarray(data)
    return array.T if array.ndim > 1 else array  # MATLAB stores column-major: HDF5 sees the transpose


def h5_loadmat(path, variable_names=None):
    """``scipy.io.loadmat(path, simplify_cells=True)`` for MATLAB v7.3 (HDF5) files, through h5py."""
    import h5py

    with h5py.File(path, "r") as f:
        names = [name for name in f if not name.startswith("#") and (variable_names is None or name in variable_names)]
        return {name: _h5_value(f[name], h5py) for name in names}


def _install_h5_fallback():
    """Make ``connectome._loadmat`` read v7.3 files with h5py when scipy cannot (this process only)."""
    from . import connectome

    original = connectome._loadmat
    if getattr(original, "_h5_fallback", False):
        return

    def loadmat(path, **kwargs):
        try:
            return original(path, **kwargs)
        except connectome.UnsupportedFormatError:
            return h5_loadmat(path, variable_names=kwargs.get("variable_names"))

    loadmat._h5_fallback = True
    connectome._loadmat = loadmat


#
# Command line
#


def _add_source_arguments(parser):
    parser.add_argument("--source", choices=SOURCES, required=True, help="nifti: a folder or glob of 4D NIfTI runs on one mask; leaddbs: a Lead-DBS connectome folder")
    parser.add_argument("--input", required=True, help="the NIfTI folder or glob, or the Lead-DBS dataset folder (dataset_volsurf.mat, vol/)")
    parser.add_argument("--mask", default=None, help="mask NIfTI that defines the connectome grid (nifti)")
    parser.add_argument("--dtype", choices=tuple(BYTES_PER_SAMPLE), default="float16", help="sample type of the bundle (default float16)")
    parser.add_argument("--python-packages", default=None, help="folder of user-installed packages (h5py for MATLAB v7.3 files)")


def _gsr(text):
    value = (text or "unknown").strip().lower()
    if value in ("true", "1", "yes"):
        return True
    if value in ("false", "0", "no"):
        return False
    return "unknown"


def _convert_main(argv):
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.manager convert", description="Convert connectome data into a connectome bundle (.lnmc), resumably.")
    jobs.add_common_arguments(parser)
    _add_source_arguments(parser)
    parser.add_argument("--out", required=True, help="the bundle folder (.lnmc) to write")
    parser.add_argument("--runs-grouping", default=None, help="nifti: regular expression whose group 'subject' groups run files of one subject")
    parser.add_argument("--participants", default=None, help="nifti: participants.tsv describing the subjects (copied into the bundle)")
    parser.add_argument("--max-subjects", type=jobs.positive_int, default=None, help="leaddbs: convert at most N subjects")
    parser.add_argument("--name", default=None, help="bundle name (default: the folder name)")
    parser.add_argument("--description", default="", help="description")
    parser.add_argument("--citation", default="", help="citation text")
    parser.add_argument("--terms-file", default=None, help="text file with the data use terms (shown before Compute)")
    parser.add_argument("--gsr", default=None, help="global signal regression in the preprocessing: true, false or unknown (default: left as recorded)")
    parser.add_argument("--space", default=None, help="template space of the grid (for example MNI152NLin6Asym)")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing bundle at --out")
    args = parser.parse_args(argv)
    if args.source == "nifti" and not args.mask:
        parser.error("--source nifti needs --mask")
    cancel = jobs.CancelToken(args.cancel_file)
    out = os.path.abspath(args.out)
    if os.path.isfile(os.path.join(out, "connectome.json")) and not (args.resume or args.overwrite):
        raise jobs.JobError(f"A connectome bundle already exists at {out} (use --overwrite or --resume)", code="exists")
    terms = ""
    if args.terms_file:
        with open(args.terms_file, encoding="utf-8") as f:
            terms = f.read().strip()
    if h5py_available(args.python_packages):
        _install_h5_fallback()
    jobs.emit("start", job="convert", argv=list(argv), threads=args.threads, source=args.source)
    from .connectome import UnsupportedFormatError

    try:
        estimate = estimate_conversion(args.source, args.input, mask=args.mask, out=out, dtype=args.dtype)
    except UnsupportedFormatError as exc:
        raise jobs.JobError(str(exc), code="unsupported_format", needs="h5py" if "h5py" in str(exc) else None) from None
    except Exception as exc:
        raise jobs.JobError(f"{type(exc).__name__}: {exc}", code="input") from None
    jobs.emit("result", kind="estimate", **estimate)
    if estimate["enough"] is False and not args.resume:
        raise jobs.JobError(f"Not enough free space: the bundle needs about {estimate['size']}, {estimate['free']} are free", code="disk_space")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    lock = jobs.OutputLock(out.rstrip("/\\") + LOCK_NAME)
    lock.acquire()
    try:
        jobs.emit("stage", name="subjects", index=1, total=1)
        started = time.monotonic()

        def progress(done, total, _elapsed):
            jobs.progress(done, total, started)

        common = {"name": args.name, "description": args.description, "citation": args.citation, "data_use_terms": terms, "dtype": args.dtype,
                  "resume": args.resume, "overwrite": args.overwrite and not args.resume, "progress": progress, "cancel": cancel}
        from . import connectome

        try:
            if args.source == "nifti":
                files = nifti_inputs(args.input)
                path = connectome.from_nifti_timeseries(files, args.mask, out, runs_grouping=args.runs_grouping, participants=args.participants, **common)
            else:
                path = connectome.from_leaddbs(args.input, out, max_subjects=args.max_subjects, **common)
        except connectome.UnsupportedFormatError as exc:
            raise jobs.JobError(str(exc), code="unsupported_format", needs="h5py" if "h5py" in str(exc) else None) from None
        except (OSError, ValueError, KeyError) as exc:
            raise jobs.JobError(f"{type(exc).__name__}: {exc}", code="input") from None
        if path is None:
            raise jobs.Cancelled(reason="cancel file")
        keys = {}
        if args.gsr is not None:
            keys["preprocessing"] = {**(connectome.ConnectomeBundle.open(path).provenance.get("preprocessing") or {}), "gsr": _gsr(args.gsr)}
        if args.space:
            keys["space"] = args.space
        if keys:
            connectome.set_metadata(path, **keys)
    finally:
        lock.release()
    jobs.emit("result", kind="connectome", path=path)
    jobs.emit("done", outputs=[path], elapsed=round(jobs.elapsed(), 3), run_id=None)
    return jobs.EXIT_OK


def _estimate_main(argv):
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.manager estimate", description="Estimate the size of a connectome conversion and the free space.")
    jobs.add_common_arguments(parser)
    _add_source_arguments(parser)
    parser.add_argument("--out", default=None, help="the bundle folder that would be written (for the free space)")
    args = parser.parse_args(argv)
    if h5py_available(args.python_packages):
        _install_h5_fallback()
    jobs.emit("start", job="convert-estimate", argv=list(argv), threads=args.threads)
    try:
        estimate = estimate_conversion(args.source, args.input, mask=args.mask, out=args.out, dtype=args.dtype)
    except Exception as exc:
        raise jobs.JobError(f"{type(exc).__name__}: {exc}", code="input") from None
    jobs.emit("result", kind="estimate", **estimate)
    jobs.emit("done", outputs=[], elapsed=round(jobs.elapsed(), 3), run_id=None, bytes=estimate["bytes"])
    return jobs.EXIT_OK


_COMMANDS = {"convert": _convert_main, "estimate": _estimate_main}


def main(argv=None):
    """Command-line entry point (see the module docstring); returns the exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in _COMMANDS:
        parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.manager", description="Connectome manager jobs.")
        parser.add_argument("command", choices=list(_COMMANDS))
        parser.parse_args(argv[:1])
        return jobs.EXIT_USAGE
    return _COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    jobs.run_job(main, job="manager")

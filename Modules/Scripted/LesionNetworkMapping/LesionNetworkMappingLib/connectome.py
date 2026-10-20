"""Normative functional connectome bundles (".lnmc" directories), converters and synthetic data.

Bundle layout::

    connectome.json          metadata (see below)
    mask.nii.gz              brain mask on the connectome grid
    subjects/<id>.f16.npy    (V, T_total) float16, voxel-major. Each run segment of each voxel is
                             demeaned and scaled to unit L2 norm; zero-variance voxels are all zeros.
    subjects/<id>.norms.npy  (nRuns, V) float32 L2 norms of the demeaned run segments
    derived/degree.npy       optional (V,) float32 connectome degree map (see compute_degree)
    derived/degree_fz.npy    optional (V,) float32 signed mean Fz degree map (compute_degree measure "fz")

connectome.json::

    {"format": "lnmc", "version": 1, "name", "description", "citation", "dataUseTerms",
     "shape": [ni, nj, nk], "affine": 4x4 voxel-to-RAS, "nVoxels": V,
     "subjects": [{"id": ..., "runs": [nTR, ...]}, ...], "dtype": "float16"}

Optional provenance keys (camelCase; all may be missing, and readers that do not know them ignore
them, so the format stays "lnmc" version 1; see :data:`PROVENANCE_KEYS` and :func:`set_metadata`)::

    "source": {"doi", "version", "url", "name", "tag", "citation",
               "files": [{"name", "md5" | "sha1" | "sha256", "size"}, ...]}
    "kind": "timeseries" (default) | "group_pca" | "tractogram"
    "mask": {"source": text, "sha256": mask_content_sha256(mask)}   (the voxels, not a file's bytes)
    "subjectsSha256": sha256 of the subject ids, each followed by a newline, in bundle order
    "licence": "GSP-DUT-2014" | an SPDX licence id | "unknown"
    "termsSha256": sha256 of the data use terms text that was accepted
    "acknowledgement": wording the data use terms require in publications
    "preprocessing": {"gsr": true | false | "unknown", "nuisance", "bandpass", "smoothingMm", "pipelineUrl"}
    "population": {"n", "ageRange", "sexRatio", "scanners"}
    "trS": repetition time in seconds
    "subsets": {name: [subject ids]}   named subject subsets (for example the Lead-DBS
               "First 100 Subjects"); subsets defined by the user are named "user:<name>"
    "participants": relative path of a participants.tsv describing the subjects

``ConnectomeWriter`` fills in "subjectsSha256" and "mask.sha256" when it finalizes a bundle.

The V voxels are the non-zero voxels of the mask in C order of the [i, j, k] array
(``geometry.mask_indices``). With this layout the Pearson correlation of every voxel with a unit-norm
seed is a single matrix product (see engine.py). A "float32" dtype (``<id>.f32.npy``) is also accepted.
"""

import copy
import glob
import hashlib
import json
import math
import os
import queue
import re
import shutil
import threading
import time
from typing import NamedTuple

import numpy as np
from numpy.lib import format as npy_format

from .geometry import Grid, affine_from_spm_mat, mask_indices
from .nifti import read_nifti, read_nifti_header, write_nifti

FORMAT_NAME = "lnmc"
FORMAT_VERSION = 1
MANIFEST_NAME = "connectome.json"
PARTIAL_MANIFEST_NAME = "connectome.partial.json"
# connectome.partial.json only: the writer's provenance, kept so that a resumed conversion still writes it
_PENDING_PROVENANCE_KEY = "pendingProvenance"
SUBJECT_DTYPES = {"float16": ("f16", np.dtype(np.float16)), "float32": ("f32", np.dtype(np.float32))}
_SUBJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# Keys that define the data of a bundle: set_metadata() never changes them
REQUIRED_KEYS = ("format", "version", "name", "shape", "affine", "nVoxels", "subjects", "dtype")
# Optional provenance keys (format "lnmc" version 1.1); older readers ignore them
PROVENANCE_KEYS = ("source", "kind", "mask", "subjectsSha256", "licence", "termsSha256", "acknowledgement", "preprocessing", "population", "trS", "subsets", "participants")
# Other optional keys that set_metadata() may change
DESCRIPTIVE_KEYS = ("description", "citation", "dataUseTerms", "space")
KINDS = ("timeseries", "group_pca", "tractogram")
GSR_VALUES = (True, False, "unknown")
USER_SUBSET_PREFIX = "user:"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_DOI_PATTERN = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.IGNORECASE)


class UnsupportedFormatError(ValueError):
    """Raised for input data that cannot be converted (e.g. MATLAB v7.3 files without h5py)."""


class SubjectData(NamedTuple):
    """One subject loaded from a bundle."""

    id: str
    data: np.ndarray  # (V, T_total), per-run demeaned unit-norm rows
    norms: np.ndarray  # (nRuns, V) float32
    runs: tuple  # number of time points of each run


def sanitize_subject_id(text):
    """Turn an arbitrary string into a valid subject id (letters, digits, '.', '_', '-')."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(text)).lstrip("._-")
    return cleaned or "sub"


def _is_cancelled(cancel):
    if cancel is None:
        return False
    if hasattr(cancel, "is_set"):
        return cancel.is_set()
    return bool(cancel())


def _write_json_atomic(path, obj):
    tmp_path = f"{path}.{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp_path, path)


def subjects_sha256(subject_ids):
    """Hex sha256 of a subject list: the UTF-8 ids, each followed by a newline, in the given order."""
    return hashlib.sha256("".join(f"{subject_id}\n" for subject_id in subject_ids).encode("utf-8")).hexdigest()


def mask_content_sha256(mask):
    """Hex sha256 of a 3D mask's voxels: its shape and its non-zero voxels as C-order packed bits.

    The hash depends only on which voxels are in the mask, not on how mask.nii.gz is encoded or compressed.
    """
    mask = np.asarray(mask)
    mask = mask.reshape(mask.shape[:3]) != 0
    digest = hashlib.sha256("lnmc-mask {} {} {}\n".format(*mask.shape).encode("ascii"))
    digest.update(np.packbits(mask.reshape(-1)).tobytes())
    return digest.hexdigest()


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _check_type(key, value, types, what):
    if not isinstance(value, types):
        raise ValueError(f"connectome.json {key!r} must be {what}, got {value!r}")


def _validate_subsets(subsets, subject_ids):
    """Check a "subsets" value ({name: [ids]}) against the bundle's subject ids; return it as a new dict."""
    _check_type("subsets", subsets, dict, "an object {name: [subject ids]}")
    known = set(subject_ids)
    out = {}
    for name, ids in subsets.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"connectome.json subset names must be non-empty text, got {name!r}")
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
            raise ValueError(f"Subset {name!r} must be a non-empty list of subject ids")
        if len(set(ids)) != len(ids):
            raise ValueError(f"Subset {name!r} lists a subject more than once")
        unknown = [i for i in ids if i not in known]
        if unknown:
            raise ValueError(f"Subset {name!r} has {len(unknown)} subject id(s) that are not in the bundle: {', '.join(unknown[:10])}{' ...' if len(unknown) > 10 else ''}")
        out[name] = list(ids)
    return out


def _validate_provenance_value(key, value, subject_ids):
    """Check one optional key of connectome.json (see the module docstring); return a copy of the value."""
    if key in ("description", "citation", "dataUseTerms", "acknowledgement", "licence", "space"):
        _check_type(key, value, str, "text")
    elif key == "kind":
        if value not in KINDS:
            raise ValueError(f"connectome.json 'kind' must be one of {', '.join(KINDS)}, got {value!r}")
    elif key in ("termsSha256", "subjectsSha256"):
        if not isinstance(value, str) or not _SHA256_PATTERN.match(value):
            raise ValueError(f"connectome.json {key!r} must be a lowercase hex sha256, got {value!r}")
    elif key == "trS":
        if not _is_number(value) or value <= 0:
            raise ValueError(f"connectome.json 'trS' must be a positive number of seconds, got {value!r}")
    elif key == "participants":
        _check_type(key, value, str, "a relative path")
        parts = value.replace("\\", "/").split("/")
        if not value or os.path.isabs(value) or value.startswith(("/", "\\")) or ".." in parts:
            raise ValueError(f"connectome.json 'participants' must be a path inside the bundle, got {value!r}")
    elif key == "subsets":
        return _validate_subsets(value, subject_ids)
    elif key == "source":
        _check_type(key, value, dict, "an object")
        files = value.get("files", [])
        if not isinstance(files, list) or not all(isinstance(f, dict) and isinstance(f.get("name"), str) for f in files):
            raise ValueError("connectome.json 'source.files' must be a list of objects with a 'name'")
    elif key == "mask":
        _check_type(key, value, dict, "an object")
        if value.get("sha256") is not None and not _SHA256_PATTERN.match(str(value["sha256"])):
            raise ValueError(f"connectome.json 'mask.sha256' must be a lowercase hex sha256, got {value['sha256']!r}")
    elif key == "preprocessing":
        _check_type(key, value, dict, "an object")
        if "gsr" in value and value["gsr"] not in GSR_VALUES:
            raise ValueError(f"connectome.json 'preprocessing.gsr' must be true, false or \"unknown\", got {value['gsr']!r}")
        if value.get("smoothingMm") is not None and (not _is_number(value["smoothingMm"]) or value["smoothingMm"] < 0):
            raise ValueError(f"connectome.json 'preprocessing.smoothingMm' must be a number of mm, got {value['smoothingMm']!r}")
    elif key == "population":
        _check_type(key, value, dict, "an object")
        if value.get("n") is not None and (not isinstance(value["n"], int) or isinstance(value["n"], bool) or value["n"] < 0):
            raise ValueError(f"connectome.json 'population.n' must be a number of subjects, got {value['n']!r}")
    else:
        raise ValueError(f"Unknown connectome.json key {key!r}")
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"connectome.json {key!r} is not plain JSON: {exc}") from None
    return copy.deepcopy(value)


def _apply_metadata(metadata, keys):
    """Return a copy of ``metadata`` with optional ``keys`` set (None removes a key), all validated.

    Required keys (:data:`REQUIRED_KEYS`) and unknown keys are refused. "subjectsSha256" and
    "mask.sha256", which describe the data, may only be set to their actual values.
    """
    refused = [key for key in keys if key in REQUIRED_KEYS]
    if refused:
        raise ValueError(f"Required connectome.json keys cannot be changed: {', '.join(refused)}")
    unknown = [key for key in keys if key not in PROVENANCE_KEYS and key not in DESCRIPTIVE_KEYS]
    if unknown:
        raise ValueError(f"Unknown connectome.json keys: {', '.join(unknown)} (optional keys: {', '.join(PROVENANCE_KEYS + DESCRIPTIVE_KEYS)})")
    subject_ids = [entry["id"] for entry in metadata["subjects"]]
    out = copy.deepcopy(metadata)
    for key, value in keys.items():
        if value is None:
            out.pop(key, None)
            continue
        value = _validate_provenance_value(key, value, subject_ids)
        if key == "subjectsSha256" and value != subjects_sha256(subject_ids):
            raise ValueError("connectome.json 'subjectsSha256' does not match the bundle's subject list")
        out[key] = value
    return out


def _save_npy_atomic(path, array):
    tmp_path = f"{path}.{os.getpid()}.tmp"
    with open(tmp_path, "wb") as f:
        np.save(f, array)
    os.replace(tmp_path, path)


def _read_npy(path, shape, dtype, out=None):
    """Read a C-order .npy file of known shape and dtype, optionally into a preallocated array."""
    with open(path, "rb") as f:
        version = npy_format.read_magic(f)
        if version == (1, 0):
            file_shape, fortran_order, file_dtype = npy_format.read_array_header_1_0(f)
        elif version == (2, 0):
            file_shape, fortran_order, file_dtype = npy_format.read_array_header_2_0(f)
        else:
            raise ValueError(f"Unsupported .npy version {version}: {path}")
        if tuple(file_shape) != tuple(shape) or fortran_order or file_dtype != dtype:
            raise ValueError(f"{path}: expected C-order {np.dtype(dtype).name} array of shape {tuple(shape)}, found {file_dtype} {tuple(file_shape)}")
        if out is None:
            out = np.empty(shape, dtype=dtype)
        view = memoryview(out.reshape(-1).view(np.uint8))
        filled = 0
        while filled < len(view):
            n = f.readinto(view[filled:])
            if not n:
                raise ValueError(f"Truncated file: {path}")
            filled += n
    return out


class ConnectomeBundle:
    """Read access to a connectome bundle directory."""

    def __init__(self, path):
        self.path = os.path.abspath(os.fspath(path))
        manifest = os.path.join(self.path, MANIFEST_NAME)
        if not os.path.isfile(manifest):
            raise FileNotFoundError(f"Not a connectome bundle (missing {MANIFEST_NAME}): {self.path}")
        with open(manifest, encoding="utf-8") as f:
            self.metadata = json.load(f)
        self._validate_metadata()
        self._subjects = {entry["id"]: entry for entry in self.metadata["subjects"]}
        self._mask = None
        self._mask_indices = None
        self._mask_sha256 = None
        self._prefetchers = set()
        self._lock = threading.Lock()

    @classmethod
    def open(cls, path):
        """Open and validate a bundle."""
        return cls(path)

    def _validate_metadata(self):
        # Optional provenance keys are checked where they are used (provenance, subsets, mask_sha256),
        # so that an odd optional key never prevents opening a bundle.
        meta = self.metadata
        if meta.get("format") != FORMAT_NAME:
            raise ValueError(f"Unknown connectome format {meta.get('format')!r} in {self.path}")
        if int(meta.get("version", 0)) > FORMAT_VERSION:
            raise ValueError(f"Connectome bundle version {meta.get('version')} is newer than supported ({FORMAT_VERSION})")
        if meta.get("dtype") not in SUBJECT_DTYPES:
            raise ValueError(f"Unsupported connectome dtype {meta.get('dtype')!r}")
        if len(meta.get("shape", [])) != 3 or np.asarray(meta.get("affine")).shape != (4, 4):
            raise ValueError("connectome.json must define a 3D 'shape' and a 4x4 'affine'")
        ids = [entry.get("id") for entry in meta.get("subjects", [])]
        if len(set(ids)) != len(ids) or not all(isinstance(i, str) and _SUBJECT_ID_PATTERN.match(i) for i in ids):
            raise ValueError("connectome.json has invalid or duplicate subject ids")
        for entry in meta["subjects"]:
            if not entry.get("runs") or min(entry["runs"]) < 1:
                raise ValueError(f"Subject {entry['id']} has no runs")

    # Properties

    @property
    def name(self):
        return self.metadata.get("name") or os.path.splitext(os.path.basename(self.path))[0]

    @property
    def description(self):
        return self.metadata.get("description", "")

    @property
    def citation(self):
        return self.metadata.get("citation", "")

    @property
    def data_use_terms(self):
        return self.metadata.get("dataUseTerms", "")

    @property
    def shape(self):
        return tuple(int(n) for n in self.metadata["shape"])

    @property
    def affine(self):
        return np.array(self.metadata["affine"], dtype=np.float64)

    @property
    def grid(self):
        return Grid(self.shape, self.affine)

    @property
    def n_voxels(self):
        return int(self.metadata["nVoxels"])

    @property
    def dtype(self):
        return SUBJECT_DTYPES[self.metadata["dtype"]][1]

    @property
    def subject_ids(self):
        return [entry["id"] for entry in self.metadata["subjects"]]

    @property
    def n_subjects(self):
        return len(self.metadata["subjects"])

    @property
    def data_size_bytes(self):
        """Total size of the subject time series."""
        return sum(self.n_voxels * sum(entry["runs"]) * self.dtype.itemsize for entry in self.metadata["subjects"])

    def runs(self, subject_id):
        """Number of time points of each run of a subject."""
        return tuple(self._entry(subject_id)["runs"])

    @property
    def mask(self):
        """Boolean mask indexed [i, j, k] (loaded and validated on first access)."""
        if self._mask is None:
            data, affine, _ = read_nifti(os.path.join(self.path, "mask.nii.gz"))
            data = data.reshape(data.shape[:3])
            if data.shape != self.shape or not np.allclose(affine, self.affine, atol=1e-4, rtol=0):
                raise ValueError(f"mask.nii.gz geometry does not match connectome.json in {self.path}")
            mask = data != 0
            if int(mask.sum()) != self.n_voxels:
                raise ValueError(f"mask.nii.gz has {int(mask.sum())} voxels, connectome.json says {self.n_voxels}")
            self._mask = mask
        return self._mask

    @property
    def mask_indices(self):
        """Flat C-order indices of the mask voxels (defines the voxel order of all vectors)."""
        if self._mask_indices is None:
            self._mask_indices = mask_indices(self.mask)
        return self._mask_indices

    @property
    def degree(self):
        """Degree map (V,) float32 from derived/degree.npy, or None if not computed."""
        path = os.path.join(self.path, "derived", "degree.npy")
        if not os.path.isfile(path):
            return None
        return _read_npy(path, (self.n_voxels,), np.dtype(np.float32))

    # Provenance (optional connectome.json keys)

    @property
    def kind(self):
        """Bundle kind: "timeseries" (the default; the only kind the engine computes maps from)."""
        return self.metadata.get("kind") or "timeseries"

    @property
    def provenance(self):
        """The optional provenance keys of connectome.json (a new dict) with defaults for missing ones.

        Keys: source, kind, mask, subjectsSha256, licence ("unknown"), termsSha256, acknowledgement,
        preprocessing (gsr "unknown"), population, trS, subsets and participants (see the module docstring).
        Values are returned as stored; :attr:`subsets` and :attr:`mask_sha256` validate theirs.
        """
        meta = self.metadata

        def section(key, defaults):
            value = meta.get(key)
            return {**defaults, **(copy.deepcopy(value) if isinstance(value, dict) else {})}

        return {
            "source": section("source", {"doi": None, "version": None, "url": None, "files": []}),
            "kind": self.kind,
            "mask": section("mask", {"source": None, "sha256": None}),
            "subjectsSha256": meta.get("subjectsSha256"),
            "licence": meta.get("licence") or "unknown",
            "termsSha256": meta.get("termsSha256"),
            "acknowledgement": meta.get("acknowledgement") or "",
            "preprocessing": section("preprocessing", {"gsr": "unknown", "nuisance": None, "bandpass": None, "smoothingMm": None, "pipelineUrl": None}),
            "population": section("population", {"n": None, "ageRange": None, "sexRatio": None, "scanners": None}),
            "trS": meta.get("trS"),
            "subsets": copy.deepcopy(meta.get("subsets")) if isinstance(meta.get("subsets"), dict) else {},
            "participants": meta.get("participants"),
        }

    @property
    def subsets(self):
        """Named subject subsets {name: [subject ids]} (empty when there are none).

        Raises ValueError when a subset is malformed or lists ids that are not in the bundle.
        """
        subsets = self.metadata.get("subsets")
        return {} if subsets is None else _validate_subsets(subsets, self.subject_ids)

    @property
    def mask_sha256(self):
        """sha256 of the mask voxels (:func:`mask_content_sha256`), computed once.

        Raises ValueError when connectome.json declares a different "mask.sha256".
        """
        if self._mask_sha256 is None:
            computed = mask_content_sha256(self.mask)
            declared = (self.metadata.get("mask") or {}).get("sha256") if isinstance(self.metadata.get("mask"), dict) else None
            if declared and declared != computed:
                raise ValueError(f"mask.nii.gz does not match the mask sha256 recorded in connectome.json ({self.path})")
            self._mask_sha256 = computed
        return self._mask_sha256

    def resolve_subject_list(self, spec):
        """Resolve a subject list specification; return a :class:`SubjectList`.

        :param spec: a subset name (see :attr:`subsets`); the path of a text file of subject ids (one per
          line, or separated by commas, tabs or spaces; "#" starts a comment; a first line
          "participant_id", "subject" or "id" is a header, and a header with several columns makes the file a
          table of which only the first column is read); a comma-separated text of ids; or a list of ids.
        The ids are returned in bundle order, without duplicates. Unknown ids raise ValueError, which lists them.
        """
        name = path = None
        if isinstance(spec, (str, os.PathLike)):
            text = os.fspath(spec)
            subsets = self.subsets
            if text not in subsets and text.strip() in subsets:
                text = text.strip()  # " First 100 Subjects " names the subset "First 100 Subjects"
            if text in subsets:
                source, name, ids = "subset", text, subsets[text]
            elif os.path.isfile(text):
                source, path = "file", os.path.abspath(text)
                ids = _read_subject_list_file(text)
            else:
                source, ids = "ids", [token.strip() for token in text.split(",") if token.strip()]
                if len(ids) == 1 and ids[0] not in self._subjects:
                    named = f" (named subsets: {', '.join(repr(n) for n in subsets)})" if subsets else " (it has no named subsets)"
                    raise ValueError(f"{text!r} is not a subset name, a file or a subject id of the connectome {self.name!r}{named}")
        else:
            source, ids = "ids", [str(i) for i in spec]
        if not ids:
            raise ValueError("The subject list is empty")
        unknown = [i for i in dict.fromkeys(ids) if i not in self._subjects]
        if unknown:
            raise ValueError(f"{len(unknown)} subject id(s) of the list are not in the connectome {self.name!r}: {', '.join(unknown[:20])}{' ...' if len(unknown) > 20 else ''}")
        wanted = set(ids)
        ordered = [subject_id for subject_id in self.subject_ids if subject_id in wanted]
        return SubjectList(ordered, name, source, path, subjects_sha256(ordered))

    def subject_list(self, spec):
        """Subject ids of a subset name, a file of ids or a list of ids, in bundle order (see :meth:`resolve_subject_list`)."""
        return self.resolve_subject_list(spec).ids

    def participants_rows(self):
        """Rows of the bundle's participants.tsv as dicts (column name to text); [] when there is none."""
        relative = self.metadata.get("participants")
        if not isinstance(relative, str) or not relative:
            return []
        path = os.path.normpath(os.path.join(self.path, relative))
        if not path.startswith(self.path + os.sep) or not os.path.isfile(path):
            return []
        with open(path, encoding="utf-8-sig", newline="") as f:
            lines = [line.rstrip("\r\n") for line in f]
        lines = [line for line in lines if line.strip()]
        if not lines:
            return []
        header = lines[0].split("\t")
        return [dict(zip(header, (line.split("\t") + [""] * len(header))[: len(header)], strict=True)) for line in lines[1:]]

    # Subjects

    def _entry(self, subject_id):
        try:
            return self._subjects[subject_id]
        except KeyError:
            raise KeyError(f"Unknown subject {subject_id!r} in {self.name}") from None

    def subject_files(self, subject_id):
        """Return (time series path, norms path) of a subject."""
        self._entry(subject_id)
        suffix = SUBJECT_DTYPES[self.metadata["dtype"]][0]
        base = os.path.join(self.path, "subjects", subject_id)
        return f"{base}.{suffix}.npy", f"{base}.norms.npy"

    def load_subject(self, subject_id, buffer=None):
        """Load one subject.

        :param buffer: optional flat array of the bundle dtype with at least V * T_total elements;
          the time series is read into it (no allocation).
        """
        runs = self.runs(subject_id)
        shape = (self.n_voxels, sum(runs))
        data_path, norms_path = self.subject_files(subject_id)
        out = None if buffer is None else buffer[: shape[0] * shape[1]].reshape(shape)
        data = _read_npy(data_path, shape, self.dtype, out)
        norms = _read_npy(norms_path, (len(runs), self.n_voxels), np.dtype(np.float32))
        return SubjectData(subject_id, data, norms, runs)

    def select_subjects(self, n=None, random=False, seed=0):
        """Return subject ids: all (n=None), the first n, or n chosen at random (reproducible with seed).

        Random subsets keep the bundle order.
        """
        ids = self.subject_ids
        if n is None or n >= len(ids):
            return ids
        if n < 1:
            raise ValueError("The number of subjects must be at least 1")
        if not random:
            return ids[:n]
        chosen = np.sort(np.random.default_rng(seed).choice(len(ids), size=n, replace=False))
        return [ids[i] for i in chosen]

    def iter_subjects(self, subject_ids=None, prefetch=True, reuse_buffers=False):
        """Iterate over subjects, reading the next one in a background thread.

        :param subject_ids: ids to load (default: all, in bundle order).
        :param prefetch: read ahead in a background thread while the caller computes.
        :param reuse_buffers: recycle the time series arrays: each yielded ``SubjectData.data`` is only
          valid until the next iteration step. Avoids allocating ~100 MB per subject.
        The background thread stops when the iteration ends, the generator is closed (``break``
        or garbage collection) or the bundle is closed.
        """
        ids = self.subject_ids if subject_ids is None else list(subject_ids)
        for subject_id in ids:
            self._entry(subject_id)
        if not prefetch:
            buffer = self._new_buffer(ids) if reuse_buffers and ids else None
            for subject_id in ids:
                yield self.load_subject(subject_id, buffer)
            return
        prefetcher = _Prefetcher(self, ids, reuse_buffers)
        with self._lock:
            self._prefetchers.add(prefetcher)
        try:
            yield from prefetcher
        finally:
            prefetcher.close()
            with self._lock:
                self._prefetchers.discard(prefetcher)

    def _new_buffer(self, ids):
        return np.empty(self.n_voxels * max(sum(self.runs(i)) for i in ids), dtype=self.dtype)

    def close(self):
        """Stop all background readers."""
        with self._lock:
            prefetchers = list(self._prefetchers)
        for prefetcher in prefetchers:
            prefetcher.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __repr__(self):
        return f"ConnectomeBundle({self.name!r}, subjects={self.n_subjects}, voxels={self.n_voxels})"


class SubjectList(NamedTuple):
    """A resolved subject list (see :meth:`ConnectomeBundle.resolve_subject_list`)."""

    ids: list  # subject ids in bundle order
    name: str  # subset name, or None
    source: str  # "subset", "file" or "ids"
    path: str  # absolute path of the file of ids, or None
    sha256: str  # subjects_sha256(ids)


_SUBJECT_LIST_HEADERS = {"participant_id", "subject", "subject_id", "id", "ids"}


def _read_subject_list_file(path):
    """Subject ids of a text file: ids separated by commas, tabs, spaces or new lines; in a table (a TSV,
    or a CSV or TSV whose header row has several columns, such as participants.tsv) only the first column.
    """
    ids = []
    first = True
    table = False
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            fields = [field for field in re.split(r"[,\s]+", line) if field]
            if first and fields[0].lower() in _SUBJECT_LIST_HEADERS:
                first = False
                table = len(fields) > 1
                continue
            first = False
            if table or "\t" in line:
                fields = fields[:1]  # only the first column holds ids
            ids.extend(fields)
    return ids


class _Failure:
    def __init__(self, exc):
        self.exc = exc


class _Prefetcher:
    """Loads subjects one step ahead of the consumer in a daemon thread."""

    _END = object()
    _POLL = 0.05

    def __init__(self, bundle, subject_ids, reuse_buffers, depth=1):
        self._bundle = bundle
        self._ids = subject_ids
        self._queue = queue.Queue(maxsize=depth)
        self._stop = threading.Event()
        self._free = None
        if reuse_buffers and subject_ids:
            # One buffer held by the consumer, `depth` queued and one being filled
            self._free = queue.Queue()
            for _ in range(depth + 2):
                self._free.put(bundle._new_buffer(subject_ids))
        self._thread = threading.Thread(target=self._run, name="LNM-prefetch", daemon=True)
        self._thread.start()

    def _put(self, item):
        while not self._stop.is_set():
            try:
                self._queue.put(item, timeout=self._POLL)
                return True
            except queue.Full:
                pass
        return False

    def _run(self):
        try:
            for subject_id in self._ids:
                buffer = None
                if self._free is not None:
                    while buffer is None:
                        if self._stop.is_set():
                            return
                        try:
                            buffer = self._free.get(timeout=self._POLL)
                        except queue.Empty:
                            pass
                if self._stop.is_set():
                    return
                subject = self._bundle.load_subject(subject_id, buffer)
                if not self._put((subject, buffer)):
                    return
        except Exception as exc:
            self._put(_Failure(exc))
            return
        self._put(self._END)

    def __iter__(self):
        while True:
            item = self._get()
            if item is self._END:
                return
            if isinstance(item, _Failure):
                raise item.exc
            subject, buffer = item
            yield subject
            if buffer is not None:
                self._free.put(buffer)

    def _get(self):
        while not self._stop.is_set():
            try:
                return self._queue.get(timeout=self._POLL)
            except queue.Empty:
                if not self._thread.is_alive() and self._queue.empty():
                    raise RuntimeError("Connectome prefetch thread stopped unexpectedly")
        return self._END

    def _drain(self):
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def close(self):
        self._stop.set()
        self._drain()  # unblocks a pending put; the thread then sees the stop flag
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=10)
        self._drain()


class ConnectomeWriter:
    """Build a bundle incrementally: ``add_subject`` for each subject, then ``finalize``.

    Subjects are saved as they are added and listed in connectome.partial.json, so an interrupted
    conversion can continue with ``resume=True`` (subjects already written are kept; use ``has_subject``).
    Otherwise (e.g. ``overwrite=True``) the manifests, subject files and derived maps of a previous
    bundle at the same path are deleted first.
    """

    def __init__(self, path, mask, affine, name=None, description="", citation="", data_use_terms="", dtype="float16", overwrite=False, resume=False, provenance=None):
        """:param provenance: optional dict of provenance keys (see the module docstring and
        :func:`set_metadata`), validated and added to connectome.json by :meth:`finalize`.
        """
        if dtype not in SUBJECT_DTYPES:
            raise ValueError(f"dtype must be one of {sorted(SUBJECT_DTYPES)}")
        self.provenance = dict(provenance or {})
        # Fail early on bad keys; the values that depend on the subjects are checked by finalize()
        _apply_metadata({"subjects": []}, {key: value for key, value in self.provenance.items() if key not in ("subsets", "subjectsSha256")})
        self.path = os.path.abspath(os.fspath(path))
        manifest = os.path.join(self.path, MANIFEST_NAME)
        if os.path.exists(manifest) and not (overwrite or resume):
            raise FileExistsError(f"A connectome bundle already exists at {self.path}")
        os.makedirs(os.path.join(self.path, "subjects"), exist_ok=True)
        self.mask = np.asarray(mask).reshape(np.asarray(mask).shape[:3]) != 0
        # Affines are stored as float32 in NIfTI headers; keep connectome.json identical to them.
        self.affine = np.asarray(affine, dtype=np.float64).astype(np.float32).astype(np.float64)
        self.mask_indices = mask_indices(self.mask)
        self.metadata = {
            "format": FORMAT_NAME,
            "version": FORMAT_VERSION,
            "name": name or os.path.splitext(os.path.basename(self.path))[0],
            "description": description,
            "citation": citation,
            "dataUseTerms": data_use_terms,
            "shape": list(self.mask.shape),
            "affine": self.affine.tolist(),
            "nVoxels": len(self.mask_indices),
            "subjects": [],
            "dtype": dtype,
        }
        self._suffix, self._dtype = SUBJECT_DTYPES[dtype]
        if resume:
            self._resume()
        else:
            self._remove_previous()
        write_nifti(os.path.join(self.path, "mask.nii.gz"), self.mask.astype(np.uint8), self.affine, descrip="LNM connectome mask")

    @property
    def n_voxels(self):
        return len(self.mask_indices)

    def _resume(self):
        for name in (PARTIAL_MANIFEST_NAME, MANIFEST_NAME):
            path = os.path.join(self.path, name)
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as f:
                previous = json.load(f)
            same_grid = previous.get("shape") == self.metadata["shape"] and np.allclose(previous.get("affine", np.zeros((4, 4))), self.affine, atol=1e-4, rtol=0)
            if not same_grid or previous.get("nVoxels") != self.n_voxels or previous.get("dtype") != self.metadata["dtype"] or not self._same_mask_on_disk():
                raise ValueError(f"Cannot resume: the existing bundle at {self.path} has a different grid, mask or dtype")
            # Keep the provenance of the interrupted conversion; keys given to this writer take precedence
            saved = previous.get(_PENDING_PROVENANCE_KEY) if name == PARTIAL_MANIFEST_NAME else previous
            if isinstance(saved, dict):
                saved = {key: value for key, value in saved.items() if key in PROVENANCE_KEYS and key != "subjectsSha256"}
                self.provenance = {**saved, **self.provenance}
            for entry in previous.get("subjects", []):
                if all(os.path.isfile(p) for p in self._subject_files(entry["id"])):
                    self.metadata["subjects"].append(entry)
            return

    def _same_mask_on_disk(self):
        mask_path = os.path.join(self.path, "mask.nii.gz")
        if not os.path.isfile(mask_path):
            return True
        previous = read_nifti(mask_path)[0]
        return previous.size == self.mask.size and np.array_equal(previous.reshape(self.mask.shape) != 0, self.mask)

    def _remove_previous(self):
        """Delete a previous bundle's manifests (first, so that an interrupted overwrite never leaves
        a manifest describing new data), its subject files and derived degree maps.
        """
        stale = [os.path.join(self.path, "derived", name) for name in ("degree.npy", "degree_fz.npy")]
        for name in (MANIFEST_NAME, PARTIAL_MANIFEST_NAME):
            path = os.path.join(self.path, name)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    previous = json.load(f)
                suffix = SUBJECT_DTYPES.get(previous.get("dtype"), ("f16",))[0]
                for entry in previous.get("subjects", []):
                    subject_id = entry.get("id")
                    if isinstance(subject_id, str) and _SUBJECT_ID_PATTERN.match(subject_id):
                        base = os.path.join(self.path, "subjects", subject_id)
                        stale += [f"{base}.{suffix}.npy", f"{base}.norms.npy"]
            except (OSError, ValueError, AttributeError):
                pass  # unreadable manifest: only the manifest itself is removed
            os.remove(path)
        for path in stale:
            if os.path.isfile(path):
                os.remove(path)

    def _subject_files(self, subject_id):
        base = os.path.join(self.path, "subjects", subject_id)
        return f"{base}.{self._suffix}.npy", f"{base}.norms.npy"

    def has_subject(self, subject_id):
        return any(entry["id"] == subject_id for entry in self.metadata["subjects"])

    def add_subject(self, subject_id, runs):
        """Normalize and save one subject.

        :param subject_id: letters, digits, '.', '_' and '-' (see :func:`sanitize_subject_id`).
        :param runs: list of (V, T_r) arrays of masked time series (voxel-major, mask voxel order).
          Non-finite values are treated as 0.
        """
        if not _SUBJECT_ID_PATTERN.match(subject_id):
            raise ValueError(f"Invalid subject id {subject_id!r}")
        if self.has_subject(subject_id):
            raise ValueError(f"Subject {subject_id!r} was already added")
        runs = [np.asarray(run) for run in runs]
        if not runs:
            raise ValueError(f"Subject {subject_id!r} has no runs")
        for run in runs:
            if run.ndim != 2 or run.shape[0] != self.n_voxels or run.shape[1] < 2:
                raise ValueError(f"Subject {subject_id!r}: each run must have shape (V={self.n_voxels}, T>=2), got {run.shape}")
        lengths = [run.shape[1] for run in runs]
        data = np.empty((self.n_voxels, sum(lengths)), dtype=self._dtype)
        norms = np.empty((len(runs), self.n_voxels), dtype=np.float32)
        start = 0
        for index, run in enumerate(runs):
            norms[index] = normalize_run(run, out=data[:, start:start + run.shape[1]])[1]
            start += run.shape[1]
        data_path, norms_path = self._subject_files(subject_id)
        _save_npy_atomic(data_path, data)
        _save_npy_atomic(norms_path, norms)
        self.metadata["subjects"].append({"id": subject_id, "runs": lengths})
        partial = {**self.metadata, _PENDING_PROVENANCE_KEY: self.provenance} if self.provenance else self.metadata
        _write_json_atomic(os.path.join(self.path, PARTIAL_MANIFEST_NAME), partial)

    def finalize(self):
        """Write connectome.json (with the provenance keys, "subjectsSha256" and "mask.sha256") and return the bundle path."""
        if not self.metadata["subjects"]:
            raise ValueError("Cannot finalize a connectome bundle without subjects")
        provenance = dict(self.provenance)
        mask_info = dict(provenance.get("mask") or {})
        computed = mask_content_sha256(self.mask)
        if mask_info.get("sha256") not in (None, computed):
            raise ValueError("The provenance 'mask.sha256' does not match the bundle mask")
        provenance["mask"] = {**mask_info, "sha256": computed}
        provenance["subjectsSha256"] = subjects_sha256(entry["id"] for entry in self.metadata["subjects"])
        self.metadata = _apply_metadata(self.metadata, provenance)
        _write_json_atomic(os.path.join(self.path, MANIFEST_NAME), self.metadata)
        partial = os.path.join(self.path, PARTIAL_MANIFEST_NAME)
        if os.path.exists(partial):
            os.remove(partial)
        return self.path

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.finalize()


def set_metadata(bundle_path, **keys):
    """Set optional connectome.json keys of a bundle in place (atomically); a value of None removes the key.

    Only the provenance keys (:data:`PROVENANCE_KEYS`) and the descriptive keys (:data:`DESCRIPTIVE_KEYS`)
    can be set; the keys that define the data (:data:`REQUIRED_KEYS`) are refused. Values are validated
    (for example "subsets" may only list subjects of the bundle). Note that any change changes the
    manifest sha256 that runs and terms acceptances refer to.

    Example: ``set_metadata(path, licence="GSP-DUT-2014", preprocessing={"gsr": True}, subsets={"user:young": ids})``

    :return: the new metadata dict.
    """
    path = os.path.join(os.path.abspath(os.fspath(bundle_path)), MANIFEST_NAME)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Not a connectome bundle (missing {MANIFEST_NAME}): {bundle_path}")
    bundle = ConnectomeBundle(os.path.dirname(path))  # the bundle must be valid
    metadata = bundle.metadata
    if isinstance(keys.get("mask"), dict):
        computed = mask_content_sha256(bundle.mask)
        if keys["mask"].get("sha256") not in (None, computed):
            raise ValueError("'mask.sha256' does not match the bundle mask")
        keys["mask"] = {**keys["mask"], "sha256": computed}
    if "subjectsSha256" not in keys:
        keys["subjectsSha256"] = subjects_sha256(bundle.subject_ids)
    metadata = _apply_metadata(metadata, keys)
    _write_json_atomic(path, metadata)
    return metadata


def normalize_run(run, out=None, block=8192):
    """Demean each row of a (V, T) run and scale it to unit L2 norm (computed in float64).

    :param out: optional (V, T) array (any float dtype, may be a strided view) receiving the result.
    :return: (unit-norm rows, float32 norms). Non-finite values are treated as 0; zero-variance rows
      (relative to their magnitude) become all zeros with norm 0.
    """
    run = np.asarray(run)
    if out is None:
        out = np.empty(run.shape, dtype=np.float64)
    norms = np.empty(run.shape[0], dtype=np.float32)
    for v0 in range(0, run.shape[0], block):
        x = np.array(run[v0:v0 + block], dtype=np.float64)
        if not np.isfinite(x).all():
            np.nan_to_num(x, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
        raw_norm = np.sqrt(np.einsum("ij,ij->i", x, x))
        x -= x.mean(axis=1, keepdims=True)
        norm = np.sqrt(np.einsum("ij,ij->i", x, x))
        valid = norm > 1e-7 * raw_norm
        norm[~valid] = 0.0
        x *= np.divide(1.0, norm, out=np.zeros_like(norm), where=valid)[:, np.newaxis]
        out[v0:v0 + block] = x
        norms[v0:v0 + block] = norm
    return out, norms


# Converters


def _default_name(out_dir):
    return os.path.splitext(os.path.basename(os.path.abspath(os.fspath(out_dir))))[0]


def _strip_nifti_extension(path):
    name = os.path.basename(os.fspath(path))
    for ext in (".nii.gz", ".nii"):
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return name


def from_nifti_timeseries(
    files_or_glob,
    mask_path,
    out_dir,
    runs_grouping=None,
    name=None,
    description="",
    citation="",
    data_use_terms="",
    dtype="float16",
    resume=False,
    overwrite=False,
    progress=None,
    cancel=None,
    participants=None,
    provenance=None,
):
    """Build a bundle from preprocessed 4D NIfTI time series (one file per run).

    :param files_or_glob: list of .nii/.nii.gz files or a glob pattern (files are sorted).
    :param mask_path: mask NIfTI defining the connectome grid; every file must be on this grid.
    :param runs_grouping: optional regular expression searched in each file name; the group named
      "subject" (or the first group, or the whole match) is the subject id, and files with the same id
      become runs of that subject. By default every file is a separate single-run subject.
    :param progress: optional callback(done_subjects, total_subjects, elapsed_s).
    :param cancel: optional threading.Event or callable; when set the conversion stops and None is
      returned (call again with resume=True to continue).
    :param participants: optional participants.tsv describing the subjects; it is copied into the
      bundle as participants.tsv (with its participants.json data dictionary, when there is one).
    :param provenance: optional provenance keys for connectome.json (see :func:`set_metadata`).
    :return: bundle path.
    """
    files = sorted(glob.glob(os.fspath(files_or_glob))) if isinstance(files_or_glob, (str, os.PathLike)) else [os.fspath(f) for f in files_or_glob]
    if not files:
        raise FileNotFoundError(f"No time series files found for {files_or_glob}")
    mask_data, mask_affine, _ = read_nifti(mask_path)
    mask = mask_data.reshape(mask_data.shape[:3]) != 0
    mask_grid = Grid(mask.shape, mask_affine)

    pattern = re.compile(runs_grouping) if runs_grouping else None
    groups = {}
    original_ids = {}
    for path in files:
        base = _strip_nifti_extension(path)
        if pattern is None:
            subject = base
        else:
            match = pattern.search(base)
            if match is None:
                raise ValueError(f"File name does not match the runs grouping pattern {runs_grouping!r}: {path}")
            if "subject" in pattern.groupindex:
                subject = match.group("subject")
            else:
                subject = match.group(1) if pattern.groups else match.group(0)
        subject_id = sanitize_subject_id(subject)
        if original_ids.setdefault(subject_id, subject) != subject:
            raise ValueError(f"Subject ids {original_ids[subject_id]!r} and {subject!r} both become {subject_id!r}; rename the files or change runs_grouping")
        groups.setdefault(subject_id, []).append(path)

    # Check the geometry of every file before converting anything
    for path in files:
        header = read_nifti_header(path)
        if len(header["shape"]) != 4 or header["shape"][3] < 2:
            raise ValueError(f"Expected a 4D time series with at least 2 volumes: {path} has shape {header['shape']}")
        if not Grid(header["shape"][:3], header["affine"]).matches(mask_grid, atol=1e-3):
            raise ValueError(f"{path} is not on the mask grid (shape {header['shape'][:3]} vs {mask_grid.shape}, or different affine); resample it first")

    provenance = dict(provenance or {})
    if participants is not None:
        if not os.path.isfile(participants):
            raise FileNotFoundError(f"participants file not found: {participants}")
        provenance["participants"] = "participants.tsv"
    writer = ConnectomeWriter(
        out_dir,
        mask,
        mask_affine,
        name=name or _default_name(out_dir),
        description=description,
        citation=citation,
        data_use_terms=data_use_terms,
        dtype=dtype,
        overwrite=overwrite,
        resume=resume,
        provenance=provenance,
    )
    if participants is not None:
        _copy_participants(participants, writer.path)
    start = time.perf_counter()
    for done, (subject, paths) in enumerate(groups.items(), 1):
        if _is_cancelled(cancel):
            return None
        if not writer.has_subject(subject):
            runs = [read_nifti(path)[0][mask].astype(np.float32) for path in paths]
            writer.add_subject(subject, runs)
        if progress:
            progress(done, len(groups), time.perf_counter() - start)
    return writer.finalize()


def _copy_participants(source, bundle_path):
    """Copy a participants.tsv (and its participants.json sidecar, if any) into a bundle folder."""
    for source_path, name in ((source, "participants.tsv"), (os.path.splitext(source)[0] + ".json", "participants.json")):
        target = os.path.join(bundle_path, name)
        if os.path.isfile(source_path) and not (os.path.exists(target) and os.path.samefile(source_path, target)):
            tmp_path = f"{target}.{os.getpid()}.tmp"
            shutil.copyfile(source_path, tmp_path)
            os.replace(tmp_path, target)


def _loadmat(path, **kwargs):
    from scipy import io as sio

    with open(path, "rb") as f:
        head = f.read(520)
    if b"MATLAB 7.3" in head[:116] or head[512:520] == b"\x89HDF\r\n\x1a\n":
        raise UnsupportedFormatError(
            f"{path} is a MATLAB v7.3 (HDF5) file, which scipy cannot read. Install h5py "
            "(for example slicer.util.pip_install('h5py')) and convert it, or re-save it in MATLAB "
            "with save(filename, '-struct', 's', '-v7').",
        )
    return sio.loadmat(path, simplify_cells=True, **kwargs)


def _as_list(value):
    if isinstance(value, np.ndarray):
        return value.tolist() if value.dtype != object else list(value.ravel())
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _leaddbs_subject_entries(sub_ids):
    """Normalize vol.subIDs (a cell array of {id, run file, ...} cells) to lists of strings."""
    items = _as_list(sub_ids)
    if items and all(isinstance(item, str) for item in items):
        return [items]  # a single subject: loadmat squeezes the outer cell away
    return [[str(v) for v in _as_list(item)] for item in items]


def _leaddbs_subsets(info, subject_ids, n_listed):
    """Named subsets of dataset_info.json ("subsets": [{"name", "subs": 1-based indices}]) as {name: [ids]}.

    "subs" may be a scalar (a single subject). Subsets that refer to subjects that were not converted
    (``max_subjects``) are left out; malformed entries are ignored.
    """
    entries = info.get("subsets") or []
    if isinstance(entries, dict):
        entries = [entries]  # a single subset (MATLAB jsonencode of a 1x1 struct)
    out = {}
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry["name"].strip():
            continue
        try:
            values = [float(i) for i in _as_list(entry.get("subs"))]
        except (TypeError, ValueError):
            continue
        if not all(value.is_integer() for value in values):
            continue
        indices = [int(value) for value in values]
        if not indices or min(indices) < 1 or max(indices) > n_listed or len(set(indices)) != len(indices):
            continue
        if max(indices) <= len(subject_ids):
            out[entry["name"].strip()] = [subject_ids[i - 1] for i in indices]
    return out


def _leaddbs_source(info):
    """The "source" provenance of a Lead-DBS dataset: name, tag and citation of dataset_info.json, and the DOI in the citation."""
    source = {key: info[key] for key in ("name", "tag", "citation") if info.get(key)}
    citation = info.get("citation")
    texts = [citation] if isinstance(citation, str) else [v for v in citation.values() if isinstance(v, str)] if isinstance(citation, dict) else []
    for text in texts:
        match = _DOI_PATTERN.search(text)
        if match:
            source["doi"] = match.group(1).rstrip(".,;)")
            break
    return source


def _leaddbs_citation_text(info):
    citation = info.get("citation")
    if isinstance(citation, str):
        return citation
    if isinstance(citation, dict):
        return citation.get("long") or citation.get("short") or ""
    return ""


def from_leaddbs(dataset_dir, out_dir, name=None, description="", citation="", data_use_terms="", dtype="float16", max_subjects=None, resume=False, overwrite=False, progress=None, cancel=None, provenance=None):
    """Convert a Lead-DBS fMRI connectome folder (dataset_volsurf.mat, dataset_info.json, vol/*.mat).

    ``vol.outidx`` holds 1-based MATLAB (Fortran-order) voxel indices into ``vol.space`` (an SPM
    volume with a 1-based ``mat``); ``vol.subIDs`` lists, per subject, the id followed by one file
    per run, each containing ``gmtc`` (T x V in format 1.1, V x T in format 1.0). Voxels are reordered
    to the bundle's C order. Only MATLAB v5/v7 files are supported (v7.3 raises UnsupportedFormatError).

    The named subsets of dataset_info.json (for example "First 100 Subjects", given as 1-based indices
    into vol.subIDs) become "subsets" of connectome.json, and its name, tag, citation and DOI are
    recorded as "source". Without ``citation`` the dataset's citation text is used.

    :param max_subjects: optional limit on the number of converted subjects.
    :param provenance: optional provenance keys for connectome.json (see :func:`set_metadata`);
      its "source" keys are added to the ones read from dataset_info.json.
    :return: bundle path, or None if cancelled.
    """
    dataset_dir = os.fspath(dataset_dir)
    info = {}
    info_path = os.path.join(dataset_dir, "dataset_info.json")
    if os.path.isfile(info_path):
        with open(info_path, encoding="utf-8") as f:
            info = json.load(f)
    if info.get("type") == "fMRI_matrix":
        raise UnsupportedFormatError("Lead-DBS matrix connectomes (precomputed correlations) are not supported; a time series connectome is needed")
    vol = _loadmat(os.path.join(dataset_dir, "dataset_volsurf.mat"))["vol"]
    space = vol["space"]
    shape = tuple(int(v) for v in np.ravel(space["dim"])[:3])
    affine = affine_from_spm_mat(np.asarray(space["mat"], dtype=np.float64).reshape(4, 4))
    outidx = np.asarray(vol["outidx"], dtype=np.int64).ravel() - 1
    if outidx.size == 0 or outidx.min() < 0 or outidx.max() >= np.prod(shape):
        raise ValueError("vol.outidx is out of range for vol.space.dim")
    c_index = np.ravel_multi_index(np.unravel_index(outidx, shape, order="F"), shape)
    if len(np.unique(c_index)) != len(c_index):
        raise ValueError("vol.outidx contains duplicate voxels")
    order = np.argsort(c_index, kind="stable")
    mask = np.zeros(int(np.prod(shape)), dtype=bool)
    mask[c_index] = True
    n_voxels = len(outidx)

    entries = _leaddbs_subject_entries(vol["subIDs"])
    n_listed = len(entries)
    if max_subjects is not None:
        entries = entries[: int(max_subjects)]
    subject_ids = []
    for entry in entries:
        if len(entry) < 2:
            raise ValueError(f"Subject {entry[0] if entry else ''!r} lists no run files in vol.subIDs")
        subject_ids.append(sanitize_subject_id(entry[0]))
        if subject_ids.count(subject_ids[-1]) > 1:
            raise ValueError(f"vol.subIDs has duplicate subject id {entry[0]!r} (as {subject_ids[-1]!r})")
    provenance = dict(provenance or {})
    source = {**_leaddbs_source(info), **(provenance.get("source") or {})}
    if source:
        provenance["source"] = source
    subsets = {**_leaddbs_subsets(info, subject_ids, n_listed), **(provenance.get("subsets") or {})}
    if subsets:
        provenance["subsets"] = subsets
    writer = ConnectomeWriter(
        out_dir,
        mask.reshape(shape),
        affine,
        name=name or _default_name(out_dir),
        description=description,
        citation=citation or _leaddbs_citation_text(info),
        data_use_terms=data_use_terms,
        dtype=dtype,
        overwrite=overwrite,
        resume=resume,
        provenance=provenance,
    )
    format_version = float(info.get("formatversion", 1.0))
    start = time.perf_counter()
    for done, (subject, entry) in enumerate(zip(subject_ids, entries, strict=True), 1):
        if _is_cancelled(cancel):
            return None
        if not writer.has_subject(subject):
            runs = []
            for run_file in entry[1:]:
                path = os.path.join(dataset_dir, "vol", run_file)
                if not os.path.exists(path) and os.path.exists(path + ".mat"):
                    path += ".mat"
                gmtc = np.asarray(_loadmat(path, variable_names=["gmtc"])["gmtc"], dtype=np.float32)
                if gmtc.ndim != 2 or n_voxels not in gmtc.shape:
                    raise ValueError(f"{path}: gmtc has shape {gmtc.shape}, expected T x {n_voxels}")
                voxel_major = gmtc.shape[0] == n_voxels and (gmtc.shape[1] != n_voxels or format_version < 1.1)
                runs.append((gmtc if voxel_major else gmtc.T)[order])
            writer.add_subject(subject, runs)
        if progress:
            progress(done, len(entries), time.perf_counter() - start)
    return writer.finalize()


def make_synthetic(out_dir, shape=(20, 24, 20), n_subjects=6, runs=(30, 30), seed=0, amplitude=1.5, radius=3.0, affine=None, name="Synthetic", dtype="float16", overwrite=True):
    """Write a small synthetic connectome with planted networks (for tests and demos).

    The mask is an ellipsoid. Blobs A and A2 (distant from each other) share a common signal, blob B
    carries the negated signal (anti-correlated with A), every voxel has independent Gaussian noise
    and a constant baseline offset.

    :param affine: grid affine; default 2 mm RAS+ voxels centered on the origin.
    :return: dict with "path", "shape", "affine" and "blobs" ({name: {"center_ijk", "center_world",
      "mask"}}), where "mask" is a boolean vector over the bundle voxels.
    """
    rng = np.random.default_rng(seed)
    shape = tuple(int(n) for n in shape)
    if affine is None:
        affine = np.diag([2.0, 2.0, 2.0, 1.0])
        affine[:3, 3] = -(np.array(shape) - 1)
    grid = Grid(shape, affine)
    ijk = np.indices(shape).reshape(3, -1).T
    center = (np.array(shape) - 1) / 2.0
    mask = (((ijk - center) / (np.array(shape) / 2.2)) ** 2).sum(axis=1) <= 1.0
    voxels = ijk[mask]

    blobs = {}
    for blob_name, fraction in (("A", (0.25, 0.25, 0.5)), ("A2", (0.72, 0.75, 0.5)), ("B", (0.5, 0.5, 0.2))):
        blob_center = np.round(np.array(fraction) * (np.array(shape) - 1)).astype(int)
        blobs[blob_name] = {
            "center_ijk": tuple(int(v) for v in blob_center),
            "center_world": tuple(float(v) for v in grid.voxel_to_world(blob_center[np.newaxis])[0]),
            "mask": np.linalg.norm(voxels - blob_center, axis=1) <= radius,
        }
    network = blobs["A"]["mask"] | blobs["A2"]["mask"]
    anti = blobs["B"]["mask"]
    offsets = 100.0 + 10.0 * rng.random(len(voxels))

    writer = ConnectomeWriter(out_dir, mask.reshape(shape), affine, name=name, description="Synthetic connectome with planted networks", dtype=dtype, overwrite=overwrite)
    for s in range(n_subjects):
        subject_runs = []
        for n_tr in runs:
            latent = rng.standard_normal(n_tr)
            x = rng.standard_normal((len(voxels), n_tr))
            x[network] += amplitude * latent
            x[anti] -= amplitude * latent
            subject_runs.append(x + offsets[:, np.newaxis])
        writer.add_subject(f"sub-{s + 1:03d}", subject_runs)
    path = writer.finalize()
    return {"path": path, "shape": shape, "affine": grid.affine, "blobs": blobs}


def compute_degree(bundle, n_seeds=1000, subjects=None, measure="abs_fz", seed=0, chunk=8192, store=True, progress=None, cancel=None):
    """Connectome degree map for confound QA: the mean |Fisher z| ("hubness"), mean signed Fisher z
    or mean r of each voxel with a random sample of seed voxels, averaged over subjects.
    Self-correlations are excluded.

    :param subjects: None (all), an int (first n) or a list of ids.
    :param measure: "abs_fz" (mean |Fz|, hubness), "fz" (signed mean Fz) or "r" (mean r).
    :param store: save the result in the bundle's derived folder: degree.npy (the map used by the QA,
      see :attr:`ConnectomeBundle.degree`) for "abs_fz" and "r", degree_fz.npy for the signed "fz"
      (so that it never replaces the hubness reference).
    :return: (V,) float32 degree map, or None if cancelled.
    """
    from .engine import RMAX, iter_correlation_chunks

    if measure not in ("abs_fz", "fz", "r"):
        raise ValueError("measure must be 'abs_fz', 'fz' or 'r'")
    if isinstance(subjects, int):
        ids = bundle.select_subjects(subjects)
    else:
        ids = bundle.subject_ids if subjects is None else list(subjects)
    n_voxels = bundle.n_voxels
    seeds = np.sort(np.random.default_rng(seed).choice(n_voxels, size=min(int(n_seeds), n_voxels), replace=False))
    is_seed = np.zeros(n_voxels, dtype=bool)
    is_seed[seeds] = True
    seed_column = np.full(n_voxels, -1, dtype=np.int64)
    seed_column[seeds] = np.arange(len(seeds))
    counts = np.where(is_seed, len(seeds) - 1, len(seeds)).astype(np.float64)
    total = np.zeros(n_voxels, dtype=np.float64)
    start = time.perf_counter()
    done = 0
    for subject in bundle.iter_subjects(ids, reuse_buffers=True):
        if _is_cancelled(cancel):
            return None
        # Rows are unit norm per run, so a single voxel's seed is its own row divided by nRuns.
        seed_matrix = np.ascontiguousarray(subject.data[seeds].astype(np.float32).T / len(subject.runs))
        for v0, v1, r in iter_correlation_chunks(subject.data, seed_matrix, chunk):
            if measure == "r":
                values = r.astype(np.float64)
            else:
                values = np.arctanh(np.clip(r, -RMAX, RMAX))
                if measure == "abs_fz":
                    np.abs(values, out=values)
            rows = np.flatnonzero(is_seed[v0:v1])
            values[rows, seed_column[v0 + rows]] = 0.0
            total[v0:v1] += values.sum(axis=1)
        done += 1
        if progress:
            progress(done, len(ids), time.perf_counter() - start)
    degree = (total / np.maximum(counts, 1) / max(done, 1)).astype(np.float32)
    if store:
        os.makedirs(os.path.join(bundle.path, "derived"), exist_ok=True)
        _save_npy_atomic(os.path.join(bundle.path, "derived", "degree_fz.npy" if measure == "fz" else "degree.npy"), degree)
    return degree

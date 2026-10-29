"""Lesion-aware normalization of a native scan to an MNI template (BRAINSFit child processes).

Researchers who trace lesions on the patient's own scan get a template-space lesion that lesion
network mapping can use. The scan is registered to the template with the dilated lesion excluded from
the similarity metric (cost-function masking; Brett et al. 2001, Andersen et al. 2010): an affine
stage, then a BSpline stage. The lesion is warped with linear interpolation onto the template
family's 1 mm grid (fractional and binary files with the exact TemplateFlow header), the scan is
warped for review, and the run writes QC metrics with a suggestion ("review" or "looks-ok", never a
verdict), a QC mosaic PNG and a provenance JSON.

BRAINSFit, BRAINSResample and BRAINSROIAuto ship with every Slicer and run as child processes
(argument lists, never a shell). ANTs is not built (USE_ANTS is OFF), so SyN is not available.
This module imports only the standard library at import time and never imports slicer, qt, vtk or
ctk; numpy, scipy, SimpleITK and Pillow are imported when needed. NIfTI files are read and written
through :mod:`.nifti`.

Command line (one JSON event per line on standard output, see :mod:`.jobs`)::

    python -m LesionNetworkMappingLib.normalize run --scan S [--lesion L] --out DIR [options]
    python -m LesionNetworkMappingLib.normalize --scan S --lesion L --out DIR      ("run" is implied)
    python -m LesionNetworkMappingLib.normalize batch --study DIR [--subjects a,b | --all-native] [--force] [options]
    python -m LesionNetworkMappingLib.normalize warp --run DIR --image PATH [--kind lesion|image] [--out PATH]
    python -m LesionNetworkMappingLib.normalize check [--template ID] [--template-dir DIR ...]

Your own templates (Slicer Studio, A-10): ``--template user:<id> --template-dir "<Slicer Studio folder>/Templates"``
registers to a template that the user added (its ``<name>/template.json``: the image, its brain mask and an optional
whole-head image). The outputs are written on the grid of that image (named ``space-user<Id>``, without a res entity)
and normalization.json records the template's id, name, space and sha256.

Registration (``run``):

- costmask: the lesion on the scan grid (through the affines, never by index), dilated by
  ``--dilate-mm`` (3 mm); the cost mask is NOT(dilated lesion) over the scan. ``--dilate-mm -1``
  disables the masking (diagnostics only; the suggestion is then always "review").
- anat (``--anat`` T1w of the same subject, for FLAIR/T2w/DWI/CT scans): rigid scan -> T1w, then the
  template stages register the T1w; the final transform is a flattened composite.
- affine: an attempt ladder accepted when the NCC inside the template brain minus the dilated
  lesion (``ncc_xl``) is at least 0.40: A1 centre-of-head initialization; A2 head-to-head rigid
  against the whole-head template; A3 a 7-start rigid search (pitch/yaw offsets); A4 replaces the A1
  initialization with ``--initial-transform``.
- bspline (quality standard: grid 8,8,8; fine: 14,10,12; affine: none), NC metric, same masks; the
  affine is kept when the BSpline lowers ncc_xl by more than 0.02.
- warp: BRAINSResample, linear, onto the template's 1 mm grid; the lesion as a fraction
  (desc-lesionProb, float32) and binarized at 0.5 (desc-lesion, uint8), the scan as int16 when it
  fits (desc-preproc).
- qc: metrics and suggestion from :mod:`.normqc`, the QC mosaic and ``<sid>_desc-normqc.json``.

Outputs in ``--out`` (``<sid>`` from --subject, else the sub- entity of the lesion or scan name)::

    <sid>_from-<mod>_to-<tpl>_mode-image_xfm.h5              final transform (resamples native images onto
                                                             the template grid; maps template points to
                                                             native points; Slicer's "FromParent")
    <sid>_from-<src>_to-<tpl>_desc-affine_mode-image_xfm.h5  affine stage (<src> = <mod>, or T1w with --anat)
    <sid>_from-<tpl>_to-<src>_desc-affine_mode-image_xfm.h5  its inverse (the BSpline has no inverse)
    <sid>_from-<mod>_to-T1w_mode-image_xfm.h5                rigid scan -> T1w (with --anat)
    <sid>_space-<tpl>_res-1_desc-lesion_mask.nii.gz (+ .json BIDS sidecar)
    <sid>_space-<tpl>_res-1_desc-lesionProb_mask.nii.gz
    <sid>_space-<tpl>_res-1_desc-preproc_<mod>.nii.gz
    <sid>_desc-costfunction_mask.nii.gz                      cost mask on the moving image's grid
    <sid>_desc-normqc.png, <sid>_desc-normqc.json, normalization.json, logs/*.log

Reuse: a run whose inputs, template files, method and tool versions have the same fingerprint as a
complete previous result is reported as "reused" without computing (``--no-reuse`` forces a rerun).
Work files go to ``<out>/.work-<pid>/``; final files replace the previous ones only after QC, so a
failed or cancelled run leaves the previous result intact. ``<out>/.normalize.lock`` keeps two jobs
from writing the same folder (exit code 75).

``batch`` normalizes every included native-space row of a study, one subject at a time, into
``<study>/derivatives/normalization/<sid>/``, and posts the row values to the study inbox
(:meth:`.study.Study.post_result`); it never writes participants.tsv or lesions/.

Exit codes: 0 done (batch: also with skipped subjects), 1 error, 2 usage, 3 cancelled, 4 nothing to
do (empty lesion, scan already in template space, no eligible row), 75 locked.
"""

import os
import sys

if __name__ == "__main__":
    from LesionNetworkMappingLib.jobs import set_blas_threads

    set_blas_threads(sys.argv[1:])  # before numpy is imported: BLAS reads these variables once

import hashlib
import json
import math
import re
import shutil
import subprocess
import threading
import time

from . import jobs

REQUIRES_SLICER = False  # runs in a plain PythonSlicer (A-04 contract)
NORMALIZE_VERSION = 1
FORMAT = "slicer-lnm-normalization"
EXIT_NOTHING_TO_DO = 4
JOB = "normalize"
COMMANDS = ("run", "batch", "warp", "check")

QUALITY = {
    "standard": {"grid": (8, 8, 8), "bspline": True},
    "fine": {"grid": (14, 10, 12), "bspline": True},
    "affine": {"grid": None, "bspline": False},
}
DEFAULTS = {
    "quality": "standard",
    "dilate_mm": 3.0,
    "affine_sampling": 0.05,
    "bspline_sampling": 0.05,
    "min_step": (0.005, 0.001),
    # the masked rigid stage sometimes oscillates until BRAINSFit's 1500 iterations (60 s); it converges in 50-70
    "max_iterations": (200, 1500),
    "bspline_metric": "NC",
    "binarize": 0.5,
    "accept_ncc": 0.40,
    # (pitch about the left-right axis, yaw about the superior-inferior axis) in degrees
    "tilt_offsets": ((-30, 0), (-15, 0), (15, 0), (30, 0), (0, -20), (0, 20)),
    "template": "auto",
}
DEFAULT_TEMPLATE = "MNI152NLin2009cAsym"
MODALITIES = ("T1w", "T2w", "FLAIR", "DWI", "CT")
TEMPLATEFLOW_URL = "https://templateflow.s3.amazonaws.com/tpl-{id}/{name}"
# Template files: the 2 mm registration target ("fixed") and brain mask, the whole-head image for the
# tilt recovery ("head") and the 1 mm image whose header the outputs copy ("reference", optional).
# The 1 mm output grids are L-05's geometry.GRID_REGISTRY entries named by "grid1".
TEMPLATES = {
    "MNI152NLin2009cAsym": {
        "grid1": "TF2009-1mm",
        "terms": "McGill (bundled brain and mask; LICENSE-MNI152NLin2009cAsym.txt)",
        "files": {
            "fixed": {"name": "tpl-MNI152NLin2009cAsym_res-02_desc-brain_T1w.nii.gz", "sha256": "7b168153a9508f5f116ac37d33835fa44dd168ba7a9ae31a3b33e6f63bd23292", "bytes": 451810},
            "mask": {"name": "tpl-MNI152NLin2009cAsym_res-02_desc-brain_mask.nii.gz", "sha256": "7a71e9ce99d2af7b68bb5c163908c8e25f4c7a198806fb0893a5c1759b480ecd", "bytes": 29576},
            "head": {"name": "tpl-MNI152NLin2009cAsym_res-02_T1w.nii.gz", "sha256": "7c4e551ae8150ac1f468595bd62074aa15aed747cd334fce6d177182972b6220", "bytes": 1763084},
            "reference": {"name": "tpl-MNI152NLin2009cAsym_res-01_T1w.nii.gz", "sha256": "1f27aabea9f7183dc0c69dafa71e1787b921ca778d655d9c1bf302b273d5627a", "bytes": 13685075},
        },
    },
    "MNI152NLin6Asym": {
        "grid1": "NLin6-1mm",
        "terms": "FSL licence terms (non-commercial); downloaded on request only",
        "files": {
            # TemplateFlow has no brain-extracted 2 mm NLin6 T1w: the whole head is the target and the brain mask restricts the metric
            "fixed": {"name": "tpl-MNI152NLin6Asym_res-02_T1w.nii.gz", "sha256": "2a814da50173599a857d96246dc057d548072bd6dffa499f75724dbad20792b1", "bytes": 1412252},
            "mask": {"name": "tpl-MNI152NLin6Asym_res-02_desc-brain_mask.nii.gz", "sha256": "e4e2b284170271afdafe26ac0997b2af5a0f5ddac35e28a7e796b52e8bc5adb1", "bytes": 28557},
            "head": {"name": "tpl-MNI152NLin6Asym_res-02_T1w.nii.gz", "sha256": "2a814da50173599a857d96246dc057d548072bd6dffa499f75724dbad20792b1", "bytes": 1412252},
            "reference": {"name": "tpl-MNI152NLin6Asym_res-01_T1w.nii.gz", "sha256": "f675946c0a077e0682885c3d6085f10acb0dd22b2274a6cbbf04969c08a77258", "bytes": 11027773},
        },
    },
}
TEMPLATE_IDS = tuple(TEMPLATES)
REQUIRED_TEMPLATE_FILES = ("fixed", "mask")
# Your own templates (A-10): "user:<id>" names the folder <Templates>/<name>/ whose template.json has that id
USER_TEMPLATE_PREFIX = "user:"
USER_TEMPLATE_JSON = "template.json"
USER_TEMPLATE_FORMAT = "slicer-studio-template"
_USER_TEMPLATES = {}  # "user:<id>" -> its template.json record (with "folder"), filled by find_template

# Progress model: stage weights (renormalized for the stages that run) and expected seconds at 3 threads
STAGE_WEIGHTS = {"costmask": 0.03, "anat": 0.07, "affine": 0.20, "bspline": 0.40, "warp": 0.20, "qc": 0.10}
EXPECTED_SECONDS = {"costmask": 1.0, "anat": 7.0, "affine": 8.0, "head_rigid": 10.0, "bspline": 15.0, "bspline_fine": 55.0, "warp": 8.0, "qc": 5.0}
PROGRESS_INTERVAL_S = 0.5  # at most 2 progress events per second
POLL_S = 0.25  # cancellation is checked this often while a child runs
TERMINATE_WAIT_S = 5.0
SUBJECT_DISK_BYTES = 15 * 1024 * 1024
WORK_DISK_BYTES = 200 * 1024 * 1024  # uncompressed intermediate images in .work-<pid> (removed after each subject)
LOCK_NAME = ".normalize.lock"
PROVENANCE_JSON = "normalization.json"
NOTE = "BRAINSFit samples randomly and multi-threaded; reruns may differ slightly; reuse by fingerprint keeps results stable"
INBOX_SOURCE = "normalize"
ALIGN_HINT = "Roughly align the scan by hand and retry (--initial-transform)"

_STAGE_LINE = re.compile(r"Starting Transform Estimations for (\w+)\((\d+) of (\d+)\)|Running bspline registration")
_SHA_CACHE = {}
_VERSION_CACHE = {}
_IMAGE_CACHE = {}
_IS_WINDOWS = sys.platform == "win32"


# Small helpers


def _np():
    import numpy as np

    return np


def _sitk():
    import SimpleITK as sitk

    return sitk


def _sha256(path):
    """sha256 of a file, cached per (path, size, mtime_ns) within the process."""
    path = os.path.abspath(os.fspath(path))
    stat = os.stat(path)
    key = (path, stat.st_size, stat.st_mtime_ns)
    if key not in _SHA_CACHE:
        from .export import file_sha256

        _SHA_CACHE[key] = file_sha256(path)
    return _SHA_CACHE[key]


def _rel(path, folder):
    """``path`` relative to ``folder`` with "/" separators (absolute when on another drive)."""
    try:
        return os.path.relpath(os.fspath(path), os.fspath(folder)).replace(os.sep, "/")
    except ValueError:
        return os.fspath(path)


def _now():
    from .export import now_iso

    return now_iso()


def _plain(obj):
    """A strict-JSON copy of ``obj`` (numpy values converted, NaN and infinities as null)."""
    return json.loads(jobs.to_json(obj))


def _nifti_stem(path):
    name = os.path.basename(os.fspath(path))
    for extension in (".nii.gz", ".nii", ".nrrd", ".nhdr", ".mha", ".mhd"):
        if name.lower().endswith(extension):
            return name[: -len(extension)]
    return os.path.splitext(name)[0]


def _is_nifti(path):
    return os.fspath(path).lower().endswith((".nii", ".nii.gz"))


def _warn(message, code, **fields):
    return jobs.emit("warning", message=message, code=code, **fields)


# Templates and executables


def template_grid(template_id):
    """The output grid of a template: the 1 mm grid of a built-in template (a :class:`.geometry.GridSpec` from
    GRID_REGISTRY), the grid of the image of a user template (registered by :func:`find_template`).
    """
    from .geometry import GRID_BY_ID

    if is_user_template(template_id):
        if template_id not in GRID_BY_ID:
            raise jobs.JobError(f"The grid of {template_id} is not known: locate the template first (find_template)", code="template_missing", template=template_id)
        return GRID_BY_ID[template_id]
    return GRID_BY_ID[TEMPLATES[template_id]["grid1"]]


def is_user_template(template_id):
    """True for the id of a template that the user added ("user:<id>")."""
    return str(template_id or "").startswith(USER_TEMPLATE_PREFIX)


def user_template_folders(dirs=()):
    """Folders that hold a template.json: each folder of ``dirs`` that has one, else its subfolders that have one."""
    folders = []
    for folder in dirs:
        if not folder or not os.path.isdir(folder):
            continue
        if os.path.isfile(os.path.join(folder, USER_TEMPLATE_JSON)):
            folders.append(os.path.abspath(folder))
            continue
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        folders += [os.path.abspath(os.path.join(folder, name)) for name in names if os.path.isfile(os.path.join(folder, name, USER_TEMPLATE_JSON))]
    return folders


def read_user_template(folder):
    """The template.json of a user template folder (with "folder" added), or None when it is missing or not one."""
    try:
        with open(os.path.join(folder, USER_TEMPLATE_JSON), encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("schema") != USER_TEMPLATE_FORMAT or not record.get("id"):
        return None
    record["folder"] = os.path.abspath(folder)
    return record


def resolve_user_template(template_id, dirs=()):
    """The template.json record of "user:<id>" found in ``dirs`` (Templates folders or template folders), or None."""
    wanted = str(template_id)[len(USER_TEMPLATE_PREFIX):] if is_user_template(template_id) else str(template_id)
    for folder in user_template_folders(dirs):
        record = read_user_template(folder)
        if record is not None and record["id"] == wanted:
            return record
    return None


def template_space(template_id):
    """The space label of a template: the id of a built-in template, the space of a user template (e.g.
    MNI152NLin2009bAsym, or "MNI" for "MNI (other)") once :func:`find_template` located it.
    """
    if is_user_template(template_id):
        return (_USER_TEMPLATES.get(template_id) or {}).get("space") or "MNI"
    return template_id


def template_label(template_id):
    """The BIDS ``space`` label of the outputs: the id of a built-in template, "user<Id>" for "user:<id>" (letters and
    digits only, e.g. "user:icbm-2009b" -> "userIcbm2009b").
    """
    if not is_user_template(template_id):
        return template_id
    words = re.split(r"[^A-Za-z0-9]+", str(template_id)[len(USER_TEMPLATE_PREFIX):])
    return "user" + "".join(word[:1].upper() + word[1:] for word in words if word)


def _res_entity(template_id):
    """The ``_res-1`` entity of the outputs of a built-in template ("" for a user template: its own grid)."""
    return "" if is_user_template(template_id) else "_res-1"


def template_record(template_id):
    """{"id", "name", "space", "sha256"} of a user template located by :func:`find_template` (None otherwise)."""
    record = _USER_TEMPLATES.get(template_id)
    if record is None:
        return None
    return {"id": template_id, "name": record.get("name"), "space": record.get("space"), "sha256": (record.get("sha256") or {}).get("image")}


def template_url(template_id, role):
    """TemplateFlow download URL of a template file (the CLI never downloads; the GUI does on request)."""
    return TEMPLATEFLOW_URL.format(id=template_id, name=TEMPLATES[template_id]["files"][role]["name"])


def template_search_dirs(dirs=()):
    """Folders searched for template files: ``dirs`` (--template-dir, Slicer cache folders), then the
    SampleData data folder (bundled 2009c brain and mask) and the source-tree fallbacks.
    """
    folders = [os.fspath(folder) for folder in dirs if folder]
    try:
        import importlib.util

        spec = importlib.util.find_spec("SampleData")
        if spec is not None and spec.origin:
            folders.append(os.path.join(os.path.dirname(spec.origin), "Resources", "Data"))
    except (ImportError, ValueError):
        pass
    module_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../LesionNetworkMapping or qt-scripted-modules
    folders.append(os.path.join(os.path.dirname(module_dir), "SampleData", "Resources", "Data"))  # source tree
    folders.append(os.path.join(module_dir, "Resources", "Data"))  # build tree (next to SampleData.py)
    unique = []
    for folder in folders:
        folder = os.path.normpath(os.path.abspath(folder))
        if folder not in unique:
            unique.append(folder)
    return unique


def _verified(path, info):
    """Whether a file has the expected size and sha256 (cached per path, size and mtime)."""
    try:
        if info.get("bytes") and os.path.getsize(path) != info["bytes"]:
            return False
        return not info.get("sha256") or _sha256(path) == info["sha256"]
    except OSError:
        return False


def find_template(template_id, image=None, mask=None, head=None, dirs=()):
    """Locate the files of a template.

    Search order: explicit paths (``image``, ``mask``, ``head``: accepted as given, a warning is
    recorded when the checksum differs from TemplateFlow's); each folder of ``dirs`` (and its
    ``tpl-<id>/`` subfolder); the SampleData data folder; the source-tree fallbacks. Searched files
    must match their TemplateFlow size and sha256.

    :return: {"id", "files": {role: path or None}, "sha256": {role: sha256}, "missing": [roles],
      "warnings": [text], "head_available": bool, "reference_available": bool}.
    :raises jobs.JobError: code "unknown_template" for an unsupported id.
    """
    if is_user_template(template_id):
        return _find_user_template(template_id, image, mask, head, dirs)
    if template_id not in TEMPLATES:
        raise jobs.JobError(f"Unknown template {template_id!r}; supported: {', '.join(TEMPLATE_IDS)} or user:<id>", code="unknown_template")
    spec = TEMPLATES[template_id]
    explicit = {"fixed": image, "mask": mask, "head": head}
    folders = template_search_dirs(dirs)
    files, sha, warnings = {}, {}, []
    for role, info in spec["files"].items():
        found = None
        given = explicit.get(role)
        if given:
            if not os.path.isfile(given):
                raise jobs.JobError(f"Template {role} file not found: {given}", code="template_missing", role=role)
            found = os.path.abspath(given)
            if not _verified(found, info):
                warnings.append(f"The {role} file {os.path.basename(found)} differs from TemplateFlow's {info['name']} (sha256 {info['sha256'][:12]})")
        else:
            for folder in folders:
                for candidate in (os.path.join(folder, info["name"]), os.path.join(folder, f"tpl-{template_id}", info["name"])):
                    if os.path.isfile(candidate) and _verified(candidate, info):
                        found = candidate
                        break
                if found:
                    break
        files[role] = found
        if found:
            sha[role] = info["sha256"] if not given else _sha256(found)
    missing = [role for role in spec["files"] if not files[role]]
    return {
        "id": template_id,
        "files": files,
        "sha256": sha,
        "missing": missing,
        "warnings": warnings,
        "head_available": bool(files.get("head")),
        "reference_available": bool(files.get("reference")),
    }


def _find_user_template(template_id, image=None, mask=None, head=None, dirs=()):
    """:func:`find_template` for "user:<id>": the files of its template.json (explicit files win), their sha256 (a
    warning when a file changed since it was added) and its image grid, registered in :data:`.geometry.GRID_BY_ID`.
    """
    from .geometry import grid_spec_from_affine, register_grid
    from .nifti import read_nifti_header

    record = resolve_user_template(template_id, dirs)
    if record is None:
        raise jobs.JobError(
            f"Template {template_id} is not in the template folders; pass the Templates folder of your Slicer Studio folder with --template-dir",
            code="template_missing",
            template=template_id,
            files=[],
        )
    folder = record["folder"]
    listed = record.get("files") or {}
    stored = record.get("sha256") or {}
    files, sha, warnings = {}, {}, []
    for role, key, given in (("fixed", "image", image), ("mask", "mask", mask), ("head", "head", head)):
        name = listed.get(key)
        path = os.path.abspath(given) if given else (os.path.join(folder, *str(name).split("/")) if name else None)
        if given and not os.path.isfile(path):
            raise jobs.JobError(f"Template {role} file not found: {given}", code="template_missing", role=role)
        if path and os.path.isfile(path):
            files[role] = path
            sha[role] = _sha256(path)
            if not given and stored.get(key) and stored[key] != sha[role]:
                warnings.append(f"The {key} file {os.path.basename(path)} of template {record.get('name')} changed since it was added (sha256 {stored[key][:12]})")
        else:
            files[role] = None
    files["reference"] = files["fixed"]
    if files["fixed"]:
        sha["reference"] = sha["fixed"]
        header = read_nifti_header(files["fixed"]) if _is_nifti(files["fixed"]) else None
        if header is None:
            raise jobs.JobError(f"The image of template {record.get('name')} must be a NIfTI file", code="template_grid", template=template_id)
        spec = grid_spec_from_affine(template_id, header["shape"][:3], header["affine"], record.get("space") or "MNI")
        if spec is None:
            raise jobs.JobError(f"The image of template {record.get('name')} has no grid with cubic voxels aligned with the axes: it cannot be a normalization target",
                                code="template_grid", template=template_id)
        register_grid(spec)
    _USER_TEMPLATES[template_id] = record
    missing = [role for role in ("fixed", "mask", "head") if not files[role]]
    return {
        "id": template_id,
        "files": files,
        "sha256": sha,
        "missing": missing,
        "warnings": warnings,
        "head_available": bool(files.get("head")),
        "reference_available": bool(files.get("reference")),
        "user": {"id": template_id, "name": record.get("name"), "space": record.get("space"), "sha256": stored.get("image") or sha.get("fixed")},
    }


def _require_template(found):
    missing = [role for role in REQUIRED_TEMPLATE_FILES if role in found["missing"]]
    if missing and is_user_template(found["id"]):
        name = (found.get("user") or {}).get("name") or found["id"]
        what = " and ".join({"fixed": "image", "mask": "brain mask"}[role] for role in missing)
        raise jobs.JobError(f"Template {name} has no {what}: add one in Settings > Templates to normalize to it", code="template_missing",
                            template=found["id"], files=[])
    if missing:
        template_id = found["id"]
        details = [{"role": role, "url": template_url(template_id, role), "bytes": TEMPLATES[template_id]["files"][role]["bytes"]} for role in missing]
        names = ", ".join(f"{TEMPLATES[template_id]['files'][role]['name']} ({detail['bytes']:,} bytes, {detail['url']})" for role, detail in zip(missing, details, strict=True))
        raise jobs.JobError(
            f"Template {template_id} is not available: missing {names}. Download it on request (terms: {TEMPLATES[template_id]['terms']}) and pass its folder with --template-dir",
            code="template_missing",
            template=template_id,
            files=details,
        )
    return found


def find_executable(name, explicit=None):
    """Path of a BRAINSTools executable: ``explicit`` (the GUI passes slicer.modules.<m>.path), else
    shutil.which(name) (PythonSlicer's launcher puts Slicer's cli-modules on PATH).

    :raises jobs.JobError: code "executable_missing".
    """
    if explicit:
        if os.path.isfile(explicit):
            return os.path.abspath(explicit)
        raise jobs.JobError(f"{name} not found at {explicit}", code="executable_missing", executable=name)
    found = shutil.which(name)
    if not found:
        raise jobs.JobError(f"{name} was not found on PATH; run with Slicer's PythonSlicer or pass --{name.lower()}", code="executable_missing", executable=name)
    return os.path.abspath(found)


def executable_version(path):
    """Version of a Slicer CLI executable (its XML description: the sibling .xml file, else ``--xml``)."""
    path = os.path.abspath(os.fspath(path))
    try:
        stat = os.stat(path)
        key = (path, stat.st_size, stat.st_mtime_ns)
    except OSError:
        return None
    if key in _VERSION_CACHE:
        return _VERSION_CACHE[key]
    text = ""
    sibling = os.path.splitext(path)[0] + ".xml"
    try:
        with open(sibling, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        try:
            text = subprocess.run([path, "--xml"], capture_output=True, text=True, errors="replace", timeout=60, check=False, **_child_flags()).stdout
        except (OSError, subprocess.SubprocessError):
            text = ""
    match = re.search(r"<version>\s*([^<]+?)\s*</version>", text)
    version = match.group(1) if match else None
    _VERSION_CACHE[key] = version
    return version


def _child_flags():
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if _IS_WINDOWS else {}


# Planning (header-only checks)


def guess_modality(*paths):
    """Modality from BIDS suffixes or file names: (modality, guessed). Unknown names give ("T1w", True)."""
    tables = (
        ("FLAIR", {"flair"}),
        ("T1w", {"t1w", "t1", "mprage", "mp2rage", "spgr", "t1weighted"}),
        ("T2w", {"t2w", "t2", "t2weighted"}),
        ("DWI", {"dwi", "adc", "trace", "b1000"}),
        ("CT", {"ct", "cta", "ncct"}),
    )
    for path in paths:
        if not path:
            continue
        tokens = [token for token in re.split(r"[-_.+\s]+", _nifti_stem(path).lower()) if token]
        if not tokens:
            continue
        for modality, words in tables:  # BIDS suffix first
            if tokens[-1] in words or (modality == "FLAIR" and "flair" in tokens[-1]):
                return modality, False
        for modality, words in tables:
            if any(token in words or (modality == "FLAIR" and "flair" in token) for token in tokens):
                return modality, False
    return "T1w", True


def subject_id(subject=None, lesion=None, scan=None):
    """``<sid>``: --subject, else the sub- entity of the lesion or scan name, else a label from the scan name."""
    from .study import participant_label

    if subject:
        return participant_label(subject)
    for path in (lesion, scan):
        if path:
            match = re.search(r"(?:^|[_/\\])sub-([A-Za-z0-9]+)", os.path.basename(os.fspath(path)))
            if match:
                return f"sub-{match.group(1)}"
    return participant_label(_nifti_stem(scan or lesion or "subject"))


def read_geometry(path):
    """(shape, affine RAS 4x4, dtype name) of a scalar 3D image from its header only.

    NIfTI through nifti.read_nifti_header (its sform and qform must agree, since ITK and numpy readers
    would otherwise place the voxels differently); other formats through SimpleITK.

    :raises jobs.JobError: code "unsupported_scan" for 4D, vector or unreadable images.
    """
    np = _np()
    name = os.path.basename(os.fspath(path))
    if not os.path.isfile(path):
        raise jobs.JobError(f"File not found: {path}", code="file_missing", path=os.fspath(path))
    try:
        if _is_nifti(path):
            from .nifti import XFORM_WARNING_MM, read_nifti_header, xform_disagreement

            header = read_nifti_header(path)
            shape = tuple(int(n) for n in header["shape"])
            if len(shape) == 4 and shape[3] == 1:
                shape = shape[:3]
            if len(shape) != 3:
                raise jobs.JobError(f"{name} is not a 3D image (shape {shape})", code="unsupported_scan", path=os.fspath(path))
            disagreement = xform_disagreement(header)
            if disagreement > XFORM_WARNING_MM:
                raise jobs.JobError(
                    f"The sform and qform of {name} disagree by {disagreement:.3g} mm, so programs place it differently; make them agree first",
                    code="unsupported_scan",
                    path=os.fspath(path),
                )
            return shape, np.asarray(header["affine"], dtype=np.float64), str(header["dtype"].newbyteorder("=").name)
        sitk = _sitk()
        from .transforms import ras_affine

        reader = sitk.ImageFileReader()
        reader.SetFileName(os.fspath(path))
        reader.ReadImageInformation()
        size = reader.GetSize()
        if reader.GetNumberOfComponents() != 1 or not (len(size) == 3 or (len(size) == 4 and size[3] == 1)):
            raise jobs.JobError(f"{name} is not a scalar 3D image", code="unsupported_scan", path=os.fspath(path))
        direction = np.asarray(reader.GetDirection(), dtype=np.float64).reshape(len(size), len(size))[:3, :3]
        affine = ras_affine(reader.GetOrigin()[:3], reader.GetSpacing()[:3], direction)
        dtype = sitk.GetPixelIDValueAsString(reader.GetPixelID())
        return tuple(int(n) for n in size[:3]), affine, dtype
    except jobs.JobError:
        raise
    except Exception as exc:
        raise jobs.JobError(f"Cannot read {name}: {exc}", code="unsupported_scan", path=os.fspath(path)) from None


def read_image(path):
    """(array [i, j, k] float32, affine RAS 4x4) of a scalar 3D image (NIfTI through nifti.py, others through SimpleITK).

    The last image read is cached (by path, size and modification time): plan() and the cost-mask
    stage read the same lesion.
    """
    stat = os.stat(path)
    key = (os.path.abspath(os.fspath(path)), stat.st_size, stat.st_mtime_ns)
    if _IMAGE_CACHE.get("key") == key:
        array, affine = _IMAGE_CACHE["value"]
        return array.copy(), affine.copy()
    array, affine = _read_image(path)
    _IMAGE_CACHE.update(key=key, value=(array, affine))
    return array.copy(), affine.copy()


def _read_image(path):
    np = _np()
    read_geometry(path)  # validates dimensions and the NIfTI xforms
    if _is_nifti(path):
        from .nifti import read_nifti

        data, affine, _ = read_nifti(path)
        return np.asarray(data, dtype=np.float32).reshape(data.shape[:3]), affine
    sitk = _sitk()
    from .transforms import ras_affine

    image = sitk.ReadImage(os.fspath(path))
    size = image.GetSize()
    if len(size) == 4:
        image = sitk.Extract(image, [*size[:3], 0], [0, 0, 0, 0])
    array = sitk.GetArrayFromImage(image).astype(np.float32).transpose(2, 1, 0)
    return array, ras_affine(image.GetOrigin(), image.GetSpacing(), image.GetDirection())


def _lesion_fraction(array):
    """Lesion as fractions 0..1: values in [0, 1] are fractions; label maps (values above 1) count as 1 where non-zero."""
    np = _np()
    array = np.nan_to_num(np.asarray(array, dtype=np.float32))
    if array.size and (array.max() > 1.0 or array.min() < 0.0):
        return (array != 0).astype(np.float32), "labels"
    kind = "binary" if array.size and np.all((array == 0) | (array == 1)) else "fraction"
    return np.clip(array, 0.0, 1.0), kind


def _template_grid_match(shape, affine):
    from .geometry import detect_grid, detect_grid_candidates

    space, _ = detect_grid(shape, affine)
    if space != "native":
        return space
    match = detect_grid_candidates(shape, affine)
    return match.space if match is not None else None


class Plan(dict):
    """Result of :func:`plan` (a dict with attribute access): the checked inputs of one subject."""

    __getattr__ = dict.get


def plan(scan, lesion=None, anat=None, modality="auto", subject=None, force=False, template=DEFAULT_TEMPLATE):
    """Check the inputs of a normalization from their headers (the lesion's voxels are read).

    :raises jobs.JobError: "already_template_space" (exit 4; unless ``force``), "empty_lesion"
      (exit 4), "lesion_outside_scan", "unsupported_scan", "file_missing".
    :return: :class:`Plan` with scan/lesion/anat {path, shape, affine, dtype}, modality,
      modality_guessed, subject, two_step, scan_only, cross_modality, native_ml, lesion_kind, warnings.
    """
    np = _np()
    started = time.monotonic()
    warnings = []
    shape, affine, dtype = read_geometry(scan)
    space = _template_grid_match(shape, affine)
    if space and not force:
        raise jobs.JobError(
            f"{os.path.basename(scan)} is already on the {space} template grid; normalization is for native-space scans (use --force to register it anyway)",
            code="already_template_space",
            exit_code=EXIT_NOTHING_TO_DO,
            space=space,
        )
    if modality in (None, "", "auto"):
        modality, guessed = guess_modality(scan)
        if guessed:
            warnings.append(("modality guessed: T1w", "modality_guessed"))
    else:
        guessed = False
        if modality not in MODALITIES:
            raise jobs.JobError(f"Unknown modality {modality!r}; expected one of {', '.join(MODALITIES)}", code="usage", exit_code=jobs.EXIT_USAGE)
    result = Plan(
        scan={"path": os.path.abspath(scan), "shape": list(shape), "affine": affine.tolist(), "dtype": dtype},
        lesion=None,
        anat=None,
        modality=modality,
        modality_guessed=guessed,
        subject=subject_id(subject, lesion, scan),
        template=template,
        scan_only=lesion is None,
        native_ml=None,
        lesion_kind=None,
    )
    if anat:
        anat_shape, anat_affine, anat_dtype = read_geometry(anat)
        if modality == "T1w":
            warnings.append(("--anat is ignored: the scan is already T1-weighted", "anat_ignored"))
        else:
            result["anat"] = {"path": os.path.abspath(anat), "shape": list(anat_shape), "affine": anat_affine.tolist(), "dtype": anat_dtype}
    result["two_step"] = result["anat"] is not None
    result["cross_modality"] = modality != "T1w"
    if lesion is None:
        warnings.append(("No lesion: scan-only mode, the registration is not lesion-aware", "scan_only"))
    else:
        data, lesion_affine = read_image(lesion)
        fraction, kind = _lesion_fraction(data)
        nonzero = np.argwhere(fraction > 0)
        if not len(nonzero):
            raise jobs.JobError(f"The lesion {os.path.basename(lesion)} is empty", code="empty_lesion", exit_code=EXIT_NOTHING_TO_DO)
        from .geometry import voxel_to_world, world_to_voxel

        # Through the affines: the lesion voxel centres must lie in the scan's field of view
        sample = nonzero[:: max(1, len(nonzero) // 20000)].astype(np.float64)
        ijk = world_to_voxel(voxel_to_world(sample, lesion_affine), affine)
        inside = np.all((ijk > -0.5) & (ijk < np.asarray(shape) - 0.5), axis=1)
        if inside.mean() < 0.5:
            raise jobs.JobError(
                f"Only {100 * inside.mean():.0f}% of the lesion lies inside the scan's field of view: the lesion and the scan do not belong together",
                code="lesion_outside_scan",
            )
        voxel_ml = abs(float(np.linalg.det(lesion_affine[:3, :3]))) / 1000.0
        result["lesion"] = {"path": os.path.abspath(lesion), "shape": list(fraction.shape), "affine": lesion_affine.tolist(), "kind": kind}
        result["lesion_kind"] = kind
        result["native_ml"] = float(fraction.sum()) * voxel_ml
    result["warnings"] = warnings
    result["seconds"] = round(time.monotonic() - started, 3)
    return result


def output_names(sid, modality, template_id, two_step=False):
    """File names of a run's outputs in its --out folder (see the module documentation)."""
    source = "T1w" if two_step else modality
    label, res = template_label(template_id), _res_entity(template_id)
    names = {
        "transform": f"{sid}_from-{modality}_to-{label}_mode-image_xfm.h5",
        "affine": f"{sid}_from-{source}_to-{label}_desc-affine_mode-image_xfm.h5",
        "affine_inverse": f"{sid}_from-{label}_to-{source}_desc-affine_mode-image_xfm.h5",
        "lesion": f"{sid}_space-{label}{res}_desc-lesion_mask.nii.gz",
        "lesion_sidecar": f"{sid}_space-{label}{res}_desc-lesion_mask.json",
        "lesion_prob": f"{sid}_space-{label}{res}_desc-lesionProb_mask.nii.gz",
        "scan": f"{sid}_space-{label}{res}_desc-preproc_{modality}.nii.gz",
        "costmask": f"{sid}_desc-costfunction_mask.nii.gz",
        "qc_png": f"{sid}_desc-normqc.png",
        "qc_json": f"{sid}_desc-normqc.json",
    }
    if two_step:
        names["anat_transform"] = f"{sid}_from-{modality}_to-T1w_mode-image_xfm.h5"
    return names


def method_label(quality, two_step=False, dilate_mm=DEFAULTS["dilate_mm"], scan_only=False):
    """The norm_method column value: brainsfit-cfm:<quality>[:two-step] (brainsfit-nomask without cost-function masking)."""
    tool = "brainsfit-nomask" if scan_only or (dilate_mm is not None and dilate_mm < 0) else "brainsfit-cfm"
    return f"{tool}:{quality}" + (":two-step" if two_step else "")


def read_result(out):
    """The normalization.json of an output folder (a dict), or None."""
    try:
        with open(os.path.join(os.fspath(out), PROVENANCE_JSON), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("format") == FORMAT else None


def is_reusable(out, fingerprint):
    """Whether ``out`` holds a complete result with this fingerprint (every output with its recorded size)."""
    record = read_result(out)
    if not record or record.get("status") != "done" or record.get("fingerprint") != fingerprint:
        return False
    for item in (record.get("outputs") or {}).values():
        path = os.path.join(os.fspath(out), *str(item.get("path", "")).split("/"))
        try:
            if os.path.getsize(path) != item.get("bytes"):
                return False
        except OSError:
            return False
    return bool(record.get("outputs"))


# Child processes and progress


class _Progress:
    """Stage and progress events of one subject (fraction model of the module documentation)."""

    def __init__(self, subject, stages, threads, quality, done_subjects=0, total_subjects=1, expected=None):
        total = sum(STAGE_WEIGHTS[name] for name in stages)
        self.weights = {name: STAGE_WEIGHTS[name] / total for name in stages}
        self.stages = list(stages)
        self.subject = subject
        self.done_subjects = done_subjects
        self.total_subjects = total_subjects
        scale = 3.0 / max(1, threads)
        self.expected = {name: EXPECTED_SECONDS["bspline_fine" if name == "bspline" and quality == "fine" else name] * scale for name in stages}
        self.expected.update(expected or {})  # measured medians of completed subjects (batch)
        self.current = None
        self.fraction = 0.0
        self.completed = []
        self.durations = {}
        self._stage_started = None
        self._last = 0.0

    def stage(self, name, attempt=None):
        if self.current and self.current != name:
            self.finish()
        if self.current != name:
            self.current = name
            self.fraction = 0.0
            self._stage_started = time.monotonic()
        jobs.emit("stage", name=name, index=self.stages.index(name) + 1, total=len(self.stages), subject=self.subject, attempt=attempt)
        self.update(0.0, force=True)

    def finish(self):
        if self.current and self.current not in self.completed:
            self.completed.append(self.current)
            self.durations[self.current] = round(time.monotonic() - self._stage_started, 3)
        self.current = None

    def overall(self):
        done = sum(self.weights[name] for name in self.completed)
        if self.current:
            done += self.weights[self.current] * self.fraction
        return min(done, 1.0)

    def eta(self):
        if self.current is None:
            remaining_stages = [name for name in self.stages if name not in self.completed]
            return sum(self.expected[name] for name in remaining_stages)
        spent = time.monotonic() - self._stage_started
        current = max(self.expected[self.current] - spent, self.expected[self.current] * (1 - self.fraction), 0.0)
        later = self.stages[self.stages.index(self.current) + 1:]
        return current + sum(self.expected[name] for name in later)

    def update(self, fraction, force=False):
        self.fraction = max(self.fraction, min(float(fraction), 1.0))
        now = time.monotonic()
        if not force and now - self._last < PROGRESS_INTERVAL_S:
            return
        self._last = now
        overall = self.overall()
        jobs.emit(
            "progress",
            subject=self.subject,
            stage=self.current,
            fraction=round(overall, 4),
            stage_fraction=round(self.fraction, 4),
            done=self.done_subjects,
            total=self.total_subjects,
            elapsed=round(jobs.elapsed(), 3),
            eta=round(self.eta(), 1),
        )


class _Runner:
    """Runs the BRAINSTools children of one job: logs, cancellation, progress, provenance argv."""

    def __init__(self, cancel, progress, logs_dir, out_dir, threads):
        self.cancel = cancel
        self.progress = progress
        self.logs_dir = logs_dir
        self.out_dir = out_dir
        self.threads = threads
        self.argv = []
        self.peak_rss_mb = None
        self.self_peak_rss_mb = None

    def _portable(self, args):
        portable = [os.path.basename(args[0])]
        for arg in args[1:]:
            portable.append(_rel(arg, self.out_dir) if os.path.isabs(arg) else arg)
        return portable

    def _scrubber(self, args):
        """A function that rewrites absolute local paths in a log line: the logs sit in a shareable study folder (derivatives/normalization/<sid>/logs), so they hold paths relative to ``out_dir`` like the argv of normalization.json."""
        swaps = {}
        exe_dir = os.path.dirname(args[0])
        if exe_dir:
            swaps[exe_dir + os.sep] = ""
        for arg in args[1:]:
            if os.path.isabs(arg):
                swaps[arg] = _rel(arg, self.out_dir)
        swaps[os.path.abspath(self.out_dir) + os.sep] = ""
        out_abs = os.path.abspath(self.out_dir)
        swaps[os.path.abspath(self.logs_dir) + os.sep] = ""
        ordered = sorted(swaps.items(), key=lambda item: -len(item[0]))

        out_pattern = re.compile(re.escape(out_abs) + r"(?![\w.-])")

        def scrub(line):
            for old, new in ordered:
                if old and old in line:
                    line = line.replace(old, new)
            return out_pattern.sub(".", line)

        return scrub

    def run(self, exe, options, log_name, expected_s):
        """Run ``exe`` with --key value options (True: flag; tuples: comma-joined); return the elapsed seconds."""
        args = [exe]
        for key, value in options.items():
            if value is None or value is False:
                continue
            if value is True:
                args.append(f"--{key}")
            elif isinstance(value, (list, tuple)):
                args += [f"--{key}", ",".join(str(v) for v in value)]
            else:
                args += [f"--{key}", str(value)]
        self.argv.append(self._portable(args))
        log_path = os.path.join(self.logs_dir, log_name)
        os.makedirs(self.logs_dir, exist_ok=True)
        done_stages = len(self.progress.completed) if self.progress is not None else 0
        self.cancel.raise_if_cancelled(done=done_stages)
        started = time.monotonic()
        floor = {"value": 0.0}
        scrub = self._scrubber(args)
        with open(log_path, "w", encoding="utf-8", errors="replace") as log:
            log.write(scrub(" ".join([os.path.basename(args[0])] + args[1:])) + "\n")
            log.flush()
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, errors="replace", **_child_flags())

            def read():
                for line in process.stdout:
                    log.write(scrub(line))
                    match = _STAGE_LINE.search(line)
                    if match:
                        if match.group(1) and match.group(3) == "2":
                            floor["value"] = 0.0 if match.group(2) == "1" else 0.5
                        elif not match.group(1):
                            floor["value"] = 0.0

            reader = threading.Thread(target=read, name="lnm-normalize-child-log", daemon=True)
            reader.start()
            try:
                while process.poll() is None:
                    if self.cancel.cancelled():
                        self._stop(process)
                        reader.join(timeout=2)
                        raise jobs.Cancelled(done=done_stages, reason=self.cancel.reason)
                    if self.progress is not None:
                        elapsed = time.monotonic() - started
                        self.progress.update(max(floor["value"], min(0.95, 1.0 - math.exp(-elapsed / max(expected_s, 0.1)))))
                    time.sleep(POLL_S)
            except BaseException:
                if process.poll() is None:
                    self._stop(process)
                raise
            reader.join(timeout=10)
        self._record_rss()
        if process.returncode != 0:
            raise jobs.JobError(
                f"{os.path.basename(exe)} failed with exit code {process.returncode}; see {log_name}",
                code="child_failed",
                executable=os.path.basename(exe),
                log=log_path,
                tail=_tail(log_path),
            )
        return round(time.monotonic() - started, 3)

    @staticmethod
    def _stop(process):
        try:
            process.terminate()
            process.wait(timeout=TERMINATE_WAIT_S)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=TERMINATE_WAIT_S)
        except OSError:
            pass

    def _record_rss(self):
        try:
            import resource

            unit = 1024 * 1024 if sys.platform == "darwin" else 1024
            # children's peak includes this process's pages shared at fork time; this process's own peak is recorded too
            self.peak_rss_mb = round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / unit)
            self.self_peak_rss_mb = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / unit)
        except (ImportError, AttributeError, OSError):
            pass


def _tail(path, lines=12):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return [line.rstrip("\n") for line in f.readlines()[-lines:]]
    except OSError:
        return []


# Image and transform helpers (numpy / SimpleITK, imported lazily)


def _dilate(mask, affine, mm):
    """Dilate a boolean mask by ``mm`` (Euclidean, voxel spacing aware) inside its bounding box plus margin."""
    np = _np()
    from scipy import ndimage

    if mm <= 0 or not mask.any():
        return mask.astype(bool)
    spacing = np.sqrt((np.asarray(affine)[:3, :3] ** 2).sum(axis=0))
    margin = np.ceil(mm / spacing).astype(int) + 1
    nonzero = np.argwhere(mask)
    lo = np.maximum(nonzero.min(0) - margin, 0)
    hi = np.minimum(nonzero.max(0) + margin + 1, mask.shape)
    crop = mask[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]].astype(bool)
    distance = ndimage.distance_transform_edt(~crop, sampling=spacing)
    out = np.zeros(mask.shape, dtype=bool)
    out[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]] = distance <= mm
    return out


def _crop(array, affine, margin=1):
    """(the non-zero bounding box of ``array`` plus ``margin`` voxels, its affine); a 1-voxel array when empty."""
    np = _np()
    nonzero = np.argwhere(array != 0)
    if not len(nonzero):
        lo, hi = np.zeros(3, dtype=int), np.ones(3, dtype=int)
    else:
        lo = np.maximum(nonzero.min(0) - margin, 0)
        hi = np.minimum(nonzero.max(0) + margin + 1, array.shape)
    shifted = np.asarray(affine, dtype=np.float64).copy()
    shifted[:3, 3] = affine[:3, :3] @ lo + affine[:3, 3]
    return array[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]], shifted


def _to_grid(fraction, src_affine, shape, affine):
    """A lesion fraction on another grid: copied when the grids are identical, else fraction resampling."""
    np = _np()
    from .geometry import Grid, resample_to_grid

    if tuple(fraction.shape) == tuple(shape) and np.allclose(src_affine, affine, atol=1e-4):
        return np.asarray(fraction, dtype=np.float32)
    return resample_to_grid(fraction, src_affine, Grid(shape, affine), mode="fraction").astype(np.float32)


def _array_ijk(image):
    np = _np()
    return _sitk().GetArrayFromImage(image).transpose(2, 1, 0).astype(np.float32)


def _read_float(path):
    """(array [i, j, k] float32, affine) of a NIfTI written by a BRAINSTools child."""
    np = _np()
    from .nifti import read_nifti

    data, affine, _ = read_nifti(path)
    return np.asarray(data, dtype=np.float32).reshape(data.shape[:3]), affine


def _matrix_ras(transform):
    """4x4 RAS matrix of a linear SimpleITK transform (it maps the same points, in RAS instead of LPS)."""
    np = _np()
    matrix = np.asarray(transform.GetMatrix(), dtype=np.float64).reshape(3, 3)
    center = np.asarray(transform.GetCenter(), dtype=np.float64)
    offset = np.asarray(transform.GetTranslation(), dtype=np.float64) + center - matrix @ center
    flip = np.diag([-1.0, -1.0, 1.0])
    out = np.eye(4)
    out[:3, :3] = flip @ matrix @ flip
    out[:3, 3] = flip @ offset
    return out


def _linear_matrix(path):
    """4x4 RAS matrix of the affine (first linear) part of a transform file (template -> moving points)."""
    sitk = _sitk()
    from .transforms import read_transform

    transform = read_transform(path)
    if transform.GetName() == "CompositeTransform":
        transform = sitk.CompositeTransform(transform).GetNthTransform(0)
    return _matrix_ras(transform)


def _rigid_from(transform):
    """A VersorRigid3DTransform copy of a rigid transform read from BRAINSFit."""
    sitk = _sitk()
    if transform.GetName() == "CompositeTransform":
        transform = sitk.CompositeTransform(transform).GetNthTransform(0)
    if transform.GetName() == "VersorRigid3DTransform":
        return sitk.VersorRigid3DTransform(transform)
    rigid = sitk.VersorRigid3DTransform()
    rigid.SetCenter(transform.GetCenter())
    rigid.SetMatrix(transform.GetMatrix())
    rigid.SetTranslation(transform.GetTranslation())
    return rigid


def _offset_start(base, pitch_deg, yaw_deg):
    """``base`` composed with a rotation about its centre in template space (pitch about x, yaw about z)."""
    np = _np()
    sitk = _sitk()
    center = base.GetCenter()
    euler = sitk.Euler3DTransform()
    euler.SetCenter(center)
    euler.SetRotation(math.radians(pitch_deg), 0.0, math.radians(yaw_deg))
    matrix = np.asarray(base.GetMatrix()).reshape(3, 3) @ np.asarray(euler.GetMatrix()).reshape(3, 3)
    # BRAINSFit rejects Euler3DTransform initial transforms ("Invalid type"): write a VersorRigid3DTransform
    start = sitk.VersorRigid3DTransform()
    start.SetCenter(center)
    start.SetMatrix([float(v) for v in matrix.ravel()])
    start.SetTranslation(base.GetTranslation())
    return start


def _report_metric(path):
    """MetricValue of a BRAINSFit --logFileReport CSV (last row), or None."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            rows = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        return float(rows[-1].split(",")[1]) if rows else None
    except (OSError, ValueError, IndexError):
        return None


# Normalization of one subject


class _Context:
    """State of one subject's run (paths, options, intermediate results)."""

    def __init__(self, **values):
        self.__dict__.update(values)


def _fingerprint(ctx):
    from .export import LIBRARY_VERSION

    payload = {
        "normalize_version": NORMALIZE_VERSION,
        "library_version": LIBRARY_VERSION,
        "tools": {"BRAINSFit": ctx.versions.get("BRAINSFit"), "BRAINSResample": ctx.versions.get("BRAINSResample")},
        "inputs": {
            "scan": _sha256(ctx.scan),
            "lesion": _sha256(ctx.lesion) if ctx.lesion else None,
            "anat": _sha256(ctx.anat) if ctx.anat else None,
            "initial_transform": _sha256(ctx.initial_transform) if ctx.initial_transform else None,
        },
        "template": {"id": ctx.template_id, "sha256": {role: ctx.template["sha256"].get(role) for role in ("fixed", "mask")}},
        "method": ctx.method_options,
        "subject": ctx.sid,
    }
    if "BRAINSROIAuto" in ctx.versions:  # only runs that use a head mask (results without one keep their fingerprint)
        payload["tools"]["BRAINSROIAuto"] = ctx.versions["BRAINSROIAuto"]
    if ctx.tilt_search == "always":
        payload["template"]["sha256"]["head"] = ctx.template["sha256"].get("head")
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _stages_for(two_step, bspline):
    stages = ["costmask"]
    if two_step:
        stages.append("anat")
    stages.append("affine")
    if bspline:
        stages.append("bspline")
    stages += ["warp", "qc"]
    return stages


def normalize(
    scan,
    lesion,
    out,
    subject=None,
    template="auto",
    template_image=None,
    template_mask=None,
    template_head=None,
    template_dirs=(),
    modality="auto",
    anat=None,
    quality=DEFAULTS["quality"],
    dilate_mm=DEFAULTS["dilate_mm"],
    moving_mask="auto",
    initial_transform=None,
    tilt_search="auto",
    threads=None,
    brainsfit=None,
    brainsresample=None,
    brainsroiauto=None,
    reuse=True,
    force=False,
    keep_work=False,
    cancel=None,
    batch_progress=None,
    **advanced,
):
    """Normalize one subject (see the module documentation); return the result dict.

    :param advanced: affine_sampling, bspline_sampling, min_step, bspline_metric, binarize,
      accept_ncc, tilt_offsets (defaults in :data:`DEFAULTS`).
    :param batch_progress: (done subjects, total subjects, {stage: expected seconds}) in a batch.
    :return: {"status": "done" | "reused", "out", "subject", "template", "modality", "quality",
      "two_step", "fingerprint", "outputs": {key: absolute path}, "qc": {...}, "method_label",
      "volume_ml", "volume_native_ml", "timings", "attempts", "warnings"}.
    :raises jobs.JobError, jobs.Locked, jobs.Cancelled.
    """
    unknown = set(advanced) - set(DEFAULTS)
    if unknown:
        raise TypeError(f"Unknown options: {', '.join(sorted(unknown))}")
    options = {key: advanced.get(key, DEFAULTS[key]) for key in DEFAULTS if key not in ("quality", "dilate_mm", "template")}
    if quality not in QUALITY:
        raise jobs.JobError(f"Unknown quality {quality!r}", code="usage", exit_code=jobs.EXIT_USAGE)
    if moving_mask not in ("auto", "none", "head"):
        raise jobs.JobError(f"Unknown moving mask {moving_mask!r}", code="usage", exit_code=jobs.EXIT_USAGE)
    if tilt_search not in ("auto", "always", "off"):
        raise jobs.JobError(f"Unknown tilt search {tilt_search!r}", code="usage", exit_code=jobs.EXIT_USAGE)
    cancel = cancel or jobs.CancelToken()
    started = time.monotonic()
    template_id = DEFAULT_TEMPLATE if template in (None, "", "auto") else template
    threads = jobs.budget_threads(threads)
    out = os.path.abspath(os.fspath(out))
    scan = os.path.abspath(os.fspath(scan))
    lesion = os.path.abspath(os.fspath(lesion)) if lesion else None
    anat = os.path.abspath(os.fspath(anat)) if anat else None
    initial_transform = os.path.abspath(initial_transform) if initial_transform else None
    for path, what in ((scan, "scan"), (lesion, "lesion"), (anat, "anat"), (initial_transform, "initial transform")):
        if path and not os.path.isfile(path):
            raise jobs.JobError(f"The {what} file does not exist: {path}", code="file_missing", path=path)

    exes = {"BRAINSFit": find_executable("BRAINSFit", brainsfit), "BRAINSResample": find_executable("BRAINSResample", brainsresample)}
    versions = {name: executable_version(path) for name, path in exes.items()}
    found = _require_template(find_template(template_id, template_image, template_mask, template_head, template_dirs))
    for text in found["warnings"]:
        _warn(text, "template_unverified", subject=subject)

    # Modality and two-step decisions only need the names (reuse is decided before numpy is imported)
    if modality in (None, "", "auto"):
        effective_modality, guessed = guess_modality(scan)
    else:
        effective_modality, guessed = modality, False
    if moving_mask == "head" or (moving_mask == "auto" and effective_modality == "CT"):
        # the head mask enters the result: its tool version belongs in the fingerprint
        exes["BRAINSROIAuto"] = find_executable("BRAINSROIAuto", brainsroiauto)
        versions["BRAINSROIAuto"] = executable_version(exes["BRAINSROIAuto"])
    two_step = bool(anat) and effective_modality != "T1w"
    cross_modality = effective_modality != "T1w"
    effective_quality = quality
    if cross_modality and not two_step and QUALITY[quality]["bspline"]:
        effective_quality = "affine"
    sid = subject_id(subject, lesion, scan)
    method_options = {
        "quality": effective_quality,
        "grid": QUALITY[effective_quality]["grid"],
        "dilate_mm": float(dilate_mm),
        "moving_mask": moving_mask,
        "modality": effective_modality,
        "two_step": two_step,
        "tilt_search": tilt_search,
        **{key: (list(value) if isinstance(value, tuple) else value) for key, value in options.items()},
    }
    method_options["tilt_offsets"] = [list(offset) for offset in options["tilt_offsets"]]
    ctx = _Context(
        scan=scan,
        lesion=lesion,
        anat=anat,
        out=out,
        sid=sid,
        template_id=template_id,
        template=found,
        tilt_search=tilt_search,
        initial_transform=initial_transform,
        versions=versions,
        method_options=method_options,
        options=options,
        quality=effective_quality,
        requested_quality=quality,
        modality=effective_modality,
        modality_guessed=guessed,
        two_step=two_step,
        cross_modality=cross_modality,
        dilate_mm=float(dilate_mm),
        moving_mask=moving_mask,
        threads=threads,
        exes=exes,
        cancel=cancel,
        warnings=[],
        attempts=[],
        timings={},
        keep_work=keep_work,
        brainsroiauto=brainsroiauto,
    )

    os.makedirs(out, exist_ok=True)
    try:
        free = shutil.disk_usage(out).free
    except OSError:
        free = None
    if free is not None and free < 3 * SUBJECT_DISK_BYTES + WORK_DISK_BYTES:
        raise jobs.JobError(f"Not enough free disk space in {out} ({free / 1e6:.0f} MB)", code="disk_full")

    lock = jobs.OutputLock(os.path.join(out, LOCK_NAME))
    lock.acquire()
    try:
        if lock.reclaimed is not None:
            _warn(f"Reclaimed a stale lock of process {lock.reclaimed.get('pid')} on {lock.reclaimed.get('host')}", "stale_lock", subject=sid)
            _remove_stale_work(out)
        ctx.fingerprint = _fingerprint(ctx)
        if reuse and is_reusable(out, ctx.fingerprint):
            record = read_result(out)
            result = _result_from_record(record, out, "reused")
            if guessed and not any(item.get("code") == "modality_guessed" for item in result["warnings"]):
                message = f"modality guessed: {effective_modality}"
                result["warnings"].append({"message": message, "code": "modality_guessed"})
                _warn(message, "modality_guessed", subject=sid)
            jobs.emit("result", kind="normalization", status="reused", subject=sid, path=os.path.join(out, PROVENANCE_JSON), fingerprint=ctx.fingerprint, suggestion=result["qc"].get("suggestion"))
            return result
        return _run(ctx, force, batch_progress, started)
    finally:
        lock.release()


def _remove_stale_work(out):
    """Remove the .work-* folders of a job that died holding the lock (the lock proves no job writes there now)."""
    for name in os.listdir(out):
        path = os.path.join(out, name)
        if name.startswith(".work-") and os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)


def _result_from_record(record, out, status):
    outputs = {key: os.path.join(out, *item["path"].split("/")) for key, item in (record.get("outputs") or {}).items()}
    qc = record.get("qc") or {}
    metrics = qc.get("metrics") or {}
    method = record.get("method") or {}
    return {
        "status": status,
        "out": out,
        "subject": record.get("subject"),
        "template": (record.get("template") or {}).get("id"),
        "modality": method.get("modality"),
        "quality": method.get("quality"),
        "two_step": method.get("two_step"),
        "fingerprint": record.get("fingerprint"),
        "outputs": outputs,
        "qc": qc,
        "method_label": method.get("label"),
        "volume_ml": metrics.get("mni_ml"),
        "volume_native_ml": metrics.get("native_ml"),
        "timings": record.get("timings"),
        "attempts": method.get("attempts"),
        "warnings": record.get("warnings") or [],
    }


def _run(ctx, force, batch_progress, started):
    ctx.plan = plan(ctx.scan, ctx.lesion, ctx.anat, ctx.modality, ctx.sid, force=force, template=ctx.template_id)
    for message, code in ctx.plan["warnings"]:
        ctx.warnings.append({"message": message, "code": code})
        _warn(message, code, subject=ctx.sid)
    if ctx.modality_guessed:  # normalize() resolved the modality before plan(), which therefore cannot warn
        message = f"modality guessed: {ctx.modality}"
        ctx.warnings.append({"message": message, "code": "modality_guessed"})
        _warn(message, "modality_guessed", subject=ctx.sid)
    ctx.scan_only = ctx.plan["scan_only"]
    if ctx.cross_modality and not ctx.two_step:
        message = "cross-modality registration to a T1 template is unreliable (measured 5-9 mm); give the same subject's T1w (--anat). Experimental: affine only, always review"
        ctx.warnings.append({"message": message, "code": "cross_modality"})
        _warn(message, "cross_modality", subject=ctx.sid)
    if ctx.dilate_mm < 0 and not ctx.scan_only:
        message = "cost-function masking is disabled (--dilate-mm -1): diagnostics only, the lesion may be distorted"
        ctx.warnings.append({"message": message, "code": "masking_disabled"})
        _warn(message, "masking_disabled", subject=ctx.sid)
    bspline = QUALITY[ctx.quality]["bspline"]
    stages = _stages_for(ctx.two_step, bspline)
    done_subjects, total_subjects, expected = batch_progress or (0, 1, None)
    ctx.progress = _Progress(ctx.sid, stages, ctx.threads, ctx.quality, done_subjects, total_subjects, expected)
    ctx.work = os.path.join(ctx.out, f".work-{os.getpid()}")
    shutil.rmtree(ctx.work, ignore_errors=True)
    os.makedirs(ctx.work)
    ctx.logs = os.path.join(ctx.work, "logs")
    ctx.final = os.path.join(ctx.work, "final")
    os.makedirs(ctx.final)
    ctx.names = output_names(ctx.sid, ctx.modality, ctx.template_id, ctx.two_step)
    ctx.runner = _Runner(ctx.cancel, ctx.progress, ctx.logs, ctx.out, ctx.threads)
    ctx.grid1 = template_grid(ctx.template_id)
    status = "error"
    error = None
    failure = None
    try:
        ctx.cancel.raise_if_cancelled(done=0)
        _stage_costmask(ctx)
        if ctx.two_step:
            _stage_anat(ctx)
        _stage_affine(ctx)
        if bspline:
            _stage_bspline(ctx)
        _stage_warp(ctx)
        _stage_qc(ctx)
        ctx.cancel.raise_if_cancelled(done=len(ctx.progress.completed))
        result = _commit(ctx, started)
        status = "done"
        return result
    except jobs.Cancelled:
        status = "cancelled"
        raise
    except jobs.JobError as exc:
        error = {"message": exc.message, "code": exc.code}
        failure = exc
        raise
    except Exception as exc:
        error = {"message": str(exc) or type(exc).__name__, "code": "exception"}
        raise
    finally:
        if status != "done":
            _record_failure(ctx, status, error, started, failure)
        if not ctx.keep_work:
            shutil.rmtree(ctx.work, ignore_errors=True)


def _point_error_to_kept_log(ctx, exc, error, logs_out):
    """Make a child failure name the log that was kept (``<out>/logs/last-error``), not the one in the work folder."""
    log = exc.fields.get("log")
    if not log or os.path.dirname(os.path.abspath(log)) != os.path.abspath(ctx.logs):
        return
    name = os.path.basename(log)
    kept = os.path.join(logs_out, name)
    if not os.path.isfile(kept):
        return
    relative = "/".join(os.path.relpath(kept, ctx.out).split(os.sep))
    exc.fields["log"] = kept
    for holder, key in ((exc, "message"), (error, "message")):
        text = holder.get(key) if isinstance(holder, dict) else getattr(holder, key, None)
        if isinstance(text, str) and text.endswith(f"see {name}"):
            text = text[: -len(name)] + relative
            if isinstance(holder, dict):
                holder[key] = text
            else:
                setattr(holder, key, text)
                holder.args = (text,)


def _record_failure(ctx, status, error, started, exc=None):
    """Keep the logs of a failed run and record its status without touching a previous complete result.

    The logs of an error are copied to ``<out>/logs/last-error`` and ``exc`` (a JobError) is made to name them there.
    """
    try:
        logs_out = os.path.join(ctx.out, "logs", f"last-{status}")
        if status == "error" and os.path.isdir(ctx.logs):
            shutil.rmtree(logs_out, ignore_errors=True)
            shutil.copytree(ctx.logs, logs_out)
            if exc is not None and error is not None:
                _point_error_to_kept_log(ctx, exc, error, logs_out)
        record = {
            "format": FORMAT,
            "version": NORMALIZE_VERSION,
            "status": status,
            "fingerprint": getattr(ctx, "fingerprint", None),
            "created": _now(),
            "subject": ctx.sid,
            "error": error,
            "attempts": ctx.attempts,
            "warnings": ctx.warnings,
            "seconds": round(time.monotonic() - started, 3),
        }
        previous = read_result(ctx.out)
        if previous is None or previous.get("status") != "done":
            from .export import write_json_atomic

            write_json_atomic(os.path.join(ctx.out, PROVENANCE_JSON), _plain(record))
    except Exception:  # never mask the original failure
        pass


def _stage_costmask(ctx):
    """Cost-function mask on the moving image's grid (the scan, or the T1w with --anat: built after the anat stage)."""
    np = _np()
    from .nifti import XFORM_SCANNER, write_nifti

    ctx.progress.stage("costmask")
    t0 = time.monotonic()
    shape, affine = tuple(ctx.plan["scan"]["shape"]), np.asarray(ctx.plan["scan"]["affine"])
    ctx.native_affine = affine
    if not ctx.scan_only:
        data, lesion_affine = read_image(ctx.lesion)
        fraction, _ = _lesion_fraction(data)
        ctx.native_fraction = fraction
        ctx.native_lesion_affine = lesion_affine
        # the native lesion as float32 for BRAINSResample (its own grid; voxels are placed through the affines)
        ctx.lesion_float = os.path.join(ctx.work, "lesion_float.nii")
        write_nifti(ctx.lesion_float, fraction.astype(np.float32), lesion_affine, sform_code=XFORM_SCANNER, qform_code=XFORM_SCANNER)
    if not ctx.two_step:
        _write_costmask(ctx, shape, affine, None if ctx.scan_only else _to_grid(ctx.native_fraction, ctx.native_lesion_affine, shape, affine), ctx.scan)
    ctx.timings["costmask"] = round(time.monotonic() - t0, 3)
    ctx.progress.finish()


def _write_costmask(ctx, shape, affine, lesion_on_grid, moving_path):
    """Write the cost mask (uint8, 1 = used by the metric) on a grid: NOT(dilated lesion), optionally AND a head mask."""
    np = _np()
    from .nifti import XFORM_SCANNER, write_nifti

    keep = np.ones(shape, dtype=bool)
    ctx.excluded_ml = 0.0
    if lesion_on_grid is not None and ctx.dilate_mm >= 0:
        lesion_mask = lesion_on_grid >= 0.5
        if not lesion_mask.any():
            lesion_mask = lesion_on_grid > 0  # a lesion smaller than one scan voxel
        excluded = _dilate(lesion_mask, affine, ctx.dilate_mm)
        keep &= ~excluded
        ctx.excluded_ml = float(excluded.sum()) * abs(float(np.linalg.det(affine[:3, :3]))) / 1000.0
    use_head = ctx.moving_mask == "head" or (ctx.moving_mask == "auto" and ctx.modality == "CT")
    if use_head:
        exe = ctx.exes.get("BRAINSROIAuto") or find_executable("BRAINSROIAuto", ctx.brainsroiauto)
        ctx.versions.setdefault("BRAINSROIAuto", executable_version(exe))
        head_path = os.path.join(ctx.work, "head_mask.nii")
        ctx.runner.run(exe, {"inputVolume": moving_path, "outputROIMaskVolume": head_path, "ROIAutoDilateSize": 3, "numberOfThreads": ctx.threads}, "roiauto.log", 6.0)
        head, head_affine = _read_float(head_path)
        keep &= _to_grid((head > 0).astype(np.float32), head_affine, shape, affine) >= 0.5
    ctx.costmask = os.path.join(ctx.work, "costmask.nii")
    write_nifti(ctx.costmask, keep.astype(np.uint8), affine, sform_code=XFORM_SCANNER, qform_code=XFORM_SCANNER)
    write_nifti(os.path.join(ctx.final, ctx.names["costmask"]), keep.astype(np.uint8), affine, sform_code=XFORM_SCANNER, qform_code=XFORM_SCANNER)
    ctx.moving = moving_path
    ctx.moving_grid = (shape, affine)


def _stage_anat(ctx):
    """Two-step path: rigid scan -> same-subject T1w; the template stages then register the T1w."""
    np = _np()
    ctx.progress.stage("anat")
    t0 = time.monotonic()
    rigid = os.path.join(ctx.work, "anat_rigid.h5")
    ctx.runner.run(
        ctx.exes["BRAINSFit"],
        {
            "fixedVolume": ctx.anat,
            "movingVolume": ctx.scan,
            "initializeTransformMode": "useGeometryAlign",
            "useRigid": True,
            "costMetric": "MMI",
            "samplingPercentage": ctx.options["affine_sampling"],
            "linearTransform": rigid,
            "numberOfThreads": ctx.threads,
        },
        "anat.log",
        ctx.progress.expected["anat"],
    )
    ctx.anat_rigid = rigid
    shape, affine = tuple(ctx.plan["anat"]["shape"]), np.asarray(ctx.plan["anat"]["affine"])
    lesion_on_anat = None
    if not ctx.scan_only:
        # the lesion on the T1w grid through the rigid transform (T1w points -> scan points)
        resampled = os.path.join(ctx.work, "lesion_on_anat.nii")
        ctx.runner.run(
            ctx.exes["BRAINSResample"],
            {"inputVolume": ctx.lesion_float, "referenceVolume": ctx.anat, "outputVolume": resampled, "pixelType": "float", "warpTransform": rigid, "interpolationMode": "NearestNeighbor", "numberOfThreads": ctx.threads},
            "anat_lesion.log",
            2.0,
        )
        lesion_on_anat, resampled_affine = _read_float(resampled)
        lesion_on_anat = _to_grid(np.clip(lesion_on_anat, 0, 1), resampled_affine, shape, affine)
    _write_costmask(ctx, shape, affine, lesion_on_anat, ctx.anat)
    ctx.timings["anat"] = round(time.monotonic() - t0, 3)
    ctx.progress.finish()


def _moving_lesion_image(ctx):
    """The lesion fraction in the moving image's space, as a SimpleITK image (for the acceptance check)."""
    if ctx.scan_only:
        return None
    sitk = _sitk()
    if getattr(ctx, "_moving_lesion", None) is None:
        if ctx.two_step:
            from .nifti import XFORM_SCANNER, write_nifti

            path = os.path.join(ctx.work, "lesion_moving.nii")
            shape, affine = ctx.moving_grid
            fraction = _read_float(os.path.join(ctx.work, "lesion_on_anat.nii"))
            write_nifti(path, _to_grid(_np().clip(fraction[0], 0, 1), fraction[1], shape, affine), affine, sform_code=XFORM_SCANNER, qform_code=XFORM_SCANNER)
            ctx._moving_lesion = sitk.ReadImage(path)
        else:
            ctx._moving_lesion = sitk.ReadImage(ctx.lesion_float)
    return ctx._moving_lesion


def _template_images(ctx):
    """(fixed array, mask bool, affine, SimpleITK fixed image) of the 2 mm template, cached."""
    if getattr(ctx, "_template_images", None) is None:
        fixed, affine = _read_float(ctx.template["files"]["fixed"])
        mask, mask_affine = _read_float(ctx.template["files"]["mask"])
        if mask.shape != fixed.shape or not _np().allclose(mask_affine, affine, atol=1e-3):
            raise jobs.JobError("The template image and brain mask are not on the same grid", code="template_mismatch")
        ctx._template_images = (fixed, mask >= 0.5, affine, _sitk().ReadImage(ctx.template["files"]["fixed"]))
    return ctx._template_images


def _evaluate_candidate(ctx, transform_path, warped_path):
    """ncc_xl of a candidate transform: NCC inside the template brain minus the dilated warped lesion (2 mm grid)."""
    np = _np()
    sitk = _sitk()
    from . import normqc
    from .transforms import read_transform

    fixed, mask, _, fixed_image = _template_images(ctx)
    warped, _ = _read_float(warped_path)
    lesion = None
    moving_lesion = _moving_lesion_image(ctx)
    if moving_lesion is not None:
        resampled = sitk.Resample(moving_lesion, fixed_image, read_transform(transform_path), sitk.sitkLinear, 0.0, sitk.sitkFloat32)
        lesion = np.clip(_array_ijk(resampled), 0.0, 1.0)
    return normqc.ncc_xl(fixed, warped, mask, lesion), lesion


def _affine_attempt(ctx, name, initial=None, mode="useCenterOfHeadAlign"):
    """One affine registration (A1 settings) from an initialization; returns its attempt record."""
    t0 = time.monotonic()
    transform = os.path.join(ctx.work, f"affine_{name}.h5")
    warped = os.path.join(ctx.work, f"warped_affine_{name}.nii")
    options = {
        "fixedVolume": ctx.template["files"]["fixed"],
        "fixedBinaryVolume": ctx.template["files"]["mask"],
        "movingVolume": ctx.moving,
        "movingBinaryVolume": ctx.costmask,
        "maskProcessingMode": "ROI",
        "initializeTransformMode": "Off" if initial else mode,
        "initialTransform": initial,
        "useRigid": True,
        "useAffine": True,
        "costMetric": "MMI",
        "samplingPercentage": ctx.options["affine_sampling"],
        "minimumStepLength": tuple(ctx.options["min_step"]),
        "numberOfIterations": tuple(ctx.options["max_iterations"]),
        # an attempt that reaches the iteration cap is judged by ncc_xl like any other
        "failureExitCode": 0,
        "writeTransformOnFailure": True,
        "linearTransform": transform,
        "outputVolume": warped,
        "numberOfThreads": ctx.threads,
    }
    ctx.runner.run(ctx.exes["BRAINSFit"], options, f"affine_{name}.log", ctx.progress.expected["affine"])
    ncc, _ = _evaluate_candidate(ctx, transform, warped)
    accepted = _accept(ctx, ncc)
    record = {"name": name, "ncc_xl": None if not math.isfinite(ncc) else round(ncc, 4), "accepted": accepted, "seconds": round(time.monotonic() - t0, 2)}
    ctx.attempts.append(record)
    if accepted:
        ctx.affine = transform
        ctx.affine_warped = warped
        ctx.affine_ncc = ncc
    return record


def _accept(ctx, ncc):
    if ctx.cross_modality and not ctx.two_step:
        # NCC against a T1 template is not meaningful for another contrast (FLAIR-like: 0.07-0.21 when aligned):
        # the first attempt is kept and the suggestion is always "review"
        return True
    return math.isfinite(ncc) and ncc >= ctx.options["accept_ncc"]


def _head_rigid(ctx, name, initial=None):
    """Head-to-head rigid registration against the whole-head template; returns (transform path, MMI metric)."""
    transform = os.path.join(ctx.work, f"{name}.h5")
    report = os.path.join(ctx.work, f"{name}.csv")
    ctx.runner.run(
        ctx.exes["BRAINSFit"],
        {
            "fixedVolume": ctx.template["files"]["head"],
            "movingVolume": ctx.moving,
            "initializeTransformMode": "Off" if initial else "useCenterOfHeadAlign",
            "initialTransform": initial,
            "useRigid": True,
            "costMetric": "MMI",
            "samplingPercentage": 0.02,
            "minimumStepLength": ctx.options["min_step"][0],
            "numberOfIterations": ctx.options["max_iterations"][0],
            "failureExitCode": 0,
            "writeTransformOnFailure": True,
            "linearTransform": transform,
            "logFileReport": report,
            "numberOfThreads": ctx.threads,
        },
        f"{name}.log",
        EXPECTED_SECONDS["head_rigid"] * 3.0 / max(1, ctx.threads),
    )
    return transform, _report_metric(report)


def _stage_affine(ctx):
    """The attempt ladder A1 (or A4) -> A2 (head rigid) -> A3 (multi-start), accepted on ncc_xl."""
    sitk = _sitk()
    ctx.progress.stage("affine", attempt=1)
    t0 = time.monotonic()
    ctx.affine = None
    head = ctx.template["files"].get("head")
    tilt = ctx.tilt_search != "off" and head is not None
    if ctx.tilt_search != "always" or not head:
        name = "A4" if ctx.initial_transform else "A1"
        _affine_attempt(ctx, name, initial=ctx.initial_transform)
    if ctx.affine is None and tilt:
        ctx.progress.stage("affine", attempt=len(ctx.attempts) + 1)
        rigid, metric = _head_rigid(ctx, "head_rigid")  # its own centre-of-head start, also after a failed --initial-transform
        record = _affine_attempt(ctx, "A2", initial=rigid)
        record["head_rigid_metric"] = metric
        if ctx.affine is None:
            ctx.progress.stage("affine", attempt=len(ctx.attempts) + 1)
            base = _rigid_from(sitk.ReadTransform(rigid))
            starts = [{"offset": [0, 0], "metric": metric, "transform": rigid}]
            for index, (pitch, yaw) in enumerate(ctx.options["tilt_offsets"]):
                ctx.cancel.raise_if_cancelled(done=len(ctx.progress.completed))
                start = os.path.join(ctx.work, f"start_{index}.h5")
                sitk.WriteTransform(_offset_start(base, pitch, yaw), start)
                result, value = _head_rigid(ctx, f"head_rigid_start{index}", initial=start)
                starts.append({"offset": [pitch, yaw], "metric": value, "transform": result})
            valid = [item for item in starts if item["metric"] is not None]
            best = min(valid, key=lambda item: item["metric"]) if valid else starts[0]
            record = _affine_attempt(ctx, "A3", initial=best["transform"])
            record["starts"] = [{"offset": item["offset"], "metric": item["metric"]} for item in starts]
            record["best_offset"] = best["offset"]
    ctx.timings["affine"] = round(time.monotonic() - t0, 3)
    if ctx.affine is None:
        attempts = [{"name": item["name"], "ncc_xl": item["ncc_xl"]} for item in ctx.attempts]
        codes = ["alignment_failed"] + ([] if head else ["head_template_missing"])
        message = "The scan could not be aligned to the template (" + ", ".join(f"{item['name']} ncc_xl {item['ncc_xl']}" for item in attempts) + f"). {ALIGN_HINT}"
        if not head and ctx.tilt_search != "off":
            head_name = TEMPLATES[ctx.template_id]["files"]["head"]["name"] if ctx.template_id in TEMPLATES else "a head image added to the template"
            message += f"; the whole-head template ({head_name}) enables the automatic tilt recovery"
        raise jobs.JobError(message, code="alignment_failed", codes=codes, attempts=attempts, head_template_missing=not head, hint=ALIGN_HINT)
    ctx.final_transform = ctx.affine
    ctx.final_warped = ctx.affine_warped
    ctx.final_ncc = ctx.affine_ncc
    ctx.stage_used = "affine"
    ctx.progress.finish()


def _stage_bspline(ctx):
    """BSpline stage (NC; MMI only single-threaded) from the accepted affine; the affine is kept when it does not improve the fit."""
    ctx.progress.stage("bspline")
    t0 = time.monotonic()
    transform = os.path.join(ctx.work, "bspline.h5")
    warped = os.path.join(ctx.work, "warped_bspline.nii")
    metric = ctx.options["bspline_metric"]
    ctx.runner.run(
        ctx.exes["BRAINSFit"],
        {
            "fixedVolume": ctx.template["files"]["fixed"],
            "fixedBinaryVolume": ctx.template["files"]["mask"],
            "movingVolume": ctx.moving,
            "movingBinaryVolume": ctx.costmask,
            "maskProcessingMode": "ROI",
            "initialTransform": ctx.affine,
            "initializeTransformMode": "Off",
            "useBSpline": True,
            "splineGridSize": QUALITY[ctx.quality]["grid"],
            "costMetric": metric,
            "samplingPercentage": ctx.options["bspline_sampling"],
            "bsplineTransform": transform,
            "outputVolume": warped,
            # MMI BSpline with more than one thread exceeded 8 GB in the A-03 measurements
            "numberOfThreads": 1 if metric == "MMI" else ctx.threads,
        },
        "bspline.log",
        ctx.progress.expected["bspline"],
    )
    ncc, _ = _evaluate_candidate(ctx, transform, warped)
    ctx.bspline_ncc = ncc
    if math.isfinite(ncc) and ncc >= ctx.affine_ncc - 0.02:
        ctx.final_transform, ctx.final_warped, ctx.final_ncc, ctx.stage_used = transform, warped, ncc, "bspline"
    else:
        message = "nonlinear stage did not improve the fit; affine kept"
        ctx.warnings.append({"message": message, "code": "bspline_not_kept"})
        _warn(message, "bspline_not_kept", subject=ctx.sid, ncc_affine=ctx.affine_ncc, ncc_bspline=ncc)
    ctx.timings["bspline"] = round(time.monotonic() - t0, 3)
    ctx.progress.finish()


def _compose(ctx):
    """Two-step: flatten rigid (T1w -> scan points) and the template -> T1w transform into one composite."""
    np = _np()
    sitk = _sitk()
    from .transforms import read_transform

    rigid = read_transform(ctx.anat_rigid)
    template_to_t1 = read_transform(ctx.final_transform)
    composite = sitk.CompositeTransform(3)
    composite.AddTransform(rigid)
    if template_to_t1.GetName() == "CompositeTransform":
        inner = sitk.CompositeTransform(template_to_t1)
        for index in range(inner.GetNumberOfTransforms()):
            composite.AddTransform(inner.GetNthTransform(index))
    else:
        composite.AddTransform(template_to_t1)
    path = os.path.join(ctx.work, "final_composite.h5")
    sitk.WriteTransform(composite, path)
    check = read_transform(path)
    for point in ((-40.0, 12.0, 18.0), (0.0, 0.0, 0.0), (30.0, 50.0, 40.0)):  # LPS
        expected = np.asarray(rigid.TransformPoint(template_to_t1.TransformPoint(point)))
        if np.abs(np.asarray(check.TransformPoint(point)) - expected).max() > 1e-6:
            raise jobs.JobError("The composed transform does not reproduce its parts", code="compose_failed")
    return path


def _write_on_grid1(ctx, path, array_ijk, affine, dtype):
    """Write an array on the template's 1 mm grid with the exact reference header (res-01 file, else the grid's)."""
    from .nifti import write_like

    write_like(path, array_ijk.transpose(2, 1, 0), affine, ctx.reference, dtype=dtype)


def _stage_warp(ctx):
    """Lesion (fraction and binary) and scan onto the 1 mm template grid with BRAINSResample (linear)."""
    np = _np()
    from .nifti import write_nifti

    ctx.progress.stage("warp")
    t0 = time.monotonic()
    if ctx.two_step:
        ctx.final_transform = _compose(ctx)
    ref1 = os.path.join(ctx.work, "ref_1mm.nii")
    write_nifti(ref1, np.zeros(ctx.grid1.shape, dtype=np.uint8), ctx.grid1.affine)  # sform = qform = grid, codes 4 (MNI)
    ctx.reference = ctx.template["files"].get("reference") or ref1
    if ctx.reference != ref1:
        from .geometry import signed_permutation
        from .nifti import read_nifti_header

        header = read_nifti_header(ctx.reference)
        mapping = signed_permutation(ctx.grid1.affine, header["affine"])
        if tuple(header["shape"][:3]) != tuple(ctx.grid1.shape) or mapping is None:
            _warn(f"{os.path.basename(ctx.reference)} is not on the {ctx.grid1.grid_id} grid; its header is not used", "reference_mismatch", subject=ctx.sid)
            ctx.reference = ref1
    expected = ctx.progress.expected["warp"]
    if not ctx.scan_only:
        prob = _warp_lesion(ctx, ref1, expected * 0.4)
        prob_affine = ctx.grid1.affine
        binary = (prob >= ctx.options["binarize"]).astype(np.uint8)
        _write_on_grid1(ctx, os.path.join(ctx.final, ctx.names["lesion_prob"]), prob, prob_affine, np.float32)
        _write_on_grid1(ctx, os.path.join(ctx.final, ctx.names["lesion"]), binary, prob_affine, np.uint8)
        ctx.prob1 = prob
        ctx.prob1_affine = prob_affine
    ctx.cancel.raise_if_cancelled(done=len(ctx.progress.completed))
    scan_out = os.path.join(ctx.work, "scan_1mm.nii")
    ctx.runner.run(
        ctx.exes["BRAINSResample"],
        {"inputVolume": ctx.scan, "referenceVolume": ref1, "outputVolume": scan_out, "pixelType": "float", "warpTransform": ctx.final_transform, "interpolationMode": "Linear", "numberOfThreads": ctx.threads},
        "warp_scan.log",
        expected * 0.6,
    )
    warped, warped_affine = _read_float(scan_out)
    dtype = np.float32
    if not re.search(r"float|double", ctx.plan["scan"]["dtype"], re.IGNORECASE):
        rounded = np.round(warped)
        if rounded.size and rounded.min() >= -32768 and rounded.max() <= 32767:
            warped, dtype = rounded.astype(np.int16), np.int16
    _write_on_grid1(ctx, os.path.join(ctx.final, ctx.names["scan"]), warped, warped_affine, dtype)
    ctx.timings["warp"] = round(time.monotonic() - t0, 3)
    ctx.progress.finish()


def _lesion_box(ctx, margin_mm=20.0):
    """(lo, hi) voxel box of the 1 mm grid around the lesion mapped back through the linear transforms.

    The BSpline moves points by a few millimetres (8x8x8 grid), far less than ``margin_mm``; the
    caller checks that the warped lesion does not touch the box border and warps the whole grid otherwise.
    """
    np = _np()
    from .geometry import voxel_to_world, world_to_voxel
    from .transforms import read_transform

    template_to_native = _linear_matrix(ctx.affine)
    if ctx.two_step:
        template_to_native = _matrix_ras(read_transform(ctx.anat_rigid)) @ template_to_native
    nonzero = np.argwhere(ctx.native_fraction > 0)
    lo, hi = nonzero.min(0), nonzero.max(0)
    corners = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])], dtype=np.float64)
    native = voxel_to_world(corners, ctx.native_lesion_affine)
    template = world_to_voxel(native, template_to_native)  # through the inverse of the linear mapping
    ijk = world_to_voxel(template, ctx.grid1.affine)
    margin = margin_mm / float(ctx.grid1.spacing)
    box_lo = np.maximum(np.floor(ijk.min(0) - margin).astype(int), 0)
    box_hi = np.minimum(np.ceil(ijk.max(0) + margin).astype(int) + 1, ctx.grid1.shape)
    return box_lo, box_hi


def _warp_lesion(ctx, ref1, expected_s, use_box=True):
    """The lesion fraction on the full 1 mm grid; BRAINSResample runs on the lesion's box when that is exact."""
    np = _np()
    from .geometry import signed_permutation
    from .nifti import write_nifti

    shape = tuple(ctx.grid1.shape)
    box = None
    if use_box and (ctx.stage_used == "bspline" or ctx.two_step):
        try:
            box = _lesion_box(ctx)
        except Exception:
            box = None
        if box is not None and np.prod(box[1] - box[0]) > 0.6 * np.prod(shape):
            box = None
    if box is not None:
        lo, hi = box
        box_affine = ctx.grid1.affine.copy()
        box_affine[:3, 3] = ctx.grid1.affine[:3, :3] @ lo + ctx.grid1.affine[:3, 3]
        reference = os.path.join(ctx.work, "ref_lesion_box.nii")
        write_nifti(reference, np.zeros(tuple(hi - lo), dtype=np.uint8), box_affine)
    else:
        reference = ref1
    out = os.path.join(ctx.work, "lesion_1mm.nii")
    ctx.runner.run(
        ctx.exes["BRAINSResample"],
        {"inputVolume": ctx.lesion_float, "referenceVolume": reference, "outputVolume": out, "pixelType": "float", "warpTransform": ctx.final_transform, "interpolationMode": "Linear", "numberOfThreads": ctx.threads},
        "warp_lesion.log",
        expected_s,
    )
    values, affine = _read_float(out)
    values = np.clip(values, 0.0, 1.0).astype(np.float32)
    if box is None:
        mapping = signed_permutation(affine, ctx.grid1.affine)
        if mapping is None or mapping.axes != (0, 1, 2) or mapping.signs != (1, 1, 1) or any(mapping.offsets) or values.shape != shape:
            raise jobs.JobError("BRAINSResample did not write the lesion on the template grid", code="warp_failed")
        return values
    lo, hi = box
    touches = any(
        ((values[tuple(slice(0, 1) if axis == a else slice(None) for a in range(3))] > 0).any() and lo[axis] > 0)
        or ((values[tuple(slice(-1, None) if axis == a else slice(None) for a in range(3))] > 0).any() and hi[axis] < shape[axis])
        for axis in range(3)
    )
    if touches or not np.allclose(affine, box_affine, atol=1e-3) or values.shape != tuple(hi - lo):
        # the BSpline moved the lesion further than the margin: warp the whole grid
        os.replace(os.path.join(ctx.logs, "warp_lesion.log"), os.path.join(ctx.logs, "warp_lesion_box.log"))
        return _warp_lesion(ctx, ref1, expected_s, use_box=False)
    full = np.zeros(shape, dtype=np.float32)
    full[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]] = values
    return full


def _jacobian(ctx, fixed_image):
    """Jacobian determinant of the final transform on the 2 mm template grid (SimpleITK)."""
    sitk = _sitk()
    from .transforms import read_transform

    transform = read_transform(ctx.final_transform)
    field = sitk.TransformToDisplacementField(transform, sitk.sitkVectorFloat64, fixed_image.GetSize(), fixed_image.GetOrigin(), fixed_image.GetSpacing(), fixed_image.GetDirection())
    return _array_ijk(sitk.DisplacementFieldJacobianDeterminant(field))


def _stage_qc(ctx):
    """QC metrics (normqc), suggestion, mosaic, normqc JSON and the lesion sidecar."""
    np = _np()
    from . import normqc
    from .export import versions as library_versions
    from .export import write_json_atomic
    from .geometry import Grid, resample_to_grid

    ctx.progress.stage("qc")
    t0 = time.monotonic()
    fixed, mask, affine2, fixed_image = _template_images(ctx)
    warped2, _ = _read_float(ctx.final_warped)
    metrics = {}
    grid2 = Grid(fixed.shape, affine2)
    lesion2 = None
    if not ctx.scan_only:
        # the lesion's bounding box on the 1 mm grid: every lesion metric is computed there (8.5 M voxels -> about 0.1 M)
        crop, crop_affine = _crop(ctx.prob1, ctx.prob1_affine)
        lesion2 = resample_to_grid(crop, crop_affine, grid2, mode="fraction")
    metrics["ncc_xl"] = normqc.ncc_xl(fixed, warped2, mask, lesion2)
    metrics["nmi_xl"] = normqc.nmi_xl(fixed, warped2, mask, lesion2)
    template_brain_ml = normqc.volume_ml(mask, affine2, mode="binary")
    metrics["template_brain_ml"] = template_brain_ml
    template_to_moving = _linear_matrix(ctx.affine)
    metrics["affine_scale"] = normqc.affine_scale(template_to_moving)
    if ctx.stage_used == "bspline":
        jac = _jacobian(ctx, fixed_image)
        metrics["folding_pct"] = normqc.folding_pct(jac, mask)
        if lesion2 is not None:
            metrics["logJ_lesion_minus_brain"] = normqc.log_jacobian_lesion_minus_brain(jac, lesion2, mask)
    if not ctx.scan_only:
        mask1 = resample_to_grid(mask.astype(np.float32), affine2, Grid(crop.shape, crop_affine), mode="nearest") >= 0.5
        metrics["native_ml"] = normqc.volume_ml(ctx.native_fraction, ctx.native_lesion_affine, mode="fraction")
        metrics["mni_ml"] = normqc.volume_ml(ctx.prob1, ctx.prob1_affine, mode="binary", threshold=ctx.options["binarize"])
        metrics["mni_ml_frac"] = normqc.volume_ml(ctx.prob1, ctx.prob1_affine, mode="fraction")
        change = normqc.lesion_volume_change(metrics["native_ml"], metrics["mni_ml_frac"], normqc.native_brain_ml_from_affine(template_brain_ml, template_to_moving), template_brain_ml)
        metrics.update({key: change[key] for key in ("raw_ratio", "scale_ratio", "normalized_ratio")})
        metrics["outside_brain_pct"] = normqc.pct_outside_brain(crop, mask1)
        centroid = normqc.centroid_mm(crop, crop_affine)
        metrics["centroid_mni"] = centroid
        metrics["side"] = normqc.lesion_side(centroid)
        metrics["excluded_ml"] = ctx.excluded_ml
    metrics["attempts"] = len(ctx.attempts)
    method = {
        "stage": ctx.stage_used,
        "dilate_mm": ctx.dilate_mm,
        "masking": not ctx.scan_only and ctx.dilate_mm >= 0,
        "cross_modality": ctx.cross_modality,
        "two_step": ctx.two_step,
        "scan_only": ctx.scan_only,
        "attempts": len(ctx.attempts),
    }
    evaluation = normqc.evaluate(metrics, method)
    if ctx.cross_modality and not ctx.two_step and normqc.SUGGEST_REVIEW != evaluation["suggestion"]:
        evaluation["suggestion"] = normqc.SUGGEST_REVIEW
    ctx.evaluation = evaluation
    ctx.metrics = metrics
    ctx.versions_all = {**library_versions(), **{name: version for name, version in ctx.versions.items() if version}}
    ctx.method_record = _method_record(ctx)
    content = normqc.normqc_json(metrics, evaluation, method={**method, **ctx.method_record}, versions=ctx.versions_all, subject=ctx.sid, template=ctx.template_id)
    write_json_atomic(os.path.join(ctx.final, ctx.names["qc_json"]), content)
    ctx.cancel.raise_if_cancelled(done=len(ctx.progress.completed))
    lesion_for_png = lesion2 if lesion2 is not None else np.zeros(fixed.shape, dtype=np.float32)
    title = f"{(ctx.template.get('user') or {}).get('name') or ctx.template_id} | {ctx.sid} | research use only"
    ctx.mosaic = normqc.qc_mosaic_png(fixed, warped2, mask, lesion_for_png, os.path.join(ctx.final, ctx.names["qc_png"]), checkerboard=True, affine=affine2, title=title)
    if not ctx.scan_only:
        _write_sidecar(ctx)
    ctx.timings["qc"] = round(time.monotonic() - t0, 3)
    ctx.progress.finish()


def _method_record(ctx):
    return {
        "tool": "BRAINSFit",
        "version": ctx.versions.get("BRAINSFit"),
        "label": method_label(ctx.quality, ctx.two_step, ctx.dilate_mm, ctx.scan_only),
        "quality": ctx.quality,
        "requested_quality": ctx.requested_quality,
        "grid": QUALITY[ctx.quality]["grid"],
        "stage_used": ctx.stage_used,
        "metrics": {"affine": "MMI", "bspline": ctx.options["bspline_metric"] if QUALITY[ctx.quality]["bspline"] else None, "acceptance": "ncc_xl"},
        "dilate_mm": ctx.dilate_mm,
        "moving_mask": ctx.moving_mask,
        "modality": ctx.modality,
        "modality_guessed": ctx.modality_guessed,
        "two_step": ctx.two_step,
        "scan_only": ctx.scan_only,
        "experimental": ctx.cross_modality and not ctx.two_step,
        "tilt_search": ctx.tilt_search,
        "initial_transform": os.path.basename(ctx.initial_transform) if ctx.initial_transform else None,
        "attempts": ctx.attempts,
        "options": ctx.method_options,
        "argv": ctx.runner.argv,
        "logs": "logs/*.log hold the child output with paths relative to this folder",
        "note": NOTE,
    }


def _write_sidecar(ctx):
    """BIDS sidecar of the template-space lesion (key names of study.Study.write_lesion_sidecar)."""
    from .export import write_json_atomic

    folder = ctx.out
    sources = [_rel(ctx.lesion, folder), ctx.names["transform"]]
    content = {
        "Type": "Lesion",
        "Source": sources[0],
        "Sources": sources,
        "SpatialReference": ctx.template_id if not is_user_template(ctx.template_id) else template_space(ctx.template_id),
        "Resolution": "1 mm" if not is_user_template(ctx.template_id) else f"{float(ctx.grid1.spacing):g} mm",
        "SlicerStudio": {
            "participant_id": ctx.sid,
            "space": template_space(ctx.template_id),
            "modified": _now(),
            "norm_method": ctx.method_record["label"],
            "norm_qc": "unreviewed",
            "norm_suggestion": ctx.evaluation["suggestion"],
            "norm_fingerprint": ctx.fingerprint[:12],
            "fingerprint": ctx.fingerprint,
            "lesion_prob": ctx.names["lesion_prob"],
            "binarize_at": ctx.options["binarize"],
        },
    }
    write_json_atomic(os.path.join(ctx.final, ctx.names["lesion_sidecar"]), content)


def _commit(ctx, started):
    """Move the final files into --out (after QC), then write normalization.json."""
    from .export import DISCLAIMER, host_info, write_json_atomic

    sources = {
        "transform": ctx.final_transform,
        "affine": ctx.affine,
        "affine_inverse": os.path.splitext(ctx.affine)[0] + "_Inverse.h5",
    }
    if ctx.two_step:
        sources["anat_transform"] = ctx.anat_rigid
    for key, source in sources.items():
        if os.path.isfile(source):
            shutil.copyfile(source, os.path.join(ctx.final, ctx.names[key]))
    outputs = {}
    for key, name in ctx.names.items():
        path = os.path.join(ctx.final, name)
        if os.path.isfile(path):
            outputs[key] = {"path": name, "bytes": os.path.getsize(path), "sha256": _sha256(path)}
    for item in outputs.values():
        os.replace(os.path.join(ctx.final, item["path"]), os.path.join(ctx.out, item["path"]))
    logs_out = os.path.join(ctx.out, "logs")
    os.makedirs(logs_out, exist_ok=True)
    for name in os.listdir(ctx.logs) if os.path.isdir(ctx.logs) else ():
        os.replace(os.path.join(ctx.logs, name), os.path.join(logs_out, name))
    ctx.timings["total"] = round(time.monotonic() - started, 3)
    if ctx.runner.peak_rss_mb is not None:
        ctx.timings["child_peak_rss_mb"] = ctx.runner.peak_rss_mb
        ctx.timings["peak_rss_mb"] = ctx.runner.self_peak_rss_mb

    def described(path, extra=None):
        if not path:
            return None
        info = {"path": _rel(path, ctx.out), "sha256": _sha256(path)}
        info.update(extra or {})
        return info

    plan_info = ctx.plan
    record = {
        "format": FORMAT,
        "version": NORMALIZE_VERSION,
        "status": "done",
        "fingerprint": ctx.fingerprint,
        "created": _now(),
        "subject": ctx.sid,
        "inputs": {
            "scan": described(ctx.scan, {"grid": {"shape": plan_info["scan"]["shape"], "affine": plan_info["scan"]["affine"]}, "dtype": plan_info["scan"]["dtype"]}),
            "lesion": described(ctx.lesion, {"grid": {"shape": plan_info["lesion"]["shape"], "affine": plan_info["lesion"]["affine"]}, "kind": plan_info["lesion"]["kind"]}) if ctx.lesion else None,
            "anat": described(ctx.anat, {"grid": {"shape": plan_info["anat"]["shape"], "affine": plan_info["anat"]["affine"]}}) if ctx.two_step else None,
            "initial_transform": described(ctx.initial_transform),
        },
        "template": {
            "id": ctx.template_id,
            "files": {role: os.path.basename(path) for role, path in ctx.template["files"].items() if path},
            "sha256": ctx.template["sha256"],
            "grid": {"id": ctx.grid1.grid_id, "shape": list(ctx.grid1.shape), "affine": ctx.grid1.affine.tolist()},
            "header_reference": os.path.basename(ctx.reference) if ctx.reference and ctx.template["files"].get("reference") == ctx.reference else None,
            **({"name": ctx.template["user"]["name"], "space": ctx.template["user"]["space"], "image_sha256": ctx.template["user"]["sha256"]}
               if ctx.template.get("user") else {}),
        },
        "method": ctx.method_record,
        "outputs": outputs,
        "qc": {"metrics": ctx.metrics, "levels": ctx.evaluation["levels"], "suggestion": ctx.evaluation["suggestion"], "reasons": ctx.evaluation["reasons"]},
        "review": {"verdict": None, "by": None, "at": None},
        "warps": [],
        "warnings": ctx.warnings,
        "timings": ctx.timings,
        "versions": ctx.versions_all,
        "host": host_info(),
        "disclaimer": DISCLAIMER,
    }
    write_json_atomic(os.path.join(ctx.out, PROVENANCE_JSON), _plain(record))
    for key, item in outputs.items():
        jobs.emit("result", kind=key, path=os.path.join(ctx.out, item["path"]), subject=ctx.sid)
    result = _result_from_record(_plain(record), ctx.out, "done")
    jobs.emit(
        "result",
        kind="normalization",
        status="done",
        subject=ctx.sid,
        path=os.path.join(ctx.out, PROVENANCE_JSON),
        fingerprint=ctx.fingerprint,
        suggestion=ctx.evaluation["suggestion"],
        reasons=ctx.evaluation["reasons"],
        metrics={key: ctx.metrics.get(key) for key in ("ncc_xl", "normalized_ratio", "logJ_lesion_minus_brain", "folding_pct", "outside_brain_pct", "mni_ml", "native_ml")},
        timings=ctx.timings,
    )
    return result


# Re-warping another native image with a run's transform


def warp(run_dir, image, kind="lesion", out=None, template_dirs=(), brainsresample=None, threads=None, cancel=None):
    """Re-warp another native image (an edited lesion, a second rater) with a run's final transform onto
    the template's 1 mm grid with the exact header; append the record to normalization.json ``warps``.

    :param kind: "lesion" (linear, clipped to 0..1; writes the binary mask at ``out`` and the fraction
      next to it with desc-lesionProb) or "image" (linear; int16 when it fits, else float32).
    :return: {"outputs": {...}, "record": {...}}.
    """
    np = _np()
    from .export import write_json_atomic
    from .nifti import write_like, write_nifti

    cancel = cancel or jobs.CancelToken()
    run_dir = os.path.abspath(os.fspath(run_dir))
    record = read_result(run_dir)
    if not record or record.get("status") != "done":
        raise jobs.JobError(f"{run_dir} holds no complete normalization", code="no_result")
    if kind not in ("lesion", "image"):
        raise jobs.JobError(f"Unknown kind {kind!r}", code="usage", exit_code=jobs.EXIT_USAGE)
    image = os.path.abspath(os.fspath(image))
    if not os.path.isfile(image):
        raise jobs.JobError(f"The image does not exist: {image}", code="file_missing", path=image)
    template_id = record["template"]["id"]
    transform = os.path.join(run_dir, *record["outputs"]["transform"]["path"].split("/"))
    exe = find_executable("BRAINSResample", brainsresample)
    threads = jobs.budget_threads(threads)
    stem = _nifti_stem(image)
    if out is None:
        suffix = "desc-lesion_mask" if kind == "lesion" else "desc-warped"
        out = os.path.join(run_dir, "warps", f"{stem}_space-{template_label(template_id)}{_res_entity(template_id)}_{suffix}.nii.gz")
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    found = find_template(template_id, dirs=template_dirs)
    grid1 = template_grid(template_id)
    lock = jobs.OutputLock(os.path.join(run_dir, LOCK_NAME))
    lock.acquire()
    if lock.reclaimed is not None:
        _remove_stale_work(run_dir)
    work = os.path.join(run_dir, f".work-warp-{os.getpid()}")
    try:
        os.makedirs(work, exist_ok=True)
        ref1 = os.path.join(work, "ref_1mm.nii")
        write_nifti(ref1, np.zeros(grid1.shape, dtype=np.uint8), grid1.affine)
        # the run's own template-grid output carries the header of its outputs (the res-01 file's when it was found)
        key = "lesion" if "lesion" in record["outputs"] else "scan"
        reference = os.path.join(run_dir, *record["outputs"][key]["path"].split("/"))
        if not os.path.isfile(reference):
            reference = found["files"].get("reference") or ref1
        source = image
        if kind == "lesion":
            data, affine = read_image(image)
            fraction, _ = _lesion_fraction(data)
            source = os.path.join(work, "lesion_float.nii")
            from .nifti import XFORM_SCANNER

            write_nifti(source, fraction, affine, sform_code=XFORM_SCANNER, qform_code=XFORM_SCANNER)
        cancel.raise_if_cancelled(done=0)
        runner = _Runner(cancel, None, os.path.join(work, "logs"), run_dir, threads)
        resampled = os.path.join(work, "warped.nii")
        runner.run(exe, {"inputVolume": source, "referenceVolume": ref1, "outputVolume": resampled, "pixelType": "float", "warpTransform": transform, "interpolationMode": "Linear", "numberOfThreads": threads}, "warp.log", 3.0)
        array, affine = _read_float(resampled)
        outputs = {}
        if kind == "lesion":
            prob = np.clip(array, 0.0, 1.0).astype(np.float32)
            prob_path = _prob_path(out)
            write_like(prob_path, prob.transpose(2, 1, 0), affine, reference, dtype=np.float32)
            write_like(out, (prob >= DEFAULTS["binarize"]).astype(np.uint8).transpose(2, 1, 0), affine, reference, dtype=np.uint8)
            outputs = {"lesion": out, "lesion_prob": prob_path}
        else:
            rounded = np.round(array)
            if rounded.size and rounded.min() >= -32768 and rounded.max() <= 32767 and not re.search(r"float|double", read_geometry(image)[2], re.IGNORECASE):
                write_like(out, rounded.astype(np.int16).transpose(2, 1, 0), affine, reference, dtype=np.int16)
            else:
                write_like(out, array.transpose(2, 1, 0), affine, reference, dtype=np.float32)
            outputs = {"image": out}
        entry = {
            "created": _now(),
            "kind": kind,
            "input": {"path": _rel(image, run_dir), "sha256": _sha256(image)},
            "outputs": {key: {"path": _rel(path, run_dir), "sha256": _sha256(path)} for key, path in outputs.items()},
            "transform": os.path.basename(transform),
            "interpolation": "linear",
            "argv": runner.argv,
        }
        record = read_result(run_dir)
        record.setdefault("warps", []).append(entry)
        write_json_atomic(os.path.join(run_dir, PROVENANCE_JSON), _plain(record))
        for key, path in outputs.items():
            jobs.emit("result", kind=key, path=path)
        return {"outputs": outputs, "record": entry}
    finally:
        shutil.rmtree(work, ignore_errors=True)
        lock.release()


def _prob_path(out):
    """The probability file next to a binary lesion output: desc-lesionProb_mask, else a _prob suffix (file name only)."""
    folder, name = os.path.split(out)
    if "desc-lesion_mask" in name:
        name = name.replace("desc-lesion_mask", "desc-lesionProb_mask")
    else:
        stem = _nifti_stem(name)
        name = stem + "_prob" + name[len(stem) :]
    return os.path.join(folder, name)


# Environment check


def check(template=None, template_dirs=(), brainsfit=None, brainsresample=None, brainsroiauto=None):
    """Report the executables and their versions, the templates and their files, PythonSlicer and threads.

    :return: (report dict, ok): ok when BRAINSFit and BRAINSResample are found and the requested (or
      default) template is usable.
    """
    report = {"normalize_version": NORMALIZE_VERSION, "requires_slicer": REQUIRES_SLICER, "python": sys.executable, "pythonslicer": shutil.which("PythonSlicer"), "threads": jobs.budget_threads()}
    executables = {}
    for name, explicit in (("BRAINSFit", brainsfit), ("BRAINSResample", brainsresample), ("BRAINSROIAuto", brainsroiauto)):
        try:
            path = find_executable(name, explicit)
            executables[name] = {"found": True, "path": path, "version": executable_version(path)}
        except jobs.JobError as exc:
            executables[name] = {"found": False, "path": None, "version": None, "message": exc.message}
    report["executables"] = executables
    templates = {}
    for template_id in TEMPLATE_IDS if template in (None, "", "auto") else (template,):
        if is_user_template(template_id):
            templates[template_id] = _check_user_template(template_id, template_dirs)
            continue
        found = find_template(template_id, dirs=template_dirs)
        grid = template_grid(template_id)
        templates[template_id] = {
            "usable": not any(role in found["missing"] for role in REQUIRED_TEMPLATE_FILES),
            "head_template": found["head_available"],
            "header_reference": found["reference_available"],
            "grid1": {"id": grid.grid_id, "shape": list(grid.shape), "affine": grid.affine.tolist()},
            "terms": TEMPLATES[template_id]["terms"],
            "files": {
                role: {"path": found["files"][role], "found": bool(found["files"][role]), "sha256": found["sha256"].get(role), "bytes": info["bytes"], "url": template_url(template_id, role)}
                for role, info in TEMPLATES[template_id]["files"].items()
            },
        }
    report["templates"] = templates
    report["search_dirs"] = template_search_dirs(template_dirs)
    wanted = DEFAULT_TEMPLATE if template in (None, "", "auto") else template
    ok = executables["BRAINSFit"]["found"] and executables["BRAINSResample"]["found"] and templates[wanted]["usable"]
    report["ok"] = bool(ok)
    return report, bool(ok)


def _user_template_missing_message(template_id, found):
    """'Template user:<id> ("<name>"): its image <file> is missing from <folder>' for the required files of a user
    template that are not on disk (None when none is missing).
    """
    missing = [role for role in REQUIRED_TEMPLATE_FILES if role in found["missing"]]
    if not missing:
        return None
    record = _USER_TEMPLATES.get(template_id) or {}
    listed = record.get("files") or {}
    parts = []
    for role in missing:
        key, what = {"fixed": ("image", "image"), "mask": ("mask", "brain mask")}[role]
        name = listed.get(key)
        parts.append(f"its {what} {name}" if name else f"its {what} (not listed in template.json)")
    where = f" from {record['folder']}" if record.get("folder") else ""
    verb = "is" if len(parts) == 1 else "are"
    return f'Template {template_id} ("{found["user"]["name"]}"): ' + " and ".join(parts) + f" {verb} missing{where}"


def _check_user_template(template_id, template_dirs):
    """The ``check`` report of a user template."""
    try:
        found = find_template(template_id, dirs=template_dirs)
    except jobs.JobError as exc:
        return {"usable": False, "message": exc.message, "files": {}}
    files = {role: {"path": path, "found": bool(path), "sha256": found["sha256"].get(role)} for role, path in found["files"].items() if role != "reference"}
    message = _user_template_missing_message(template_id, found)
    if message:
        return {"usable": False, "message": message, "name": found["user"]["name"], "space": found["user"]["space"], "files": files}
    grid = template_grid(template_id)
    return {
        "usable": True,
        "name": found["user"]["name"],
        "space": found["user"]["space"],
        "head_template": found["head_available"],
        "header_reference": True,
        "grid1": {"id": grid.grid_id, "shape": list(grid.shape), "affine": grid.affine.tolist()},
        "terms": "your own template (its licence is yours to check)",
        "files": files,
    }


# Batch over a study


def _study_user_template(study, template_dirs, template_image=None, template_mask=None, template_head=None):
    """The "user:<id>" of the study's own template (Slicer Studio A-10) when its image and brain mask are in
    ``template_dirs``, else None. Found by the image sha256 that study.json records (ids are chosen on each computer;
    the same id is preferred), else by id.
    """
    user = study.user_template() if hasattr(study, "user_template") else None
    if not user or not user.get("id"):
        return None
    records = [record for record in (read_user_template(folder) for folder in user_template_folders(template_dirs)) if record]
    if user.get("sha256"):
        matches = [record for record in records if (record.get("sha256") or {}).get("image") == user["sha256"]]
        matches.sort(key=lambda record: f"{USER_TEMPLATE_PREFIX}{record['id']}" != user["id"])
    else:
        matches = [record for record in records if f"{USER_TEMPLATE_PREFIX}{record['id']}" == user["id"]]
    for record in matches:
        template_id = f"{USER_TEMPLATE_PREFIX}{record['id']}"
        try:
            found = find_template(template_id, template_image, template_mask, template_head, template_dirs)
        except jobs.JobError:
            continue
        if not any(role in found["missing"] for role in REQUIRED_TEMPLATE_FILES):
            return template_id
    return None


def _study_template(study, explicit, template_dirs, template_image=None, template_mask=None, template_head=None):
    """Template of a batch: explicit, else the study's own template ("user:<id>", A-10) when its image and mask are in
    ``template_dirs``, else the study's trace_policy space or connectome family when usable, else 2009c.
    """
    if explicit not in (None, "", "auto"):
        return explicit, None
    user_id = _study_user_template(study, template_dirs, template_image, template_mask, template_head)
    if user_id:
        return user_id, None
    candidates = []
    policy_space = (study.trace_policy or {}).get("space")
    if policy_space:
        candidates.append(policy_space)
    for entry in study.metadata.get("connectomes") or []:
        if isinstance(entry, dict):
            candidates += [entry.get(key) for key in ("space", "template", "family") if entry.get(key)]
        elif isinstance(entry, str):
            candidates.append(entry)
    from .geometry import space_family

    for space in candidates:
        for template_id in TEMPLATE_IDS:
            if str(space).lower() == template_id.lower() or space_family(space) == space_family(template_id):
                found = find_template(template_id, template_image, template_mask, template_head, template_dirs)
                if not any(role in found["missing"] for role in REQUIRED_TEMPLATE_FILES):
                    return template_id, None
                break
    named = next((space for space in candidates if space), None)
    if named and space_family(named) != space_family(DEFAULT_TEMPLATE):
        return DEFAULT_TEMPLATE, f"The study's space {named} is not available for normalization; using {DEFAULT_TEMPLATE} (template family mismatch: transform the lesions to {named} afterwards)"
    return DEFAULT_TEMPLATE, None


def batch(study_path, subjects=None, force=False, cancel=None, **options):
    """Normalize the eligible rows of a study one at a time and post their values to the study inbox.

    Eligible: include = 1 and either space = native (or missing, with a native lesion header) or
    norm_method set. Inputs are (scan_native or scan, lesion_native or lesion). Rows without a scan
    are skipped ("needs scan"); a row whose lesion was edited in template space after its last
    normalization is skipped unless ``force``.

    :param options: :func:`normalize` options (template "auto" follows the study, see :func:`_study_template`).
    :return: {"subjects": [{subject, status, ...}], "counts": {...}, "template": id}.
    :raises jobs.JobError: exit 4 when no row is eligible.
    """
    from .study import Study, detect_file_space, is_missing

    cancel = cancel or jobs.CancelToken()
    study = Study.open(study_path)
    template_id, mismatch = _study_template(study, options.pop("template", "auto"), options.get("template_dirs", ()), options.get("template_image"), options.get("template_mask"), options.get("template_head"))
    if mismatch:
        _warn(mismatch, "template_family_mismatch", study_space=(study.trace_policy or {}).get("space"))
    wanted = None if not subjects else {s if s.startswith("sub-") else f"sub-{s}" for s in subjects}
    rows, skipped = [], []
    for row in study.subjects:
        sid = row["participant_id"]
        if wanted is not None and sid not in wanted:
            continue
        if row.get("include") != "1":
            continue
        norm_method = None if is_missing(row.get("norm_method")) else row["norm_method"]
        space = None if is_missing(row.get("space")) else row["space"]
        lesion = study.abspath(row.get("lesion_native")) if norm_method and not is_missing(row.get("lesion_native")) else study.abspath(row.get("lesion"))
        if not norm_method:
            if space is None and lesion and os.path.isfile(lesion):
                try:
                    space = detect_file_space(lesion)
                except (OSError, ValueError):
                    space = None
            if space != "native":
                continue
        scan = study.abspath(row.get("scan_native")) if not is_missing(row.get("scan_native")) else study.abspath(row.get("scan"))
        if not scan:
            skipped.append({"subject": sid, "status": "skipped", "reason": "needs scan"})
            continue
        if not lesion:
            skipped.append({"subject": sid, "status": "skipped", "reason": "needs lesion"})
            continue
        out = os.path.join(study.path, "derivatives", "normalization", sid)
        if norm_method and not force:
            previous = read_result(out)
            current = study.abspath(row.get("lesion"))
            recorded = None
            if previous and (previous.get("outputs") or {}).get("lesion"):
                item = previous["outputs"]["lesion"]
                recorded = (os.path.join(out, *item["path"].split("/")), item.get("sha256"))
            if current and recorded and (os.path.normcase(os.path.abspath(current)) != os.path.normcase(recorded[0]) or not os.path.isfile(current) or _sha256(current) != recorded[1]):
                skipped.append({"subject": sid, "status": "skipped", "reason": "lesion edited in template space (use --force to normalize again)"})
                continue
        rows.append({"subject": sid, "scan": scan, "lesion": lesion, "out": out, "row": row})
    for item in skipped:
        jobs.emit("result", kind="subject", **item)
    if not rows:
        raise jobs.JobError("No row is eligible for normalization (included native-space rows with a scan and a lesion)", code="nothing_to_do", exit_code=EXIT_NOTHING_TO_DO, skipped=skipped)
    try:
        free = shutil.disk_usage(study.path).free
    except OSError:
        free = None
    if free is not None and free < 3 * SUBJECT_DISK_BYTES * len(rows) + WORK_DISK_BYTES:
        raise jobs.JobError(f"Not enough free disk space in {study.path} for {len(rows)} subjects ({free / 1e6:.0f} MB)", code="disk_full")
    results = list(skipped)
    durations = {}
    for index, item in enumerate(rows):
        cancel.raise_if_cancelled(done=index)
        sid = item["subject"]
        expected = {stage: sorted(values)[len(values) // 2] for stage, values in durations.items()} if durations else None
        try:
            result = normalize(item["scan"], item["lesion"], item["out"], subject=sid, template=template_id, cancel=cancel, batch_progress=(index, len(rows), expected), **options)
        except jobs.Locked as exc:
            entry = {"subject": sid, "status": "locked", "reason": exc.message}
            results.append(entry)
            jobs.emit("result", kind="subject", **entry)
            continue
        except jobs.Cancelled:
            raise
        except jobs.JobError as exc:
            entry = {"subject": sid, "status": "failed", "code": exc.code, "reason": exc.message}
            results.append(entry)
            jobs.emit("result", kind="subject", **entry)
            continue
        for stage, seconds in (result.get("timings") or {}).items():
            if stage in STAGE_WEIGHTS:
                durations.setdefault(stage, []).append(seconds)
        values, audit = _inbox_values(study, item["row"], result)
        inbox = Study.post_result(study.path, sid, values, source=INBOX_SOURCE, audit=audit)
        entry = {"subject": sid, "status": result["status"], "out": item["out"], "suggestion": result["qc"].get("suggestion"), "inbox": inbox, "values": values}
        results.append(entry)
        jobs.emit("result", kind="subject", **entry)
    counts = {}
    for entry in results:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    return {"subjects": results, "counts": counts, "template": template_id}


def _inbox_values(study, row, result):
    """Row values posted to the study inbox after a subject (and its audit record)."""
    from .study import is_missing

    outputs = result["outputs"]
    native_scan = row.get("scan_native") if not is_missing(row.get("scan_native")) else row.get("scan")
    native_lesion = row.get("lesion_native") if not is_missing(row.get("lesion_native")) else row.get("lesion")
    volume = result.get("volume_ml")
    native_volume = result.get("volume_native_ml")
    values = {
        "scan_native": native_scan,
        "lesion_native": native_lesion,
        "scan": study.relpath(outputs["scan"]),
        "lesion": study.relpath(outputs["lesion"]),
        "space": template_space(result["template"]),
        "norm_method": result["method_label"],
        "norm_qc": "unreviewed",
        "norm_suggestion": result["qc"].get("suggestion"),
        "norm_fingerprint": (result.get("fingerprint") or "")[:12],
        "volume_ml": None if volume is None else round(float(volume), 3),
        "volume_native_ml": None if native_volume is None else round(float(native_volume), 3),
    }
    audit = {
        "event": "normalize",
        "sid": row["participant_id"],
        "fingerprint": result.get("fingerprint"),
        "template": result["template"],
        "quality": result.get("quality"),
        "suggestion": result["qc"].get("suggestion"),
        "status": result["status"],
        "by": "normalize",
        "at": _now(),
    }
    return values, audit


# Command line


def _check_lines(report):
    """The readable lines of a ``check`` report for the text output: one per tool (found or not, version, path or
    reason), one per template (usable or not, with the reason when not) and the folders searched.
    """
    lines = []
    for name, info in (report.get("executables") or {}).items():
        if info.get("found"):
            lines.append(f"  {name}: found, version {info.get('version') or 'unknown'} ({info.get('path')})")
        else:
            lines.append(f"  {name}: not found" + (f" ({info['message']})" if info.get("message") else ""))
    for template_id, info in (report.get("templates") or {}).items():
        if info.get("usable"):
            lines.append(f"  template {template_id}: usable" + (f" ({info['name']})" if info.get("name") else ""))
        else:
            reason = info.get("message") or "; ".join(f"{role} file missing" for role, item in (info.get("files") or {}).items() if not item.get("found"))
            lines.append(f"  template {template_id}: not usable" + (f": {reason}" if reason else ""))
    if report.get("search_dirs"):
        lines.append("  template folders searched: " + ", ".join(str(folder) for folder in report["search_dirs"]))
    return lines


class _EventStream:
    """Standard output filter: JSON event lines as ASCII (non-ASCII characters as JSON unicode escapes) or as readable text."""

    def __init__(self, stream, text=False):
        self._stream = stream
        self._text = text
        self._pending = ""

    def write(self, data):
        self._pending += data
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            self._stream.write(self._format(line) + "\n")
        return len(data)

    def _format(self, line):
        if not self._text:
            return "".join(c if ord(c) < 128 else "".join(f"\\u{unit:04x}" for unit in _utf16(c)) for c in line)
        try:
            event = json.loads(line)
        except ValueError:
            return line
        if not isinstance(event, dict):
            return line
        name = event.get("event")
        head = f"[{event.get('t', 0):7.1f}s] {name}"
        if name == "progress":
            return f"{head} {event.get('subject') or ''} {event.get('stage') or ''} {100 * (event.get('fraction') or 0):.0f}% eta {event.get('eta')} s"
        if name == "stage":
            return f"{head} {event.get('subject') or ''} {event.get('name')} ({event.get('index')}/{event.get('total')})"
        if name in ("warning", "error"):
            return f"{head} [{event.get('code')}] {event.get('message')}"
        if name == "result":
            first = f"{head} {event.get('kind')} {event.get('status') or ''} {event.get('path') or event.get('reason') or ''}".rstrip()
            if event.get("kind") == "check":
                return "\n".join([first] + _check_lines(event))
            return first
        return f"{head} " + " ".join(f"{key}={value}" for key, value in event.items() if key not in ("event", "t") and not isinstance(value, (dict, list)))

    def flush(self):
        self._stream.flush()

    def fileno(self):
        return self._stream.fileno()

    def isatty(self):
        return self._stream.isatty()

    @property
    def encoding(self):
        return getattr(self._stream, "encoding", "utf-8")


def _utf16(character):
    data = character.encode("utf-16-be")
    return [int.from_bytes(data[i:i + 2], "big") for i in range(0, len(data), 2)]


def _template_argument(text):
    """argparse type of --template: "auto", a built-in id or "user:<id>"."""
    import argparse

    if text in ("auto", *TEMPLATE_IDS) or (is_user_template(text) and len(text) > len(USER_TEMPLATE_PREFIX)):
        return text
    raise argparse.ArgumentTypeError(f"invalid choice: {text!r} (choose from auto, {', '.join(TEMPLATE_IDS)} or user:<id>)")


def _common_options(parser, with_run_options=True):
    jobs.add_common_arguments(parser, threads_default=None)
    for action in parser._actions:
        if action.dest == "threads":
            action.help = "worker threads of each child (default: CPUs - 1, capped by $SLICER_LNM_MAX_THREADS)"
    parser.add_argument("--nice", type=int, default=0, help="lower the priority of the job (POSIX nice; Windows below normal when > 0)")
    parser.add_argument("--parent-pid", type=int, default=None, help="stop when this process is gone")
    parser.add_argument("--events", choices=("auto", "json", "text"), default="auto", help="event format on standard output (auto: text on a terminal)")
    parser.add_argument("--template-dir", action="append", default=[], help="folder searched for template files (repeatable)")
    parser.add_argument("--brainsfit", default=None, help="BRAINSFit executable (default: on PATH)")
    parser.add_argument("--brainsresample", default=None, help="BRAINSResample executable (default: on PATH)")
    parser.add_argument("--brainsroiauto", default=None, help="BRAINSROIAuto executable (default: on PATH)")
    if not with_run_options:
        return
    parser.add_argument("--template", type=_template_argument, default="auto",
                        help=f"target template: auto (in a batch the study's own template when it is in --template-dir, else the study's space; else MNI152NLin2009cAsym), {', '.join(TEMPLATE_IDS)}, or user:<id> "
                             "(your own template; with --template-dir <Slicer Studio folder>/Templates)")
    parser.add_argument("--template-image", default=None, help="2 mm registration target of the template")
    parser.add_argument("--template-mask", default=None, help="2 mm brain mask of the template")
    parser.add_argument("--template-head", default=None, help="2 mm whole-head image of the template (tilt recovery)")
    parser.add_argument("--modality", choices=("auto", *MODALITIES), default="auto", help="scan contrast (auto: from the file name)")
    parser.add_argument("--quality", choices=tuple(QUALITY), default=DEFAULTS["quality"], help="standard (affine + BSpline 8x8x8), fine (14x10x12) or affine")
    parser.add_argument("--dilate-mm", type=float, default=DEFAULTS["dilate_mm"], help="lesion dilation excluded from the metric (-1 disables masking: diagnostics only)")
    parser.add_argument("--moving-mask", choices=("auto", "none", "head"), default="auto", help="head mask of the moving image (auto: BRAINSROIAuto for CT only)")
    parser.add_argument("--initial-transform", default=None, help="linear transform file (template -> scan) replacing the automatic initialization")
    parser.add_argument("--tilt-search", choices=("auto", "always", "off"), default="auto", help="head-to-head rigid and multi-start search (auto: after a failed alignment)")
    parser.add_argument("--no-reuse", action="store_true", help="recompute even when a complete result with the same fingerprint exists")
    parser.add_argument("--force", action="store_true", help="run: also register a scan on a template grid; batch: also rows edited in template space")
    parser.add_argument("--keep-work", action="store_true", help="keep the .work-<pid> folder")


def _parser(command):
    prog = f"python -m LesionNetworkMappingLib.normalize {command}"
    parser = jobs.JsonArgumentParser(prog=prog, description=__doc__.split("\n\n")[0])
    if command == "run":
        parser.add_argument("--scan", required=True, help="native scan (any ITK-readable 3D image)")
        parser.add_argument("--lesion", default=None, help="native lesion (NIfTI or NRRD, binary or fractional, any grid); without it: scan-only mode")
        parser.add_argument("--out", required=True, help="output folder")
        parser.add_argument("--subject", default=None, help="participant id (default: from the file names)")
        parser.add_argument("--anat", default=None, help="T1w of the same subject (two-step path for FLAIR/T2w/DWI/CT)")
        _common_options(parser)
    elif command == "batch":
        parser.add_argument("--study", required=True, help="study folder")
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--subjects", default=None, help="comma-separated participant ids")
        group.add_argument("--all-native", action="store_true", help="every eligible row (default)")
        _common_options(parser)
    elif command == "warp":
        parser.add_argument("--run", required=True, help="output folder of a completed normalization")
        parser.add_argument("--image", required=True, help="native image to warp (same space as the run's scan)")
        parser.add_argument("--kind", choices=("lesion", "image"), default="lesion")
        parser.add_argument("--out", default=None, help="output NIfTI (default: <run>/warps/<name>_space-<tpl>_res-1_desc-...)")
        _common_options(parser, with_run_options=False)
    else:
        parser.add_argument("--template", type=_template_argument, default="auto")
        _common_options(parser, with_run_options=False)
    return parser


def _run_options(args):
    return {
        "template": args.template,
        "template_image": args.template_image,
        "template_mask": args.template_mask,
        "template_head": args.template_head,
        "template_dirs": args.template_dir,
        "modality": args.modality,
        "quality": args.quality,
        "dilate_mm": args.dilate_mm,
        "moving_mask": args.moving_mask,
        "initial_transform": args.initial_transform,
        "tilt_search": args.tilt_search,
        "threads": args.threads,
        "brainsfit": args.brainsfit,
        "brainsresample": args.brainsresample,
        "brainsroiauto": args.brainsroiauto,
        "reuse": not args.no_reuse,
        "keep_work": args.keep_work,
    }


def _split_command(argv):
    argv = list(argv)
    if argv and argv[0] in COMMANDS:
        return argv[0], argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        return None, argv
    if argv[0].startswith("--"):
        return "run", argv
    return "", argv


def main(argv):
    """Command-line entry point (see the module documentation); events go to standard output."""
    command, rest = _split_command(argv)
    if command is None:
        print(__doc__, file=sys.stderr)
        if not argv:
            jobs.emit("error", code="usage", message=f"Give a command: {', '.join(COMMANDS)}")
            return jobs.EXIT_USAGE
        return jobs.EXIT_OK
    if command == "":
        jobs.emit("error", code="usage", message=f"Unknown command {rest[0]!r}; expected one of {', '.join(COMMANDS)}")
        return jobs.EXIT_USAGE
    args = _parser(command).parse_args(rest)
    cancel = jobs.CancelToken(args.cancel_file)
    if args.parent_pid:
        jobs.start_watchdogs(cancel, parent_pid=args.parent_pid, orphan=False)
    jobs.set_priority(args.nice)
    threads = jobs.budget_threads(args.threads)
    jobs.emit("start", job=JOB, command=command, argv=list(argv), threads=threads, parent_runs=args.parent_run)
    if command == "check":
        report, ok = check(args.template, args.template_dir, args.brainsfit, args.brainsresample, args.brainsroiauto)
        jobs.emit("result", kind="check", **report)
        if not ok:
            missing = [name for name in ("BRAINSFit", "BRAINSResample") if not report["executables"][name]["found"]]
            wanted = DEFAULT_TEMPLATE if args.template in (None, "", "auto") else args.template
            reason = (report["templates"].get(wanted) or {}).get("message")
            raise jobs.JobError(
                "Normalization is not available: " + (f"{', '.join(missing)} not found" if missing else (reason or "the template files are missing")),
                code="executable_missing" if missing else "template_missing",
            )
        jobs.emit("done", outputs=[], elapsed=round(jobs.elapsed(), 3), run_id=None)
        return jobs.EXIT_OK
    with jobs.KeepAwake():
        if command == "warp":
            result = warp(args.run, args.image, args.kind, args.out, args.template_dir, args.brainsresample, threads, cancel)
            jobs.emit("done", outputs=list(result["outputs"].values()), elapsed=round(jobs.elapsed(), 3), run_id=None)
            return jobs.EXIT_OK
        if command == "run":
            result = normalize(args.scan, args.lesion, args.out, subject=args.subject, anat=args.anat, force=args.force, cancel=cancel, **_run_options(args))
            jobs.emit("done", outputs=sorted(result["outputs"].values()), elapsed=round(jobs.elapsed(), 3), run_id=None, status=result["status"], suggestion=result["qc"].get("suggestion"))
            return jobs.EXIT_OK
        subjects = [item.strip() for item in args.subjects.split(",") if item.strip()] if args.subjects else None
        result = batch(args.study, subjects=subjects, force=args.force, cancel=cancel, **_run_options(args))
        jobs.emit("done", outputs=[entry.get("out") for entry in result["subjects"] if entry.get("out")], elapsed=round(jobs.elapsed(), 3), run_id=None, counts=result["counts"], template=result["template"])
        return jobs.EXIT_ERROR if result["counts"].get("failed") else jobs.EXIT_OK


def _events_mode(argv):
    mode = "auto"
    for index, arg in enumerate(argv):
        if arg == "--events" and index + 1 < len(argv):
            mode = argv[index + 1]
        elif arg.startswith("--events="):
            mode = arg.split("=", 1)[1]
    if mode == "auto":
        try:
            return "text" if sys.stdout.isatty() else "json"
        except (AttributeError, ValueError):
            return "json"
    return mode


if __name__ == "__main__":
    sys.stdout = _EventStream(sys.stdout, text=_events_mode(sys.argv[1:]) == "text")
    jobs.run_job(main, job=JOB)

"""Control-lesion sets: a registry of control-lesion folders, their cached maps per connectome, and study links.

Specificity tests and empirical nulls compare a study's lesions with 500-1000 control lesions (Ferguson 2019: 490;
Siddiqi 2026: 1,090). A control set is registered once, its lesions are mapped once per connectome, and any number of
studies link to it without copying a file::

    <Studio/LNM/DataDirectory>/ControlSets/<name>/
        controlset.json   {"format": "slicer-studio-controlset", "version": 1, "name", "source", "licence_note",
                           "space", "spaces", "grid_id", "grids", "family", "n", "dimensionality", "hemisphere",
                           "folder", "lesions": [{"id", "rel", "sha256", "volume_ml", "dimensionality", "hemisphere",
                           "left_share", "grid_id", "space"}], "skipped", "content_sha256", "created", "software"}
        lesions.tsv       participant_id, lesion, lesion_sha256 (the cohortmaps ``--lesions-tsv`` input)
        cache/            cohortmaps cache: conn-<label>_subj-<sha8>/<key>/ (the same keys as a study's cache)
        maps/             cohortmaps runs: conn-<label>_subj-<sha8>/maps.tsv, cohortmaps.json

``folder`` is the lesion folder relative to the set folder ("/" separators; absolute only when it is on another drive),
and each lesion's ``rel`` is relative to that folder. The lesion files stay where they are: nothing is copied.

**Registration** (:func:`register_folder`) scans the folder with the study recognisers (the whole-word lesion rule, the
anatomical-file rule and the subject id rule of ``Study.add_files``; headers only) and recognises the template grid of
each file (``geometry.detect_grid_candidates``). A grid that does not name its space (the TemplateFlow 2009 grids hold
2009c and 2009a images) is asked once: ``ask(question)`` answers it, or :class:`AmbiguousSpaceError` lists the questions.
Lesions of different template families are refused. Each lesion is then read once: its sha256, volume, dimensionality
(``lesionops.detect_dimensionality``) and hemisphere (``lesionops.side_check``'s rule) are recorded.

**Study links** (``Study.link_control_set``) add one row per control lesion with group = control, source_type =
control_db, source_dataset = <name>, source_id = <lesion id> and lesion = ``controlset:<name>/<rel>``.
``Study.abspath`` resolves such paths through the registry: ``Study.controlset_root``, else the environment variable
:data:`ENV_ROOT`, else the default data directory. study.json records ``control_sets: [{"name", "sha256"}]``, where
sha256 is :func:`content_sha256` of the set (its space and lesion hashes), so a changed set is detected.

**Maps** go to the set's own cache (:func:`maps_cache_root`): :func:`map_controls` (or the cohortmaps command line with
:func:`map_job_args`) maps the set once per connectome and subject list, and every study that links the set looks its
lesions up in the same cache (:func:`study_control_lesions` with ``cache_root=maps_cache_root(entry)``).

**Balance**: :func:`mismatch_report` compares a study's cases with the set (standardized mean differences of volume,
2D share and left hemisphere share, |SMD| > 0.25 flagged, and the template family); :func:`matched_subsample` picks a
seeded nearest-neighbour subsample of the set (``lesionset.matched_subsample``).

numpy only (no Slicer, VTK or Qt). The registry holds local state; studies hold only ``controlset:`` paths.
"""

import collections
import datetime
import hashlib
import json
import math
import os
import re
import shutil
import sys

FORMAT = "slicer-studio-controlset"
VERSION = 1
CONTROLSET_JSON = "controlset.json"
LESIONS_TSV = "lesions.tsv"
CACHE_DIR = "cache"
MAPS_DIR = "maps"
#: Folder of the registry in the data directory (``Studio/LNM/DataDirectory``)
REGISTRY_DIR = "ControlSets"
#: Environment variable with the registry folder (<DataDirectory>/ControlSets), read when Study.controlset_root is not set
ENV_ROOT = "SLICER_STUDIO_CONTROLSETS"
#: Table paths of control lesions: "controlset:<name>/<path relative to the set's lesion folder>"
PATH_PREFIX = "controlset:"
SOURCE_TYPE = "control_db"
GROUP = "control"
#: |SMD| above this is flagged (lesionset.SMD_FLAG)
SMD_FLAG = 0.25
#: Balance covariates of the mismatch report
MISMATCH_COVARIATES = ("volume_ml", "dim_2d", "hemisphere_left")
COVARIATE_LABELS = {"volume_ml": "Volume (mL)", "log_volume_ml": "log volume", "dim_2d": "2D share", "hemisphere_left": "Left hemisphere share"}
NO_GRID = "none"
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SPACE_ENTITY = re.compile(r"(?:^|_)space-([A-Za-z0-9]+)")

SKIPPED_UNREADABLE = "unreadable"
SKIPPED_NOT_3D = "not a 3D image"
SKIPPED_ANATOMICAL = "brain mask, deface mask or anatomical segmentation"
SKIPPED_NOT_LESION = "not a lesion mask (by its name)"
SKIPPED_DUPLICATE_ID = "another lesion has this id"
SKIPPED_NO_GRID = "not on a standard template grid (control lesions must be in a template space)"
SKIPPED_EMPTY = "empty lesion"


class AmbiguousSpaceError(ValueError):
    """The template space of a grid must be chosen by the user.

    :ivar questions: [{"grid_id", "candidates" (the hint first, when a file name names one), "n", "family", "resolution", "hint"}].
    """

    def __init__(self, questions, message=None):
        self.questions = list(questions)
        grids = ", ".join(f"{q['grid_id']} ({' / '.join(q['candidates'])})" for q in self.questions)
        super().__init__(message or f"Choose the template space of the lesions on {grids}")


# Registry location


def default_data_directory():
    """The default ``Studio/LNM/DataDirectory`` without Qt: /data/SlicerStudio in the web or Docker build, else ~/Documents/SlicerStudio."""
    if os.environ.get("SLICER_STUDIO_WEB") or os.path.exists("/.dockerenv"):
        return "/data/SlicerStudio"
    return os.path.join(_documents_directory(), "SlicerStudio")


def _documents_directory():
    """The Documents folder as Qt's QStandardPaths.DocumentsLocation gives it: XDG_DOCUMENTS_DIR of user-dirs.dirs on Linux
    (a localized folder, for example), else ~/Documents.
    """
    home = os.path.expanduser("~")
    if sys.platform.startswith("linux"):
        config = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        try:
            with open(os.path.join(config, "user-dirs.dirs"), encoding="utf-8") as f:
                for line in f:
                    match = re.match(r'\s*XDG_DOCUMENTS_DIR\s*=\s*"(.*)"\s*$', line)
                    if match:
                        value = match.group(1).replace("$HOME", home)
                        if value.startswith("/"):
                            return os.path.normpath(value)
        except OSError:
            pass
    return os.path.join(home, "Documents")


def registry_root(root=None):
    """The registry folder: ``root``, else the environment variable :data:`ENV_ROOT`, else <default data directory>/ControlSets."""
    if root:
        return os.path.abspath(os.fspath(root))
    value = os.environ.get(ENV_ROOT, "").strip()
    if value:
        return os.path.abspath(value)
    return os.path.join(default_data_directory(), REGISTRY_DIR)


def root_for_data_directory(data_directory):
    """<data directory>/ControlSets."""
    return os.path.join(os.fspath(data_directory), REGISTRY_DIR)


def validate_name(name):
    """A set name: letters, digits, "_", "." and "-", starting with a letter or digit, at most 64 characters."""
    name = str(name or "").strip()
    if not NAME_PATTERN.match(name) or name in (".", ".."):
        raise ValueError(f"Invalid control set name {name!r}: use letters, digits, '_', '.' or '-' (at most 64)")
    return name


def set_dir(name, root=None):
    """<registry>/<name>."""
    return os.path.join(registry_root(root), validate_name(name))


def _rel(path, folder):
    """``path`` relative to ``folder`` with "/" separators (absolute when on another drive)."""
    try:
        return os.path.relpath(path, folder).replace(os.sep, "/")
    except ValueError:
        return os.path.abspath(path)


def _join(folder, rel):
    rel = os.fspath(rel)
    if os.path.isabs(rel):
        return os.path.normpath(rel)
    return os.path.normpath(os.path.join(folder, *rel.split("/")))


def _now():
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _write_text_atomic(path, text):
    tmp_path = f"{os.fspath(path)}.{os.getpid()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


# Table paths


def controlset_path(name, rel):
    """The table path of a control lesion: "controlset:<name>/<rel>"."""
    return f"{PATH_PREFIX}{validate_name(name)}/{rel}"


def parse_path(value):
    """(name, rel) of a "controlset:<name>/<rel>" table path, or None for any other value."""
    if not isinstance(value, str) or not value.startswith(PATH_PREFIX):
        return None
    name, sep, rel = value[len(PATH_PREFIX):].partition("/")
    if not sep or not name or not rel:
        return None
    return name, rel


_entries_cache = {}  # controlset.json path: ((mtime_ns, size), entry)
#: What :func:`resolve_path` gives for an invalid set name (a missing file inside the registry)
INVALID_PATH = ".invalid-control-set-path"


def resolve_path(value, root=None):
    """Absolute path of a "controlset:<name>/<rel>" table path through the registry.

    A set that is not registered (on this computer) gives <registry>/<name>/<rel>, a path that does not exist, so callers
    report the lesion file as missing. Raises ValueError for a value that is not a control-set path.
    """
    parsed = parse_path(value)
    if parsed is None:
        raise ValueError(f"Not a control set path: {value!r}")
    name, rel = parsed
    registry = registry_root(root)
    invalid = os.path.join(registry, INVALID_PATH)  # a path that does not exist, inside the registry
    try:
        validate_name(name)
    except ValueError:
        return invalid
    try:
        entry = load(name, registry)
    except (OSError, ValueError):
        path = _join(os.path.join(registry, name), rel)
        return path if path.startswith(os.path.join(registry, name, "")) else invalid
    return _join(entry["folder_path"], rel)


# The registry


def content_sha256(entry):
    """sha256 of what a set's maps depend on: its space and its lesions' ids, relative paths and sha256 (canonical JSON)."""
    content = {"space": entry.get("space"), "lesions": [[lesion["id"], lesion["rel"], lesion["sha256"]] for lesion in entry.get("lesions", [])]}
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _read_entry(path):
    stat = os.stat(path)
    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _entries_cache.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    with open(path, encoding="utf-8") as f:
        entry = json.load(f)
    if not isinstance(entry, dict) or entry.get("format") != FORMAT:
        raise ValueError(f"Not a control set: {path}")
    if int(entry.get("version", 0)) > VERSION:
        raise ValueError(f"Control set version {entry.get('version')} is newer than supported ({VERSION})")
    if not isinstance(entry.get("lesions"), list):
        raise ValueError(f"{path} has no lesion list")
    _entries_cache[path] = (stamp, entry)
    return entry


def load(name, root=None):
    """The registry entry of a set (a dict as in the module docstring, plus "dir" and the absolute "folder_path").

    ``name`` may also be an entry (returned as it is). Raises FileNotFoundError for a set that is not registered.
    """
    if isinstance(name, dict):
        return name
    folder = set_dir(name, root)
    path = os.path.join(folder, CONTROLSET_JSON)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"No control set named {name!r} in {registry_root(root)}")
    entry = dict(_read_entry(path))
    entry["dir"] = folder
    entry["folder_path"] = _join(folder, entry.get("folder") or ".")
    return entry


def list_sets(root=None):
    """The registered sets (entries, sorted by name); unreadable entries are skipped."""
    registry = registry_root(root)
    out = []
    try:
        names = sorted(os.listdir(registry))
    except OSError:
        return out
    for name in names:
        if not NAME_PATTERN.match(name) or not os.path.isfile(os.path.join(registry, name, CONTROLSET_JSON)):
            continue
        try:
            out.append(load(name, registry))
        except (OSError, ValueError):
            continue
    return out


def unregister(name, root=None):
    """Remove a set from the registry (its controlset.json, lesions.tsv and map cache). The lesion files are not touched."""
    folder = set_dir(name, root)
    if not os.path.isfile(os.path.join(folder, CONTROLSET_JSON)):
        raise FileNotFoundError(f"No control set named {name!r}")
    _entries_cache.pop(os.path.join(folder, CONTROLSET_JSON), None)
    shutil.rmtree(folder)


def set_lesions(entry, ids=None):
    """[(participant id, absolute lesion path, sha256)] of a set (all lesions, or those whose id is in ``ids``)."""
    entry = load(entry)
    wanted = None if ids is None else set(ids)
    return [(lesion["id"], _join(entry["folder_path"], lesion["rel"]), lesion["sha256"]) for lesion in entry["lesions"] if wanted is None or lesion["id"] in wanted]


# Scanning and registration


def _iter_files(folder):
    for current, subfolders, files in os.walk(folder):
        subfolders[:] = sorted(d for d in subfolders if not d.startswith("."))
        for name in sorted(files):
            yield os.path.join(current, name)


def _space_hint(stem):
    match = _SPACE_ENTITY.search(stem)
    return match.group(1) if match else None


def scan_folder(folder, lesion_rule=None, id_regex=None):
    """Header-only scan of a folder of control lesions with the study recognisers and the grid registry.

    :param lesion_rule: whole-word rule of lesion file names (default: the study default). When no file matches it,
      every 3D image that is not an anatomical file is taken as a lesion (``all_lesions`` is then True).
    :param id_regex: subject id rule (default: the study default, "sub-<id>"; other names use the study's fallback).
    :return: {"folder", "lesions": [{"id", "path", "rel", "grid_id", "candidates", "family", "ambiguous", "hint"}],
      "skipped": [{"rel", "reason"}], "grids": {grid id or "none": {"candidates", "ambiguous", "family", "resolution",
      "n", "hints": {space: count}}}, "all_lesions"}.
    """
    from .geometry import detect_grid_candidates
    from .nifti import read_nifti_header
    from .study import _ANATOMICAL, DEFAULT_RULES, Study, _nifti_stem, participant_label, whole_word_rule

    folder = os.path.abspath(os.fspath(folder))
    if not os.path.isdir(folder):
        raise NotADirectoryError(f"Not a folder: {folder}")
    lesion_re = whole_word_rule(lesion_rule or DEFAULT_RULES["lesion"])
    id_re = re.compile(id_regex or DEFAULT_RULES["id"])
    skipped, named, others = [], [], []
    for path in _iter_files(folder):
        stem = _nifti_stem(path)
        if stem is None:
            continue
        rel = _rel(path, folder)
        try:
            header = read_nifti_header(path)
        except (OSError, ValueError) as exc:
            skipped.append({"rel": rel, "reason": f"{SKIPPED_UNREADABLE}: {exc}"})
            continue
        shape = tuple(header["shape"])
        if len(shape) > 3 and any(int(n) != 1 for n in shape[3:]):
            skipped.append({"rel": rel, "reason": SKIPPED_NOT_3D})
            continue
        if _ANATOMICAL.search(stem) and "lesion" not in stem.lower():
            skipped.append({"rel": rel, "reason": SKIPPED_ANATOMICAL})
            continue
        try:
            sid = participant_label(Study._subject_id(path, stem, id_re, lesion_re))
        except ValueError:
            sid = participant_label(re.sub(r"[^A-Za-z0-9]", "", stem) or "lesion")
        match = detect_grid_candidates(shape[:3], header["affine"])
        item = {
            "id": sid,
            "path": path,
            "rel": rel,
            "grid_id": match.grid_id if match else None,
            "candidates": list(match.candidates) if match else [],
            "family": match.family if match else None,
            "ambiguous": bool(match.ambiguous) if match else False,
            "resolution": match.resolution if match else None,
            "hint": _space_hint(stem),
        }
        (named if lesion_re.search(stem) else others).append(item)
    all_lesions = not named
    lesions = others if all_lesions else named
    if not all_lesions:
        skipped += [{"rel": item["rel"], "reason": SKIPPED_NOT_LESION} for item in others]
    seen, unique = set(), []
    for item in lesions:
        if item["id"] in seen:
            skipped.append({"rel": item["rel"], "reason": f"{SKIPPED_DUPLICATE_ID} ({item['id']})"})
            continue
        seen.add(item["id"])
        unique.append(item)
    grids = {}
    for item in unique:
        key = item["grid_id"] or NO_GRID
        grid = grids.setdefault(key, {"candidates": item["candidates"], "ambiguous": item["ambiguous"], "family": item["family"], "resolution": item["resolution"], "n": 0, "hints": {}})
        grid["n"] += 1
        if item["hint"]:
            grid["hints"][item["hint"]] = grid["hints"].get(item["hint"], 0) + 1
    return {"folder": folder, "lesions": unique, "skipped": skipped, "grids": grids, "all_lesions": all_lesions}


def questions(scan, answers=None):
    """The unanswered ambiguous grids of a scan: [{"grid_id", "candidates" (a file-name hint first), "n", "family", "resolution", "hint"}]."""
    answers = dict(answers or {})
    out = []
    for grid_id, grid in scan["grids"].items():
        if grid_id == NO_GRID or not grid["ambiguous"] or grid_id in answers:
            continue
        hint = max(grid["hints"], key=grid["hints"].get) if grid["hints"] else None
        candidates = list(grid["candidates"])
        if hint in candidates:
            candidates.remove(hint)
            candidates.insert(0, hint)
        out.append({"grid_id": grid_id, "candidates": candidates, "n": grid["n"], "family": grid["family"], "resolution": grid["resolution"], "hint": hint})
    return out


def _resolve_spaces(scan, space, ask):
    """{grid id: space} of the scan's grids ("none": the explicit space or None)."""
    from .geometry import same_family, space_family

    answers = dict(space) if isinstance(space, dict) else {}
    explicit = space if isinstance(space, str) and space else None
    if explicit is not None:
        for grid_id, grid in scan["grids"].items():
            if grid_id == NO_GRID or explicit in grid["candidates"]:
                continue
            if not any(same_family(explicit, candidate) for candidate in grid["candidates"]):
                raise ValueError(f"{grid['n']} lesions are on the {grid_id} grid ({space_family(grid['candidates'][0])} family), not in {explicit}")
    resolved = {}
    for grid_id, grid in scan["grids"].items():
        if grid_id == NO_GRID:
            resolved[grid_id] = answers.get(NO_GRID) or explicit
        elif grid_id in answers:
            resolved[grid_id] = answers[grid_id]
        elif explicit is not None:
            resolved[grid_id] = explicit
        elif not grid["ambiguous"]:
            resolved[grid_id] = grid["candidates"][0]
    pending = [question for question in questions(scan, resolved) if question["grid_id"] not in resolved]
    for question in pending:
        answer = ask(question) if ask is not None else None
        if not answer:
            raise AmbiguousSpaceError(pending)
        if answer not in question["candidates"]:
            raise ValueError(f"{answer!r} is not a space of the {question['grid_id']} grid ({', '.join(question['candidates'])})")
        resolved[question["grid_id"]] = answer
    return resolved


def _lesion_stats(data, affine):
    """{"volume_ml", "dimensionality", "hemisphere", "left_share"} of a lesion image (None values for an empty lesion)."""
    import numpy as np

    from . import lesionops

    empty = {"volume_ml": 0.0, "dimensionality": None, "hemisphere": None, "left_share": None}
    affine = np.asarray(affine, dtype=np.float64)
    data = np.asarray(data)
    # Crop to the bounding box of the non-zero voxels (the affine moves its origin to the box's first voxel): the values do
    # not change, and dimensionality and side, which work on voxel indices through the affine, are computed on far fewer voxels
    occupied = data != 0
    if np.issubdtype(data.dtype, np.floating):
        occupied &= np.isfinite(data)
    if data.ndim != 3 or not occupied.any():
        return empty
    lo, hi = [], []
    for axis in range(3):
        indices = np.flatnonzero(occupied.any(axis=tuple(other for other in range(3) if other != axis)))
        lo.append(int(indices[0]))
        hi.append(int(indices[-1]) + 1)
    values = np.asarray(data[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]], dtype=np.float32)
    affine = affine.copy()
    affine[:3, 3] = affine[:3, :3] @ np.asarray(lo, dtype=np.float64) + affine[:3, 3]
    values = np.where(np.isfinite(values), values, 0.0)
    fraction = np.clip(values, 0.0, 1.0) if float(values.max()) <= 1.0 else (values > 0).astype(np.float32)
    voxel_ml = abs(float(np.linalg.det(affine[:3, :3]))) / 1000.0
    volume = float(fraction.sum()) * voxel_ml
    if volume <= 0:
        return empty
    dimensionality = lesionops.detect_dimensionality(fraction, affine)["dimensionality"]
    side = lesionops.side_check(fraction, affine, None)
    traced = side["traced_side"]
    hemisphere = traced if traced in ("L", "R") else "bilateral"  # the hemisphere column has no "midline" level
    return {"volume_ml": round(volume, 4), "dimensionality": dimensionality, "hemisphere": hemisphere, "left_share": round(float(side["left_frac"]), 4)}


def _tally(values):
    counts = collections.Counter("unknown" if value is None else str(value) for value in values)
    return dict(sorted(counts.items()))


def _check_cancel(cancel, done):
    if cancel is not None and (cancel() if callable(cancel) else cancel.is_set()):
        from .jobs import Cancelled

        raise Cancelled(done=done)


def _read_lesion(item, space):
    """The registry record of a scanned lesion (its file is read once), or the reason it is skipped."""
    from .nifti import read_nifti

    try:
        with open(item["path"], "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
        image, affine, _header = read_nifti(item["path"])
    except (OSError, ValueError, EOFError) as exc:
        return f"{SKIPPED_UNREADABLE}: {exc}"
    if image.ndim > 3:
        image = image.reshape(image.shape[:3])
    stats = _lesion_stats(image, affine)
    if not stats["volume_ml"]:
        return SKIPPED_EMPTY
    return {"id": item["id"], "rel": item["rel"], "sha256": sha, **stats, "grid_id": item["grid_id"], "space": space}


def register_folder(folder, name, source=None, licence_note=None, space=None, ask=None, root=None, lesion_rule=None, id_regex=None, replace=False, scan=None, progress=None, cancel=None):
    """Register a folder of control lesions as the set ``name`` (no file is copied).

    :param source: where the lesions come from (a citation, URL or dataset name).
    :param licence_note: the licence or data-use agreement note shown with the set.
    :param space: a space for every lesion, or {grid id: space} answers (an ambiguous grid needs one).
    :param ask: ``ask(question) -> space or None`` for each ambiguous grid without an answer (see :func:`questions`);
      without ``ask`` (or when it returns None) :class:`AmbiguousSpaceError` is raised and nothing is written.
    :param replace: replace a registered set of this name (its map cache is kept: entries are keyed by content).
    :param scan: a :func:`scan_folder` result to use instead of scanning again.
    :param progress: optional ``progress(done, total)``; ``cancel``: optional callable or Event (``jobs.Cancelled`` is raised).
    :return: the registry entry (see :func:`load`).
    :raises FileExistsError: a set of this name exists and ``replace`` is False.
    :raises ValueError: no usable lesion, lesions of different template families, or a space that the grid cannot hold.
    """
    from .export import LIBRARY_VERSION
    from .geometry import space_family

    name = validate_name(name)
    target = set_dir(name, root)
    json_path = os.path.join(target, CONTROLSET_JSON)
    if os.path.exists(json_path) and not replace:
        raise FileExistsError(f"A control set named {name!r} is already registered")
    scan = scan if scan is not None else scan_folder(folder, lesion_rule, id_regex)
    folder = scan["folder"]
    spaces = _resolve_spaces(scan, space, ask)
    skipped = list(scan["skipped"])
    usable = []
    for item in scan["lesions"]:
        item_space = spaces.get(item["grid_id"] or NO_GRID)
        if not item_space:
            skipped.append({"rel": item["rel"], "reason": SKIPPED_NO_GRID})
            continue
        usable.append((item, item_space))
    if not usable:
        raise ValueError(f"No usable control lesion in {folder}")
    families = {space_family(item_space) for _item, item_space in usable}
    if len(families) > 1:
        raise ValueError(f"The lesions are in spaces of different template families ({', '.join(sorted(families))}); register one set per family")
    lesions = []
    total = len(usable)
    for done, (item, item_space) in enumerate(usable):
        _check_cancel(cancel, done)
        lesion = _read_lesion(item, item_space)
        if isinstance(lesion, str):
            skipped.append({"rel": item["rel"], "reason": lesion})
        else:
            lesions.append(lesion)
        if progress is not None:
            progress(done + 1, total)
    if not lesions:
        raise ValueError(f"No usable control lesion in {folder}")
    space_counts = collections.Counter(lesion["space"] for lesion in lesions)
    grid_counts = collections.Counter(lesion["grid_id"] or NO_GRID for lesion in lesions)
    entry = {
        "format": FORMAT,
        "version": VERSION,
        "name": name,
        "source": source or "",
        "licence_note": licence_note or "",
        "space": space_counts.most_common(1)[0][0],
        "spaces": dict(sorted(space_counts.items())),
        "grid_id": grid_counts.most_common(1)[0][0],
        "grids": dict(sorted(grid_counts.items())),
        "family": families.pop(),
        "n": len(lesions),
        "dimensionality": _tally(lesion["dimensionality"] for lesion in lesions),
        "hemisphere": _tally(lesion["hemisphere"] for lesion in lesions),
        "folder": _rel(folder, target),
        "all_lesions": bool(scan.get("all_lesions")),
        "lesions": lesions,
        "skipped": skipped,
        "created": _now(),
        "software": {"library": LIBRARY_VERSION},
    }
    entry["content_sha256"] = content_sha256(entry)
    os.makedirs(target, exist_ok=True)
    rows = "".join(f"{lesion['id']}\t{_rel(_join(folder, lesion['rel']), target)}\t{lesion['sha256']}\n" for lesion in lesions)
    _write_text_atomic(os.path.join(target, LESIONS_TSV), "participant_id\tlesion\tlesion_sha256\n" + rows)
    _write_text_atomic(json_path, json.dumps(entry, indent=1) + "\n")
    _entries_cache.pop(json_path, None)
    return load(name, root)


# Maps (one cohortmaps cache per set, keyed as a study's)


def maps_cache_root(entry, root=None):
    """<set>/cache: the cohortmaps cache root of a set (``cohortmaps.run(cache_root=...)``)."""
    return os.path.join(load(entry, root)["dir"], CACHE_DIR)


def _subject_ids(bundle, subjects=None, random_subset=False, seed=0, subject_list=None):
    from . import engine

    if subject_list is not None:
        return list(bundle.resolve_subject_list(subject_list).ids)
    return engine.resolve_subjects(bundle, subjects, random_subset, seed)


def _bundle(bundle):
    from .connectome import ConnectomeBundle

    return ConnectomeBundle.open(bundle) if isinstance(bundle, (str, os.PathLike)) else bundle


def maps_cache_dir(entry, bundle, subjects=None, random_subset=False, seed=0, subject_list=None, root=None):
    """The cache folder of a set for a connectome and subject list: <set>/cache/conn-<label>_subj-<sha8> (cohortmaps' scheme)."""
    from . import cohortmaps
    from .export import subjects_sha256

    bundle = _bundle(bundle)
    ids = _subject_ids(bundle, subjects, random_subset, seed, subject_list)
    return cohortmaps.cache_dir_for(maps_cache_root(entry, root), bundle.name, subjects_sha256(ids))


def maps_out_dir(entry, bundle, subjects=None, random_subset=False, seed=0, subject_list=None, root=None):
    """<set>/maps/conn-<label>_subj-<sha8>: where :func:`map_controls` writes maps.tsv and cohortmaps.json."""
    return os.path.join(load(entry, root)["dir"], MAPS_DIR, os.path.basename(maps_cache_dir(entry, bundle, subjects, random_subset, seed, subject_list, root)))


def cache_status(entry, bundle, root=None, **options):
    """Cache lookup of a set (no computation): ``cohortmaps.lookup``'s dict (n, hits, new, cache_dir, ...)."""
    from . import cohortmaps

    entry = load(entry, root)
    return cohortmaps.lookup(set_lesions(entry), bundle, cache_root=maps_cache_root(entry), **options)


def map_controls(entry, bundle, out_dir=None, root=None, emit=None, cancel=None, **options):
    """Map a set's lesions with a connectome into the set's cache (``cohortmaps.run``); return the cohortmaps.json record.

    ``options`` are passed to ``cohortmaps.run`` (subjects, subject_list, budget_bytes, threads, ...). Lesions already in
    the cache are not computed again.
    """
    from . import cohortmaps

    entry = load(entry, root)
    if out_dir is None:
        out_dir = maps_out_dir(entry, bundle, options.get("subjects"), options.get("random_subset", False), options.get("seed", 0), options.get("subject_list"))
    return cohortmaps.run(set_lesions(entry), bundle, out_dir, cache_root=maps_cache_root(entry), run_root=entry["dir"], emit=emit, cancel=cancel, **options)


def map_job_args(entry, bundle_path, out_dir=None, root=None):
    """Arguments of ``python -m LesionNetworkMappingLib.cohortmaps`` that map a set into its cache (the GUI's job)."""
    entry = load(entry, root)
    out_dir = out_dir or maps_out_dir(entry, bundle_path)
    return ["--lesions-tsv", os.path.join(entry["dir"], LESIONS_TSV), "--bundle", os.fspath(bundle_path), "--out", out_dir,
            "--cache", maps_cache_root(entry), "--run-root", entry["dir"]]


# Studies


def study_participant_id(name, lesion_id):
    """The participant_id of a control lesion in a study: "sub-<set name letters and digits><lesion id label>"."""
    from .study import participant_label

    label = lesion_id[4:] if str(lesion_id).startswith("sub-") else str(lesion_id)
    return participant_label(re.sub(r"[^A-Za-z0-9]", "", name) + label)


def is_control_row(row, name=None):
    """True for a study row linked from a control set (``name``: from that set)."""
    if row.get("source_type") != SOURCE_TYPE:
        return False
    parsed = parse_path(row.get("lesion"))
    if parsed is None:
        return False
    return name is None or parsed[0] == name


def linked_rows(study, name=None):
    """The study rows linked from control sets (from the set ``name``)."""
    return [row for row in study.subjects if is_control_row(row, name)]


def study_control_lesions(study, name, root=None):
    """[(participant id, absolute path, sha256)] of the study's rows linked from the set ``name`` (for cohortmaps with
    ``cache_root=maps_cache_root(name)``: the set's cached maps are reused by every study).
    """
    entry = load(name, root if root is not None else getattr(study, "controlset_root", None))
    by_id = {lesion["id"]: lesion for lesion in entry["lesions"]}
    out = []
    for row in linked_rows(study, entry["name"]):
        lesion = by_id.get(row.get("source_id"))
        if lesion is not None:
            out.append((row["participant_id"], _join(entry["folder_path"], lesion["rel"]), lesion["sha256"]))
    return out


def lesion_covariates(entry, ids=None):
    """{lesion id: {"volume_ml", "log_volume_ml", "dim_2d", "hemisphere_left"}} of a set's lesions (``lesionset`` names)."""
    entry = load(entry)
    wanted = None if ids is None else set(ids)
    return {lesion["id"]: _covariates(lesion.get("volume_ml"), lesion.get("dimensionality"), lesion.get("hemisphere")) for lesion in entry["lesions"] if wanted is None or lesion["id"] in wanted}


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _covariates(volume, dimensionality, hemisphere):
    from .study import is_missing

    volume = _number(volume)
    dimensionality = None if dimensionality is None or is_missing(dimensionality) else str(dimensionality)
    hemisphere = None if hemisphere is None or is_missing(hemisphere) else str(hemisphere)
    return {
        "volume_ml": volume,
        "log_volume_ml": math.log(volume) if volume else None,
        "dim_2d": None if dimensionality is None else (1.0 if dimensionality in ("2D", "multi2D") else 0.0),
        "hemisphere_left": None if hemisphere is None else (1.0 if hemisphere == "L" else 0.0),
    }


def case_rows(study):
    """The study's included rows to compare with a control set: group = case when the study has any, else every included
    row that is not linked from a control set.
    """
    from .study import _included

    rows = [row for row in study.subjects if _included(row) and not is_control_row(row)]
    cases = [row for row in rows if row.get("group") == "case"]
    return cases if cases else [row for row in rows if row.get("group") != "control"]


def _stats_key(path):
    """The ``cache`` key of a lesion file's values: (path, mtime, size), or None when the file is missing."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (path, stat.st_mtime_ns, stat.st_size)


def _file_stats(path, cache=None):
    """:func:`_lesion_stats` of a lesion file ({} when it cannot be read), kept in ``cache`` by path, mtime and size."""
    from .nifti import read_nifti

    key = _stats_key(path)
    if key is None:
        return {}
    if cache is not None and key in cache:
        return cache[key]
    try:
        image, affine, _header = read_nifti(path)
        stats = _lesion_stats(image.reshape(image.shape[:3]) if image.ndim > 3 else image, affine)
    except (OSError, ValueError, EOFError):
        stats = {}
    if cache is not None:
        cache[key] = stats
    return stats


def _row_needs_file(row):
    from .study import is_missing

    return any(value is None or is_missing(value) for value in (row.get("volume_ml"), row.get("dimensionality"), row.get("hemisphere")))


def missing_case_files(study, cache=None):
    """Lesion files of the study's cases whose row lacks volume_ml, dimensionality or hemisphere and whose values ``cache``
    does not hold yet: what :func:`study_case_covariates` would read (for a caller that reads them in a worker thread with
    :func:`read_case_stats` first).
    """
    out = []
    for row in case_rows(study):
        if not _row_needs_file(row):
            continue
        path = study.abspath(row.get("lesion"))
        key = _stats_key(path) if path and os.path.isfile(path) else None
        if key is not None and (cache is None or key not in cache) and path not in out:
            out.append(path)
    return out


def read_case_stats(paths, progress=None, cancel=None):
    """{cache key: lesion values} of lesion files (see :func:`missing_case_files`): merge the result into the ``cache`` of
    :func:`study_case_covariates` / :func:`mismatch_report`. Safe in a worker thread (it changes nothing shared).

    :param progress: optional callable(done, total).
    :param cancel: optional callable or threading.Event; raises ``jobs.Cancelled`` when set.
    """
    out = {}
    for index, path in enumerate(paths):
        _check_cancel(cancel, index)
        _file_stats(path, out)
        if progress is not None:
            progress(index + 1, len(paths))
    return out


def study_case_covariates(study, compute_missing=True, cache=None):
    """{participant id: covariates} of the study's cases from its columns (volume_ml, dimensionality, hemisphere); a missing
    value is computed from the lesion file when ``compute_missing`` (the file is read; the table is not changed). ``cache``
    (a dict) keeps the values computed from files for later calls.
    """
    from .study import is_missing

    out = {}
    for row in case_rows(study):
        volume, dimensionality, hemisphere = row.get("volume_ml"), row.get("dimensionality"), row.get("hemisphere")
        if compute_missing and _row_needs_file(row):
            path = study.abspath(row.get("lesion"))
            if path and os.path.isfile(path):
                stats = _file_stats(path, cache)
                volume = stats.get("volume_ml") if is_missing(volume) else volume
                dimensionality = stats.get("dimensionality") if is_missing(dimensionality) else dimensionality
                hemisphere = stats.get("hemisphere") if is_missing(hemisphere) else hemisphere
        out[row["participant_id"]] = _covariates(volume, dimensionality, hemisphere)
    return out


def matched_subsample(entry, cases, covariates=("volume_ml",), seed=0, ratio=1, caliper=None, root=None):
    """A seeded nearest-neighbour subsample of a set matched to the cases (``lesionset.matched_subsample``).

    :param cases: a ``Study`` (its :func:`case_rows`) or {case id: {covariate: value}}.
    :return: lesionset's result with "controls" (the matched lesion ids of the set, in pair order) added; "sids" mixes
      case ids and lesion ids as lesionset returns them.
    """
    from . import lesionset

    entry = load(entry, root)
    if not isinstance(cases, dict):
        cases = study_case_covariates(cases)
    controls = lesion_covariates(entry)
    case_ids = {f"case:{sid}": values for sid, values in cases.items()}
    control_ids = {f"control:{sid}": values for sid, values in controls.items()}
    result = lesionset.matched_subsample(case_ids, control_ids, list(covariates), seed=seed, ratio=ratio, caliper=caliper)

    def strip(value):
        return value.split(":", 1)[1]

    result["pairs"] = [[strip(case), strip(control), distance] for case, control, distance in result["pairs"]]
    result["sids"] = sorted(strip(value) for value in result["sids"])
    result["unmatched_cases"] = [strip(value) for value in result["unmatched_cases"]]
    result["missing"] = [strip(value) for value in result["missing"]]
    result["controls"] = [pair[1] for pair in result["pairs"]]
    result["set"] = entry["name"]
    return result


def mismatch_report(entry, study, flag=SMD_FLAG, compute_missing=True, root=None, cache=None):
    """How a set differs from a study's cases: SMD of volume, 2D share and left hemisphere share, and the template family.

    The controls are the set's lesions that the study links (all of the set when it links none). Case values missing from
    the table are computed from the lesion files when ``compute_missing`` (see :func:`study_case_covariates`, ``cache``).

    :return: {"name", "n_cases", "n_controls", "covariates": {name: {"smd", "flagged", "case_mean", "control_mean",
      "label"}}, "family": {"set", "study", "set_space", "study_space", "differ"}, "flagged", "warnings": [text]}.
    """
    from . import lesionset
    from .geometry import space_family
    from .study import is_missing

    entry = load(entry, root if root is not None else getattr(study, "controlset_root", None))
    linked = [row.get("source_id") for row in linked_rows(study, entry["name"])]
    controls = lesion_covariates(entry, linked or None)
    cases = study_case_covariates(study, compute_missing, cache)
    report = {"name": entry["name"], "n_cases": len(cases), "n_controls": len(controls), "covariates": {}, "warnings": []}
    for name in MISMATCH_COVARIATES:
        a = [values[name] for values in cases.values()]
        b = [values[name] for values in controls.values()]
        value = lesionset.smd(a, b, binary=name != "volume_ml")
        finite_a = [v for v in a if v is not None]
        finite_b = [v for v in b if v is not None]
        flagged = bool(value is not None and abs(value) > flag)
        report["covariates"][name] = {
            "smd": value,
            "flagged": flagged,
            "case_mean": sum(finite_a) / len(finite_a) if finite_a else None,
            "control_mean": sum(finite_b) / len(finite_b) if finite_b else None,
            "label": COVARIATE_LABELS[name],
        }
        if flagged:
            report["warnings"].append(f"{COVARIATE_LABELS[name]}: SMD {value:+.2f} (|SMD| > {flag:g})")
    study_space = study.space
    if not study_space:
        spaces = collections.Counter(row.get("space") for row in case_rows(study) if not is_missing(row.get("space")))
        study_space = spaces.most_common(1)[0][0] if spaces else None
    set_family, study_family = entry.get("family") or space_family(entry.get("space")), space_family(study_space)
    differ = bool(study_space) and set_family != study_family
    report["family"] = {"set": set_family, "study": study_family, "set_space": entry.get("space"), "study_space": study_space, "differ": differ}
    if differ:
        report["warnings"].append(f"Template family: the set is {entry.get('space')} ({set_family}), the study {study_space} ({study_family})")
    report["flagged"] = bool(report["warnings"])
    return report

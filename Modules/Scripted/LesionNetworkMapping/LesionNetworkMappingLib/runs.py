"""Run registry and provenance graph of lesion network mapping results (standard library only).

Every run (one lesion map, cohort maps, a GLM, nulls, validation, a figure, a report, ...) is
recorded in ``<root>/derivatives/lnm/runs.jsonl``, where ``root`` is a study folder or any output
root. The file is append-only JSON Lines; it is created on demand. Two kinds of records::

    {"op": "register", "id", "kind", "dir", "parents", "params", "params_sha256", "seeds",
     "status", "created", "software", "summary"}
    {"op": "update", "id", "updated", <fields>}   (for example status, summary, outputs, ended)

``load()`` folds them into {id: record}: later updates win, field by field. ``dir`` is relative to
the root (with "/" separators) when the run folder lies inside it, absolute otherwise. ``parents``
are the ids of the runs this run derives from (a GLM's cohort maps, a figure's GLM, ...), which
makes the registry a provenance graph (``lineage()``, ``children()``).

Concurrency: every record is written with one ``os.write`` of one encoded line on an O_APPEND
descriptor, at most ``MAX_RECORD_BYTES`` long (a larger summary or parameter set goes to a file in
the run folder, referenced as {"$file": path relative to the root}); POSIX also holds an advisory
flock and Windows an msvcrt lock file around the write. Readers skip malformed lines with a
warning, so a crashed writer never makes the registry unreadable.

Values are stored as strict JSON: NaN and infinite numbers (for example an undefined AUC) become
null, numpy scalars and arrays become numbers and lists. ``params_sha256`` is computed from the
same normalized parameters, so it matches the stored ones.
"""

import datetime
import functools
import hashlib
import json
import math
import os
import re
import secrets
import sys
import time
import warnings

REGISTRY_RELPATH = ("derivatives", "lnm", "runs.jsonl")
MAX_RECORD_BYTES = 64 * 1024

KINDS = [
    "map",
    "cohortmaps",
    "glm",
    "slnm",
    "validation",
    "nulls",
    "robustness",
    "reference",
    "preflight",
    "figure",
    "report",
    "calibration",
    "parity",
    "fastindex",
    "vlsm",
    "circuits",
    "export",
    "normalization",
    "pipeline",
]
STATUSES = ("running", "done", "failed", "cancelled")

_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9]*$")
_SPILL_KEYS = ("summary", "params", "seeds")
_IS_WINDOWS = sys.platform == "win32"


class RegistryWarning(UserWarning):
    """A record of runs.jsonl could not be read (it is skipped)."""


def register_kind(kind):
    """Add a run kind (lowercase letters and digits) to ``KINDS``; for jobs added after this module."""
    if not _KIND_PATTERN.match(kind or ""):
        raise ValueError(f"Invalid run kind {kind!r}: use lowercase letters and digits")
    if kind not in KINDS:
        KINDS.append(kind)
    return kind


def registry_path(root):
    """Path of the registry file of ``root`` (it may not exist yet)."""
    return os.path.join(os.fspath(root), *REGISTRY_RELPATH)


def _utc_now():
    return datetime.datetime.now(datetime.UTC)


def _iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def new_run_id(kind):
    """A new run id: ``<kind>-YYYYmmddTHHMMSS-xxxx`` (UTC time and 4 random hexadecimal digits)."""
    _check_kind(kind)
    return f"{kind}-{_utc_now():%Y%m%dT%H%M%S}-{secrets.token_hex(2)}"


def _check_kind(kind):
    if kind not in KINDS:
        raise ValueError(f"Unknown run kind {kind!r} (known: {', '.join(KINDS)})")


def _json_default(value):
    if hasattr(value, "item") and callable(value.item):  # numpy scalars
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    if hasattr(value, "tolist") and callable(value.tolist):
        return value.tolist()
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _finite(value):
    """``value`` with NaN and infinite floats (numpy ones too) replaced by None and dictionary keys JSON can hold."""
    if isinstance(value, float):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key if key is None or isinstance(key, (str, int, float, bool)) else str(key): _finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(item) for item in value]
    return value


def _finite_default(value):
    return _finite(_json_default(value))


def _dumps(obj, **kwargs):
    """Strict JSON of ``obj``: numpy values, paths and sets converted; NaN and infinities become null."""
    return json.dumps(_finite(obj), ensure_ascii=False, allow_nan=False, default=_finite_default, **kwargs)


def canonical_json(obj):
    """Canonical JSON text of ``obj``: sorted keys, no spaces, UTF-8 characters kept, NaN and infinities as null."""
    return _dumps(obj, sort_keys=True, separators=(",", ":"))


def params_sha256(params):
    """SHA-256 (hex) of the canonical JSON of ``params``; equal parameter sets give equal hashes."""
    return hashlib.sha256(canonical_json(params).encode("utf-8")).hexdigest()


@functools.cache
def _software():
    from .export import versions

    return versions()


def software():
    """Versions of Python, numpy, scipy, BLAS and this library (export.versions(), computed once per process)."""
    try:
        return dict(_software())
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _relative_dir(root, folder):
    if folder is None:
        return None
    folder = os.fspath(folder)
    root_abs = os.path.realpath(os.path.abspath(os.fspath(root)))
    folder_abs = os.path.realpath(os.path.abspath(folder if os.path.isabs(folder) else os.path.join(os.fspath(root), folder)))
    try:
        inside = os.path.commonpath([root_abs, folder_abs]) == root_abs
    except ValueError:  # different drives
        inside = False
    if not inside:
        return folder_abs
    relative = os.path.relpath(folder_abs, root_abs)
    return relative.replace(os.sep, "/")


def run_dir(root, record):
    """Absolute folder of a run record (None when it has none)."""
    folder = record.get("dir") if record else None
    if not folder:
        return None
    return os.path.normpath(folder if os.path.isabs(folder) else os.path.join(os.fspath(root), *folder.split("/")))


def _encode(record):
    return (_dumps(record, separators=(",", ":")) + "\n").encode("utf-8")


def _spill(root, record, folder):
    """Move the largest spillable fields of ``record`` to files in ``folder`` until the line fits."""
    data = _encode(record)
    for key in sorted((key for key in _SPILL_KEYS if record.get(key) is not None), key=lambda key: -len(_encode({key: record[key]}))):
        if len(data) <= MAX_RECORD_BYTES:
            break
        if folder is None:
            raise ValueError(f"Run record of {record.get('id')} is larger than {MAX_RECORD_BYTES} bytes and the run has no folder for its {key}")
        os.makedirs(folder, exist_ok=True)
        name = f"{record['id']}_{key}_{secrets.token_hex(2)}.json"
        path = os.path.join(folder, name)
        tmp_path = f"{path}.{os.getpid()}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(_dumps(record[key], indent=1))
        os.replace(tmp_path, path)
        record[key] = {"$file": _relative_dir(root, path)}
        data = _encode(record)
    if len(data) > MAX_RECORD_BYTES:
        raise ValueError(f"Run record of {record.get('id')} is larger than {MAX_RECORD_BYTES} bytes")
    return data


def _lock(fd):
    """Advisory exclusive lock of the open registry (POSIX flock); a no-op where unsupported."""
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)
        return True
    except (ImportError, OSError):
        return False


def _unlock(fd):
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
    except (ImportError, OSError):
        pass


def _append(root, data):
    """Append one encoded line to the registry with a single os.write on an O_APPEND descriptor."""
    path = registry_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    win_lock = None
    if _IS_WINDOWS:
        import msvcrt

        win_lock = os.open(path + ".lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
        while True:
            try:
                msvcrt.locking(win_lock, msvcrt.LK_LOCK, 1)  # retries for 10 s, then raises
                break
            except OSError:
                time.sleep(0.05)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
        try:
            locked = not _IS_WINDOWS and _lock(fd)
            try:
                written = os.write(fd, data)
            finally:
                if locked:
                    _unlock(fd)
            if written != len(data):
                raise OSError(f"Partial write to {path} ({written} of {len(data)} bytes)")
        finally:
            os.close(fd)
    finally:
        if win_lock is not None:
            import msvcrt

            try:
                os.lseek(win_lock, 0, os.SEEK_SET)
                msvcrt.locking(win_lock, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            os.close(win_lock)


def register(root, kind, dir, parents=(), params=None, seeds=None, status="running", summary=None, run_id=None, software_versions=None):  # noqa: A002 (the record field is "dir")
    """Record a new run and return its id.

    :param root: study folder or output root; the registry is ``<root>/derivatives/lnm/runs.jsonl``.
    :param kind: one of ``KINDS``.
    :param dir: the run's output folder (stored relative to ``root`` when inside it).
    :param parents: ids of the runs this run derives from.
    :param params: JSON-compatible parameters; ``params_sha256`` is computed from their canonical JSON.
    :param seeds: random seeds used by the run.
    :param run_id: a precomputed id (``new_run_id(kind)``); a new one by default.
    :param software_versions: overrides ``software()`` (export.versions(), which imports numpy and scipy).
    """
    _check_kind(kind)
    run_id = run_id or new_run_id(kind)
    if isinstance(parents, str):
        parents = [parents]
    record = {
        "op": "register",
        "id": run_id,
        "kind": kind,
        "dir": _relative_dir(root, dir),
        "parents": [str(parent) for parent in parents or () if parent],
        "params": params,
        "params_sha256": None if params is None else params_sha256(params),
        "seeds": seeds,
        "status": status,
        "created": _iso(_utc_now()),
        "software": software() if software_versions is None else software_versions,
        "summary": summary,
    }
    _append(root, _spill(root, record, run_dir(root, record)))
    return run_id


def update(root, run_id, **fields):
    """Record changed fields of a run (for example ``status="done", summary={...}``); later updates win."""
    for key in ("op", "id"):
        if key in fields:
            raise ValueError(f"The field {key!r} of a run cannot be updated")
    if "dir" in fields:
        fields["dir"] = _relative_dir(root, fields["dir"])
    record = {"op": "update", "id": run_id, "updated": _iso(_utc_now()), **fields}
    data = _encode(record)
    if len(data) > MAX_RECORD_BYTES:
        known = load(root).get(run_id)
        data = _spill(root, record, run_dir(root, fields if fields.get("dir") else known))
    _append(root, data)


def _parse_line(line):
    try:
        record = json.loads(line)
    except ValueError:
        # A writer that crashed mid-line leaves a fragment; the next record follows it on the same line.
        start = line.rfind(b'{"op":')
        if start <= 0:
            raise
        record = json.loads(line[start:])
    if not isinstance(record, dict) or record.get("op") not in ("register", "update") or not isinstance(record.get("id"), str):
        raise ValueError("not a run record")
    return record


def read_records(root):
    """Raw records of the registry in file order (malformed lines skipped with a RegistryWarning)."""
    path = registry_path(root)
    try:
        with open(path, "rb") as f:
            content = f.read()
    except FileNotFoundError:
        return []
    records = []
    skipped = 0
    for line in content.split(b"\n"):
        if not line.strip():
            continue
        try:
            records.append(_parse_line(line))
        except (ValueError, UnicodeDecodeError):
            skipped += 1
    if skipped:
        warnings.warn(f"{path}: skipped {skipped} malformed line(s)", RegistryWarning, stacklevel=3)
    return records


def load(root):
    """Fold the registry into {id: record} in registration order (later updates win, field by field)."""
    runs = {}
    pending = {}
    for record in read_records(root):
        run_id = record["id"]
        fields = {key: value for key, value in record.items() if key != "op"}
        if record["op"] == "register":
            runs[run_id] = fields
            for early in pending.pop(run_id, []):  # an update written before its registration
                runs[run_id].update(early)
        elif run_id in runs:
            runs[run_id].update(fields)
        else:
            pending.setdefault(run_id, []).append(fields)
    return runs


def read_field(root, record, key):
    """A field of a run record (``summary``, ``params``, ``seeds``, ...), read from its file when it was too large for the registry."""
    value = record.get(key) if record else None
    if isinstance(value, dict) and set(value) == {"$file"}:
        path = value["$file"]
        path = path if os.path.isabs(path) else os.path.join(os.fspath(root), *path.split("/"))
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return value


def read_summary(root, record):
    """The summary of a run record, read from its file when it was too large for the registry."""
    return read_field(root, record, "summary")


def read_params(root, record):
    """The parameters of a run record, read from their file when they were too large for the registry."""
    return read_field(root, record, "params")


def _matches(record, where):
    if where is None:
        return True
    if callable(where):
        return bool(where(record))
    return all(record.get(key) == value for key, value in where.items())


def list_runs(root, kind=None, status=None, where=None, runs=None):
    """Run records in registration order, filtered by kind and status (a value or a collection) and ``where``.

    ``where`` is a callable(record) -> bool or a dict of field values. ``runs`` is a ``load()``
    result to reuse.
    """
    runs = load(root) if runs is None else runs
    kinds = None if kind is None else ({kind} if isinstance(kind, str) else set(kind))
    statuses = None if status is None else ({status} if isinstance(status, str) else set(status))
    return [record for record in runs.values() if (kinds is None or record.get("kind") in kinds) and (statuses is None or record.get("status") in statuses) and _matches(record, where)]


def latest(root, kind, where=None, status=None, runs=None):
    """The most recently registered run of ``kind`` matching ``where`` (and ``status``), or None."""
    matching = list_runs(root, kind=kind, status=status, where=where, runs=runs)
    return matching[-1] if matching else None


def lineage(root, run_id, runs=None):
    """Ancestors of a run, nearest first (breadth-first, each once; cycles are safe).

    A parent id that is not in the registry is returned as {"id": id, "missing": True}.
    """
    runs = load(root) if runs is None else runs
    start = runs.get(run_id)
    if start is None:
        return []
    seen = {run_id}
    queue = list(start.get("parents") or [])
    ancestors = []
    while queue:
        parent = queue.pop(0)
        if parent in seen:
            continue
        seen.add(parent)
        record = runs.get(parent)
        if record is None:
            ancestors.append({"id": parent, "missing": True})
            continue
        ancestors.append(record)
        queue.extend(record.get("parents") or [])
    return ancestors


def children(root, run_id, runs=None):
    """Runs that list ``run_id`` as a parent, in registration order."""
    runs = load(root) if runs is None else runs
    return [record for record in runs.values() if run_id in (record.get("parents") or [])]

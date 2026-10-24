"""Lesion studies stored as plain files (no Slicer dependency), BIDS-compatible where it matters.

Folder layout::

    <study>/
      study.json          {"format": "slicer-studio-study", "version": 1, "name", "created", "template",
                           "space", "rules": {"lesion": <regex>, "id": <regex>}, "connectomes", "software"}
      participants.tsv    one row per subject; participant_id first, "n/a" for missing values
      participants.json   BIDS data dictionary plus "SlicerStudio": {"type", "role", "confirmed"} per column
      lesions/            sub-<id>_space-<space>_desc-lesion_mask.nii.gz, each with a BIDS sidecar .json
      derivatives/lnm/    network-mapping outputs (see export.py)
      .studio/            thumbs/, journal/, inbox/, audit.jsonl, merge.log, participants.lock
                          (application state, ignored by analyses)

``participants.tsv`` is the single source of truth about subjects. Its standard columns are
:data:`STANDARD_COLUMNS`; any other columns (e.g. clinical scores) are kept with their order. Paths
in the table are relative to the study folder (with "/" separators). Every column has a typed role
(:mod:`.schema`): pickers and design matrices use :meth:`Study.score_columns`,
:meth:`Study.covariate_columns` and :meth:`Study.analysis_rows`, never "all numeric columns".
Optional registry columns (``schema.OPTIONAL_COLUMNS``) are added only when first written, so old
studies open and save with their columns unchanged; additive ``study.json`` keys
(``trace_policy``, ``review_required``, ...) are read with defaults and written only when set.

Changes are made in memory (:meth:`Study.set`, :meth:`Study.add_files`, ...) and written by
:meth:`Study.save`, which replaces the files atomically. Only NIfTI headers are read.

**Single-writer rule.** Only the GUI session (on its main thread), or a headless tool working on a
study that no session has open, writes ``participants.tsv``, always through :meth:`Study.save`:
it takes ``.studio/participants.lock`` and merges the cells that changed on disk since the study was
opened (three-way: a cell changed only on disk keeps the disk value; when both sides changed it,
memory wins and ``.studio/merge.log`` gets a line). Jobs (subprocesses and worker threads) never
write ``participants.tsv``: they post their per-subject values with :meth:`Study.post_result`, which
writes ``.studio/inbox/<utc>-<pid>-<source>.json``, and the session merges them with
:meth:`Study.apply_inbox` (validated atomically per subject; audit records go to ``.studio/audit.jsonl``).
"""

import copy
import contextlib
import datetime
import hashlib
import json
import logging
import math
import os
import re
import threading
import time

from . import schema

STUDY_FORMAT = "slicer-studio-study"
STUDY_VERSION = 1
STUDY_JSON = "study.json"
PARTICIPANTS_TSV = "participants.tsv"
LESIONS_DIR = "lesions"
DERIVATIVES_DIR = "derivatives"
PRIVATE_DIR = ".studio"
NA = "n/a"

STANDARD_COLUMNS = (
    "participant_id",
    "group",
    "include",
    "status",
    "qc",
    "scan",
    "lesion",
    "space",
    "volume_ml",
    "n_parts",
    "gaps",
    "centroid_x",
    "centroid_y",
    "centroid_z",
    "outside_brain_pct",
    "rater",
    "trace_seconds",
    "modified",
    "notes",
)
DEFAULTS = {"group": NA, "include": "1", "status": "untraced", "qc": "unreviewed"}
ALLOWED_VALUES = {
    "group": ("case", "control", NA),
    "include": ("1", "0"),
    "status": ("untraced", "inprogress", "traced", "mapped"),
    "qc": ("unreviewed", "pass", "fail"),
}
PARTICIPANTS_JSON = schema.DICTIONARY_JSON
INBOX_DIR = "inbox"  # in PRIVATE_DIR
INBOX_FORMAT = "slicer-studio-inbox"
INBOX_VERSION = 1
AUDIT_JSONL = "audit.jsonl"  # in PRIVATE_DIR
MERGE_LOG = "merge.log"  # in PRIVATE_DIR
LOCK_FILE = "participants.lock"  # in PRIVATE_DIR
#: A participants.lock whose holder has not refreshed it for this long is stolen
LOCK_STALE_S = 30
#: How long Study.save waits for participants.lock (a dead holder's lock is stale after LOCK_STALE_S)
LOCK_TIMEOUT_S = 40
#: Reasons of rows that Study.analysis_rows excludes (plus "missing <column>" and "not a number: <column>")
EXCLUDED_INCLUDE = "include=0"
EXCLUDED_REVIEW = "review_status != approved"
EXCLUDED_NORM_QC = "norm_qc != pass"
EXCLUDED_AUTO = "automatic segmentation not approved"
DEFAULT_RULES = {"lesion": "lesion|mask|seg|label|roi", "id": "sub-[A-Za-z0-9]+"}
NIFTI_EXTENSIONS = (".nii.gz", ".nii")
# Word boundaries in file names (case-sensitive): "_", "-", ".", "+" or spaces, letters next to digits,
# and CamelCase ("LesionSmooth", "MNILesion")
_WORD_START = r"(?-i:^|(?<=[-_.+\s])|(?<=[0-9])(?=[A-Za-z])|(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z]))"
_WORD_END = r"(?-i:(?=[-_.+\s0-9]|$)|(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z]))"


def whole_word_rule(pattern):
    """Compile a rule (a regular expression, case-insensitive) that matches whole words of file names.

    Words are separated by "_", "-", ".", "+", spaces, digits or CamelCase; a match may be a plural or
    two matches written together. With "lesion|mask": "sub-01_desc-lesion_mask", "LesionSmooth",
    "T1lesion", "lesions" and "lesionmask" match; "sub-01_T1w_masked" and "defacemask" do not.
    """
    flags = re.match(r"\(\?[aiLmsux]+\)", pattern)  # global flags must stay at the start of the expression
    head, body = (flags.group(0), pattern[flags.end() :]) if flags else ("", pattern)
    return re.compile(rf"{head}{_WORD_START}(?i:(?:{body}){{1,2}}s?){_WORD_END}")


# Never lesions or scans unless their name contains "lesion": brain and deface masks, anatomical segmentations
_ANATOMICAL = whole_word_rule(r"brain[-_]?mask|defacemask|dseg|probseg|aseg|aparc|wmparc")
# Words of non-BIDS file names that describe the image, not the subject ("patient01_T1w_2mm.nii.gz")
_SCAN_WORD = re.compile(
    r"t1|t1w|t1c|t1ce|t1gd|t2|t2w|t2star|flair|mprage|mp2rage|swi|adc|dwi|dti|bold|ct|anat|scan|image|img|brain"
    r"|mni|mni152\w*|\d+mm|(?:ses|acq|ce|rec|run|echo|part|space|res|den|label|desc)-\w+",
    re.IGNORECASE,
)
_SUBJECT_WORDS = ("sub", "subj", "subject")
_PARTICIPANT = re.compile(r"^sub-[A-Za-z0-9]+$")


def now_iso():
    """Current UTC time as ISO 8601 text with seconds."""
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def participant_label(subject_id):
    """BIDS participant_id of a subject id: "sub-" followed by its letters and digits ("01" gives "sub-01")."""
    text = str(subject_id).strip()
    label = text[4:] if text.startswith("sub-") else text
    label = re.sub(r"[^A-Za-z0-9]", "", label)
    if not label:
        raise ValueError(f"Invalid subject id {subject_id!r}")
    return f"sub-{label}"


def format_value(value):
    """TSV text of a value: "n/a" for None, NaN and empty text; "1"/"0" for booleans; comma-separated sequences."""
    if value is None:
        return NA
    if type(value) is str and value and value == value.strip() and "\t" not in value and "\n" not in value and "\r" not in value:
        return value  # fast path: table cells are already TSV text
    if hasattr(value, "tolist") and not isinstance(value, (str, bytes)):
        return format_value(value.tolist())  # numpy scalar or array
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (list, tuple)):
        return ",".join(format_value(item) for item in value) if value else "none"
    if isinstance(value, datetime.datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, float):
        if not math.isfinite(value):
            return NA
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        text = f"{value:.6g}"
        return repr(value) if "e" in text else text
    text = re.sub(r"[\t\r\n]+", " ", str(value)).strip()
    return text or NA


def is_missing(value):
    return value is None or value == NA or value == ""


def _nifti_stem(path):
    name = os.path.basename(os.fspath(path))
    for ext in NIFTI_EXTENSIONS:
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return None


def detect_file_space(path):
    """Template space of a NIfTI file from its header: "MNI152NLin6Asym", "MNI152NLin2009cAsym" or "native"."""
    from .geometry import detect_space
    from .nifti import read_nifti_header

    header = read_nifti_header(path)
    return detect_space(header["shape"][:3], header["affine"])


def read_tsv(path):
    """Read a TSV table: (column names, list of row dicts). Empty values become "n/a".

    :raises ValueError: for duplicate or empty column names, or a row with more non-empty values
      than there are columns (they would be lost on the next save); extra empty fields are ignored.
    """
    with open(path, encoding="utf-8-sig", newline="") as f:
        return _parse_tsv(f.read(), path)


def _parse_tsv(text, path):
    """:func:`read_tsv` of the file text (``path`` is only used in messages)."""
    lines = [(number, line.rstrip("\r")) for number, line in enumerate(text.split("\n"), start=1)]
    lines = [(number, line) for number, line in lines if line.strip()]
    if not lines:
        return [], []
    columns = [name.strip() for name in lines[0][1].split("\t")]
    if len(set(columns)) != len(columns) or not all(columns):
        raise ValueError(f"{path}: duplicate or empty column names")
    rows = []
    for number, line in lines[1:]:
        values = [value.strip() for value in line.split("\t")]
        if any(values[len(columns):]):
            raise ValueError(f"{path}, line {number}: {len(values)} fields but only {len(columns)} columns; add a column name for the extra values or remove them")
        values += [NA] * (len(columns) - len(values))
        rows.append({column: value or NA for column, value in zip(columns, values[: len(columns)], strict=True)})
    return columns, rows


def write_tsv(path, columns, rows):
    """Write a TSV table atomically (missing values as "n/a")."""
    _write_text_atomic(path, _tsv_text(columns, rows))


def _tsv_text(columns, rows):
    lines = ["\t".join(columns)]
    lines += ["\t".join(format_value(row.get(column)) for column in columns) for row in rows]
    return "\n".join(lines) + "\n"


def _write_text_atomic(path, text):
    tmp_path = f"{os.fspath(path)}.{os.getpid()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def _validate(column, text):
    allowed = ALLOWED_VALUES.get(column)
    if allowed is not None and text not in allowed:
        raise ValueError(f"Invalid {column} value {text!r}; expected one of {', '.join(allowed)}")


def _normalize(column, text):
    """Check a cell (TSV text): (text, warning or None).

    Standard columns keep their exact :data:`ALLOWED_VALUES`; registered optional columns go through
    :func:`schema.normalize_value` (strict ones raise ValueError; lenient ones map synonyms and keep
    unknown values with a warning). Other columns are free.
    """
    if column in ALLOWED_VALUES:
        _validate(column, text)
        return text, None
    return schema.normalize_value(column, text)


def _included(row):
    return str(row.get("include", "1")).strip().lower() not in ("0", "false", "no")


def _digest(data):
    """Content digest of file bytes (or text, as written in UTF-8), or None for a missing file."""
    if data is None:
        return None
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.blake2b(data, digest_size=16).digest()


def _read_bytes(path):
    """The content of a file, or None when it does not exist."""
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _parse_dictionary(data, path):
    """:func:`schema.read_dictionary` of the file bytes ({} for None; ``path`` is only used in messages)."""
    if data is None:
        return {}
    dictionary = json.loads(data.decode("utf-8-sig"))
    if not isinstance(dictionary, dict):
        raise ValueError(f"{path} is not a JSON object")
    return dictionary


_ABSENT = object()


def _merge3(base, mine, theirs, conflict):
    """Three-way merge of the top-level keys of dicts: a key changed only in ``theirs`` takes its value;
    otherwise ``mine`` is kept and ``conflict(key, base, theirs, mine)`` is called when both changed it differently.
    """
    merged = {}
    for key in [*mine, *(key for key in theirs if key not in mine)]:
        old, new, other = base.get(key, _ABSENT), mine.get(key, _ABSENT), theirs.get(key, _ABSENT)
        value = new
        if other not in (old, new):
            if new == old:
                value = other
            else:
                conflict(key, old, other, new)
        if value is not _ABSENT:
            merged[key] = value
    return merged


def _shown(value):
    return "(absent)" if value is _ABSENT else json.dumps(value, ensure_ascii=False)[:200]


def _append_lines(path, lines):
    """Append text lines to a log file (one write call, created when missing)."""
    if not lines:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write("".join(line.rstrip("\n") + "\n" for line in lines))


def _path_from(folder, path):
    """``path`` relative to ``folder`` with "/" separators (absolute when on another drive)."""
    try:
        return os.path.relpath(path, folder).replace(os.sep, "/")
    except ValueError:
        return os.fspath(path)


_inbox_lock = threading.Lock()
_last_post = [None]


class Study:
    """A study folder: ``study.json`` metadata plus the ``participants.tsv`` table (see the module docstring).

    Rows are dicts of TSV text keyed by column name; change them through :meth:`set` so that
    values are validated and formatted, then call :meth:`save`.

    ``dictionary`` is the participants.json data dictionary ({column: entry}). ``validation_warnings``
    collects (participant_id, column, value, message) for values of lenient registry columns that
    were kept although unknown (for example ``sex`` = "unknown"). ``inbox_errors`` lists
    (file, participant_id, message) for inbox values that :meth:`apply_inbox` rejected.
    """

    def __init__(self, path, metadata, columns, rows, dictionary=None):
        self.path = os.path.abspath(os.fspath(path))
        self.metadata = metadata
        self.dictionary = dict(dictionary or {})
        self.validation_warnings = []
        self.inbox_errors = []
        self._columns = list(columns)
        self._rows = []
        self._index = {}
        for row in rows:
            self._insert(row)
        self._baseline = None  # state of the files when last read or written (merge-save)
        self._digests = {}  # {file name: content digest} when last read or written
        self._dictionary_error = None  # participants.json could not be read: never overwrite it
        self._pending_inbox = []  # inbox files applied in memory, finished by the next save

    # Creation and storage

    @classmethod
    def create(cls, path, name=None, template=None, space=None, rules=None, connectomes=None, software=None):
        """Create a new study folder (it may exist but must not contain a study) and save it.

        :param template: optional {"id", "res", "source", "sha256"} of the template that lesions are drawn on.
        :param space: study space; default: the template id, else None (decided per subject).
        :param rules: optional {"lesion": regex, "id": regex} for :meth:`add_files`.
        """
        path = os.path.abspath(os.fspath(path))
        if os.path.exists(os.path.join(path, STUDY_JSON)):
            raise FileExistsError(f"A study already exists in {path}")
        if os.path.exists(os.path.join(path, PARTICIPANTS_TSV)):
            raise FileExistsError(f"{path} already has a participants.tsv; open or import it instead")
        for folder in (LESIONS_DIR, DERIVATIVES_DIR, os.path.join(PRIVATE_DIR, "thumbs"), os.path.join(PRIVATE_DIR, "journal")):
            os.makedirs(os.path.join(path, folder), exist_ok=True)
        metadata = {
            "format": STUDY_FORMAT,
            "version": STUDY_VERSION,
            "name": name or os.path.basename(path),
            "created": now_iso(),
            "template": template,
            "space": space or (template or {}).get("id"),
            "rules": {**DEFAULT_RULES, **(rules or {})},
            "connectomes": list(connectomes or []),
            "software": dict(software or {}),
        }
        study = cls(path, metadata, STANDARD_COLUMNS, [])
        study.save()
        return study

    @classmethod
    def open(cls, path):
        """Open a study folder (or its study.json), with its participants.json data dictionary when present.

        No column is added: optional registry columns appear only when first written.
        """
        path = os.path.abspath(os.fspath(path))
        if os.path.basename(path) == STUDY_JSON and os.path.isfile(path):
            path = os.path.dirname(path)
        # Each file is read once and parsed from those bytes, whose digest is the merge baseline: any
        # later change on disk (even within one timestamp tick of a coarse file system) is merged by save()
        with open(os.path.join(path, STUDY_JSON), "rb") as f:
            data = f.read()
        digests = {STUDY_JSON: _digest(data)}
        metadata = json.loads(data.decode("utf-8"))
        if not isinstance(metadata, dict) or metadata.get("format") != STUDY_FORMAT:
            raise ValueError(f"Not a study: {os.path.join(path, STUDY_JSON)}")
        if int(metadata.get("version", 0)) > STUDY_VERSION:
            raise ValueError(f"Study version {metadata.get('version')} is newer than supported ({STUDY_VERSION})")
        tsv = os.path.join(path, PARTICIPANTS_TSV)
        data = _read_bytes(tsv) if os.path.isfile(tsv) else None
        digests[PARTICIPANTS_TSV] = _digest(data)
        columns, rows = _parse_tsv(data.decode("utf-8-sig"), tsv) if data is not None else ([], [])
        if columns and "participant_id" not in columns:
            raise ValueError(f"{tsv} has no participant_id column")
        # participant_id first, other columns in file order, then the missing standard columns
        columns = ["participant_id"] + [c for c in columns if c != "participant_id"]
        columns += [c for c in STANDARD_COLUMNS if c not in columns]
        dictionary_path = os.path.join(path, PARTICIPANTS_JSON)
        dictionary, dictionary_error, data = {}, None, None
        try:
            data = _read_bytes(dictionary_path)
            dictionary = _parse_dictionary(data, dictionary_path)
        except (OSError, ValueError) as exc:
            dictionary_error = str(exc)
            logging.warning(f"{dictionary_path} is not readable and will not be rewritten: {exc}")
        digests[PARTICIPANTS_JSON] = _digest(data)
        study = cls(path, metadata, columns, rows, dictionary)
        study._dictionary_error = dictionary_error
        study._snapshot(digests)
        return study

    @staticmethod
    def is_study(path):
        """True if the folder contains a study.json."""
        return os.path.isfile(os.path.join(os.fspath(path), STUDY_JSON))

    def save(self, merge=True, lock_timeout=LOCK_TIMEOUT_S):
        """Write participants.tsv, participants.json and study.json (each through a temporary file and an atomic rename).

        :param merge: take ``.studio/participants.lock`` (waiting up to ``lock_timeout`` seconds; a lock
          not refreshed for :data:`LOCK_STALE_S` seconds is stolen and logged) and merge what changed
          on disk since the study was opened or last saved (see the module docstring): cells and
          rows added on disk are kept, rows are removed only when removed in memory (a row removed
          on disk only is kept and logged), and when both sides changed a cell differently memory
          wins and ``.studio/merge.log`` gets a line. Changes are detected by content, not by
          timestamps. The in-memory study then holds the merged table. With False, the files are
          overwritten without lock or merge (the behaviour before schema v2).
        :param lock_timeout: seconds to wait for the lock. On the GUI main thread pass a short
          timeout and retry later (for example from a timer) on :class:`jobs.Locked`.
        :raises jobs.Locked: when the lock could not be taken in time.
        :raises ValueError: when participants.tsv on disk changed and cannot be read (for example a
          row with more values than columns); the files are then left untouched.
        """
        if merge:
            with self._lock(lock_timeout):
                self._merge_from_disk()
                self._write_files()
        else:
            self._write_files()
        self._finish_inbox()

    def _write_files(self):
        tsv_path = os.path.join(self.path, PARTICIPANTS_TSV)
        dictionary_path = os.path.join(self.path, PARTICIPANTS_JSON)
        json_path = os.path.join(self.path, STUDY_JSON)
        # The baseline is the digest of the text written (not of the file afterwards), so that a file
        # replaced by a lockless writer right after this write is still merged by the next save
        digests = dict(self._digests)
        text = _tsv_text(self._columns, self._rows)
        _write_text_atomic(tsv_path, text)
        digests[PARTICIPANTS_TSV] = _digest(text)
        if self._dictionary_error is None:
            self.dictionary = self._dictionary_for_save()
            text = schema.dictionary_text(self.dictionary)
            if _digest(text) != _digest(_read_bytes(dictionary_path)):  # unchanged files are not rewritten
                schema.write_text_atomic(dictionary_path, text)
            digests[PARTICIPANTS_JSON] = _digest(text)
        text = json.dumps(self.metadata, indent=2, ensure_ascii=False) + "\n"
        _write_text_atomic(json_path, text)
        digests[STUDY_JSON] = _digest(text)
        self._snapshot(digests)

    def _snapshot(self, digests):
        """Remember the state of the files (for the three-way merge of the next save)."""
        self._baseline = {
            "rows": {row["participant_id"]: dict(row) for row in self._rows},
            "metadata": copy.deepcopy(self.metadata),
            "dictionary": copy.deepcopy(self.dictionary),
        }
        self._digests = dict(digests)

    @contextlib.contextmanager
    def _lock(self, timeout):
        from . import jobs

        path = os.path.join(self.path, PRIVATE_DIR, LOCK_FILE)
        deadline = time.monotonic() + timeout
        while True:
            lock = jobs.OutputLock(path, heartbeat_s=10, stale_s=LOCK_STALE_S)
            try:
                lock.acquire()
                break
            except jobs.Locked:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        try:
            if lock.reclaimed is not None:
                holder = lock.reclaimed
                message = f"stole a stale {LOCK_FILE} (process {holder.get('pid')} on {holder.get('host')} since {holder.get('started')})"
                logging.warning(f"{self.path}: {message}")
                self._log_merge([message])
            yield
        finally:
            lock.release()

    def _log_merge(self, messages):
        stamp = f"{now_iso()}\tpid {os.getpid()}\t"
        _append_lines(os.path.join(self.path, PRIVATE_DIR, MERGE_LOG), [stamp + message for message in messages])

    def _merge_from_disk(self):
        """Merge the files that changed on disk since they were last read or written into memory."""
        base = self._baseline or {"rows": {}, "metadata": {}, "dictionary": {}}
        conflicts = []

        def conflict(file):
            def record(key, old, other, new):
                conflicts.append(f"{file} {key}: kept {_shown(new)} over {_shown(other)} on disk (was {_shown(old)})")

            return record

        # A file changed when its content differs from the baseline. Timestamps are not used: on file
        # systems with coarse timestamps (FAT, HFS+, older ext4) a same-size write within one tick
        # would go unnoticed. Reading the files costs about 1 ms for 1000 rows.
        def changed(name):
            path = os.path.join(self.path, name)
            data = _read_bytes(path)
            return (path, data) if data is not None and _digest(data) != self._digests.get(name) else (path, None)

        tsv_path, data = changed(PARTICIPANTS_TSV)
        if data is not None:
            columns, rows = _parse_tsv(data.decode("utf-8-sig"), tsv_path)
            self._merge_rows(base["rows"], columns, rows, conflicts)
        _, data = changed(STUDY_JSON)
        if data is not None:
            try:
                disk = json.loads(data.decode("utf-8"))
            except ValueError as exc:
                disk = None
                conflicts.append(f"{STUDY_JSON} on disk is not readable ({exc}); memory kept")
            if isinstance(disk, dict):
                self.metadata = _merge3(base["metadata"], self.metadata, disk, conflict(STUDY_JSON))
        dictionary_path, data = changed(PARTICIPANTS_JSON) if self._dictionary_error is None else (None, None)
        if data is not None:
            try:
                disk = _parse_dictionary(data, dictionary_path)
            except ValueError as exc:
                disk = None
                conflicts.append(f"{PARTICIPANTS_JSON} on disk is not readable ({exc}); memory kept")
            if disk is not None:
                self.dictionary = _merge3(base["dictionary"], self.dictionary, disk, conflict(PARTICIPANTS_JSON))
        self._log_merge(conflicts)

    def _merge_rows(self, base_rows, disk_columns, disk_rows, conflicts):
        for column in disk_columns:
            if column not in self._columns:
                self._columns.append(column)
                for row in self._rows:
                    row.setdefault(column, NA)
        columns = self._columns
        disk_index = {}
        for row in disk_rows:
            disk_index[row.get("participant_id")] = row
        for row in self._rows:
            sid = row["participant_id"]
            disk = disk_index.get(sid)
            if disk is None:
                # Added in memory, or removed on disk only: kept (a row is removed only when removed
                # in memory, so no subject is lost to another writer); the restored row is logged
                if sid in base_rows:
                    conflicts.append(f"{PARTICIPANTS_TSV} {sid}: row removed on disk was kept (it is still in this session)")
                continue
            base = base_rows.get(sid) or {}
            for column in columns:
                old = base.get(column, NA)
                other = disk.get(column, old)  # a column missing on disk is unchanged there
                new = row.get(column, NA)
                if other in (old, new):
                    continue
                if new == old:
                    row[column] = other
                else:
                    conflicts.append(f"{PARTICIPANTS_TSV} {sid} {column}: kept {new!r} over {other!r} on disk (was {old!r})")
        for sid, disk in disk_index.items():
            if sid in self._index or sid in base_rows:
                continue  # in memory, or removed in memory
            self._insert(disk)  # added on disk

    # Metadata

    @property
    def name(self):
        return self.metadata.get("name") or os.path.basename(self.path)

    @property
    def space(self):
        return self.metadata.get("space")

    @property
    def template(self):
        return self.metadata.get("template")

    @property
    def rules(self):
        return {**DEFAULT_RULES, **(self.metadata.get("rules") or {})}

    @property
    def columns(self):
        """Column names in file order."""
        return list(self._columns)

    @property
    def subjects(self):
        """Copies of the rows (dicts of TSV text), in table order; change cells with :meth:`set`."""
        return [dict(row) for row in self._rows]

    @property
    def subject_ids(self):
        return [row["participant_id"] for row in self._rows]

    def __len__(self):
        return len(self._rows)

    def __contains__(self, sid):
        return sid in self._index

    def __repr__(self):
        return f"Study({self.name!r}, subjects={len(self)})"

    # Paths

    def abspath(self, path):
        """Absolute path of a table path (relative to the study, "/" separators)."""
        if is_missing(path):
            return None
        path = os.fspath(path)
        return path if os.path.isabs(path) else os.path.normpath(os.path.join(self.path, *path.split("/")))

    def relpath(self, path):
        """Table path of a file: relative to the study with "/" separators (absolute if on another drive)."""
        path = os.path.abspath(os.fspath(path))
        try:
            return os.path.relpath(path, self.path).replace(os.sep, "/")
        except ValueError:
            return path

    def lesion_relpath(self, sid, space=None):
        """Table path of a subject's lesion file: lesions/<sid>_space-<space>_desc-lesion_mask.nii.gz."""
        row = self._row(sid)
        space = space or (None if is_missing(row.get("space")) else row["space"]) or self.space or "native"
        if not re.fullmatch(r"[A-Za-z0-9]+", space):
            raise ValueError(f"Invalid space label {space!r}")
        return f"{LESIONS_DIR}/{sid}_space-{space}_desc-lesion_mask.nii.gz"

    detect_space = staticmethod(detect_file_space)

    # Rows

    def _insert(self, row):
        sid = row.get("participant_id")
        if is_missing(sid) or not _PARTICIPANT.match(sid):
            raise ValueError(f"Invalid participant_id {sid!r} (expected sub-<letters and digits>)")
        if sid in self._index:
            raise ValueError(f"Duplicate participant_id {sid}")
        full = {column: row.get(column, DEFAULTS.get(column, NA)) for column in self._columns}
        for column, value in row.items():
            if column not in full:
                self._columns.append(column)
                full[column] = value
        self._rows.append(full)
        self._index[sid] = full
        return full

    def _row(self, sid):
        try:
            return self._index[sid]
        except KeyError:
            raise KeyError(f"Unknown subject {sid!r} in study {self.name!r}") from None

    def subject(self, sid):
        """A copy of the row of a subject (KeyError if unknown); change cells with :meth:`set`."""
        return dict(self._row(sid))

    def value(self, sid, column, cast=str):
        """A cell converted with ``cast`` (None when missing, "n/a" or not convertible)."""
        text = self._row(sid).get(column, NA)
        if is_missing(text):
            return None
        try:
            return cast(text)
        except (TypeError, ValueError):
            return None

    def add_subject(self, sid, values=None, **fields):
        """Add a subject (its id is normalized with :func:`participant_label`) and return a copy of its row."""
        sid = participant_label(sid)
        if sid in self._index:
            raise ValueError(f"Subject {sid} is already in the study")
        self._insert({"participant_id": sid})
        try:
            return self.set(sid, values, **fields)
        except Exception:
            self.remove_subject(sid)
            raise

    def remove_subject(self, sid):
        """Remove a subject's row (its files are kept)."""
        row = self._row(sid)
        self._rows = [other for other in self._rows if other is not row]
        del self._index[sid]

    def set(self, sid, values=None, **fields):
        """Set cells of a subject's row; unknown columns are added at the end of the table.

        :param values: optional {column: value} (for column names that are not Python identifiers).
        :return: a copy of the row.
        :raises ValueError: invalid group, include, status or qc values, or an invalid value of a
          strict registry column such as review_status (nothing is changed then). Values of lenient
          registry columns are normalized ("female" gives "F" for sex) or kept with a warning in
          :attr:`validation_warnings`.
        """
        row = self._row(sid)
        updates = {**(values or {}), **fields}
        if "participant_id" in updates and updates["participant_id"] != sid:
            raise ValueError("participant_id cannot be changed")
        texts = {}
        warnings = []
        for column, value in updates.items():
            text = format_value(value)
            texts[column], warning = _normalize(column, text)
            if warning:
                warnings.append((sid, column, text, warning))
        for column, text in texts.items():
            if column not in self._columns:
                self._columns.append(column)
                for other in self._rows:
                    other.setdefault(column, NA)
            row[column] = text
        self.validation_warnings.extend(warnings)
        return dict(row)

    def next_subject(self, after=None, status="untraced", include_only=True):
        """Next subject in table order after ``after`` (wrapping around) with the given status (None: any).

        :return: participant_id, or None if no other subject qualifies.
        """
        ids = self.subject_ids
        start = ids.index(after) + 1 if after in self._index else 0
        for sid in ids[start:] + ids[:start]:
            row = self._index[sid]
            if sid == after or (include_only and not _included(row)):
                continue
            if status is None or row.get("status") == status:
                return sid
        return None

    def lesion_paths(self, include_only=True, group=None, existing_only=False, with_ids=False):
        """Absolute lesion file paths of the subjects that have one.

        :param include_only: skip subjects with include = 0.
        :param group: "case" or "control" to select a group (None: all).
        :param existing_only: skip files that do not exist.
        :param with_ids: return (participant_id, path) tuples.
        """
        out = []
        for row in self._rows:
            if is_missing(row.get("lesion")) or (include_only and not _included(row)) or (group is not None and row.get("group") != group):
                continue
            path = self.abspath(row["lesion"])
            if existing_only and not os.path.isfile(path):
                continue
            out.append((row["participant_id"], path) if with_ids else path)
        return out

    def numeric_columns(self):
        """Non-standard columns whose non-missing values are all numbers.

        Kept for compatibility. It includes bookkeeping and covariate columns (fit_rms_mm, n_slices,
        age, ...): score pickers and analyses must use :meth:`score_columns` and the other role
        methods instead.
        """
        out = []
        for column in self._columns:
            if column in STANDARD_COLUMNS:
                continue
            values = [row.get(column, NA) for row in self._rows if not is_missing(row.get(column))]
            try:
                if values and all(math.isfinite(float(value)) for value in values):
                    out.append(column)
            except ValueError:
                continue
        return out

    # Column roles (schema v2)

    def column_spec(self, name):
        """The :class:`schema.ColumnSpec` of a column: the role the user confirmed in participants.json,
        else the registry's, else one inferred from the values (role "unknown").
        """
        entry = self.dictionary.get(name)
        spec = schema.registered(name)
        if spec is None:
            spec = schema.infer_spec(name, [row.get(name) for row in self._rows])
        elif schema.fork_entry(entry).get("by") == "registry":
            # Written by the application, not confirmed by the user: the current registry's type and role win
            entry = {key: value for key, value in entry.items() if key != schema.FORK_KEY}
        return schema.apply_entry(spec, entry)

    def set_column_role(self, name, role, type=None, levels=None, units=None, description=None):  # noqa: A002 (participants.json key)
        """Confirm the role (and optionally the type, levels, units and description) of a column in participants.json.

        Group and block roles make the column categorical, and the score role numeric, unless ``type`` says otherwise.

        :return: the new spec.
        :raises KeyError: for a column the study does not have.
        :raises ValueError: for an unknown role or type, or another role for a standard column.
        """
        if name not in self._columns:
            raise KeyError(f"Unknown column {name!r} in study {self.name!r}")
        if role not in schema.ROLES:
            raise ValueError(f"Unknown role {role!r}; expected one of {', '.join(schema.ROLES)}")
        standard = schema.STANDARD_ROLES.get(name)
        if standard is not None and role != standard.role:
            raise ValueError(f"The role of the standard column {name} is {standard.role}")
        spec = self.column_spec(name)
        changes = {"role": role}
        if type is not None:
            changes["type"] = type
        elif role in ("group", "block") and spec.type not in ("categorical", "ordinal"):
            changes["type"] = "categorical"
        elif role == "score" and spec.type not in ("numeric", "ordinal"):
            changes["type"] = "numeric"
        given = {"levels": levels, "units": units, "description": description}
        changes.update({key: value for key, value in given.items() if value is not None})
        spec = spec.replace(**changes)
        entry = schema.dictionary_entry(spec, True, self.dictionary.get(name))
        bids = spec.bids_entry()
        for key, field in (("Levels", "levels"), ("Units", "units"), ("Description", "description")):
            if given[field] is not None and key in bids:
                entry[key] = bids[key]
        schema.fork_entry(entry).pop("by", None)
        self.dictionary[name] = entry
        return spec

    def score_columns(self):
        """Columns that pickers offer as scores: [(name, confirmed)].

        Columns whose role is "score" are confirmed; numeric columns that are neither standard nor
        registered and whose role nobody confirmed are offered unconfirmed. Registered bookkeeping,
        metadata and covariate columns (fit_rms_mm, n_slices, age, ...) are never offered.
        """
        out = []
        for column in self._columns:
            if column in STANDARD_COLUMNS:
                continue
            entry = self.dictionary.get(column)
            known = schema.registered(column)
            if known is not None or schema.is_confirmed(entry):
                if self.column_spec(column).role == "score":
                    out.append((column, True))
                continue
            spec = self.column_spec(column)
            if spec.role == schema.UNKNOWN_ROLE and spec.type == "numeric":
                out.append((column, False))
        return out

    def _columns_with_role(self, role):
        return [column for column in self._columns if self.column_spec(column).role == role]

    def covariate_columns(self):
        """Columns with the role "covariate" (volume_ml, and age, sex, ... once written)."""
        return self._columns_with_role("covariate")

    def group_columns(self):
        """The "group" column, then the other categorical columns with the role "group" (multi-level factors)."""
        others = [column for column in self._columns if column != "group" and self.column_spec(column).role == "group" and self.column_spec(column).type in ("categorical", "ordinal")]
        return ["group", *others]

    def block_columns(self):
        """Columns with the role "block" (exchangeability blocks, such as dataset)."""
        return self._columns_with_role("block")

    def analysis_rows(self, columns=(), include_only=True, require_review=None, require_norm_qc=True):
        """Rows an analysis may use, with listwise exclusion (missing values are never imputed).

        :param columns: columns that must have a value in every row used (numeric columns must hold numbers).
        :param include_only: exclude rows with include = 0.
        :param require_review: exclude rows whose review_status is not "approved"; None uses
          :attr:`review_required`. Unless False (a recorded override), an automatic segmentation (an
          ``auto:<tool>`` token in trace_method) that no human approved is excluded whatever the setting.
        :param require_norm_qc: exclude normalized rows (with a norm_method) whose norm_qc is not "pass".
        :return: (copies of the rows used, {reason: [participant ids]}); each excluded row is counted once,
          under the first reason: :data:`EXCLUDED_INCLUDE`, :data:`EXCLUDED_REVIEW`, :data:`EXCLUDED_AUTO`,
          :data:`EXCLUDED_NORM_QC`, "missing <column>" or "not a number: <column>".
        """
        if isinstance(columns, str):
            columns = (columns,)
        review = self.review_required if require_review is None else bool(require_review)
        numeric = {column for column in columns if self.column_spec(column).type == "numeric"}
        rows, excluded = [], {}
        for row in self._rows:
            reason = None
            if include_only and not _included(row):
                reason = EXCLUDED_INCLUDE
            elif review and row.get("review_status") != "approved":
                reason = EXCLUDED_REVIEW
            elif require_review is not False and row.get("review_status") != "approved" and any(
                    token.startswith("auto:") for token in str(row.get("trace_method") or "").split("+")):
                reason = EXCLUDED_AUTO
            elif require_norm_qc and not is_missing(row.get("norm_method")) and row.get("norm_qc") != "pass":
                reason = EXCLUDED_NORM_QC
            else:
                for column in columns:
                    value = row.get(column)
                    if is_missing(value):
                        reason = f"missing {column}"
                        break
                    if column in numeric and schema._number(value) is None:
                        reason = f"not a number: {column}"
                        break
            if reason is None:
                rows.append(dict(row))
            else:
                excluded.setdefault(reason, []).append(row["participant_id"])
        return rows, excluded

    def _dictionary_for_save(self):
        """participants.json content: an entry for every column, then the entries of other keys (kept)."""
        out = {}
        for column in self._columns:
            entry = self.dictionary.get(column)
            known = schema.registered(column)
            fork = schema.fork_entry(entry)
            if schema.is_confirmed(entry) and fork.get("by") != "registry":
                out[column] = entry  # confirmed by the user
            elif known is not None:
                out[column] = schema.dictionary_entry(known, True, entry)
                out[column][schema.FORK_KEY]["by"] = "registry"
            else:
                spec = self.column_spec(column)
                updated = dict(entry) if isinstance(entry, dict) else {}
                updated[schema.FORK_KEY] = {**fork, "type": spec.type, "role": schema.UNKNOWN_ROLE, "confirmed": False}
                out[column] = updated
        for key, value in self.dictionary.items():
            out.setdefault(key, value)
        return out

    # Study settings (optional study.json keys, read with defaults and written only when set)

    @property
    def trace_policy(self):
        """{"space", "res", "convention_default", "extrude_2d"} (defaults: the study space, the template
        resolution or 1, "radiological", None), updated by study.json ``trace_policy``.
        """
        policy = {"space": self.space, "res": (self.template or {}).get("res") or 1, "convention_default": "radiological", "extrude_2d": None}
        stored = self.metadata.get("trace_policy")
        if isinstance(stored, dict):
            policy.update(copy.deepcopy(stored))
        return policy

    @trace_policy.setter
    def trace_policy(self, policy):
        self.metadata["trace_policy"] = copy.deepcopy(dict(policy))

    @property
    def review_required(self):
        """study.json ``review_required``; by default True when a row has source_type = case_report."""
        value = self.metadata.get("review_required")
        if value is not None:
            return bool(value)
        return any(row.get("source_type") == "case_report" for row in self._rows)

    @review_required.setter
    def review_required(self, value):
        if value is None:
            self.metadata.pop("review_required", None)  # back to the default
        else:
            self.metadata["review_required"] = bool(value)

    @property
    def blind_tracing(self):
        """study.json ``blind_tracing`` (default False): hide group, score and symptom columns from tracers."""
        return bool(self.metadata.get("blind_tracing", False))

    @blind_tracing.setter
    def blind_tracing(self, value):
        self.metadata["blind_tracing"] = bool(value)

    @property
    def subsets(self):
        """study.json ``subsets`` ({name: [participant ids]}; default {})."""
        stored = self.metadata.get("subsets")
        return copy.deepcopy(stored) if isinstance(stored, dict) else {}

    @subsets.setter
    def subsets(self, subsets):
        self.metadata["subsets"] = {str(name): list(ids) for name, ids in dict(subsets).items()}

    @property
    def grid_answers(self):
        """study.json ``grid_answers`` ({grid id: space} answered by the user; default {})."""
        stored = self.metadata.get("grid_answers")
        return copy.deepcopy(stored) if isinstance(stored, dict) else {}

    @grid_answers.setter
    def grid_answers(self, answers):
        self.metadata["grid_answers"] = dict(answers)

    # Inbox (single-writer rule: jobs post results, the session applies them)

    @staticmethod
    def post_result(study_path, sid, values, source, audit=None):
        """Post a subject's values for the study session to merge (see the module docstring).

        Jobs call this instead of writing participants.tsv. The file
        ``.studio/inbox/<utc>-<pid>-<source>.json`` is written atomically as
        {"format": "slicer-studio-inbox", "version": 1, "source", "created", "pid", "rows": {sid: values}, "audit": [...]}.

        :param study_path: the study folder (or its study.json).
        :param values: {column: value}; formatted like :meth:`set`.
        :param source: short name of the job ("normalize", "pipeline", ...).
        :param audit: optional audit record (dict) or records that :meth:`apply_inbox` appends to
          ``.studio/audit.jsonl``.
        :return: path of the inbox file.
        :raises ValueError: an invalid participant id, or an invalid value of a strict column.
        """
        return Study.post_results(study_path, {sid: values}, source, audit)

    @staticmethod
    def post_results(study_path, rows, source, audit=None):
        """:meth:`post_result` for several subjects in one file: ``rows`` is {sid: {column: value}}."""
        study_path = os.path.abspath(os.fspath(study_path))
        if os.path.basename(study_path) == STUDY_JSON:
            study_path = os.path.dirname(study_path)
        prepared = {}
        for sid, values in rows.items():
            label = participant_label(sid)
            if label in prepared:
                raise ValueError(f"{sid!r} and another key are the same participant {label}")
            texts = {}
            for column, value in dict(values).items():
                if column == "participant_id":
                    raise ValueError("participant_id cannot be posted")
                texts[str(column)], _ = _normalize(column, format_value(value))
            prepared[label] = texts
        records = [] if audit is None else [audit] if isinstance(audit, dict) else list(audit)
        name = re.sub(r"[^A-Za-z0-9_]+", "", str(source)) or "job"
        with _inbox_lock:  # file names stay unique and ordered within a process, even with a coarse clock
            moment = datetime.datetime.now(datetime.UTC)
            if _last_post[0] is not None and moment <= _last_post[0]:
                moment = _last_post[0] + datetime.timedelta(microseconds=1)
            _last_post[0] = moment
        content = {"format": INBOX_FORMAT, "version": INBOX_VERSION, "source": str(source), "created": moment.isoformat(timespec="microseconds"), "pid": os.getpid(), "rows": prepared, "audit": records}
        folder = os.path.join(study_path, PRIVATE_DIR, INBOX_DIR)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{moment:%Y%m%dT%H%M%S%f}Z-{os.getpid()}-{name}.json")
        schema.write_text_atomic(path, json.dumps(content, ensure_ascii=False) + "\n")
        return path

    def apply_inbox(self, save=True):
        """Merge the values that jobs posted (:meth:`post_result`), oldest first.

        Each subject's values are validated together (all or nothing). Unknown subjects and invalid
        values are listed in :attr:`inbox_errors`. With ``save``, the study is then saved (with merge);
        otherwise the next :meth:`save` finishes the inbox. After the save, the audit records of the
        files are appended to ``.studio/audit.jsonl`` (a record {"event": "inbox", ...} for files
        without one), applied files are deleted, and files with rejected values move to
        ``.studio/inbox/rejected/``.

        :return: [(file path, [participant ids applied])].
        """
        folder = os.path.join(self.path, PRIVATE_DIR, INBOX_DIR)
        try:
            names = sorted(name for name in os.listdir(folder) if name.endswith(".json") and not name.startswith("."))
        except FileNotFoundError:
            return []
        pending = {item["path"] for item in self._pending_inbox}
        applied = []
        for name in names:
            path = os.path.join(folder, name)
            if path in pending:
                continue
            errors, sids, columns = [], [], set()  # errors: (participant id or None, message)
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError) as exc:
                data, errors = {}, [(None, f"unreadable: {exc}")]
            if not isinstance(data, dict):
                data, errors = {}, [(None, "not a JSON object")]
            rows = data.get("rows")
            if not errors and (data.get("format") != INBOX_FORMAT or not isinstance(data.get("version"), int) or data["version"] > INBOX_VERSION or not isinstance(rows, dict)):
                errors.append((None, f"not a {INBOX_FORMAT} file of version {INBOX_VERSION}"))
            for sid, values in rows.items() if not errors else ():
                if sid not in self._index:
                    errors.append((sid, "unknown subject"))
                    continue
                if not isinstance(values, dict):
                    errors.append((sid, "values are not a JSON object"))
                    continue
                try:
                    self.set(sid, values)  # validated together: all or nothing
                except ValueError as exc:
                    errors.append((sid, str(exc)))
                    continue
                sids.append(sid)
                columns.update(values)
            self.inbox_errors.extend((path, sid, message) for sid, message in errors)
            records = data.get("audit")
            records = [records] if isinstance(records, dict) else [record for record in records if isinstance(record, dict)] if isinstance(records, list) else []
            if not records:
                records = [{"event": "inbox", "source": data.get("source"), "file": name, "sids": sids, "columns": sorted(columns), "at": now_iso()}]
            self._pending_inbox.append({"path": path, "errors": [f"{sid}: {message}" if sid else message for sid, message in errors], "audit": records if sids else []})
            applied.append((path, sids))
        if save and self._pending_inbox:
            self.save()
        return applied

    def _finish_inbox(self):
        """After a save: append the audit records of the applied inbox files and remove the files."""
        if not self._pending_inbox:
            return
        pending, self._pending_inbox = self._pending_inbox, []
        audit, rejected = [], []
        for item in pending:
            path = item["path"]
            # Removing (or moving) the file claims it: when two sessions applied the same file, only
            # the one that removes it writes its audit records
            try:
                if item["errors"]:
                    target = os.path.join(os.path.dirname(path), "rejected", os.path.basename(path))
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    os.replace(path, target)
                    rejected.append(f"inbox {os.path.basename(path)} moved to rejected/: {'; '.join(item['errors'])}")
                else:
                    os.remove(path)
            except FileNotFoundError:
                continue
            audit += [json.dumps(record, ensure_ascii=False, sort_keys=False) for record in item["audit"]]
        _append_lines(os.path.join(self.path, PRIVATE_DIR, AUDIT_JSONL), audit)
        if rejected:
            logging.warning("\n".join(rejected))
            self._log_merge(rejected)

    # Lesion sidecars (BIDS)

    def lesion_sidecar_path(self, sid):
        """The BIDS sidecar of a subject's lesion: the lesion path with .json instead of .nii[.gz]
        (the default lesion path of :meth:`lesion_relpath` when the row has no lesion).
        """
        row = self._row(sid)
        lesion = self.abspath(row.get("lesion")) or self.abspath(self.lesion_relpath(sid))
        stem = _nifti_stem(lesion)
        if stem is None:
            stem = os.path.splitext(os.path.basename(lesion))[0]  # x.nrrd -> x.json
        return os.path.join(os.path.dirname(lesion), stem + ".json")

    def read_lesion_sidecar(self, sid):
        """The lesion sidecar of a subject (a dict), or None when there is none."""
        try:
            with open(self.lesion_sidecar_path(sid), encoding="utf-8-sig") as f:
                data = json.load(f)
        except FileNotFoundError:
            return None
        return data if isinstance(data, dict) else None

    def write_lesion_sidecar(self, sid, studio=None, sources=None, spatial_reference=None, resolution=None, allow_outside=False):
        """Write (or update) the BIDS sidecar next to a subject's lesion mask.

        The existing file is merged: unknown keys are kept and ``SlicerStudio`` is updated key by key.
        Paths are relative to the sidecar ("/" separators), like the sidecars that Draw writes.

        :param studio: provenance values for the ``SlicerStudio`` object (participant_id, space and
          the modification time are always set).
        :param sources: source files (default: the row's scan). ``Source`` (the singular key of the
          sidecars that Draw writes) is set to the first one unless the sidecar already has one.
        :param spatial_reference: reference image (default: the row's scan when the lesion is in native space).
        :param resolution: BIDS Resolution text (default: from the lesion header, for example "1 mm").
        :param allow_outside: also write next to a lesion outside the study folder (for example in
          the source dataset that :meth:`add_files` referenced, which is otherwise never modified).
        :return: the sidecar path.
        :raises ValueError: for a lesion outside the study (unless ``allow_outside``) or an existing
          sidecar that is not a valid JSON object (it is not overwritten).
        """
        row = self._row(sid)
        path = self.lesion_sidecar_path(sid)
        folder = os.path.dirname(path)
        relative = self.relpath(path)
        if not allow_outside and (relative.startswith("../") or os.path.isabs(relative)):
            raise ValueError(f"The lesion of {sid} is outside the study; its sidecar is written only with allow_outside=True")
        try:
            with open(path, encoding="utf-8-sig") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {}
        if not isinstance(data, dict):
            raise ValueError(f"{path} is not a JSON object; it is not overwritten")
        scan = self.abspath(row.get("scan"))
        space = None if is_missing(row.get("space")) else row["space"]
        space = space or self.space or "native"
        out = {"Type": "Lesion"}
        if sources is not None:
            out["Sources"] = [_path_from(folder, self.abspath(source)) for source in sources]
        elif "Sources" not in data and scan:
            out["Sources"] = [_path_from(folder, scan)]
        listed = out.get("Sources", data.get("Sources"))
        if isinstance(listed, list) and listed and isinstance(listed[0], str) and (sources is not None or "Source" not in data):
            out["Source"] = listed[0]  # the single source image of Draw's sidecars (D-U3)
        if spatial_reference is not None:
            out["SpatialReference"] = _path_from(folder, self.abspath(spatial_reference))
        elif space == "native" and "SpatialReference" not in data:
            if scan:
                out["SpatialReference"] = _path_from(folder, scan)
            else:
                logging.warning(f"{sid}: the lesion is in native space but the row has no scan for its SpatialReference")
        if resolution is not None:
            out["Resolution"] = str(resolution)
        elif "Resolution" not in data:
            text = self._lesion_resolution(row)
            if text:
                out["Resolution"] = text
        studio_values = dict(schema.fork_entry(data))
        studio_values.update({"participant_id": sid, "space": space, "modified": now_iso()})
        studio_values.update(studio or {})
        merged = {**data, **out}
        merged[schema.FORK_KEY] = studio_values
        # Standard keys first, then the others in their existing order
        ordered = {key: merged[key] for key in ("Type", "Source", "Sources", "SpatialReference", "Resolution") if key in merged}
        ordered.update({key: value for key, value in merged.items() if key not in ordered})
        os.makedirs(folder, exist_ok=True)
        schema.write_text_atomic(path, json.dumps(ordered, indent=2, ensure_ascii=False) + "\n")
        return path

    def _lesion_resolution(self, row):
        path = self.abspath(row.get("lesion"))
        if not path or not os.path.isfile(path):
            return None
        from .nifti import read_nifti_header

        try:
            affine = read_nifti_header(path)["affine"]
        except (OSError, ValueError):
            return None
        sizes = [math.sqrt(sum(float(affine[r][c]) ** 2 for r in range(3))) for c in range(3)]
        texts = [format_value(round(size, 3)) for size in sizes]
        return f"{texts[0]} mm" if len(set(texts)) == 1 else f"{'x'.join(texts)} mm"

    # Import and export

    def import_tsv(self, path):
        """Merge a TSV table keyed by participant_id (ids are normalized to "sub-<label>").

        New columns are appended in the file's order; non-missing values overwrite existing cells
        and unknown subjects are added. All values are validated before anything changes.

        :return: {"added": [ids], "updated": [ids], "columns": [new columns]}.
        """
        columns, rows = read_tsv(path)
        if "participant_id" not in columns:
            raise ValueError(f"{path} has no participant_id column")
        prepared = []
        for row in rows:
            sid = participant_label(row["participant_id"])
            values = {column: value for column, value in row.items() if column != "participant_id" and not is_missing(value)}
            for column, value in values.items():
                _normalize(column, format_value(value))  # strict columns raise; set() records the warnings
            prepared.append((sid, values))
        if len({sid for sid, _ in prepared}) != len(prepared):
            raise ValueError(f"{path} has duplicate participant ids")
        new_columns = [column for column in columns if column != "participant_id" and column not in self._columns]
        for column in new_columns:
            self._columns.append(column)
            for row in self._rows:
                row.setdefault(column, NA)
        added, updated = [], []
        for sid, values in prepared:
            if sid in self._index:
                self.set(sid, values)
                updated.append(sid)
            else:
                self.add_subject(sid, values)
                added.append(sid)
        return {"added": added, "updated": updated, "columns": new_columns}

    def export_tsv(self, path):
        """Write the participants table to another file."""
        write_tsv(path, self._columns, self._rows)
        return path

    def add_files(self, paths, lesion_rule=None, id_regex=None):
        """Add scans and lesion masks, paired by subject id (only NIfTI headers are read).

        :param paths: NIfTI files and/or folders (searched recursively; hidden folders and the
          study's own derivatives and .studio folders are skipped). BIDS layouts work: the subject
          id is searched in the file name, then in the parent folders (sub-01/anat/...).
        :param lesion_rule: regular expression (case-insensitive) that marks lesion files by name, matched
          as whole words (see :func:`whole_word_rule`: "sub-01_desc-lesion_mask" and "LesionSmooth" are
          lesions, "sub-01_T1w_masked" and "Segura_01_T1" are scans); default: the study's rule
          ("lesion|mask|seg|label|roi"). Brain masks, deface masks and anatomical segmentations
          (brain_mask, defacemask, dseg, aseg, ...) are skipped unless their name contains "lesion".
        :param id_regex: regular expression of subject ids (default: the study's rule, "sub-[A-Za-z0-9]+").
          Files without a match use the start of their name: the words before the first modality or
          lesion word (T1, FLAIR, lesion, ...) up to the first word with a digit ("patient_01_T1.nii.gz":
          "patient01"; "sub_01_lesion.nii.gz": "01"; "Patient 7 T1.nii": "Patient7"), or the first
          word when there is no digit ("smith_T1.nii": "smith").
        :return: {"added": [ids], "updated": [ids], "skipped": [{"path", "reason"}], "pairs": {id: {"scan", "lesion"}},
          "warnings": [{"path", "message"}]} (files that are added but lie at different places in
          different programs because their sform and qform disagree).
        """
        from .geometry import detect_space
        from .nifti import XFORM_WARNING_MM, read_nifti_header, xform_disagreement

        rules = self.rules
        lesion_re = whole_word_rule(lesion_rule or rules["lesion"])
        id_re = re.compile(id_regex or rules["id"])
        report = {"added": [], "updated": [], "skipped": [], "pairs": {}, "warnings": []}
        candidates = {}
        original_stems = {}
        for path in self._expand(paths):
            stem = _nifti_stem(path)
            if stem is None:
                continue
            try:
                header = read_nifti_header(path)
            except (OSError, ValueError) as exc:
                report["skipped"].append({"path": path, "reason": f"unreadable: {exc}"})
                continue
            if len(header["shape"]) > 3:
                report["skipped"].append({"path": path, "reason": f"not a 3D image (shape {list(header['shape'])})"})
                continue
            if _ANATOMICAL.search(stem) and "lesion" not in stem.lower():
                report["skipped"].append({"path": path, "reason": "brain mask, deface mask or anatomical segmentation"})
                continue
            is_lesion = bool(lesion_re.search(stem))
            try:
                raw_id = self._subject_id(path, stem, id_re, lesion_re)
                sid = participant_label(raw_id)
            except ValueError:
                report["skipped"].append({"path": path, "reason": "no subject id"})
                continue
            if sid != raw_id:
                original_stems[path] = stem  # not a BIDS id: the row keeps the file's stem as source_id
            kind = "lesion" if is_lesion else "scan"
            candidates.setdefault(sid, {"scan": [], "lesion": []})[kind].append((path, header))
            disagreement = xform_disagreement(header)
            if disagreement > XFORM_WARNING_MM:
                report["warnings"].append({
                    "path": path,
                    "message": f"The sform and qform disagree by up to {disagreement:.2g} mm: Slicer may show this image at another place than FSL and the network mapping, which use the sform.",
                })

        def file_space(item):
            return detect_space(item[1]["shape"][:3], item[1]["affine"])

        for sid, kinds in candidates.items():
            # The lesion first: the scan should be in its space (BIDS derivatives may hold native and template T1w files)
            lesions = sorted(kinds["lesion"], key=lambda item: ("lesion" not in os.path.basename(item[0]).lower(), item[0]))
            chosen = {"lesion": lesions[0] if lesions else None}
            lesion_space = file_space(chosen["lesion"]) if chosen["lesion"] else None
            scans = sorted(kinds["scan"], key=lambda item: (lesion_space is not None and file_space(item) != lesion_space, "t1" not in os.path.basename(item[0]).lower(), item[0]))
            chosen["scan"] = scans[0] if scans else None
            for kind, files in (("scan", scans), ("lesion", lesions)):
                for path, _ in files[1:]:
                    report["skipped"].append({"path": path, "reason": f"another {kind} was chosen for {sid}"})
            space = lesion_space or file_space(chosen["scan"])
            values = {"scan": self.relpath(chosen["scan"][0]) if chosen["scan"] else None, "lesion": self.relpath(chosen["lesion"][0]) if chosen["lesion"] else None}
            report["pairs"][sid] = {kind: chosen[kind][0] if chosen[kind] else None for kind in ("scan", "lesion")}
            source_file = (chosen["lesion"] or chosen["scan"])[0]
            source_id = {"source_id": original_stems[source_file]} if source_file in original_stems else {}
            if sid not in self._index:
                self.add_subject(sid, {**values, "space": space, "status": "traced" if chosen["lesion"] else "untraced", **source_id})
                report["added"].append(sid)
                continue
            row = self._index[sid]
            updates = {column: value for column, value in values.items() if value is not None and is_missing(row.get(column))}
            if is_missing(row.get("space")):
                updates["space"] = space
            if updates.get("lesion") and row.get("status") == "untraced":
                updates["status"] = "traced"
            for column, value in values.items():
                if value is not None and not is_missing(row.get(column)) and row[column] != value:
                    report["skipped"].append({"path": self.abspath(value), "reason": f"{sid} already has a {column}"})
            if updates and source_id and is_missing(row.get("source_id")):
                updates.update(source_id)
            if updates:
                self.set(sid, updates)
                report["updated"].append(sid)
        return report

    def _expand(self, paths):
        if isinstance(paths, (str, os.PathLike)):
            paths = [paths]
        excluded = {os.path.join(self.path, DERIVATIVES_DIR, "lnm"), os.path.join(self.path, PRIVATE_DIR)}
        for path in paths:
            path = os.path.abspath(os.fspath(path))
            if os.path.isfile(path):
                yield path
                continue
            for folder, subfolders, files in os.walk(path):
                subfolders[:] = sorted(d for d in subfolders if not d.startswith(".") and os.path.join(folder, d) not in excluded)
                for name in sorted(files):
                    yield os.path.join(folder, name)

    @staticmethod
    def _subject_id(path, stem, id_re, lesion_re):
        match = id_re.search(stem)
        folder = os.path.dirname(path)
        for _ in range(3):  # BIDS: sub-<id>/[ses-<id>/]anat/<file>
            if match is not None or os.path.dirname(folder) == folder:
                break
            match = id_re.search(os.path.basename(folder))
            folder = os.path.dirname(folder)
        if match is not None:
            if "id" in id_re.groupindex:
                return match.group("id")
            return match.group(1) if id_re.groups else match.group(0)
        # The first run of words that are not modality or lesion words ("T1_patient_01_lesion_v2": patient, 01)
        def named(word):
            return _SCAN_WORD.fullmatch(word) or lesion_re.fullmatch(word)

        tokens = []
        for word in re.split(r"[_.\s]+", stem):
            parts = word.split("-")
            # "-" separates a modality or lesion word ("patient01-lesion"), not BIDS entities or names ("smith-jones")
            tokens += parts if not named(word) and any(named(part) for part in parts) else [word]
        words = []
        for word in tokens:
            if word and named(word):
                if words:
                    break
            elif word:
                words.append(word)
        if not words:
            raise ValueError(f"No subject id in {path}")
        if len(words) > 1 and words[0].lower() in _SUBJECT_WORDS:
            words = words[1:]  # "sub_01_T1": 01
        for index, word in enumerate(words):
            if any(char.isdigit() for char in word):
                return "".join(words[: index + 1])  # "patient_01_T1": patient01
        return words[0]

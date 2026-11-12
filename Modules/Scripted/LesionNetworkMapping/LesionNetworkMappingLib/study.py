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
in the table are relative to the study folder (with "/" separators), or "controlset:<name>/<rel>" for lesions
linked from a registered control set (:meth:`Study.link_control_set`, never copied). Every column has a typed role
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
import csv
import datetime
import hashlib
import io
import json
import logging
import math
import os
import re
import threading
import time
import zlib

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
#: A lesion marked "QC fail" (the qc column, Library ▸ QC) is never analysed: every analysis and Run everything leave it out
EXCLUDED_QC = "qc=fail"
EXCLUDED_REVIEW = "review_status != approved"
EXCLUDED_NORM_QC = "norm_qc != pass"
EXCLUDED_AUTO = "automatic segmentation not approved"
DEFAULT_RULES = {"lesion": "lesion|mask|seg|label|roi", "id": "sub-[A-Za-z0-9]+"}
NIFTI_EXTENSIONS = (".nii.gz", ".nii")
#: Table paths of lesions linked from a control set (resolved through the registry, see controlsets.py)
CONTROLSET_PREFIX = "controlset:"
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


def _case_twin_message(sid, twin):
    return f"Subject {sid} differs from {twin} only in letter case: on macOS they would share files"


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


def _is_relative_to(path, folder):
    """Whether ``path`` is ``folder`` or inside it (absolute paths; letter case as the platform compares it)."""
    path, folder = os.path.normcase(path), os.path.normcase(folder)
    try:
        return os.path.commonpath([path, folder]) == folder
    except ValueError:  # another drive
        return False


_BIDS_SESSION = re.compile(r"(?:^|_)ses-([A-Za-z0-9]+)(?=_|$)")


def _bids_session(path):
    """The BIDS session label of a file ("2" for sub-01_ses-2_T1w.nii.gz or .../ses-2/anat/...), or None."""
    path = os.path.abspath(os.fspath(path))
    parts = [_nifti_stem(path) or os.path.basename(path)]
    folder = os.path.dirname(path)
    for _ in range(3):  # sub-01/ses-2/anat/file
        parts.append(os.path.basename(folder))
        folder = os.path.dirname(folder)
    for part in parts:
        match = _BIDS_SESSION.search(part)
        if match:
            return match.group(1)
    return None


def _same_grid(header, other, tolerance=1e-3):
    """Whether two NIfTI headers describe the same voxel grid (shape and voxel-to-RAS matrix)."""
    if tuple(header["shape"][:3]) != tuple(other["shape"][:3]):
        return False
    a, b = header["affine"], other["affine"]
    return all(abs(float(a[i][j]) - float(b[i][j])) <= tolerance for i in range(4) for j in range(4))


def detect_file_space(path):
    """Template space of a NIfTI file from its header: "MNI152NLin6Asym", "MNI152NLin2009cAsym" or "native"."""
    from .geometry import detect_space
    from .nifti import read_nifti_header

    header = read_nifti_header(path)
    return detect_space(header["shape"][:3], header["affine"])


class TableError(ValueError):
    """A problem with the user's table file (its encoding, its columns): the message says what to do about it.

    ``user_problem`` lets the application show the message without logging a traceback (it is not a program error).
    """

    user_problem = True


def decode_table(data, path):
    """The text of the bytes of a TSV or CSV table (UTF-8, with or without a byte order mark).

    :raises TableError: for any other encoding (Windows-1252 and UTF-16 are what Excel writes), with what to do.
    """
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        name = os.path.basename(os.fspath(path))
        raise TableError(f"{name} is not saved as UTF-8 text (for example as Windows-1252 or UTF-16 by Excel). "
                         "Save the table as UTF-8 (in Excel: Save As > CSV UTF-8, then rename it to .tsv if needed; "
                         "in a text editor: Save with encoding UTF-8) and try again.") from None


def read_tsv(path):
    """Read a TSV table: (column names, list of row dicts). Empty values become "n/a".

    A value in double quotes may contain tabs, line breaks and doubled quotes (BIDS escapes a tab
    that way, and pandas, R and spreadsheets write such values so).

    :raises ValueError: for duplicate or empty column names, or a row with more non-empty values
      than there are columns (they would be lost on the next save); extra empty fields are ignored,
      and so are empty column names at the end of the header (spreadsheets write them as trailing tabs).
      A table that is not UTF-8 text raises :class:`TableError` (a ``ValueError``) that says so.
    """
    with open(path, "rb") as f:
        data = f.read()
    return _parse_tsv(decode_table(data, path), path)


def _tsv_records_plain(text):
    """Records of a TSV text that has no quoting: [(line number, values)], one per line."""
    return [(number, line.rstrip("\r").split("\t")) for number, line in enumerate(text.split("\n"), start=1)]


def _tsv_records_quoted(text):
    """Records of a TSV text with double-quoted values ([(first line number, values)]); None when the quoting is broken."""
    reader = csv.reader(io.StringIO(text, newline=""), delimiter="\t", quotechar='"', doublequote=True, strict=True)
    records, line = [], 1
    try:
        for values in reader:
            records.append((line, values))
            line = reader.line_num + 1
    except csv.Error:
        return None
    return records


def _tsv_records(text):
    """Records of a TSV text: [(first line number, values)].

    Values in double quotes are read as CSV readers do. A text whose quoting is broken, or whose
    quoted reading gives rows of the wrong length while the plain one does not (a file written
    before quoting, with a value that starts with a lone quote), is read without quoting as before.
    """
    plain = _tsv_records_plain(text)
    if '"' not in text:
        return plain
    quoted = _tsv_records_quoted(text)
    if quoted is None:
        return plain

    def regular(records):
        records = [values for _number, values in records if any(value.strip() for value in values)]
        width = len(records[0]) if records else 0
        return all(len(values) == width or (len(values) > width and not any(v.strip() for v in values[width:])) for values in records)

    return quoted if regular(quoted) or not regular(plain) else plain


def _parse_tsv(text, path):
    """:func:`read_tsv` of the file text (``path`` is only used in messages).

    Tabs and line breaks inside a quoted value become one space, as :func:`format_value` writes them,
    so that a table read back equals the one written.
    """
    records = [(number, [re.sub(r"[\t\r\n]+", " ", value).strip() for value in values]) for number, values in _tsv_records(text)]
    records = [(number, values) for number, values in records if any(values)]
    if not records:
        return [], []
    columns = records[0][1]
    while columns and not columns[-1]:
        columns = columns[:-1]  # empty names after the last column: trailing tabs; values under them are refused below
    if len(set(columns)) != len(columns) or not all(columns):
        raise TableError(f"{os.path.basename(os.fspath(path))}: duplicate or empty column names")
    rows = []
    for number, values in records[1:]:
        if any(values[len(columns):]):
            raise TableError(f"{os.path.basename(os.fspath(path))}, line {number}: {len(values)} fields but only {len(columns)} columns; add a column name for the extra values or remove them")
        values += [NA] * (len(columns) - len(values))
        rows.append({column: value or NA for column, value in zip(columns, values[: len(columns)], strict=True)})
    return columns, rows


def write_tsv(path, columns, rows):
    """Write a TSV table atomically (missing values as "n/a")."""
    _write_text_atomic(path, _tsv_text(columns, rows))


#: Characters that open a quotation: a straight double quote after one of them (or at the start) becomes an opening quote
_OPENING_CONTEXT = "([{<\u2018\u201c-\u2013\u2014/"


def typographic_quotes(text):
    """``text`` with each straight double quote (") replaced by a typographic one (“ or ”).

    Official 3D Slicer reads tables with VTK's delimited text reader, which takes every straight double
    quote as the start or end of a quoted string and knows no escaped quote: "Müller "et al." 2020"
    comes back as "et al. 2020", and an odd number of quotes merges the rest of the row into one cell.
    No spelling of a straight quote reads the same there and in BIDS tools (pandas, R, the csv module),
    so Studio writes the typographic quote, which all of them keep as it is. A quote at the start or
    after a space or an opening bracket opens (“), any other closes (” also serves as an inch mark).
    """
    if '"' not in text:
        return text
    out = []
    for index, char in enumerate(text):
        if char == '"':
            before = text[index - 1] if index else ""
            char = "“" if not before or before.isspace() or before in _OPENING_CONTEXT else "”"
        out.append(char)
    return "".join(out)


def _tsv_field(text):
    """A TSV cell that official 3D Slicer and BIDS tools read alike.

    Straight double quotes become typographic ones (:func:`typographic_quotes`); a cell with a tab or a
    line break is put in double quotes (both readers keep it whole then).
    """
    text = typographic_quotes(text)
    if "\t" in text or "\n" in text or "\r" in text:
        return '"' + text.replace('"', '""') + '"'
    return text


def _tsv_text(columns, rows):
    lines = ["\t".join(_tsv_field(column) for column in columns)]
    lines += ["\t".join(_tsv_field(format_value(row.get(column))) for column in columns) for row in rows]
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


#: Spellings of include values (any case) that keep their meaning, besides numbers equal to 1 or 0 (:func:`include_value`)
INCLUDE_WORDS = {"1": "1", "true": "1", "yes": "1", "y": "1", "0": "0", "false": "0", "no": "0", "n": "0"}


def include_value(value):
    """The canonical include value ("1" or "0") of a cell, or None when it is missing or not an include value.

    Besides 1 and 0 it accepts :data:`INCLUDE_WORDS` and numbers equal to 1 or 0, which is how pandas and
    spreadsheets write the column ("1.0", "0.0", for example when a column of integers has an empty cell).
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in INCLUDE_WORDS:
        return INCLUDE_WORDS[text]
    try:
        number = float(text)
    except ValueError:
        return None
    return "1" if number == 1 else "0" if number == 0 else None


def canonical_value(column, value):
    """The allowed value (:data:`ALLOWED_VALUES`) that a cell of a standard column with fixed values means, or None.

    Include values go through :func:`include_value` ("yes", "1.0" give "1"); the other columns accept their allowed values
    in another case ("Traced" gives "traced"). Values that mean none of them ("alive" in a status column) give None, as do
    columns without fixed values.
    """
    allowed = ALLOWED_VALUES.get(column)
    if allowed is None or value is None:
        return None
    text = str(value).strip()
    if text in allowed:
        return text
    if column == "include":
        return include_value(text)
    return next((v for v in allowed if v == text.lower()), None)


#: Values of the space column besides "native": the BIDS standard template identifiers (BIDS appendix "Coordinate
#: systems"), the TemplateFlow spellings Studio writes (MNI152NLin6Asym) and "MNI" (an MNI template of another family,
#: Slicer Studio's own templates). A space column holding anything else ("3T", a field strength) is a user's column.
SPACE_LABELS = (
    "native", "MNI", "MNI152Lin", "MNI152NLin2009aSym", "MNI152NLin2009bSym", "MNI152NLin2009cSym", "MNI152NLin2009aAsym",
    "MNI152NLin2009bAsym", "MNI152NLin2009cAsym", "MNI152NLin6Sym", "MNI152NLin6Asym", "MNI152NLin6ASym", "MNI305", "MNIColin27",
    "MNIInfant", "MNIPediatricAsym", "ICBM452AirSpace", "ICBM452Warp5Space", "IXI549Space", "NKI", "OASIS30AntsOASISAnts",
    "OASIS30Atropos", "Talairach", "UNCInfant", "fsaverage", "fsaverageSym", "fsLR", "fsaverage3", "fsaverage4", "fsaverage5",
    "fsaverage6",
)
_SPACE_LABELS_FOLDED = {label.casefold() for label in SPACE_LABELS}


def is_space_value(value):
    """Whether a cell can be the study's space column: missing, "native" or a template space (:data:`SPACE_LABELS`, any case)."""
    return is_missing(value) or str(value).strip().casefold() in _SPACE_LABELS_FOLDED


def is_included(row):
    """True unless the row's include value means 0 (:func:`include_value`); the one test every reader uses."""
    return include_value((row or {}).get("include", "1")) != "0"


_included = is_included


def _canonical_cells(rows, path, fill_absent=False):
    """Write the standard columns with fixed values (:data:`ALLOWED_VALUES`) of rows read from a file as Studio does.

    Tables edited outside Studio can hold other spellings: an include column written by pandas holds "1.0" / "0.0"
    and empty cells. Include values are made "1" or "0" (:func:`include_value`), other values in another case
    ("Traced") the allowed one, and missing cells (empty, "n/a" or "nan") the default (:data:`DEFAULTS`), so that every
    reader sees the same values and the next save repairs the file. Unknown values are kept (with a warning).

    :param fill_absent: also give a missing column its default (on open; a merge treats a column missing on disk as
      unchanged there).
    """
    unknown = {}
    for row in rows:
        for column, allowed in ALLOWED_VALUES.items():
            if column not in row:
                if fill_absent:
                    row[column] = DEFAULTS.get(column, NA)
                continue
            value = row[column]
            if value in allowed:
                continue
            text = str(value).strip().lower()
            if is_missing(text) or text == "nan":
                row[column] = DEFAULTS.get(column, NA)
                continue
            canonical = canonical_value(column, text)
            if canonical is None:
                unknown.setdefault(column, []).append(row.get("participant_id"))
            else:
                row[column] = canonical
    for column, sids in unknown.items():
        logging.warning(f"{path}: {column} has values other than {', '.join(ALLOWED_VALUES[column])} ({', '.join(map(str, sids[:5]))}"
                        f"{', ...' if len(sids) > 5 else ''}); they are kept as they are")


def qc_failed(row):
    """True when the row's lesion is marked "QC fail" (qc = fail): analyses leave it out (:data:`EXCLUDED_QC`)."""
    return str(row.get("qc") or "").strip().lower() == "fail"


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
    (file, participant_id, message) for inbox values that :meth:`apply_inbox` rejected, and
    ``inbox_held`` (file, [participant ids]) for the files that the last :meth:`apply_inbox` held back.
    """

    #: Registry folder of the control sets (<Studio/LNM/DataDirectory>/ControlSets) that ``controlset:`` paths resolve
    #: through; None: the environment variable SLICER_STUDIO_CONTROLSETS, else the default data directory
    controlset_root = None

    def __init__(self, path, metadata, columns, rows, dictionary=None):
        self.path = os.path.abspath(os.fspath(path))
        self.metadata = metadata
        self.dictionary = dict(dictionary or {})
        self.validation_warnings = []
        self.inbox_errors = []
        self.inbox_held = []
        self._columns = list(columns)
        self._rows = []
        self._index = {}
        self._folded = {}  # {casefolded id: [ids]}: ids that differ only in case name one file on macOS
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

        :param template: optional {"id", "res", "source", "sha256"} of the template that lesions are drawn on ("id" is its
          space). The user's own template (Slicer Studio A-10) has "source" "user:<slug>" and also "name" and "space":
          a study names it by id, name, space and checksum, never by path (:meth:`user_template`).
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
        columns, rows = _parse_tsv(decode_table(data, tsv), tsv) if data is not None else ([], [])
        if columns and "participant_id" not in columns:
            raise ValueError(f"{tsv} has no participant_id column")
        # participant_id first, other columns in file order, then the missing standard columns
        columns = ["participant_id"] + [c for c in columns if c != "participant_id"]
        columns += [c for c in STANDARD_COLUMNS if c not in columns]
        _canonical_cells(rows, tsv, fill_absent=True)
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
        for row in self._rows:  # memory holds what the file holds (_tsv_field writes typographic quotes)
            for column, value in row.items():
                if type(value) is str and '"' in value and column != "participant_id":  # ids index the rows
                    row[column] = typographic_quotes(value)
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
            columns, rows = _parse_tsv(decode_table(data, tsv_path), tsv_path)
            _canonical_cells(rows, tsv_path)
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

    def user_template(self):
        """{"id": "user:<slug>", "name", "space", "sha256"} when the study's template is one the user added (Slicer Studio
        A-10; the files are on the user's computer, found by checksum), else None.
        """
        template = self.template
        if not isinstance(template, dict) or not str(template.get("source") or "").startswith("user:"):
            return None
        return {"id": template["source"], "name": template.get("name"), "space": template.get("space") or template.get("id"),
                "sha256": template.get("sha256")}

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

    def case_twin(self, sid):
        """The subject id that differs from ``sid`` only in letter case ("sub-P01" for "sub-p01"), else None.

        Subject ids name files (lesions/<sid>_...), and on a case-insensitive disk (the macOS default) such
        ids would share one file, so new subjects may not be case twins of existing ones.
        """
        return next((other for other in self._folded.get(str(sid).casefold(), ()) if other != sid), None)

    def id_taken(self, sid):
        """True when ``sid`` is a subject id of the study or differs from one only in letter case."""
        return sid in self._index or self.case_twin(sid) is not None

    def __repr__(self):
        return f"Study({self.name!r}, subjects={len(self)})"

    # Paths

    def abspath(self, path):
        """Absolute path of a table path (relative to the study, "/" separators).

        "controlset:<name>/<rel>" (a lesion linked from a control set) is resolved through the control-set registry
        (:attr:`controlset_root`); the file is never copied into the study.
        """
        if is_missing(path):
            return None
        path = os.fspath(path)
        if path.startswith(CONTROLSET_PREFIX):
            from .controlsets import resolve_path

            return resolve_path(path, self.controlset_root)
        return path if os.path.isabs(path) else os.path.normpath(os.path.join(self.path, *path.split("/")))

    def _same_file(self, a, b):
        """Whether two table paths (relative or absolute) name one file."""
        if a == b:
            return True
        pa, pb = self.abspath(a), self.abspath(b)
        return pa is not None and pb is not None and os.path.normcase(os.path.normpath(pa)) == os.path.normcase(os.path.normpath(pb))

    def relpath(self, path):
        """Table path of a file: relative to the study with "/" separators (absolute if on another drive)."""
        path = os.path.abspath(os.fspath(path))
        try:
            return os.path.relpath(path, self.path).replace(os.sep, "/")
        except ValueError:
            return path

    def follow_move(self, old_path, moved=None):
        """Make the table paths that leave the study ("../...") point at their files again after the study folder moved.

        The study was at ``old_path`` and is now at :attr:`path`. A relative path to a file outside the study (one that
        Add files referenced in place) would otherwise resolve from the new place, which is a different file or none when
        the new folder is at another depth. Such a path is written again relative to the new place. ``moved`` maps the
        absolute path a file had before the move to where it is now (files that moved with the study's parent folder);
        files that stayed where they were are found at their old path. Paths inside the study, "controlset:" paths,
        absolute paths and paths whose file is missing are kept. Running it again changes nothing. Changes the table
        in memory (save it with :meth:`save`); return the number of cells changed.
        """
        old_path = os.path.normpath(os.path.abspath(os.fspath(old_path)))
        if os.path.normcase(old_path) == os.path.normcase(os.path.normpath(self.path)):
            return 0
        columns = [column for column in self._columns if column in ("scan", "lesion") or self.column_spec(column).type == "path"]
        changed = 0
        for row in self._rows:
            for column in columns:
                value = row.get(column)
                if is_missing(value) or value.startswith(CONTROLSET_PREFIX) or os.path.isabs(value):
                    continue
                parts = value.split("/")
                if os.path.normpath(os.path.join(*parts)).split(os.sep)[0] != os.pardir:
                    continue  # inside the study: it moved with it
                before = os.path.normpath(os.path.join(old_path, *parts))
                now = moved(before) if moved is not None else before
                if not now or not os.path.exists(now):
                    continue
                if os.path.normcase(os.path.normpath(self.abspath(value))) == os.path.normcase(os.path.normpath(now)):
                    continue
                self.set(row["participant_id"], {column: self.relpath(now)})
                changed += 1
        return changed

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
        self._folded.setdefault(sid.casefold(), []).append(sid)
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
        twin = self.case_twin(sid)
        if twin is not None:
            raise ValueError(_case_twin_message(sid, twin))
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
        twins = self._folded.get(sid.casefold(), [])
        if sid in twins:
            twins.remove(sid)

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
            text = typographic_quotes(format_value(value))  # as written to participants.tsv (_tsv_field)
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
        if levels is None and spec.levels and spec.type not in ("categorical", "ordinal"):
            # The levels were inferred while the column looked categorical: BIDS Levels belong to categorical columns
            spec = spec.replace(levels=None)
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

    def analysis_rows(self, columns=(), include_only=True, require_review=None, require_norm_qc=True, exclude_qc_fail=True):
        """Rows an analysis may use, with listwise exclusion (missing values are never imputed).

        :param columns: columns that must have a value in every row used (numeric columns must hold numbers).
        :param include_only: exclude rows with include = 0.
        :param require_review: exclude rows whose review_status is not "approved"; None uses
          :attr:`review_required`. Unless False (a recorded override), an automatic segmentation (an
          ``auto:<tool>`` token in trace_method) that no human approved is excluded whatever the setting.
        :param require_norm_qc: exclude normalized rows (with a norm_method) whose norm_qc is not "pass".
        :param exclude_qc_fail: exclude rows whose lesion is marked "QC fail" (qc = fail), as Run everything does.
        :return: (copies of the rows used, {reason: [participant ids]}); each excluded row is counted once,
          under the first reason: :data:`EXCLUDED_INCLUDE`, :data:`EXCLUDED_QC`, :data:`EXCLUDED_REVIEW`,
          :data:`EXCLUDED_AUTO`, :data:`EXCLUDED_NORM_QC`, "missing <column>" or "not a number: <column>".
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
            elif exclude_qc_fail and qc_failed(row):
                reason = EXCLUDED_QC
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

    @property
    def control_sets(self):
        """study.json ``control_sets`` ([{"name", "sha256"}] of the linked control sets; default [])."""
        stored = self.metadata.get("control_sets")
        return [dict(item) for item in stored if isinstance(item, dict) and item.get("name")] if isinstance(stored, list) else []

    def link_control_set(self, name, sha256=None, ids=None, root=None):
        """Link a registered control set: one row per control lesion, files never copied (see controlsets.py).

        Rows get group = control, source_type = control_db, source_dataset = <name>, source_id = <lesion id>, lesion =
        "controlset:<name>/<rel>", status = traced and the set's space, volume_ml, dimensionality and hemisphere. Rows
        already linked from the set are updated, not duplicated. The link is the selection: rows linked earlier whose
        lesion is no longer in the set (a set registered again) or not among ``ids`` are removed. study.json
        ``control_sets`` records {name, sha256} while at least one row is linked from the set.

        :param sha256: expected ``content_sha256`` of the set (ValueError when the registered set differs).
        :param ids: link only these lesion ids of the set (e.g. a matched subsample); default all.
        :param root: registry folder (default :attr:`controlset_root`).
        :return: {"added": [participant ids], "updated": [...], "removed": [...], "skipped": [{"id", "reason"}], "sha256"}.
        """
        from . import controlsets

        entry = controlsets.load(name, root or self.controlset_root)
        digest = entry.get("content_sha256") or controlsets.content_sha256(entry)
        if sha256 and sha256 != digest:
            raise ValueError(f"The control set {entry['name']!r} changed since it was linked (sha256 {digest[:12]}, expected {sha256[:12]})")
        wanted = None if ids is None else set(ids)
        linked = {row.get("source_id"): row["participant_id"] for row in controlsets.linked_rows(self, entry["name"])}
        report = {"added": [], "updated": [], "removed": [], "skipped": [], "sha256": digest}
        for lesion in entry["lesions"]:
            if wanted is not None and lesion["id"] not in wanted:
                continue
            values = {
                "group": controlsets.GROUP,
                "lesion": controlsets.controlset_path(entry["name"], lesion["rel"]),
                "space": lesion.get("space") or entry.get("space"),
                "status": "traced",
                "source_type": controlsets.SOURCE_TYPE,
                "source_dataset": entry["name"],
                "source_id": lesion["id"],
                "volume_ml": lesion.get("volume_ml"),
                "dimensionality": lesion.get("dimensionality"),
                "hemisphere": lesion.get("hemisphere"),
            }
            sid = linked.get(lesion["id"])
            if sid is not None:
                self.set(sid, values)
                report["updated"].append(sid)
                continue
            sid = controlsets.study_participant_id(entry["name"], lesion["id"])
            if sid in self._index:
                report["skipped"].append({"id": lesion["id"], "reason": f"{sid} is already a subject of the study"})
                continue
            twin = self.case_twin(sid)
            if twin is not None:
                report["skipped"].append({"id": lesion["id"], "reason": _case_twin_message(sid, twin)})
                continue
            self.add_subject(sid, values)
            report["added"].append(sid)
        keep = {lesion["id"] for lesion in entry["lesions"] if wanted is None or lesion["id"] in wanted}
        for row in controlsets.linked_rows(self, entry["name"]):
            if row.get("source_id") not in keep:
                self.remove_subject(row["participant_id"])
                report["removed"].append(row["participant_id"])
        sets = [item for item in self.control_sets if item["name"] != entry["name"]]
        if controlsets.linked_rows(self, entry["name"]):
            sets.append({"name": entry["name"], "sha256": digest})
        if sets:
            self.metadata["control_sets"] = sets
        else:
            self.metadata.pop("control_sets", None)
        return report

    def unlink_control_set(self, name):
        """Remove the rows linked from a control set and its study.json record (the set itself is kept); return the removed ids."""
        from . import controlsets

        removed = [row["participant_id"] for row in controlsets.linked_rows(self, name)]
        for sid in removed:
            self.remove_subject(sid)
        sets = [item for item in self.control_sets if item["name"] != name]
        if sets:
            self.metadata["control_sets"] = sets
        else:
            self.metadata.pop("control_sets", None)
        return removed

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

    def unsaved_rows(self):
        """The cells changed in memory since the files were last read or written: ``({sid: {column: value}}, added, removed)``.

        ``added`` and ``removed`` are the participant ids of rows added or removed in memory (sorted).
        """
        base = (self._baseline or {}).get("rows", {})
        changed, added = {}, []
        for row in self._rows:
            sid = row["participant_id"]
            if sid not in base:
                added.append(sid)
                continue
            values = {column: value for column, value in row.items() if column != "participant_id" and value != base[sid].get(column, NA)}
            if values:
                changed[sid] = values
        removed = sorted(sid for sid in base if sid not in self._index)
        return changed, sorted(added), removed

    def keep_unsaved(self, reason, source="unsaved"):
        """Keep the cells changed in memory that :meth:`save` could not write (participants.tsv on disk cannot be read, or
        it stayed locked) as an inbox file, which the next session merges like a job result (:meth:`apply_inbox`)
        once the table can be read again. Nothing is written when no cell changed.

        Rows added or removed in memory cannot be posted (the inbox changes existing rows only); they are returned.

        :return: ``(inbox file path or None, [participant ids kept], [ids added or removed that are not kept])``.
        """
        changed, added, removed = self.unsaved_rows()
        path = None
        if changed:
            # The study's own folder is not named in the record (no local path in the study's files)
            reason = str(reason).replace(self.path + os.sep, "").replace(self.path, ".")
            audit = {"event": "unsaved_kept", "reason": reason, "sids": sorted(changed), "at": now_iso()}
            path = Study.post_results(self.path, changed, source, audit=audit)
        return path, sorted(changed), added + removed

    def apply_inbox(self, save=True, hold=None):
        """Merge the values that jobs posted (:meth:`post_result`), oldest first.

        Each subject's values are validated together (all or nothing). Unknown subjects and invalid
        values are listed in :attr:`inbox_errors`. ``hold(participant_id, values)`` (optional) returns
        True for values that must wait: a file with such values is left in the inbox, untouched, for a
        later merge, and is listed in :attr:`inbox_held`. With ``save``, the study is then saved (with merge);
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
        self.inbox_held = []
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
            if not errors and hold is not None:
                held = [sid for sid, values in rows.items() if sid in self._index and isinstance(values, dict) and hold(sid, values)]
                if held:
                    self.inbox_held.append((path, held))
                    continue
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
        and unknown subjects are added. All values are validated before anything changes: the standard
        columns with fixed values take their other spellings (:func:`canonical_value`: include "yes" or
        "1.0" is 1) and refuse values that mean none of them.

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
                canonical = canonical_value(column, value)
                if canonical is not None:
                    values[column] = canonical
                else:
                    _normalize(column, format_value(value))  # strict columns raise; set() records the warnings
            prepared.append((sid, values))
        if len({sid for sid, _ in prepared}) != len(prepared):
            raise ValueError(f"{path} has duplicate participant ids")
        first = {}
        for sid, _ in prepared:
            twin = first.setdefault(sid.casefold(), sid)
            if twin == sid:
                twin = self.case_twin(sid)
            if twin is not None:
                raise ValueError(f"{path}: {_case_twin_message(sid, twin)}")
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

    def add_files(self, paths, lesion_rule=None, id_regex=None, root=None):
        """Add scans and lesion masks, paired by subject id (only NIfTI headers are read).

        :param paths: NIfTI files and/or folders (searched recursively; hidden folders and the
          study's own derivatives and .studio folders are skipped). BIDS layouts work: the subject
          id is searched in the file name, then in the parent folders (sub-01/anat/...), up to the
          folder that was chosen (a folder of ``paths``, else ``root``): folders above it are read only
          when the chosen folder is itself a modality or session folder (sub-01/anat chosen: sub-01).
        :param lesion_rule: regular expression (case-insensitive) that marks lesion files by name, matched
          as whole words (see :func:`whole_word_rule`: "sub-01_desc-lesion_mask" and "LesionSmooth" are
          lesions, "sub-01_T1w_masked" and "Segura_01_T1" are scans); default: the study's rule
          ("lesion|mask|seg|label|roi"). Brain masks, deface masks and anatomical segmentations
          (brain_mask, defacemask, dseg, aseg, ...) are skipped unless their name contains "lesion".
        :param id_regex: regular expression of subject ids (default: the study's rule, "sub-[A-Za-z0-9]+").
          Files without a match use the start of their name: the words before the first modality or
          lesion word (T1, FLAIR, lesion, ...) up to the first word with a digit ("patient_01_T1.nii.gz":
          "patient01"; "sub_01_lesion.nii.gz": "01"; "Patient 7 T1.nii": "Patient7"), or the first
          word when there is no digit ("smith_T1.nii": "smith"). A name made only of such words
          ("T1.nii.gz", "lesion.nii.gz") takes its id the same way from the nearest folder below the
          chosen folder that gives one ("patients/patient01/T1.nii.gz": "patient01").
        :param root: the folder that ``paths`` given as files were chosen in (``createStudy()``: the
          study folder); without it, such files search three parent folders for ``id_regex`` and
          take no id from a folder name.
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
        canonical = {}  # casefolded id: the id its files go to
        root = os.path.abspath(os.fspath(root)) if root else None
        for path, chosen_root in self._expand(paths, root):
            stem = _nifti_stem(path)
            if stem is None:
                continue
            try:
                header = read_nifti_header(path)
            except (OSError, EOFError, ValueError, zlib.error) as exc:
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
                raw_id, id_source = self._subject_id_and_source(path, stem, id_re, lesion_re, chosen_root)
                sid = participant_label(raw_id)
            except ValueError:
                report["skipped"].append({"path": path, "reason": "no subject id"})
                continue
            if sid != raw_id:
                # Not a BIDS id: the row keeps the file's stem (or the folder name the id came from) as source_id
                original_stems[path] = id_source
            # Ids differing only in letter case name one subject (one file name on macOS): "patient01_T1w.nii.gz"
            # belongs to the row sub-Patient01 of the participants table, and files spelling one new id in
            # different cases go to one new subject
            sid = canonical.setdefault(sid.casefold(), sid if sid in self._index else (self.case_twin(sid) or sid))
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
            # A scan on the lesion's own grid first, then one of the lesion's BIDS session (ses-2 lesion: the ses-2 scan,
            # not the ses-1 scan that sorts first), then one in its template space
            lesion_session = _bids_session(chosen["lesion"][0]) if chosen["lesion"] else None
            scans = sorted(kinds["scan"], key=lambda item: (
                chosen["lesion"] is not None and not _same_grid(item[1], chosen["lesion"][1]),
                lesion_session is not None and _bids_session(item[0]) != lesion_session,
                lesion_space is not None and file_space(item) != lesion_space,
                "t1" not in os.path.basename(item[0]).lower(),
                item[0],
            ))
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
                if value is not None and not is_missing(row.get(column)) and not self._same_file(row[column], value):
                    report["skipped"].append({"path": self.abspath(value), "reason": f"{sid} already has a {column}"})
            if updates and source_id and is_missing(row.get("source_id")):
                updates.update(source_id)
            if updates:
                self.set(sid, updates)
                report["updated"].append(sid)
        return report

    def _expand(self, paths, root=None):
        """Yield (file, the folder it was chosen in): a folder of ``paths``, else ``root`` (None when not given)."""
        if isinstance(paths, (str, os.PathLike)):
            paths = [paths]
        excluded = {os.path.join(self.path, DERIVATIVES_DIR, "lnm"), os.path.join(self.path, PRIVATE_DIR)}
        for path in paths:
            path = os.path.abspath(os.fspath(path))
            if os.path.isfile(path):
                yield path, root
                continue
            for folder, subfolders, files in os.walk(path):
                subfolders[:] = sorted(d for d in subfolders if not d.startswith(".") and os.path.join(folder, d) not in excluded)
                for name in sorted(files):
                    yield os.path.join(folder, name), path

    @staticmethod
    def _subject_id(path, stem, id_re, lesion_re, root=None):
        """The subject id of a file (see :meth:`add_files`); ValueError when it has none."""
        return Study._subject_id_and_source(path, stem, id_re, lesion_re, root)[0]

    @staticmethod
    def _subject_id_and_source(path, stem, id_re, lesion_re, root=None):
        """(subject id, the name it was taken from: the file's stem or a folder name); ValueError when there is none.

        ``root`` is the folder the file was chosen in: the parent folders are searched up to it (it included), and a stem
        without an id takes it from the nearest folder below it that gives one. Folders above it are read only when the
        chosen folder is itself a modality or session folder (sub-01/anat chosen: anat names no subject, sub-01 above it
        does). Without ``root`` (or for a file outside it), three parent folders are searched for ``id_re`` and no folder
        name gives an id.
        """
        ancestors = []  # nearest first, at most three: BIDS sub-<id>/[ses-<id>/]anat/<file>
        folder = os.path.dirname(path)
        while len(ancestors) < 3 and os.path.dirname(folder) != folder:
            ancestors.append(folder)
            folder = os.path.dirname(folder)
        if root is not None and not _is_relative_to(os.path.dirname(path), root):
            root = None
        # The folders up to the chosen one (it included), those strictly below it, and those above it
        folders, below, above = ancestors, ancestors, []
        if root is not None:
            for index, folder in enumerate(ancestors):
                if os.path.normcase(folder) == os.path.normcase(root):
                    folders, below, above = ancestors[: index + 1], ancestors[:index], ancestors[index + 1 :]
                    break

        def matched(match):
            if "id" in id_re.groupindex:
                return match.group("id")
            return match.group(1) if id_re.groups else match.group(0)

        source = stem
        match = id_re.search(stem)
        for folder in folders:
            if match is not None:
                break
            source = os.path.basename(folder)
            match = id_re.search(source)
        if match is not None:
            return matched(match), source
        try:
            return Study._id_from_name(stem, lesion_re, path), stem
        except ValueError:
            if root is None:
                raise
        # patients/patient01/T1.nii.gz: the subject folder names the subject (not the chosen folder itself)
        for folder in below:
            try:
                return Study._id_from_name(os.path.basename(folder), lesion_re, path), os.path.basename(folder)
            except ValueError:
                continue  # anat/, ses-01/: modality words
        # sub-01/anat chosen: the chosen folder is a modality or session folder, so the subject folder lies above it;
        # any other chosen folder ("patients") bounds the search, so that an ancestor such as "sub-generic" never names
        # every subject
        if above and not Study._names_subject(os.path.basename(root), lesion_re):
            for folder in above:
                match = id_re.search(os.path.basename(folder))
                if match is not None:
                    return matched(match), os.path.basename(folder)
        raise ValueError(f"No subject id in {path}")

    @staticmethod
    def _names_subject(name, lesion_re):
        """Whether a folder name can name a subject: not one made only of modality or lesion words (anat, ses-01, lesions)."""
        try:
            Study._id_from_name(name, lesion_re, name)
        except ValueError:
            return False
        return True

    @staticmethod
    def _id_from_name(stem, lesion_re, path):
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

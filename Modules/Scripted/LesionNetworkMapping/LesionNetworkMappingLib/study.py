"""Lesion studies stored as plain files (no Slicer dependency), BIDS-compatible where it matters.

Folder layout::

    <study>/
      study.json          {"format": "slicer-studio-study", "version": 1, "name", "created", "template",
                           "space", "rules": {"lesion": <regex>, "id": <regex>}, "connectomes", "software"}
      participants.tsv    one row per subject; participant_id first, "n/a" for missing values
      lesions/            sub-<id>_space-<space>_desc-lesion_mask.nii.gz
      derivatives/lnm/    network-mapping outputs (see export.py)
      .studio/            thumbs/, journal/ (application state, ignored by analyses)

``participants.tsv`` is the single source of truth about subjects. Its standard columns are
:data:`STANDARD_COLUMNS`; any other columns (e.g. clinical scores) are kept with their order. Paths
in the table are relative to the study folder (with "/" separators).

Changes are made in memory (:meth:`Study.set`, :meth:`Study.add_files`, ...) and written by
:meth:`Study.save`, which replaces both files atomically. Only NIfTI headers are read.
"""

import datetime
import json
import math
import os
import re

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
DEFAULT_RULES = {"lesion": "lesion|mask|seg|label|roi", "id": "sub-[A-Za-z0-9]+"}
NIFTI_EXTENSIONS = (".nii.gz", ".nii")
# Never lesions, even though they match the default lesion rule: brain masks and anatomical segmentations
_ANATOMICAL = re.compile(r"brain[_-]?mask|desc-brain|dseg|probseg|aseg|aparc|wmparc", re.IGNORECASE)
# Words that do not identify a subject in non-BIDS file names (e.g. "patient01_T1.nii.gz")
_SCAN_WORDS = {"t1", "t1w", "t2", "t2w", "flair", "mprage", "anat", "scan", "image", "img", "brain", "mni", "dwi", "bold"}
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
        lines = [(number, line.rstrip("\r")) for number, line in enumerate(f.read().split("\n"), start=1)]
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
    lines = ["\t".join(columns)]
    lines += ["\t".join(format_value(row.get(column)) for column in columns) for row in rows]
    _write_text_atomic(path, "\n".join(lines) + "\n")


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


def _included(row):
    return str(row.get("include", "1")).strip().lower() not in ("0", "false", "no")


class Study:
    """A study folder: ``study.json`` metadata plus the ``participants.tsv`` table (see the module docstring).

    Rows are dicts of TSV text keyed by column name; change them through :meth:`set` so that
    values are validated and formatted, then call :meth:`save`.
    """

    def __init__(self, path, metadata, columns, rows):
        self.path = os.path.abspath(os.fspath(path))
        self.metadata = metadata
        self._columns = list(columns)
        self._rows = []
        self._index = {}
        for row in rows:
            self._insert(row)

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
        """Open a study folder (or its study.json)."""
        path = os.path.abspath(os.fspath(path))
        if os.path.basename(path) == STUDY_JSON and os.path.isfile(path):
            path = os.path.dirname(path)
        with open(os.path.join(path, STUDY_JSON), encoding="utf-8") as f:
            metadata = json.load(f)
        if not isinstance(metadata, dict) or metadata.get("format") != STUDY_FORMAT:
            raise ValueError(f"Not a study: {os.path.join(path, STUDY_JSON)}")
        if int(metadata.get("version", 0)) > STUDY_VERSION:
            raise ValueError(f"Study version {metadata.get('version')} is newer than supported ({STUDY_VERSION})")
        tsv = os.path.join(path, PARTICIPANTS_TSV)
        columns, rows = read_tsv(tsv) if os.path.isfile(tsv) else ([], [])
        if columns and "participant_id" not in columns:
            raise ValueError(f"{tsv} has no participant_id column")
        # participant_id first, other columns in file order, then the missing standard columns
        columns = ["participant_id"] + [c for c in columns if c != "participant_id"]
        columns += [c for c in STANDARD_COLUMNS if c not in columns]
        return cls(path, metadata, columns, rows)

    @staticmethod
    def is_study(path):
        """True if the folder contains a study.json."""
        return os.path.isfile(os.path.join(os.fspath(path), STUDY_JSON))

    def save(self):
        """Write participants.tsv and study.json (each through a temporary file and an atomic rename)."""
        write_tsv(os.path.join(self.path, PARTICIPANTS_TSV), self._columns, self._rows)
        _write_text_atomic(os.path.join(self.path, STUDY_JSON), json.dumps(self.metadata, indent=2, ensure_ascii=False) + "\n")

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
        """The rows (dicts of TSV text), in table order."""
        return list(self._rows)

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
        row = self.subject(sid)
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

    def subject(self, sid):
        """The row of a subject (KeyError if unknown)."""
        try:
            return self._index[sid]
        except KeyError:
            raise KeyError(f"Unknown subject {sid!r} in study {self.name!r}") from None

    def value(self, sid, column, cast=str):
        """A cell converted with ``cast`` (None when missing, "n/a" or not convertible)."""
        text = self.subject(sid).get(column, NA)
        if is_missing(text):
            return None
        try:
            return cast(text)
        except (TypeError, ValueError):
            return None

    def add_subject(self, sid, values=None, **fields):
        """Add a subject (its id is normalized with :func:`participant_label`) and return its row."""
        sid = participant_label(sid)
        if sid in self._index:
            raise ValueError(f"Subject {sid} is already in the study")
        row = self._insert({"participant_id": sid})
        try:
            self.set(sid, values, **fields)
        except Exception:
            self.remove_subject(sid)
            raise
        return row

    def remove_subject(self, sid):
        """Remove a subject's row (its files are kept)."""
        row = self.subject(sid)
        self._rows.remove(row)
        del self._index[sid]

    def set(self, sid, values=None, **fields):
        """Set cells of a subject's row; unknown columns are added at the end of the table.

        :param values: optional {column: value} (for column names that are not Python identifiers).
        :raises ValueError: invalid group, include, status or qc values (nothing is changed then).
        """
        row = self.subject(sid)
        updates = {**(values or {}), **fields}
        if "participant_id" in updates and updates["participant_id"] != sid:
            raise ValueError("participant_id cannot be changed")
        texts = {column: format_value(value) for column, value in updates.items()}
        for column, text in texts.items():
            _validate(column, text)
        for column, text in texts.items():
            if column not in self._columns:
                self._columns.append(column)
                for other in self._rows:
                    other.setdefault(column, NA)
            row[column] = text
        return row

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
        """Non-standard columns whose non-missing values are all numbers (e.g. symptom scores)."""
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
                _validate(column, format_value(value))
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
        :param lesion_rule: regular expression (case-insensitive) that marks lesion files by name;
          default: the study's rule ("lesion|mask|seg|label|roi"). Brain masks and anatomical
          segmentations (brain_mask, dseg, aseg, ...) are never taken as lesions unless their name
          contains "lesion".
        :param id_regex: regular expression of subject ids (default: the study's rule, "sub-[A-Za-z0-9]+").
          Files without a match use the first word of their name ("patient01_T1.nii.gz": "patient01").
        :return: {"added": [ids], "updated": [ids], "skipped": [{"path", "reason"}], "pairs": {id: {"scan", "lesion"}},
          "warnings": [{"path", "message"}]} (files that are added but lie at different places in
          different programs because their sform and qform disagree).
        """
        from .geometry import detect_space
        from .nifti import XFORM_WARNING_MM, read_nifti_header, xform_disagreement

        rules = self.rules
        lesion_re = re.compile(lesion_rule or rules["lesion"], re.IGNORECASE)
        id_re = re.compile(id_regex or rules["id"])
        report = {"added": [], "updated": [], "skipped": [], "pairs": {}, "warnings": []}
        candidates = {}
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
            is_lesion = bool(lesion_re.search(stem))
            if is_lesion and _ANATOMICAL.search(stem) and "lesion" not in stem.lower():
                report["skipped"].append({"path": path, "reason": "brain mask or anatomical segmentation"})
                continue
            try:
                sid = participant_label(self._subject_id(path, stem, id_re, lesion_re))
            except ValueError:
                report["skipped"].append({"path": path, "reason": "no subject id"})
                continue
            kind = "lesion" if is_lesion else "scan"
            candidates.setdefault(sid, {"scan": [], "lesion": []})[kind].append((path, header))
            disagreement = xform_disagreement(header)
            if disagreement > XFORM_WARNING_MM:
                report["warnings"].append({
                    "path": path,
                    "message": f"The sform and qform disagree by up to {disagreement:.2g} mm: Slicer may show this image at another place than FSL and the network mapping, which use the sform.",
                })

        for sid, kinds in candidates.items():
            chosen = {}
            for kind, preferred in (("scan", re.compile(r"t1", re.IGNORECASE)), ("lesion", re.compile(r"lesion", re.IGNORECASE))):
                files = sorted(kinds[kind], key=lambda item: (not preferred.search(os.path.basename(item[0])), item[0]))
                chosen[kind] = files[0] if files else None
                for path, _ in files[1:]:
                    report["skipped"].append({"path": path, "reason": f"another {kind} was chosen for {sid}"})
            space_source = chosen["scan"] or chosen["lesion"]
            space = detect_space(space_source[1]["shape"][:3], space_source[1]["affine"])
            values = {"scan": self.relpath(chosen["scan"][0]) if chosen["scan"] else None, "lesion": self.relpath(chosen["lesion"][0]) if chosen["lesion"] else None}
            report["pairs"][sid] = {kind: chosen[kind][0] if chosen[kind] else None for kind in ("scan", "lesion")}
            if sid not in self._index:
                self.add_subject(sid, {**values, "space": space, "status": "traced" if chosen["lesion"] else "untraced"})
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
        words = [word for word in re.split(r"[_.\s]+", stem) if word and word.lower() not in _SCAN_WORDS and not lesion_re.fullmatch(word)]
        if not words:
            raise ValueError(f"No subject id in {path}")
        return words[0]

"""Typed roles of the participants.tsv columns of lesion studies (standard library only).

Every column of a study has a :class:`ColumnSpec`: a value type (:data:`TYPES`), a role in analyses
(:data:`ROLES`), optional levels, units and a description. Design matrices, score pickers and
reports are built from declared roles, never from "all numeric columns" (which would offer
bookkeeping columns such as ``fit_rms_mm`` as symptom scores).

Where a column's spec comes from (see ``Study.column_spec``):

1. the study's ``participants.json`` when the user confirmed the column's role there;
2. this registry: :data:`STANDARD_ROLES` for ``study.STANDARD_COLUMNS`` and :data:`OPTIONAL_COLUMNS`
   for the optional columns (source, imaging, case, workflow and analysis columns). Optional
   columns are never added to a study; they appear in ``participants.tsv`` only when first written.
   Later work packages add columns with :func:`register_column`;
3. otherwise the spec is inferred from the values (:func:`infer_spec`) with role ``"unknown"``.

``participants.json`` is a BIDS data dictionary (``Description``, ``Levels``, ``Units`` per column)
with a fork key ``"SlicerStudio": {"type", "role", "confirmed"}`` per column. BIDS tools and official
Slicer ignore the fork key; keys this module does not know are preserved (:func:`read_dictionary`,
:func:`write_dictionary`).
"""

import dataclasses
import json
import math
import os
import re
import threading

NA = "n/a"
ROLES = ("id", "group", "score", "covariate", "block", "metadata", "bookkeeping", "path")
UNKNOWN_ROLE = "unknown"
TYPES = ("numeric", "categorical", "ordinal", "text", "path", "date")
SECTIONS = ("source", "imaging", "case", "workflow", "analysis")
FORK_KEY = "SlicerStudio"
DICTIONARY_JSON = "participants.json"
#: Inferred columns with at most this many distinct values are categorical
MAX_CATEGORIES = 20


@dataclasses.dataclass(frozen=True)
class ColumnSpec:
    """Type, role and allowed values of a participants.tsv column.

    :param levels: {level: description or None} of categorical and ordinal columns (a sequence of
      levels is accepted), or None.
    :param strict: invalid values raise ValueError; otherwise they are kept with a warning.
    :param synonyms: {lower-case text: level} accepted on input ("female" gives "F").
    :param missing_ok: whether "n/a" is a valid value (False only for include, status and qc).
    :param separator: multi-valued columns (``trace_method`` "manual+smartfill"): the token separator.
    :param token_pattern: regular expression that every token must match (lenient columns warn).
    """

    name: str
    type: str = "text"
    role: str = "metadata"
    levels: dict | None = None
    units: str | None = None
    description: str | None = None
    strict: bool = False
    synonyms: dict | None = None
    section: str = "case"
    missing_ok: bool = True
    separator: str | None = None
    token_pattern: str | None = None

    def __post_init__(self):
        if self.type not in TYPES:
            raise ValueError(f"Column {self.name!r}: unknown type {self.type!r}; expected one of {', '.join(TYPES)}")
        if self.role not in (*ROLES, UNKNOWN_ROLE):
            raise ValueError(f"Column {self.name!r}: unknown role {self.role!r}; expected one of {', '.join(ROLES)}")
        if self.section not in SECTIONS:
            raise ValueError(f"Column {self.name!r}: unknown section {self.section!r}; expected one of {', '.join(SECTIONS)}")
        if self.levels is not None and not isinstance(self.levels, dict):
            object.__setattr__(self, "levels", dict.fromkeys(str(level) for level in self.levels))
        if self.synonyms:
            object.__setattr__(self, "synonyms", {str(key).lower(): value for key, value in self.synonyms.items()})

    def __hash__(self):
        # The generated hash would fail on the dict fields; equal specs share these fields
        return hash((self.name, self.type, self.role, self.strict, self.section))

    def replace(self, **changes):
        """A copy with some fields changed (``dataclasses.replace``)."""
        return dataclasses.replace(self, **changes)

    def bids_entry(self):
        """BIDS data-dictionary keys of this column: Description, Levels, Units (only those that are set)."""
        entry = {}
        if self.description:
            entry["Description"] = self.description
        if self.levels:
            entry["Levels"] = {level: (text if text else level) for level, text in self.levels.items()}
        if self.units:
            entry["Units"] = self.units
        return entry


def _spec(name, type="text", role="metadata", section="case", **fields):  # noqa: A002 (mirrors the field name)
    return ColumnSpec(name=name, type=type, role=role, section=section, **fields)


STANDARD_ROLES = {
    spec.name: spec
    for spec in (
        _spec("participant_id", role="id", description="Participant identifier (sub-<label>)"),
        _spec(
            "group",
            "categorical",
            "group",
            levels={"case": "Lesion associated with the symptom", "control": "Control lesion", NA: "Not assigned"},
            strict=True,
            description="Case or control",
        ),
        _spec("include", "categorical", "bookkeeping", "workflow", levels={"1": "Included in analyses", "0": "Excluded"}, strict=True, missing_ok=False, description="Whether analyses use this subject"),
        _spec(
            "status",
            "categorical",
            "bookkeeping",
            "workflow",
            levels={"untraced": None, "inprogress": None, "traced": None, "mapped": None},
            strict=True,
            missing_ok=False,
            description="Tracing and mapping progress",
        ),
        _spec("qc", "categorical", "bookkeeping", "workflow", levels={"unreviewed": None, "pass": None, "fail": None}, strict=True, missing_ok=False, description="Lesion quality control"),
        _spec("scan", "path", "path", "imaging", description="Scan the lesion was traced on (path relative to the study)"),
        _spec("lesion", "path", "path", "imaging", description="Lesion mask (path relative to the study)"),
        _spec("space", "categorical", "metadata", "imaging", description="Template space of the lesion, or native"),
        _spec("volume_ml", "numeric", "covariate", "analysis", units="mL", description="Lesion volume"),
        _spec("n_parts", "numeric", "bookkeeping", "analysis", description="Number of connected parts of the lesion"),
        _spec("gaps", "text", "bookkeeping", "analysis", description="Slices without lesion between traced slices"),
        _spec("centroid_x", "numeric", "metadata", "analysis", units="mm", description="Lesion centroid, x (RAS)"),
        _spec("centroid_y", "numeric", "metadata", "analysis", units="mm", description="Lesion centroid, y (RAS)"),
        _spec("centroid_z", "numeric", "metadata", "analysis", units="mm", description="Lesion centroid, z (RAS)"),
        _spec("outside_brain_pct", "numeric", "bookkeeping", "analysis", units="%", description="Percentage of the lesion outside the brain mask"),
        _spec("rater", "text", "bookkeeping", "workflow", description="Who traced the lesion"),
        _spec("trace_seconds", "numeric", "bookkeeping", "workflow", units="s", description="Tracing time"),
        _spec("modified", "date", "bookkeeping", "workflow", description="Last change of the lesion (ISO 8601)"),
        _spec("notes", "text", "metadata", description="Free-text notes"),
    )
}

_BINARY = {"0": "No", "1": "Yes"}
_BINARY_SYNONYMS = {"no": "0", "false": "0", "n": "0", "yes": "1", "true": "1", "y": "1"}
TRACE_METHOD_TOKENS = r"manual|figure|smartfill|auto:[A-Za-z0-9_.-]+|imported|review-edit"

OPTIONAL_COLUMNS = {
    spec.name: spec
    for spec in (
        # Source of the lesion
        _spec("record_id", section="source", description="Source record (sources/records.tsv)"),
        _spec(
            "source_type",
            "categorical",
            section="source",
            levels={"own_scan": "Scan acquired by the study", "case_report": "Published case report", "dataset": "Shared dataset", "control_db": "Control lesion database"},
            strict=True,
            description="Where the lesion comes from",
        ),
        _spec("source_dataset", section="source", description="Dataset the subject comes from"),
        _spec("source_id", section="source", description="Subject identifier in the source (original file name or id)"),
        _spec("doi", section="source", description="DOI of the source publication"),
        _spec("pmid", section="source", description="PubMed identifier of the source publication"),
        _spec("citation", section="source", description="Citation of the source publication"),
        _spec("figure", section="source", description="Figure of the source publication"),
        _spec("panel", section="source", description="Figure panel"),
        _spec("case_label", section="source", description="Case label in the source publication"),
        # Imaging
        _spec(
            "image_modality",
            "categorical",
            section="imaging",
            levels=("CT", "DWI", "FLAIR", "T2", "T1", "SWI", "other"),
            synonyms={"t1w": "T1", "t2w": "T2", "adc": "DWI", "dwi/adc": "DWI", "t2-flair": "FLAIR", "mprage": "T1"},
            description="Image the lesion was traced on",
        ),
        _spec(
            "fig_convention",
            "categorical",
            section="imaging",
            levels={"radiological": "Patient's left on the image right", "neurological": "Patient's left on the image left", "unknown": None},
            strict=True,
            description="Left-right convention of the source figure",
        ),
        _spec("planes", section="imaging", description="Planes traced (for example axial)"),
        _spec("n_slices", "numeric", "bookkeeping", "imaging", description="Number of traced slices"),
        _spec(
            "dimensionality",
            "categorical",
            "covariate",
            "imaging",
            levels={"2D": "Single slice", "3D": "Volume", "multi2D": "Several slices"},
            strict=True,
            description="Dimensionality of the traced lesion",
        ),
        _spec("extrude_mm", "numeric", "bookkeeping", "imaging", units="mm", description="Thickness a 2D tracing was extruded to"),
        _spec("fit_rms_mm", "numeric", "bookkeeping", "imaging", units="mm", description="RMS residual of the figure landmark fit"),
        _spec("trace_template", section="imaging", description="Template the lesion was traced on"),
        _spec("scan_native", "path", "bookkeeping", "imaging", description="Scan in native space, before normalization"),
        _spec("lesion_native", "path", "bookkeeping", "imaging", description="Lesion in native space, before normalization"),
        # Case
        _spec("dataset", "categorical", "block", description="Dataset (exchangeability block)"),
        _spec("age", "numeric", "covariate", units="years", description="Age"),
        _spec(
            "sex",
            "categorical",
            "covariate",
            levels={"M": "Male", "F": "Female", "O": "Other"},
            synonyms={"male": "M", "female": "F", "m": "M", "f": "F", "man": "M", "woman": "F", "other": "O"},
            description="Sex",
        ),
        _spec("handedness", "categorical", levels={"R": "Right", "L": "Left", "A": "Ambidextrous"}, synonyms={"right": "R", "left": "L", "ambidextrous": "A"}, description="Handedness"),
        _spec("etiology", "categorical", description="Lesion etiology (for example ischemic stroke)"),
        _spec("onset_to_imaging_days", "numeric", "covariate", units="days", description="Days from symptom onset to imaging"),
        _spec("symptom", description="Symptom"),
        _spec("domain", "categorical", description="Symptom domain"),
        _spec(
            "reported_side",
            "categorical",
            levels={"L": "Left", "R": "Right", "bilateral": None, "midline": None},
            synonyms={"left": "L", "right": "R", "both": "bilateral"},
            description="Lesion side reported by the source",
        ),
        _spec("reported_location", description="Lesion location reported by the source"),
        _spec(
            "hemisphere",
            "categorical",
            "covariate",
            levels={"L": "Left", "R": "Right", "bilateral": None},
            synonyms={"left": "L", "right": "R", "both": "bilateral"},
            description="Lesion hemisphere",
        ),
        # Workflow
        _spec("tracer", role="bookkeeping", section="workflow", description="Who traced the lesion"),
        _spec("reviewer", role="bookkeeping", section="workflow", description="Who reviewed the lesion"),
        _spec(
            "review_status",
            "categorical",
            "bookkeeping",
            "workflow",
            levels={"none": None, "pending": None, "approved": None, "changes": "Changes requested", "rejected": None},
            strict=True,
            description="Review state of the lesion",
        ),
        _spec("review_comment", role="bookkeeping", section="workflow", description="Reviewer comment"),
        _spec("image_quality", "ordinal", section="workflow", levels={"1": "Poor", "2": "Adequate", "3": "Good"}, description="Quality of the source image"),
        _spec(
            "trace_method",
            role="bookkeeping",
            section="workflow",
            separator="+",
            token_pattern=TRACE_METHOD_TOKENS,
            description="How the lesion was traced ('+'-joined: manual, figure, smartfill, auto:<tool>, imported, review-edit)",
        ),
        _spec("flipped", "categorical", "covariate", "workflow", levels=_BINARY, synonyms=_BINARY_SYNONYMS, description="Lesion mirrored left-right"),
        _spec("multifocal", "categorical", section="workflow", levels=_BINARY, synonyms=_BINARY_SYNONYMS, description="Lesion has several foci"),
        _spec("qc_flags", role="bookkeeping", section="workflow", separator=";", description="Quality-control flags (';'-joined)"),
        _spec("duplicate_of", role="bookkeeping", section="workflow", description="participant_id this lesion duplicates"),
        _spec("norm_method", role="bookkeeping", section="workflow", description="Normalization method"),
        _spec("norm_qc", "categorical", "bookkeeping", "workflow", levels={"unreviewed": None, "pass": None, "fail": None}, strict=True, description="Normalization quality control"),
        _spec("norm_suggestion", "categorical", "bookkeeping", "workflow", levels={"looks-ok": None, "review": None}, description="Automatic normalization QC suggestion"),
        _spec("norm_fingerprint", role="bookkeeping", section="workflow", description="Fingerprint of the normalization inputs and settings"),
        # Analysis
        _spec("gm_frac", "numeric", "covariate", "analysis", description="Fraction of the lesion in gray matter"),
        _spec("wm_frac", "numeric", "covariate", "analysis", description="Fraction of the lesion in white matter"),
        _spec("csf_frac", "numeric", "covariate", "analysis", description="Fraction of the lesion in cerebrospinal fluid"),
        _spec("assist_pct", "numeric", "bookkeeping", "analysis", units="%", description="Percentage of lesion voxels added by assisted tools"),
        _spec("fractional_sum", "numeric", "bookkeeping", "analysis", units="mL", description="Sum of fractional lesion values"),
        _spec("lesion_mtime", "numeric", "bookkeeping", "analysis", description="Modification time of the lesion file (metric cache)"),
        _spec("lesion_size", "numeric", "bookkeeping", "analysis", units="bytes", description="Size of the lesion file (metric cache)"),
        _spec("volume_native_ml", "numeric", "metadata", "analysis", units="mL", description="Lesion volume in native space"),
    )
}

_registry_lock = threading.Lock()


def registered(name):
    """The registered spec of a column (standard or optional), or None."""
    return STANDARD_ROLES.get(name) or OPTIONAL_COLUMNS.get(name)


def registry():
    """{name: spec} of every registered column: the standard columns, then the optional ones."""
    return {**STANDARD_ROLES, **OPTIONAL_COLUMNS}


def register_column(spec):
    """Register an optional column (later work packages add their columns this way).

    Registering the same spec again is allowed.

    :raises ValueError: for a standard column, or when another spec is registered under that name.
    """
    if not isinstance(spec, ColumnSpec):
        raise TypeError("register_column expects a ColumnSpec")
    if spec.name in STANDARD_ROLES:
        raise ValueError(f"Column {spec.name!r} is a standard column; it cannot be registered")
    with _registry_lock:
        existing = registered(spec.name)
        if existing is not None and existing != spec:
            raise ValueError(f"Column {spec.name!r} is already registered with another spec")
        OPTIONAL_COLUMNS[spec.name] = spec
    return spec


def is_missing(text):
    return text is None or text in ("", NA)


def _number(text):
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _match_level(spec, text):
    """The level a text stands for (exact, case-insensitive, synonym or equal number), or None."""
    if text in spec.levels:
        return text
    lowered = text.lower()
    for level in spec.levels:
        if level.lower() == lowered:
            return level
    if spec.synonyms and lowered in spec.synonyms:
        return spec.synonyms[lowered]
    number = _number(text)
    if number is not None:
        for level in spec.levels:
            if _number(level) == number:
                return level
    return None


def normalize_value(column, text):
    """Check and normalize a cell of a registered column.

    :param text: the cell as TSV text (see ``study.format_value``).
    :return: (text, warning or None). Synonyms become their level ("female" gives "F") and levels
      are matched without case. Unknown values of lenient columns are kept, with a warning.
      Columns that are not registered are returned unchanged.
    :raises ValueError: for an invalid value of a strict column.
    """
    spec = registered(column)
    if spec is None:
        return text, None
    return normalize_with_spec(spec, text)


def normalize_with_spec(spec, text):
    """:func:`normalize_value` with an explicit spec."""
    if is_missing(text):
        if not spec.missing_ok:
            raise ValueError(f"Invalid {spec.name} value {text!r}; expected one of {', '.join(spec.levels or ())}")
        return NA, None
    text = str(text)
    if spec.levels:
        if spec.separator is None:
            level = _match_level(spec, text)
            if level is not None:
                return level, None
            message = f"Unknown {spec.name} value {text!r}; expected one of {', '.join(spec.levels)}"
            if spec.strict:
                raise ValueError(f"Invalid {spec.name} value {text!r}; expected one of {', '.join(spec.levels)}")
            return text, message
    if spec.separator is not None:
        tokens = [token.strip() for token in text.split(spec.separator)]
        unknown = [token for token in tokens if not token or (spec.token_pattern and not re.fullmatch(spec.token_pattern, token)) or (spec.levels and _match_level(spec, token) is None)]
        if unknown:
            message = f"Unknown {spec.name} token(s) {', '.join(repr(token) for token in unknown)}"
            if spec.strict:
                raise ValueError(message)
            return text, message
        return text, None
    if spec.type == "numeric" and _number(text) is None:
        if spec.strict:
            raise ValueError(f"Invalid {spec.name} value {text!r}; expected a number")
        return text, f"{spec.name} value {text!r} is not a number"
    if spec.type == "date":
        import datetime

        try:
            datetime.datetime.fromisoformat(text)
        except ValueError:
            if spec.strict:
                raise ValueError(f"Invalid {spec.name} value {text!r}; expected an ISO 8601 date") from None
            return text, f"{spec.name} value {text!r} is not an ISO 8601 date"
    return text, None


def infer_spec(name, values):
    """Spec of a column that is neither registered nor confirmed, from its values (role "unknown").

    Numeric when every non-missing value is a finite number; categorical when there are at most
    :data:`MAX_CATEGORIES` distinct values; text otherwise (also when every value is missing).
    """
    present = [str(value) for value in values if not is_missing(value)]
    if not present:
        return ColumnSpec(name=name, type="text", role=UNKNOWN_ROLE)
    if all(_number(value) is not None for value in present):
        return ColumnSpec(name=name, type="numeric", role=UNKNOWN_ROLE)
    distinct = sorted(set(present))
    if len(distinct) <= MAX_CATEGORIES:
        return ColumnSpec(name=name, type="categorical", role=UNKNOWN_ROLE, levels=dict.fromkeys(distinct))
    return ColumnSpec(name=name, type="text", role=UNKNOWN_ROLE)


def fork_entry(entry):
    """The "SlicerStudio" object of a data-dictionary entry ({} when there is none)."""
    fork = entry.get(FORK_KEY) if isinstance(entry, dict) else None
    return fork if isinstance(fork, dict) else {}


def is_confirmed(entry):
    """Whether the user confirmed the column's role in its data-dictionary entry."""
    return fork_entry(entry).get("confirmed") is True


def apply_entry(spec, entry, confirmed_only=True):
    """A spec updated from a data-dictionary entry.

    The BIDS keys (Description, Levels, Units) always apply; the SlicerStudio type and role apply
    only when the entry is confirmed (unless ``confirmed_only`` is False).
    """
    if not isinstance(entry, dict):
        return spec
    changes = {}
    if isinstance(entry.get("Description"), str) and entry["Description"]:
        changes["description"] = entry["Description"]
    if isinstance(entry.get("Units"), str) and entry["Units"]:
        changes["units"] = entry["Units"]
    levels = entry.get("Levels")
    if isinstance(levels, dict) and levels:
        changes["levels"] = {str(level): (text if isinstance(text, str) and text != level else None) for level, text in levels.items()}
        if spec.type in ("text", "numeric") and spec.role == UNKNOWN_ROLE:
            changes["type"] = "categorical"
    fork = fork_entry(entry)
    if fork and (is_confirmed(entry) or not confirmed_only):
        if fork.get("type") in TYPES:
            changes["type"] = fork["type"]
        if fork.get("role") in (*ROLES, UNKNOWN_ROLE):
            changes["role"] = fork["role"]
    return spec.replace(**changes) if changes else spec


def dictionary_entry(spec, confirmed, existing=None, overwrite_bids=False):
    """A participants.json entry for a column: the existing entry (unknown keys kept) plus the BIDS keys
    of the spec (only where missing, unless ``overwrite_bids``) and ``SlicerStudio: {type, role, confirmed}``.
    """
    entry = dict(existing) if isinstance(existing, dict) else {}
    for key, value in spec.bids_entry().items():
        if overwrite_bids or key not in entry:
            entry[key] = value
    fork = dict(fork_entry(entry))
    fork.update({"type": spec.type, "role": spec.role, "confirmed": bool(confirmed)})
    entry[FORK_KEY] = fork
    return entry


def read_dictionary(path):
    """Read a participants.json data dictionary ({} when the file does not exist).

    :raises ValueError: when the file is not a JSON object.
    """
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def dictionary_text(dictionary):
    """The JSON text :func:`write_dictionary` writes."""
    return json.dumps(dictionary, indent=2, ensure_ascii=False) + "\n"


def write_dictionary(path, dictionary):
    """Write a participants.json data dictionary atomically (temporary file and rename)."""
    write_text_atomic(path, dictionary_text(dictionary))


def write_text_atomic(path, text):
    """Write a text file through a temporary file in the same folder and an atomic rename."""
    path = os.fspath(path)
    folder, name = os.path.split(path)
    tmp_path = os.path.join(folder, f".{name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

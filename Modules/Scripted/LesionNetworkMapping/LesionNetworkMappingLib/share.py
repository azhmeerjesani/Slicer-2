"""Share-safe export of a lesion study: a BIDS-derivatives folder (or zip) and a NeuroVault-ready folder (standard library and numpy).

Only about one paper in eight shares its lesion masks, and the ones that do often ship the wrong files: figure crops of
copyrighted papers, scans, rater names, dates, absolute local paths, or a CC0 licence on maps computed from a connectome
whose data use terms say otherwise. :func:`export_dataset` writes a copy of a study that leaves those out by construction::

    <out>/
      dataset_description.json   DatasetType "derivative", GeneratedBy [{Name: "Slicer Studio", Version, CodeURL, Container}],
                                 SourceDatasets (connectome DOIs of the exported maps, OpenNeuro/DOI source datasets), License
      participants.tsv / .json   the chosen columns only (free text, tracer names and dates are off by default); ids pseudonymised
      sub-<id>/anat/sub-<id>_space-<sp>_desc-lesion_mask.nii.gz + .json      opt-in (template space only; headers scrubbed)
      sub-<id>/anat/sub-<id>_label-lesion_roi.nii.gz                         optional fMRIPrep alias (listed in .bidsignore)
      sub-<id>/anat/sub-<id>_label-lesion_space-<sp>_mask.nii.gz             optional lacuna alias (listed in .bidsignore)
      group/<map>.nii.gz + .json   opt-in unthresholded group maps, each with its data use terms
      neurovault/                  opt-in NeuroVault-ready folder (:func:`neurovault_folder`)
      sources/records.tsv          citation columns only
      README.md, CITATION.cff, CHANGES, LICENSE, .bidsignore

**Never exported**: ``sources/<record>/`` figures and PDFs, scans, native-space data, lesions linked from control sets
(their own licence governs them), ``.studio/`` and the autosave folder (nothing is ever walked: every file is chosen by
name). NIfTI files are copied with ``descrip``, ``aux_file``, ``db_name`` and header extensions removed; voxels and
geometry are unchanged.

**Pseudonyms.** With ``pseudonymise`` the participant ids become ``sub-P001``... in a random order (another letter when
the study's own ids already use that series); the mapping is kept only in ``<study>/.studio/share/<time>.json`` (never in
the export), with the options and the own-patient decision.

**Licence.** Always an explicit choice (``options["licence"]``). :func:`suggested_licence` proposes CC0 for the user's own
masks and nothing when GSP-derived maps are included: their sidecars carry ``terms: GSP-DUT-2014``, the GSP data use terms
note (:data:`GSP_TERMS_NOTE`) is shown and the export needs ``terms_acknowledged``. The app never claims licensing is cleared.

**Checklist** (:func:`plan`): ids without names or dates, sensitive columns, scrubbed headers, no absolute paths in JSON or
TSV, own-patient masks need an explicit decision, licence chosen. Items are "ok", "warn" or "blocker"; the export refuses
while a blocker is open and verifies the written files again (:func:`verify_export`) before it moves them into place.

:func:`neurovault_folder` writes unthresholded maps with ``images.json`` and ``images.csv`` from
``export.neurovault_metadata`` (df per map type, sign coding, GSP acknowledgement). Nothing is uploaded: this module opens
no network connection. :func:`validate_name` and :func:`validate_dataset` are a lightweight BIDS naming check.
"""

import csv
import datetime
import fnmatch
import io
import json
import os
import re
import secrets
import shutil
import zipfile

import numpy as np

from . import export
from . import schema
from .study import NA, PRIVATE_DIR, Study, is_included, is_missing

SHARE_FORMAT = "slicer-studio-share"
SHARE_VERSION = 1
#: Private record of each export (pseudonym mapping, options, own-patient decision), in the study's .studio/ only
SHARE_DIR = "share"
BIDS_VERSION = "1.10.0"
GENERATOR_NAME = "Slicer Studio"
CODE_URL = "https://github.com/azhmeerjesani/Slicer-2"
PSEUDONYM_PREFIX = "P"

#: Licence choices: id -> (title, URL). "other" takes the user's own text.
LICENCES = {
    "CC0-1.0": ("CC0 1.0 Universal (public domain dedication)", "https://creativecommons.org/publicdomain/zero/1.0/"),
    "CC-BY-4.0": ("Creative Commons Attribution 4.0 International", "https://creativecommons.org/licenses/by/4.0/"),
    "PDDL-1.0": ("Open Data Commons Public Domain Dedication and License 1.0", "https://opendatacommons.org/licenses/pddl/1-0/"),
    export.GSP_LICENCE: ("Same terms as the source data: GSP Open Access Data Use Terms (2014)", "https://doi.org/10.7910/DVN/25833"),
    "other": ("Other (your own text)", None),
}
SUGGESTED_OWN_MASKS_LICENCE = "CC0-1.0"
#: Neutral note shown with GSP-derived maps (SPEC_LNM_ADDENDUM 4.3: no CC0 default, never "licensing is cleared")
GSP_TERMS_NOTE = (
    "Some maps were computed with a connectome derived from the Brain Genomics Superstruct Project (GSP). The GSP Open Access "
    "Data Use Terms (2014) may apply to data derived from it, including these maps; as listed in the connectome catalog, they "
    "allow derived data to be redistributed only under the same terms. Read the terms and choose the licence "
    "yourself; Slicer Studio does not decide whether licensing is cleared. Maps of GSP-derived connectomes carry the GSP "
    "acknowledgement in their metadata."
)
IDENTIFIABILITY_WARNING = (
    "Lesion masks can identify a person when combined with other information (lesion shape and site, rare etiologies, "
    "dates). Share them only when your ethics approval and consent allow it."
)
NOTHING_UPLOADED = "Nothing was uploaded. Upload these files yourself at https://neurovault.org if you wish."

ALIASES = ("fmriprep", "lacuna")
DEFAULT_OPTIONS = {
    "name": None,  # dataset name (default: the study name)
    "authors": (),  # names for dataset_description.json and CITATION.cff
    "columns": None,  # participants.tsv columns (default: default_columns(study)); participant_id is always first
    "pseudonymise": True,
    "include_excluded": False,  # also rows with include = 0
    "lesions": False,  # opt-in lesion masks (template space only)
    "own_patient": None,  # "share" | "exclude": explicit decision for own-patient masks (required when there are any)
    "aliases": (),  # subset of ALIASES
    "maps": False,  # opt-in unthresholded group maps under group/
    "neurovault": False,  # also write neurovault/ with the exported maps
    "licence": None,  # explicit choice: a key of LICENCES
    "licence_text": None,  # text of an "other" licence
    "terms_acknowledged": False,  # the GSP data use terms note was read (required when GSP-derived maps are included)
    "zip": False,  # write <out> as a .zip archive
    "software": None,  # {"version", "code_url", "container": {"Type", "Tag", "URI"}} of the exporting application
}

#: Columns never offered (paths of the study's own files)
NEVER_EXPORTED_TYPES = ("path",)
#: Rater and reviewer names, free text and file names: offered, off by default
SENSITIVE_COLUMNS = {
    "rater": "tracer name",
    "tracer": "tracer name",
    "reviewer": "reviewer name",
    "review_comment": "free text",
    "notes": "free text",
    "source_id": "original file name or id",
    "modified": "date",
    "lesion_mtime": "date",
}
#: Columns that identify the publication a case comes from: on by default (published information)
CITATION_COLUMNS = ("record_id", "source_type", "source_dataset", "doi", "pmid", "citation", "figure", "panel", "case_label")
#: Columns of sources/records.tsv that are exported (citations only)
RECORD_COLUMNS = ("record_id", "doi", "pmid", "pmcid", "citation", "title", "authors", "year", "journal", "url")
#: Files a dataset may hold at its root
ROOT_FILES = ("dataset_description.json", "participants.tsv", "participants.json", "README", "README.md", "CHANGES", "LICENSE",
              "CITATION.cff", ".bidsignore")
#: Extensions that must never appear in an export (figures, documents, scenes, scans in other formats)
FORBIDDEN_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".bmp", ".svg", ".pdf", ".mrb", ".mrml", ".nrrd", ".nhdr",
                        ".seg.nrrd", ".dcm", ".mgz", ".mha", ".mhd", ".vtk", ".h5", ".npz", ".jsonl", ".lock", ".log")
#: Words of scan file names (a lesion export holds masks, never images)
_SCAN_SUFFIX = re.compile(r"_(T1w|T2w|FLAIR|T2star|PD|PDw|dwi|bold|sbref|CT|ct|scan|angio|swi|defacemask)\.nii(\.gz)?$")
#: Absolute local paths in text: POSIX home and system roots, drive letters, UNC paths, file URLs and "~/"
ABSOLUTE_PATH = re.compile(
    r"(?:^|[\s\"'(=,;\[])(?:/(?:home|Users|root|mnt|media|tmp|var|opt|Volumes|private|srv|data|net|usr|etc|scratch)/"
    r"|[A-Za-z]:[\\/]|\\\\[A-Za-z0-9]|file:/|~[/\\])")
_DATE_IN_ID = re.compile(r"(?:19|20)\d{2}[-_]?(?:0[1-9]|1[0-2])[-_]?(?:0[1-9]|[12]\d|3[01])|\d{1,2}(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\d{2,4}",
                         re.IGNORECASE)
#: Letter runs of ids that are not names
_ID_WORDS = {"sub", "subj", "subject", "case", "cases", "ctrl", "ctl", "control", "controls", "patient", "pat", "pt", "lit", "lesion", "stroke",
             "demo", "test", "fig", "figure", "panel", "study", "site", "scan", "mni", "arc", "soop", "atlas", "tumor", "tumour", "infarct",
             "bleed", "hem", "isch", "left", "right", "lh", "rh"}
#: BIDS entities in their required order (a lightweight subset for lesion and map files)
BIDS_ENTITIES = ("sub", "ses", "task", "acq", "ce", "rec", "run", "echo", "part", "hemi", "space", "seg", "res", "den", "label", "desc")
_ENTITY = re.compile(r"^([a-z]+)-([A-Za-z0-9]+)$")
_SUFFIX = re.compile(r"^[A-Za-z0-9]+$")
_LABEL = re.compile(r"^[A-Za-z0-9]+$")
_GROUP_MAP = re.compile(r"_desc-([A-Za-z0-9]+)_map\.nii(?:\.gz)?$")
_OPENNEURO = re.compile(r"^ds\d{6}$")
_DOI = re.compile(r"^(?:doi:|https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9}/\S+)$", re.IGNORECASE)


class ShareError(ValueError):
    """The export was refused (an open blocker, an unsafe output folder, or a failed verification); ``checklist`` explains why."""

    def __init__(self, message, checklist=None):
        super().__init__(message)
        self.checklist = list(checklist or [])


def _cancelled(cancel):
    if cancel is not None and cancel():
        from .jobs import Cancelled

        raise Cancelled(reason="stopped")


def _progress(progress, stage, done=None, total=None):
    if progress is not None:
        progress(stage, done, total)


def _now():
    return datetime.datetime.now(datetime.UTC)


def _item(item_id, label, status, detail=""):
    return {"id": item_id, "label": label, "status": status, "detail": detail}


def blockers(checklist):
    """The checklist items that stop an export ("blocker" and "fail")."""
    return [item for item in checklist if item["status"] in ("blocker", "fail")]


def _options(options):
    merged = dict(DEFAULT_OPTIONS)
    merged.update(options or {})
    unknown = sorted(set(merged) - set(DEFAULT_OPTIONS))
    if unknown:
        raise ValueError(f"Unknown share option(s): {', '.join(unknown)}")
    aliases = tuple(merged["aliases"] or ())
    if any(alias not in ALIASES for alias in aliases):
        raise ValueError(f"Unknown alias(es) {aliases}; expected some of {', '.join(ALIASES)}")
    merged["aliases"] = aliases
    if merged["own_patient"] not in (None, "share", "exclude"):
        raise ValueError("own_patient must be None, 'share' or 'exclude'")
    return merged


def _open(study):
    return study if hasattr(study, "subjects") else Study.open(study)


#
# Columns
#


def _sensitivity(study, name):
    """Why a column is sensitive (tracer name, free text, date, ...), or None."""
    if name in SENSITIVE_COLUMNS:
        return SENSITIVE_COLUMNS[name]
    spec = study.column_spec(name)
    if spec.type == "date":
        return "date"
    if spec.type == "text" and name not in CITATION_COLUMNS:
        return "free text"
    return None


def column_choices(study):
    """Columns a share may hold, in table order: [{"name", "description", "role", "sensitive" (reason or None), "default"}].

    Path columns (scan, lesion, ...) are never offered; participant_id is always exported (pseudonymised or not).
    """
    study = _open(study)
    out = []
    for name in study.columns:
        if name == "participant_id":
            continue
        spec = study.column_spec(name)
        if spec.type in NEVER_EXPORTED_TYPES or spec.role == "path":
            continue
        sensitive = _sensitivity(study, name)
        default = sensitive is None and (spec.role in ("group", "score", "covariate", "block") or name in CITATION_COLUMNS
                                         or (spec.role == "metadata" and spec.type in ("numeric", "categorical", "ordinal")))
        out.append({"name": name, "description": spec.description or "", "role": spec.role, "sensitive": sensitive, "default": bool(default)})
    return out


def default_columns(study):
    """The columns exported unless the user chooses others: roles group/score/covariate/block, citation columns and
    categorical or numeric metadata; never free text, tracer names, dates or paths.
    """
    return [choice["name"] for choice in column_choices(study) if choice["default"]]


#
# Rows, lesions and maps
#


def is_own_patient(row):
    """Whether a row's lesion was traced on a scan of the study's own patients: source_type "own_scan", or no source type
    with a scan in the study and no figure tracing (when in doubt the mask counts as own-patient data).
    """
    source = row.get("source_type")
    if source == "own_scan":
        return True
    if not is_missing(source):
        return False
    return not is_missing(row.get("scan")) and "figure" not in str(row.get("trace_method") or "").split("+")


def _rows(study, options):
    rows = study.subjects
    if not options["include_excluded"]:
        rows = [row for row in rows if is_included(row)]
    return rows


def _lesion_space(study, row):
    space = None if is_missing(row.get("space")) else row["space"]
    return space or study.space or "native"


def lesion_plan(study, rows, options):
    """([(sid, source path, space)], [(sid, reason)]) of the lesion masks an export copies and the ones it leaves out."""
    copied, skipped = [], []
    for row in rows:
        sid, lesion = row["participant_id"], row.get("lesion")
        if is_missing(lesion):
            continue
        space = _lesion_space(study, row)
        if str(lesion).startswith("controlset:") or row.get("source_type") == "control_db":
            skipped.append((sid, "control set: governed by its own licence, not redistributed"))
        elif space == "native" or not _LABEL.match(space):
            skipped.append((sid, "native space (only template-space masks are shared)"))
        elif is_own_patient(row) and options["own_patient"] != "share":
            skipped.append((sid, "own patient: needs a sharing decision" if options["own_patient"] is None else "own patient: left out"))
        else:
            path = study.abspath(lesion)
            if not path or not os.path.isfile(path):
                skipped.append((sid, "lesion file missing"))
            elif not path.lower().endswith((".nii", ".nii.gz")):
                skipped.append((sid, "not a NIfTI file"))
            else:
                copied.append((sid, path, space))
    return copied, skipped


def study_group_maps(root):
    """Unthresholded group maps of a study or output folder (derivatives/lnm, cache and hidden folders skipped; no p maps)."""
    root = os.fspath(root)
    top = os.path.join(root, "derivatives", "lnm")
    top = top if os.path.isdir(top) else root
    maps = []
    for folder, folders, names in os.walk(top):
        folders[:] = sorted(name for name in folders if name != "cache" and not name.startswith("."))
        for name in sorted(names):
            match = _GROUP_MAP.search(name)
            if match and not match.group(1).endswith(export.P_MAP_SUFFIXES):
                maps.append(os.path.join(folder, name))
    return maps


def _lesion_maps(folder):
    """Unthresholded per-lesion maps directly in a folder."""
    out = []
    for name in sorted(os.listdir(folder)):
        lesion = export._LESION_MAP.search(name)
        if lesion and not lesion.group(1).startswith("Relocated"):
            out.append(os.path.join(folder, name))
    return out


def _map_connectome(path):
    """Connectome bundle info recorded next to a map (glm.json, slnm.json, provenance.json, cohortmaps.json), or {}."""
    source = export._connectome_source(os.path.dirname(os.path.abspath(path)))
    return source.connectome if isinstance(source.connectome, dict) else {}


def is_gsp_derived(image, connectome=None):
    """Whether a map's metadata (a neurovault image dict) or its connectome is tagged with the GSP data use terms."""
    connectome = connectome or {}
    if (image or {}).get("data_use_terms") == export.GSP_LICENCE or connectome.get("licence") == export.GSP_LICENCE:
        return True
    return "gsp" in str(connectome.get("name") or "").lower()


def terms_unknown(image, connectome=None):
    """Whether a map's data use terms are not recorded (no CC0 default then: the connectome's terms may apply)."""
    if is_gsp_derived(image, connectome):
        return False
    terms = (image or {}).get("data_use_terms")
    return (not terms or terms == export.NOT_RECORDED) and not (connectome or {}).get("licence")


def map_plan(root, maps=None):
    """[{"path", "image" (NeuroVault metadata), "connectome", "gsp", "terms_unknown"}] of the maps an export includes."""
    maps = study_group_maps(root) if maps is None else list(maps)
    images = export.neurovault_metadata(maps, root=root)["images"] if maps else []
    out = []
    for path, image in zip(maps, images, strict=True):
        connectome = _map_connectome(path)
        out.append({"path": path, "image": image, "connectome": connectome, "gsp": is_gsp_derived(image, connectome),
                    "terms_unknown": terms_unknown(image, connectome)})
    return out


def suggested_licence(gsp_derived):
    """The licence pre-selected for a share: CC0 for the user's own masks, none (an explicit choice) with GSP-derived maps
    (or maps whose data use terms are not recorded: pass True for them too).
    """
    return None if gsp_derived else SUGGESTED_OWN_MASKS_LICENCE


#
# Ids
#


def id_concerns(sid):
    """Why an id may identify a person ("date", "name"), or []."""
    label = sid[4:] if sid.startswith("sub-") else sid
    concerns = []
    if _DATE_IN_ID.search(label):
        concerns.append("date")
    words = [word.lower() for word in re.findall(r"[A-Za-z]+", label)]
    if any(len(word) >= 4 and word not in _ID_WORDS for word in words):
        concerns.append("name")
    return concerns


def pseudonyms(sids, rng=None):
    """{original id: "sub-P001"...}: numbered in a random order (the table order may follow dates or names)."""
    sids = list(dict.fromkeys(sids))
    order = list(range(len(sids)))
    rng = rng or secrets.SystemRandom()
    rng.shuffle(order)
    # The pseudonyms must not reuse an original id (studies often already use sub-P001...): the export's leak check
    # would find them, and a reader could not tell a pseudonym from an original id.
    taken = set()
    for sid in sids:
        label = sid[4:] if sid.startswith("sub-") else sid
        taken.update((sid.lower(), label.lower()))
    width = max(3, len(str(len(sids))))
    prefixes = [PSEUDONYM_PREFIX, *(letter for letter in "SQRXYZ" if letter != PSEUDONYM_PREFIX)]
    while True:
        for prefix in prefixes:
            labels = [f"{prefix}{number + 1:0{width}d}" for number in order]
            if not any(label.lower() in taken or f"sub-{label}".lower() in taken for label in labels):
                return {sid: f"sub-{label}" for sid, label in zip(sids, labels, strict=True)}
        width += 1


#
# The plan and its checklist
#


def plan(study, options=None, maps=None, mapped=None):
    """What an export would write, and its checklist (nothing is written).

    :param maps: the group maps to consider (default: :func:`study_group_maps`; used only with ``options["maps"]``).
    :param mapped: a :func:`map_plan` result to reuse instead of reading the maps' metadata again (dialogs).
    :return: {"rows", "columns", "lesions", "skipped", "own_patient", "maps", "gsp", "suggested_licence", "checklist"}.
    """
    study = _open(study)
    options = _options(options)
    rows = _rows(study, options)
    offered = {choice["name"]: choice for choice in column_choices(study)}
    columns = default_columns(study) if options["columns"] is None else [name for name in options["columns"] if name in offered]
    sensitive = [(name, offered[name]["sensitive"]) for name in columns if offered[name]["sensitive"]]
    lesions, skipped = lesion_plan(study, rows, options) if options["lesions"] else ([], [])
    own = [row["participant_id"] for row in rows if not is_missing(row.get("lesion")) and is_own_patient(row)]
    if not (options["maps"] or options["neurovault"]):
        mapped = []
    elif mapped is None:
        mapped = map_plan(study.path, maps)
    gsp = any(entry["gsp"] for entry in mapped)
    unknown_terms = any(entry.get("terms_unknown") for entry in mapped)
    checklist = []

    if options["pseudonymise"]:
        checklist.append(_item("ids", "Participant ids carry no names or dates", "ok", "Ids are replaced by numbered pseudonyms; the mapping stays in the study's .studio folder."))
    else:
        flagged = {sid: id_concerns(sid) for sid in (row["participant_id"] for row in rows)}
        flagged = {sid: concerns for sid, concerns in flagged.items() if concerns}
        detail = ", ".join(f"{sid} ({'/'.join(concerns)})" for sid, concerns in list(flagged.items())[:5])
        checklist.append(_item("ids", "Participant ids carry no names or dates", "warn" if flagged else "ok",
                               f"{len(flagged)} id(s) may hold a name or a date: {detail}. Pseudonymise them." if flagged else "Ids are exported as they are."))
    checklist.append(_item("columns", "No free text, tracer names or dates", "warn" if sensitive else "ok",
                           ("Chosen: " + ", ".join(f"{name} ({reason})" for name, reason in sensitive)) if sensitive else f"{len(columns)} column(s); paths are never exported."))
    checklist.append(_item("headers", "NIfTI descrip and aux_file scrubbed", "ok", "Every NIfTI file is copied without descrip, aux_file, db_name and header extensions."))
    local = [name for name in columns if any(ABSOLUTE_PATH.search(" " + str(row.get(name, ""))) for row in rows)]
    if local:
        checklist.append(_item("paths", "No absolute paths in JSON or TSV", "blocker",
                               f"Column(s) {', '.join(local)} hold a local path: untick them or replace the paths by a portable identifier."))
    else:
        checklist.append(_item("paths", "No absolute paths in JSON or TSV", "ok", "Checked again on the written files."))
    checklist.append(_item("exclusions", "No figures, PDFs, scans, native data or .studio", "ok", "Only the files listed in the plan are written."))
    if options["lesions"]:
        # Own-patient masks that would be shared with a "share" decision (native and control-set masks never are)
        shareable = {sid for sid, _path, _space in lesion_plan(study, rows, {**options, "own_patient": "share"})[0]}
        own_lesions = [sid for sid in own if sid in shareable]
        if own_lesions and options["own_patient"] is None:
            checklist.append(_item("own_patient", "Own-patient masks have a sharing decision", "blocker",
                                   f"{len(own_lesions)} mask(s) come from the study's own patients: choose to share them or leave them out."))
        elif own_lesions:
            checklist.append(_item("own_patient", "Own-patient masks have a sharing decision", "ok",
                                   f"{len(own_lesions)} own-patient mask(s): {'shared' if options['own_patient'] == 'share' else 'left out'} (recorded)."))
        checklist.append(_item("identifiable", "Lesion masks may be identifying", "warn", IDENTIFIABILITY_WARNING))
    licence = options["licence"]
    if not licence:
        checklist.append(_item("licence", "Licence chosen", "blocker",
                               "Choose a licence. " + ("GSP-derived maps are included: there is no default (see the GSP data use terms note)."
                                                        if gsp else "Maps whose data use terms are not recorded are included: there is no default."
                                                        if unknown_terms else "CC0 is suggested for your own lesion masks.")))
    elif licence not in LICENCES:
        checklist.append(_item("licence", "Licence chosen", "blocker", f"Unknown licence {licence!r}."))
    elif licence == "other" and not str(options["licence_text"] or "").strip():
        checklist.append(_item("licence", "Licence chosen", "blocker", "Write the licence text."))
    else:
        checklist.append(_item("licence", "Licence chosen", "ok", LICENCES[licence][0]))
    if gsp:
        checklist.append(_item("gsp_terms", "GSP data use terms read", "ok" if options["terms_acknowledged"] else "blocker", GSP_TERMS_NOTE))
    return {
        "rows": rows,
        "columns": columns,
        "lesions": lesions,
        "skipped": skipped,
        "own_patient": own,
        "maps": mapped if options["maps"] or options["neurovault"] else [],
        "gsp": gsp,
        "suggested_licence": suggested_licence(gsp or unknown_terms),
        "checklist": checklist,
    }


def checklist(study, options=None, maps=None):
    """The checklist of :func:`plan` (a preview; nothing is written)."""
    return plan(study, options, maps=maps)["checklist"]


#
# Files
#


def scrub_nifti(src, dst, compresslevel=6):
    """Copy a NIfTI-1 image without descrip, aux_file, db_name, data_type and header extensions (voxels and geometry unchanged).

    The gzip header carries no file name and no time. Return the destination path.
    """
    from . import nifti

    nifti._check_extension(src)
    nifti._check_extension(dst)
    with nifti._open(src, "rb") as f:
        raw = f.read()
    parsed = nifti._parse_header(raw, src)
    header = np.frombuffer(raw[: nifti.HEADER_SIZE], dtype=nifti._HEADER_DTYPE.newbyteorder(parsed["byteorder"])).copy().reshape(())
    offset = int(parsed["vox_offset"])
    if offset < nifti.HEADER_SIZE + 4:
        raise ValueError(f"Invalid vox_offset {offset} in {src}")
    for field in ("descrip", "aux_file", "db_name", "data_type"):
        header[field] = b""
    header["vox_offset"] = nifti.HEADER_SIZE + 4  # the extensions are dropped (they may hold DICOM fields)
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    nifti._write_image(dst, header, raw[offset:], compresslevel)
    return dst


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    schema.write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    schema.write_text_atomic(path, text)


def _tsv(columns, rows):
    lines = ["\t".join(columns)]
    for row in rows:
        lines.append("\t".join(re.sub(r"[\t\r\n]+", " ", str(row.get(column, NA) if not is_missing(row.get(column)) else NA)).strip() or NA for column in columns))
    return "\n".join(lines) + "\n"


def _doi(text):
    match = _DOI.match(str(text or "").strip())
    return f"doi:{match.group(1)}" if match else None


def _source_datasets(rows, mapped):
    """BIDS SourceDatasets: the connectomes of the exported maps (DOI and version) and the rows' source datasets."""
    out, seen = [], set()
    for entry in mapped:
        connectome = entry["connectome"] or {}
        doi = _doi(connectome.get("doi"))
        key = doi or connectome.get("name")
        if not key or key in seen:
            continue
        seen.add(key)
        item = {"DOI": doi} if doi else {"Name": str(connectome.get("name"))}
        if connectome.get("dataset_version"):
            item["Version"] = str(connectome["dataset_version"])
        out.append(item)
    for row in rows:
        value = str(row.get("source_dataset") or "").strip()
        if is_missing(value) or value in seen:
            continue
        if _OPENNEURO.match(value):
            seen.add(value)
            out.append({"URL": f"https://openneuro.org/datasets/{value}"})
        elif _doi(value):
            seen.add(value)
            out.append({"DOI": _doi(value)})
    return out


def _software(options):
    software = dict(options["software"] or {})
    version = software.get("version") or studio_version()
    generated = {"Name": GENERATOR_NAME, "Version": str(version), "CodeURL": software.get("code_url") or CODE_URL}
    if isinstance(software.get("container"), dict) and software["container"]:
        generated["Container"] = dict(software["container"])
    return generated


def studio_version():
    """The version of Slicer Studio (``StudioLib.version.STUDIO_VERSION``), read without importing Qt; "unknown" when absent."""
    try:
        from StudioLib.version import STUDIO_VERSION

        return STUDIO_VERSION
    except ImportError:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join(here, "..", "StudioLib", "version.py"), os.path.join(here, "..", "..", "Studio", "StudioLib", "version.py")):
        try:
            with open(candidate, encoding="utf-8") as f:
                match = re.search(r'^STUDIO_VERSION\s*=\s*"([^"]+)"', f.read(), re.MULTILINE)
        except OSError:
            continue
        if match:
            return match.group(1)
    return "unknown"


def _licence_text(options, gsp):
    licence = options["licence"]
    title, url = LICENCES[licence]
    if licence == "other":
        text = str(options["licence_text"]).strip() + "\n"
    elif licence == export.GSP_LICENCE:
        text = f"{title}\n\nThe files of this dataset are shared under the same terms as the source data. See {url}\n"
    else:
        text = f"{title}\n\nSPDX-License-Identifier: {licence}\nThe legal code is at {url}\n"
    if gsp:
        text += f"\nNote on derived data\n\n{GSP_TERMS_NOTE}\n\nAcknowledgement\n\n{export.GSP_ACKNOWLEDGEMENT}\n"
    return text


def _readme(name, options, plan_result, written, pseudonymised):
    lines = [f"# {name}", "", "Lesion study exported for sharing by Slicer Studio (BIDS derivatives).", "", f"_{export.DISCLAIMER}_", "", "## Contents", ""]
    lines.append(f"- participants.tsv: {len(plan_result['rows'])} participant(s); columns: {', '.join(['participant_id', *plan_result['columns']])}")
    if pseudonymised:
        lines.append("- Participant ids are pseudonyms in a random order; the key is not part of this dataset.")
    if written["lesions"]:
        lines.append(f"- {written['lesions']} lesion mask(s) in template space: sub-<id>/anat/sub-<id>_space-<space>_desc-lesion_mask.nii.gz (1 = lesion), each with a JSON sidecar.")
        lines.append(f"- {IDENTIFIABILITY_WARNING}")
    if options["aliases"]:
        lines.append(f"- Aliases for other tools ({', '.join(options['aliases'])}) are listed in .bidsignore.")
    if written["maps"]:
        lines.append(f"- {written['maps']} unthresholded group map(s) in group/, each with a JSON sidecar (statistic, df, data use terms).")
    if written["neurovault"]:
        lines.append(f"- neurovault/: the maps with images.json and images.csv for NeuroVault. {NOTHING_UPLOADED}")
    if written["records"]:
        lines.append("- sources/records.tsv: the publications the cases come from (citation columns only).")
    lines += ["", ("Not included: figures and PDFs of publications, scans, native-space data, lesions from control sets "
                   "(governed by their own licence) and application files."), "", "## Licence", "", LICENCES[options["licence"]][0]]
    if plan_result["gsp"]:
        lines += ["", GSP_TERMS_NOTE, "", "## Acknowledgement", "", export.GSP_ACKNOWLEDGEMENT]
    return "\n".join(lines) + "\n"


def _yaml(text):
    return json.dumps(str(text), ensure_ascii=False)


def _citation_cff(name, authors, licence, created):
    lines = ["cff-version: 1.2.0", "message: \"If you use this dataset, please cite it as below.\"", "type: dataset", f"title: {_yaml(name)}", "authors:"]
    for author in authors or ["Not recorded"]:
        lines.append(f"  - name: {_yaml(author)}")
    lines.append(f"date-released: {_yaml(created[:10])}")
    if licence in LICENCES and licence not in ("other", export.GSP_LICENCE):
        lines.append(f"license: {_yaml(licence)}")
    return "\n".join(lines) + "\n"


def _participants_json(study, columns):
    dictionary = {"participant_id": {"Description": "Participant identifier"}}
    for name in columns:
        entry = study.column_spec(name).bids_entry()
        dictionary[name] = entry or {"Description": name}
    return dictionary


def _records(study):
    """(columns, rows) of sources/records.tsv with the citation columns only, or None."""
    path = os.path.join(study.path, "sources", "records.tsv")
    if not os.path.isfile(path):
        return None
    from .study import read_tsv

    columns, rows = read_tsv(path)
    kept = [column for column in RECORD_COLUMNS if column in columns]
    if not kept:
        return None
    clean = []
    for row in rows:
        clean.append({column: (NA if ABSOLUTE_PATH.search(" " + str(row.get(column, ""))) else row.get(column, NA)) for column in kept})
    return kept, clean


def _lesion_sidecar(study, sid, src, space):
    """The exported sidecar of a lesion: Type, Description, Resolution and the space; no source paths, names or times."""
    data = {"Type": "Lesion", "Description": "Lesion mask (1 = lesion)"}
    try:
        original = study.read_lesion_sidecar(sid) or {}
    except (OSError, ValueError):
        original = {}
    resolution = original.get("Resolution") if isinstance(original.get("Resolution"), str) else study._lesion_resolution({"lesion": study.relpath(src)})
    if resolution:
        data["Resolution"] = resolution
    data[schema.FORK_KEY] = {"space": space}
    return data


def _map_sidecar(entry, licence):
    image = dict(entry["image"])
    sidecar = {
        "Description": image.get("description"),
        "Statistic": image.get("map_type"),
        "DegreesOfFreedom": image.get("statistic_parameters"),
        "NumberOfSubjects": image.get("number_of_subjects"),
        "SignCoding": image.get("sign_coding"),
        "Thresholded": False,
        "DataUseTerms": image.get("data_use_terms"),
    }
    if image.get("acknowledgement"):
        sidecar["Acknowledgement"] = image["acknowledgement"]
    sidecar["License"] = licence
    return {key: value for key, value in sidecar.items() if value is not None}


def _rename(name, id_map):
    """A file name with its leading sub-<id> replaced by the pseudonym."""
    for old, new in id_map.items():
        if name.startswith(old + "_") or name == old:
            return new + name[len(old):]
    return name


#
# Validation
#


def validate_name(relpath):
    """Problems of a file name under the BIDS naming rules (a lightweight entity check), [] when it is fine.

    Files in ``sub-<label>/anat/`` must be ``sub-<label>[_<key>-<value>]..._<suffix>.<ext>`` with the folder's label, known
    entities in BIDS order and alphanumeric values.
    """
    parts = relpath.replace(os.sep, "/").split("/")
    name = parts[-1]
    if len(parts) == 1:
        return [] if name in ROOT_FILES else [f"{name}: not a dataset root file"]
    problems = []
    if not parts[0].startswith("sub-"):
        return [f"{relpath}: not a subject folder (list other folders in .bidsignore)"]
    if not _LABEL.match(parts[0][4:]):
        problems.append(f"{parts[0]}: the subject label must be letters and digits")
    if len(parts) != 3 or parts[1] != "anat":
        problems.append(f"{relpath}: subject files belong in sub-<label>/anat/")
    match = re.match(r"^(.*?)\.(nii\.gz|nii|json|tsv)$", name)
    if not match:
        return [*problems, f"{name}: unknown extension"]
    tokens = match.group(1).split("_")
    if len(tokens) < 2 or not _SUFFIX.match(tokens[-1]):
        return [*problems, f"{name}: no suffix"]
    order = []
    for token in tokens[:-1]:
        entity = _ENTITY.match(token)
        if not entity:
            problems.append(f"{name}: {token!r} is not a key-value entity")
            continue
        key = entity.group(1)
        if key not in BIDS_ENTITIES:
            problems.append(f"{name}: unknown entity {key!r}")
            continue
        order.append(BIDS_ENTITIES.index(key))
    if tokens[0] != parts[0]:
        problems.append(f"{name}: must start with {parts[0]}")
    if order != sorted(order) or len(order) != len(set(order)):
        problems.append(f"{name}: entities out of BIDS order or repeated")
    return problems


def _ignored_patterns(root):
    try:
        with open(os.path.join(root, ".bidsignore"), encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip() and not line.startswith("#")]
    except OSError:
        return []


def _is_ignored(relpath, patterns):
    for pattern in patterns:
        if pattern.endswith("/") and (relpath.startswith(pattern) or ("/" + pattern) in ("/" + relpath)):
            return True
        if fnmatch.fnmatch(relpath, pattern) or fnmatch.fnmatch(os.path.basename(relpath), pattern):
            return True
    return False


def validate_dataset(root):
    """{relative path: [problems]} of the files of an exported folder that break the naming rules (.bidsignore honoured)."""
    patterns = _ignored_patterns(root)
    out = {}
    for relpath in _files(root):
        if _is_ignored(relpath, patterns):
            continue
        problems = validate_name(relpath)
        if problems:
            out[relpath] = problems
    return out


def _files(root):
    out = []
    for folder, folders, names in os.walk(root):
        folders.sort()
        for name in sorted(names):
            out.append(os.path.relpath(os.path.join(folder, name), root).replace(os.sep, "/"))
    return out


def find_absolute_paths(root):
    """[(relative file, text)] of the JSON, TSV, CSV, CFF and text files of a folder that hold an absolute local path."""
    found = []
    for relpath in _files(root):
        if not relpath.lower().endswith((".json", ".tsv", ".csv", ".cff", ".md", ".txt", "changes", "license", "readme", ".bidsignore")):
            continue
        try:
            with open(os.path.join(root, relpath), encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            found.append((relpath, "not readable as UTF-8 text"))
            continue
        if relpath.lower().endswith(".json"):
            try:
                text = "\n".join(_json_strings(json.loads(text)))
            except ValueError:
                found.append((relpath, "not valid JSON"))
                continue
        for line in text.splitlines():
            if ABSOLUTE_PATH.search(line):
                found.append((relpath, line.strip()[:120]))
                break
    return found


def _json_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _json_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_strings(item)


def verify_export(root, private_names=()):
    """Check a written export: [checklist items] (status "ok" or "fail").

    Exclusions (no figure, document or scan file, nothing hidden but .bidsignore, nothing from sources/ but records.tsv, no
    native-space NIfTI), scrubbed NIfTI headers, no absolute paths, no private id (``private_names``: the original ids
    when pseudonymised) in any file name or text, and valid JSON sidecars.
    """
    from . import nifti

    files = _files(root)
    items = []
    bad = []
    for relpath in files:
        lower = relpath.lower()
        parts = relpath.split("/")
        if any(part.startswith(".") for part in parts) and relpath != ".bidsignore":
            bad.append(f"{relpath} (hidden)")
        elif lower.endswith(FORBIDDEN_EXTENSIONS):
            bad.append(f"{relpath} (figure, document or scan format)")
        elif parts[0] == "sources" and relpath != "sources/records.tsv":
            bad.append(f"{relpath} (source material)")
        elif _SCAN_SUFFIX.search(relpath) or "space-native" in relpath:
            bad.append(f"{relpath} (scan or native data)")
    items.append(_item("exclusions", "No figures, PDFs, scans, native data or .studio", "fail" if bad else "ok", "; ".join(bad[:5])))
    unscrubbed = []
    for relpath in files:
        if relpath.lower().endswith((".nii", ".nii.gz")):
            try:
                header = nifti.read_nifti_header(os.path.join(root, relpath))
                raw = nifti.read_raw_header(os.path.join(root, relpath))
            except (OSError, ValueError) as exc:
                unscrubbed.append(f"{relpath} ({exc})")
                continue
            if header["descrip"] or header["aux_file"] or bytes(raw["db_name"]).strip(b"\0") or int(header["vox_offset"]) != nifti.HEADER_SIZE + 4:
                unscrubbed.append(relpath)
    items.append(_item("headers", "NIfTI descrip and aux_file scrubbed", "fail" if unscrubbed else "ok", "; ".join(unscrubbed[:5])))
    absolute = find_absolute_paths(root)
    items.append(_item("paths", "No absolute paths in JSON or TSV", "fail" if absolute else "ok", "; ".join(f"{path}: {text}" for path, text in absolute[:5])))
    leaked = []
    names = [name for name in private_names if name]
    if names:
        pattern = re.compile("|".join(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])" for name in sorted(names, key=len, reverse=True)))
        for relpath in files:
            if pattern.search(relpath):
                leaked.append(relpath)
                continue
            if relpath.lower().endswith((".json", ".tsv", ".csv", ".cff", ".md", "changes", "license", ".bidsignore")):
                with open(os.path.join(root, relpath), encoding="utf-8", errors="replace") as f:
                    if pattern.search(f.read()):
                        leaked.append(relpath)
    items.append(_item("pseudonyms", "No original id in the export", "fail" if leaked else "ok", "; ".join(leaked[:5])))
    invalid = []
    for relpath in files:
        if relpath.endswith("_mask.json") or (relpath.startswith("sub-") and relpath.endswith(".json")):
            try:
                with open(os.path.join(root, relpath), encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                invalid.append(relpath)
                continue
            image = relpath[: -len(".json")]
            if not isinstance(data, dict) or data.get("Type") != "Lesion" or not any(os.path.isfile(os.path.join(root, image + ext)) for ext in (".nii.gz", ".nii")):
                invalid.append(relpath)
    items.append(_item("sidecars", "Lesion sidecars are valid", "fail" if invalid else "ok", "; ".join(invalid[:5])))
    return items


#
# Export
#


def _check_output(study, out, zip_output):
    out = os.path.abspath(os.fspath(out))
    study_root = os.path.abspath(study.path)
    if out == study_root or out.startswith(study_root + os.sep):
        # Not inside the study either: a later export or map search could pick up the copy (.studio is never a place for it)
        raise ShareError("Choose an output outside the study folder.")
    if study_root.startswith(out + os.sep):
        raise ShareError("The output cannot be a folder that contains the study.")
    if zip_output:
        if not out.lower().endswith(".zip"):
            raise ShareError("A zip export needs a file name ending in .zip.")
        if os.path.exists(out):
            raise ShareError(f"{os.path.basename(out)} already exists; choose another name.")
    elif os.path.exists(out) and (not os.path.isdir(out) or os.listdir(out)):
        raise ShareError(f"{os.path.basename(out)} already exists and is not an empty folder; choose another one.")
    return out


def _record(study, record):
    """Write the private record of an export in <study>/.studio/share/ (never exported). Return its path."""
    folder = os.path.join(study.path, PRIVATE_DIR, SHARE_DIR)
    os.makedirs(folder, exist_ok=True)
    stamp = _now().strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(folder, f"{stamp}-{secrets.token_hex(3)}.json")
    _write_json(path, record)
    return path


def export_dataset(study, out, options=None, progress=None, cancel=None, maps=None):
    """Write a share-safe BIDS-derivatives copy of a study to the folder ``out`` (or the zip archive ``out`` with ``zip``).

    :param study: a ``study.Study`` or a study folder.
    :param options: see :data:`DEFAULT_OPTIONS`; ``licence`` is required (an explicit choice).
    :param progress: optional ``progress(stage, done, total)``; ``cancel``: optional callable, True stops (``jobs.Cancelled``).
    :param maps: group maps to consider instead of :func:`study_group_maps` (with ``options["maps"]``).
    :return: {"path", "files", "lesions", "maps", "skipped", "checklist", "record", "validation"}; the pseudonym
      mapping is only in the private record (``record``, in the study's .studio folder).
    :raises ShareError: while a checklist blocker is open, for an unsafe output, or when the written files fail
      :func:`verify_export` (nothing is left at ``out`` then).
    """
    study = _open(study)
    options = _options(options)
    out = _check_output(study, out, options["zip"])
    result_plan = plan(study, options, maps=maps)
    open_items = blockers(result_plan["checklist"])
    if open_items:
        raise ShareError("; ".join(f"{item['label']}: {item['detail']}" for item in open_items), result_plan["checklist"])
    rows = result_plan["rows"]
    id_map = pseudonyms([row["participant_id"] for row in rows]) if options["pseudonymise"] else {row["participant_id"]: row["participant_id"] for row in rows}
    created = _now().isoformat(timespec="seconds")
    name = str(options["name"] or study.name).strip() or "Lesion study"
    staging = (out[:-4] if options["zip"] else out.rstrip(os.sep)) + f".partial-{os.getpid()}-{secrets.token_hex(3)}"
    written = {"lesions": 0, "maps": 0, "neurovault": 0, "records": False}
    try:
        os.makedirs(staging)
        _progress(progress, "participants")
        columns = result_plan["columns"]
        table = []
        for row in rows:
            cells = {column: row.get(column, NA) for column in columns}
            if "duplicate_of" in cells and not is_missing(cells["duplicate_of"]):
                cells["duplicate_of"] = id_map.get(cells["duplicate_of"], NA)
            cells["participant_id"] = id_map[row["participant_id"]]
            table.append(cells)
        table.sort(key=lambda cells: cells["participant_id"])  # pseudonyms in number order, not in the study's order
        _write_text(os.path.join(staging, "participants.tsv"), _tsv(["participant_id", *columns], table))
        _write_json(os.path.join(staging, "participants.json"), _participants_json(study, columns))
        ignore = []
        lesions = result_plan["lesions"]
        for index, (sid, src, space) in enumerate(lesions):
            _cancelled(cancel)
            _progress(progress, "lesions", index, len(lesions))
            new = id_map[sid]
            anat = os.path.join(staging, new, "anat")
            stem = f"{new}_space-{space}_desc-lesion_mask"
            ext = ".nii.gz"
            scrub_nifti(src, os.path.join(anat, stem + ext))
            _write_json(os.path.join(anat, stem + ".json"), _lesion_sidecar(study, sid, src, space))
            if "fmriprep" in options["aliases"]:
                shutil.copyfile(os.path.join(anat, stem + ext), os.path.join(anat, f"{new}_label-lesion_roi{ext}"))
            if "lacuna" in options["aliases"]:
                shutil.copyfile(os.path.join(anat, stem + ext), os.path.join(anat, f"{new}_label-lesion_space-{space}_mask{ext}"))
            written["lesions"] += 1
        if "fmriprep" in options["aliases"] and lesions:
            ignore.append("*_label-lesion_roi.nii.gz")
        if "lacuna" in options["aliases"] and lesions:
            ignore.append("*_label-lesion_space-*_mask.nii.gz")
        mapped = result_plan["maps"] if options["maps"] else []
        licence = options["licence"]
        used = set()
        for index, entry in enumerate(mapped):
            _cancelled(cancel)
            _progress(progress, "maps", index, len(mapped))
            base = os.path.basename(entry["path"])
            target = base if base not in used else f"{os.path.basename(os.path.dirname(entry['path']))}_{base}"
            used.add(target)
            scrub_nifti(entry["path"], os.path.join(staging, "group", target))
            _write_json(os.path.join(staging, "group", re.sub(r"\.nii(\.gz)?$", ".json", target)), _map_sidecar(entry, licence))
            written["maps"] += 1
        if mapped:
            ignore.append("group/")
        if options["neurovault"] and result_plan["maps"]:
            _cancelled(cancel)
            _progress(progress, "neurovault")
            folder = neurovault_folder(study.path, os.path.join(staging, "neurovault"), maps=[entry["path"] for entry in result_plan["maps"]], id_map=id_map,
                                       licence=licence)
            written["neurovault"] = len(folder["images"])
            ignore.append("neurovault/")
        records = _records(study)
        if records is not None:
            _write_text(os.path.join(staging, "sources", "records.tsv"), _tsv(*records))
            written["records"] = True
            ignore.append("sources/")
        description = {
            "Name": name,
            "BIDSVersion": BIDS_VERSION,
            "DatasetType": "derivative",
            "License": licence if licence != "other" else "See LICENSE",
            "GeneratedBy": [_software(options)],
        }
        authors = [str(author).strip() for author in options["authors"] or () if str(author).strip()]
        if authors:
            description["Authors"] = authors
        sources = _source_datasets(rows, mapped or (result_plan["maps"] if options["neurovault"] else []))
        if sources:
            description["SourceDatasets"] = sources
        if result_plan["gsp"]:
            description["HowToAcknowledge"] = export.GSP_ACKNOWLEDGEMENT
        _write_json(os.path.join(staging, "dataset_description.json"), description)
        _write_text(os.path.join(staging, "README.md"), _readme(name, options, result_plan, written, options["pseudonymise"]))
        _write_text(os.path.join(staging, "CITATION.cff"), _citation_cff(name, authors, licence, created))
        _write_text(os.path.join(staging, "CHANGES"), f"1.0.0 {created[:10]}\n  - Exported from a Slicer Studio lesion study.\n")
        _write_text(os.path.join(staging, "LICENSE"), _licence_text(options, result_plan["gsp"]))
        if ignore:
            _write_text(os.path.join(staging, ".bidsignore"), "\n".join(dict.fromkeys(ignore)) + "\n")
        _progress(progress, "verify")
        private = [sid for sid, new in id_map.items() if sid != new]
        verification = verify_export(staging, private_names=private)
        failed = blockers(verification)
        if failed:
            raise ShareError("; ".join(f"{item['label']}: {item['detail']}" for item in failed), verification)
        validation = validate_dataset(staging)
        files = _files(staging)
        if options["zip"]:
            _zip(staging, out)
            shutil.rmtree(staging)
        else:
            if os.path.isdir(out):
                os.rmdir(out)  # an empty folder chosen in a dialog
            os.replace(staging, out)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    record = {
        "format": SHARE_FORMAT,
        "version": SHARE_VERSION,
        "created": created,
        "output": out,  # private: this file is never exported
        "pseudonymised": bool(options["pseudonymise"]),
        "pseudonyms": {new: sid for sid, new in id_map.items()} if options["pseudonymise"] else {},
        "own_patient_decision": options["own_patient"] if result_plan["own_patient"] and options["lesions"] else None,
        "own_patient_ids": result_plan["own_patient"] if options["lesions"] else [],
        "options": {key: (list(value) if isinstance(value, tuple) else value) for key, value in options.items() if key not in ("software",)},
        "files": len(files),
        "lesions": written["lesions"],
        "maps": written["maps"],
    }
    record_path = _record(study, record)
    checklist_items = [item for item in result_plan["checklist"] if item["id"] not in {entry["id"] for entry in verification}] + verification
    return {
        "path": out,
        "files": files,
        "lesions": written["lesions"],
        "maps": written["maps"],
        "neurovault": written["neurovault"],
        "skipped": result_plan["skipped"],
        "checklist": checklist_items,
        "record": record_path,
        "validation": validation,
    }


def _zip(folder, out):
    tmp = f"{out}.{os.getpid()}.tmp"
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for relpath in _files(folder):
                info = zipfile.ZipInfo(relpath, date_time=(1980, 1, 1, 0, 0, 0))  # no local times in the archive
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                with open(os.path.join(folder, relpath), "rb") as f:
                    archive.writestr(info, f.read())
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


#
# NeuroVault-ready folder
#

NEUROVAULT_CSV_COLUMNS = ("file", "name", "map_type", "modality", "analysis_level", "is_thresholded", "number_of_subjects", "statistic_parameters",
                          "sign_coding", "description", "target_template_image", "data_use_terms", "acknowledgement")


def neurovault_folder(study_or_run, out, maps=None, connectome=None, include_lesion_maps=None, id_map=None, licence=None, progress=None, cancel=None):
    """Write a NeuroVault-ready folder: the unthresholded maps (headers scrubbed), images.json and images.csv. Never uploads.

    :param study_or_run: a study (or its folder), or a run folder holding maps (glm, slnm, cohortmaps or a single map run).
    :param maps: the maps to include (default: the study's group maps, or the run folder's group maps; per-lesion maps
      when ``include_lesion_maps`` is True, or when the folder has no group map and it is not False).
    :param connectome: connectome of maps whose folder does not record it (see ``export.neurovault_metadata``).
    :param id_map: {original id: pseudonym}: per-lesion map names starting with an original id are renamed.
    :param licence: the licence chosen for the share (written in README.md; the per-image data use terms stay).
    :return: {"path", "images", "gsp"}.
    """
    source = study_or_run.path if hasattr(study_or_run, "path") else os.path.abspath(os.fspath(study_or_run))
    out = os.path.abspath(os.fspath(out))
    if os.path.exists(out) and (not os.path.isdir(out) or os.listdir(out)):
        raise ShareError(f"{os.path.basename(out)} already exists and is not an empty folder; choose another one.")
    is_study = Study.is_study(source)
    root = source if is_study else None
    if maps is None:
        maps = study_group_maps(source) if is_study else [path for path in study_group_maps(source) if os.path.dirname(path) == source]
        if include_lesion_maps or (include_lesion_maps is None and not maps and not is_study):
            maps += _lesion_maps(source)
    maps = list(maps)
    id_map = dict(id_map or {})
    metadata = export.neurovault_metadata(maps, connectome=connectome, root=root if connectome is None else None)
    images, used, gsp = [], set(), False
    os.makedirs(out, exist_ok=True)
    for index, (path, image) in enumerate(zip(maps, metadata["images"], strict=True)):
        _cancelled(cancel)
        _progress(progress, "neurovault", index, len(maps))
        base = _rename(os.path.basename(path), id_map)
        target = base if base not in used else f"{os.path.basename(os.path.dirname(path))}_{base}"
        used.add(target)
        scrub_nifti(path, os.path.join(out, target))
        image = dict(image)
        image["file"] = target
        image["name"] = re.sub(r"\.nii(\.gz)?$", "", target)
        images.append(image)
        gsp = gsp or is_gsp_derived(image, _map_connectome(path))
    content = {"format": export.NEUROVAULT_FORMAT, "version": 1, "images": images, "disclaimer": export.DISCLAIMER}
    _write_json(os.path.join(out, "images.json"), export._clean(content))
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(NEUROVAULT_CSV_COLUMNS)
    for image in images:
        writer.writerow(["" if image.get(column) is None else str(image.get(column)).lower() if isinstance(image.get(column), bool) else image.get(column)
                         for column in NEUROVAULT_CSV_COLUMNS])
    _write_text(os.path.join(out, "images.csv"), buffer.getvalue())
    lines = ["# NeuroVault-ready maps", "", NOTHING_UPLOADED, "", (f"{len(images)} unthresholded map(s). images.json and images.csv hold the metadata of "
                                                                   "each map: map type, degrees of freedom (statistic_parameters), number of subjects, "
                                                                   "sign coding and data use terms."), "", f"_{export.DISCLAIMER}_"]
    if licence:
        lines += ["", f"Licence chosen for the share: {LICENCES.get(licence, (licence,))[0]}"]
    if gsp:
        lines += ["", GSP_TERMS_NOTE, "", "Acknowledgement: " + export.GSP_ACKNOWLEDGEMENT]
    _write_text(os.path.join(out, "README.md"), "\n".join(lines) + "\n")
    return {"path": out, "images": images, "gsp": gsp}

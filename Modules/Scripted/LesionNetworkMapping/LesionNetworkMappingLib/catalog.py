"""The connectome catalog: normative connectomes that can be obtained, with their terms, sizes and disk plans.

``catalog/connectomes.json`` (format ``slicer-lnm-catalog`` version 1, catalog "connectomes") ships with the
app and is refreshed only when the user asks (addendum 4.2). It lists where each connectome comes from; it
downloads nothing. Connectomes are never bundled and never downloaded automatically: a download happens only
after an explicit click and after the user accepted the provider's live terms text (addendum 4.1, decision
D-L1), and sources whose terms are not stated are links only (:func:`link_out`).

Entry fields (snake_case, like the atlas and template catalogs of ``fetch``):

- ``id``, ``title``, ``kind`` (timeseries | group_pca | tractogram) and ``status``: "supported" (a converter
  turns it into a bundle), "unsupported" (listed, not usable in this version; ``status_note`` says why),
  "link_out" (the app only links to the source) or "local" (made on this computer, e.g. the synthetic demo);
- ``source``: ``type`` (dataverse | figshare | zenodo | sciebo | s3 | openneuro | http | lead-dbs | local),
  ``doi`` (without "doi:"), ``version``, ``url``, ``api`` (the dataset API that lists the files and terms);
- ``files``: ``name``, ``size`` (bytes), ``role`` (archive | metadata | participants | docs | data),
  ``checksum`` ``{"algo": "md5" | "sha1" | "sha256", "value": hex}`` as the source publishes it (Dataverse and
  figshare MD5, sciebo ``OC-Checksum`` SHA1; a multipart S3 ETag is never an MD5) or "record-on-first-download",
  and for Dataverse files their ``id`` and ``folder``; ``size`` is the sum of the file sizes;
- ``licence`` (an SPDX id, ``GSP-DUT-2014`` or ``unknown``) and ``badge`` (:func:`licence_badge`); ``terms``:
  ``title``, ``url``, ``fetch`` ("live": the text is fetched from ``api`` at ``field`` when the user asks, and
  accepted per :func:`terms_sha256`; "none": the licence is stated on ``url``);
- ``citation``, ``acknowledgement`` (wording the terms require in publications), ``upstream`` (notes such as
  "derived from PPMI; their terms may apply");
- ``space``, ``family``, ``grid`` (a ``geometry.GRID_REGISTRY`` id) and ``resolution_mm`` (null when unknown,
  with ``space_note``);
- ``population`` (``n``, ``age_range`` [min, max] years, ``sex_ratio``, ``scanners``), ``tr_s``,
  ``preprocessing`` (``gsr``, ``smoothing_mm``, ``bandpass_hz``, ``nuisance``, ``pipeline_url``);
- ``converter``: the ``connectome.from_tar_stream`` layout ("gsp_ilxiks", "leaddbs_vol"), "synthetic", or null;
- ``subsets``: {name: number of subjects} of the source's named subject subsets (Lead-DBS datasets only:
  an ILXIKS archive subset is a different subject list);
- ``disk``: what :func:`disk_plan` computes from: ``subjects``, ``voxels``, ``timepoints_per_run``,
  ``runs_per_subject`` [min, max] and ``stored_bytes`` [low, high] (an estimate of the converted bundle,
  float16), any of them null when unknown;
- ``link_out`` and ``link_url`` for link-out entries.
"""

import hashlib
import json
import math
import os
import re

from . import fetch
from .geometry import GRID_BY_ID, space_family

CATALOG_FORMAT = fetch.CATALOG_FORMAT
CATALOG_VERSION = fetch.CATALOG_VERSION
CATALOG_DIR = fetch.CATALOG_DIR
CONNECTOMES = "connectomes"
KINDS = ("timeseries", "group_pca", "tractogram")
STATUSES = ("supported", "unsupported", "link_out", "local")
SOURCE_TYPES = ("dataverse", "figshare", "zenodo", "sciebo", "s3", "openneuro", "http", "lead-dbs", "local")
FILE_ROLES = ("archive", "metadata", "participants", "docs", "data")
CHECKSUM_LENGTHS = {"md5": 32, "sha1": 40, "sha256": 64}
RECORD_ON_FIRST_DOWNLOAD = fetch.RECORD_ON_FIRST_DOWNLOAD
#: Converters of connectome.from_tar_stream (archives streamed into a bundle) and the synthetic demo
STREAM_CONVERTERS = ("gsp_ilxiks", "leaddbs_vol")
CONVERTERS = (*STREAM_CONVERTERS, "synthetic")
TERMS_FETCH = ("live", "none")
#: Bytes per sample of a converted bundle (float16) and per voxel and run of its norms (float32)
STORED_SAMPLE_BYTES = 2
STORED_NORM_BYTES = 4
#: Bytes per sample of a run kept in memory or staged between two archives (float32)
STAGED_SAMPLE_BYTES = 4
_DOI_PATTERN = re.compile(r"^10\.\d{4,9}/\S+$")
_ARCHIVE_SUBSET_PATTERN = re.compile(r"^archives:\s*(.+)$", re.IGNORECASE)
_SUBJECT_SUBSET_PATTERN = re.compile(r"^subjects:\s*(\d+)$", re.IGNORECASE)
_ALL_SUBSETS = ("", "all", "full", "full set")


class CatalogError(fetch.CatalogError):
    """The connectome catalog is not a valid slicer-lnm-catalog v1 file; ``problems`` lists what is wrong."""

    def __init__(self, message, problems=()):
        super().__init__(message)
        self.problems = list(problems)


# Licence, terms and link-out


def licence_badge(entry):
    """Licence badge of an entry (addendum 4.2): "green" (CC0, CC BY, MIT, Apache, BSD), "amber" (data use
    terms such as GSP-DUT-2014, registration, non-commercial or share-alike) or "grey" (terms not stated:
    never accepted automatically, see :func:`link_out`).
    """
    return fetch.badge_for(entry.get("licence") if isinstance(entry, dict) else entry)


def link_out(entry):
    """True when the app only links to the source: the entry says so, or its terms are not stated (grey badge)."""
    return bool(entry.get("link_out")) or licence_badge(entry) == "grey"


def normalize_terms_text(text):
    """The terms text with its line endings normalized to LF (CRLF and CR from different servers or copies)."""
    if not isinstance(text, str):
        raise TypeError(f"terms text must be text, got {type(text).__name__}")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def terms_sha256(text):
    """Hex sha256 of a terms text (UTF-8 with normalized newlines): the key under which an acceptance is stored."""
    return hashlib.sha256(normalize_terms_text(text).encode("utf-8")).hexdigest()


# Catalog


class ConnectomeCatalog:
    """A loaded, validated connectome catalog: ``entries`` (dicts in file order), ``get(id)``, ``ids()``."""

    def __init__(self, data, path=None):
        problems = validate_catalog(data)
        if problems:
            raise CatalogError(f"Invalid connectome catalog {path or ''}: {'; '.join(problems[:10])}{' ...' if len(problems) > 10 else ''}", problems)
        self.path = path
        self.data = data
        self.name = data.get("catalog", CONNECTOMES)
        self.entries = list(data["entries"])
        self._by_id = {entry["id"]: entry for entry in self.entries}

    def get(self, entry_id):
        try:
            return self._by_id[entry_id]
        except KeyError:
            raise fetch.UnknownResourceError(f"No entry {entry_id!r} in the {self.name} catalog", resource_id=entry_id) from None

    def __contains__(self, entry_id):
        return entry_id in self._by_id

    def __iter__(self):
        return iter(self.entries)

    def __len__(self):
        return len(self.entries)

    def ids(self):
        return list(self._by_id)


_catalog_cache = {}


def catalog_path(name=CONNECTOMES):
    """The file of a shipped catalog name ("connectomes"), or ``name`` itself when it is a path."""
    text = os.fspath(name)
    if os.sep in text or "/" in text or text.endswith(".json"):
        return text
    return os.path.join(CATALOG_DIR, f"{text}.json")


def load_catalog(name=CONNECTOMES):
    """Load and validate the connectome catalog (a shipped name or a file path; cached per file and mtime).

    Raises :class:`CatalogError` (with every problem in ``problems``) when the file is not a valid
    slicer-lnm-catalog v1 connectome catalog.
    """
    path = catalog_path(name)
    try:
        stamp = (os.path.abspath(path), os.stat(path).st_mtime_ns)
    except OSError as exc:
        raise CatalogError(f"Cannot read the catalog {os.fspath(name)!r}: {exc}") from None
    if stamp not in _catalog_cache:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            raise CatalogError(f"Cannot read the catalog {os.fspath(name)!r}: {exc}") from None
        _catalog_cache[stamp] = ConnectomeCatalog(data, path)
    return _catalog_cache[stamp]


def validate_catalog(data):
    """Problems (list of strings) of a connectome catalog; an empty list when it is valid."""
    if not isinstance(data, dict) or data.get("format") != CATALOG_FORMAT or data.get("version") != CATALOG_VERSION:
        return [f"not a {CATALOG_FORMAT} version {CATALOG_VERSION} file"]
    problems = []
    if data.get("catalog") != CONNECTOMES:
        problems.append(f"catalog must be {CONNECTOMES!r}, got {data.get('catalog')!r}")
    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        return [*problems, "no entries list"]
    seen = set()
    for index, entry in enumerate(entries):
        label = entry.get("id") if isinstance(entry, dict) and entry.get("id") else f"entry {index}"
        if isinstance(entry, dict) and entry.get("id") in seen:
            problems.append(f"{label}: duplicate id")
        seen.add(entry.get("id") if isinstance(entry, dict) else None)
        problems += [f"{label}: {problem}" for problem in validate_entry(entry)]
    return problems


def _is_count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _validate_files(entry, problems):
    files = entry.get("files")
    if not isinstance(files, list):
        problems.append("files must be a list")
        return
    names = set()
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
            problems.append("a file has no name")
            continue
        name = item["name"]
        if name in names:
            problems.append(f"file {name} is listed twice")
        names.add(name)
        if item.get("size") is not None and not _is_count(item["size"]):
            problems.append(f"file {name}: size must be a number of bytes")
        if item.get("role") not in FILE_ROLES:
            problems.append(f"file {name}: unknown role {item.get('role')!r}")
        checksum = item.get("checksum")
        if isinstance(checksum, dict):
            algo = str(checksum.get("algo", "")).lower()
            value = str(checksum.get("value", "")).lower()
            if algo not in CHECKSUM_LENGTHS:
                problems.append(f"file {name}: unsupported checksum algorithm {checksum.get('algo')!r}")
            elif not re.fullmatch(f"[0-9a-f]{{{CHECKSUM_LENGTHS[algo]}}}", value):
                problems.append(f"file {name}: the {algo} checksum is not {CHECKSUM_LENGTHS[algo]} hexadecimal digits (a multipart S3 ETag is not an MD5)")
        elif checksum is None:
            if not link_out(entry):
                problems.append(f"file {name}: a downloadable file needs a checksum or {RECORD_ON_FIRST_DOWNLOAD!r}")
        elif checksum != RECORD_ON_FIRST_DOWNLOAD:
            problems.append(f"file {name}: checksum must be an object or {RECORD_ON_FIRST_DOWNLOAD!r}")
    sizes = [item.get("size") for item in files if isinstance(item, dict)]
    if files and all(_is_count(size) for size in sizes) and entry.get("size") != sum(sizes):
        problems.append(f"size {entry.get('size')} is not the sum of the file sizes ({sum(sizes)})")
    if entry.get("size") is not None and not _is_count(entry["size"]):
        problems.append("size must be a number of bytes or null")


def _validate_disk(entry, problems):
    disk = entry.get("disk")
    if not isinstance(disk, dict):
        problems.append("disk must be an object")
        return
    for key in ("subjects", "voxels", "timepoints_per_run"):
        if disk.get(key) is not None and not (_is_count(disk[key]) and disk[key] > 0):
            problems.append(f"disk.{key} must be a positive number or null")
    for key in ("runs_per_subject", "stored_bytes"):
        value = disk.get(key)
        if value is not None and not (isinstance(value, list) and len(value) == 2 and all(_is_count(v) for v in value) and value[0] <= value[1]):
            problems.append(f"disk.{key} must be [low, high] or null")


def validate_entry(entry):
    """Problems (list of strings) of one connectome catalog entry; an empty list when it is complete."""
    if not isinstance(entry, dict):
        return ["an entry must be an object"]
    problems = []
    for key in ("id", "title", "kind", "status", "licence", "badge", "citation"):
        if not entry.get(key):
            problems.append(f"missing {key}")
    for key in ("source", "files", "disk", "converter", "space"):
        if key not in entry:
            problems.append(f"missing {key}")
    if entry.get("kind") not in KINDS:
        problems.append(f"unknown kind {entry.get('kind')!r}")
    status = entry.get("status")
    if status not in STATUSES:
        problems.append(f"unknown status {status!r}")
    if entry.get("kind") != "timeseries" and status in ("supported", "local"):
        problems.append("only time series connectomes are supported (the engine computes maps from subjects' time series)")
    if status == "unsupported" and not entry.get("status_note"):
        problems.append("an unsupported entry needs a status_note")
    badge = entry.get("badge")
    if badge not in fetch.BADGES:
        problems.append(f"unknown badge {badge!r}")
    elif badge != licence_badge(entry):
        problems.append(f"badge {badge} does not match licence {entry.get('licence')}")
    if link_out(entry):
        if status != "link_out" or not entry.get("link_out"):
            problems.append("an entry whose terms are not stated must be link-out only (status and link_out)")
        if not str(entry.get("link_url", "")).startswith("https://"):
            problems.append("a link-out entry needs an https link_url")
    elif status == "link_out":
        problems.append("status link_out needs link_out: true")
    source = entry.get("source")
    if not isinstance(source, dict) or source.get("type") not in SOURCE_TYPES:
        problems.append(f"source.type must be one of {', '.join(SOURCE_TYPES)}")
    else:
        if source.get("doi") is not None and not _DOI_PATTERN.match(str(source["doi"])):
            problems.append(f"source.doi {source['doi']!r} is not a DOI (10.xxxx/..., without 'doi:')")
        for key in ("url", "api"):
            if source.get(key) is not None and not str(source[key]).startswith("https://"):
                problems.append(f"source.{key} must be an https URL")
        if source["type"] != "local" and not source.get("url"):
            problems.append("source.url is missing")
    converter = entry.get("converter")
    if converter is not None and converter not in CONVERTERS:
        problems.append(f"unknown converter {converter!r}")
    if status == "supported" and converter not in CONVERTERS:
        problems.append("a supported entry needs a converter")
    if converter in STREAM_CONVERTERS and not any(isinstance(f, dict) and f.get("role") == "archive" for f in entry.get("files") or []):
        problems.append("a streamed converter needs archive files")
    terms = entry.get("terms")
    if status != "local" and not link_out(entry):
        if not isinstance(terms, dict) or terms.get("fetch") not in TERMS_FETCH or not str(terms.get("url", "")).startswith("https://"):
            problems.append("terms needs fetch (live | none) and an https url")
        elif terms["fetch"] == "live" and not (str(terms.get("api", "")).startswith("https://") and terms.get("field")):
            problems.append("live terms need the api URL and the field that holds the text")
    _validate_files(entry, problems)
    _validate_disk(entry, problems)
    grid = entry.get("grid")
    if grid is not None:
        if grid not in GRID_BY_ID:
            problems.append(f"unknown grid {grid!r}")
        elif entry.get("family") != GRID_BY_ID[grid].family:
            problems.append(f"family {entry.get('family')!r} is not the family of grid {grid}")
    if entry.get("space") is not None and entry.get("family") is not None and space_family(entry["space"]) != entry["family"]:
        problems.append(f"family {entry['family']!r} does not match space {entry['space']!r}")
    if entry.get("space") is None and not entry.get("space_note"):
        problems.append("an unknown space needs a space_note")
    population = entry.get("population") or {}
    if population.get("n") is not None and not _is_count(population["n"]):
        problems.append("population.n must be a number of subjects or null")
    ages = population.get("age_range")
    if ages is not None and not (isinstance(ages, list) and len(ages) == 2 and all(_is_number(a) for a in ages) and ages[0] <= ages[1]):
        problems.append("population.age_range must be [min, max] or null")
    if entry.get("tr_s") is not None and not (_is_number(entry["tr_s"]) and entry["tr_s"] > 0):
        problems.append("tr_s must be a positive number of seconds or null")
    gsr = (entry.get("preprocessing") or {}).get("gsr", "unknown")
    if gsr not in (True, False, "unknown"):
        problems.append("preprocessing.gsr must be true, false or \"unknown\"")
    subsets = entry.get("subsets")
    if subsets is not None:
        if converter != "leaddbs_vol":
            problems.append("named subsets exist only for Lead-DBS datasets (an archive subset is a different subject list)")
        n = population.get("n")
        if not isinstance(subsets, dict) or not all(isinstance(k, str) and k and _is_count(v) and v > 0 and (n is None or v <= n) for k, v in subsets.items()):
            problems.append("subsets must be {name: number of subjects}")
    return problems


# Disk plans


def archives(entry):
    """The archive files of an entry (role "archive"), in name order."""
    return sorted((f for f in entry.get("files") or [] if f.get("role") == "archive"), key=lambda f: f["name"])


def _parse_indices(text, count):
    indices = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        bounds = part.split("-")
        if len(bounds) > 2 or not all(b.strip().isdigit() for b in bounds):
            raise ValueError(f"Bad archive subset {text!r} (use for example archives:00-01 or archives:0,3,5)")
        low, high = int(bounds[0]), int(bounds[-1])
        if low > high or high >= count:
            raise ValueError(f"Archive subset {text!r} is out of range (the entry has {count} archives, 00-{count - 1:02d})")
        indices.extend(range(low, high + 1))
    if not indices:
        raise ValueError(f"Empty archive subset {text!r}")
    return sorted(set(indices))


def resolve_subset(entry, subset=None):
    """Which archives and how many subjects a subset of an entry needs.

    :param subset: None, "all" or "Full Set" (everything); "archives:00-01" or "archives:0,3,5" (archives by
      index in name order); an int or "subjects:N" (the first N subjects); or a named subset of the entry
      (``subsets``, Lead-DBS datasets only).
    :return: dict with ``archives`` (the archive file dicts), ``subjects`` (number, None when unknown),
      ``fraction`` (of the subjects, estimated from archive sizes for an archive subset) and ``exact``
      (whether the archives are known: False for a subject subset, whose archives are estimated assuming
      that subjects are spread evenly in archive order).
    """
    files = archives(entry)
    total_subjects = (entry.get("disk") or {}).get("subjects") or (entry.get("population") or {}).get("n")
    named = entry.get("subsets") or {}
    text = None if subset is None or isinstance(subset, int) else str(subset).strip()
    count = subset if isinstance(subset, int) and not isinstance(subset, bool) else None
    if text is not None and text in named:
        count = named[text]
    elif text is not None and _SUBJECT_SUBSET_PATTERN.match(text):
        count = int(_SUBJECT_SUBSET_PATTERN.match(text).group(1))
    elif text is not None and _ARCHIVE_SUBSET_PATTERN.match(text):
        if not files:
            raise ValueError(f"{entry.get('id')} has no archives")
        chosen = [files[i] for i in _parse_indices(_ARCHIVE_SUBSET_PATTERN.match(text).group(1), len(files))]
        archive_bytes = sum(f.get("size") or 0 for f in files)
        fraction = sum(f.get("size") or 0 for f in chosen) / archive_bytes if archive_bytes else len(chosen) / len(files)
        return {"archives": chosen, "subjects": None if total_subjects is None else round(total_subjects * fraction), "fraction": fraction, "exact": True}
    elif text is not None and text.lower() not in _ALL_SUBSETS:
        raise ValueError(f"Unknown subset {subset!r} of {entry.get('id')} (use all, archives:00-01, subjects:N{', or ' + ', '.join(repr(k) for k in named) if named else ''})")
    if count is None or (total_subjects is not None and count >= total_subjects):
        return {"archives": files, "subjects": total_subjects, "fraction": 1.0, "exact": True}
    if count < 1:
        raise ValueError("A subject subset needs at least 1 subject")
    if total_subjects is None:
        raise ValueError(f"{entry.get('id')} does not state its number of subjects, so a subject subset cannot be planned")
    fraction = count / total_subjects
    needed = max(1, math.ceil(fraction * len(files))) if files else 0
    return {"archives": files[:needed], "subjects": count, "fraction": fraction, "exact": False}


def disk_plan(entry, subset=None, keep_archives=False):
    """Bytes to download, to store and at the peak for an entry (or a subset of it, see :func:`resolve_subset`).

    - ``download``: the selected archives plus every other file of the entry (metadata, participants, docs);
    - ``stored``: the converted bundle (float16), the high end of ``stored_range`` ([low, high], scaled by the
      subset's fraction of the subjects); None when the catalog cannot estimate it;
    - ``peak``: the most space in use at once. Streamed converters keep no archive (unless ``keep_archives``):
      the bundle, the small files and the runs staged between two archives. Other entries keep the download
      next to what is converted from it. None for link-out entries (nothing is downloaded by the app) and
      when the bundle size is unknown.

    :return: dict with download, stored, stored_range, peak, staging, archives (names), subjects, exact
      (whether the download is exact) and notes.
    """
    selection = resolve_subset(entry, subset)
    disk = entry.get("disk") or {}
    files = entry.get("files") or []
    others = [f for f in files if f.get("role") != "archive"]
    if files:
        sizes_known = all(_is_count(f.get("size")) for f in [*selection["archives"], *others])
        download = sum(f.get("size") or 0 for f in [*selection["archives"], *others]) if sizes_known else None
    else:  # no file list (link-out and local entries): the size the source states, if any
        sizes_known = _is_count(entry.get("size"))
        download = entry["size"] if sizes_known else None
    fraction = selection["fraction"]
    notes = []
    stored_range = None
    if disk.get("stored_bytes"):
        stored_range = [round(disk["stored_bytes"][0] * fraction), round(disk["stored_bytes"][1] * fraction)]
    elif disk.get("voxels") and disk.get("timepoints_per_run") and disk.get("runs_per_subject") and selection["subjects"]:
        per_run = disk["voxels"] * (disk["timepoints_per_run"] * STORED_SAMPLE_BYTES + STORED_NORM_BYTES)
        stored_range = [per_run * runs * selection["subjects"] for runs in disk["runs_per_subject"]]
    if stored_range is None and entry.get("status") != "link_out":
        notes.append("The bundle size is not known before the conversion.")
    staging = 0
    if entry.get("converter") in STREAM_CONVERTERS and disk.get("voxels") and disk.get("timepoints_per_run"):
        runs_max = (disk.get("runs_per_subject") or [1, 1])[1]
        staging = disk["voxels"] * disk["timepoints_per_run"] * STAGED_SAMPLE_BYTES * runs_max
    stored = stored_range[1] if stored_range else None
    peak = None
    if link_out(entry):
        notes.append("Link-out only: the app does not download this connectome.")
    elif stored is not None or entry.get("status") == "unsupported":
        kept = stored or 0
        if entry.get("converter") in STREAM_CONVERTERS:
            other_bytes = sum(f.get("size") or 0 for f in others)
            peak = kept + other_bytes + staging + (download - other_bytes if keep_archives and download is not None else 0)
            if not keep_archives:
                notes.append("Archives are streamed into the bundle and never stored.")
        elif download is not None:
            peak = download + kept
    if not selection["exact"]:
        notes.append("The archives of a subject subset are estimated: subjects are assumed spread evenly over the archives in order.")
    return {
        "download": download,
        "stored": stored,
        "stored_range": stored_range,
        "peak": peak,
        "staging": staging,
        "archives": [f["name"] for f in selection["archives"]],
        "subjects": selection["subjects"],
        "exact": bool(selection["exact"] and sizes_known),
        "notes": notes,
    }

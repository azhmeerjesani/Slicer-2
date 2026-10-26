"""Tutorial cohorts: real, reusable lesion data built into ready studies (L-53, addendum 4.4 and F40).

A new lab member can learn the whole loop (overlap, lesion network mapping, a symptom score, the lesion volume
covariate) on data that may be reused. The recipes are in ``catalog/tutorials.json`` (format ``slicer-lnm-tutorials``
version 1). Nothing is bundled and nothing is downloaded unless a caller asks for one tutorial: the app calls
:func:`build_tutorial` when the user clicks Download. Only CC0-1.0 and Apache-2.0 sources are accepted
(:func:`validate_entry`); the van den Heuvel lesion repository has no licence and is never a tutorial.

Recipe fields: ``id``, ``title``, ``summary``, ``licence`` (+ ``badge``, ``terms_url``), ``citation``, ``homepage``,
``native`` (masks are in scan space: normalize first, A-03), ``space``, ``size_bytes`` and ``n_subjects`` (for the size
shown before the click), ``scan_bytes_each`` (native cohorts: the scans are an option), ``source``, ``study`` (the study
name, the group of every row, and ``columns``: the role, type, levels, units and description of every table column),
``teaches``, ``steps`` (the learning path) and ``volume_covariate`` (the guidance text of the volume covariate).

Sources (``source.type``):

- ``dataverse``: a Harvard Dataverse dataset with its files pinned in the recipe (``files`` = ``[[file id, name, bytes,
  md5], ...]``); every file is downloaded and verified on its own through ``fetch``. ``id_regex`` names the subject
  (group ``sid``) in a file name.
- ``s3``: an OpenNeuro dataset on its public S3 bucket. The folder of the lesion masks is listed at the click (the
  listing is cached for resuming offline), ``mask_regex`` names the subject (``sid``) and the path of a mask below the
  folder (``rel``); the participants table is one file; ``scan.replace`` turns a mask path into the path of its scan.
  A single-part ETag is the md5 of the file and is used to verify it.
- ``folder``: not downloaded by the app (the package is only linked): the user chooses the folder that they downloaded.
  The masks are the NIfTI files that are not templates, the grid is checked against the registry (``geometry``) and the
  scores come from the first table that matches ``table_regex``.

The built study (a folder the caller chooses, written in one step and renamed into place, so a failure leaves nothing)
has ``lesions/<sid>_space-<space>_desc-lesion_mask.nii.gz`` with BIDS sidecars, ``participants.tsv`` with the cohort's
table, ``participants.json`` with the roles of its columns (confirmed, so pickers offer the score and the covariates),
``volume_ml`` of every lesion (the sLNM covariate), a ``README.md`` (licence, citation, learning path) and
``study.json`` with ``tutorial: true`` plus ``tutorial_info`` (id, title, licence, citation, native, normalize_first,
scans, guidance). ``tutorial: true`` is what ``figures.watermark_for`` reads: figures carry "TUTORIAL DATA" (see
:func:`watermark_for_study` for other outputs such as reports). Scans of native cohorts (an option, large) go to
``sub-<id>/.../`` next to the study's ``lesions/`` and the rows get ``scan``. Native studies are flagged
"normalize first (A-03)": ``normalize batch`` needs the scans, and the normalization QC review applies as for any study.

Downloads use ``fetch`` (verified, resumable, cached in ``<cache>/tutorials/<id>/``; a cached file is not downloaded
again) and run in a few threads. The sources are fetched over HTTPS only when :func:`build_tutorial` runs.

Command line (one JSON event per line, see ``jobs``; the Library card runs it as a light job)::

    python -m LesionNetworkMappingLib.tutorials list [--catalog FILE] [--cache DIR]
    python -m LesionNetworkMappingLib.tutorials build --id ID --dest DIR [--cache DIR] [--catalog FILE] [--limit N]
        [--scans] [--source-dir DIR] [--offline] [--workers K] [--cancel-file F]
"""

import concurrent.futures
import contextlib
import csv
import gzip
import io
import json
import os
import re
import shutil
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET

from . import fetch, jobs

TUTORIALS_FORMAT = "slicer-lnm-tutorials"
TUTORIALS_VERSION = 1
CATALOG_FILE = os.path.join(fetch.CATALOG_DIR, "tutorials.json")
#: Only these licences make a tutorial (addendum 4.4: CC0 and Apache sources)
ALLOWED_LICENCES = ("CC0-1.0", "Apache-2.0")
SOURCE_TYPES = ("dataverse", "s3", "folder")
WATERMARK = "TUTORIAL DATA"
README_NAME = "README.md"
#: Folder of the downloads below the LNM cache folder
CACHE_SUBDIR = "tutorials"
LISTING_FILE = "listing.json"
DEFAULT_WORKERS = 4
# Columns the study fills itself; a table with the same names must not overwrite them.
RESERVED_COLUMNS = frozenset({"lesion", "scan", "space", "volume_ml", "include", "group", "status", "qc"})
PROGRESS_INTERVAL_S = 0.3
HTTP_TIMEOUT_S = (10.0, 60.0)
NIFTI_SUFFIXES = (".nii.gz", ".nii")
ID_COLUMNS = ("participant_id", "subject", "subject_id", "id", "sub")

_FILES_FIELDS = 4  # [file id, name, bytes, md5]


class TutorialError(Exception):
    """A tutorial cannot be built; ``code`` names the case ("unknown_id", "dest_exists", "source", "empty", ...)."""

    code = "tutorial"

    def __init__(self, message, code=None, **fields):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.fields = fields


# Catalog

_catalogs = {}


def load_catalog(path=None):
    """The tutorials catalog (a dictionary with ``entries``): the shipped one, or any file of the same format (cached per file and mtime)."""
    path = os.fspath(path) if path else CATALOG_FILE
    try:
        stamp = (os.path.abspath(path), os.stat(path).st_mtime_ns)
    except OSError as exc:
        raise TutorialError(f"Cannot read the tutorials catalog {path}: {exc}", code="catalog") from None
    if stamp not in _catalogs:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            raise TutorialError(f"Cannot read the tutorials catalog {path}: {exc}", code="catalog") from None
        if not isinstance(data, dict) or data.get("format") != TUTORIALS_FORMAT or data.get("version") != TUTORIALS_VERSION or not isinstance(data.get("entries"), list):
            raise TutorialError(f"Not a {TUTORIALS_FORMAT} version {TUTORIALS_VERSION} catalog: {path}", code="catalog")
        ids = [entry.get("id") for entry in data["entries"] if isinstance(entry, dict)]
        if len(ids) != len(data["entries"]) or not all(ids) or len(set(ids)) != len(ids):
            raise TutorialError(f"The tutorials catalog needs a unique id for every entry: {path}", code="catalog")
        _catalogs[stamp] = data
    return _catalogs[stamp]


def entries(catalog=None):
    """The recipes of a catalog (a loaded catalog, a path or None for the shipped one), in file order."""
    return list(catalog["entries"] if isinstance(catalog, dict) else load_catalog(catalog)["entries"])


def get(tutorial_id, catalog=None):
    """The recipe with this id."""
    for entry in entries(catalog):
        if entry["id"] == tutorial_id:
            return entry
    raise TutorialError(f"No tutorial {tutorial_id!r}", code="unknown_id", id=tutorial_id)


def validate_entry(entry):
    """Problems (list of strings) of one recipe; an empty list when it is complete and its licence allows a tutorial."""
    problems = []
    for key in ("id", "title", "summary", "licence", "badge", "citation", "terms_url", "source", "study", "steps"):
        if not entry.get(key):
            problems.append(f"missing {key}")
    if entry.get("licence") not in ALLOWED_LICENCES:
        problems.append(f"licence {entry.get('licence')!r} is not one of {', '.join(ALLOWED_LICENCES)}")
    if entry.get("licence") and entry.get("badge") != fetch.badge_for(entry["licence"]):
        problems.append(f"badge {entry.get('badge')} does not match licence {entry.get('licence')}")
    source = entry.get("source") or {}
    kind = source.get("type")
    if kind not in SOURCE_TYPES:
        problems.append(f"unknown source type {kind!r}")
    elif kind == "dataverse":
        for key in ("server", "doi", "id_regex", "files"):
            if not source.get(key):
                problems.append(f"dataverse source: missing {key}")
        for item in source.get("files") or []:
            if not (isinstance(item, list) and len(item) == _FILES_FIELDS and isinstance(item[0], int) and isinstance(item[1], str) and isinstance(item[2], int) and re.fullmatch(r"[0-9a-f]{32}", str(item[3]))):
                problems.append(f"dataverse source: bad file entry {item!r}")
                break
    elif kind == "s3":
        for key in ("base", "dataset", "mask_prefix", "mask_regex", "table"):
            if not source.get(key):
                problems.append(f"s3 source: missing {key}")
        if source.get("mask_regex") and not {"sid", "rel"} <= set(re.compile(source["mask_regex"]).groupindex):
            problems.append("s3 source: mask_regex needs the groups sid and rel")
    elif kind == "folder" and not source.get("link"):
        problems.append("folder source: missing link")
    for key in ("server", "base", "link"):
        if source.get(key) and not str(source[key]).startswith(("https://", "http://")):
            problems.append(f"source {key} must be an http(s) URL")
    if entry.get("native") and kind == "s3" and not (source.get("scan") or {}).get("replace"):
        problems.append("a native cohort needs the rule that finds the scan of a mask")
    for name, spec in ((entry.get("study") or {}).get("columns") or {}).items():
        if spec.get("role") not in ("score", "covariate", "group", "block", "metadata", "bookkeeping"):
            problems.append(f"column {name}: bad role {spec.get('role')!r}")
    return problems


# Sizes and the cache


def human_size(size):
    """"116 MB", "5.7 MB", "430 KB": a byte count for people (None gives "unknown size")."""
    if size is None:
        return "unknown size"
    size = float(size)
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if size >= scale:
            value = size / scale
            return f"{value:.0f} {unit}" if value >= 100 else f"{value:.1f} {unit}"
    return f"{int(size)} B"


def estimate(entry, limit=None, with_scans=False):
    """What a build downloads, before any listing: ``{"subjects", "bytes", "scan_bytes", "total_bytes"}`` (None where unknown)."""
    total = entry.get("n_subjects")
    count = min(int(limit), total) if limit and total else total
    base = entry.get("size_bytes")
    masks = None if base is None or not total else int(base * count / total)
    scans = int(count * entry.get("scan_bytes_each", 0)) if with_scans and entry.get("native") and count else 0
    return {"subjects": count, "bytes": masks, "scan_bytes": scans, "total_bytes": None if masks is None else masks + scans}


def cache_root(cache=None):
    return os.path.join(fetch.cache_dir(cache), CACHE_SUBDIR)


def tutorial_cache(tutorial_id, cache=None):
    """The folder of the downloads of one tutorial (it has its own fetch manifest)."""
    return os.path.join(cache_root(cache), tutorial_id)


def cache_bytes(tutorial_id, cache=None):
    """Bytes of the downloaded files of a tutorial (0 when there are none)."""
    total = 0
    for folder, _subfolders, files in os.walk(tutorial_cache(tutorial_id, cache)):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.path.getsize(os.path.join(folder, name))
    return total


def clear_cache(tutorial_id, cache=None):
    """Delete the downloaded files of a tutorial (studies that were built from them keep their own copies)."""
    folder = tutorial_cache(tutorial_id, cache)
    shutil.rmtree(folder, ignore_errors=True)
    return not os.path.exists(folder)


# Study metadata and watermark


def _metadata(study):
    if isinstance(study, dict):
        return study
    if hasattr(study, "metadata") and isinstance(study.metadata, dict):
        return study.metadata
    path = os.fspath(getattr(study, "path", study))
    if os.path.isdir(path):
        path = os.path.join(path, "study.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_tutorial(study):
    """True for a tutorial study. ``study``: a Study, a folder, a study.json path or its metadata dictionary."""
    meta = _metadata(study)
    tags = [str(tag).lower() for tag in meta.get("tags", []) or []]
    return bool(meta.get("tutorial")) or "tutorial" in tags


def tutorial_info(study):
    """``tutorial_info`` of a tutorial study (id, title, licence, citation, native, normalize_first, scans, guidance), else None."""
    meta = _metadata(study)
    info = meta.get("tutorial_info")
    return dict(info) if is_tutorial(meta) and isinstance(info, dict) else None


def watermark_for_study(study):
    """"TUTORIAL DATA" for a tutorial study, else None. Figures use ``figures.watermark_for``; reports and other outputs call this."""
    return WATERMARK if is_tutorial(study) else None


# Natural order and tables


def natural_key(text):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", str(text))]


def read_table(path, delimiter=None):
    """Read a csv or tsv table, honouring quotes: ``(columns, rows)`` with the cells as stripped text ("" when empty)."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        text = f.read()
    if delimiter is None:
        delimiter = "\t" if str(path).lower().endswith(".tsv") or text.split("\n", 1)[0].count("\t") >= text.split("\n", 1)[0].count(",") else ","
    rows = [row for row in csv.reader(io.StringIO(text), delimiter=delimiter) if any(cell.strip() for cell in row)]
    if not rows:
        return [], []
    columns = [cell.strip() for cell in rows[0]]
    if len(set(columns)) != len(columns) or not all(columns):
        raise TutorialError(f"{path}: duplicate or empty column names", code="table")
    out = []
    for row in rows[1:]:
        cells = [cell.strip() for cell in row][: len(columns)]
        cells += [""] * (len(columns) - len(cells))
        out.append(dict(zip(columns, cells, strict=True)))
    return columns, out


def _id_column(columns, wanted=None):
    if wanted and wanted in columns:
        return wanted
    lowered = {column.lower(): column for column in columns}
    for name in ID_COLUMNS:
        if name in lowered:
            return lowered[name]
    return columns[0] if columns else None


def _label(value):
    from .study import participant_label

    return participant_label(value)


def _match_key(value):
    """Key that matches a table id to a mask id: "sub-003", "3" and "003" are the same subject (letters are compared in lower case)."""
    text = _label(value)[4:].lower()
    return str(int(text)) if text.isdigit() else text


# HTTP helpers for the listings


def _session(session=None):
    return session or fetch.make_session()


def _http_get(url, session, what):
    import requests

    try:
        response = session.get(url, timeout=HTTP_TIMEOUT_S)
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise fetch.OfflineError(f"No network connection to read {what}.") from None
    except requests.RequestException as exc:
        raise TutorialError(f"Reading {what} failed: {exc}", code="http") from None
    if response.status_code >= 400:
        raise TutorialError(f"Reading {what} failed: HTTP {response.status_code} for {url}", code="http", status=response.status_code)
    return response.content


def list_dataverse(server, doi, session=None):
    """The files of a Dataverse dataset (latest version): ``[[file id, name, bytes, md5], ...]`` (used when a recipe does not pin them)."""
    url = f"{server.rstrip('/')}/api/datasets/:persistentId/?persistentId={urllib.parse.quote(doi, safe=':/')}"
    data = json.loads(_http_get(url, _session(session), f"the file list of {doi}"))
    try:
        files = data["data"]["latestVersion"]["files"]
        return [[int(item["dataFile"]["id"]), item["label"], int(item["dataFile"]["filesize"]), str(item["dataFile"].get("md5") or "")] for item in files]
    except (KeyError, TypeError, ValueError):
        raise TutorialError(f"The file list of {doi} is not in the expected format", code="source") from None


def list_s3(base, prefix, session=None):
    """The objects below ``prefix`` of a public S3 bucket (paginated): ``[[key, bytes, etag], ...]``."""
    session = _session(session)
    keys, token = [], None
    while True:
        url = f"{base.rstrip('/')}/?list-type=2&prefix={urllib.parse.quote(prefix)}&max-keys=1000"
        if token:
            url += f"&continuation-token={urllib.parse.quote(token)}"
        try:
            root = ET.fromstring(_http_get(url, session, f"the listing of {prefix}"))
        except ET.ParseError as exc:
            raise TutorialError(f"The listing of {prefix} is not XML: {exc}", code="source") from None
        token, truncated = None, False
        for node in root:
            tag = node.tag.rsplit("}", 1)[-1]
            if tag == "Contents":
                fields = {child.tag.rsplit("}", 1)[-1]: (child.text or "") for child in node}
                keys.append([fields.get("Key", ""), int(fields.get("Size") or 0), fields.get("ETag", "").strip('"')])
            elif tag == "IsTruncated":
                truncated = (node.text or "").strip().lower() == "true"
            elif tag == "NextContinuationToken":
                token = node.text
        if not truncated or not token:
            return keys


def _s3_listing(entry, cache, offline, session):
    """``([[key, bytes, etag]...], warnings)`` of the mask folder of an s3 source: listed now, else the copy of an earlier listing."""
    source = entry["source"]
    path = os.path.join(tutorial_cache(entry["id"], cache), LISTING_FILE)
    saved = None
    try:
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        saved = None
    if saved and saved.get("prefix") != source["mask_prefix"]:
        saved = None
    try:
        if fetch.offline_requested(offline):
            raise fetch.OfflineError("The network is switched off.")
        keys = list_s3(source["base"], source["mask_prefix"], session)
    except fetch.OfflineError as exc:
        if saved:
            return saved["keys"], [f"No connection ({exc}): the listing of {saved.get('fetched', 'an earlier download')} is used."]
        raise fetch.OfflineError(f"{entry['title']} cannot be listed: {exc}") from None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    jobs.write_json_atomic(path, {"prefix": source["mask_prefix"], "fetched": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "keys": keys})
    return keys, []


# Candidates: the subjects a source offers


def _resource(entry, name, urls, size=None, md5=None, kind="tutorial"):
    resource = {"id": f"{entry['id']}:{name}", "title": name, "kind": kind, "urls": list(urls), "filename": name, "licence": entry["licence"]}
    if size:
        resource["size"] = int(size)
    resource["checksum"] = {"algo": "md5", "value": md5.lower()} if md5 and re.fullmatch(r"[0-9a-fA-F]{32}", md5) else fetch.RECORD_ON_FIRST_DOWNLOAD
    return resource


def _object_url(base, key):
    return f"{base.rstrip('/')}/{urllib.parse.quote(key, safe='/')}"


def dataverse_candidates(entry, session=None):
    """``[{"sid", "name", "size", "resource"}]`` of the masks of a dataverse source (the pinned file list; its md5 verifies every file)."""
    source = entry["source"]
    files = source.get("files") or list_dataverse(source["server"], source["doi"], session)
    pattern = re.compile(source["id_regex"])
    out = []
    for file_id, name, size, md5 in files:
        match = pattern.match(name)
        if match is None:
            continue
        url = f"{source['server'].rstrip('/')}/api/access/datafile/{file_id}"
        out.append({"sid": _label(match.group("sid")), "name": name, "size": size, "resource": _resource(entry, name, [url], size, md5)})
    return out


def s3_candidates(entry, cache=None, offline=None, session=None):
    """``([{"sid", "name", "size", "rel", "resource", "scan"}], warnings)`` of the masks of an s3 source (a listing of the mask folder)."""
    source = entry["source"]
    keys, warnings = _s3_listing(entry, cache, offline, session)
    pattern = re.compile(source["mask_regex"])
    scan_rule = source.get("scan") or {}
    out = []
    for key, size, etag in keys:
        if not key.startswith(source["mask_prefix"]):
            continue
        match = pattern.match(key[len(source["mask_prefix"]):])
        if match is None:
            continue
        rel = match.group("rel")
        name = rel.rsplit("/", 1)[-1]
        item = {"sid": _label(match.group("sid")), "name": name, "size": size, "rel": rel, "scan": None,
                "resource": _resource(entry, f"mask:{rel}", [_object_url(source["base"], key)], size, etag if "-" not in etag else None)}
        item["resource"]["filename"] = name
        if scan_rule.get("replace"):
            scan_rel = rel
            for pattern_text, replacement in scan_rule["replace"]:
                scan_rel = re.sub(pattern_text, replacement, scan_rel)
            if scan_rel != rel:
                scan_name = scan_rel.rsplit("/", 1)[-1]
                scan = _resource(entry, f"scan:{scan_rel}", [_object_url(source["base"], scan_rule.get("root", "") + scan_rel)], entry.get("scan_bytes_each"))
                scan["filename"] = scan_name
                item["scan"] = {"rel": scan_rel, "resource": scan}
        out.append(item)
    expected = (source.get("expected") or {}).get("masks")
    if expected is not None and len(out) != expected:
        warnings.append(f"The source lists {len(out)} masks; the recipe expects {expected}. The dataset may have changed.")
    return out, warnings


def _stem(name):
    for suffix in NIFTI_SUFFIXES:
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return None


def folder_candidates(entry, source_dir):
    """``([{"sid", "name", "path"}], warnings)`` of the NIfTI masks of a folder the user chose (templates and atlases are left out)."""
    source = entry["source"]
    if not source_dir or not os.path.isdir(source_dir):
        raise TutorialError(f"{entry['title']} is not downloaded by the app: get it from {source.get('link')}, then choose its folder.", code="source_dir")
    exclude = re.compile(source["exclude_regex"], re.IGNORECASE) if source.get("exclude_regex") else None
    id_pattern = re.compile(source["id_regex"]) if source.get("id_regex") else None
    out, warnings, seen = [], [], {}
    for folder, subfolders, files in os.walk(source_dir):
        subfolders[:] = sorted(name for name in subfolders if not name.startswith("."))
        for name in sorted(files):
            stem = _stem(name)
            if stem is None or name.startswith("."):
                continue
            relative = os.path.relpath(os.path.join(folder, name), source_dir).replace(os.sep, "/")
            if exclude is not None and exclude.search(relative):
                continue
            match = id_pattern.search(stem) if id_pattern else None
            if match is not None:
                raw = match.group("sid") if "sid" in id_pattern.groupindex else match.group(0)
            else:
                digits = re.findall(r"\d+", stem)
                raw = digits[-1] if digits else re.sub(r"[^A-Za-z0-9]", "", stem)
            if not raw:
                warnings.append(f"{relative}: no subject id")
                continue
            sid = _label(raw)
            if sid in seen:
                warnings.append(f"{relative}: {sid} already has a mask ({seen[sid]})")
                continue
            seen[sid] = relative
            out.append({"sid": sid, "name": name, "size": os.path.getsize(os.path.join(folder, name)), "path": os.path.join(folder, name)})
    return out, warnings


def folder_table(entry, source_dir):
    """The path of the score table of a ``folder`` source (the first that matches ``table_regex``), else None."""
    pattern = re.compile(entry["source"].get("table_regex") or r"\.(csv|tsv)$", re.IGNORECASE)
    for folder, subfolders, files in os.walk(source_dir):
        subfolders[:] = sorted(name for name in subfolders if not name.startswith("."))
        for name in sorted(files):
            if pattern.search(name) and not name.startswith("."):
                return os.path.join(folder, name)
    return None


def table_resource(entry):
    """The participants table of an s3 source as a fetch resource (``None`` for the other sources)."""
    source = entry["source"]
    if source["type"] != "s3":
        return None
    name = source["table"].rsplit("/", 1)[-1]
    return _resource(entry, f"table:{name}", [_object_url(source["base"], source["table"])], None)


# Choosing the subjects


def _column_specs(entry):
    return (entry.get("study") or {}).get("columns") or {}


def unique_candidates(candidates, warnings=None):
    """The candidates without a second mask of the same subject ("sub-3" and "sub-03", or two sessions): the first in natural order is kept."""
    seen, out = {}, []
    for item in sorted(candidates, key=lambda item: natural_key(item.get("rel") or item["name"])):
        key = _match_key(item["sid"])
        if key in seen:
            if warnings is not None:
                warnings.append(f"{item.get('rel') or item['name']}: {item['sid']} already has a mask ({seen[key]}); only the first is used.")
            continue
        seen[key] = item.get("rel") or item["name"]
        out.append(item)
    return out


def select_subjects(entry, candidates, rows, id_column, limit=None):
    """The candidates to build: all of them, or (``limit``) the first ``limit`` in natural order, subjects with a score first."""
    ordered = sorted(candidates, key=lambda item: natural_key(item["sid"]))
    if not limit or limit >= len(ordered):
        return ordered
    scores = [name for name, spec in _column_specs(entry).items() if spec.get("role") == "score"]
    scored = set()
    if scores and rows and id_column:
        for row in rows:
            if row.get(id_column) and any(row.get(name, "") not in ("", "n/a", "NA") for name in scores):
                scored.add(_match_key(row[id_column]))
    has_score = [item for item in ordered if _match_key(item["sid"]) in scored]
    chosen = (has_score + [item for item in ordered if item not in has_score])[: int(limit)]
    return sorted(chosen, key=lambda item: natural_key(item["sid"]))


# Downloads


def _cancelled(cancel):
    """True when ``cancel`` (an object with ``is_set()``/``cancelled()``, or a callable) says stop."""
    if cancel is None:
        return False
    for name in ("is_set", "cancelled"):
        check = getattr(cancel, name, None)
        if callable(check):
            return bool(check())
    return bool(cancel()) if callable(cancel) else False


class _Stop:
    """Cancellation for the workers: the caller's token, or one worker failing."""

    def __init__(self, cancel):
        self.cancel = cancel
        self.failed = threading.Event()
        self.reason = None

    def is_set(self):
        return self.failed.is_set() or _cancelled(self.cancel)

    __call__ = is_set


def download_all(tutorial_id, jobs_list, cache=None, progress=None, cancel=None, offline=None, workers=DEFAULT_WORKERS):
    """Fetch the resources of ``jobs_list`` (``[{"resource", "optional"}]``) in ``workers`` threads; each job gets ``"path"`` (or ``"error"`` when optional).

    ``progress("download", files done, files total, bytes_done=..., bytes_total=...)`` is called at most every 0.3 s.
    A failure of a required file or Stop ends the other downloads and raises (``jobs.Cancelled`` for Stop).
    """
    folder = tutorial_cache(tutorial_id, cache)
    stop = _Stop(cancel)
    lock = threading.Lock()
    state = {"files": 0, "last": 0.0}
    partial = {}
    total_bytes = sum(int(item["resource"].get("size") or 0) for item in jobs_list)
    local = threading.local()

    def report(force=False):
        if progress is None:
            return
        now = time.monotonic()
        if force or now - state["last"] >= PROGRESS_INTERVAL_S:
            state["last"] = now
            progress("download", state["files"], len(jobs_list), bytes_done=sum(partial.values()), bytes_total=total_bytes)

    def work(item):
        resource = item["resource"]
        if not hasattr(local, "session"):
            local.session = fetch.make_session()

        def on_bytes(done, _total, _id=resource["id"]):
            with lock:
                partial[_id] = done
                report()

        try:
            path = fetch.fetch(resource, folder, progress=on_bytes, cancel=stop, offline=offline, session=local.session)
        except fetch.FetchError as exc:
            if item.get("optional") and exc.code in ("http", "not_cached") and not isinstance(exc, fetch.OfflineError):
                item["error"] = exc.message
                path = None
            else:
                raise
        with lock:
            state["files"] += 1
            partial[resource["id"]] = int(resource.get("size") or partial.get(resource["id"], 0))
            report()
        return path

    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        futures = {pool.submit(work, item): item for item in jobs_list}
        for future in concurrent.futures.as_completed(futures):
            try:
                futures[future]["path"] = future.result()
            except BaseException as exc:
                errors.append(exc)
                stop.failed.set()
    if errors:  # a real failure first: the other workers only stopped because of it
        raise next((exc for exc in errors if not isinstance(exc, jobs.Cancelled)), errors[0])
    if progress is not None:
        with lock:
            report(force=True)
    return jobs_list


# The study


def _gzip_copy(source, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(source, "rb") as f:
        head = f.read(2)
    if head == b"\x1f\x8b":
        shutil.copyfile(source, dest)
        return
    with open(source, "rb") as src, open(dest, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=6, mtime=0) as out:
        shutil.copyfileobj(src, out, 1024 * 1024)


def _plain_copy(source, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(source, dest)


def _lesion_space(entry, header):
    """``(space, warning or None)`` of a mask: native cohorts are native, the others are recognised from the grid."""
    from . import geometry

    shape, affine = header["shape"][:3], header["affine"]
    if entry.get("native"):
        return "native", None
    source = entry["source"]
    match = geometry.detect_grid_candidates(shape, affine)
    if source.get("grid"):
        if match is not None and match.grid_id == source["grid"]:
            return source.get("grid_space") or match.candidates[0], None
        return "native", f"The grid of a mask ({'x'.join(str(n) for n in shape)}) is not the expected {source['grid']}."
    space = geometry.detect_space(shape, affine)
    if space == "native":
        return "native", f"The grid of a mask ({'x'.join(str(n) for n in shape)}) is not a known template grid; its space is native."
    return space, None


def _write_sidecar(study, sid, entry, space, has_scan):
    from . import schema
    from .study import now_iso

    studio = {"tutorial": entry["id"], "source": entry.get("homepage") or entry["source"].get("link")}
    if space == "native" and not has_scan:
        row = study.subject(sid)
        sidecar = {"Type": "Lesion"}
        resolution = study._lesion_resolution(row)
        if resolution:
            sidecar["Resolution"] = resolution
        sidecar[schema.FORK_KEY] = {"participant_id": sid, "space": space, "modified": now_iso(), **studio}
        path = study.lesion_sidecar_path(sid)
        schema.write_text_atomic(path, json.dumps(sidecar, indent=2, ensure_ascii=False) + "\n")
        return path
    return study.write_lesion_sidecar(sid, studio=studio)


def _readme(entry, report):
    lines = [f"# {entry['study'].get('name') or entry['title']}", "", f"**{WATERMARK}.** This study was built by Slicer Studio from public data to practise on. Do not draw research conclusions from it.", ""]
    lines += [entry["summary"], "", "## Source and licence", "", f"- Licence: {entry['licence']} ({entry['terms_url']})", f"- Source: {entry.get('homepage') or entry['source'].get('link') or ''}", f"- Please cite: {entry['citation']}", ""]
    lines += ["## What is in this study", "", f"- {report['subjects']} subjects with a lesion mask (`lesions/`), listed in `participants.tsv`; the roles of its columns are in `participants.json`."]
    lines.append("- `volume_ml` is the lesion volume (the covariate that sLNM adjusts for by default).")
    if entry.get("native"):
        lines.append("- The masks are in scan (native) space: **normalize first (A-03)**." + (" The scans are in `sub-*/`." if report.get("scans") else " The scans were not downloaded; add them to normalize in the app."))
    lines += ["", "## Learning path", ""]
    lines += [f"{number}. {step}" for number, step in enumerate(entry["steps"], 1)]
    if entry.get("volume_covariate"):
        lines += ["", "## The lesion volume covariate", "", entry["volume_covariate"]]
    if entry.get("notes"):
        lines += ["", "## Notes", "", entry["notes"]]
    return "\n".join(lines) + "\n"


def _apply_table(study, entry, table_path, warnings, known=None):
    """Merge the cohort's table into the rows of the study.

    :param known: ids of every mask the source offers (the study may hold only the first N of them).
    :return: (number of table rows that have no mask at the source, the table's columns without the id).
    """
    columns, rows = read_table(table_path)
    id_column = _id_column(columns, (entry.get("study") or {}).get("id_column"))
    value_map = (entry.get("study") or {}).get("value_map") or {}
    kept = [column for column in columns if column != id_column and column.lower() not in RESERVED_COLUMNS]
    left_out = [column for column in columns if column != id_column and column.lower() in RESERVED_COLUMNS]
    if left_out:
        warnings.append(f"The table's column(s) {', '.join(left_out)} are used by the study itself and were not copied.")
    unmatched = 0
    by_key = {_match_key(sid): sid for sid in study.subject_ids}
    offered = {_match_key(sid) for sid in known} if known is not None else set(by_key)
    for row in rows:
        try:
            sid = by_key.get(_match_key(row[id_column]))
        except (KeyError, ValueError):
            continue
        if sid is None:
            unmatched += 0 if _match_key(row[id_column]) in offered else 1
            continue
        for column in kept:
            value = value_map.get(column, {}).get(row[column], row[column])
            if value in ("", "n/a", "NA"):
                continue
            try:
                study.set(sid, {column: value})
            except ValueError as exc:
                warnings.append(f"{sid}: {column} {value!r} was not accepted ({exc})")
    for column in kept:  # columns of the table appear even when no included subject has a value
        if column not in study.columns:
            study.set(next(iter(study.subject_ids)), {column: "n/a"})
    return unmatched, kept


def _apply_roles(study, entry, table_columns, table_path, warnings):
    specs = dict(_column_specs(entry))
    if not specs and table_path:  # a folder source: the scores are found by name
        names = [name.lower() for name in (entry["source"].get("score_columns") or [])]
        numeric = [column for column in table_columns if column in study.columns and all(study.value(sid, column) is None or study.value(sid, column, float) is not None for sid in study.subject_ids)]
        picked = [column for column in numeric if column.lower() in names] or (numeric if len(numeric) == 1 else [])
        for column in picked:
            specs[column] = {"role": "score", "type": "numeric", "description": "Score of the tutorial"}
    for name, spec in specs.items():
        if name not in study.columns:
            continue
        try:
            study.set_column_role(name, spec["role"], type=spec.get("type"), levels=spec.get("levels"), units=spec.get("units"), description=spec.get("description"))
        except (KeyError, ValueError) as exc:
            warnings.append(f"The role of {name} could not be set: {exc}")


def _sweep_stale(parent):
    """Remove ``<name>.building-<pid>`` folders that a killed build left next to the study folders (their process is gone)."""
    if os.name == "nt":  # os.kill(pid, 0) would end the process there
        return
    try:
        names = os.listdir(parent)
    except OSError:
        return
    for name in names:
        match = re.search(r"\.building-(\d+)$", name)
        if match is None or int(match.group(1)) == os.getpid():
            continue
        try:
            os.kill(int(match.group(1)), 0)
            continue  # that build is still running
        except ProcessLookupError:
            pass
        except (OSError, AttributeError, ValueError):
            continue
        shutil.rmtree(os.path.join(parent, name), ignore_errors=True)


def build_study(entry, dest, items, table_path=None, warnings=None, cancel=None, progress=None, known=None):
    """Build the study of a tutorial from local files; returns the report.

    :param items: ``[{"sid", "path", "scan_path" (optional), "scan_rel" (optional)}]``, the masks to put into the study.
    :param table_path: the cohort's table (csv or tsv), or None.
    :param dest: the study folder; it must not exist or be empty. The study is written next to it and renamed into place.
    :param known: ids of all masks of the source (default: those of ``items``), to count table rows that have no mask.
    """
    import numpy as np

    from . import nifti
    from .study import Study

    warnings = list(warnings or [])
    dest = os.path.abspath(os.fspath(dest))
    if os.path.isdir(dest) and os.listdir(dest):
        raise TutorialError(f"The folder {dest} is not empty; choose a new folder for the tutorial study.", code="dest_exists")
    if os.path.isfile(dest):
        raise TutorialError(f"{dest} is a file; choose a new folder.", code="dest_exists")
    if not items:
        raise TutorialError(f"{entry['title']}: no lesion masks were found.", code="empty")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    _sweep_stale(os.path.dirname(dest))
    work = f"{dest}.building-{os.getpid()}"
    shutil.rmtree(work, ignore_errors=True)
    try:
        study = Study.create(work, name=(entry.get("study") or {}).get("name") or entry["title"])
        group = (entry.get("study") or {}).get("group")
        spaces, skipped, scan_count = {}, [], 0
        for index, item in enumerate(items):
            if _cancelled(cancel):
                raise jobs.Cancelled(done=index)
            sid, path = item["sid"], item["path"]
            try:
                header = nifti.read_nifti_header(path)
                if len(header["shape"]) > 3:
                    raise ValueError(f"not a 3D image (shape {list(header['shape'])})")
                data, affine, _ = nifti.read_nifti(path)
            except (OSError, ValueError) as exc:
                skipped.append({"participant_id": sid, "reason": f"unreadable mask: {exc}"})
                continue
            space, warning = _lesion_space(entry, header)
            if warning and warning not in warnings:
                warnings.append(warning)
            count = int(np.count_nonzero(data))
            volume_ml = round(abs(float(np.linalg.det(np.asarray(affine, dtype=float)[:3, :3]))) * count / 1000.0, 4)
            values = {"space": space, "status": "traced", "volume_ml": volume_ml}
            if group:
                values["group"] = group
            if count == 0:
                values["include"] = "0"
                warnings.append(f"{sid}: the mask is empty; the row is excluded (include = 0).")
            study.add_subject(sid, values)
            rel = study.lesion_relpath(sid, space)
            _gzip_copy(path, os.path.join(work, *rel.split("/")))
            row_values = {"lesion": rel}
            if item.get("scan_path") and item.get("scan_rel") and os.path.isfile(item["scan_path"]):
                scan_dest = item["scan_rel"]
                _plain_copy(item["scan_path"], os.path.join(work, *scan_dest.split("/")))
                row_values["scan"] = scan_dest
                scan_count += 1
            study.set(sid, row_values)
            _write_sidecar(study, sid, entry, space, "scan" in row_values)
            spaces[space] = spaces.get(space, 0) + 1
            if progress is not None and (index % 50 == 0 or index == len(items) - 1):
                progress("build", index + 1, len(items))
        if not len(study):
            raise TutorialError(f"{entry['title']}: none of the masks could be read.", code="empty", skipped=skipped)
        unmatched, table_columns = 0, []
        if table_path:
            unmatched, table_columns = _apply_table(study, entry, table_path, warnings, known)
        _apply_roles(study, entry, table_columns, table_path, warnings)
        common = next(iter(spaces)) if len(spaces) == 1 else None
        if common and common != "native":
            study.metadata["space"] = common
        source = entry["source"]
        if source.get("grid") and source.get("grid_space"):
            study.grid_answers = {source["grid"]: source["grid_space"]}
        study.metadata["tutorial"] = True
        study.metadata["tutorial_info"] = {
            "id": entry["id"],
            "title": entry["title"],
            "licence": entry["licence"],
            "citation": entry["citation"],
            "native": bool(entry.get("native")),
            "normalize_first": bool(entry.get("native")),
            "scans": bool(scan_count),
            "guidance": entry.get("volume_covariate"),
        }
        report = {
            "id": entry["id"],
            "title": entry["title"],
            "subjects": len(study),
            "skipped": skipped,
            "without_mask": unmatched,
            "native": bool(entry.get("native")),
            "scans": scan_count,
            "spaces": spaces,
            "warnings": warnings,
            "watermark": WATERMARK,
            "score_columns": [name for name, _confirmed in study.score_columns() if _confirmed],
            "covariate_columns": study.covariate_columns(),
            "steps": list(entry["steps"]),
        }
        report["next"] = "normalize first (A-03)" if entry.get("native") else "map and analyse"
        with open(os.path.join(work, README_NAME), "w", encoding="utf-8", newline="\n") as f:
            f.write(_readme(entry, report))
        study.save()
        if os.path.isdir(dest):
            os.rmdir(dest)
        os.replace(work, dest)
        report["path"] = dest
        return report
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise


def build_tutorial(tutorial_id, dest, cache=None, progress=None, cancel=None, limit=None, with_scans=False, offline=None, catalog=None, source_dir=None, workers=DEFAULT_WORKERS, session=None):
    """Download a tutorial (only here, when the caller asks) and build its study in the new folder ``dest``; returns the report.

    :param tutorial_id: an ``id`` of the catalog (:func:`entries`).
    :param cache: the LNM cache folder (``fetch.cache_dir``); the downloads go to ``<cache>/tutorials/<id>/`` and are kept.
    :param progress: ``progress(stage, done, total, **extra)`` with the stages "plan", "download" and "build".
    :param cancel: object with ``is_set()`` (``jobs.CancelToken``): Stop; ``jobs.Cancelled`` is raised and nothing is built.
    :param limit: build the first N subjects only (subjects with a score first); None: all.
    :param with_scans: native cohorts: also download the scan of every selected mask (large; ``normalize batch`` needs them).
    :param source_dir: the folder the user downloaded, for ``folder`` sources.
    :param catalog: a loaded catalog or the path of a catalog file (default: the shipped one).
    :raises TutorialError: unknown id, a licence that is not allowed, a folder that is not empty, a source that changed.
    :raises fetch.OfflineError, fetch.ChecksumError, fetch.FetchError: the downloads (see ``fetch.fetch``).
    """
    entry = get(tutorial_id, catalog)
    problems = validate_entry(entry)
    if problems:
        raise TutorialError(f"The recipe {tutorial_id} is not valid: {'; '.join(problems)}", code="recipe")
    if progress:
        progress("plan", 0, 1)
    source = entry["source"]
    warnings = []
    table_path = None
    if source["type"] == "folder":
        candidates, found = folder_candidates(entry, source_dir)
        warnings += found
        table_path = folder_table(entry, source_dir)
    else:
        session = _session(session)
        if source["type"] == "dataverse":
            candidates = dataverse_candidates(entry, session)
        else:
            candidates, found = s3_candidates(entry, cache, offline, session)
            warnings += found
        resource = table_resource(entry)
        if resource is not None:
            table = [{"resource": resource}]
            download_all(entry["id"], table, cache, None, cancel, offline, 1)
            table_path = table[0]["path"]
    if not candidates:
        raise TutorialError(f"{entry['title']}: no lesion masks were found at the source.", code="empty")
    candidates = unique_candidates(candidates, warnings)
    rows, id_column = [], None
    if table_path:
        columns, rows = read_table(table_path)
        id_column = _id_column(columns, (entry.get("study") or {}).get("id_column"))
    chosen = select_subjects(entry, candidates, rows, id_column, limit)
    if progress:
        progress("plan", 1, 1, subjects=len(chosen), candidates=len(candidates))
    if source["type"] != "folder":
        todo = [{"item": item, "resource": item["resource"]} for item in chosen]
        if with_scans and entry.get("native"):
            todo += [{"item": item, "resource": item["scan"]["resource"], "optional": True, "scan": True} for item in chosen if item.get("scan")]
        download_all(entry["id"], todo, cache, progress, cancel, offline, workers)
        for job in todo:
            if job.get("scan"):
                if job.get("path"):
                    job["item"]["scan_path"] = job["path"]
                    job["item"]["scan_rel"] = job["item"]["scan"]["rel"]
                elif job.get("error"):
                    warnings.append(f"{job['item']['sid']}: the scan could not be downloaded ({job['error']}).")
            else:
                job["item"]["path"] = job["path"]
    if _cancelled(cancel):
        raise jobs.Cancelled(done=0)
    return build_study(entry, dest, chosen, table_path, warnings, cancel, progress, known=[item["sid"] for item in candidates])


# Command line


def _parser():
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.tutorials", description="Build the tutorial cohorts (CC0 and Apache sources) into studies.")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=jobs.JsonArgumentParser)
    listing = commands.add_parser("list", help="list the tutorials with their licence, size and cache state")
    build = commands.add_parser("build", help="download a tutorial and build its study")
    for sub in (listing, build):
        sub.add_argument("--catalog", default=None, help="a tutorials catalog file (default: the shipped one)")
        sub.add_argument("--cache", default=None, help="cache folder (default: $SLICER_LNM_CACHE or ~/.cache/SlicerStudio/LNM)")
        jobs.add_common_arguments(sub)
    build.add_argument("--id", required=True, help="tutorial id")
    build.add_argument("--dest", required=True, help="the new study folder")
    build.add_argument("--limit", type=jobs.positive_int, default=None, help="only the first N subjects (subjects with a score first)")
    build.add_argument("--scans", action="store_true", help="native cohorts: also download the scans")
    build.add_argument("--source-dir", default=None, help="the folder you downloaded (cohorts that are only linked)")
    build.add_argument("--offline", action="store_true", help="do not use the network: build from cached files")
    build.add_argument("--workers", type=jobs.positive_int, default=DEFAULT_WORKERS, help=f"parallel downloads (default: {DEFAULT_WORKERS})")
    return parser


def _main_list(args):
    for entry in entries(args.catalog):
        jobs.emit(
            "result",
            kind="tutorial",
            id=entry["id"],
            title=entry["title"],
            licence=entry["licence"],
            badge=entry["badge"],
            native=bool(entry.get("native")),
            size=entry.get("size_bytes"),
            subjects=entry.get("n_subjects"),
            cached=cache_bytes(entry["id"], args.cache),
            source=entry["source"]["type"],
        )
    jobs.emit("done", outputs=[], elapsed=round(jobs.elapsed(), 3), run_id=None)


def _main_build(args):
    cancel = jobs.CancelToken(args.cancel_file)
    started = time.monotonic()
    stages = {"plan": (1, "Listing the files"), "download": (2, "Downloading"), "build": (3, "Building the study")}
    seen = set()

    def progress(stage, done, total, **extra):
        index, label = stages[stage]
        if stage not in seen:
            seen.add(stage)
            jobs.emit("stage", name=stage, index=index, total=len(stages), label=label)
        if stage == "plan" and done == 0:
            return
        jobs.progress(done, total, started, stage=stage, **extra)

    try:
        report = build_tutorial(args.id, args.dest, cache=args.cache, progress=progress, cancel=cancel, limit=args.limit, with_scans=args.scans, offline=True if args.offline else None, catalog=args.catalog, source_dir=args.source_dir, workers=args.workers)
    except TutorialError as exc:
        raise jobs.JobError(exc.message, code=exc.code, **{key: value for key, value in exc.fields.items() if value is not None}) from None
    except fetch.FetchError as exc:
        raise jobs.JobError(exc.message, code=exc.code, **{key: value for key, value in exc.fields.items() if value is not None}) from None
    for message in report["warnings"][:50]:
        jobs.emit("warning", code="tutorial", message=message)
    jobs.emit("result", kind="study", path=report["path"], subjects=report["subjects"], native=report["native"], scans=report["scans"], without_mask=report["without_mask"], skipped=len(report["skipped"]), score_columns=report["score_columns"], next=report["next"])
    jobs.emit("done", outputs=[report["path"]], elapsed=round(jobs.elapsed(), 3), run_id=None, subjects=report["subjects"])


def main(argv):
    """Run one command (see the module documentation); events go to standard output."""
    args = _parser().parse_args(argv)
    jobs.emit("start", job="tutorials", command=args.command, argv=list(argv))
    if args.command == "list":
        return _main_list(args)
    return _main_build(args)


if __name__ == "__main__":
    jobs.run_job(main, job="tutorials")

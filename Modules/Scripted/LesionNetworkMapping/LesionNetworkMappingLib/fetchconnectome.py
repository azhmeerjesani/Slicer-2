"""Fetch a connectome from its provider and convert it into a bundle while it streams (L-35, decision D-L1).

GSP1000 (doi:10.7910/DVN/ILXIKS) is 236 GB in 20 tar archives; its Lead-DBS version (doi:10.7910/DVN/KKTJQC)
is 219 GB in 35. This job downloads the archives one after the other on the user's own network and pipes each
one into :func:`connectome.from_tar_stream`, so no archive is stored (unless ``--keep-archives``) and the extra
disk space is about zero. It runs only when the user asks for it, and only for the exact terms text the user has
accepted (addendum 4.1, never automatically, never bundled, no app-owned credentials):

- **Terms.** ``--terms-sha`` must equal the sha256 of the provider's *live* terms text (the Dataverse dataset API,
  field ``termsOfUse`` of the catalog entry), fetched when the job starts. A different text, a text that cannot be
  fetched, an entry without live terms, a link-out entry (terms not stated) and an unsupported entry are refused
  before anything is downloaded. The text that was accepted is kept in the bundle as ``terms-accepted.txt``.
- **Catalog check.** The files are listed live. The live dataset version, file ids, sizes and checksums are compared
  with the shipped catalog and every difference is reported as a ``catalog_differs`` warning; the live ids and
  checksums are the ones used.
- **Signed URLs.** ``GET /api/access/datafile/<id>`` answers 303 with a signed S3 URL. It is streamed with HTTP
  ``Range`` requests; on 403, or when it is older than 3,500 s, it is signed again, and a dropped connection resumes
  at the next unread byte (also after the next start of the job: see below). Tabular files (the participants table)
  are fetched with ``format=original``.
- **Streams.** ``--streams`` (1 to 4) archives are downloaded at the same time, each into a bounded buffer
  (``--prefetch-mb``); the bundle is written by one converter, archive by archive in name order, so the network of
  the next archives overlaps with the conversion of the current one.
- **Resume.** ``<dest>/<entry>.fetch/state.json`` records, per archive, the bytes consumed and the member offset of
  the next unconverted subject (:func:`connectome.from_tar_stream` checkpoints). ``--resume`` restarts the HTTP
  ``Range`` at that offset; archives that are done are skipped; subjects already in the bundle are never converted
  twice. With ``--keep-archives`` an interrupted archive is read again from its start, so that the stored file is
  complete.
- **Integrity.** The whole-archive MD5 is computed on the fly and compared with the one the provider lists whenever
  the stream was read from its first byte (reconnections in the same run keep the hash valid); a stream that was
  resumed from a member offset records "not verified (resumed); members verified by gzip CRC" (every member's gzip
  CRC32 and ISIZE and the NIfTI headers are always checked by the converter). On a mismatch the archive is read
  once more to compute its MD5 again; if it still differs the job stops (exit code 1, ``code: checksum``) and the
  caller may run it again with ``--accept-checksum-mismatch``, which records ``checksumOverride`` in
  ``connectome.json`` (``source``) and in the run record.
- **Keep-awake.** :class:`jobs.KeepAwake` runs ``caffeinate -i -w <pid>`` on macOS and calls
  SetThreadExecutionState on Windows; Linux does nothing.
- **Result.** ``<dest>/<entry>.lnmc`` (connectome.json keeps ``format: "lnmc"``, version 1) with ``source{doi, version,
  url, files[{name, md5, size, verification}]}``, ``kind``, ``licence``, ``termsSha256``, ``acknowledgement``,
  ``population``, ``preprocessing`` and ``trS`` from the catalog entry, ``participants`` (``participants.tsv``) and the
  dataset's documents under ``docs/``. The fetch is recorded in ``<dest>/derivatives/lnm/runs.jsonl`` (kind "fetch").

Command line (one JSON event per line, see ``jobs``; exit codes 0 done, 1 error, 2 usage or refused terms, 3 cancelled,
75 locked)::

    python -m LesionNetworkMappingLib.fetchconnectome --entry ID --dest DIR --terms-sha SHA256
        [--subset all|archives:00-01|subjects:N|NAME] [--streams 2] [--keep-archives] [--cancel-file F] [--resume]
        [--accept-checksum-mismatch] [--catalog connectomes|FILE] [--prefetch-mb 32] [--run-root DIR] [--parent-pid PID]
    python -m LesionNetworkMappingLib.fetchconnectome terms --entry ID [--catalog connectomes|FILE]
        (shows the live terms text and its sha256; downloads nothing else)
    python -m LesionNetworkMappingLib.fetchconnectome check --entry ID [--catalog ...]
        (compares the live listing with the catalog)

``--server URL`` replaces the scheme, host and port of the catalog's API URLs (a mirror, or the fake Dataverse of the
tests); the terms text must still match. The graphical interface never passes it. ``--parent-pid PID`` stops the job
(cancelled, resumable) when that process is gone: the application passes its own pid, so that a fetch never keeps
downloading after the application crashed (the PythonSlicer launcher outlives it, so the orphan watchdog alone does not
notice).
"""

import os
import sys

if __name__ == "__main__":
    from LesionNetworkMappingLib.jobs import set_blas_threads

    set_blas_threads(sys.argv[1:])  # before numpy is imported: BLAS reads these variables once

import hashlib
import html.parser
import json
import queue
import re
import shutil
import threading
import time
import urllib.parse
import warnings

from . import catalog as catalogs
from . import fetch, jobs

#: A signed URL older than this is signed again before it is used for a new request (Dataverse signs for 1 hour)
RESIGN_AFTER_S = 3500.0
CHUNK_BYTES = 256 * 1024
DEFAULT_PREFETCH_MB = 32
MAX_STREAMS = 4
#: Consecutive failed connections (without a byte of progress) before a download is given up
MAX_FAILURES = 6
#: Consecutive 403 answers (without progress) that a freshly signed URL may get before the download is given up
MAX_RESIGN_FAILURES = 3
RETRY_DELAYS_S = (0.5, 2.0, 5.0, 10.0, 20.0)
CONNECT_TIMEOUT_S = 15.0
READ_TIMEOUT_S = 60.0
PROGRESS_INTERVAL_S = 0.5
STATE_INTERVAL_S = 2.0
STATE_FORMAT = "slicer-lnm-fetch-state"
STATE_VERSION = 1
FETCH_KIND = "fetch"
#: Where the web build (SLICER_STUDIO_WEB or Docker) keeps user data
WEB_DATA_ROOT = "/data"
WEB_DEFAULT_DESTINATION = "/data/SlicerStudio"
NOT_VERIFIED_RESUMED = "not verified (resumed); members verified by gzip CRC"
VERIFICATION_MD5 = "md5"
VERIFICATION_MD5_SECOND = "md5 (second read)"
VERIFICATION_OVERRIDE = "checksum mismatch accepted by the user (checksum_override)"
VERIFICATION_NOT_PUBLISHED = "no checksum published"
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
#: Extra disk space kept beyond the plan before a fetch is allowed to start
DISK_MARGIN = 1.05


class _Transient(Exception):
    """A connection attempt failed in a way that may pass (dropped connection, 5xx, an expired signed URL)."""

    def __init__(self, message, resign=False):
        super().__init__(message)
        self.resign = resign


class _ReadCancelled(Exception):
    """The cancel token fired while a stream was read."""


def _is_cancelled(cancel):
    return fetch._is_cancelled(cancel)


#
# Platform helpers (the web build, destinations)
#


def is_web_build(environ=None, exists=os.path.exists):
    """True in the web build (``SLICER_STUDIO_WEB`` set) or inside a Docker container (``/.dockerenv``)."""
    environ = os.environ if environ is None else environ
    return bool(environ.get("SLICER_STUDIO_WEB")) or bool(exists("/.dockerenv"))


def default_destination(data_directory=None, web=None):
    """The folder proposed for a connectome: ``/data/SlicerStudio`` in the web build, else ``<data directory>/Connectomes``."""
    web = is_web_build() if web is None else web
    if web:
        return WEB_DEFAULT_DESTINATION
    base = data_directory or os.path.join(os.path.expanduser("~"), "SlicerStudio")
    return os.path.join(base, "Connectomes")


def destination_notice(destination, web=None):
    """A warning for a destination in the web build that is outside ``/data`` (it would be lost with the container), else ""."""
    web = is_web_build() if web is None else web
    if not web or not destination:
        return ""
    path = os.path.normpath(os.path.abspath(os.fspath(destination)))
    if path == WEB_DATA_ROOT or path.startswith(WEB_DATA_ROOT + os.sep):
        return ""
    return f"{destination} is inside the container but outside {WEB_DATA_ROOT}: connectomes saved there are lost when the container is removed. Use a folder under {WEB_DATA_ROOT}."


#
# Live dataset (terms, version, files)
#


def _dig(value, path):
    for part in str(path).split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def _rebase(url, server):
    """``url`` with the scheme, host and port of ``server`` (None keeps the URL)."""
    if not server or not url:
        return url
    source, target = urllib.parse.urlsplit(url), urllib.parse.urlsplit(server)
    return urllib.parse.urlunsplit((target.scheme or source.scheme, target.netloc or source.netloc, source.path, source.query, source.fragment))


class _TextExtractor(html.parser.HTMLParser):
    BLOCKS = ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._href = None

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCKS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")
        if tag == "a":
            self._href = dict(attrs).get("href")

    def handle_endtag(self, tag):
        if tag in self.BLOCKS:
            self.parts.append("\n")
        if tag == "a" and self._href:
            self.parts.append(f" ({self._href})")
            self._href = None

    def handle_data(self, data):
        self.parts.append(data)


def terms_display_text(text):
    """The terms text as plain text for display (HTML tags are removed). The sha256 is always of the original text."""
    text = str(text or "")
    if "<" not in text or ">" not in text:
        return text
    parser = _TextExtractor()
    parser.feed(text)
    parser.close()
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+\n", "\n", "".join(parser.parts))).strip()


def _get_json(session, url, what):
    if fetch.offline_requested():
        raise jobs.JobError(f"The network is switched off: {what} cannot be fetched.", code="offline")
    try:
        response = session.get(url, timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S))
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        raise jobs.JobError(f"{what} could not be fetched from {url}: {exc}", code="network") from None


def _live_file(item):
    data_file = item.get("dataFile") or {}
    checksum = data_file.get("checksum") or {}
    md5 = data_file.get("md5") or (checksum.get("value") if str(checksum.get("type", "")).upper() == "MD5" else None)
    name = item.get("label") or data_file.get("filename")
    return {
        "id": data_file.get("id"),
        "name": name,
        "folder": (item.get("directoryLabel") or "").strip("/"),
        "size": data_file.get("filesize"),
        "md5": str(md5).lower() if md5 else None,
        "tabular": bool(data_file.get("tabularData")) or str(name).lower().endswith(".tab"),
        "original_name": data_file.get("originalFileName"),
    }


def read_live(entry, session=None, server=None):
    """The provider's dataset now: ``{"terms": text, "version": "3.0", "files": [...], "api": URL}``.

    Raises ``jobs.JobError`` (``network``, ``offline`` or ``terms_missing``) when it cannot be read.
    """
    session = session or fetch.make_session()
    terms = entry.get("terms") or {}
    api = _rebase(terms.get("api") or (entry.get("source") or {}).get("api"), server)
    if not api:
        raise jobs.JobError(f"{entry.get('id')} has no dataset API to read the live terms and files from", code="not_fetchable")
    data = _get_json(session, api, "The dataset description")
    latest = _dig(data, "data.latestVersion") or {}
    number = latest.get("versionNumber")
    version = None if number is None else f"{number}.{latest.get('versionMinorNumber', 0)}"
    text = _dig(data, terms.get("field") or "data.latestVersion.termsOfUse")
    return {
        "api": api,
        "terms": text if isinstance(text, str) and text.strip() else None,
        "version": version,
        "files": [_live_file(item) for item in latest.get("files") or [] if isinstance(item, dict)],
    }


def live_terms(entry, session=None, server=None):
    """``(text, sha256)`` of the provider's live terms text of ``entry`` (a ``jobs.JobError`` when there is none)."""
    if (entry.get("terms") or {}).get("fetch") != "live":
        raise jobs.JobError(f"{entry.get('id')} has no live terms text to show", code="not_fetchable")
    live = read_live(entry, session, server)
    if not live["terms"]:
        raise jobs.JobError(f"The dataset description has no terms text at {entry['terms'].get('field')}", code="terms_missing")
    return live["terms"], catalogs.terms_sha256(live["terms"])


def check_fetchable(entry):
    """Raise ``jobs.JobError`` unless the app may fetch ``entry``: not link-out, supported, a Dataverse archive source."""
    entry_id = entry.get("id")
    if catalogs.link_out(entry):
        raise jobs.JobError(f"{entry_id}: the provider states no terms for this connectome, so the app only links to it ({entry.get('link_url')})", code="not_fetchable")
    if entry.get("status") != "supported":
        raise jobs.JobError(f"{entry_id}: {entry.get('status_note') or 'not supported in this version'}", code="not_fetchable")
    if (entry.get("source") or {}).get("type") != "dataverse":
        raise jobs.JobError(f"{entry_id}: only Dataverse sources can be fetched (source type {(entry.get('source') or {}).get('type')!r})", code="not_fetchable")
    if entry.get("converter") not in catalogs.STREAM_CONVERTERS:
        raise jobs.JobError(f"{entry_id}: no streaming converter", code="not_fetchable")
    if (entry.get("terms") or {}).get("fetch") != "live":
        raise jobs.JobError(f"{entry_id}: the terms text must be fetched live before a download", code="not_fetchable")


def _match_live(catalog_file, live_files):
    """The live file that is the catalog file: by id, else by folder and name, else by a unique name."""
    for item in live_files:
        if item["id"] is not None and item["id"] == catalog_file.get("id"):
            return item
    folder = str(catalog_file.get("folder") or "").strip("/")
    for item in live_files:
        if item["name"] == catalog_file["name"] and item["folder"] == folder:
            return item
    named = [item for item in live_files if item["name"] == catalog_file["name"]]
    return named[0] if len(named) == 1 else None


def compare_catalog(entry, live):
    """Differences between the shipped catalog entry and the live dataset: a list of human-readable strings."""
    differences = []
    version = (entry.get("source") or {}).get("version")
    if version and live["version"] and str(version) != str(live["version"]):
        differences.append(f"the dataset is at version {live['version']}; the catalog describes version {version}")
    for catalog_file in entry.get("files") or []:
        item = _match_live(catalog_file, live["files"])
        if item is None:
            differences.append(f"{catalog_file['name']} is no longer in the dataset")
            continue
        if item["id"] != catalog_file.get("id"):
            differences.append(f"{catalog_file['name']} has a new file id ({catalog_file.get('id')} -> {item['id']})")
        checksum = catalog_file.get("checksum")
        if isinstance(checksum, dict) and checksum.get("algo") == "md5" and item["md5"] and item["md5"] != str(checksum.get("value")).lower():
            differences.append(f"{catalog_file['name']} has a different checksum than the catalog")
        elif item["size"] is not None and catalog_file.get("size") is not None and not item["tabular"] and item["size"] != catalog_file["size"]:
            differences.append(f"{catalog_file['name']} has a different size ({item['size']:,} B, the catalog says {catalog_file['size']:,} B)")
    return differences


def plan_files(entry, live, subset=None):
    """The files a fetch needs, from the live listing: ``{"archives": [...], "others": [...], "selection": dict}``.

    Each file is ``{id, name, folder, size, md5, tabular, role}`` with the live id and checksum.
    Raises ``jobs.JobError`` (code ``listing``) when a needed file is no longer listed.
    """
    try:
        selection = catalogs.resolve_subset(entry, subset)
    except ValueError as exc:
        raise jobs.JobError(str(exc), code="usage", exit_code=jobs.EXIT_USAGE) from None
    chosen = {f["name"] for f in selection["archives"]}
    archives, others = [], []
    for catalog_file in entry.get("files") or []:
        is_archive = catalog_file.get("role") == "archive"
        if is_archive and catalog_file["name"] not in chosen:
            continue
        item = _match_live(catalog_file, live["files"])
        if item is None:
            raise jobs.JobError(f"{catalog_file['name']} is no longer listed in the dataset {entry['source'].get('doi')}; check the catalog for updates", code="listing")
        item = {**item, "role": catalog_file.get("role"), "size": catalog_file.get("size") if (is_archive and item["size"] is None) else item["size"]}
        (archives if is_archive else others).append(item)
    archives.sort(key=lambda f: f["name"])
    return {"archives": archives, "others": others, "selection": selection}


#
# Signed URLs and the Range stream of one file
#


class SignedUrlResolver:
    """Resolve Dataverse file ids to signed URLs: ``GET <server>/api/access/datafile/<id>`` answers 303 with the URL.

    A URL is reused until it is ``max_age_s`` old (3,500 s) or reported bad (:meth:`invalidate`); ``signs`` counts
    the resolutions made. A server that answers 200 instead of redirecting streams the file itself: the access URL is then used as it is.
    """

    def __init__(self, session, base, max_age_s=RESIGN_AFTER_S, clock=time.monotonic):
        self.session = session
        self.base = base.rstrip("/")
        self.max_age_s = max_age_s
        self.clock = clock
        self.signs = 0
        self._urls = {}
        self._lock = threading.Lock()

    def access_url(self, file):
        url = f"{self.base}/api/access/datafile/{file['id']}"
        return url + "?format=original" if file.get("tabular") else url

    def _sign(self, file):
        access = self.access_url(file)
        try:
            response = self.session.get(access, allow_redirects=False, stream=True, timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S))
        except Exception as exc:
            raise _Transient(f"cannot reach {access}: {exc}") from None
        try:
            status = response.status_code
            if status in REDIRECT_STATUSES and response.headers.get("Location"):
                self.signs += 1
                return urllib.parse.urljoin(access, response.headers["Location"])
            if status == 200:
                self.signs += 1
                return access
            if status >= 500 or status in (408, 429):
                raise _Transient(f"HTTP {status} from {access}")
            raise jobs.JobError(f"HTTP {status} for {access}: the provider does not allow this download", code="http", status=status)
        finally:
            response.close()

    def url(self, file, force=False):
        with self._lock:
            hit = self._urls.get(file["id"])
            if hit is not None and not force and self.clock() - hit[1] < self.max_age_s:
                return hit[0]
            url = self._sign(file)
            self._urls[file["id"]] = (url, self.clock())
            return url

    def invalidate(self, file, url=None):
        """Forget the URL of ``file`` (when it is still ``url``: another stream may have signed it again already)."""
        with self._lock:
            hit = self._urls.get(file["id"])
            if hit is not None and (url is None or hit[0] == url):
                del self._urls[file["id"]]


def _content_range(header):
    return fetch._content_range(header)


class ArchiveSource:
    """A read-in-order file object over the Range stream of one Dataverse file (``read(n)``, like an HTTP body).

    A network thread fills a bounded queue (``prefetch_bytes``), so the consumer (the converter) and the download
    overlap; it reconnects at the next unread byte after a dropped connection or a 5xx, and signs the URL again on 403.
    The MD5 of the bytes is computed on the fly when the stream starts at byte 0 (``md5`` is None otherwise).
    ``position`` is the offset of the next byte the consumer reads. ``tee_path`` writes every byte to that file
    (only for a stream that starts at 0).
    """

    def __init__(self, resolver, file, start=0, cancel=None, prefetch_bytes=DEFAULT_PREFETCH_MB << 20, tee_path=None, on_read=None, name=None):
        self.resolver = resolver
        self.file = file
        self.name = name or file.get("name")
        self.size = file.get("size")
        self.start = int(start)
        self.cancel = cancel
        self.tee_path = tee_path
        self.on_read = on_read
        self.position = self.start
        self.reconnects = 0
        self.resigns = 0
        self.md5 = None
        self.connected = threading.Event()
        self._fetched = self.start
        self._hash = hashlib.md5() if self.start == 0 else None  # noqa: S324 (the providers publish MD5)
        self._queue = queue.Queue(maxsize=max(2, int(prefetch_bytes) // CHUNK_BYTES))
        self._buffer = b""
        self._eof = False
        self._stop = threading.Event()
        self._thread = None
        self._tee = None
        self._response = None
        if self.tee_path and self.start:
            raise ValueError("A stream that is copied to a file must start at byte 0")

    # Network thread

    def open(self):
        """Start the network thread (reading starts it too)."""
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name=f"fetch-{self.name}", daemon=True)
            self._thread.start()
        return self

    def _put(self, item):
        while not self._stop.is_set():
            try:
                self._queue.put(item, timeout=0.2)
                return True
            except queue.Full:
                continue
        return False

    def _run(self):
        try:
            if self.tee_path:
                os.makedirs(os.path.dirname(self.tee_path), exist_ok=True)
                self._tee = open(self.tee_path, "wb")
            failures = resign_failures = 0
            while not self._stop.is_set():
                before = self._fetched
                try:
                    complete = self._connection()
                except _Transient as exc:
                    progressed = self._fetched > before
                    failures = 0 if progressed else failures + 1
                    resign_failures = 0 if progressed else resign_failures + (1 if exc.resign else 0)
                    if exc.resign:
                        self.resigns += 1
                    if resign_failures >= MAX_RESIGN_FAILURES:
                        raise jobs.JobError(f"{self.name}: the provider keeps refusing the signed URL (HTTP 403)", code="http", status=403) from None
                    if failures > MAX_FAILURES:
                        raise jobs.JobError(f"{self.name}: the download keeps failing ({exc}); what was read is kept and the job can be resumed", code="network") from None
                    self.reconnects += 1
                    self._wait(RETRY_DELAYS_S[min(failures, len(RETRY_DELAYS_S) - 1)] if failures else 0.0)
                    continue
                if complete:
                    break
                if self._stop.is_set():
                    break  # stopped (cancel): not a connection that ended early
                failures += 1  # the connection ended before the file did, without an error
                if failures > MAX_FAILURES:
                    raise jobs.JobError(f"{self.name}: the connection keeps closing early", code="network")
                self.reconnects += 1
            if self._stop.is_set():
                return
            if self._hash is not None:
                self.md5 = self._hash.hexdigest()
            self._put(None)
        except BaseException as exc:  # reported to the consumer
            self._put(exc)
        finally:
            if self._tee is not None:
                self._tee.close()
                self._tee = None

    def _wait(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self._stop.is_set():
            if _is_cancelled(self.cancel):
                self._stop.set()
                return
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def _check_total(self, total):
        if total is None:
            return
        if self.size is None:
            self.size = total
        elif total != self.size:
            raise jobs.JobError(f"{self.name} is {total:,} bytes at the provider but the listing says {self.size:,}: the dataset changed; check the catalog for updates", code="size")

    def _connection(self):
        """One connection from the next unread byte; True when the file is complete. Raises ``_Transient`` to reconnect."""
        import requests

        url = self.resolver.url(self.file)
        try:
            response = self.resolver.session.get(url, headers={"Range": f"bytes={self._fetched}-"}, stream=True, timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S))
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise _Transient(f"cannot connect: {exc}") from None
        except requests.RequestException as exc:
            raise jobs.JobError(f"{self.name}: the request failed: {exc}", code="http") from None
        self._response = response
        with response:
            status = response.status_code
            if status in (401, 403):
                self.resolver.invalidate(self.file, url)
                raise _Transient(f"HTTP {status}: the signed URL was refused", resign=True)
            if status == 416:
                _, total = _content_range(response.headers.get("Content-Range"))
                if total is not None and self._fetched >= total:
                    self._check_total(total)
                    return True
                raise jobs.JobError(f"{self.name}: the provider refused to continue at byte {self._fetched:,} (HTTP 416)", code="http", status=416)
            if status >= 500 or status in (408, 429):
                raise _Transient(f"HTTP {status}")
            if status >= 400:
                raise jobs.JobError(f"{self.name}: HTTP {status}", code="http", status=status)
            if status == 206:
                first, total = _content_range(response.headers.get("Content-Range"))
                if first != self._fetched:
                    raise jobs.JobError(f"{self.name}: the provider answered another range than the one asked for", code="range")
            elif self._fetched:
                raise jobs.JobError(f"{self.name}: the provider ignored the Range request (HTTP {status}); the download cannot continue at byte {self._fetched:,}", code="range")
            else:
                length = response.headers.get("Content-Length")
                total = int(length) if length and length.isdigit() else None
            self._check_total(total)
            self.connected.set()
            try:
                for chunk in response.iter_content(CHUNK_BYTES):
                    if self._stop.is_set():
                        return False
                    if _is_cancelled(self.cancel):
                        self._stop.set()
                        return False
                    if not chunk:
                        continue
                    if self.size is not None:
                        chunk = chunk[: max(0, self.size - self._fetched)]
                    if not chunk:
                        continue
                    if self._hash is not None:
                        self._hash.update(chunk)
                    if self._tee is not None:
                        self._tee.write(chunk)
                    if not self._put(chunk):
                        return False
                    self._fetched += len(chunk)
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError, requests.exceptions.ContentDecodingError) as exc:
                raise _Transient(f"the connection dropped at byte {self._fetched:,}: {exc}") from None
        return self.size is not None and self._fetched >= self.size

    # Consumer

    def _fill(self):
        self.open()
        while True:
            if _is_cancelled(self.cancel):
                raise _ReadCancelled()
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                # A cancel is checked first: the network thread leaves without a terminal item when it is stopped while it waits for data
                if self._stop.is_set() or _is_cancelled(self.cancel):
                    raise _ReadCancelled() from None
                if self._thread is not None and not self._thread.is_alive() and self._queue.empty():
                    raise jobs.JobError(f"{self.name}: the download ended unexpectedly", code="network") from None
                continue
            if item is None:
                self._eof = True
                return
            if isinstance(item, BaseException):
                raise item
            self._buffer += item
            return

    def read(self, size=-1):
        if size is None or size < 0:
            while not self._eof:
                self._fill()
            data, self._buffer = self._buffer, b""
        else:
            while len(self._buffer) < size and not self._eof:
                self._fill()
            data, self._buffer = self._buffer[:size], self._buffer[size:]
        self.position += len(data)
        if self.on_read is not None and data:
            self.on_read(self)
        return data

    def finish(self):
        """Read the rest of the stream (the padding after the tar end marker) so that the MD5 covers the whole file."""
        while not self._eof:
            self.read(1 << 20)
        return self.result()

    def result(self):
        """``{"md5": hex or None, "bytes": position, "complete": bool, "from_offset", "reconnects", "resigns"}``."""
        return {"md5": self.md5 if self._eof else None, "bytes": self.position, "complete": self._eof, "from_offset": self.start, "reconnects": self.reconnects, "resigns": self.resigns}

    def close(self):
        self._stop.set()
        response = self._response
        if response is not None:  # unblocks a network thread that waits for data
            try:
                response.close()
            except Exception:  # noqa: S110 (the connection is being dropped anyway)
                pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()


def download_small_file(resolver, file, path, cancel=None):
    """Download a small file (documents, participants, Lead-DBS metadata) into ``path``; returns ``(bytes, md5 or None)``."""
    source = ArchiveSource(resolver, {**file, "size": None if file.get("tabular") else file.get("size")}, cancel=cancel, prefetch_bytes=4 << 20)
    try:
        try:
            data = source.read()
        except _ReadCancelled:
            raise jobs.Cancelled(reason="cancelled") from None
    finally:
        source.close()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return len(data), source.md5


#
# State of a fetch
#


def _entry_slug(entry):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", entry["id"])


def bundle_path(entry, dest):
    """``<dest>/<entry id>.lnmc``: the bundle a fetch writes."""
    return os.path.join(os.path.abspath(os.fspath(dest)), f"{_entry_slug(entry)}.lnmc")


def lock_path(entry, dest):
    """``<dest>/.<entry id>.lock``: the output lock a running fetch holds (``jobs.OutputLock``)."""
    return os.path.join(os.path.abspath(os.fspath(dest)), f".{_entry_slug(entry)}.lock")


def running_fetch(entry, dest):
    """The lock holder (``{pid, host, started}``) of a fetch of ``entry`` into ``dest`` that a live process is running, or None."""
    lock = jobs.OutputLock(lock_path(entry, dest))
    holder = lock.read()
    return holder if holder is not None and not lock.is_stale(holder) else None


def finished_fetch(entry, dest):
    """The bundle path of a fetch of ``entry`` into ``dest`` that has finished (its state is complete), or None."""
    data = read_state(entry, dest)
    bundle = bundle_path(entry, dest)
    if data and data.get("complete") and os.path.isfile(os.path.join(bundle, "connectome.json")):
        return bundle
    return None


def work_dir(entry, dest):
    """``<dest>/<entry id>.fetch``: the state of a fetch, the accepted terms text, small files and kept archives."""
    return os.path.join(os.path.abspath(os.fspath(dest)), f"{_entry_slug(entry)}.fetch")


def bundle_name(entry):
    """The bundle's display name ("GSP1000", "GSP1000 (Lead-DBS)"); the version is shown next to it from the provenance."""
    base = re.split(r"\s+(?:Preprocessed|\()", entry["title"])[0].strip() or entry["title"]
    return base + (" (Lead-DBS)" if entry.get("converter") == "leaddbs_vol" else "")


class FetchState:
    """``state.json`` of a fetch: per archive its status ("pending", "partial", "done", "mismatch"), the bytes consumed and
    the member offset of the next unconverted subject, the verification and the files.
    """

    def __init__(self, path):
        self.path = os.fspath(path)
        self.data = {}
        self.dirty = 0.0
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        valid = isinstance(data, dict) and data.get("format") == STATE_FORMAT
        self.data = data if valid else {"format": STATE_FORMAT, "version": STATE_VERSION, "archives": {}, "files": {}}
        self.exists = valid
        return self

    def archive(self, name):
        return self.data["archives"].setdefault(name, {"status": "pending", "memberOffset": 0, "bytes": 0, "subjectsDone": 0})

    def save(self, force=True):
        now = time.monotonic()
        if not force and now - self.dirty < STATE_INTERVAL_S:
            return
        self.dirty = now
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        jobs.write_json_atomic(self.path, self.data)


def read_state(entry, dest):
    """The saved state of a fetch of ``entry`` into ``dest`` (a dict), or None when there is none."""
    state = FetchState(os.path.join(work_dir(entry, dest), "state.json"))
    return state.data if state.exists else None


def unfinished_fetch(entry, dest):
    """A summary of an interrupted fetch (``{"done", "total", "fraction", "archives"}``), or None when none is waiting."""
    data = read_state(entry, dest)
    if not data or data.get("complete"):
        return None
    sizes = data.get("sizes") or {}
    total = sum(sizes.values())
    done = sum(sizes.get(name, 0) if info.get("status") == "done" else info.get("bytes", 0) for name, info in (data.get("archives") or {}).items())
    return {"done": done, "total": total, "fraction": (done / total) if total else None, "archives": len(sizes), "subset": data.get("subset")}


#
# The job
#


def _provenance(entry, live, plan, terms_sha, results, full_set, participants, subjects, override):
    source_files = []
    for item in [*plan["archives"], *plan["others"]]:
        info = results.get(item["name"]) or {}
        record = {"name": item["name"], "size": item["size"], "verification": info.get("verification")}
        if item.get("md5"):
            record["md5"] = item["md5"]
        source_files.append(record)
    source = {
        "doi": (entry.get("source") or {}).get("doi"),
        "version": live["version"] or (entry.get("source") or {}).get("version"),
        "url": (entry.get("source") or {}).get("url"),
        "name": entry["title"],
        "files": source_files,
    }
    if not full_set:
        source["subset"] = plan["selection"].get("text") or "partial"
    if override:
        source["checksumOverride"] = True
    population = entry.get("population") or {}
    pre = entry.get("preprocessing") or {}
    provenance = {
        "source": {k: v for k, v in source.items() if v is not None},
        "kind": entry.get("kind"),
        "licence": entry.get("licence"),
        "termsSha256": terms_sha,
        "acknowledgement": entry.get("acknowledgement") or "",
        "population": {k: v for k, v in {"n": subjects, "ageRange": population.get("age_range") if full_set else None, "sexRatio": population.get("sex_ratio") if full_set else None, "scanners": population.get("scanners")}.items() if v is not None},
        "preprocessing": {k: v for k, v in {"gsr": pre.get("gsr", "unknown"), "nuisance": pre.get("nuisance"), "bandpass": pre.get("bandpass_hz"), "smoothingMm": pre.get("smoothing_mm"), "pipelineUrl": pre.get("pipeline_url")}.items() if v is not None},
    }
    if entry.get("tr_s"):
        provenance["trS"] = entry["tr_s"]
    if entry.get("space"):
        provenance["space"] = entry["space"]  # the catalog states the space: the manager, circuits and the study card read it from connectome.json
    if participants:
        provenance["participants"] = participants
    return {k: v for k, v in provenance.items() if v not in (None, {})}


def _copy_documents(plan, files_dir, bundle):
    """Copy the dataset's documents and participants table into the bundle; returns the participants path inside it."""
    participants = None
    os.makedirs(os.path.join(bundle, "docs"), exist_ok=True)
    for item in plan["others"]:
        source = os.path.join(files_dir, item["name"])
        if not os.path.isfile(source):
            continue
        if item.get("role") == "participants" and item["name"].lower().endswith((".tab", ".tsv")):
            name = "participants.tsv" if participants is None else f"participants-{item['name']}"
            shutil.copyfile(source, os.path.join(bundle, name))
            participants = participants or name
        elif item.get("role") in ("docs", "participants"):
            shutil.copyfile(source, os.path.join(bundle, "docs", item["name"]))
    if not os.listdir(os.path.join(bundle, "docs")):
        os.rmdir(os.path.join(bundle, "docs"))
    return participants


class _Progress:
    """Throttled progress events and the ETA of a fetch."""

    def __init__(self, emit, total, done_at_start, clock=time.monotonic):
        self.emit = emit
        self.total = total
        self.done_at_start = done_at_start
        self.clock = clock
        self.started = clock()
        self.last = 0.0
        self.completed = done_at_start  # bytes of the archives that are done
        self.archive = None
        self.index = 0
        self.count = 0
        self.subjects = 0

    def done(self, position):
        return min(self.total, self.completed + position) if self.total else self.completed + position

    def update(self, source=None, force=False):
        now = self.clock()
        if not force and now - self.last < PROGRESS_INTERVAL_S:
            return
        self.last = now
        done = self.done(source.position if source is not None else 0)
        spent = max(now - self.started, 1e-9)
        session = done - self.done_at_start
        rate = session / spent if session > 0 else 0.0
        eta = (self.total - done) / rate if rate > 0 and spent >= 3.0 and self.total else None
        self.emit("progress", done=int(done), total=int(self.total), elapsed=round(spent, 3), eta=None if eta is None else round(eta, 1), archive=self.archive,
                  archive_index=self.index, archive_count=self.count, subjects=self.subjects, bytes_per_s=round(rate), stage="fetch")


def _register_run(root, bundle, params):
    from . import runs

    runs.register_kind(FETCH_KIND)
    try:
        return runs.register(root, FETCH_KIND, bundle, params=params, status="running")
    except (OSError, ValueError) as exc:
        jobs.emit("warning", code="registry", message=f"The run could not be recorded in the registry: {exc}")
        return None


def _update_run(root, run_id, **fields):
    from . import runs

    if run_id is None:
        return
    try:
        runs.update(root, run_id, **fields)
    except (OSError, ValueError):
        pass


def fetch_connectome(entry, dest, terms_sha, subset=None, streams=2, keep_archives=False, cancel=None, resume=False, accept_checksum_mismatch=False,
                     server=None, session=None, emit=None, run_root=None, prefetch_mb=DEFAULT_PREFETCH_MB, resign_after_s=RESIGN_AFTER_S):
    """Fetch ``entry`` (a catalog entry dict) into ``dest`` as a bundle; returns a summary dict (see the module docstring).

    Nothing is downloaded unless ``terms_sha`` equals the sha256 of the provider's live terms text fetched now.

    :param emit: ``emit(event, **fields)`` (default ``jobs.emit``).
    :param cancel: a ``jobs.CancelToken`` or ``threading.Event``; the job stops with ``jobs.Cancelled`` and ``--resume`` continues it.
    :param server: replaces the scheme, host and port of the catalog's API URLs (tests and mirrors).
    """
    emit = emit or jobs.emit
    check_fetchable(entry)
    if not isinstance(terms_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", terms_sha or ""):
        raise jobs.JobError("--terms-sha must be the sha256 of the terms text the user accepted", code="usage", exit_code=jobs.EXIT_USAGE)
    if not 1 <= int(streams) <= MAX_STREAMS:
        raise jobs.JobError(f"--streams must be 1 to {MAX_STREAMS}", code="usage", exit_code=jobs.EXIT_USAGE)
    session = session or fetch.make_session()

    # 1. The live terms: nothing is downloaded unless the accepted text is exactly this one
    live = read_live(entry, session, server)
    if not live["terms"]:
        raise jobs.JobError(f"The dataset description has no terms text at {entry['terms'].get('field')}: nothing is downloaded", code="terms_missing", exit_code=jobs.EXIT_USAGE)
    live_sha = catalogs.terms_sha256(live["terms"])
    if live_sha != terms_sha:
        raise jobs.JobError("The provider's terms text is not the text that was accepted (it has changed or differs): show the live text again and accept it before downloading. "
                            "Nothing was downloaded.", code="terms_mismatch", exit_code=jobs.EXIT_USAGE, live_sha256=live_sha)

    # 2. The live listing against the catalog
    for difference in compare_catalog(entry, live):
        emit("warning", code="catalog_differs", message=f"The live dataset differs from the catalog: {difference}.")
    plan = plan_files(entry, live, subset)
    archives = plan["archives"]
    if not archives:
        raise jobs.JobError(f"{entry['id']} has no archives to fetch", code="listing")
    full_set = len(archives) == len(catalogs.archives(entry))
    plan["selection"]["text"] = None if subset in (None, "", "all") else str(subset)

    dest = os.path.abspath(os.fspath(dest))
    os.makedirs(dest, exist_ok=True)
    bundle = bundle_path(entry, dest)
    work = work_dir(entry, dest)
    locked = lock_path(entry, dest)
    lock = jobs.OutputLock(locked)
    holder = lock.read()
    if holder is not None and not lock.is_stale(holder):  # a live job is fetching it: say so before the "partly downloaded" refusals below
        raise jobs.Locked(f"{locked} is locked by process {holder.get('pid')} on {holder.get('host')} since {holder.get('started')}", holder=holder)
    state = FetchState(os.path.join(work, "state.json"))
    if not resume and (state.exists or os.path.exists(os.path.join(bundle, "connectome.partial.json"))):
        raise jobs.JobError(f"{os.path.basename(bundle)} is partly downloaded here: resume it (--resume) or choose another folder", code="exists")
    if not state.exists and os.path.exists(os.path.join(bundle, "connectome.json")):
        raise jobs.JobError(f"{os.path.basename(bundle)} exists already in this folder: choose another folder", code="exists")

    sizes = {item["name"]: item["size"] for item in archives}
    plan_info = catalogs.disk_plan(entry, subset, keep_archives)
    free = shutil.disk_usage(dest).free
    if plan_info["peak"] and not state.exists and free < plan_info["peak"] * DISK_MARGIN:
        raise jobs.JobError(f"Not enough free space in {dest}: the plan needs about {plan_info['peak']:,} B at the peak and {free:,} B are free", code="disk_space")

    with lock:
        if lock.reclaimed:
            emit("warning", code="lock_reclaimed", message="A lock of an earlier fetch that ended without cleaning up was reclaimed.")
        data = state.data
        data.update({"entry": entry["id"], "subset": plan["selection"]["text"], "catalogVersion": (entry.get("source") or {}).get("version"), "liveVersion": live["version"],
                     "termsSha256": terms_sha, "sizes": sizes, "keepArchives": bool(keep_archives), "complete": False, "created": data.get("created") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        data.setdefault("checksumOverride", False)
        os.makedirs(work, exist_ok=True)
        with open(os.path.join(work, "terms-accepted.txt"), "w", encoding="utf-8", newline="\n") as f:
            f.write(catalogs.normalize_terms_text(live["terms"]))
        state.save()

        run_root = run_root or dest
        run_id = _register_run(run_root, bundle, {"entry": entry["id"], "subset": plan["selection"]["text"], "termsSha256": terms_sha, "streams": int(streams), "keepArchives": bool(keep_archives),
                                                   "doi": (entry.get("source") or {}).get("doi"), "version": live["version"]})
        try:
            with jobs.KeepAwake():
                summary = _run_fetch(entry, live, plan, terms_sha, state, work, bundle, dest, session, server, streams, keep_archives, cancel, accept_checksum_mismatch, emit, prefetch_mb, resign_after_s, full_set)
        except jobs.Cancelled:
            _update_run(run_root, run_id, status="cancelled", ended=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            raise
        except BaseException as exc:
            _update_run(run_root, run_id, status="failed", summary={"error": str(exc)}, ended=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            raise
        _update_run(run_root, run_id, status="done", summary={k: summary[k] for k in ("subjects", "archives", "checksum_override", "unverified_resumed", "reconnects", "resigns", "bytes")}, outputs=[os.path.basename(bundle)],
                    ended=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        summary["run_id"] = run_id
        return summary


def _run_fetch(entry, live, plan, terms_sha, state, work, bundle, dest, session, server, streams, keep_archives, cancel, accept_mismatch, emit, prefetch_mb, resign_after_s, full_set):
    from . import connectome

    data = state.data
    archives = plan["archives"]
    base = _rebase((entry.get("source") or {}).get("api"), server)
    resolver = SignedUrlResolver(session, f"{urllib.parse.urlsplit(base).scheme}://{urllib.parse.urlsplit(base).netloc}", max_age_s=resign_after_s)
    files_dir = os.path.join(work, "files")
    dataset_dir = os.path.join(work, "dataset")
    results = data.setdefault("files", {})
    layout = entry["converter"]

    # Small files first: documents, the participants table and (Lead-DBS) the dataset description that maps runs to subjects
    for item in plan["others"]:
        target_dir = dataset_dir if layout == "leaddbs_vol" and item.get("role") == "metadata" else files_dir
        path = os.path.join(target_dir, item["name"])
        record = results.get(item["name"])
        if record and record.get("md5") == item.get("md5") and os.path.isfile(path):
            continue
        for attempt in (1, 2):
            size, md5 = download_small_file(resolver, item, path, cancel)
            if not item.get("md5") or md5 == item["md5"]:
                verification = VERIFICATION_MD5 if item.get("md5") else VERIFICATION_NOT_PUBLISHED
                break
            if attempt == 2:
                if not accept_mismatch:
                    raise jobs.JobError(f"{item['name']}: the checksum does not match the one the provider lists (twice). Run again with --accept-checksum-mismatch to keep it.", code="checksum", override="--accept-checksum-mismatch", file=item["name"])
                verification = VERIFICATION_OVERRIDE
                data["checksumOverride"] = True
                emit("warning", code="checksum_override", message=f"{item['name']}: the checksum did not match; the file was kept at your request (checksum_override).")
        results[item["name"]] = {"md5": item.get("md5"), "bytes": size, "verification": verification}
        state.save()

    for item in archives:
        info = data["archives"].get(item["name"])
        if info is not None and info.get("md5Published") and item.get("md5") and info["md5Published"] != item["md5"] and info.get("status") != "done":
            emit("warning", code="catalog_differs", message=f"{item['name']} has changed at the provider since it was started; it is read again from its start.")
            info.update({"status": "pending", "memberOffset": 0, "bytes": 0})

    def completed():
        """Bytes of the archives that are converted (the checksum of a "mismatch" archive is pending only)."""
        return sum(item["size"] for item in archives if data["archives"].get(item["name"], {}).get("status") in ("done", "mismatch"))

    total = sum(item["size"] for item in archives)
    done_at_start = completed() + sum(data["archives"].get(item["name"], {}).get("bytes", 0) for item in archives if data["archives"].get(item["name"], {}).get("status") == "partial")
    progress = _Progress(emit, total, done_at_start)
    progress.count = len(archives)
    todo = [i for i, item in enumerate(archives) if data["archives"].get(item["name"], {}).get("status") != "done"]
    totals = {"reconnects": 0, "resigns": 0, "bytes": 0, "unverified_resumed": 0}
    pool = {}

    def make_source(index):
        item = archives[index]
        info = state.archive(item["name"])
        if info.get("status") == "mismatch":
            return None  # converted already; only its checksum is pending (see _verify)
        start = 0 if keep_archives or info.get("status") != "partial" else int(info.get("memberOffset", 0))
        tee = os.path.join(work, "archives", item["name"] + ".part") if keep_archives else None
        return ArchiveSource(resolver, item, start=start, cancel=cancel, prefetch_bytes=int(prefetch_mb) << 20, tee_path=tee, on_read=progress.update).open()

    def stream_md5(item):
        """Read an archive again only to compute its MD5 (the second read after a mismatch)."""
        source = ArchiveSource(resolver, item, cancel=cancel, prefetch_bytes=int(prefetch_mb) << 20)  # no on_read: the archive is counted in the progress already
        try:
            return source.open().finish()
        except _ReadCancelled:
            raise jobs.Cancelled(reason="cancelled") from None
        finally:
            source.close()

    def convert(source, item, info, last):
        """Convert one archive from its stream; returns the stream's result (see ``ArchiveSource.result``)."""
        try:
            for checkpoint in connectome.from_tar_stream(
                source, bundle, layout=layout, name=bundle_name(entry), resume=True, start_offset=source.start, cancel=cancel, finalize=last, description=description,
                citation=entry.get("citation") or "", data_use_terms=f"{entry.get('licence')} (accepted terms text sha256 {terms_sha[:12]}...)",
                dataset_dir=dataset_dir if layout == "leaddbs_vol" else None, grid=_expected_grid(entry),
            ):
                if checkpoint.get("cancelled"):
                    info.update({"status": "partial", "memberOffset": int(checkpoint["member_offset"]), "bytes": source.position})
                    state.save()
                    raise jobs.Cancelled(done=progress.done(source.position), reason="cancelled")
                if checkpoint.get("done"):
                    info["memberOffset"] = int(checkpoint["member_offset"])
                    break
                info.update({"status": "partial", "memberOffset": int(checkpoint["member_offset"]), "bytes": source.position, "subjectsDone": checkpoint.get("subjects_done", 0)})
                progress.subjects = checkpoint.get("subjects_done", 0)
                for message in checkpoint.get("warnings") or []:
                    emit("warning", code="conversion", message=message, archive=item["name"], subject=checkpoint.get("subject"))
                state.save(force=False)
                progress.update(source)
            return source.finish()
        except _ReadCancelled:
            info.update({"status": "partial", "bytes": source.position})
            state.save()
            raise jobs.Cancelled(done=progress.done(source.position), reason="cancelled") from None
        finally:
            source.close()

    description = f"{entry['title']} (doi:{(entry.get('source') or {}).get('doi')}), fetched with Slicer Studio"
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", connectome.ConversionWarning)
            for position, index in enumerate(todo):
                for ahead in todo[position : position + int(streams)]:
                    if ahead not in pool:
                        pool[ahead] = make_source(ahead)
                item = archives[index]
                source = pool.pop(index)
                info = state.archive(item["name"])
                info.update({"md5Published": item.get("md5"), "size": item["size"]})
                progress.archive, progress.index = item["name"], index + 1
                progress.completed = completed()
                if source is not None:
                    progress.update(source, force=True)
                    result = convert(source, item, info, last=index == len(archives) - 1)
                    if keep_archives:
                        final = os.path.join(work, "archives", item["name"])
                        os.replace(final + ".part", final)
                else:
                    result = {"md5": None, "bytes": item["size"], "complete": False, "reconnects": 0, "resigns": 0}
                totals["reconnects"] += result["reconnects"]
                totals["resigns"] += result["resigns"]
                verification = _verify(item, result, info, accept_mismatch, stream_md5, emit, data, state)
                info.update({"status": "done", "bytes": item["size"], "verification": verification, "reconnects": result["reconnects"], "resigns": result["resigns"]})
                if verification == NOT_VERIFIED_RESUMED:
                    totals["unverified_resumed"] += 1
                    emit("warning", code="not_verified", message=f"{item['name']}: {NOT_VERIFIED_RESUMED}.", archive=item["name"])
                results[item["name"]] = {"md5": item.get("md5"), "bytes": item["size"], "verification": verification}
                progress.completed = completed()
                totals["bytes"] += item["size"]
                state.save()
                emit("result", kind="archive", name=item["name"], verification=verification, bytes=item["size"], reconnects=result["reconnects"], resigns=result["resigns"])
                progress.update(force=True)
    finally:
        for source in pool.values():
            if source is not None:
                source.close()
        state.save()

    subjects = len(connectome.ConnectomeBundle.open(bundle).subject_ids)
    participants = _copy_documents(plan, files_dir, bundle)
    shutil.copyfile(os.path.join(work, "terms-accepted.txt"), os.path.join(bundle, "terms-accepted.txt"))
    keys = _provenance(entry, live, plan, terms_sha, results, full_set, participants, subjects, bool(data.get("checksumOverride")))
    connectome.set_metadata(bundle, **keys)
    data.update({"complete": True, "subjects": subjects})
    state.save()
    shutil.rmtree(files_dir, ignore_errors=True)
    emit("result", kind="connectome", path=bundle, subjects=subjects, licence=entry.get("licence"), terms_sha256=terms_sha, checksum_override=bool(data.get("checksumOverride")))
    return {"path": bundle, "subjects": subjects, "archives": len(archives), "checksum_override": bool(data.get("checksumOverride")), "unverified_resumed": totals["unverified_resumed"],
            "reconnects": totals["reconnects"], "resigns": totals["resigns"], "bytes": totals["bytes"], "work_dir": work}


def _expected_grid(entry):
    """The run grid the catalog entry states (a ``geometry.Grid``), or False (any grid) when it states none."""
    from .geometry import GRID_BY_ID

    grid = entry.get("grid")
    return GRID_BY_ID[grid].grid if grid in GRID_BY_ID else False


def _verify(item, result, info, accept_mismatch, stream_md5, emit, data, state):
    """The verification of an archive after it was converted (see the module docstring)."""
    expected = item.get("md5")
    if info.get("status") == "mismatch" and accept_mismatch:
        data["checksumOverride"] = True
        return VERIFICATION_OVERRIDE
    if not expected:
        return VERIFICATION_NOT_PUBLISHED
    md5 = result.get("md5")
    if md5 is None and info.get("status") != "mismatch":
        return NOT_VERIFIED_RESUMED
    if md5 == expected:
        return VERIFICATION_MD5
    emit("warning", code="checksum_retry", message=f"{item['name']}: the MD5 does not match the one the provider lists; reading the archive once more to check it.", archive=item["name"])
    again = stream_md5(item)
    if again["md5"] == expected:
        return VERIFICATION_MD5_SECOND
    if accept_mismatch:
        data["checksumOverride"] = True
        emit("warning", code="checksum_override", message=f"{item['name']}: the checksum did not match twice; the data were kept at your request (checksum_override).", archive=item["name"])
        return VERIFICATION_OVERRIDE
    info["status"] = "mismatch"
    state.save()
    raise jobs.JobError(f"{item['name']}: the MD5 of the downloaded archive ({again['md5']}) is not the one the provider lists ({expected}), also after reading it twice. "
                        "The subjects converted from it are in the bundle (every member passed its gzip CRC). Run again with --accept-checksum-mismatch to keep them and record "
                        "checksum_override, or delete the folder and start again.", code="checksum", override="--accept-checksum-mismatch", file=item["name"])


#
# Command line
#


def _load_entry(args):
    from . import fetch as _fetch

    try:
        catalog = catalogs.load_catalog(args.catalog)
        return catalog.get(args.entry)
    except (_fetch.FetchError, KeyError) as exc:
        raise jobs.JobError(str(getattr(exc, "message", None) or exc), code="usage", exit_code=jobs.EXIT_USAGE) from None


def _common(parser):
    parser.add_argument("--entry", required=True, help="id of the connectome in the catalog (for example gsp1000-ilxiks)")
    parser.add_argument("--catalog", default=catalogs.CONNECTOMES, help="the shipped catalog name or a catalog file")
    parser.add_argument("--server", default=None, help="replace the scheme, host and port of the catalog's API URLs (a mirror; tests)")


def _fetch_main(argv):
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.fetchconnectome", description="Fetch a connectome and convert it into a bundle while it streams (only for the terms text the user accepted).")
    _common(parser)
    parser.add_argument("--dest", required=True, help="folder that receives <entry>.lnmc")
    parser.add_argument("--terms-sha", required=True, help="sha256 of the live terms text the user accepted")
    parser.add_argument("--subset", default=None, help="all, archives:00-01, subjects:N or a named subset of the entry")
    parser.add_argument("--streams", type=jobs.positive_int, default=2, help=f"archives downloaded at the same time (1-{MAX_STREAMS}; default 2)")
    parser.add_argument("--keep-archives", action="store_true", help="also keep the downloaded tar archives (about the size of the download)")
    parser.add_argument("--accept-checksum-mismatch", action="store_true", help="keep an archive whose checksum does not match (recorded as checksum_override)")
    parser.add_argument("--prefetch-mb", type=jobs.positive_int, default=DEFAULT_PREFETCH_MB, help="buffer per stream in MB")
    parser.add_argument("--parent-pid", type=int, default=None, help="stop (cancelled, resumable) when this process is gone (the application that started the job)")
    jobs.add_common_arguments(parser)
    args = parser.parse_args(argv)
    cancel = jobs.CancelToken(args.cancel_file)
    if args.parent_pid:
        jobs.start_watchdogs(cancel, parent_pid=args.parent_pid, orphan=False)
    jobs.emit("start", job="fetchconnectome", argv=argv, threads=args.threads)
    entry = _load_entry(args)
    summary = fetch_connectome(entry, args.dest, args.terms_sha, subset=args.subset, streams=args.streams, keep_archives=args.keep_archives, cancel=cancel, resume=args.resume,
                               accept_checksum_mismatch=args.accept_checksum_mismatch, server=args.server, run_root=args.run_root, prefetch_mb=args.prefetch_mb)
    jobs.emit("done", outputs=[summary["path"]], elapsed=round(jobs.elapsed(), 3), run_id=summary.get("run_id"), checksum_override=summary["checksum_override"])
    return jobs.EXIT_OK


def _terms_main(argv):
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.fetchconnectome terms", description="Show the provider's live terms text of a connectome and its sha256.")
    _common(parser)
    jobs.add_common_arguments(parser)
    args = parser.parse_args(argv)
    jobs.emit("start", job="fetchconnectome-terms", argv=argv)
    entry = _load_entry(args)
    text, sha = live_terms(entry, server=args.server)
    jobs.emit("result", kind="terms", text=text, display=terms_display_text(text), sha256=sha, url=(entry.get("terms") or {}).get("url"), licence=entry.get("licence"), entry=entry["id"])
    jobs.emit("done", outputs=[], elapsed=round(jobs.elapsed(), 3), run_id=None)
    return jobs.EXIT_OK


def _check_main(argv):
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.fetchconnectome check", description="Compare the live listing of a connectome's dataset with the shipped catalog (nothing is downloaded).")
    _common(parser)
    jobs.add_common_arguments(parser)
    args = parser.parse_args(argv)
    jobs.emit("start", job="fetchconnectome-check", argv=argv)
    entry = _load_entry(args)
    live = read_live(entry, server=args.server)
    differences = compare_catalog(entry, live)
    jobs.emit("result", kind="catalog_check", entry=entry["id"], catalog_version=(entry.get("source") or {}).get("version"), live_version=live["version"], differences=differences, current=not differences)
    jobs.emit("done", outputs=[], elapsed=round(jobs.elapsed(), 3), run_id=None)
    return jobs.EXIT_OK


def main(argv=None):
    """Command-line entry point (see the module docstring)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "terms":
        return _terms_main(argv[1:])
    if argv and argv[0] == "check":
        return _check_main(argv[1:])
    return _fetch_main(argv)


if __name__ == "__main__":
    jobs.run_job(main, job="fetchconnectome")

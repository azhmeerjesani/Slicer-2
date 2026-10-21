"""Verified, resumable, offline-tolerant fetch layer for the small reference files of lesion network mapping.

Atlases, templates, tissue priors and template-family transforms are listed in the catalogs
``catalog/atlases.json`` and ``catalog/templates.json`` (format ``slicer-lnm-catalog`` version 1; the
catalogs ship with the app and are never refreshed from the network). Nothing in them is bundled, and
nothing is downloaded unless a caller asks for one entry: connectomes and GSP-derived files are not in
these catalogs (addendum 4.1).

Catalog entry fields (a :class:`Resource` wraps one entry):

- ``id``, ``title``, ``kind`` (atlas | template | prior | transform | labels);
- ``urls`` (tried in order), ``filename``, ``size`` (bytes or null);
- ``checksum``: ``{"algo": "md5" | "sha1" | "sha256", "value": hex}`` or ``"record-on-first-download"``.
  A multipart S3 ETag is never an MD5. ``sha256`` may add a second declared digest;
- ``unzip_member``: the file is a member of the downloaded zip archive (``member_sha256`` checks it);
- ``licence`` (SPDX id or a custom label), ``badge`` (green | amber | grey, see :func:`badge_for`),
  ``terms_url``, ``citation``, ``note``, ``warning``;
- ``space`` and ``family`` (see ``geometry.space_family``), ``grid`` (a ``geometry.GRID_REGISTRY`` id);
- ``label_table``: how the label names are obtained (a file of the archive or another URL, or inline names);
- ``link_out``: the app only links to the source (the file is adopted after a manual download, see :func:`adopt`);
- ``derive``: not downloadable, produced from other entries.

Cache layout (:func:`cache_dir`): ``<cache>/files/<id of the URL>/<filename>`` (entries that share a URL share the
file), archive members under ``.../members/``, and ``<cache>/manifest.json`` with the size, sha256 and time of
every file. Entries marked ``record-on-first-download`` get their sha256 recorded there at the first download
and are verified against it on reuse. Cached files of at most 32 MB are hashed again on every reuse; larger
ones are accepted while their size and modification time match the manifest.

:func:`fetch` uses ``requests`` (bundled with Slicer's Python; imported on first use) with the CA bundle of the
environment (``REQUESTS_CA_BUNDLE`` or ``SSL_CERT_FILE``, else certifi's), timeouts and the proxy variables of
the environment. An interrupted download keeps its ``.part`` file and resumes with a Range request. A checksum
mismatch is retried once from scratch, then raises :class:`ChecksumError` and leaves the file next to its
destination as ``.unverified`` so that a caller can offer an override (:func:`accept_unverified`, recorded as
``checksum_override`` in the manifest). Offline (no connection, or ``$SLICER_LNM_OFFLINE`` set), a cached file is
returned and anything else raises :class:`OfflineError`.

Command line (one JSON event per line, see ``jobs``; the GUI runs it as a light job)::

    python -m LesionNetworkMappingLib.fetch get --catalog atlases|templates|FILE --id ID [--cache DIR]
        [--offline] [--accept-unverified] [--retries N] [--no-labels]
    python -m LesionNetworkMappingLib.fetch list --catalog atlases|templates|FILE [--cache DIR]
    python -m LesionNetworkMappingLib.fetch adopt --catalog atlases|templates|FILE --id ID --file PATH
        [--labels-file PATH] [--cache DIR]
"""

import hashlib
import json
import os
import re
import shutil
import threading
import time
import zipfile

CATALOG_FORMAT = "slicer-lnm-catalog"
CATALOG_VERSION = 1
CATALOG_NAMES = ("atlases", "templates")
KINDS = ("atlas", "template", "prior", "transform", "labels")
BADGES = ("green", "amber", "grey")
ALGORITHMS = ("md5", "sha1", "sha256")
RECORD_ON_FIRST_DOWNLOAD = "record-on-first-download"
MANIFEST_FORMAT = "slicer-lnm-fetch-manifest"
MANIFEST_NAME = "manifest.json"
CACHE_ENV = "SLICER_LNM_CACHE"
OFFLINE_ENV = "SLICER_LNM_OFFLINE"
CATALOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog")

CHUNK_BYTES = 64 * 1024
HASH_CHUNK_BYTES = 1024 * 1024
FAST_VERIFY_BYTES = 32 * 1024 * 1024  # larger cached files are trusted while their size and mtime match the manifest
CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 30.0
DEFAULT_RETRIES = 3  # attempts after the first for a dropped connection or a 5xx answer
RETRY_DELAYS_S = (1.0, 3.0, 8.0)  # waits before the retries (the last one repeats)
PROGRESS_INTERVAL_S = 0.5

GREEN_LICENCES = ("CC0-1.0", "CC-BY-3.0", "CC-BY-4.0", "MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "Melbourne-MSA", "MNI-McGill-permissive")
GREY_LICENCES = ("", "unknown", "not-stated")


# Errors


class FetchError(Exception):
    """A resource could not be obtained; ``code`` names the case ("http", "offline", "checksum", "link_out", ...)."""

    code = "fetch"

    def __init__(self, message, code=None, **fields):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.fields = fields


class OfflineError(FetchError):
    """The network is not reachable (or offline mode is on) and the resource is not cached."""

    code = "offline"


class NotCachedError(OfflineError):
    """The resource is not cached and the caller asked not to download (``cached_path`` found nothing)."""

    code = "not_cached"


class ChecksumError(FetchError):
    """The downloaded file does not match its checksum, twice. ``path`` is the unverified file (``.unverified``)."""

    code = "checksum"

    def __init__(self, message, resource_id=None, algo=None, expected=None, actual=None, path=None, url=None):
        super().__init__(message, resource_id=resource_id, algo=algo, expected=expected, actual=actual, path=path, url=url)
        self.resource_id = resource_id
        self.algo = algo
        self.expected = expected
        self.actual = actual
        self.path = path
        self.url = url


class NotDownloadableError(FetchError):
    """The entry is a link-out or derived: there is nothing to download (``code`` "link_out" or "derive")."""

    code = "link_out"


class UnknownResourceError(FetchError, KeyError):
    """No entry has this id in the catalog."""

    code = "unknown_id"

    def __str__(self):
        return self.message


class CatalogError(FetchError):
    """A catalog file is not a valid slicer-lnm-catalog v1 file."""

    code = "catalog"


# Licence badges


def badge_for(licence):
    """Badge colour of a licence label (addendum 4.2): green (CC0, CC BY, MIT, Apache, BSD and the permissive
    Melbourne and McGill texts), grey (terms not stated: "unknown"), amber for everything else (share-alike,
    non-commercial, registration, FSL terms, derived data, custom terms).
    """
    text = str(licence or "").strip()
    if text.lower() in GREY_LICENCES:
        return "grey"
    if text in GREEN_LICENCES:
        return "green"
    return "amber"


# Cache folder


def cache_dir(explicit=None):
    """Cache folder: ``explicit``, else ``$SLICER_LNM_CACHE``, else ``~/.cache/SlicerStudio/LNM``.

    The application passes ``slicer.app.cachePath/LNM``. The folder is created when a file is written.
    """
    path = explicit or os.environ.get(CACHE_ENV) or os.path.join(os.path.expanduser("~"), ".cache", "SlicerStudio", "LNM")
    return os.path.abspath(os.path.expanduser(os.fspath(path)))


def manifest_path(cache=None):
    return os.path.join(cache_dir(cache), MANIFEST_NAME)


def offline_requested(offline=None):
    """True when offline mode is on: ``offline`` if given, else a non-empty, non-"0" ``$SLICER_LNM_OFFLINE``."""
    if offline is not None:
        return bool(offline)
    return os.environ.get(OFFLINE_ENV, "").strip().lower() not in ("", "0", "false", "no")


# Catalog


class Resource:
    """One catalog entry. ``resource["field"]`` and ``resource.get("field")`` read the entry's JSON fields."""

    def __init__(self, entry, catalog=None):
        if isinstance(entry, Resource):
            entry = entry.data
        self.data = dict(entry)
        self.catalog = catalog

    @classmethod
    def coerce(cls, value):
        return value if isinstance(value, cls) else cls(value)

    def __getitem__(self, key):
        return self.data[key]

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __contains__(self, key):
        return key in self.data

    def __repr__(self):
        return f"Resource({self.data.get('id')!r})"

    @property
    def id(self):
        return self.data["id"]

    @property
    def title(self):
        return self.data.get("title", self.id)

    @property
    def kind(self):
        return self.data.get("kind", "atlas")

    @property
    def urls(self):
        return list(self.data.get("urls") or [])

    @property
    def filename(self):
        return self.data.get("filename") or (os.path.basename(self.urls[0].split("?")[0]) if self.urls else self.id)

    @property
    def size(self):
        return self.data.get("size")

    @property
    def member(self):
        return self.data.get("unzip_member")

    @property
    def link_out(self):
        return bool(self.data.get("link_out"))

    @property
    def derive(self):
        return self.data.get("derive")

    @property
    def licence(self):
        return self.data.get("licence", "unknown")

    @property
    def badge(self):
        return self.data.get("badge") or badge_for(self.licence)

    @property
    def record_on_first_download(self):
        return self.data.get("checksum") in (None, RECORD_ON_FIRST_DOWNLOAD)

    @property
    def checksum(self):
        """(algo, value) of the declared checksum, or None for record-on-first-download."""
        declared = self.data.get("checksum")
        if isinstance(declared, dict):
            return str(declared["algo"]).lower(), str(declared["value"]).lower()
        return None

    @property
    def sha256(self):
        """The sha256 of the downloaded file when it is declared (as the checksum or as a second digest)."""
        declared = self.checksum
        if declared and declared[0] == "sha256":
            return declared[1]
        value = self.data.get("sha256")
        return str(value).lower() if value else None

    def label_resource(self):
        """The label table as a Resource (its own file, or a member of the same archive), or None (inline or none)."""
        table = self.data.get("label_table")
        if not table or table.get("format") == "inline" or not (table.get("filename") or table.get("unzip_member") or table.get("urls")):
            return None
        data = {
            "id": f"{self.id}/labels",
            "title": f"Labels of {self.title}",
            "kind": "labels",
            "urls": table.get("urls") or self.data.get("urls") or [],
            "filename": table.get("filename") or (self.data.get("filename") if table.get("unzip_member") else None),
            "size": table.get("size") if table.get("filename") or table.get("urls") else self.data.get("size"),
            "checksum": table.get("checksum", self.data.get("checksum") if table.get("unzip_member") and not table.get("urls") else RECORD_ON_FIRST_DOWNLOAD),
            "unzip_member": table.get("unzip_member"),
            "member_sha256": table.get("member_sha256"),
            "sha256": table.get("sha256") if table.get("urls") or table.get("filename") else self.data.get("sha256"),
            "link_out": self.data.get("link_out", False),
            "licence": self.licence,
            "badge": self.badge,
            "terms_url": self.data.get("terms_url"),
            "citation": self.data.get("citation"),
        }
        if data["filename"] is None:
            data.pop("filename")
        return Resource({k: v for k, v in data.items() if v is not None}, self.catalog)


class Catalog:
    """A loaded catalog file: ``entries`` (Resources in file order) and ``get(id)``."""

    def __init__(self, data, path=None):
        if not isinstance(data, dict) or data.get("format") != CATALOG_FORMAT or data.get("version") != CATALOG_VERSION:
            raise CatalogError(f"Not a {CATALOG_FORMAT} version {CATALOG_VERSION} catalog: {path}")
        entries = data.get("entries")
        if not isinstance(entries, list):
            raise CatalogError(f"The catalog has no entries list: {path}")
        self.path = path
        self.data = data
        self.name = data.get("catalog") or (os.path.splitext(os.path.basename(path))[0] if path else "")
        self.entries = [Resource(entry, self) for entry in entries]
        self._by_id = {}
        for resource in self.entries:
            if "id" not in resource.data:
                raise CatalogError(f"A catalog entry has no id: {path}")
            if resource.id in self._by_id:
                raise CatalogError(f"Duplicate catalog id {resource.id!r}: {path}")
            self._by_id[resource.id] = resource

    def get(self, resource_id):
        try:
            return self._by_id[resource_id]
        except KeyError:
            raise UnknownResourceError(f"No entry {resource_id!r} in the {self.name} catalog", resource_id=resource_id) from None

    def __contains__(self, resource_id):
        return resource_id in self._by_id

    def __iter__(self):
        return iter(self.entries)

    def __len__(self):
        return len(self.entries)

    def ids(self):
        return list(self._by_id)


_catalog_cache = {}


def load_catalog(name_or_path):
    """Load a shipped catalog by name ("atlases", "templates") or any catalog file by path (cached per file and mtime)."""
    text = os.fspath(name_or_path)
    path = os.path.join(CATALOG_DIR, f"{text}.json") if text in CATALOG_NAMES else text
    try:
        stamp = (os.path.abspath(path), os.stat(path).st_mtime_ns)
    except OSError as exc:
        raise CatalogError(f"Cannot read the catalog {text!r}: {exc}") from None
    if stamp not in _catalog_cache:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            raise CatalogError(f"Cannot read the catalog {text!r}: {exc}") from None
        _catalog_cache[stamp] = Catalog(data, path)
    return _catalog_cache[stamp]


def find_resource(resource_id, catalogs=CATALOG_NAMES):
    """The Resource with this id in the first of ``catalogs`` that has it (default: atlases, then templates)."""
    for name in catalogs:
        catalog = load_catalog(name)
        if resource_id in catalog:
            return catalog.get(resource_id)
    raise UnknownResourceError(f"No catalog entry {resource_id!r}", resource_id=resource_id)


def validate_entry(entry):
    """Problems (list of strings) of one catalog entry; an empty list when it is complete."""
    resource = Resource.coerce(entry)
    data = resource.data
    problems = []
    for key in ("id", "title", "kind", "licence", "badge", "citation", "space"):
        if not data.get(key):
            problems.append(f"missing {key}")
    if data.get("kind") not in KINDS:
        problems.append(f"unknown kind {data.get('kind')!r}")
    if data.get("badge") not in BADGES:
        problems.append(f"unknown badge {data.get('badge')!r}")
    elif data.get("licence") is not None and data["badge"] != badge_for(data["licence"]):
        problems.append(f"badge {data['badge']} does not match licence {data['licence']}")
    if not (resource.derive or resource.urls):
        problems.append("no urls")
    if any(not str(url).startswith(("https://", "http://")) for url in resource.urls):
        problems.append("urls must be http(s)")
    if resource.derive and resource.urls:
        problems.append("a derived entry has no urls")
    declared = data.get("checksum")
    if isinstance(declared, dict):
        if str(declared.get("algo", "")).lower() not in ALGORITHMS:
            problems.append(f"unsupported checksum algorithm {declared.get('algo')!r}")
        if not re.fullmatch(r"[0-9a-f]+", str(declared.get("value", "")).lower()):
            problems.append("checksum value is not hexadecimal")
    elif declared not in (None, RECORD_ON_FIRST_DOWNLOAD) and not resource.derive:
        problems.append(f"checksum must be an object or {RECORD_ON_FIRST_DOWNLOAD!r}")
    if not resource.derive and "checksum" not in data:
        problems.append("missing checksum")
    return problems


# Files, manifest and verification


def _hash_file(path, algos):
    """Digests of a file for the given algorithm names, read in 1 MB chunks."""
    digests = {algo: hashlib.new(algo) for algo in algos}
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(HASH_CHUNK_BYTES), b""):
            for digest in digests.values():
                digest.update(chunk)
    return {algo: digest.hexdigest() for algo, digest in digests.items()}


def file_digest(path, algo="sha256"):
    """Hex digest of a file."""
    return _hash_file(path, (algo,))[algo]


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _write_json_atomic(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=1, sort_keys=True)
            f.write("\n")
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def read_manifest(cache=None):
    """The cache manifest ({"format", "version", "files": {relative path: record}}); an unreadable one is set aside."""
    path = manifest_path(cache)
    empty = {"format": MANIFEST_FORMAT, "version": 1, "files": {}}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return empty
    except (OSError, ValueError):
        try:
            os.replace(path, f"{path}.bad")
        except OSError:
            pass
        return empty
    if not isinstance(data, dict) or data.get("format") != MANIFEST_FORMAT or not isinstance(data.get("files"), dict):
        return empty
    return data


_MANIFEST_LOCK_WAIT_S = 10.0
_MANIFEST_LOCK_STALE_S = 30.0
_manifest_thread_lock = threading.RLock()


class _ManifestLock:
    """Exclusive use of the manifest for one read-modify-write (threads of this process, and other processes).

    An ``O_EXCL`` lock file with a short retry; a lock file older than a few seconds is taken over, because a
    read-modify-write of this small file takes milliseconds (so a holder that was killed cannot block for long).
    """

    def __init__(self, cache):
        self.path = f"{manifest_path(cache)}.lock"

    def __enter__(self):
        _manifest_thread_lock.acquire()
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            deadline = time.monotonic() + _MANIFEST_LOCK_WAIT_S
            while True:
                try:
                    os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                    return self
                except FileExistsError:
                    try:
                        if time.time() - os.stat(self.path).st_mtime > _MANIFEST_LOCK_STALE_S:
                            os.remove(self.path)
                            continue
                    except OSError:
                        continue
                    if time.monotonic() > deadline:
                        raise FetchError(f"Cannot lock the cache manifest ({self.path})", code="locked") from None
                    time.sleep(0.01)
        except BaseException:
            _manifest_thread_lock.release()
            raise

    def __exit__(self, *exc):
        try:
            os.remove(self.path)
        except OSError:
            pass
        _manifest_thread_lock.release()


def _record_file(cache, relpath, record):
    with _ManifestLock(cache):
        manifest = read_manifest(cache)  # read inside the lock: other jobs add records too
        manifest["files"][relpath] = record
        _write_json_atomic(manifest_path(cache), manifest)


def _drop_record(cache, relpath):
    with _ManifestLock(cache):
        manifest = read_manifest(cache)
        if manifest["files"].pop(relpath, None) is not None:
            _write_json_atomic(manifest_path(cache), manifest)


def _relpath(cache, path):
    return os.path.relpath(path, cache_dir(cache)).replace(os.sep, "/")


def resource_dir(resource, cache=None):
    """Folder of a resource's files: entries with the same first URL share it (and so their downloaded file)."""
    resource = Resource.coerce(resource)
    key = resource.urls[0] if resource.urls else resource.id
    return os.path.join(cache_dir(cache), "files", hashlib.sha1(key.encode("utf-8")).hexdigest()[:10])


def download_path(resource, cache=None):
    """Where the downloaded file (the archive, for ``unzip_member`` entries) is stored."""
    resource = Resource.coerce(resource)
    return os.path.join(resource_dir(resource, cache), resource.filename)


def primary_path(resource, cache=None):
    """Where :func:`fetch` leaves the file the entry stands for (the extracted member, or the downloaded file)."""
    resource = Resource.coerce(resource)
    if resource.member:
        return os.path.join(resource_dir(resource, cache), "members", os.path.basename(resource.member))
    return download_path(resource, cache)


def _stat_matches(path, record):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size == record.get("size") and st.st_mtime_ns == record.get("mtime_ns")


def _file_ok(resource, cache, path, member=False):
    """True when ``path`` is a verified cached copy of the resource's file (or of its member)."""
    record = read_manifest(cache)["files"].get(_relpath(cache, path))
    if not os.path.isfile(path):
        return False
    declared = None if member else resource.checksum
    expected_member = resource.get("member_sha256") if member else None
    if record is None:
        if declared is None:
            return False  # nothing to verify a file of unknown origin against
        return _hash_file(path, (declared[0],))[declared[0]] == declared[1]
    stat_ok = _stat_matches(path, record)
    if stat_ok and record["size"] > FAST_VERIFY_BYTES and _record_agrees(record, resource, declared, expected_member):
        return True
    if declared and not record.get("override") and not _record_agrees(record, resource, declared, expected_member):
        algo, value = declared
        return _hash_file(path, (algo,))[algo] == value
    sha = file_digest(path, "sha256")
    if sha != record.get("sha256"):
        return False
    if expected_member and not record.get("override") and sha != expected_member:
        return False
    if not stat_ok:
        record = dict(record, mtime_ns=os.stat(path).st_mtime_ns, size=os.path.getsize(path))
        try:
            _record_file(cache, _relpath(cache, path), record)
        except OSError:
            pass
    return True


def _record_agrees(record, resource, declared, expected_member):
    """True when the manifest record was made for the checksum the catalog declares now."""
    if record.get("override"):
        return True
    if expected_member:
        return record.get("sha256") == expected_member
    if declared is None:
        return True
    algo, value = declared
    if algo == "sha256":
        return record.get("sha256") == value
    return (record.get("checksum") or {}).get("algo") == algo and (record.get("checksum") or {}).get("value") == value


def cached_path(resource, cache=None):
    """Path of the entry's file when it is cached and verified, else None. Never touches the network."""
    resource = Resource.coerce(resource)
    path = primary_path(resource, cache)
    try:
        return path if _file_ok(resource, cache, path, member=bool(resource.member)) else None
    except OSError:
        return None


def label_table_path(resource, cache=None):
    """Path of the cached label table of an entry, or None (not cached, or the names are inline)."""
    labels = Resource.coerce(resource).label_resource()
    return None if labels is None else cached_path(labels, cache)


def is_cached(resource, cache=None):
    return cached_path(resource, cache) is not None


# Downloading


class _PartLock:
    """Exclusive use of a destination while it is downloaded (a lock file holding the process id)."""

    def __init__(self, dest):
        self.path = f"{dest}.lock"

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._holder_alive():
                    raise FetchError(f"Another process is downloading {os.path.basename(self.path[:-5])}", code="locked") from None
                try:
                    os.remove(self.path)
                except OSError:
                    pass
                continue
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            return self
        raise FetchError(f"Cannot lock {self.path}", code="locked")

    def _holder_alive(self):
        try:
            with open(self.path) as f:
                pid = int(f.read().strip() or 0)
        except (OSError, ValueError):
            return False
        if pid == os.getpid():
            return True
        from .jobs import pid_alive

        return pid > 0 and pid_alive(pid)

    def __exit__(self, *exc):
        try:
            os.remove(self.path)
        except OSError:
            pass


class _Transient(Exception):
    """A download attempt failed in a way that may pass (dropped connection, 5xx); ``offline`` for no connection."""

    def __init__(self, message, offline=False):
        super().__init__(message)
        self.offline = offline


class _Restart(Exception):
    """The partial file does not fit the remote file: it was removed and the download starts again (no retry used)."""


def _is_cancelled(cancel):
    if cancel is None:
        return False
    for name in ("is_set", "cancelled"):
        check = getattr(cancel, name, None)
        if callable(check):
            return bool(check())
    return bool(cancel()) if callable(cancel) else False


def _raise_if_cancelled(cancel, done):
    if _is_cancelled(cancel):
        from .jobs import Cancelled

        raise Cancelled(done=done, reason=getattr(cancel, "reason", None))


def ca_bundle():
    """CA bundle for HTTPS: ``$REQUESTS_CA_BUNDLE`` or ``$SSL_CERT_FILE`` when set, else certifi's."""
    for name in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"):
        path = os.environ.get(name)
        if path and os.path.exists(path):
            return path
    try:
        import certifi

        return certifi.where()
    except ImportError:
        return True


def _user_agent():
    try:
        from .export import LIBRARY_VERSION

        return f"SlicerStudio-LNM/{LIBRARY_VERSION}"
    except Exception:
        return "SlicerStudio-LNM"


def make_session():
    """A requests session with the environment's proxy settings, the CA bundle and the app's user agent."""
    import requests

    session = requests.Session()
    session.trust_env = True
    session.verify = ca_bundle()
    session.headers["User-Agent"] = _user_agent()
    session.headers["Accept-Encoding"] = "identity"  # byte counts and Range offsets are those of the stored file
    return session


def _content_range(header):
    """(start, total) of a "bytes START-END/TOTAL" or "bytes */TOTAL" header; None where unknown."""
    match = re.match(r"\s*bytes\s+(?:(\d+)-\d+|\*)/(\d+|\*)", header or "")
    if not match:
        return None, None
    start = int(match.group(1)) if match.group(1) is not None else None
    total = int(match.group(2)) if match.group(2) != "*" else None
    return start, total


def _stream(session, url, part, size_hint, progress, cancel):
    """One attempt: download ``url`` into ``part``, resuming from its current size. Returns normally when the file is complete."""
    import requests

    start = os.path.getsize(part) if os.path.exists(part) else 0
    headers = {"Range": f"bytes={start}-"} if start else {}
    try:
        response = session.get(url, headers=headers, stream=True, timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S), allow_redirects=True)
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise _Transient(f"cannot connect to {url}: {exc}", offline=True) from None
    except requests.RequestException as exc:
        raise FetchError(f"Request to {url} failed: {exc}", code="http") from None
    with response:
        status = response.status_code
        if status == 416 and start:
            _, total = _content_range(response.headers.get("Content-Range"))
            if total is not None and total == start:
                return  # the .part file was complete already
            os.remove(part)  # the file changed or the .part file is longer than it: start again
            raise _Restart()
        if status >= 500 or status in (408, 429):
            raise _Transient(f"HTTP {status} from {url}")
        if status >= 400:
            raise FetchError(f"HTTP {status} for {url}", code="http", status=status, url=url)
        if status == 206:
            first, total = _content_range(response.headers.get("Content-Range"))
            if start and first != start:
                os.remove(part)
                raise _Restart()
        elif start:
            start = 0  # the server ignored the Range header: it sends the whole file
        length = response.headers.get("Content-Length")
        expected_total = (int(length) + start) if length and length.isdigit() else None
        total = expected_total if expected_total is not None else size_hint
        done = start
        mode = "ab" if start and status == 206 else "wb"
        _raise_if_cancelled(cancel, done)
        try:
            with open(part, mode) as f:
                for chunk in response.iter_content(CHUNK_BYTES):
                    if chunk:
                        f.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
                    _raise_if_cancelled(cancel, done)
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError, requests.exceptions.ContentDecodingError) as exc:
            raise _Transient(f"the connection dropped after {done} bytes: {exc}", offline=done == start) from None
        if expected_total is not None and done != expected_total:
            raise _Transient(f"the connection closed after {done} of {expected_total} bytes")


def _download(resource, dest, progress, cancel, session, retries):
    """Download the first working URL of a resource into ``dest + ".part"``, resuming and retrying; returns the URL used."""
    part = f"{dest}.part"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    session = session or make_session()
    last = None
    for url in resource.urls:
        attempt = 0
        restarts = 0
        while True:
            try:
                _stream(session, url, part, resource.size, progress, cancel)
                return url
            except _Restart:
                restarts += 1
                if restarts > 3:
                    last = FetchError(f"The remote file of {resource.title} keeps changing while it is downloaded.", code="http", resource_id=resource.id)
                    break
            except _Transient as exc:
                last = exc
                if attempt >= retries:
                    break
                delay = RETRY_DELAYS_S[min(attempt, len(RETRY_DELAYS_S) - 1)]
                attempt += 1
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline:
                    _raise_if_cancelled(cancel, os.path.getsize(part) if os.path.exists(part) else 0)
                    time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
            except FetchError as exc:
                last = exc
                break
    if isinstance(last, FetchError):
        raise last
    if last is not None and last.offline:
        raise OfflineError(f"No network connection to download {resource.title}: {last}. A partial download is kept and resumes next time.", resource_id=resource.id)
    raise FetchError(f"Download of {resource.title} was interrupted: {last}. The partial file is kept and resumes next time.", code="interrupted", resource_id=resource.id)


def _record_for(resource, path, url, sha256, recorded, override=False, archive=None):
    declared = resource.checksum
    record = {
        "id": resource.id,
        "url": url,
        "size": os.path.getsize(path),
        "sha256": sha256,
        "checksum": {"algo": declared[0], "value": declared[1]} if declared else None,
        "recorded_on_first_download": bool(recorded),
        "checksum_override": bool(override),
        "mtime_ns": os.stat(path).st_mtime_ns,
        "fetched": _now(),
    }
    if override:
        record["override"] = True
    if archive:
        record["archive"] = archive
    return record


def _content_problem(resource, path):
    """Why a download of a file without a published checksum cannot be what the entry stands for, else None.

    What such a download records becomes the trusted checksum, so a captive-portal or proxy page served with HTTP 200
    must not get in: the start of the file is checked against the format the file name promises.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(512)
    except OSError:
        return None
    name = resource.filename.lower()
    if head.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html", b"<head", b"<?xml")):
        return "a web page (HTML) instead of the data file"
    if name.endswith((".gz", ".mgz")) and head[:2] != b"\x1f\x8b":
        return "something other than a gzip-compressed file"
    if name.endswith(".zip") and head[:2] != b"PK":
        return "something other than a zip archive"
    if name.endswith(".nii") and len(head) >= 4 and int.from_bytes(head[:4], "little") not in (348, 540) and int.from_bytes(head[:4], "big") not in (348, 540):
        return "something other than a NIfTI image"
    return None


def _verify_download(resource, part, cache, relpath):
    """(ok, expected, actual, algo) of a finished download: the declared checksum(s), else the sha256 recorded earlier."""
    declared = resource.checksum
    algos = {"sha256"}
    if declared:
        algos.add(declared[0])
    digests = _hash_file(part, sorted(algos))
    if declared:
        algo, value = declared
        if digests[algo] != value:
            return False, value, digests[algo], algo
        extra = resource.get("sha256")
        if extra and algo != "sha256" and digests["sha256"] != str(extra).lower():
            return False, str(extra).lower(), digests["sha256"], "sha256"
        return True, value, digests[algo], algo
    previous = read_manifest(cache)["files"].get(relpath)
    if previous and previous.get("sha256"):
        return digests["sha256"] == previous["sha256"], previous["sha256"], digests["sha256"], "sha256"
    return True, None, digests["sha256"], "sha256"


def _ensure_file(resource, cache, progress, cancel, offline, retries, session):
    """The downloaded file of a resource (the archive for members): the verified cached copy or a fresh download."""
    dest = download_path(resource, cache)
    relpath = _relpath(cache, dest)
    if _file_ok(resource, cache, dest):
        return dest
    if offline_requested(offline):
        raise OfflineError(f"{resource.title} is not cached and the network is switched off.", resource_id=resource.id)
    with _PartLock(dest):
        if _file_ok(resource, cache, dest):  # another process finished it meanwhile
            return dest
        part = f"{dest}.part"
        unverified = f"{dest}.unverified"
        for round_ in (1, 2):
            url = _download(resource, dest, progress, cancel, session, retries)
            problem = None if resource.checksum else _content_problem(resource, part)
            if problem:
                os.remove(part)
                raise FetchError(
                    f"{resource.title}: the server at {url} sent {problem}. A proxy or a login page may be in the way; nothing was kept.",
                    resource_id=resource.id,
                    code="content",
                    url=url,
                )
            ok, expected, actual, algo = _verify_download(resource, part, cache, relpath)
            if ok:
                break
            if round_ == 1:
                os.remove(part)  # a mismatch may come from a resumed or truncated file: one clean retry
                continue
            os.replace(part, unverified)  # kept, so that a caller can offer to accept it (accept_unverified)
            raise ChecksumError(
                f"{resource.title} does not match its {algo} checksum (expected {expected}, got {actual}). It was downloaded twice; the file may have changed at the source.",
                resource_id=resource.id,
                algo=algo,
                expected=expected,
                actual=actual,
                path=unverified,
                url=url,
            )
        if os.path.exists(unverified):
            os.remove(unverified)
        previous = read_manifest(cache)["files"].get(relpath)
        recorded = bool(previous.get("recorded_on_first_download")) if previous else resource.checksum is None
        os.replace(part, dest)
        _record_file(cache, relpath, _record_for(resource, dest, url, actual if algo == "sha256" else file_digest(dest, "sha256"), recorded))
    return dest


def _find_member(names, member):
    """The archive name of ``member``: the exact name, else the only name that ends with ``/member``."""
    if member in names:
        return member
    matches = [name for name in names if name.endswith("/" + member) and not name.startswith("__MACOSX/")]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise FetchError(f"The archive has no member {member!r}", code="member")
    raise FetchError(f"The member {member!r} is ambiguous in the archive: {', '.join(matches[:3])}", code="member")


def _extract(resource, cache, archive, override=False):
    """Extract the entry's member from the verified archive into ``members/``, checked and recorded."""
    dest = primary_path(resource, cache)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = f"{dest}.part"
    digest = hashlib.sha256()
    try:
        with zipfile.ZipFile(archive) as zf:
            name = _find_member(zf.namelist(), resource.member)
            with zf.open(name) as source, open(part, "wb") as target:
                for chunk in iter(lambda: source.read(HASH_CHUNK_BYTES), b""):
                    target.write(chunk)
                    digest.update(chunk)
    except zipfile.BadZipFile as exc:
        os.remove(archive)
        _drop_record(cache, _relpath(cache, archive))
        raise FetchError(f"The archive of {resource.title} is damaged ({exc}); it was removed and downloads again next time.", code="archive") from None
    sha = digest.hexdigest()
    expected = resource.get("member_sha256")
    if expected and sha != str(expected).lower() and not override:
        os.remove(part)
        raise ChecksumError(
            f"The member {resource.member} of the archive does not match its sha256 (expected {expected}, got {sha}).",
            resource_id=resource.id,
            algo="sha256",
            expected=expected,
            actual=sha,
            path=None,
        )
    os.replace(part, dest)
    record = _record_for(resource, dest, resource.urls[0] if resource.urls else None, sha, recorded=not expected, override=override and bool(expected) and sha != str(expected).lower(), archive=_relpath(cache, archive))
    record["checksum"] = None
    _record_file(cache, _relpath(cache, dest), record)
    return dest


def fetch(resource, cache=None, progress=None, cancel=None, offline=None, retries=DEFAULT_RETRIES, session=None):
    """Return the path of a resource's file, downloading it first when it is not cached.

    :param resource: a :class:`Resource` or a catalog entry dictionary.
    :param cache: cache folder (see :func:`cache_dir`).
    :param progress: ``progress(done_bytes, total_bytes_or_None)``, called with every received chunk.
    :param cancel: object with ``is_set()`` (or a callable) that turns true to stop; ``jobs.Cancelled`` is raised
      and the ``.part`` file is kept for the next call.
    :param offline: True to forbid the network (default: ``$SLICER_LNM_OFFLINE``).
    :param retries: retries after a dropped connection or a 5xx answer (waits of 1, 3 and 8 s).
    :raises ChecksumError: the file does not match its checksum, also on a second download.
    :raises OfflineError: not cached and the network is unreachable or switched off.
    :raises NotDownloadableError: a link-out or derived entry that is not cached (adopt the file after a manual download).
    :raises FetchError: HTTP errors and damaged archives (``code`` tells which).
    """
    resource = Resource.coerce(resource)
    path = cached_path(resource, cache)
    if path:
        return path
    if resource.derive:
        raise NotDownloadableError(f"{resource.title} is derived from other files and is not downloaded.", code="derive", resource_id=resource.id)
    if resource.link_out or not resource.urls:
        raise NotDownloadableError(
            f"{resource.title} is not downloaded by the app: get it from {(resource.urls or [resource.get('terms_url')])[0]} in a browser, then adopt the file.",
            code="link_out",
            resource_id=resource.id,
        )
    archive = _ensure_file(resource, cache, progress, cancel, offline, retries, session)
    return _extract(resource, cache, archive) if resource.member else archive


def accept_unverified(resource, cache=None):
    """Keep the file of a :class:`ChecksumError` anyway (the user's override): it moves into place and the manifest
    records ``checksum_override: true``. Returns the path of the entry's file.
    """
    resource = Resource.coerce(resource)
    dest = download_path(resource, cache)
    unverified = f"{dest}.unverified"
    if not os.path.isfile(unverified):
        raise FetchError(f"There is no unverified download of {resource.title} to accept.", code="missing", resource_id=resource.id)
    os.replace(unverified, dest)
    record = _record_for(resource, dest, resource.urls[0] if resource.urls else None, file_digest(dest, "sha256"), recorded=False, override=True)
    _record_file(cache, _relpath(cache, dest), record)
    if resource.member:
        return _extract(resource, cache, dest, override=True)
    return dest


def fetch_labels(resource, cache=None, **kwargs):
    """Fetch the label table of an entry; returns its path, or None when the names are inline or there is no table."""
    labels = Resource.coerce(resource).label_resource()
    return None if labels is None else fetch(labels, cache, **kwargs)


def fetch_set(resource, cache=None, labels=True, **kwargs):
    """Fetch an entry and its label table: ``{"path": ..., "labels": path or None}``."""
    resource = Resource.coerce(resource)
    path = fetch(resource, cache, **kwargs)
    return {"path": path, "labels": fetch_labels(resource, cache, **kwargs) if labels else None}


def adopt(resource, path, cache=None):
    """Copy a file the user downloaded in a browser into the cache, after verifying it against the entry's checksum.

    This is how link-out entries (for example the Harvard AAN atlas, whose host needs a sign-in) are made available.
    Entries without a declared checksum record the sha256 of the adopted file. For ``unzip_member`` entries the
    file is the archive and the member is extracted. Returns the path of the entry's file.
    """
    resource = Resource.coerce(resource)
    path = os.fspath(path)
    if not os.path.isfile(path):
        raise FetchError(f"No such file: {path}", code="missing")
    ok, expected, actual, algo = _verify_download(resource, path, cache, _relpath(cache, download_path(resource, cache)))
    if not ok:
        raise ChecksumError(f"{os.path.basename(path)} does not match the {algo} checksum of {resource.title} (expected {expected}, got {actual}).", resource_id=resource.id, algo=algo, expected=expected, actual=actual, path=path)
    dest = download_path(resource, cache)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.abspath(path) != os.path.abspath(dest):
        shutil.copyfile(path, f"{dest}.part")
        os.replace(f"{dest}.part", dest)
    recorded = resource.checksum is None
    record = _record_for(resource, dest, None, actual if algo == "sha256" else file_digest(dest, "sha256"), recorded)
    record["adopted"] = True
    _record_file(cache, _relpath(cache, dest), record)
    return _extract(resource, cache, dest) if resource.member else dest


def manifest_record(resource, cache=None):
    """The manifest record of an entry's file (size, sha256, recorded_on_first_download, ...), or None."""
    return read_manifest(cache)["files"].get(_relpath(cache, primary_path(resource, cache)))


# Command line


def _resolve(args):
    return load_catalog(args.catalog).get(args.id)


def _parser():
    from . import jobs

    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.fetch", description="Fetch, verify and cache atlases, templates, priors and transforms.")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=jobs.JsonArgumentParser)
    get = commands.add_parser("get", help="download an entry into the cache (a cached, verified file is not downloaded again)")
    lst = commands.add_parser("list", help="list the entries of a catalog with their licence badge and cache state")
    adopt_ = commands.add_parser("adopt", help="copy a manually downloaded file into the cache after checking it")
    for sub in (get, lst, adopt_):
        sub.add_argument("--catalog", required=True, help="atlases, templates or the path of a catalog file")
        sub.add_argument("--cache", default=None, help="cache folder (default: $SLICER_LNM_CACHE or ~/.cache/SlicerStudio/LNM)")
        sub.add_argument("--cancel-file", default=None, help="stop (exit code 3) as soon as this file exists; a partial download is kept")
    for sub in (get, adopt_):
        sub.add_argument("--id", required=True, help="catalog entry id")
    get.add_argument("--offline", action="store_true", help="do not use the network: return the cached file or fail")
    get.add_argument("--accept-unverified", action="store_true", help="after a checksum error, keep the downloaded file anyway (recorded as checksum_override)")
    get.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help=f"retries after a dropped connection (default: {DEFAULT_RETRIES})")
    get.add_argument("--no-labels", action="store_true", help="do not fetch the label table")
    adopt_.add_argument("--file", required=True, help="the downloaded file (the archive for entries inside an archive)")
    adopt_.add_argument("--labels-file", default=None, help="the downloaded label table")
    return parser


def _entry_fields(resource):
    return {"id": resource.id, "title": resource.title, "kind": resource.kind, "licence": resource.licence, "badge": resource.badge, "terms_url": resource.get("terms_url"), "citation": resource.get("citation"), "warning": resource.get("warning"), "space": resource.get("space"), "family": resource.get("family")}


def main(argv):
    """Run one command (see the module documentation); events go to standard output."""
    from . import jobs

    args = _parser().parse_args(argv)
    cancel = jobs.CancelToken(args.cancel_file)
    jobs.emit("start", job="fetch", command=args.command, argv=list(argv))
    try:
        if args.command == "list":
            return _main_list(args)
        resource = _resolve(args)
        if args.command == "adopt":
            return _main_adopt(args, resource)
        return _main_get(args, resource, cancel)
    except FetchError as exc:
        fields = {key: value for key, value in exc.fields.items() if value is not None}
        raise jobs.JobError(exc.message, code=exc.code, **fields) from None


def _main_list(args):
    from . import jobs

    catalog = load_catalog(args.catalog)
    for resource in catalog:
        jobs.emit("result", cached=is_cached(resource, args.cache), size=resource.size, link_out=resource.link_out, derived=bool(resource.derive), **_entry_fields(resource))
    jobs.emit("done", outputs=[], elapsed=round(jobs.elapsed(), 3), run_id=None, entries=len(catalog))


def _main_adopt(args, resource):
    from . import jobs

    jobs.emit("stage", name="adopt", index=1, total=1)
    outputs = [adopt(resource, args.file, args.cache)]
    labels = resource.label_resource()
    if args.labels_file and labels is not None:
        outputs.append(adopt(labels, args.labels_file, args.cache))
    for path in outputs:
        jobs.emit("result", path=path, **_entry_fields(resource))
    jobs.emit("done", outputs=outputs, elapsed=round(jobs.elapsed(), 3), run_id=None)


def _main_get(args, resource, cancel):
    from . import jobs

    steps = [resource]
    labels = None if args.no_labels else resource.label_resource()
    if labels is not None:
        steps.append(labels)
    outputs = []
    for index, step in enumerate(steps, 1):
        cancel.raise_if_cancelled(done=index - 1)
        jobs.emit("stage", name="fetch" if step is resource else "labels", index=index, total=len(steps), id=step.id)
        started = time.monotonic()
        last = [0.0]

        def report(done, total, _started=started, _last=last, _id=step.id):
            now = time.monotonic()
            if now - _last[0] >= PROGRESS_INTERVAL_S or (total is not None and done >= total):
                _last[0] = now
                jobs.progress(done, total, _started, id=_id)

        was_cached = cached_path(step, args.cache) is not None
        try:
            path = fetch(step, args.cache, progress=report, cancel=cancel, offline=True if args.offline else None, retries=args.retries)
        except ChecksumError as exc:
            if not args.accept_unverified or not exc.path:
                raise
            path = accept_unverified(step, args.cache)
            jobs.emit("warning", code="checksum_override", message=f"{step.title}: the checksum did not match; the file was kept at your request (checksum_override).", id=step.id)
        record = manifest_record(step, args.cache) or {}
        if record.get("recorded_on_first_download") and not was_cached:
            jobs.emit("warning", code="checksum_recorded", message=f"{step.title}: no checksum is published for this file, so its sha256 was recorded at this first download.", id=step.id, sha256=record.get("sha256"))
        jobs.emit("result", path=path, cached=was_cached, size=record.get("size"), sha256=record.get("sha256"), checksum_override=bool(record.get("checksum_override")), part=step.kind, **_entry_fields(resource))
        outputs.append(path)
    jobs.emit("done", outputs=outputs, elapsed=round(jobs.elapsed(), 3), run_id=None)


if __name__ == "__main__":
    from LesionNetworkMappingLib import jobs as _jobs

    _jobs.run_job(main, job="fetch")

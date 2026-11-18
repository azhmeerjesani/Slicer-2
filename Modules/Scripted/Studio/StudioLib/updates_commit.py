"""Code updates from ``main`` between releases (decision D-U1, extended).

Releases (``updates.py``) bring full builds. In addition, an installed Slicer Studio picks up the Python and resource
files of new commits on ``main`` of ``azhmeerjesani/Slicer-2``: the files that the build copies verbatim into the
application (scripted modules, ``bin/Python``). Anything compiled into the application (C++, CMake, ``.ui`` or ``.qrc``
files built into C++, CLI XML, ...) waits for the next release.

**What the build ships.** ``CMake/SlicerStudioBuildInfo.cmake`` records, at configure time, every file that the
scripted-module and ``Base/Python`` install rules copy verbatim, with its installed path; at build time
``Utilities/Scripts/SlicerStudioRelease/make_build_info.py`` writes ``bin/Python/studio_build_info.json`` (the commit
B the build was made from, its tree, whether the working tree was clean) and ``bin/Python/studio_build_files.json.gz``
(the listing of B: path, mode and blob id of every file, and the source -> installed path map). Both hold relative
paths only.

**Check** (worker thread, after the start-up decision of the release updater, and daily; never with the GitHub API):

1. the head H of ``refs/heads/main`` with git's smart HTTP protocol version 2 (``ls-refs``) from
   ``https://github.com/azhmeerjesani/Slicer-2.git``;
2. B must be an ancestor of H (``baseIsOnMain``: the commits of main from H down to B, ``filter tree:0`` with ``have
   B``, each hashed; a chain of parents from H must name B). A build made from another branch, or from a commit ahead
   of main, takes nothing (``notOnMain``): main's code would be older than its own;
3. the commit H and its trees, without blobs (``fetch`` with ``deepen 1`` and ``filter blob:none``): every object is
   hashed, the commit must hash to H, every tree to its id, so the listing L_H is exactly commit H's;
4. the shipped listing L_B is checked against the tree id of B, and the two are compared;
5. every changed path is classified (``classify``): applied (a mapped file, or a file added next to mapped files with
   the same extension; a deletion only where the overlay hides the installed file: inside a regular package or a
   folder without Python files), ignored (documentation, tests, developer and release tools), or "needs a new build"
   (everything else);
6. when every change can be applied, each needed file is downloaded from ``raw.githubusercontent.com`` pinned to H and
   accepted only when its git blob id matches the id in the verified tree.

**Staging.** For every installed folder with a change (for example ``lib/Slicer-X.Y/qt-scripted-modules``), every
top-level package, module or ``Resources`` folder that holds a mapped file is copied whole from the installation into
``<state>/commit/<B>/staging/...`` (except ``studio_overlay.py``, ``studio_profile.py`` and ``slicerqt.py``, which run before an
update applies; a unit with a symbolic link or compiled code is refused), the changed files are written in, deleted ones
removed, and the result is compiled (a syntax error refuses the update). Under the state lock, the folder is then
renamed (atomically) to ``versions/<H>`` and ``state.json`` gets ``pending = H``. ``studio_overlay.py`` (in ``bin/Python``, run by ``slicerqt.py`` before any Studio import)
activates it at the next start, and rolls it back automatically when that start fails.

**What is trusted.** TLS and GitHub for the id of the head of ``main``; SHA-1 (as git uses it) for everything else:
the files that run are byte for byte those of commit H. Commits are not signature-checked, so anyone who can push to
``main`` can run code on every installation at its next start: protect ``main`` (required reviews, no force pushes).

The environment variable ``SLICER_STUDIO_COMMIT_UPDATES`` turns this off (``off``) or replaces the repository address
for tests (``http://127.0.0.1:<port>/<path>``, loopback addresses only: git at that address, files at
``<address>/raw/<commit>/<path>``; any other value is ignored, and a version staged from a test address is applied only
while it is set); ``SLICER_STUDIO_UPDATE_FEED=off`` also turns it off. No Qt here: the tests run it with plain Python.
"""

import gzip
import hashlib
import json
import os
import secrets
import shutil
import sys
import time
import urllib.parse

from . import updates_git as git

try:
    import studio_overlay
except ImportError:  # plain Python without Base/Python on sys.path
    _here = os.path.dirname(os.path.abspath(__file__))
    for _candidate in (os.path.join(_here, "..", "..", "..", "..", "Base", "Python"),):
        if os.path.isfile(os.path.join(_candidate, "studio_overlay.py")):
            sys.path.append(os.path.normpath(_candidate))
    import studio_overlay

REPOSITORY = "azhmeerjesani/Slicer-2"
BRANCH = "main"
GIT_URL = studio_overlay.OFFICIAL_GIT_URL
RAW_URL = "https://raw.githubusercontent.com/" + REPOSITORY + "/{commit}/{path}"
ENVIRONMENT_VARIABLE = studio_overlay.ENVIRONMENT_VARIABLE
BUILD_FILES_NAME = "studio_build_files.json.gz"
FORMAT = 1

#: Network timeouts (seconds) and sizes
CHECK_TIMEOUT_S = 5
CHECK_DEADLINE_S = 10
REFS_MAX_BYTES = 1024 * 1024
FETCH_TIMEOUT_S = 20
FETCH_DEADLINE_S = 120
FETCH_MAX_BYTES = 40 * 1024 * 1024
BLOB_TIMEOUT_S = 20
#: Limits of one update
MAX_FILES = 2000
MAX_BLOB_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
#: Free space needed besides twice the size of the update
FREE_SPACE_MARGIN = 200 * 1024 * 1024
SUBJECT_MAX_CHARS = 120
#: Folders of other builds (commit/<base>) unused for this long are removed
UNUSED_BASE_S = 60 * 24 * 3600
#: Staging folders of a run that is gone, or older than this, are removed
STALE_STAGING_S = 24 * 3600

#: Results of a check
UP_TO_DATE = "upToDate"
READY = "ready"
NEEDS_BUILD = "needsBuild"
TOO_LARGE = "tooLarge"
UNAVAILABLE = "unavailable"
NOT_ON_MAIN = "notOnMain"
OFFLINE = "offline"
FAILED = "failed"
SKIPPED = "skipped"
#: Results that do not change until main moves (the check reuses them for the same head)
STABLE_RESULTS = (UP_TO_DATE, NEEDS_BUILD, TOO_LARGE, NOT_ON_MAIN)
#: The build commit must be among this many commits of main below its head (else the build gets no code update)
MAX_COMMITS_AHEAD = 5000

_NATIVE_MAGIC = (b"\x7fELF", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe")
_NATIVE_SUFFIXES = (".so", ".dylib", ".pyd", ".dll", ".exe")


class CommitUpdateError(Exception):
    """A check or a staging that cannot go on; ``kind`` is one of the results above."""

    def __init__(self, kind, message):
        Exception.__init__(self, message)
        self.kind = kind


class VerificationError(CommitUpdateError):
    def __init__(self, message):
        CommitUpdateError.__init__(self, FAILED, message)


#
# Addresses
#


def override(env=None):
    """The value of ``SLICER_STUDIO_COMMIT_UPDATES``: "off", the replacement address of the tests (http(s) on this
    computer only), or None (not set, or a value that is neither: "on", "1", another host... are ignored)."""
    env = os.environ if env is None else env
    if studio_overlay.isTurnedOff(env):
        return "off"
    return studio_overlay.overrideUrl(env)


def endpoints(env=None):
    """{"git": repository address, "raw": address template of a file, "policy": host rules}, or None when off."""
    value = override(env)
    if value == "off":
        return None
    if not value:
        return {"git": GIT_URL, "raw": RAW_URL, "policy": {"extraHosts": (), "allowInsecureLoopback": False, "allowFile": False}}
    base = value.rstrip("/")
    host = (urllib.parse.urlsplit(base).hostname or "").lower()
    # Loopback only (studio_overlay.overrideUrl): no other host is ever allowed
    return {"git": base, "raw": base + "/raw/{commit}/{path}",
            "policy": {"extraHosts": (host,), "allowInsecureLoopback": True, "allowFile": False}}


def _updates():
    from . import updates

    return updates


def _agent():
    return f"SlicerStudio/{_updates().versionModule.STUDIO_VERSION}"


def _request(url, policy, timeout, deadlineS, maxBytes, data=None, headers=None, cancelled=None):
    """GET (or POST ``data``) ``url`` with the host rules ``policy``; return the body. Raise ``CommitUpdateError``
    (OFFLINE for a network failure)."""
    import http.client

    updates = _updates()
    deadline = time.monotonic() + deadlineS
    try:
        response = updates._open(url, None, timeout, notFound=f"not found ({url.rsplit('/', 1)[-1][:80]})", data=data, headers=headers, policy=policy)
    except updates.UpdateError as error:
        raise CommitUpdateError(OFFLINE, str(error))
    with response:
        length = response.headers.get("Content-Length") if hasattr(response, "headers") else None
        if length and length.isdigit() and int(length) > maxBytes:
            raise CommitUpdateError(TOO_LARGE, "the answer is too large")
        read = getattr(response, "read1", None) or response.read
        blocks, size = [], 0
        while True:
            if cancelled is not None and cancelled():
                raise CommitUpdateError(OFFLINE, "cancelled")
            try:
                block = read(64 * 1024)
            except (OSError, http.client.HTTPException) as error:
                raise CommitUpdateError(OFFLINE, f"the connection was interrupted ({error.__class__.__name__})")
            if not block:
                break
            blocks.append(block)
            size += len(block)
            if size > maxBytes:
                raise CommitUpdateError(TOO_LARGE, "the answer is too large")
            if time.monotonic() > deadline:
                raise CommitUpdateError(OFFLINE, "the server answers too slowly")
    return b"".join(blocks)


_GIT_HEADERS = {"Git-Protocol": "version=2"}


def _postCommand(endpoint, body, timeout, deadlineS, maxBytes, cancelled=None):
    headers = dict(_GIT_HEADERS, **{"Content-Type": "application/x-git-upload-pack-request",
                                    "Accept": "application/x-git-upload-pack-result"})
    return _request(endpoint["git"] + "/git-upload-pack", endpoint["policy"], timeout, deadlineS, maxBytes,
                    data=body, headers=headers, cancelled=cancelled)


def remoteHead(endpoint, cancelled=None):
    """The commit id of ``refs/heads/main`` (git protocol version 2: the capabilities, then ``ls-refs``)."""
    advertisement = _request(endpoint["git"] + "/info/refs?service=git-upload-pack", endpoint["policy"], CHECK_TIMEOUT_S,
                             CHECK_DEADLINE_S, REFS_MAX_BYTES, headers=dict(_GIT_HEADERS), cancelled=cancelled)
    try:
        git.checkCapabilities(git.parseCapabilities(advertisement))
        answer = _postCommand(endpoint, git.lsRefsRequest(f"refs/heads/{BRANCH}", _agent()), CHECK_TIMEOUT_S,
                              CHECK_DEADLINE_S, REFS_MAX_BYTES, cancelled=cancelled)
        refs = git.parseLsRefs(answer)
    except git.GitProtocolError as error:
        raise CommitUpdateError(FAILED, f"unexpected answer of the repository: {error}")
    head = refs.get(f"refs/heads/{BRANCH}")
    if not git.isObjectId(head):
        raise CommitUpdateError(FAILED, f"the repository has no branch {BRANCH}")
    return head


def fetchCommit(endpoint, head, cancelled=None):
    """The commit ``head`` and its listing, verified: (commit fields, listing path -> (mode, blob id))."""
    answer = _postCommand(endpoint, git.fetchRequest(head, _agent()), FETCH_TIMEOUT_S, FETCH_DEADLINE_S, FETCH_MAX_BYTES,
                          cancelled=cancelled)
    try:
        _shallow, pack = git.parseFetchResponse(answer)
        objects = git.parsePack(pack)
        commit = objects.get(head)
        if commit is None or commit[0] != "commit":
            raise git.GitProtocolError("the commit is not in the answer")
        fields = git.parseCommit(commit[1])
        listing = git.walkTree(objects, fields["tree"])
    except git.GitProtocolError as error:
        raise VerificationError(f"the commit could not be verified: {error}")
    return fields, listing


def baseIsOnMain(endpoint, base, head, cancelled=None):
    """True when the build commit ``base`` is an ancestor of the head ``head`` of main, so that main is newer than the
    build. The commits (no trees, no blobs: ``filter tree:0``) of main from its head down to ``base`` are fetched, with
    ``have base`` so that the server stops there, and ``deepen MAX_COMMITS_AHEAD`` as a cap. Every commit is hashed; a
    chain of parents from the head that names ``base`` proves it. A build made from a commit that main does not contain
    (another branch, a commit ahead of main, an unpushed commit) is never "updated" to main's code."""
    request = git.fetchRequest(head, _agent(), depth=MAX_COMMITS_AHEAD, filterSpec="tree:0", haves=[base])
    answer = _postCommand(endpoint, request, FETCH_TIMEOUT_S, FETCH_DEADLINE_S, FETCH_MAX_BYTES, cancelled=cancelled)
    try:
        _shallow, pack = git.parseFetchResponse(answer)
        objects = git.parsePack(pack)
        return git.reachesCommit(objects, head, base)
    except git.GitProtocolError as error:
        raise VerificationError(f"the history of main could not be verified: {error}")


def subjectOf(message):
    line = (message or "").strip().split("\n", 1)[0]
    line = "".join(c for c in line if c.isprintable())
    return line[:SUBJECT_MAX_CHARS]


#
# Build files
#


class BuildFiles:
    """The listing of the build commit B and the source -> installed path map (``studio_build_files.json.gz``)."""

    def __init__(self, data):
        self.commit = data.get("commit")
        self.tree = data.get("tree")
        self.listing = {}
        for entry in data.get("entries") or []:
            if isinstance(entry, list) and len(entry) == 3 and all(isinstance(value, str) for value in entry):
                self.listing[entry[0]] = (entry[1], entry[2])
        self.map = []
        for entry in data.get("map") or []:
            if not isinstance(entry, dict):
                continue
            src, dst, root = entry.get("src"), entry.get("dst"), entry.get("root")
            if not all(isinstance(value, str) and value for value in (src, dst, root)):
                continue
            if not _safeRelative(src) or not _safeRelative(dst) or not _safeRelative(root) or not _under(dst, root):
                continue
            module = entry.get("module")
            self.map.append({"src": src, "dst": dst, "root": root, "module": module if isinstance(module, str) and module.isidentifier() else None})
        self.bySrc = {}
        for entry in self.map:
            self.bySrc.setdefault(entry["src"], []).append(entry)
        self.dsts = {entry["dst"] for entry in self.map}
        self.preOverlay = set(data.get("preOverlay") or [])
        self.scriptedRoot = data.get("scriptedRoot")

    def roots(self):
        return sorted({entry["root"] for entry in self.map})


def _safeRelative(path):
    parts = path.split("/")
    return bool(path) and not path.startswith("/") and "\\" not in path and all(part not in ("", ".", "..") for part in parts)


def _under(path, root):
    return path.startswith(root + "/")


def _unit(dst, root):
    return dst[len(root) + 1:].split("/", 1)[0]


def loadBuildFiles(home, info):
    """The ``BuildFiles`` of the installation in ``home`` with the build information ``info``. Its listing must build
    the tree id of the build commit, so it is exactly the listing of B. Raise ``CommitUpdateError`` (UNAVAILABLE)."""
    name = info.get("files") or BUILD_FILES_NAME
    if "/" in name or name.startswith("."):
        raise CommitUpdateError(UNAVAILABLE, "invalid build information")
    path = os.path.join(home, studio_overlay.BUILD_INFO_FOLDER, name)
    try:
        with gzip.open(path, "rb") as f:
            data = json.loads(f.read(64 * 1024 * 1024).decode("utf-8"))
    except (OSError, ValueError, EOFError):
        raise CommitUpdateError(UNAVAILABLE, "the list of files of this build is missing")
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise CommitUpdateError(UNAVAILABLE, "the list of files of this build cannot be read")
    files = BuildFiles(data)
    if files.commit != info.get("commit") or files.tree != info.get("tree"):
        raise CommitUpdateError(UNAVAILABLE, "the list of files does not belong to this build")
    try:
        if git.treeIdFromListing(files.listing) != files.tree:
            raise CommitUpdateError(UNAVAILABLE, "the list of files of this build does not match its commit")
    except ValueError:
        raise CommitUpdateError(UNAVAILABLE, "the list of files of this build is damaged")
    return files


#
# Classification
#


#: Top-level files of the repository that are not part of the application
_IGNORED_TOP_FILES = (".pre-commit-config.yaml", ".codespellrc", ".codespellignore", ".clang-format", ".readthedocs.yaml",
                      ".ruff.toml", ".git-blame-ignore-revs", "CITATION.cff", "requirements-docs.txt")
#: Files of developer tools anywhere in the tree
_IGNORED_NAMES = ("pyproject.toml", ".ruff.toml", "setup.cfg", "pytest.ini", "tox.ini")


def isIgnored(path):
    """Changes that do not affect the application: documentation, tests, repository and developer files, the release,
    packaging and web tools of Slicer Studio. Only paths that are not mapped get here (a mapped file is applied
    whatever its name), and only files that no build step reads may be ignored."""
    parts = path.split("/")
    name = parts[-1]
    if parts[0] in ("Docs", ".github", ".claude", ".devcontainer"):
        return True
    if any(part in ("Testing", "tests") for part in parts[:-1]):
        return True
    if name.endswith(".md"):
        return True  # documentation (no build step reads a Markdown file below the top level; README.md is not mapped)
    if len(parts) == 1:
        return name.startswith(".git") or name in _IGNORED_TOP_FILES
    if name in _IGNORED_NAMES:
        return True
    if parts[0] == "Utilities":
        # Slicer Studio's developer, release, packaging and web tools (Utilities/Scripts/Studio*, SlicerStudio*,
        # Utilities/SlicerStudio*): none is part of the application
        if len(parts) >= 3 and parts[1] == "Scripts" and parts[2].startswith(("Studio", "SlicerStudio")):
            return True
        if parts[1].startswith("SlicerStudio"):
            return True
    return False


def _extension(name):
    index = name.rfind(".")
    return name[index:] if index > 0 else ""


class Classification:
    def __init__(self):
        self.apply = []  # {"src", "dst", "root", "mode", "blob", "action": "write"|"delete"}
        self.needsBuild = []
        self.ignored = []

    @property
    def kind(self):
        if self.needsBuild:
            return NEEDS_BUILD
        if self.apply:
            return READY
        return UP_TO_DATE


def _deletable(dst, root, buildFiles):
    """True when deleting the installed file ``dst`` from its overlay unit hides it for real: the unit is a regular
    package (its ``__init__.py`` is mapped; Python then never looks in the installed copy) or a folder without Python
    files (resources, found through the overlay path). A deleted top-level module or folder, or a file of a namespace
    package, would still be found in the installed files after the overlay folder: that needs a new build."""
    relative = dst[len(root) + 1:]
    if "/" not in relative:
        return False
    unit = relative.split("/", 1)[0]
    if relative == f"{unit}/__init__.py":
        return False  # the package would become a namespace package
    if f"{root}/{unit}/__init__.py" in buildFiles.dsts:
        return True
    return not any(other.startswith(f"{root}/{unit}/") and other.endswith(".py") for other in buildFiles.dsts)


def classify(changes, buildFiles):
    """Sort each change (path, old entry, new entry) of B -> H into applied, ignored or "needs a new build"."""
    result = Classification()
    # Folders of mapped files: (source folder, extension) -> (root, installed folder), when all agree
    folders = {}
    for entry in buildFiles.map:
        src, dst = entry["src"], entry["dst"]
        key = (os.path.dirname(src) if "/" in src else "", _extension(os.path.basename(src)))
        value = (entry["root"], os.path.dirname(dst))
        if folders.get(key, value) != value:
            value = None
        folders[key] = value
    for path, before, after in changes:
        modes = [entry[0] for entry in (before, after) if entry is not None]
        if path in buildFiles.preOverlay:
            result.needsBuild.append(path)
            continue
        mapped = buildFiles.bySrc.get(path)
        if mapped:
            if any(mode not in git.REGULAR_MODES for mode in modes) or (before and after and before[0] != after[0]):
                result.needsBuild.append(path)
                continue
            if after is None and not all(_deletable(entry["dst"], entry["root"], buildFiles) for entry in mapped):
                result.needsBuild.append(path)
                continue
            for entry in mapped:
                if after is None:
                    result.apply.append({"src": path, "dst": entry["dst"], "root": entry["root"], "mode": None, "blob": None, "action": "delete"})
                else:
                    result.apply.append({"src": path, "dst": entry["dst"], "root": entry["root"], "mode": after[0], "blob": after[1], "action": "write"})
            continue
        if isIgnored(path):
            result.ignored.append(path)
            continue
        if before is None and after is not None and after[0] in git.REGULAR_MODES:
            name = path.rsplit("/", 1)[-1]
            key = (os.path.dirname(path) if "/" in path else "", _extension(name))
            target = folders.get(key) if key[1] else None
            if target is not None:
                root, folder = target
                dst = f"{folder}/{name}"
                if _under(dst, root) and _safeRelative(dst):
                    result.apply.append({"src": path, "dst": dst, "root": root, "mode": after[0], "blob": after[1], "action": "write"})
                    continue
        result.needsBuild.append(path)
    return result


#
# Files
#


def blobIdOfFile(path):
    """The git blob id of the file ``path``, or None when it cannot be read."""
    try:
        size = os.path.getsize(path)
        digest = hashlib.sha1(f"blob {size}\0".encode("ascii"))
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def _isNative(path):
    if path.endswith(_NATIVE_SUFFIXES):
        return True
    try:
        with open(path, "rb") as f:
            head = f.read(4)
    except OSError:
        return False
    return head in _NATIVE_MAGIC


def _treeSize(path):
    total = 0
    for folder, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(folder, name)).st_size
            except OSError:
                pass
    return total


def _copyUnit(source, destination):
    """Copy the installed unit ``source`` (a file or a folder) to ``destination``, keeping times (so that the compiled
    files stay valid). Raise ``CommitUpdateError`` when it holds a native binary or a symbolic link (a write through a
    link could reach the installation)."""
    name = os.path.basename(source)
    if os.path.islink(source):
        raise CommitUpdateError(UNAVAILABLE, f"{name} is a symbolic link")
    if os.path.isdir(source):
        for folder, dirs, files in os.walk(source):
            for entry in dirs + files:
                if os.path.islink(os.path.join(folder, entry)):
                    raise CommitUpdateError(UNAVAILABLE, f"{name} holds a symbolic link")
            for entry in files:
                if _isNative(os.path.join(folder, entry)):
                    raise CommitUpdateError(UNAVAILABLE, f"{name} holds compiled code")
        shutil.copytree(source, destination, symlinks=False, copy_function=shutil.copy2)
    else:
        if _isNative(source):
            raise CommitUpdateError(UNAVAILABLE, f"{name} is compiled code")
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copy2(source, destination, follow_symlinks=False)


def _checkInside(path, folder):
    """Raise ``VerificationError`` unless ``path`` (after links are followed) stays inside ``folder``: nothing is ever
    written or deleted outside the staged copy."""
    real = os.path.realpath(path)
    top = os.path.realpath(folder)
    if not (real == top or real.startswith(top.rstrip(os.sep) + os.sep)):
        raise VerificationError(f"{os.path.basename(path)} is outside the update folder")


def _writeFile(path, data, mode):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.lexists(path):
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
    with open(path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(path, 0o755 if mode == git.MODE_EXECUTABLE else 0o644)


def _removeCompiled(path):
    folder, name = os.path.split(path)
    stem = name[:-3] if name.endswith(".py") else None
    cache = os.path.join(folder, "__pycache__")
    if stem is None or not os.path.isdir(cache):
        return
    for entry in os.listdir(cache):
        if entry.split(".", 1)[0] == stem and entry.endswith(".pyc"):
            try:
                os.remove(os.path.join(cache, entry))
            except OSError:
                pass


def _compile(stagedFiles, changedPython):
    """Compile the changed Python files (a syntax error refuses the update), then the rest of the overlay (only files
    whose compiled form is missing or out of date; errors there are ignored, as in the installation)."""
    import compileall
    import py_compile

    for path in changedPython:
        try:
            py_compile.compile(path, doraise=True)
        except py_compile.PyCompileError as error:
            raise CommitUpdateError(FAILED, f"{os.path.basename(path)} has an error: {error.exc_type_name}")
        except OSError:
            pass
    try:
        compileall.compile_dir(stagedFiles, quiet=2, workers=1)
    except Exception:
        pass


#
# State
#


def baseDirectory(stateRoot, base):
    return studio_overlay.baseDirectory(stateRoot, base)


def readState(stateRoot, base):
    return studio_overlay.readState(baseDirectory(stateRoot, base)) or studio_overlay.newState(base)


def _update(stateRoot, base, change):
    state = studio_overlay.updateState(baseDirectory(stateRoot, base), base, change)
    if state is None:
        raise CommitUpdateError(FAILED, "the update state is locked by another Slicer Studio")
    return state


def retire(stateRoot, base):
    """main is back to the installed code: use the installed files from the next start."""
    if not os.path.isfile(os.path.join(baseDirectory(stateRoot, base), studio_overlay.STATE_FILE)):
        return False

    def change(state):
        if state.get("active") or (state.get("pending") and state.get("pending") != "installed"):
            state["pending"] = "installed"
            return True
        return False

    return bool(_update(stateRoot, base, change).get("pending") == "installed")


def setDisabled(stateRoot, base, disabled, reason="setting"):
    """Turn the code updates of this build off (the installed code runs from the next start) or on again."""

    def change(state):
        if disabled:
            if not state.get("disabled"):
                state["disabledBefore"] = False
            state["disabled"] = True
            state["disabledReason"] = reason
        else:
            state["disabled"] = False
            state.pop("disabledReason", None)
            state.pop("disabledBefore", None)
        return True

    return _update(stateRoot, base, change)


def clearForRelease(stateRoot, base):
    """A release is about to be installed: its build runs no commit update. Return whether the updates were already
    off (so that a failed install can restore it)."""
    if not base or not os.path.isfile(os.path.join(baseDirectory(stateRoot, base), studio_overlay.STATE_FILE)):
        return None  # this build never staged a code update: nothing to clear
    before = {}

    def change(state):
        if state.get("disabled") and state.get("disabledReason") == "release":
            before["disabled"] = bool(state.get("disabledBefore"))  # already done (Install when I quit, then Restart now)
            return False
        before["disabled"] = bool(state.get("disabled"))
        state["disabledBefore"] = bool(state.get("disabled"))
        state["disabled"] = True
        state["disabledReason"] = "release"
        return True

    try:
        _update(stateRoot, base, change)
    except CommitUpdateError:
        return None
    return before.get("disabled")


def restoreAfterRelease(stateRoot, base):
    """The release install did not happen or failed: undo ``clearForRelease``."""
    folder = baseDirectory(stateRoot, base) if base else None
    if not folder or not os.path.isfile(os.path.join(folder, studio_overlay.STATE_FILE)):
        return False
    current = studio_overlay.readState(folder)
    if current is None or current.get("disabledReason") != "release":
        return False  # nothing to undo (read without the lock: this runs at every start)

    def change(state):
        if state.get("disabledReason") != "release":
            return False
        state["disabled"] = bool(state.get("disabledBefore"))
        state.pop("disabledBefore", None)
        if state["disabled"]:
            state["disabledReason"] = "setting"
        else:
            state.pop("disabledReason", None)
        return True

    try:
        _update(stateRoot, base, change)
    except CommitUpdateError:
        return False
    return True


def forgetBase(stateRoot, base):
    """Remove everything staged for the build ``base`` (after a release replaced it)."""
    if not base or not studio_overlay.isCommitId(base):
        return False
    folder = baseDirectory(stateRoot, base)
    if os.path.isdir(folder):
        shutil.rmtree(folder, ignore_errors=True)
        return True
    return False


def requestRollback(stateRoot, base, target=None, env=None):
    """Go back from the running version to ``target`` (a staged version, or None for the installed files) at the next
    start. The version gone back from is not offered again until main moves on."""

    def change(state):
        current = state.get("active")
        if current:
            studio_overlay.mark(state, "skipped", current)
        if target and studio_overlay.validVersion(baseDirectory(stateRoot, base), target, base, env):
            state["pending"] = target
        else:
            state["pending"] = "installed"
        return True

    return _update(stateRoot, base, change)


def retry(stateRoot, base, version):
    """Allow a version marked bad or skipped again (the next check stages it again if it is still the head)."""

    def change(state):
        for key in ("bad", "skipped"):
            state[key] = [value for value in state.get(key) or [] if value != version]
        return True

    return _update(stateRoot, base, change)


def _cleanStaging(baseDir, now):
    folder = os.path.join(baseDir, studio_overlay.STAGING_FOLDER)
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        path = os.path.join(folder, name)
        parts = name.split("-")
        pid = int(parts[1]) if len(parts) >= 3 and parts[1].isdigit() else None
        try:
            age = now - os.stat(path).st_mtime
        except OSError:
            continue
        if pid is None or not studio_overlay.processAlive(pid) or age > STALE_STAGING_S:
            shutil.rmtree(path, ignore_errors=True)


def collectGarbage(stateRoot, base, now=None):
    """Remove versions that are not active, previous or pending and that no running Slicer Studio uses, and the folders
    of other builds that were not used for ``UNUSED_BASE_S``. Return the removed paths."""
    now = time.time() if now is None else now
    removed = []
    baseDir = baseDirectory(stateRoot, base)
    versions = os.path.join(baseDir, studio_overlay.VERSIONS_FOLDER)
    trash = []

    def change(state):
        # Under the lock: a stager publishes versions/<H> and sets pending = H under the same lock (stageUpdate), so
        # a version is never seen here without its state. Doomed versions are only renamed here (fast), and removed
        # after the lock is released.
        keep = {value for value in (state.get("active"), state.get("previous"), state.get("pending")) if value}
        for boot in state.get("boots") or []:
            if isinstance(boot, dict) and studio_overlay.bootAlive(boot, now):
                keep.add(boot.get("version"))
        try:
            names = os.listdir(versions)
        except OSError:
            names = []
        for name in names:
            if name in keep:
                continue
            target = os.path.join(baseDir, studio_overlay.STAGING_FOLDER, f"trash-{os.getpid()}-{secrets.token_hex(4)}")
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                os.rename(os.path.join(versions, name), target)
            except OSError:
                continue
            trash.append(target)
            removed.append(os.path.join(versions, name))
        return False

    if os.path.isfile(os.path.join(baseDir, studio_overlay.STATE_FILE)):
        if studio_overlay.updateState(baseDir, base, change) is None:
            return removed
        for path in trash:
            if _isStrictlyInside(path, baseDir):
                shutil.rmtree(path, ignore_errors=True)
        _cleanStaging(baseDir, now)
    try:
        others = os.listdir(stateRoot)
    except OSError:
        others = []
    for name in others:
        if name == base or not studio_overlay.isCommitId(name):
            continue
        folder = os.path.join(stateRoot, name)
        state = studio_overlay.readState(folder)
        if state is not None and any(isinstance(boot, dict) and studio_overlay.bootAlive(boot, now) for boot in state.get("boots") or []):
            continue
        try:
            used = max(os.stat(os.path.join(folder, entry)).st_mtime for entry in os.listdir(folder)) if os.listdir(folder) else os.stat(folder).st_mtime
        except OSError:
            continue
        if now - used > UNUSED_BASE_S and _isStrictlyInside(folder, stateRoot):
            shutil.rmtree(folder, ignore_errors=True)
            removed.append(folder)
    return removed


def _isStrictlyInside(path, folder):
    """True when ``path`` is inside ``folder`` (and not ``folder`` itself): clean-ups never delete anything else."""
    path, folder = os.path.realpath(path), os.path.realpath(folder)
    return path != folder and path.startswith(folder.rstrip(os.sep) + os.sep)


#
# Check and staging
#


def _result(kind, **values):
    result = {"result": kind, "head": None, "base": None, "committed": None, "subject": "", "files": 0, "paths": [],
              "needsBuild": 0, "error": None, "checked": studio_overlay.isoTime()}
    result.update(values)
    return result


def check(home, stateRoot, env=None, cancelled=None, cached=None, stage=True):
    """Check main and stage its code when it can be applied. Return a result dict: ``result`` (see the constants),
    ``head``, ``base``, ``committed``, ``subject``, ``files`` (count of applied files), ``paths`` (the first ones),
    ``needsBuild`` (count), ``error``. Runs in a worker thread; never raises for a network problem (OFFLINE)."""
    env = os.environ if env is None else env
    endpoint = endpoints(env)
    if endpoint is None:
        return _result(UNAVAILABLE, error="turned off")
    info = studio_overlay.readBuildInfo(home)
    if info is None or not info.get("commit"):
        return _result(UNAVAILABLE, error="this build does not know its commit")
    base = info["commit"]
    if info.get("dirty"):
        return _result(UNAVAILABLE, base=base, error="this build was made from changed files")
    try:
        head = remoteHead(endpoint, cancelled)
    except CommitUpdateError as error:
        return _result(error.kind, base=base, error=str(error))
    baseDir = baseDirectory(stateRoot, base)
    state = studio_overlay.readState(baseDir) or studio_overlay.newState(base)
    if head == base:
        retire(stateRoot, base)
        return _result(UP_TO_DATE, base=base, head=head)
    if head in (state.get("bad") or []) or head in (state.get("skipped") or []):
        return _result(SKIPPED, base=base, head=head)
    activationError = state.get("activationError") if isinstance(state.get("activationError"), dict) else None
    if head == state.get("active") and activationError and activationError.get("version") == head:
        # Staged, but the bootstrap could not apply it: say so instead of "up to date"
        return _result(FAILED, base=base, head=head, error=f"the code update could not be applied: {activationError.get('error')}")
    if head in (state.get("active"), state.get("pending")) and studio_overlay.validVersion(baseDir, head, base, env):
        manifest = studio_overlay.readManifest(baseDir, head) or {}
        try:
            # The staged files are checked again (a damaged file is downloaded again and replaced)
            repaired = repairVersion(baseDir, head, manifest, endpoint, cancelled)
        except CommitUpdateError as error:
            return _result(error.kind, base=base, head=head, error=str(error))
        return _result(READY, base=base, head=head, committed=manifest.get("committed"), subject=manifest.get("subject", ""),
                       files=len(manifest.get("changed") or []), paths=(manifest.get("changed") or [])[:10],
                       already=("active" if head == state.get("active") else "pending"), repaired=repaired)
    if isinstance(cached, dict) and cached.get("head") == head and cached.get("base") == base and cached.get("result") in STABLE_RESULTS:
        return dict(cached, checked=studio_overlay.isoTime(), cachedResult=True)
    try:
        buildFiles = loadBuildFiles(home, info)
        if not baseIsOnMain(endpoint, base, head, cancelled):
            return _result(NOT_ON_MAIN, base=base, head=head,
                           error=f"this build was made from commit {base[:7]}, which {BRANCH} does not contain")
        fields, listing = fetchCommit(endpoint, head, cancelled)
        committed = studio_overlay.isoTime(fields["committed"]) if fields.get("committed") else None
        subject = subjectOf(fields.get("message"))
        changes = git.diffListings(buildFiles.listing, listing)
        classification = classify(changes, buildFiles)
        common = {"base": base, "head": head, "committed": committed, "subject": subject}
        if classification.kind == UP_TO_DATE:
            retire(stateRoot, base)
            return _result(UP_TO_DATE, ignored=len(classification.ignored), **common)
        if classification.kind == NEEDS_BUILD:
            return _result(NEEDS_BUILD, needsBuild=len(classification.needsBuild), paths=classification.needsBuild[:10],
                           files=len(classification.apply), **common)
        if len(classification.apply) > MAX_FILES:
            return _result(TOO_LARGE, files=len(classification.apply), **common)
        changed = [item["dst"] for item in classification.apply]
        if not stage:
            return _result(READY, files=len(changed), paths=changed[:10], staged=False, **common)
        stageUpdate(home, stateRoot, info, buildFiles, listing, classification, head, fields, endpoint, cancelled)
        return _result(READY, files=len(changed), paths=changed[:10], staged=True, **common)
    except CommitUpdateError as error:
        return _result(error.kind, base=base, head=head, error=str(error))


def _blobSources(baseDir, exclude):
    """blob id -> file of the versions already staged (to avoid downloading a file again)."""
    sources = {}
    versions = os.path.join(baseDir, studio_overlay.VERSIONS_FOLDER)
    try:
        names = os.listdir(versions)
    except OSError:
        return sources
    for name in names:
        if name == exclude:
            continue
        manifest = studio_overlay.readJson(os.path.join(versions, name, studio_overlay.MANIFEST_FILE))
        files = manifest.get("files") if manifest else None
        if not isinstance(files, dict):
            continue
        for dst, blob in files.items():
            if isinstance(dst, str) and isinstance(blob, str) and _safeRelative(dst):
                sources.setdefault(blob, os.path.join(versions, name, studio_overlay.FILES_FOLDER, dst))
    return sources


class _Downloader:
    def __init__(self, endpoint, head, cancelled, reuse):
        self.endpoint = endpoint
        self.head = head
        self.cancelled = cancelled
        self.reuse = reuse
        self.total = 0
        self.downloaded = 0

    def get(self, src, blob):
        """The bytes of ``src`` at the head commit, whose blob id must be ``blob``."""
        candidate = self.reuse.get(blob)
        if candidate:
            try:
                with open(candidate, "rb") as f:
                    data = f.read(MAX_BLOB_BYTES + 1)
                if git.blobId(data) == blob:
                    return data
            except OSError:
                pass  # removed meanwhile (another window cleaned it up): downloaded instead
        url = self.endpoint["raw"].format(commit=self.head, path=urllib.parse.quote(src, safe="/"))
        data = _request(url, self.endpoint["policy"], BLOB_TIMEOUT_S, FETCH_DEADLINE_S, MAX_BLOB_BYTES, cancelled=self.cancelled)
        if git.blobId(data) != blob:
            raise VerificationError(f"{src} does not match commit {self.head[:7]}")
        self.total += len(data)
        self.downloaded += 1
        if self.total > MAX_TOTAL_BYTES:
            raise CommitUpdateError(TOO_LARGE, "the update is too large")
        return data


def _isBootstrap(entry, buildFiles):
    """True for a mapped file that runs before an update applies (``preOverlay``: studio_overlay.py, studio_profile.py,
    slicerqt.py)."""
    if entry["src"] in buildFiles.preOverlay:
        return True
    unit = _unit(entry["dst"], entry["root"])
    return unit.endswith(".py") and unit[:-3] in studio_overlay.BOOTSTRAP_MODULES


def _envOf(endpoint):
    """An environment in which ``endpoint`` is allowed (for ``studio_overlay.validVersion``)."""
    if endpoint["git"] == GIT_URL:
        return {}
    return {ENVIRONMENT_VARIABLE: endpoint["git"]}


def stageUpdate(home, stateRoot, info, buildFiles, listing, classification, headId, fields, endpoint, cancelled=None):
    """Build the overlay of the head commit in a staging folder, verify it, and publish it as ``versions/<head>`` with
    ``pending = head``. Raise ``CommitUpdateError``."""
    base = info["commit"]
    if not git.isObjectId(headId):
        raise VerificationError("invalid commit id")
    for item in classification.apply:
        if item["action"] == "write" and not git.isObjectId(item["blob"] or ""):
            raise VerificationError("invalid file id")
    baseDir = baseDirectory(stateRoot, base)
    now = time.time()
    _cleanStaging(baseDir, now)
    staging = os.path.join(baseDir, studio_overlay.STAGING_FOLDER, f"{headId}-{os.getpid()}-{secrets.token_hex(4)}")
    filesDir = os.path.join(staging, studio_overlay.FILES_FOLDER)
    changedRoots = sorted({item["root"] for item in classification.apply})
    reuse = _blobSources(baseDir, headId)
    downloader = _Downloader(endpoint, headId, cancelled, reuse)
    try:
        os.makedirs(filesDir)
        # The units of each changed root: every top-level entry with a mapped (or added) file, and __pycache__. The
        # files that run before an update applies (studio_overlay.py, studio_profile.py, slicerqt.py) are never part of one: they are
        # already loaded from the installation when the overlay is activated.
        units = {}
        for entry in buildFiles.map:
            if entry["root"] in changedRoots and not _isBootstrap(entry, buildFiles):
                units.setdefault(entry["root"], set()).add(_unit(entry["dst"], entry["root"]))
        for item in classification.apply:
            units.setdefault(item["root"], set()).add(_unit(item["dst"], item["root"]))
        size = 0
        for root in changedRoots:
            installedRoot = os.path.join(home, root)
            if os.path.isdir(os.path.join(installedRoot, "__pycache__")):
                units[root].add("__pycache__")
            for unit in units[root]:
                source = os.path.join(installedRoot, unit)
                if os.path.lexists(source):
                    size += _treeSize(source) if os.path.isdir(source) else os.lstat(source).st_size
        free = shutil.disk_usage(baseDir).free
        if free < 2 * size + FREE_SPACE_MARGIN:
            raise CommitUpdateError(UNAVAILABLE, "not enough free disk space for the update")
        for root in changedRoots:
            for unit in sorted(units[root]):
                if cancelled is not None and cancelled():
                    raise CommitUpdateError(OFFLINE, "cancelled")
                source = os.path.join(home, root, unit)
                if os.path.lexists(source):
                    _copyUnit(source, os.path.join(filesDir, root, unit))
        # The final files of the mapped entries of the changed roots, from the verified listing of the head
        applied = {item["dst"]: item for item in classification.apply}
        files = {}
        sources = {}
        changedPython = []
        for entry in buildFiles.map:
            if entry["root"] not in changedRoots or entry["dst"] in applied or _isBootstrap(entry, buildFiles):
                continue
            src, dst = entry["src"], entry["dst"]
            target = os.path.join(filesDir, dst)
            headEntry = listing.get(src)
            if headEntry is None:
                continue
            _checkInside(target, filesDir)
            files[dst] = headEntry[1]
            sources[dst] = src
            # Unchanged file: the installed copy must be the one of the build (a file changed by hand is downloaded)
            if blobIdOfFile(target) != headEntry[1]:
                _writeFile(target, downloader.get(src, headEntry[1]), headEntry[0])
                _removeCompiled(target)
                if target.endswith(".py"):
                    changedPython.append(target)
        for dst, item in sorted(applied.items()):
            if cancelled is not None and cancelled():
                raise CommitUpdateError(OFFLINE, "cancelled")
            target = os.path.join(filesDir, dst)
            _checkInside(target, filesDir)
            _removeCompiled(target)
            if item["action"] == "delete":
                if os.path.lexists(target):
                    os.remove(target)
                continue
            _writeFile(target, downloader.get(item["src"], item["blob"]), item["mode"])
            files[dst] = item["blob"]
            sources[dst] = item["src"]
            if target.endswith(".py"):
                changedPython.append(target)
        _compile(filesDir, changedPython)
        modules = {}
        for entry in buildFiles.map:
            if entry["module"] and entry["root"] in changedRoots and os.path.isfile(os.path.join(filesDir, entry["dst"])):
                modules[entry["module"]] = entry["dst"]
        manifest = {
            "format": FORMAT,
            "base": base,
            "commit": headId,
            "source": endpoint["git"],
            "committed": studio_overlay.isoTime(fields["committed"]) if fields.get("committed") else None,
            "subject": subjectOf(fields.get("message")),
            "roots": changedRoots,
            "units": {root: sorted(units[root]) for root in changedRoots},
            "files": files,
            "sources": sources,
            "modules": modules,
            "changed": sorted(applied),
            "deleted": sorted(dst for dst, item in applied.items() if item["action"] == "delete"),
            "downloaded": downloader.downloaded,
            "created": studio_overlay.isoTime(now),
        }
        if hasattr(os, "sync"):
            os.sync()
        studio_overlay.writeJsonAtomic(os.path.join(staging, studio_overlay.MANIFEST_FILE), manifest)
        versions = os.path.join(baseDir, studio_overlay.VERSIONS_FOLDER)
        os.makedirs(versions, exist_ok=True)
        final = os.path.join(versions, headId)

        def change(state):
            # Published under the state lock, together with pending = head: collectGarbage (also under the lock) never
            # sees versions/<head> without its state
            if headId in (state.get("bad") or []) or headId in (state.get("skipped") or []):
                return False
            if studio_overlay.validVersion(baseDir, headId, base, _envOf(endpoint)) is None:
                if os.path.lexists(final):
                    shutil.rmtree(final)  # a damaged leftover
                os.rename(staging, final)
                _fsyncFolder(versions)
            # else: another window staged the same commit first (the staging folder is removed below)
            if state.get("active") == headId:
                if state.get("pending"):
                    state["pending"] = None
                    return True
                return False
            state["pending"] = headId
            return True

        _update(stateRoot, base, change)
    except CommitUpdateError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except OSError as error:
        shutil.rmtree(staging, ignore_errors=True)
        raise CommitUpdateError(FAILED, f"the update could not be staged: {error.strerror or error}")
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(staging, ignore_errors=True)
    return final


def repairVersion(baseDir, version, manifest, endpoint, cancelled=None):
    """Hash every file of the staged ``version`` against its blob id; download a damaged or missing one again (pinned to
    the commit, verified) and replace it atomically. Return the number of files replaced."""
    files = manifest.get("files") or {}
    sources = manifest.get("sources") or {}
    filesDir = os.path.join(studio_overlay.versionDirectory(baseDir, version), studio_overlay.FILES_FOLDER)
    downloader = None
    repaired = 0
    for dst, blob in sorted(files.items()):
        if not (isinstance(dst, str) and _safeRelative(dst) and git.isObjectId(blob or "")):
            continue
        path = os.path.join(filesDir, dst)
        _checkInside(path, filesDir)
        if blobIdOfFile(path) == blob:
            continue
        src = sources.get(dst)
        if not isinstance(src, str) or not _safeRelative(src):
            raise CommitUpdateError(FAILED, f"{dst} of the code update is damaged")
        if downloader is None:
            downloader = _Downloader(endpoint, version, cancelled, {})
        data = downloader.get(src, blob)
        temporary = f"{path}.{os.getpid()}.repair"
        _writeFile(temporary, data, git.MODE_EXECUTABLE if os.access(path, os.X_OK) else git.MODE_FILE)
        os.replace(temporary, path)
        _removeCompiled(path)
        repaired += 1
    return repaired


def _fsyncFolder(path):
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


#
# What the settings panel shows
#


def describe(home, stateRoot):
    """The commit updates of this installation: the build, the running code and the state (main thread or worker)."""
    info = studio_overlay.buildInfo() or (studio_overlay.readBuildInfo(home) if home else None)
    running = studio_overlay.status()
    result = {"build": info, "running": running, "state": None}
    if info and info.get("commit") and stateRoot:
        baseDir = baseDirectory(stateRoot, info["commit"])
        state = studio_overlay.readState(baseDir)
        result["state"] = state
        if state:
            result["manifests"] = {version: studio_overlay.readManifest(baseDir, version) for version in
                                   {state.get("active"), state.get("previous"), state.get("pending")} if studio_overlay.isCommitId(version or "")}
    return result

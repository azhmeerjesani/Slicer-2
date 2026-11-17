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
2. the commit H and its trees, without blobs (``fetch`` with ``deepen 1`` and ``filter blob:none``): every object is
   hashed, the commit must hash to H, every tree to its id, so the listing L_H is exactly commit H's;
3. the shipped listing L_B is checked against the tree id of B, and the two are compared;
4. every changed path is classified (``classify``): applied (a mapped file, or a file added next to mapped files with
   the same extension), ignored (documentation, tests, ...), or "needs a new build" (everything else);
5. when every change can be applied, each needed file is downloaded from ``raw.githubusercontent.com`` pinned to H and
   accepted only when its git blob id matches the id in the verified tree.

**Staging.** For every installed folder with a change (for example ``lib/Slicer-X.Y/qt-scripted-modules``), every
top-level package, module or ``Resources`` folder that holds a mapped file is copied whole from the installation into
``<state>/commit/<B>/staging/...``, the changed files are written in, deleted ones removed, and the result is compiled
(a syntax error refuses the update). The folder is then renamed (atomically) to ``versions/<H>`` and ``state.json``
gets ``pending = H``. ``studio_overlay.py`` (in ``bin/Python``, run by ``slicerqt.py`` before any Studio import)
activates it at the next start, and rolls it back automatically when that start fails.

**What is trusted.** TLS and GitHub for the id of the head of ``main``; SHA-1 (as git uses it) for everything else:
the files that run are byte for byte those of commit H. Commits are not signature-checked, so anyone who can push to
``main`` can run code on every installation at its next start: protect ``main`` (required reviews, no force pushes).

The environment variable ``SLICER_STUDIO_COMMIT_UPDATES`` turns this off (``off``) or replaces the repository address
for tests (``http://127.0.0.1:<port>/<path>``: git at that address, files at ``<address>/raw/<commit>/<path>``);
``SLICER_STUDIO_UPDATE_FEED=off`` also turns it off. No Qt here: the tests run it with plain Python.
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
GIT_URL = f"https://github.com/{REPOSITORY}.git"
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
OFFLINE = "offline"
FAILED = "failed"
SKIPPED = "skipped"
#: Results that do not change until main moves (the check reuses them for the same head)
STABLE_RESULTS = (UP_TO_DATE, NEEDS_BUILD, TOO_LARGE)

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
    """The value of ``SLICER_STUDIO_COMMIT_UPDATES``: None when not set, "off", or the replacement address."""
    env = os.environ if env is None else env
    if studio_overlay.isTurnedOff(env):
        return "off"
    value = str(env.get(ENVIRONMENT_VARIABLE, "")).strip()
    return value or None


def endpoints(env=None):
    """{"git": repository address, "raw": address template of a file, "policy": host rules}, or None when off."""
    value = override(env)
    if value == "off":
        return None
    if not value:
        return {"git": GIT_URL, "raw": RAW_URL, "policy": {"extraHosts": (), "allowInsecureLoopback": False, "allowFile": False}}
    base = value.rstrip("/")
    host = (urllib.parse.urlsplit(base).hostname or "").lower()
    return {"git": base, "raw": base + "/raw/{commit}/{path}",
            "policy": {"extraHosts": (host,) if host else (), "allowInsecureLoopback": True, "allowFile": False}}


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


def isIgnored(path):
    """Changes that do not affect the application: documentation, tests, repository files, release scripts."""
    parts = path.split("/")
    if parts[0] in ("Docs", ".github"):
        return True
    if "Testing" in parts[:-1]:
        return True
    if len(parts) == 1:
        name = parts[0]
        return name.endswith(".md") or name.startswith(".git") or name in (".pre-commit-config.yaml", ".codespellrc")
    if path.startswith(("Utilities/Scripts/SlicerStudioRelease/", "Utilities/Scripts/SlicerStudioMacOS/")):
        return True
    if len(parts) == 3 and parts[0] == "Utilities" and parts[1] == "Scripts" and parts[2].startswith("Studio") and "Check" in parts[2]:
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
    files stay valid). Raise ``CommitUpdateError`` when it holds a native binary."""
    if os.path.isdir(source) and not os.path.islink(source):
        for folder, _dirs, files in os.walk(source):
            for name in files:
                if _isNative(os.path.join(folder, name)):
                    raise CommitUpdateError(UNAVAILABLE, f"{os.path.basename(source)} holds compiled code")
        shutil.copytree(source, destination, symlinks=True, copy_function=shutil.copy2)
    else:
        if _isNative(source):
            raise CommitUpdateError(UNAVAILABLE, f"{os.path.basename(source)} is compiled code")
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copy2(source, destination, follow_symlinks=False)


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


def requestRollback(stateRoot, base, target=None):
    """Go back from the running version to ``target`` (a staged version, or None for the installed files) at the next
    start. The version gone back from is not offered again until main moves on."""

    def change(state):
        current = state.get("active")
        if current:
            studio_overlay.mark(state, "skipped", current)
        if target and studio_overlay.validVersion(baseDirectory(stateRoot, base), target, base):
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
    keep = set()

    def change(state):
        keep.update(value for value in (state.get("active"), state.get("previous"), state.get("pending")) if value)
        for boot in state.get("boots") or []:
            if isinstance(boot, dict) and studio_overlay.bootAlive(boot, now):
                keep.add(boot.get("version"))
        return False

    if os.path.isfile(os.path.join(baseDir, studio_overlay.STATE_FILE)):
        if studio_overlay.updateState(baseDir, base, change) is None:
            return removed
        versions = os.path.join(baseDir, studio_overlay.VERSIONS_FOLDER)
        try:
            names = os.listdir(versions)
        except OSError:
            names = []
        for name in names:
            if name not in keep:
                path = os.path.join(versions, name)
                shutil.rmtree(path, ignore_errors=True)
                removed.append(path)
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
        if now - used > UNUSED_BASE_S:
            shutil.rmtree(folder, ignore_errors=True)
            removed.append(folder)
    return removed


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
    if head in (state.get("active"), state.get("pending")) and studio_overlay.validVersion(baseDir, head, base):
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
        if candidate and blobIdOfFile(candidate) == blob:
            with open(candidate, "rb") as f:
                return f.read()
        url = self.endpoint["raw"].format(commit=self.head, path=urllib.parse.quote(src, safe="/"))
        data = _request(url, self.endpoint["policy"], BLOB_TIMEOUT_S, FETCH_DEADLINE_S, MAX_BLOB_BYTES, cancelled=self.cancelled)
        if git.blobId(data) != blob:
            raise VerificationError(f"{src} does not match commit {self.head[:7]}")
        self.total += len(data)
        self.downloaded += 1
        if self.total > MAX_TOTAL_BYTES:
            raise CommitUpdateError(TOO_LARGE, "the update is too large")
        return data


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
        # The units of each changed root: every top-level entry with a mapped (or added) file, and __pycache__
        units = {}
        for entry in buildFiles.map:
            if entry["root"] in changedRoots:
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
            if entry["root"] not in changedRoots or entry["dst"] in applied:
                continue
            src, dst = entry["src"], entry["dst"]
            target = os.path.join(filesDir, dst)
            headEntry = listing.get(src)
            if headEntry is None:
                continue
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
        try:
            os.rename(staging, final)
        except OSError:
            if studio_overlay.validVersion(baseDir, headId, base) is None:
                raise
            shutil.rmtree(staging, ignore_errors=True)  # another window staged the same commit first
        _fsyncFolder(versions)
    except CommitUpdateError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except OSError as error:
        shutil.rmtree(staging, ignore_errors=True)
        raise CommitUpdateError(FAILED, f"the update could not be staged: {error.strerror or error}")
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    def change(state):
        if headId in (state.get("bad") or []) or headId in (state.get("skipped") or []):
            return False
        if state.get("active") == headId:
            if state.get("pending"):
                state["pending"] = None
                return True
            return False
        state["pending"] = headId
        return True

    _update(stateRoot, base, change)
    return os.path.join(baseDir, studio_overlay.VERSIONS_FOLDER, headId)


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

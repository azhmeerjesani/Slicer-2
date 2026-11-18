"""Slicer Studio: applies a staged code update from ``main`` before any Slicer or Studio module is imported.

Slicer Studio picks up the Python and resource files of new commits on ``main`` between releases (see
``StudioLib/updates_commit.py``). A worker thread stages each update in a private folder of the user's data:

  ``<AppLocalDataLocation>/StudioUpdate/commit/<build commit>/versions/<commit>/files/<paths relative to the Slicer home>``

The installed application (and the signed macOS bundle) is never written. At the next start, ``slicerqt.py`` calls
``activateFromApplication()`` before its first import, and the active version is put in front of the installed files:

- its folders (for example ``lib/Slicer-X.Y/qt-scripted-modules`` and ``bin/Python``) are inserted into ``sys.path``
  just before the installed ones, so ``import StudioLib`` finds the updated package;
- each scripted module of the version (``Studio.py``, ...) is put into ``sys.modules`` as a lazily loaded module
  whose ``__file__`` is in the overlay. The module factory of Slicer looks in ``sys.modules`` first, so it uses the
  updated module, and code that finds its resources next to ``__file__`` finds them in the overlay too.

The state of the overlay is ``state.json`` (changed under ``state.lock``, written through a temporary file and
``os.replace``)::

    {"format": 1, "base": B, "active": H|null, "previous": H|null, "pending": H|"installed"|null,
     "disabled": false, "disabledReason": "setting"|"release", "disabledBefore": false,
     "bad": [H...], "skipped": [H...], "crashes": {"version": H, "count": n}|null,
     "boots": [{"version", "pid", "token", "startedAt", "confirmed", "settled", "exited": null|"early"|"clean"}],
     "rolledBack": {"from", "to", "at"}|null, "lastUsed": {H: time}, "activationError": text|null}

**Promotion.** A ``pending`` version (staged by the worker, or chosen by "Go back") becomes ``active`` at the next
start; the old one is kept as ``previous``. ``"installed"`` means the installed files again.

**Automatic rollback.** Every interactive start of an overlay is recorded in ``boots``. It is *confirmed* when, after
the start-up completed, every updated module that was used came from the overlay, the Studio module was created, and
Studio reported that its start-up steps (the interface and the update service, which must stay able to fetch a fix
from main) all ran (``reportStartup()``, called by ``Studio.py``). It is *settled* ``SETTLE_DELAY_MS`` later, and
*exited* when the application quits (``"early"`` when it quits before Studio finished starting, which says nothing about
the update). At the next start, every recorded start whose process is gone is judged:

- not confirmed (and not exited early): the start failed: its version is marked ``bad`` and the previous version (or
  the installed one) is used;
- confirmed but neither settled nor exited: it crashed within minutes; ``CRASH_LIMIT`` such crashes in a row of the
  active version also roll it back (a crash loop after the confirmation);
- a module of the update that fails while it loads rolls back at once (``_preseedFailed``): the rest of that session
  uses the installed files of that folder, and the next start uses the previous version.

Nothing is applied in a build tree or source checkout (the development channel), under ``--testing``, in the web
version, when the build information is missing or the build was made from changed files, when
``SLICER_STUDIO_COMMIT_UPDATES=off`` (or ``SLICER_STUDIO_UPDATE_FEED=off``) is set, or when the user turned the commit
updates off (``disabled``). A staged version is used only when it came from the official repository, or from the
loopback address that ``SLICER_STUDIO_COMMIT_UPDATES`` sets for tests (only while it is set).

This file runs before the overlay can apply, so it is never part of one (a change to it needs a new build), and it
uses the Python standard library only. The start-up cost is a stat when there is no overlay; with one, two small JSON
files are read and one is written (no subprocess).
"""

import json
import os
import sys
import time

FORMAT = 1
#: The repository whose main branch the code updates come from (git smart HTTP)
OFFICIAL_GIT_URL = "https://github.com/azhmeerjesani/Slicer-2.git"
#: Environment variable of the commit updates: "off" turns them off; an http(s) address on this computer (loopback)
#: replaces the repository address (tests). Any other value is ignored.
ENVIRONMENT_VARIABLE = "SLICER_STUDIO_COMMIT_UPDATES"
FEED_ENVIRONMENT_VARIABLE = "SLICER_STUDIO_UPDATE_FEED"
#: Written at build time next to this file (bin/Python), installed with it
BUILD_INFO_NAME = "studio_build_info.json"
BUILD_INFO_FOLDER = "bin/Python"
STATE_FILE = "state.json"
LOCK_FILE = "state.lock"
VERSIONS_FOLDER = "versions"
STAGING_FOLDER = "staging"
MANIFEST_FILE = "manifest.json"
FILES_FOLDER = "files"
#: The start-up waits at most this long for the state lock (another window that starts at the same time)
LOCK_TIMEOUT_S = 1.0
#: The state lock is waited for this long when the application quits (the outcome of the start is written then)
QUIT_LOCK_TIMEOUT_S = 2.0
#: The start is checked this long after the start-up completed (ms)
HEALTH_DELAY_MS = 5000
#: A confirmed start is settled this long after the confirmation (ms): a crash before that counts towards CRASH_LIMIT
SETTLE_DELAY_MS = 10 * 60 * 1000
#: Crashes in a row (confirmed, not settled, not exited) of the active version that roll it back
CRASH_LIMIT = 2
#: The confirmation is written in a worker thread; it tries this many times (each waits up to 5 s for the lock)
CONFIRM_ATTEMPTS = 6
#: An unconfirmed start whose process start time is unknown (macOS) is taken as gone after this long (seconds)
UNCONFIRMED_WITHOUT_TOKEN_S = 600
#: Most entries kept in "bad" and "skipped"
MAX_MARKED = 50
#: Files that run before an update can apply (bin/Python): never taken from an overlay
BOOTSTRAP_MODULES = ("studio_overlay", "slicerqt", "studio_profile")

_OFF_VALUES = ("off", "0", "none", "false", "no")
_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")

#: What the activation of this process did (``status()``)
_status = {"state": "inactive", "reason": "not started"}
_buildInfo = None
#: (installed root as a real path, installed root as given, overlay root) of each overlaid folder
_roots = []
#: The modules put into sys.modules by the activation: name -> module
_preseeded = {}
#: (base folder, version, pid) of the start recorded in "boots"
_boot = None
#: (base folder, base, version) of the version applied to this process
_applied = None
#: sys.path and sys.modules of the activation (the real ones, except in the tests)
_sysPath = None
_sysModules = None
#: What the start-up steps reported (``reportStartup()``): name -> (ok, reason)
_startupReports = {}
#: The health monitor of this start (the application's main thread)
_monitor = None


class ActivationError(Exception):
    pass


#
# Small helpers (also used by StudioLib.updates_commit)
#


def isTurnedOff(env=None):
    """True when the environment turns the commit updates off."""
    env = os.environ if env is None else env
    for name in (ENVIRONMENT_VARIABLE, FEED_ENVIRONMENT_VARIABLE):
        if str(env.get(name, "")).strip().lower() in _OFF_VALUES:
            return True
    return False


def isLoopbackUrl(value):
    """True for an http(s) address on this computer (127.0.0.1, ::1, localhost)."""
    import urllib.parse

    try:
        parts = urllib.parse.urlsplit(str(value))
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and host in _LOOPBACK_HOSTS and not parts.username and not parts.password


def overrideUrl(env=None):
    """The repository address that ``SLICER_STUDIO_COMMIT_UPDATES`` sets for tests (a loopback address, without the
    final slash), or None. Values that are not a loopback address ("on", "1", another host) are ignored."""
    env = os.environ if env is None else env
    if isTurnedOff(env):
        return None
    value = str(env.get(ENVIRONMENT_VARIABLE, "")).strip()
    return value.rstrip("/") if value and isLoopbackUrl(value) else None


def sourceAllowed(manifest, env=None):
    """True when the staged version ``manifest`` came from the official repository, or from the test address set now."""
    source = manifest.get("source")
    if source == OFFICIAL_GIT_URL:
        return True
    return isinstance(source, str) and bool(source) and source.rstrip("/") == overrideUrl(env)


def isCommitId(text):
    return isinstance(text, str) and len(text) == 40 and all(c in "0123456789abcdef" for c in text)


def isoTime(seconds=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if seconds is None else seconds))


def readJson(path):
    """The JSON object in ``path``, or None when it cannot be read or is not an object."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def writeJsonAtomic(path, data):
    """Write ``data`` to ``path`` through a temporary file in the same folder and ``os.replace`` (never half written)."""
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(temporary, path)


def readBuildInfo(home):
    """The build information of the installation in ``home`` (``bin/Python/studio_build_info.json``), or None."""
    info = readJson(os.path.join(home, BUILD_INFO_FOLDER, BUILD_INFO_NAME))
    if info is None or info.get("format") != FORMAT:
        return None
    commit = info.get("commit")
    if commit is not None and not isCommitId(commit):
        return None
    return info


def buildInfo():
    """The build information read by the activation of this process (None in a build tree, or when it is missing)."""
    return _buildInfo


def baseDirectory(stateRoot, base):
    return os.path.join(stateRoot, base)


def versionDirectory(baseDir, version):
    return os.path.join(baseDir, VERSIONS_FOLDER, version)


def newState(base):
    return {"format": FORMAT, "base": base, "active": None, "previous": None, "pending": None, "disabled": False,
            "bad": [], "skipped": [], "crashes": None, "boots": [], "rolledBack": None, "lastUsed": {}, "activationError": None}


def readState(baseDir):
    state = readJson(os.path.join(baseDir, STATE_FILE))
    if state is None or state.get("format") != FORMAT:
        return None
    for key, value in newState(state.get("base")).items():
        if key not in state or not isinstance(state[key], type(value)) and value is not None:
            state[key] = value
    return state


def writeState(baseDir, state):
    writeJsonAtomic(os.path.join(baseDir, STATE_FILE), state)


def readManifest(baseDir, version):
    return readJson(os.path.join(versionDirectory(baseDir, version), MANIFEST_FILE))


def validVersion(baseDir, version, base, env=None):
    """The manifest of the staged ``version`` when it can be used on the build ``base`` (and came from an allowed
    address, see ``sourceAllowed``), else None."""
    if not isCommitId(version):
        return None
    manifest = readManifest(baseDir, version)
    if manifest is None or manifest.get("format") != FORMAT or manifest.get("base") != base or manifest.get("commit") != version:
        return None
    if not isinstance(manifest.get("roots"), list) or not isinstance(manifest.get("modules", {}), dict):
        return None
    if not sourceAllowed(manifest, env):
        return None
    return manifest


def mark(state, key, version):
    values = [value for value in state.get(key) or [] if value != version]
    values.append(version)
    state[key] = values[-MAX_MARKED:]


class StateLock:
    """The lock of a state folder (``fcntl.flock`` on ``state.lock``). ``acquired`` is False when another process holds
    it for longer than ``timeout`` seconds (or the lock file cannot be opened): the state must then only be read. On a
    file system without locks (some network homes) ``acquired`` is True and ``unlocked`` is True: the state is changed
    without a lock, as on Windows."""

    def __init__(self, baseDir, timeout):
        self.baseDir = baseDir
        self.timeout = timeout
        self.file = None
        self.acquired = False
        self.unlocked = False

    def __enter__(self):
        import errno

        try:
            import fcntl
        except ImportError:  # Windows: no lock (Slicer Studio packages are for macOS and Linux)
            self.acquired = self.unlocked = True
            return self
        try:
            os.makedirs(self.baseDir, exist_ok=True)
            self.file = open(os.path.join(self.baseDir, LOCK_FILE), "a+")
        except OSError:
            return self
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.acquired = True
                return self
            except OSError as error:
                if error.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES, errno.EINTR):
                    # No locks on this file system (ENOLCK, EOPNOTSUPP, ...): go on without one
                    self.acquired = self.unlocked = True
                    return self
                if time.monotonic() >= deadline:
                    return self
                time.sleep(0.02)

    def __exit__(self, *args):
        if self.file is not None:
            try:
                if self.acquired and not self.unlocked:
                    import fcntl

                    fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            self.file.close()
            self.file = None
        return False


def updateState(baseDir, base, change, timeout=10.0):
    """Read the state of ``baseDir`` under its lock (a new state when there is none), call ``change(state)`` and write
    the state when it returns True. Return the state, or None when the lock could not be taken."""
    with StateLock(baseDir, timeout) as lock:
        if not lock.acquired:
            return None
        state = readState(baseDir) or newState(base)
        if state.get("base") != base:
            state = newState(base)
        if change(state):
            writeState(baseDir, state)
        return state


def processAlive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists, but belongs to another user
    return True


def processStartToken(pid):
    """When the process ``pid`` started (a text that changes when the system gives the pid to another process), or None
    where ``/proc`` is not available (macOS: no subprocess is started for it, this runs on the start-up path)."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
            return f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def bootAlive(boot, now=None):
    """True while the process of the recorded start ``boot`` still runs (a start that recorded its exit is gone)."""
    if boot.get("exited"):
        return False
    pid = boot.get("pid")
    if type(pid) is not int or pid <= 0 or not processAlive(pid):
        return False
    token = boot.get("token")
    if token:
        current = processStartToken(pid)
        return current is None or current == token
    if boot.get("confirmed"):
        return True
    # Without a start time (macOS), a pid may have been given to another process: an unconfirmed start that is this old
    # is taken as gone (a start is confirmed seconds after the start-up)
    started = boot.get("startedAt")
    now = time.time() if now is None else now
    return isinstance(started, (int, float)) and now - started < UNCONFIRMED_WITHOUT_TOKEN_S


def bootOutcome(boot):
    """How the recorded start ``boot``, whose process is gone, ended: "failed" (not confirmed), "crashed" (confirmed,
    then gone within ``SETTLE_DELAY_MS`` without quitting), "neutral" (quit before Studio finished starting) or "ok"."""
    if not boot.get("confirmed"):
        return "neutral" if boot.get("exited") == "early" else "failed"
    if boot.get("settled") or boot.get("exited"):
        return "ok"
    return "crashed"


def rollBack(state, baseDir, base, failed, now=None, env=None):
    """Change ``state``: the version ``failed`` is marked bad, and the previous version (when it can be used) or the
    installed files are used from the next start. Return the ``rolledBack`` record."""
    previous = state.get("previous")
    target = None
    if previous and previous != failed and previous not in (state.get("bad") or []) and validVersion(baseDir, previous, base, env):
        target = previous
    mark(state, "bad", failed)
    if state.get("active") == failed:
        state["active"] = target
        state["previous"] = None
    if state.get("pending") == failed:
        state["pending"] = None
    state["crashes"] = None
    state["rolledBack"] = {"from": failed, "to": target or "installed", "at": isoTime(now)}
    return dict(state["rolledBack"])


#
# Activation
#


def _result(state, **values):
    result = {"state": state, "reason": None, "base": None, "active": None, "overlay": None, "promoted": None,
              "rolledBack": None, "error": None, "modules": [], "roots": []}
    result.update(values)
    return result


def _isUnder(path, folder):
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def _prepare(home, filesDir, manifest, sysPath, sysModules):
    """Check that ``manifest`` can be activated now; return the sys.path insertions and the module files."""
    roots = [root for root in manifest.get("roots") or [] if isinstance(root, str)]
    units = manifest.get("units") or {}
    modules = {name: dst for name, dst in (manifest.get("modules") or {}).items() if isinstance(name, str) and isinstance(dst, str)}
    for root in roots:
        if root.startswith("/") or ".." in root.split("/"):
            raise ActivationError(f"unsafe folder {root}")
        for unit in units.get(root) or []:
            name = unit[:-3] if unit.endswith(".py") else unit
            if name in BOOTSTRAP_MODULES:
                continue  # runs before the update applies: always the installed file (never staged since)
            if name.isidentifier() and name in sysModules:
                raise ActivationError(f"{name} was imported before the update could apply")
    for name in modules:
        if name in sysModules:
            raise ActivationError(f"{name} was imported before the update could apply")
    homeReal = os.path.realpath(home)
    insertions = []
    realEntries = [os.path.realpath(entry) if isinstance(entry, str) and entry else None for entry in sysPath]
    for root in roots:
        installed = os.path.normpath(os.path.join(home, root))
        installedReal = os.path.realpath(installed)
        overlay = os.path.join(filesDir, root)
        if not os.path.isdir(overlay):
            raise ActivationError(f"the folder {root} of the update is missing")
        indexes = [index for index, real in enumerate(realEntries) if real == installedReal]
        if not indexes:
            first = next((index for index, real in enumerate(realEntries) if real and _isUnder(real, homeReal)), len(sysPath))
            indexes = [first]
        insertions.append((indexes, overlay, installedReal, installed))
    moduleFiles = {}
    for name, dst in modules.items():
        path = os.path.join(filesDir, dst)
        if name.isidentifier() and os.path.isfile(path):
            root = next((overlay for _indexes, overlay, _real, _installed in insertions if _isUnder(path, overlay)), None)
            moduleFiles[name] = (path, root)
    return insertions, moduleFiles


class _CheckedLoader:
    """The loader of a pre-seeded scripted module: when the module of the update fails while it loads (lazily, at its
    first use), the update is rolled back at once (``_preseedFailed``) before the error goes on. Everything else is the
    file loader's."""

    def __init__(self, loader, name, root):
        self._loader = loader
        self._name = name
        self._root = root

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        try:
            self._loader.exec_module(module)
        except Exception as error:
            try:
                _preseedFailed(self._name, self._root, error)
            except Exception as failure:
                print(f"Slicer Studio code update: the rollback failed ({failure})", file=sys.stderr)
            raise

    def __getattr__(self, name):
        return getattr(self._loader, name)


def _apply(insertions, moduleFiles, sysPath, sysModules):
    """Insert the overlay folders into sys.path and pre-seed the scripted modules (all or nothing)."""
    import importlib.util

    added = []
    roots = []
    for indexes, overlay, installedReal, installed in insertions:
        roots.append((installedReal, installed, overlay))
    seeded = {}
    try:
        allIndexes = sorted({(index, overlay) for indexes, overlay, _real, _installed in insertions for index in indexes}, reverse=True)
        for index, overlay in allIndexes:
            sysPath.insert(index, overlay)
            added.append(overlay)
        for name, (path, root) in sorted(moduleFiles.items()):
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                raise ActivationError(f"cannot load {name}")
            loader = importlib.util.LazyLoader(_CheckedLoader(spec.loader, name, root))
            spec.loader = loader
            module = importlib.util.module_from_spec(spec)
            sysModules[name] = module
            seeded[name] = module
            loader.exec_module(module)  # lazy: the file runs on the first use of the module
    except BaseException:
        for overlay in added:
            try:
                sysPath.remove(overlay)
            except ValueError:
                pass
        for name, module in seeded.items():
            if sysModules.get(name) is module:
                del sysModules[name]
        raise
    return roots, seeded


def _isLazy(module):
    return type(module).__name__ == "_LazyModule"


def _moduleFile(module):
    """``__file__`` of ``module`` without loading a lazy module."""
    try:
        return object.__getattribute__(module, "__dict__").get("__file__") or ""
    except Exception:
        return ""


def _findBoot(state, version, pid):
    """The latest record of the start of ``pid`` with ``version``."""
    for boot in reversed(state.get("boots") or []):
        if isinstance(boot, dict) and boot.get("pid") == pid and boot.get("version") == version:
            return boot
    return None


def _newBoot(version, pid, token, startedAt):
    return {"version": version, "pid": pid, "token": token, "startedAt": startedAt, "confirmed": False, "settled": False, "exited": None}


def _judgeBoots(state, baseDir, base, now, env, result):
    """The automatic rollback: judge the recorded starts whose process is gone (``bootOutcome``). Return True when the
    state changed."""
    changed = False
    alive, dead = [], []
    for boot in state.get("boots") or []:
        if not isinstance(boot, dict):
            changed = True
        elif bootAlive(boot, now):
            alive.append(boot)
        else:
            dead.append(boot)
            changed = True
    dead.sort(key=lambda boot: boot.get("startedAt") if isinstance(boot.get("startedAt"), (int, float)) else 0)
    for boot in dead:
        version = boot.get("version")
        if not version or version != state.get("active"):
            continue
        outcome = bootOutcome(boot)
        crashes = state.get("crashes") if isinstance(state.get("crashes"), dict) else None
        if outcome == "failed":
            result["rolledBack"] = rollBack(state, baseDir, base, version, now, env)
        elif outcome == "crashed":
            count = (crashes.get("count", 0) if crashes and crashes.get("version") == version else 0) + 1
            if count >= CRASH_LIMIT:
                result["rolledBack"] = rollBack(state, baseDir, base, version, now, env)
            else:
                state["crashes"] = {"version": version, "count": count}
        elif outcome == "ok" and crashes:
            state["crashes"] = None
    state["boots"] = alive
    return changed


def activate(home, stateRoot, runInfo, env=None, sysPath=None, sysModules=None, now=None):
    """Apply the staged or active code update of the installation in ``home`` to this process, before any Slicer or
    Studio module is imported. ``runInfo``: ``installed``, ``testing``, ``interactive`` (a normal start with the main
    window, where the start is recorded for the automatic rollback), ``pid`` and ``token`` (its start time).

    Return the status (also kept for ``status()``). Never raises for a problem of the update: the installed code is
    then used.
    """
    global _status, _buildInfo, _roots, _preseeded, _boot, _applied, _sysPath, _sysModules
    env = os.environ if env is None else env
    sysPath = sys.path if sysPath is None else sysPath
    sysModules = sys.modules if sysModules is None else sysModules
    now = time.time() if now is None else now
    _roots, _preseeded, _boot, _applied = [], {}, None, None
    _sysPath, _sysModules = sysPath, sysModules
    _startupReports.clear()
    if isTurnedOff(env):
        _status = _result("inactive", reason="environment")
        return _status
    if not runInfo.get("installed"):
        _status = _result("inactive", reason="development")
        return _status
    if runInfo.get("testing"):
        _status = _result("inactive", reason="testing")
        return _status
    if env.get("SLICER_STUDIO_WEB") == "1":
        _status = _result("inactive", reason="web")
        return _status
    info = readBuildInfo(home)
    _buildInfo = info
    if info is None or info.get("dirty") or not info.get("commit"):
        _status = _result("inactive", reason="build")
        return _status
    base = info["commit"]
    baseDir = baseDirectory(stateRoot, base)
    if not os.path.exists(os.path.join(baseDir, STATE_FILE)):
        _status = _result("inactive", reason="none", base=base)
        return _status

    result = _result("inactive", base=base)
    with StateLock(baseDir, LOCK_TIMEOUT_S) as lock:
        state = readState(baseDir)
        if state is None or state.get("base") != base:
            result["reason"] = "unreadable"
            _status = result
            return _status
        changed = False
        if state.get("disabled"):
            result["reason"] = "disabled"
            _status = result
            return _status
        if lock.acquired:
            changed = _judgeBoots(state, baseDir, base, now, env, result) or changed
            # Promotion of a pending version
            pending = state.get("pending")
            if pending == "installed":
                state["active"], state["previous"], state["pending"] = None, None, None
                state["crashes"] = None
                changed = True
            elif pending:
                if pending not in state["bad"] and validVersion(baseDir, pending, base, env):
                    old = state.get("active")
                    if old != pending:
                        state["previous"] = old if old and old not in state["skipped"] else None
                        state["active"] = pending
                        state["crashes"] = None
                        result["promoted"] = pending
                state["pending"] = None
                changed = True
        version = state.get("active")
        if version:
            manifest = validVersion(baseDir, version, base, env)
            try:
                if manifest is None:
                    raise ActivationError("the update is missing or damaged, or comes from another address")
                filesDir = os.path.join(versionDirectory(baseDir, version), FILES_FOLDER)
                insertions, moduleFiles = _prepare(home, filesDir, manifest, sysPath, sysModules)
                _roots, _preseeded = _apply(insertions, moduleFiles, sysPath, sysModules)
                _applied = (baseDir, base, version)
                result.update(state="active", active=version, overlay=filesDir, modules=sorted(_preseeded),
                              roots=[root for root in manifest.get("roots") or []])
                if state.get("activationError"):
                    state["activationError"] = None
                    changed = True
            except Exception as error:
                result["error"] = str(error) or error.__class__.__name__
                result["reason"] = "error"
                if lock.acquired:
                    state["activationError"] = {"version": version, "error": result["error"], "at": isoTime(now)}
                    if manifest is None:
                        state["active"] = None
                    changed = True
        if result["state"] == "active" and runInfo.get("interactive"):
            pid = int(runInfo.get("pid") or os.getpid())
            _boot = (baseDir, version, pid)
            if lock.acquired:
                state["boots"].append(_newBoot(version, pid, runInfo.get("token"), now))
                changed = True
            else:
                # Another window holds the lock: the start is recorded by the health monitor (recordBoot())
                result["bootDeferred"] = {"token": runInfo.get("token"), "startedAt": now}
        if changed and lock.acquired:
            try:
                writeState(baseDir, state)
            except OSError as error:
                result["error"] = result["error"] or f"cannot write the update state: {error}"
    _status = result
    return _status


def status():
    """What the activation of this process did: ``state`` ("active" or "inactive"), ``reason``, ``base``, ``active``
    (the commit whose code runs), ``overlay`` (its folder), ``promoted`` (a version first used by this start),
    ``rolledBack`` ({from, to, at} when this start went back), ``error``, ``modules`` and ``roots``; later ``confirmed``,
    ``healthError`` and ``loadFailed`` ({version, module, error}: a module of the update failed and the update was
    rolled back during this session)."""
    return dict(_status)


def mapPath(path):
    """``path`` of an installed file inside an overlaid folder, mapped to the same file in the active overlay when its
    top-level package, module or folder is in the overlay (for example the path of a scripted module, whose resources
    are looked up next to it). A file that the update deleted maps to its missing path in the overlay."""
    if not _roots or not path:
        return path
    try:
        candidates = [os.path.normpath(path)]
        real = os.path.realpath(path)
        if real not in candidates:
            candidates.append(real)
        for installedReal, installed, overlay in _roots:
            for candidate in candidates:
                for root in (installed, installedReal):
                    if candidate != root and _isUnder(candidate, root):
                        relative = os.path.relpath(candidate, root)
                        unit = relative.split(os.sep, 1)[0]
                        mapped = os.path.join(overlay, relative)
                        return mapped if os.path.lexists(os.path.join(overlay, unit)) else path
    except (OSError, ValueError):
        pass
    return path


def _preseedFailed(name, overlayRoot, error, now=None):
    """A scripted module of the update failed while it loaded. Slicer then loads the installed file of the module: so
    that it does not run with the updated packages of the same folder (a mix of two commits), that overlay folder is
    taken out of sys.path and its modules that are not in use are forgotten. The update is rolled back at once (marked
    bad; the previous version or the installed files from the next start), and this start is not confirmed."""
    global _roots, _boot
    text = f"{name} failed to load ({error.__class__.__name__}: {error})"
    _status["healthError"] = text
    version = _applied[2] if _applied else None
    _status["loadFailed"] = {"version": version, "module": name, "error": text}
    print(f"Slicer Studio code update {(version or '')[:7]}: {text}; the installed files are used.", file=sys.stderr)
    sysPath = _sysPath if _sysPath is not None else sys.path
    sysModules = _sysModules if _sysModules is not None else sys.modules
    if overlayRoot:
        while overlayRoot in sysPath:
            sysPath.remove(overlayRoot)
        _roots = [entry for entry in _roots if entry[2] != overlayRoot]
        for moduleName, module in list(sysModules.items()):
            if module is None:
                continue
            if moduleName != name and _preseeded.get(moduleName) is module and not _isLazy(module):
                continue  # a scripted module of the update that Slicer already uses
            filePath = _moduleFile(module)
            if filePath and _isUnder(os.path.normpath(filePath), overlayRoot):
                del sysModules[moduleName]
    if sysModules.get(name) is _preseeded.get(name):
        sysModules.pop(name, None)
    if _applied is None:
        return
    baseDir, base, version = _applied
    pid = _boot[2] if _boot else os.getpid()

    def change(state):
        if state.get("active") != version:
            return False
        rollBack(state, baseDir, base, version, now)
        state["boots"] = [boot for boot in state.get("boots") or [] if not (isinstance(boot, dict) and boot.get("pid") == pid)]
        state["activationError"] = {"version": version, "error": text, "at": isoTime(now)}
        return True

    try:
        updateState(baseDir, base, change, timeout=QUIT_LOCK_TIMEOUT_S)
    except OSError as writeError:
        print(f"Slicer Studio code update: cannot write the update state: {writeError}", file=sys.stderr)
    _boot = None


#
# Health of the start (main thread of the application)
#


def reportStartup(name, ok, reason=None):
    """Called by the start-up of Studio (``Studio.py``) when its steps ran (``ok``) or one failed. A start is confirmed
    only when Studio reported success; a failure keeps it unconfirmed (it is rolled back at the next start)."""
    _startupReports[name] = (bool(ok), reason)
    if _monitor is not None:
        _monitor.onReport()


def health(app=None):
    """("ok" | "pending" | "failed", reason) of this start: every pre-seeded module that was used came from the overlay,
    no module of the update failed, the Studio module was loaded and created, and Studio reported a successful start-up
    (pending until it reports). Whatever the update changed (scripted modules or only bin/Python), Studio's report is
    needed."""
    if _status.get("healthError"):
        return "failed", _status["healthError"]
    overlay = _status.get("overlay") or ""
    sysModules = _sysModules if _sysModules is not None else sys.modules
    for name, module in _preseeded.items():
        current = sysModules.get(name)
        if current is None:
            continue
        if current is module and _isLazy(current):
            continue  # never used (the module is not loaded in this run)
        if current is not module or not _isUnder(os.path.normpath(_moduleFile(current)), os.path.normpath(overlay)):
            return "failed", f"{name} was loaded from the installed files"
    factory = None
    if app is not None:
        try:
            factory = app.moduleManager().factoryManager()
        except Exception:
            factory = None
    if factory is not None:
        # Slicer Studio always has the Studio module (a start that ignores it is not recorded, see _runInfo): when it
        # is missing, the scripted modules could not load (for example an updated slicer package that fails)
        try:
            if not factory.isRegistered("Studio"):
                return "failed", "the Studio module is not loaded"
            if not factory.isInstantiated("Studio"):
                return "failed", "the Studio module could not be created"
        except Exception:
            pass
    report = _startupReports.get("Studio")
    if report is None:
        return "pending", "Studio has not finished starting"
    if not report[0]:
        return "failed", f"Studio could not start ({report[1] or 'a start-up step failed'})"
    return "ok", None


def finishBoot(confirm=False, settled=False, exited=None, now=None, timeout=5.0):
    """Write the outcome of this start into its record (created when the activation could not record it): confirmed,
    settled, exited ("early" or "clean"). Return True when it was written."""
    global _boot
    if _boot is None:
        return False
    baseDir, version, pid = _boot
    now = time.time() if now is None else now
    token = processStartToken(pid)
    deferred = _status.get("bootDeferred") or {}

    def change(state):
        boot = _findBoot(state, version, pid)
        if boot is None:
            boot = _newBoot(version, pid, deferred.get("token") or token, deferred.get("startedAt") or now)
            state.setdefault("boots", []).append(boot)
        if confirm and not boot.get("confirmed"):
            boot["confirmed"] = True
            boot["confirmedAt"] = now
            lastUsed = state.get("lastUsed") if isinstance(state.get("lastUsed"), dict) else {}
            lastUsed[version] = isoTime(now)
            state["lastUsed"] = lastUsed
        if token and not boot.get("token"):
            boot["token"] = token
        if settled:
            boot["settled"] = True
        if exited and not boot.get("exited"):
            boot["exited"] = exited
        return True

    try:
        result = updateState(baseDir, _status.get("base") or "", change, timeout=timeout)
    except OSError:
        return False
    if result is None:
        return False
    if confirm:
        _status["confirmed"] = True
    if exited:
        _boot = None
    return True


def confirmStart(healthy=True, now=None):
    """Mark the recorded start of this process as confirmed (it started well). Return True when it was written."""
    if not healthy:
        return False
    return finishBoot(confirm=True, now=now)


def recordBoot(now=None):
    """Record this start when the activation could not take the lock (another window was starting)."""
    return finishBoot(now=now, timeout=30.0)


class HealthMonitor:
    """Judges this start: ``HEALTH_DELAY_MS`` after the start-up completed (and again when Studio reports), the start is
    confirmed when ``health()`` is "ok"; ``SETTLE_DELAY_MS`` later it is settled; at the exit its outcome is written
    (synchronously, so that it is never lost). ``startTimer(ms, callback)`` starts a single-shot timer (Qt in the
    application, direct calls in the tests); ``startThread(target)`` runs ``target`` in a worker thread."""

    def __init__(self, app=None, startTimer=None, startThread=None):
        self.app = app
        self.startTimer = startTimer
        self.startThread = startThread or self._thread
        self.completed = False
        self.delayPassed = False
        self.verdict = None  # "ok" or "failed" once decided
        self.thread = None
        self.timers = []
        self.quitting = False

    @staticmethod
    def _thread(target):
        import threading

        # Not a daemon thread: the interpreter waits for it at the exit, so the write is never cut off
        thread = threading.Thread(target=target, name="StudioOverlayConfirm", daemon=False)
        thread.start()
        return thread

    def _after(self, ms, callback):
        if self.startTimer is not None:
            self.timers.append(self.startTimer(ms, callback))

    def onCompleted(self):
        self.completed = True
        self._after(HEALTH_DELAY_MS, self.onDelay)

    def onDelay(self):
        self.delayPassed = True
        self.check()

    def onReport(self):
        if self.delayPassed:
            self.check()

    def check(self):
        """Decide when the start is healthy or failed; wait while it is pending (Studio still starting)."""
        if self.verdict is not None:
            return self.verdict
        verdict, reason = health(self.app)
        if verdict == "pending":
            return verdict
        self.verdict = verdict
        if verdict == "failed":
            _status["healthError"] = reason
            print(f"Slicer Studio code update {(_status.get('active') or '')[:7]}: {reason}; the next start goes back.", file=sys.stderr)
            return verdict
        self.thread = self.startThread(self._confirm)
        self._after(SETTLE_DELAY_MS, self.onSettle)
        return verdict

    def _confirm(self):
        for _attempt in range(CONFIRM_ATTEMPTS):
            if self.quitting or _boot is None or confirmStart(True):
                return
            time.sleep(1.0)

    def onSettle(self):
        if _boot is not None and self.verdict == "ok":
            self.startThread(lambda: finishBoot(settled=True))

    def onQuit(self):
        """The application quits: write the outcome of the start (the confirmation thread is waited for)."""
        self.quitting = True
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(6.0)
        if _boot is None:
            return
        if not self.completed:
            finishBoot(exited="early", timeout=QUIT_LOCK_TIMEOUT_S)
            return
        verdict = self.verdict or health(self.app)[0]
        if verdict == "ok":
            finishBoot(confirm=True, exited="clean", timeout=QUIT_LOCK_TIMEOUT_S)
        elif verdict == "pending":
            # Quit before Studio finished starting (for example from a start-up dialog): says nothing about the update
            finishBoot(exited="early", timeout=QUIT_LOCK_TIMEOUT_S)
        else:
            finishBoot(exited="clean", timeout=QUIT_LOCK_TIMEOUT_S)  # unconfirmed: rolled back at the next start


def _startQtTimer(ms, callback):
    import qt

    timer = qt.QTimer()
    timer.setSingleShot(True)
    timer.setInterval(int(ms))
    timer.connect("timeout()", callback)
    timer.start()
    return timer


def _scheduleHealthCheck(app):
    """Connect the health monitor of this start to the application."""
    global _monitor
    _monitor = HealthMonitor(app, _startQtTimer)
    if _status.get("bootDeferred"):
        _monitor.startThread(recordBoot)
    app.connect("startupCompleted()", _monitor.onCompleted)
    app.connect("aboutToQuit()", _monitor.onQuit)


def _runInfo(app, env):
    def value(obj, name, default=None):
        try:
            attribute = getattr(obj, name)
        except Exception:
            return default
        try:
            return attribute() if callable(attribute) else attribute
        except Exception:
            return default

    options = value(app, "commandOptions")
    interactive = options is not None
    testing = False
    if options is not None:
        # testingEnabled is a property of the command options; slicer.app.testingEnabled() is a decorator that exists
        # only once "import slicer" ran (after the activation)
        testing = bool(value(options, "testingEnabled", False))
        for name in ("noMainWindow", "exitAfterStartup", "runPythonAndExit", "disableScriptedLoadableModules",
                     "disableBuiltInScriptedLoadableModules", "disableModules", "disableBuiltInModules"):
            if value(options, name, False):
                interactive = False
        if value(options, "pythonScript", "") or value(options, "extraPythonScript", ""):
            interactive = False
        ignored = value(options, "modulesToIgnore", []) or []
        if "Studio" in list(ignored):
            interactive = False
    testing = testing or bool(value(app, "testingEnabled", False))
    pid = os.getpid()
    return {"installed": bool(value(app, "isInstalled", False)), "testing": testing,
            "interactive": interactive and not testing, "pid": pid, "token": processStartToken(pid)}


def stateRootFromApplication():
    """``<profile>/StudioUpdate/commit`` (the same folder as ``StudioLib.updates.stateDirectory()``): the user profile
    folder (``studio_profile.root()``), whose default is the private application data folder."""
    try:
        import studio_profile
    except ImportError:  # a build without the user profile
        studio_profile = None
    if studio_profile is not None:
        base = studio_profile.root()
    else:
        import qt

        base = qt.QStandardPaths.writableLocation(qt.QStandardPaths.AppLocalDataLocation)
    return os.path.join(base, "StudioUpdate", "commit")


def activateFromApplication():
    """Called by ``slicerqt.py`` before its first import: activate the code update of this installation, if any."""
    import __main__

    app = getattr(__main__, "_qSlicerCoreApplicationInstance", None)
    if app is None or isTurnedOff():
        return status()
    runInfo = _runInfo(app, os.environ)
    if not runInfo["installed"] or runInfo["testing"]:
        activate("", "", runInfo)
        return status()
    home = str(app.slicerHome)
    stateRoot = stateRootFromApplication()
    result = activate(home, stateRoot, runInfo)
    if result.get("error"):
        print(f"Slicer Studio code update not applied: {result['error']}", file=sys.stderr)
    if _boot is not None:
        try:
            _scheduleHealthCheck(app)
        except Exception as error:
            print(f"Slicer Studio code update: the start-up check is not available ({error})", file=sys.stderr)
    return status()

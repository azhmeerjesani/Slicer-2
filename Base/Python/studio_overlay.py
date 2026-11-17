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
     "bad": [H...], "skipped": [H...], "boots": [{"version", "pid", "token", "startedAt", "confirmed"}],
     "rolledBack": {"from", "to", "at"}|null, "lastUsed": {H: time}, "activationError": text|null}

**Promotion.** A ``pending`` version (staged by the worker, or chosen by "Go back") becomes ``active`` at the next
start; the old one is kept as ``previous``. ``"installed"`` means the installed files again.

**Automatic rollback.** Every interactive start of an overlay is recorded in ``boots``; it is confirmed about 5 s after
the start-up completed when every updated module loaded from the overlay. A start that is not confirmed and whose
process is gone failed: its version is marked ``bad`` and the previous version (or the installed one) is used.

Nothing is applied in a build tree or source checkout (the development channel), under ``--testing``, in the web
version, when the build information is missing or the build was made from changed files, when
``SLICER_STUDIO_COMMIT_UPDATES=off`` (or ``SLICER_STUDIO_UPDATE_FEED=off``) is set, or when the user turned the commit
updates off (``disabled``).

This file runs before the overlay can apply, so it is never part of one (a change to it needs a new build), and it
uses the Python standard library only. The start-up cost is a stat when there is no overlay; with one, two small JSON
files are read and one is written.
"""

import json
import os
import sys
import time

FORMAT = 1
#: Environment variable of the commit updates: "off" turns them off; a URL replaces the repository address (tests)
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
#: The start is confirmed this long after the start-up completed (ms)
HEALTH_DELAY_MS = 5000
#: An unconfirmed start whose process start time is unknown is taken as failed after this long (seconds)
UNCONFIRMED_WITHOUT_TOKEN_S = 600
#: Most entries kept in "bad" and "skipped"
MAX_MARKED = 50

_OFF_VALUES = ("off", "0", "none", "false", "no")

#: What the activation of this process did (``status()``)
_status = {"state": "inactive", "reason": "not started"}
_buildInfo = None
#: (installed root as a real path, installed root as given, overlay root) of each overlaid folder
_roots = []
#: The modules put into sys.modules by the activation: name -> module
_preseeded = {}
#: (base folder, version, pid) of the start recorded in "boots"
_boot = None
_healthTimer = None
_callbacks = []


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
            "bad": [], "skipped": [], "boots": [], "rolledBack": None, "lastUsed": {}, "activationError": None}


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


def validVersion(baseDir, version, base):
    """The manifest of the staged ``version`` when it can be used on the build ``base``, else None."""
    if not isCommitId(version):
        return None
    manifest = readManifest(baseDir, version)
    if manifest is None or manifest.get("format") != FORMAT or manifest.get("base") != base or manifest.get("commit") != version:
        return None
    if not isinstance(manifest.get("roots"), list) or not isinstance(manifest.get("modules", {}), dict):
        return None
    return manifest


def mark(state, key, version):
    values = [value for value in state.get(key) or [] if value != version]
    values.append(version)
    state[key] = values[-MAX_MARKED:]


class StateLock:
    """The lock of a state folder (``fcntl.flock`` on ``state.lock``). ``acquired`` is False when it could not be taken
    within ``timeout`` seconds (or locks are not available): the state must then only be read."""

    def __init__(self, baseDir, timeout):
        self.baseDir = baseDir
        self.timeout = timeout
        self.file = None
        self.acquired = False

    def __enter__(self):
        try:
            import fcntl
        except ImportError:  # Windows: no lock (Slicer Studio packages are for macOS and Linux)
            self.acquired = True
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
            except OSError:
                if time.monotonic() >= deadline:
                    return self
                time.sleep(0.02)

    def __exit__(self, *args):
        if self.file is not None:
            try:
                if self.acquired:
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


def processStartToken(pid, slow=True):
    """When the process ``pid`` started (a text that changes when the system gives the pid to another process), or None.
    On macOS it asks ``ps``, unless ``slow`` is False."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
            return f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        pass
    if sys.platform != "darwin" or not slow:
        return None
    import subprocess

    try:
        result = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], stdin=subprocess.DEVNULL, capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.decode(errors="replace").strip() or None


def bootAlive(boot, now=None):
    """True while the process of the recorded start ``boot`` still runs."""
    pid = boot.get("pid")
    if type(pid) is not int or pid <= 0 or not processAlive(pid):
        return False
    token = boot.get("token")
    if token:
        current = processStartToken(pid)
        return current is None or current == token
    if boot.get("confirmed"):
        return True
    started = boot.get("startedAt")
    now = time.time() if now is None else now
    return isinstance(started, (int, float)) and now - started < UNCONFIRMED_WITHOUT_TOKEN_S


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
            moduleFiles[name] = path
    return insertions, moduleFiles


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
        for name, path in sorted(moduleFiles.items()):
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                raise ActivationError(f"cannot load {name}")
            loader = importlib.util.LazyLoader(spec.loader)
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


def activate(home, stateRoot, runInfo, env=None, sysPath=None, sysModules=None, now=None):
    """Apply the staged or active code update of the installation in ``home`` to this process, before any Slicer or
    Studio module is imported. ``runInfo``: ``installed``, ``testing``, ``interactive`` (a normal start with the main
    window, where the start is recorded for the automatic rollback), ``pid`` and ``token`` (its start time).

    Return the status (also kept for ``status()``). Never raises for a problem of the update: the installed code is
    then used.
    """
    global _status, _buildInfo, _roots, _preseeded, _boot
    env = os.environ if env is None else env
    sysPath = sys.path if sysPath is None else sysPath
    sysModules = sys.modules if sysModules is None else sysModules
    now = time.time() if now is None else now
    _roots, _preseeded, _boot = [], {}, None
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
            # Automatic rollback: a start of the active version that was not confirmed and whose process is gone
            alive = []
            for boot in state.get("boots") or []:
                if not isinstance(boot, dict):
                    changed = True
                    continue
                if bootAlive(boot, now):
                    alive.append(boot)
                    continue
                changed = True
                failed = boot.get("version")
                if not boot.get("confirmed") and failed and failed == state.get("active"):
                    previous = state.get("previous")
                    target = previous if previous and previous not in state["bad"] and validVersion(baseDir, previous, base) else None
                    mark(state, "bad", failed)
                    state["active"] = target
                    state["previous"] = None
                    if state.get("pending") == failed:
                        state["pending"] = None
                    state["rolledBack"] = {"from": failed, "to": target or "installed", "at": isoTime(now)}
                    result["rolledBack"] = dict(state["rolledBack"])
            state["boots"] = alive
            # Promotion of a pending version
            pending = state.get("pending")
            if pending == "installed":
                state["active"], state["previous"], state["pending"] = None, None, None
                changed = True
            elif pending:
                if pending not in state["bad"] and validVersion(baseDir, pending, base):
                    old = state.get("active")
                    if old != pending:
                        state["previous"] = old if old and old not in state["skipped"] else None
                        state["active"] = pending
                        result["promoted"] = pending
                state["pending"] = None
                changed = True
        version = state.get("active")
        if version:
            manifest = validVersion(baseDir, version, base)
            try:
                if manifest is None:
                    raise ActivationError("the update is missing or damaged")
                filesDir = os.path.join(versionDirectory(baseDir, version), FILES_FOLDER)
                insertions, moduleFiles = _prepare(home, filesDir, manifest, sysPath, sysModules)
                _roots, _preseeded = _apply(insertions, moduleFiles, sysPath, sysModules)
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
        if result["state"] == "active" and lock.acquired and runInfo.get("interactive"):
            pid = int(runInfo.get("pid") or os.getpid())
            state["boots"].append({"version": version, "pid": pid, "token": runInfo.get("token"), "startedAt": now, "confirmed": False})
            _boot = (baseDir, version, pid)
            changed = True
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
    ``rolledBack`` ({from, to, at} when this start went back), ``error``, ``modules`` and ``roots``."""
    return dict(_status)


def mapPath(path):
    """``path`` of an installed file inside an overlaid folder, mapped to the same file in the active overlay when it
    exists there (for example the path of a scripted module, whose resources are looked up next to it)."""
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
                    if _isUnder(candidate, root):
                        mapped = os.path.join(overlay, os.path.relpath(candidate, root))
                        return mapped if os.path.exists(mapped) else path
    except (OSError, ValueError):
        pass
    return path


#
# Health check of the start (main thread of the application)
#


def _overlayModulesHealthy(app=None):
    """True when every pre-seeded module that was used came from the overlay, and Studio was instantiated when it was
    registered. Return (healthy, reason)."""
    overlay = _status.get("overlay") or ""
    factory = None
    if app is not None:
        try:
            factory = app.moduleManager().factoryManager()
        except Exception:
            factory = None
    for name, module in _preseeded.items():
        current = sys.modules.get(name)
        if current is None:
            continue
        if current is module and type(current).__name__ == "_LazyModule":
            continue  # never used (the module is not loaded in this run)
        if current is not module:
            return False, f"{name} was loaded from the installed files"
        try:
            filePath = object.__getattribute__(current, "__dict__").get("__file__") or ""
        except Exception:
            filePath = ""
        if not _isUnder(os.path.normpath(filePath), os.path.normpath(overlay)):
            return False, f"{name} was loaded from the installed files"
    if factory is not None and "Studio" in _preseeded:
        try:
            if factory.isRegistered("Studio") and not factory.isInstantiated("Studio"):
                return False, "Studio could not start"
        except Exception:
            pass
    return True, None


def confirmStart(healthy=True, now=None):
    """Mark the recorded start of this process as confirmed (it started well), or leave it unconfirmed. Return True
    when it was confirmed."""
    global _boot
    if _boot is None or not healthy:
        return False
    baseDir, version, pid = _boot
    now = time.time() if now is None else now
    token = processStartToken(pid)

    def change(state):
        for boot in state.get("boots") or []:
            if isinstance(boot, dict) and boot.get("pid") == pid and boot.get("version") == version and not boot.get("confirmed"):
                boot["confirmed"] = True
                boot["confirmedAt"] = now
                if token and not boot.get("token"):
                    boot["token"] = token
                lastUsed = state.get("lastUsed") if isinstance(state.get("lastUsed"), dict) else {}
                lastUsed[version] = isoTime(now)
                state["lastUsed"] = lastUsed
                return True
        return False

    base = (_status.get("base") or "")
    result = updateState(baseDir, base, change, timeout=5.0)
    if result is None:
        return False
    _boot = None
    _status["confirmed"] = True
    return True


def _scheduleHealthCheck(app):
    """Confirm the start ``HEALTH_DELAY_MS`` after the start-up completed (or at the exit, when it completed)."""
    global _healthTimer
    import qt

    state = {"completed": False, "done": False}

    def check():
        if state["done"]:
            return
        state["done"] = True
        healthy, reason = _overlayModulesHealthy(app)
        if not healthy:
            _status["healthError"] = reason
            print(f"Slicer Studio code update {(_status.get('active') or '')[:7]}: {reason}; the next start goes back.", file=sys.stderr)
            return
        import threading

        threading.Thread(target=confirmStart, name="StudioOverlayConfirm", daemon=True).start()

    def onCompleted():
        global _healthTimer
        state["completed"] = True
        _healthTimer = qt.QTimer()
        _healthTimer.setSingleShot(True)
        _healthTimer.setInterval(HEALTH_DELAY_MS)
        _healthTimer.connect("timeout()", check)
        _healthTimer.start()

    def onQuit():
        if state["completed"] and not state["done"]:
            state["done"] = True
            healthy, _reason = _overlayModulesHealthy(app)
            confirmStart(healthy)

    app.connect("startupCompleted()", onCompleted)
    app.connect("aboutToQuit()", onQuit)
    _callbacks[:] = [onCompleted, onQuit]  # keep them alive


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
    if options is not None:
        for name in ("noMainWindow", "exitAfterStartup", "runPythonAndExit", "disableScriptedLoadableModules",
                     "disableBuiltInScriptedLoadableModules", "disableModules", "disableBuiltInModules"):
            if value(options, name, False):
                interactive = False
        if value(options, "pythonScript", "") or value(options, "extraPythonScript", ""):
            interactive = False
        ignored = value(options, "modulesToIgnore", []) or []
        if "Studio" in list(ignored):
            interactive = False
    pid = os.getpid()
    return {"installed": bool(value(app, "isInstalled", False)), "testing": bool(value(app, "testingEnabled", False)),
            "interactive": interactive, "pid": pid, "token": processStartToken(pid, slow=False)}


def stateRootFromApplication():
    """``<AppLocalDataLocation>/StudioUpdate/commit`` (the same folder as ``StudioLib.updates.stateDirectory()``)."""
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

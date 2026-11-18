"""Slicer Studio user profile: the user data that survives reinstalling and rebuilding the application.

Replacing the application (dragging a new ``Slicer Studio.app`` over the old one, installing a new Linux package, an
in-app update) must not lose what the user set up. Everything the user owns lives outside the application, in the
profile folder ``<P>`` and in ``~/.config/slicer.org``:

- ``<P>`` is ``root()``: the environment variable ``SLICER_STUDIO_PROFILE_DIR`` (absolute, ``~`` allowed), else the
  setting ``Studio/Profile/Directory`` of ``Slicer.ini``, else the private application data folder
  (``QStandardPaths.AppLocalDataLocation``: ``~/Library/Application Support/slicer.org/Slicer`` on macOS,
  ``${XDG_DATA_HOME:-~/.local/share}/slicer.org/Slicer`` on Linux), where autosave, the update state and the move
  journal already were. A folder inside the application (or around it), that cannot be written, or that does not
  exist (renamed, a synced folder not restored yet: never created empty in its place) is refused, with the reason
  printed and shown in Settings > Profile, and the default is used for that run without carrying, importing, syncing or
  trashing anything. A folder chosen in Settings must be empty (the profile is copied there) or already hold a profile.
- ``<P>/StudioExtensions/<server revision>-<os>-<arch>/``: the installed extensions. They are keyed by the revision
  of the official build the extensions server knows this build by (``Slicer_EXTENSIONS_SERVER_REVISION``), not by
  the fork's own revision (a commit count that changes with every build): every build of the same upstream base
  shares them, and a merge of a newer upstream, which needs extensions built for it, starts a new folder. On Linux
  this is the extensions install folder (``Extensions/InstallPath``). On macOS the application always installs
  extensions inside the bundle (``Contents/Extensions-<rev>``, where their libraries find the application's), so the
  store is kept as a mirror of that folder: it is restored into a new bundle at the first start, and what the user
  installs, updates or uninstalls is copied back (``StudioLib/profile_extensions.py``, and at every start). The marker
  of a bundle (``.studio-extensions.json``) names the real path of the store and the extensions it held at its last
  sync, so that several bundles share a store and a bundle that meets another store merges with it instead of
  removing anything (``planExtensionsSync``). An extension that the user uninstalled goes to
  ``<P>/StudioExtensions-trash/<date>/`` for 30 days, never deleted at once. An import of the extensions of a previous
  build that did not finish is taken up again at the next starts (``importExtensions``).
- ``<P>/StudioPython/<cache tag>-<platform>-<machine>/``: the Python packages that the user installs with pip
  (``slicer.util.pip_install`` adds ``--user`` with ``PYTHONUSERBASE`` set to this folder; the setting
  ``Studio/Profile/PipToProfile`` turns that off). Keyed by the Python ABI.
- ``<P>/StudioLegacy/``: copies of what older builds kept inside the application (``StudioLib/profile_legacy.py``).
  The first start of a new build also looks in the older builds themselves: on Linux the Slicer Studio folders next
  to it, on macOS the other Slicer Studio bundles in Applications (the old build renamed while the new one starts).
- ``<P>/StudioProfile.json``: what was carried forward and when; ``<P>/StudioProfile.lock``: the lock of the profile.
- ``~/.config/slicer.org/SlicerStudio-<rev>.ini``: the revision-specific settings (the build sets
  ``Slicer_STORE_SETTINGS_IN_APPLICATION_HOME_DIR`` OFF and the file base name ``Studio``, so they are outside the
  application and never mixed with official Slicer's ``Slicer-<rev>.ini``). A new build has a new revision and so a
  new file: at its first start ``startup()`` carries the user's values forward from the newest previous file
  (``planCarryForward``) and marks it ``Studio/Profile/Initialized``.

``startup(app)`` runs from ``slicerqt.py`` before anything else: Python starts before the application creates the
extensions manager and reads the module paths, so what it changes applies to this run. It does nothing with
``--testing`` or ``--disable-settings``, nor in a portable build (``Slicer_STORE_SETTINGS_IN_APPLICATION_HOME_DIR``
ON), and it costs one settings read and one small file read when there is nothing to do. On Linux the
launcher has already set the library paths of this run, so after carrying extensions forward it asks for one restart
(``Studio/Profile/RestartNeeded``; the Studio module shows it).

Rules: copies are verified before they are used and their sources are never deleted; only ``Studio/*`` keys are
written to ``Slicer.ini``; the profile and the revision file are private (absolute paths are allowed there), shared
files never get them. No Qt at import time, and every rule is a plain function: the tests run them with plain Python.
"""

import hashlib
import json
import os
import platform as _platformModule
import re
import shutil
import subprocess
import sys
import sysconfig
import time

FORMAT = 1

ENVIRONMENT_VARIABLE = "SLICER_STUDIO_PROFILE_DIR"
#: Set by ``startup()`` when pip installs into the profile (read by ``slicer.packaging``)
USERBASE_ENVIRONMENT_VARIABLE = "SLICER_STUDIO_PYTHON_USERBASE"

#: Slicer.ini (fork-only keys)
DIRECTORY_KEY = "Studio/Profile/Directory"
PENDING_DIRECTORY_KEY = "Studio/Profile/PendingDirectory"
#: The profile folder used before the last change in Settings (kept, never deleted: Settings > Profile shows it)
PREVIOUS_DIRECTORY_KEY = "Studio/Profile/PreviousDirectory"
PIP_KEY = "Studio/Profile/PipToProfile"
#: The revision-specific settings file (fork-only file)
INITIALIZED_KEY = "Studio/Profile/Initialized"
CARRIED_FROM_KEY = "Studio/Profile/CarriedFrom"
RESTART_KEY = "Studio/Profile/RestartNeeded"

ORGANIZATION = "slicer.org"
APPLICATION = "Slicer"
#: SLICER_REVISION_SPECIFIC_USER_SETTINGS_FILEBASENAME of this fork
SETTINGS_BASENAME = "Studio"

MANIFEST_FILE = "StudioProfile.json"
LOCK_FILE = "StudioProfile.lock"
EXTENSIONS_FOLDER = "StudioExtensions"
TRASH_FOLDER = "StudioExtensions-trash"
PYTHON_FOLDER = "StudioPython"
LEGACY_FOLDER = "StudioLegacy"
#: In the bundle's extensions folder (macOS): the store it mirrors
SYNC_MARKER = ".studio-extensions.json"

TRASH_DAYS = 30
RESERVED_FREE_BYTES = 5 * 1024 * 1024 * 1024
HASH_LIMIT = 1024 * 1024
PREFETCH_KEEP = 3

#: Revision-specific keys carried to a new build (``planCarryForward``)
CARRIED_KEYS = (
    "Modules/IgnoreModules",
    "Modules/MostRecentlySelectedPath",
    "Extensions/ManagerEnabled",
    "Extensions/AutoUpdateCheck",
    "Extensions/AutoUpdateInstall",
    "Extensions/AutoUpdateFrequencyMinutes",
    "Extensions/AutoInstallDependencies",
    "Extensions/Bookmarked",
)
#: Carried together: the application drops both URLs itself when the server API changed (it compares LastServerAPI)
SERVER_KEYS = ("Extensions/ServerUrl", "Extensions/FrontendServerUrl", "Extensions/LastServerAPI")
PATH_LIST_KEYS = ("Modules/AdditionalPaths",)
#: Launcher settings groups (arrays of ``path``) and the key=value group
LAUNCHER_PATH_GROUPS = ("LibraryPaths", "Paths", "PYTHONPATH", "QT_PLUGIN_PATH")
LAUNCHER_VALUE_GROUPS = ("EnvironmentVariables",)

_REVISION_FILE = re.compile(r"^Slicer(?P<base>Studio)?-(?P<rev>\d+)\.ini$")
_RELATIVE_EXTENSION_PATH = re.compile(r"^(?:slicer\.org/)?Extensions-\d+/(?P<rest>.+)$")

_state = {"root": None, "source": None, "problem": None}
_status = {"state": "not started"}


def _log(message):
    print(f"Slicer Studio profile: {message}", file=sys.stderr)


#
# Paths
#


def isInside(path, folder):
    """True when ``path`` is ``folder`` or inside it (real paths)."""
    if not path or not folder:
        return False
    path, folder = os.path.realpath(path), os.path.realpath(folder)
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def defaultRoot(env, home, platform):
    """The default profile folder (what Qt's AppLocalDataLocation is for organization slicer.org, application Slicer)."""
    if platform == "darwin":
        return os.path.join(home, "Library", "Application Support", ORGANIZATION, APPLICATION)
    data = env.get("XDG_DATA_HOME") or ""
    base = data if os.path.isabs(data) else os.path.join(home, ".local", "share")
    return os.path.join(base, ORGANIZATION, APPLICATION)


def expandPath(value, home):
    """``value`` with a leading ``~`` expanded, normalized; None when it is empty or not absolute."""
    value = str(value or "").strip()
    if not value:
        return None
    if value == "~" or value.startswith("~/"):
        value = home + value[1:]
    return os.path.normpath(value) if os.path.isabs(value) else None


def validateOverride(path, forbiddenRoots=(), mustExist=False):
    """None when ``path`` can be the profile folder, else the reason it cannot: not absolute, inside an application
    bundle, inside or around one of ``forbiddenRoots`` (the application, its update copies), not a folder, not
    writable, or (``mustExist``) missing. A profile folder in use must exist: a missing one was renamed, moved or not
    restored yet, and an empty folder made in its place would hide the real profile. Only a folder chosen in Settings
    (``applyPendingDirectory``) is created."""
    if not path or not os.path.isabs(path):
        return "it is not an absolute folder"
    real = os.path.realpath(path)
    if any(part.endswith(".app") for part in real.split(os.sep)):
        return "it is inside an application bundle"
    for root in forbiddenRoots:
        if root and (isInside(real, root) or isInside(root, real)):
            return "it is inside the application (or holds it)"
    if os.path.exists(real) and not os.path.isdir(real):
        return "it is not a folder"
    if mustExist and not os.path.isdir(real):
        return "it does not exist (create it, or bring the profile back there, to use it)"
    probe = real
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    if not os.access(probe, os.W_OK | os.X_OK):
        return "it cannot be written"
    return None


def resolveRoot(env, home, platform, settingValue, forbiddenRoots=(), default=None):
    """``(root, source, problem)``: the environment variable, else the setting, else ``default`` (else
    ``defaultRoot``). An override that ``validateOverride`` refuses (a missing folder included) gives the default and
    the reason: ``startup()`` then neither migrates nor syncs anything in that folder."""
    default = default or defaultRoot(env, home, platform)
    for source, raw in (("environment", env.get(ENVIRONMENT_VARIABLE)), ("setting", settingValue)):
        if raw is None or not str(raw).strip():
            continue
        path = expandPath(raw, home)
        problem = validateOverride(path, forbiddenRoots, mustExist=True)
        if problem is None:
            return path, source, None
        name = ENVIRONMENT_VARIABLE if source == "environment" else DIRECTORY_KEY
        return default, "default", f"{name} = {raw!r} is not used ({problem}); using {default}"
    return default, "default", None


def applicationRoots(slicerHome, platform):
    """The folders the profile must never be in or around: the application and the copies an update makes."""
    if not slicerHome:
        return []
    home = os.path.realpath(slicerHome)
    target = home
    if platform == "darwin":
        probe = home
        while probe and probe != os.path.dirname(probe):
            if probe.endswith(".app"):
                target = probe
                break
            probe = os.path.dirname(probe)
    parent, name = os.path.dirname(target), os.path.basename(target)
    return [home, target, os.path.join(parent, f".{name}.studio-previous"), os.path.join(parent, f".{name}.studio-update")]


#: Inside a protected folder, the folders whose contents deleting code may remove (downloads, staged updates)
REMOVABLE_INSIDE = ("StudioUpdate",)


def protectedRoots(home=None, profileRoot=None):
    """Folders that deleting code (the updater, clean-ups) must never remove, nor any folder holding them: the
    profile and the user settings folder."""
    home = os.path.expanduser("~") if home is None else home
    roots = [os.path.join(home, ".config", ORGANIZATION)]
    profileRoot = root() if profileRoot is None else profileRoot
    if profileRoot:
        roots.insert(0, profileRoot)
    return roots


def removableInside(path, protected):
    """True when ``path`` is strictly inside one of the ``REMOVABLE_INSIDE`` folders of ``protected`` (the update
    state: what the updater downloads and stages there is its own to delete)."""
    real = os.path.realpath(path)
    for name in REMOVABLE_INSIDE:
        folder = os.path.realpath(os.path.join(protected, name))
        if real.startswith(folder.rstrip(os.sep) + os.sep):
            return True
    return False


def checkRemovable(path, roots=None):
    """Raise OSError when deleting ``path`` would delete one of ``roots`` (default ``protectedRoots()``) or part of it,
    except the contents of its update state folder (``removableInside``)."""
    roots = protectedRoots() if roots is None else roots
    for protected in roots:
        if not protected:
            continue
        if isInside(protected, path) or (isInside(path, protected) and not removableInside(path, protected)):
            raise OSError(f"{path} holds Slicer Studio user data ({protected}): it is not deleted")


def extensionsStore(profileRoot, serverRevision, os_, arch):
    return os.path.join(profileRoot, EXTENSIONS_FOLDER, f"{serverRevision}-{os_}-{arch}")


def pythonUserBase(profileRoot, cacheTag=None, platform=None, machine=None):
    cacheTag = cacheTag or sys.implementation.cache_tag
    platform = platform or sys.platform
    machine = machine or _platformModule.machine() or "unknown"
    return os.path.join(profileRoot, PYTHON_FOLDER, f"{cacheTag}-{platform}-{machine}")


def userSitePackages(base):
    """The site-packages folder of pip's user scheme for ``PYTHONUSERBASE=base`` (what ``pip install --user`` uses)."""
    try:
        scheme = sysconfig.get_preferred_scheme("user")
    except AttributeError:  # Python < 3.10
        scheme = f"{os.name}_user"
    return sysconfig.get_path("purelib", scheme, vars={"userbase": base})


#
# The profile folder of this process
#


def _value(obj, name, default=None):
    try:
        attribute = getattr(obj, name)
    except Exception:
        return default
    try:
        return attribute() if callable(attribute) else attribute
    except Exception:
        return default


def _application():
    import __main__

    app = getattr(__main__, "_qSlicerCoreApplicationInstance", None)
    if app is None:
        try:
            import slicer

            app = slicer.app
        except Exception:
            app = None
    return app


def settingsDisabled(app):
    """True with ``--disable-settings``: the application then uses temporary settings and its name ends with
    ``-tmp`` (asked of the name, because ``slicerRevisionUserSettingsFilePath`` clears temporary settings)."""
    return str(_value(app, "applicationName", "") or "").endswith("-tmp")


def setRoot(path, source="test"):
    """Use ``path`` as the profile folder of this process (tests), or resolve it again (None)."""
    _state.update(root=path, source=source if path else None, problem=None)


def root():
    """The profile folder of this process (resolved once; see the module documentation). Empty without one."""
    if _state["root"] is None:
        try:
            _resolveFromApplication()
        except Exception as error:
            _state.update(root="", source=None, problem=str(error))
    return _state["root"] or ""


def rootSource():
    """``(source, problem)``: "environment", "setting", "default" or "test", and why an override was not used."""
    root()
    return _state["source"], _state["problem"]


def _resolveFromApplication():
    import qt

    app = _application()
    default = str(qt.QStandardPaths.writableLocation(qt.QStandardPaths.AppLocalDataLocation) or "")
    env, setting = os.environ, None
    if app is not None and settingsDisabled(app):
        env = {}  # temporary settings (tests): never the user's folder
    else:
        settings = _value(app, "userSettings") if app is not None else None
        settings = settings if settings is not None else qt.QSettings()
        setting = settings.value(DIRECTORY_KEY)
    forbidden = applicationRoots(str(_value(app, "slicerHome", "") or ""), sys.platform) if app is not None else []
    path, source, problem = resolveRoot(env, os.path.expanduser("~"), sys.platform, setting, forbidden, default or None)
    _state.update(root=path, source=source, problem=problem)
    if problem:
        _log(problem)


#
# Copies
#


def freeBytes(path):
    while path and not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def treeSize(path):
    total = 0
    for folder, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(folder, name)).st_size
            except OSError:
                pass
    return total


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verifyTree(source, copy, hashes=True):
    """None when ``copy`` holds every file, folder and link of ``source`` (same sizes, same SHA-256 up to
    ``HASH_LIMIT`` when ``hashes``, same link targets), else the first difference."""
    for folder, dirs, files in os.walk(source):
        relative = os.path.relpath(folder, source)
        other = os.path.normpath(os.path.join(copy, relative))
        if not os.path.isdir(other):
            return f"missing folder {relative}"
        for name in dirs + files:
            path, target = os.path.join(folder, name), os.path.join(other, name)
            if os.path.islink(path):
                if not os.path.islink(target) or os.readlink(path) != os.readlink(target):
                    return f"different link {os.path.join(relative, name)}"
                continue
            if name in dirs:
                continue
            try:
                size = os.stat(path).st_size
                copied = os.lstat(target)
            except OSError:
                return f"missing file {os.path.join(relative, name)}"
            if os.path.islink(target) or copied.st_size != size:
                return f"different file {os.path.join(relative, name)}"
            if hashes and size <= HASH_LIMIT and _sha256(path) != _sha256(target):
                return f"different content {os.path.join(relative, name)}"
    return None


def cloneTree(source, destination, platform=None, run=subprocess.run):
    """Copy the folder ``source`` to ``destination`` (which must not exist): a clone where the file system can (APFS
    ``cp -c``, Btrfs/XFS ``cp --reflink=always``), else a plain copy. Links are copied as links. Return "clone" when
    the file system cloned the files (they share their blocks with the source: nothing was read or written, so a check
    of paths and sizes is enough), else "copy"."""
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        commands = [("clone", ["cp", "-c", "-R", "-p", source, destination]), ("copy", ["cp", "-R", "-p", source, destination])]
    elif platform.startswith("linux"):
        commands = [("clone", ["cp", "-a", "--reflink=always", source, destination]), ("copy", ["cp", "-a", source, destination])]
    else:
        commands = []
    for kind, command in commands:
        try:
            if run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                return kind
        except OSError:
            pass
        shutil.rmtree(destination, ignore_errors=True)
    shutil.copytree(source, destination, symlinks=True)
    return "copy"


def copyTreeVerified(source, destination, platform=None, clone=cloneTree):
    """Copy the folder ``source`` to ``destination`` (which must not exist) through ``destination.partial-<pid>``,
    checked with ``verifyTree`` before it is renamed in place (paths and sizes only after a clone, which reads no
    file: this runs at start-up). None on success, else the reason (nothing is left behind). ``source`` is never
    changed."""
    if os.path.lexists(destination):
        return "the destination exists"
    partial = f"{destination}.partial-{os.getpid()}"
    shutil.rmtree(partial, ignore_errors=True)
    try:
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        kind = clone(source, partial, platform)
        problem = verifyTree(source, partial, hashes=kind != "clone")
        if problem is None:
            os.replace(partial, destination)
            return None
    except (OSError, shutil.Error) as error:
        problem = str(error)
    shutil.rmtree(partial, ignore_errors=True)
    return problem


def _copyFileAtomic(source, destination):
    temporary = f"{destination}.partial-{os.getpid()}"
    shutil.copy2(source, temporary)
    os.replace(temporary, destination)


def readJson(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def writeJsonAtomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(temporary, path)


class ProfileLock:
    """An exclusive ``flock`` on ``<root>/StudioProfile.lock``. ``acquired`` is False when another process holds it
    for longer than ``timeout`` seconds. The system releases it when the process dies, so a lock is never stale. On a
    file system without locks the work goes on unlocked (every write is an atomic rename)."""

    def __init__(self, profileRoot, timeout=0.0):
        self.profileRoot = profileRoot
        self.timeout = timeout
        self.file = None
        self.acquired = False

    def __enter__(self):
        import errno

        try:
            import fcntl
        except ImportError:
            self.acquired = True
            return self
        try:
            os.makedirs(self.profileRoot, exist_ok=True)
            self.file = open(os.path.join(self.profileRoot, LOCK_FILE), "a+")
        except OSError:
            return self
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.acquired = True
                try:
                    self.file.seek(0)
                    self.file.truncate()
                    self.file.write(f"{os.getpid()}\n")
                    self.file.flush()
                except OSError:
                    pass
                return self
            except OSError as error:
                if error.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES, errno.EINTR):
                    self.acquired = True  # no locks on this file system
                    return self
                if time.monotonic() >= deadline:
                    return self
                time.sleep(0.02)

    def __exit__(self, *args):
        if self.file is not None:
            self.file.close()
            self.file = None
        return False


#
# Previous builds
#


def _revisionFiles(folder, studioOnly):
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    found = []
    for name in names:
        match = _REVISION_FILE.match(name)
        if not match or (studioOnly and not match.group("base")) or (not studioOnly and match.group("base")):
            continue
        found.append((match.group("rev"), os.path.join(folder, name)))
    return found


def _inAppExtensions(home, revision, platform):
    if platform == "darwin":
        return os.path.join(os.path.dirname(home), "Contents", f"Extensions-{revision}")
    return os.path.join(home, ORGANIZATION, f"Extensions-{revision}")


def _candidate(kind, revision, ini, home, extensions):
    try:
        mtime = os.stat(ini).st_mtime
    except OSError:
        return None
    return {"kind": kind, "revision": str(revision), "ini": ini, "home": home, "mtime": mtime,
            "extensions": extensions if extensions and os.path.isdir(extensions) else None}


def applicationFolders(slicerHome, home=None):
    """macOS: the folders where other Slicer Studio bundles may be: the folder of this bundle, ``/Applications`` and
    ``~/Applications``."""
    home = os.path.expanduser("~") if home is None else home
    folders = []
    probe = os.path.realpath(slicerHome) if slicerHome else ""
    while probe and probe != os.path.dirname(probe):
        if probe.endswith(".app"):
            folders.append(os.path.dirname(probe))
            break
        probe = os.path.dirname(probe)
    folders += ["/Applications", os.path.join(home, "Applications")]
    return folders


def _otherBundles(folders, slicerHome):
    """macOS: ``Contents`` of the Slicer Studio bundles in ``folders`` (those with ``studio_build_info.json``) other
    than the one of ``slicerHome``."""
    current = os.path.realpath(slicerHome) if slicerHome else ""
    found, seen = [], set()
    for folder in folders:
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in names:
            if name.startswith(".") or not name.endswith(".app"):
                continue
            contents = os.path.join(folder, name, "Contents")
            real = os.path.realpath(contents)
            if real == current or real in seen or not os.path.isfile(os.path.join(contents, "bin", "Python", "studio_build_info.json")):
                continue
            seen.add(real)
            found.append(contents)
    return found


def findRevisionCandidates(configDirs, profileRoot, slicerHome, currentRevision, platform, otherApplicationFolders=None):
    """Revision-specific settings files of previous Slicer Studio builds, newest first: a list of ``{"kind",
    "revision", "ini", "home", "mtime", "extensions"}`` (``home``: the Slicer home its relative paths are relative to;
    ``extensions``: its extensions folder, when one exists). Official Slicer files are never read: only
    ``SlicerStudio-<rev>.ini`` in ``configDirs``, the snapshots of ``StudioLegacy``, and ``slicer.org/Slicer-<rev>.ini``
    inside this installation, the copy kept by an in-app update, and other Slicer Studio installations: on Linux the
    folders next to this one, on macOS the bundles in ``otherApplicationFolders`` (default ``applicationFolders()``),
    for example ``Slicer Studio old.app`` kept while the new build starts for the first time."""
    found = []
    home = os.path.realpath(slicerHome) if slicerHome else ""
    seen = set()
    for folder in configDirs:
        if not folder or os.path.realpath(folder) in seen:
            continue
        seen.add(os.path.realpath(folder))
        for revision, ini in _revisionFiles(folder, studioOnly=True):
            if revision != str(currentRevision):
                found.append(_candidate("settings", revision, ini, home, _inAppExtensions(home, revision, platform) if platform == "darwin" else None))
    legacy = os.path.join(profileRoot, LEGACY_FOLDER) if profileRoot else ""
    try:
        names = os.listdir(legacy) if legacy else []
    except OSError:
        names = []
    for name in names:
        folder = os.path.join(legacy, name)
        info = readJson(os.path.join(folder, "snapshot.json"))
        if info is None or ".partial-" in name or ".old-" in name:
            continue
        ini = os.path.join(folder, str(info.get("ini") or f"Slicer-{info.get('revision')}.ini"))
        found.append(_candidate("snapshot", info.get("revision"), ini, str(info.get("home") or home), os.path.join(folder, "Extensions")))
    homes = []
    if home:
        homes.append(("installed", home))
        roots = applicationRoots(home, platform)
        backup = roots[2]
        homes.append(("backup", os.path.join(backup, "Contents") if platform == "darwin" else backup))
        if platform != "darwin":
            parent = os.path.dirname(home)
            try:
                siblings = sorted(os.listdir(parent))
            except OSError:
                siblings = []
            for name in siblings:
                sibling = os.path.join(parent, name)
                if sibling != home and not name.startswith(".") and os.path.isfile(os.path.join(sibling, "bin", "Python", "studio_build_info.json")):
                    homes.append(("sibling", sibling))
        else:
            folders = applicationFolders(home) if otherApplicationFolders is None else otherApplicationFolders
            for contents in _otherBundles(folders, home):
                homes.append(("sibling", contents))
    for kind, folderHome in homes:
        for revision, ini in _revisionFiles(os.path.join(folderHome, ORGANIZATION), studioOnly=False):
            if kind == "installed" and revision == str(currentRevision):
                continue
            found.append(_candidate(kind, revision, ini, folderHome, _inAppExtensions(folderHome, revision, platform)))
    found = [entry for entry in found if entry is not None]
    found.sort(key=lambda entry: entry["mtime"], reverse=True)
    return found


#
# Carrying the revision-specific settings forward
#


def _asList(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item)]
    value = str(value)
    return [value] if value else []


def _settingsValue(values):
    """A list as QSettings writes a string list in an INI file (one item: the plain string)."""
    return values[0] if len(values) == 1 else list(values)


def _readArray(values, group, field="path"):
    try:
        size = int(values.get(f"{group}/size") or 0)
    except (TypeError, ValueError):
        size = 0
    return [str(values[f"{group}/{index}/{field}"]) for index in range(1, size + 1) if values.get(f"{group}/{index}/{field}")]


def mapPath(entry, ctx):
    """The path ``entry`` of a previous build's settings, as it must be in this build's, or None to drop it.

    ``ctx``: ``oldHome`` (what relative entries were relative to), ``newHome``, ``oldExtensionRoots`` (extensions
    folders of the previous build), ``newExtensions`` (this build's), ``exists`` (a function). A path in a previous
    extensions folder moves to the same place in this build's; other paths inside the old or new application are
    dropped (they are built in); others are kept when they exist. The result is relative to ``newHome`` when it is
    inside it (as the application writes them), else absolute."""
    exists = ctx.get("exists", os.path.exists)
    oldHome, newHome, newExtensions = ctx.get("oldHome") or "", ctx.get("newHome") or "", ctx.get("newExtensions")
    entry = str(entry).replace("\\", "/")
    mapped = None
    if not os.path.isabs(entry):
        match = _RELATIVE_EXTENSION_PATH.match(entry)
        if match and newExtensions:
            mapped = os.path.join(newExtensions, match.group("rest"))
        else:
            entry = os.path.normpath(os.path.join(oldHome or newHome, entry))
    if mapped is None:
        for root in ctx.get("oldExtensionRoots") or []:
            if root and newExtensions and (entry == root or entry.startswith(root.rstrip("/") + "/")):
                mapped = os.path.normpath(os.path.join(newExtensions, os.path.relpath(entry, root)))
                break
    if mapped is None:
        if any(home and (entry == home or entry.startswith(home.rstrip("/") + "/")) for home in (oldHome, newHome)):
            return None
        mapped = entry
    if not exists(mapped):
        return None
    if newHome and (mapped == newHome or mapped.startswith(newHome.rstrip("/") + "/")):
        return os.path.relpath(mapped, newHome)
    return mapped


def planCarryForward(old, current, defaults, ctx):
    """The values to write into this build's revision-specific settings, from ``old`` (a previous build's, as a dict of
    QSettings keys) when ``current`` (this build's) is new. Only keys that are missing from ``current`` or still hold
    their ``defaults`` value are written. See ``CARRIED_KEYS``, ``SERVER_KEYS``, ``PATH_LIST_KEYS`` and the launcher
    groups; nothing else is carried (never the scheduled installs, updates and uninstalls, the server metadata, the
    install folder, or the General and Application groups)."""

    def seeded(key):
        return key not in current or (key in defaults and current[key] == defaults[key])

    plan = {}
    for key in CARRIED_KEYS:
        if key in old and seeded(key):
            plan[key] = old[key]
    if all(key in old for key in SERVER_KEYS[2:]) and all(seeded(key) for key in SERVER_KEYS):
        for key in SERVER_KEYS:
            if key in old:
                plan[key] = old[key]
    for key in PATH_LIST_KEYS:
        if key in old and seeded(key):
            paths = []
            for entry in _asList(old[key]):
                mapped = mapPath(entry, ctx)
                if mapped and mapped not in paths:
                    paths.append(mapped)
            if paths:
                plan[key] = _settingsValue(paths)
    for group in LAUNCHER_PATH_GROUPS:
        if any(key.startswith(group + "/") for key in current) or f"{group}/size" not in old:
            continue
        paths = []
        for entry in _readArray(old, group):
            mapped = mapPath(entry, ctx)
            if mapped and mapped not in paths:
                paths.append(mapped)
        if paths:
            plan[f"{group}/size"] = len(paths)
            for index, path in enumerate(paths, start=1):
                plan[f"{group}/{index}/path"] = path
    for group in LAUNCHER_VALUE_GROUPS:
        if any(key.startswith(group + "/") for key in current):
            continue
        for key, value in old.items():
            if key.startswith(group + "/"):
                plan[key] = value
    return plan


#
# Extensions
#


def extensionDescriptions(folder):
    """``{name: bytes of <name>.s4ext}`` for the extensions in ``folder`` that have both their description file and
    their folder."""
    try:
        names = os.listdir(folder)
    except OSError:
        return {}
    found = {}
    for name in names:
        if not name.endswith(".s4ext"):
            continue
        extension = name[: -len(".s4ext")]
        if not os.path.isdir(os.path.join(folder, extension)):
            continue
        try:
            with open(os.path.join(folder, name), "rb") as f:
                found[extension] = f.read()
        except OSError:
            continue
    return found


def parseDescription(data):
    values = {}
    for line in data.decode("utf-8", errors="replace").splitlines():
        key, _separator, value = line.strip().partition(" ")
        if key:
            values[key] = value.strip()
    return values


def compatible(description, serverRevision, os_, arch):
    """True when an extension was built for this build (its ``slicer_revision``, ``os`` and ``arch``, when given)."""
    values = parseDescription(description)
    for key, expected in (("slicer_revision", serverRevision), ("os", os_), ("arch", arch)):
        if values.get(key) and expected and values[key] != str(expected):
            return False
    return True


def descriptionDigest(data):
    return hashlib.sha256(data).hexdigest()


def planExtensionsSync(store, bundle, synced):
    """What to do between the store and the bundle's extensions folder (macOS); ``store`` and ``bundle`` are
    ``extensionDescriptions()``, ``synced`` is ``{name: descriptionDigest}`` of what the bundle held at its last sync
    with this same store (its marker), or None when it never synced with it (a new installation, another profile).

    Without ``synced`` the two are merged both ways and nothing is removed: the bundle gets what only the store has
    (``restore``) and the store what only the bundle has (``capture``). With it, a change made in this bundle since its
    last sync is copied to the store (``capture``: installed or updated here), an extension that this bundle had at its
    last sync and no longer has goes from the store to the trash (``trash``: uninstalled here; only when the store
    still holds the version synced then), and what the store got from another application since is copied into the
    bundle (``restore``: installed or updated there). Several bundles built on the same 3D Slicer share the store this
    way. Nothing is ever deleted: a replaced copy goes to the trash."""
    restore, capture, trash = [], [], []
    for name in sorted(set(store) | set(bundle)):
        inStore, inBundle = store.get(name), bundle.get(name)
        if inStore == inBundle:
            continue
        if synced is None:
            if inBundle is None:
                restore.append(name)
            elif inStore is None:
                capture.append(name)
            continue  # both have it, different: left as they are
        before = synced.get(name)
        if inBundle is not None:
            if before is None or descriptionDigest(inBundle) != before:
                capture.append(name)  # installed or updated in this bundle
            elif inStore is not None and descriptionDigest(inStore) != before:
                restore.append(name)  # updated through another application
            # else: unchanged here and removed through another application: left in this bundle
        elif before is not None:
            if descriptionDigest(inStore) == before:
                trash.append(name)  # uninstalled in this bundle
        else:
            restore.append(name)  # installed through another application
    action = "noop"
    if restore or capture or trash:
        action = "restore" if restore and not capture and not trash else "capture" if not restore else "merge"
    return {"action": action, "restore": restore, "capture": capture, "trash": trash}


def storeIdentity(store):
    """What the marker of a bundle records of the store it mirrors: its real path (another profile, or the same
    revision in another profile folder, is another store)."""
    return os.path.realpath(store)


def syncedNames(marker, store):
    """``{name: digest}`` of the marker when it was written for ``store``, else None."""
    if not isinstance(marker, dict) or marker.get("store") != storeIdentity(store):
        return None
    names = marker.get("names")
    return {str(name): str(digest) for name, digest in names.items()} if isinstance(names, dict) else None


def copyExtension(name, source, destination, trashRoot=None, platform=None, now=None):
    """Copy the extension ``name`` (folder and ``.s4ext``) from the extensions folder ``source`` to ``destination``,
    verified. An older copy in ``destination`` goes to ``trashRoot`` (when given) once the new one is complete. None
    on success, else the reason."""
    staging = os.path.join(destination, f".incoming-{os.getpid()}")
    shutil.rmtree(staging, ignore_errors=True)
    problem = copyTreeVerified(os.path.join(source, name), os.path.join(staging, name), platform)
    if problem is not None:
        shutil.rmtree(staging, ignore_errors=True)
        return problem
    try:
        if os.path.lexists(os.path.join(destination, name)):
            if trashRoot is None:
                raise OSError(f"{name} is already there")
            trashExtension(name, destination, trashRoot, now)
        os.replace(os.path.join(staging, name), os.path.join(destination, name))
        _copyFileAtomic(os.path.join(source, f"{name}.s4ext"), os.path.join(destination, f"{name}.s4ext"))
    except OSError as error:
        return str(error)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return None


def trashExtension(name, folder, trashRoot, now=None):
    """Move the extension ``name`` out of ``folder`` into ``trashRoot/<date>/`` (never deleted here)."""
    now = time.time() if now is None else now
    day = os.path.join(trashRoot, time.strftime("%Y-%m-%d", time.localtime(now)))
    os.makedirs(day, exist_ok=True)
    target = os.path.join(day, name)
    if os.path.lexists(target):
        target = f"{target}-{int(now)}-{os.getpid()}"
    description = os.path.join(folder, f"{name}.s4ext")
    if os.path.lexists(description):
        os.replace(description, target + ".s4ext")
    if os.path.lexists(os.path.join(folder, name)):
        os.replace(os.path.join(folder, name), target)
    return target


def purgeTrash(trashRoot, now=None, days=TRASH_DAYS):
    """Delete the days of ``trashRoot`` older than ``days`` (only ``YYYY-MM-DD`` folders inside it)."""
    now = time.time() if now is None else now
    removed = []
    try:
        names = os.listdir(trashRoot)
    except OSError:
        return removed
    for name in names:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", name):
            continue
        try:
            day = time.mktime(time.strptime(name, "%Y-%m-%d"))
        except ValueError:
            continue
        if now - day > days * 86400:
            shutil.rmtree(os.path.join(trashRoot, name), ignore_errors=True)
            removed.append(name)
    return removed


def importComplete(progress):
    """True when the import record ``progress`` (``StudioProfile.json``, ``migrations["extensions:<store>"]``) has
    nothing left to copy."""
    if not isinstance(progress, dict):
        return True
    if "complete" in progress:
        return bool(progress["complete"])
    return not progress.get("problems")


def importExtensions(candidates, store, serverRevision, os_, arch, freeBytesFn=freeBytes, platform=None, progress=None, onStart=None):
    """Fill ``store`` from the newest previous build that has extensions (``candidates`` of
    ``findRevisionCandidates``), copying only the extensions built for this build, while ``RESERVED_FREE_BYTES``
    stay free. ``progress`` is the record of an earlier import into this store (None: there was none): an import
    that did not finish (the application quit or crashed during the copy, not enough space) is taken up again from
    the same source at every start until nothing is left; a finished one, or a store that the user filled without
    an import, is never filled again. ``onStart(candidate, names)`` is called before the first copy (to record that an import
    is under way). Return ``{"source", "candidate", "copied", "skipped", "problems", "complete"}`` (``source`` None
    when there was nothing to do)."""
    result = {"source": None, "candidate": None, "copied": [], "skipped": [], "problems": {}, "complete": True}
    if progress is None and extensionDescriptions(store):
        return result
    if progress is not None and importComplete(progress):
        return result
    chosen = None
    if progress is not None:
        folder = progress.get("sourcePath")
        if folder and extensionDescriptions(folder):
            chosen = {"extensions": folder, "ini": progress.get("ini"), "home": progress.get("home"), "kind": progress.get("sourceKind"),
                      "revision": progress.get("revision")}
        else:
            # The previous installation is gone: what was not copied cannot be any more (Settings > Profile lists it)
            result.update(source=folder or "", candidate=None, complete=True,
                          problems={str(name): "the previous installation is gone" for name in progress.get("problems") or {}})
            return result
    else:
        for candidate in candidates:
            folder = candidate.get("extensions")
            if folder and extensionDescriptions(folder):
                chosen = candidate
                break
    if chosen is None:
        return result
    folder = chosen["extensions"]
    found = extensionDescriptions(folder)
    result.update(source=folder, candidate=chosen)
    os.makedirs(store, exist_ok=True)
    present = extensionDescriptions(store)
    todo = []
    for name in sorted(found):
        if not compatible(found[name], serverRevision, os_, arch):
            result["skipped"].append(name)
        elif name not in present:  # else copied by an earlier start, or installed by the user since
            todo.append(name)
    if onStart is not None:
        onStart(chosen, todo)
    for name in todo:
        size = treeSize(os.path.join(folder, name))
        free = freeBytesFn(store)
        if free is None or free - size <= RESERVED_FREE_BYTES:
            result["problems"][name] = "not enough free space"
            continue
        problem = copyExtension(name, folder, store, platform=platform)
        if problem is None:
            result["copied"].append(name)
        else:
            result["problems"][name] = problem
    result["complete"] = not result["problems"]
    return result


def _record(plan, name, problem):
    if problem is None:
        plan["done"].append(name)
    else:
        plan["problems"][name] = problem


def syncBundle(store, bundle, trashRoot, freeBytesFn=freeBytes, platform=None, now=None):
    """macOS: keep the store and the bundle's extensions folder the same (``planExtensionsSync``). Return the plan
    with ``done`` and ``problems``."""
    storeFound = extensionDescriptions(store)
    bundleFound = extensionDescriptions(bundle)
    marker = readJson(os.path.join(bundle, SYNC_MARKER))
    synced = syncedNames(marker, store)
    plan = planExtensionsSync(storeFound, bundleFound, synced)
    plan["done"], plan["problems"] = [], {}
    if plan["action"] == "noop" and synced is not None and synced == {name: descriptionDigest(data) for name, data in bundleFound.items()}:
        return plan
    try:
        os.makedirs(bundle, exist_ok=True)
        os.makedirs(store, exist_ok=True)
    except OSError as error:
        plan["problems"]["*"] = str(error)
        return plan
    for name in plan["restore"]:
        size = treeSize(os.path.join(store, name))
        free = freeBytesFn(bundle)
        if free is None or (free - size <= RESERVED_FREE_BYTES and platform != "darwin"):
            # A clone (APFS) takes no space; elsewhere the copy must leave the reserve free
            plan["problems"][name] = "not enough free space"
            continue
        _record(plan, name, copyExtension(name, store, bundle, trashRoot=trashRoot, platform=platform, now=now))
    for name in plan["capture"]:
        _record(plan, name, copyExtension(name, bundle, store, trashRoot=trashRoot, platform=platform, now=now))
    for name in plan["trash"]:
        try:
            trashExtension(name, store, trashRoot, now)
            plan["done"].append(name)
        except OSError as error:
            plan["problems"][name] = str(error)
    if not plan["problems"]:
        names = {name: descriptionDigest(data) for name, data in extensionDescriptions(bundle).items()}
        try:
            writeJsonAtomic(os.path.join(bundle, SYNC_MARKER), {"format": FORMAT, "store": storeIdentity(store), "names": names, "synced": time.time()})
        except OSError as error:
            plan["problems"]["*"] = str(error)
    return plan


#
# Python packages
#


def pipArguments(args):
    """The pip arguments ``args`` with ``--user`` added to an ``install`` that names no other destination."""
    args = list(args)
    if not args or args[0] != "install":
        return args
    for arg in args[1:]:
        if arg in ("--user", "--target", "-t", "--prefix", "--root") or arg.startswith(("--target=", "--prefix=", "--root=")) or (arg.startswith("-t") and len(arg) > 2 and not arg.startswith("--")):
            return args
    return ["install", "--user"] + args[1:]


def profileEnvironment(env, userBase, site):
    """The environment changes that make child Pythons (pip, PythonSlicer) use the profile's packages: a dict of
    name -> value (None: remove)."""
    paths = [entry for entry in (env.get("PYTHONPATH") or "").split(os.pathsep) if entry]
    if site not in paths:
        paths.append(site)
    return {USERBASE_ENVIRONMENT_VARIABLE: userBase, "PYTHONUSERBASE": userBase, "PYTHONPATH": os.pathsep.join(paths)}


def addUserSite(site, sysPath=None):
    """Add ``site`` as Python adds its user site (``site.addsitedir``: its ``.pth`` files are processed, ``import``
    lines included, as editable installs and other packages need), then move what was added before the first
    site-packages folder of ``sysPath`` (default ``sys.path``), where Python puts the user site. Return the entries
    added."""
    import site as _site

    sysPath = sys.path if sysPath is None else sysPath
    before = list(sysPath)
    saved = sys.path
    sys.path = sysPath  # addsitedir works on sys.path
    try:
        _site.addsitedir(site)
    finally:
        sys.path = saved
    added = [entry for entry in sysPath if entry not in before]
    added = list(dict.fromkeys(added))
    rest = [entry for entry in sysPath if entry not in added]
    index = next((i for i, entry in enumerate(rest) if isinstance(entry, str) and os.path.basename(entry.rstrip("/")) in ("site-packages", "dist-packages")), len(rest))
    sysPath[:] = rest[:index] + added + rest[index:]
    return added


def previousPythonFolders(profileRoot, current):
    """Other package folders of the profile (other Python versions), with the distributions they hold."""
    base = os.path.join(profileRoot, PYTHON_FOLDER)
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    found = []
    for name in names:
        folder = os.path.join(base, name)
        if folder == current or not os.path.isdir(folder):
            continue
        distributions = []
        for folderPath, dirs, _files in os.walk(folder):
            for entry in dirs:
                if entry.endswith(".dist-info"):
                    distributions.append(entry[: -len(".dist-info")].rsplit("-", 1)[0])
            if os.path.basename(folderPath) == "site-packages":
                dirs[:] = []
        found.append((folder, sorted(set(distributions), key=str.lower)))
    return found


#
# Start-up
#


def _truthy(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _settingsDict(settings):
    return {str(key): settings.value(key) for key in settings.allKeys()}


def buildConfig():
    """The build values of ``studio_build_config.py`` (configured by CMake), or {} (a build without it)."""
    try:
        import studio_build_config
    except ImportError:
        return {}
    return {name: getattr(studio_build_config, name) for name in dir(studio_build_config) if name.isupper()}


def status():
    return dict(_status)


def _recordMigration(manifest, identifier, kind, source, details=None):
    migrations = manifest.setdefault("migrations", {})
    entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": kind, "sourceName": os.path.basename(source or "")}
    if details:
        entry.update(details)
    migrations[identifier] = entry


def trimPrefetchLists(folder, keep=PREFETCH_KEEP):
    """Keep the newest ``keep`` startup prefetch lists of Slicer Studio in ``folder``."""
    try:
        names = [name for name in os.listdir(folder) if re.fullmatch(r"SlicerStudio-\d+-StartupFilePrefetch\.txt", name)]
    except OSError:
        return []
    paths = sorted((os.path.join(folder, name) for name in names), key=lambda path: os.stat(path).st_mtime, reverse=True)
    removed = []
    for path in paths[keep:]:
        try:
            os.remove(path)
            removed.append(path)
        except OSError:
            pass
    return removed


def pendingDirectoryProblem(path, currentRoot, forbidden=()):
    """None when the folder ``path`` can become the profile folder, else why not: ``validateOverride``, inside or
    around the current profile, or a folder that holds other files (only an empty folder, or one that already holds a
    Slicer Studio profile, chosen on purpose, can be used: nothing of the current profile would be found in another
    folder)."""
    problem = validateOverride(path, forbidden)
    if problem is not None:
        return problem
    if currentRoot and os.path.realpath(path) == os.path.realpath(currentRoot):
        return None
    if currentRoot and (isInside(path, currentRoot) or isInside(currentRoot, path)):
        return "it is inside the current profile folder (or holds it)"
    if os.path.isdir(path) and os.listdir(path) and not os.path.isfile(os.path.join(path, MANIFEST_FILE)):
        return "it is not empty and does not hold a Slicer Studio profile"
    return None


def applyPendingDirectory(userSettings, currentRoot, env, home, platform, forbidden):
    """A profile folder chosen in Settings takes effect at the next start (here, before anything uses the profile): the
    current profile is copied there (verified) when that folder is empty, or that folder is used as it is when it
    already holds a profile; a folder with other files is refused (``pendingDirectoryProblem``). Only then does
    ``Studio/Profile/Directory`` name it, and ``Studio/Profile/PreviousDirectory`` the old folder, which is kept.
    Return the new folder, or None."""
    pending = userSettings.value(PENDING_DIRECTORY_KEY)
    if not pending:
        return None
    path = expandPath(pending, home)
    problem = pendingDirectoryProblem(path, currentRoot, forbidden) if path else "it is not an absolute folder"
    if problem is None and env.get(ENVIRONMENT_VARIABLE):
        problem = f"{ENVIRONMENT_VARIABLE} is set"
    if problem is None and currentRoot and os.path.realpath(path) != os.path.realpath(currentRoot):
        empty = not os.path.isdir(path) or not os.listdir(path)
        if empty and os.path.isdir(currentRoot):
            with ProfileLock(currentRoot) as lock:
                if not lock.acquired:
                    return None  # another Slicer Studio uses the profile: next start
                if os.path.isdir(path):
                    os.rmdir(path)
                problem = copyTreeVerified(currentRoot, path, platform)
    userSettings.remove(PENDING_DIRECTORY_KEY)
    if problem is not None:
        _log(f"the profile folder was not changed to {pending}: {problem}")
        userSettings.sync()
        return None
    if currentRoot and os.path.realpath(path) != os.path.realpath(currentRoot):
        userSettings.setValue(PREVIOUS_DIRECTORY_KEY, currentRoot)
    userSettings.setValue(DIRECTORY_KEY, path)
    userSettings.sync()
    return path


def startup(app=None, platform=None):
    """Called by ``slicerqt.py`` before the overlay of the code updates and anything else (see the module
    documentation). Errors go to the caller, which prints them and goes on. ``platform``: for tests."""
    app = _application() if app is None else app
    if app is None:
        return status()
    options = _value(app, "commandOptions")
    if settingsDisabled(app) or bool(_value(options, "testingEnabled", False)) or bool(_value(app, "testingEnabled", False)):
        _status.update(state="skipped", reason="testing or temporary settings")
        return status()
    platform = sys.platform if platform is None else platform
    home = os.path.expanduser("~")
    slicerHome = str(_value(app, "slicerHome", "") or "")
    userSettings = _value(app, "userSettings")
    if userSettings is not None:
        try:
            if applyPendingDirectory(userSettings, root(), os.environ, home, platform, applicationRoots(slicerHome, platform)):
                setRoot(None)
        except Exception as error:
            _log(f"the new profile folder could not be prepared: {error}")
    profile = root()
    if not profile:
        _status.update(state="skipped", reason="no profile folder")
        return status()
    config = buildConfig()
    if portableBuild(config):
        # Slicer_STORE_SETTINGS_IN_APPLICATION_HOME_DIR ON: a portable build keeps everything inside itself
        _status.update(state="portable", root=profile)
        return status()
    _source, problem = rootSource()
    try:
        os.makedirs(profile, exist_ok=True)  # an override is used only when it exists (resolveRoot): this is the default
    except OSError as error:
        _status.update(state="error", reason=str(error))
        return status()
    _setUpPython(app, profile, userSettings)
    if problem:
        # The chosen profile folder is missing: this run uses the default folder, and nothing is carried, imported,
        # synced or trashed (the real profile may come back; Settings > Profile shows the problem)
        _status.update(state="fallback", root=profile, reason=problem)
        return status()
    if not bool(_value(app, "isInstalled", False)):
        _status.update(state="build tree", root=profile)
        return status()
    revisionSettings = _value(app, "revisionUserSettings")
    if revisionSettings is None:
        return status()
    revision = str(_value(app, "revision", "") or "")
    serverRevision = str(config.get("EXTENSIONS_SERVER_REVISION") or "")
    os_, arch = str(_value(app, "os", "") or ""), str(_value(app, "arch", "") or "")
    store = extensionsStore(profile, serverRevision, os_, arch) if serverRevision and os_ and arch else None
    bundle = _inAppExtensions(slicerHome, revision, "darwin") if platform == "darwin" else None
    initialized = _truthy(revisionSettings.value(INITIALIZED_KEY))
    if store is not None and bundle is None and revisionSettings.value("Extensions/InstallPath") != store:
        # Linux: the extensions are installed in the store (set before the extensions manager reads it, even when
        # another Slicer Studio holds the lock of the profile below)
        try:
            os.makedirs(store, exist_ok=True)
            revisionSettings.setValue("Extensions/InstallPath", store)
            revisionSettings.sync()
        except OSError as error:
            _log(f"the extensions folder {store} cannot be created: {error}")
    if initialized and not importPending(profile, store) and (bundle is None or store is None or _bundleLooksSynced(store, bundle)):
        _status.update(state="ready", root=profile)
        return status()
    with ProfileLock(profile) as lock:
        if not lock.acquired:
            _status.update(state="busy", root=profile, reason="another Slicer Studio is updating the profile")
            return status()
        _migrate(app, profile, revisionSettings, revision, slicerHome, store, bundle, serverRevision, os_, arch, initialized, platform)
    return status()


def portableBuild(config=None):
    """True for a build with ``Slicer_STORE_SETTINGS_IN_APPLICATION_HOME_DIR`` ON (``studio_build_config``): it keeps
    its settings and extensions inside itself, so the profile neither takes its extensions nor carries settings."""
    config = buildConfig() if config is None else config
    return _truthy(config.get("STORE_SETTINGS_IN_APPLICATION_HOME_DIR", ""))


def importKey(store):
    return f"extensions:{os.path.basename(store)}"


def importPending(profile, store):
    """True when an import of extensions into ``store`` did not finish (one small file read)."""
    if store is None:
        return False
    manifest = readJson(os.path.join(profile, MANIFEST_FILE)) or {}
    progress = (manifest.get("migrations") or {}).get(importKey(store))
    return progress is not None and not importComplete(progress)


def pendingImports(profile):
    """``{store name: {"source", "names"}}``: the imports of the profile that did not finish, or that finished without
    some extensions (the previous installation is gone), with the names still missing."""
    manifest = readJson(os.path.join(profile, MANIFEST_FILE)) or {}
    found = {}
    for key, entry in (manifest.get("migrations") or {}).items():
        names = sorted(name for name in (entry.get("problems") or {}) if name != "*") if isinstance(entry, dict) else []
        if not str(key).startswith("extensions:") or not names:
            continue
        found[str(key)[len("extensions:"):]] = {"source": entry.get("sourceName"), "names": names}
    return found


def otherStoreExtensions(profile, store):
    """Names of the extensions in the other stores of ``profile`` for the same os and architecture as ``store`` (built
    for another 3D Slicer: after a merge of a newer 3D Slicer they must be installed again)."""
    base = os.path.join(profile, EXTENSIONS_FOLDER)
    current = os.path.basename(store) if store else ""
    _revision, _separator, suffix = current.partition("-")
    try:
        names = os.listdir(base)
    except OSError:
        return []
    found = set()
    for name in names:
        if name == current or not suffix or not name.endswith("-" + suffix) or ".partial-" in name or name.startswith("."):
            continue
        found.update(extensionDescriptions(os.path.join(base, name)))
    return sorted(found, key=str.lower)


def planPathAdditions(old, current, ctx):
    """The values to write so that ``current`` also has the paths of ``old`` (a previous build's settings) that lead
    into this build's extensions folder (``ctx["newExtensions"]``) and that it lacks: for extensions copied after the
    settings were carried forward (an import taken up again). Other paths are never added again (the user may have
    removed them since)."""
    newExtensions = ctx.get("newExtensions") or ""
    newHome = ctx.get("newHome") or ""

    def wanted(entries):
        mapped = []
        for entry in entries:
            path = mapPath(entry, ctx)
            if not path:
                continue
            absolute = path if os.path.isabs(path) else os.path.normpath(os.path.join(newHome, path))
            if newExtensions and isInside(absolute, newExtensions) and path not in mapped:
                mapped.append(path)
        return mapped

    plan = {}
    for key in PATH_LIST_KEYS:
        existing = _asList(current.get(key))
        additions = [path for path in wanted(_asList(old.get(key))) if path not in existing]
        if additions:
            plan[key] = _settingsValue(existing + additions)
    for group in LAUNCHER_PATH_GROUPS:
        existing = _readArray(current, group)
        additions = [path for path in wanted(_readArray(old, group)) if path not in existing]
        if additions:
            paths = existing + additions
            plan[f"{group}/size"] = len(paths)
            for index, path in enumerate(paths, start=1):
                plan[f"{group}/{index}/path"] = path
    return plan


def _bundleLooksSynced(store, bundle):
    """The quick check of every start (macOS): the bundle was synced with this store and both hold the same
    extension descriptions."""
    if syncedNames(readJson(os.path.join(bundle, SYNC_MARKER)), store) is None:
        return False
    return extensionDescriptions(store) == extensionDescriptions(bundle)


def _setUpPython(app, profile, userSettings):
    enabled = True
    if userSettings is not None:
        value = userSettings.value(PIP_KEY)
        enabled = value is None or value == "" or _truthy(value)
    if not enabled:
        return
    userBase = pythonUserBase(profile)
    site = userSitePackages(userBase)
    try:
        os.makedirs(site, exist_ok=True)  # exists before the first import, so that a package installed later is found
    except OSError as error:
        _log(f"the Python packages folder cannot be created: {error}")
        return
    addUserSite(site)
    for name, value in profileEnvironment(os.environ, userBase, site).items():
        os.environ[name] = value
        try:
            app.setEnvironmentVariable(name, value)
        except Exception:
            pass


def _carryContext(source, slicerHome, newExtensions, platform, old):
    oldHome = str(source.get("home") or "")
    oldExtensionRoots = [root for root in (source.get("extensions"), _inAppExtensions(oldHome, source.get("revision"), platform) if oldHome else None,
                                           old.get("Extensions/InstallPath")) if root]
    if source.get("kind") == "snapshot":
        oldExtensionRoots.append(os.path.join(os.path.dirname(source["ini"]), "Extensions"))
    return {"oldHome": oldHome, "newHome": os.path.realpath(slicerHome), "oldExtensionRoots": [str(root) for root in oldExtensionRoots],
            "newExtensions": newExtensions}


def _migrate(app, profile, revisionSettings, revision, slicerHome, store, bundle, serverRevision, os_, arch, initialized, platform):
    started = time.monotonic()
    manifestPath = os.path.join(profile, MANIFEST_FILE)
    manifest = readJson(manifestPath) or {"format": FORMAT}
    configDir = os.path.dirname(str(_value(app, "slicerRevisionUserSettingsFilePath", "") or ""))
    home = os.path.expanduser("~")
    candidates = findRevisionCandidates([configDir, os.path.join(home, ".config", ORGANIZATION)], profile, slicerHome, revision, platform)
    restartNeeded = False
    newExtensions = None
    resumed = None
    # 1. Extensions: the store gets the extensions of the previous build when it has none yet, and an import that did
    # not finish is taken up again
    if store is not None:
        key = importKey(store)
        progress = (manifest.get("migrations") or {}).get(key)
        earlier = list(progress.get("copied") or []) if isinstance(progress, dict) else []

        def onStart(candidate, names):
            if not names:
                return
            _recordMigration(manifest, key, "extensions", candidate.get("extensions"), {
                "sourcePath": candidate.get("extensions"), "ini": candidate.get("ini"), "home": candidate.get("home"),
                "sourceKind": candidate.get("kind"), "revision": candidate.get("revision"), "copied": earlier, "skipped": [],
                "problems": dict.fromkeys(names, "not copied yet"), "complete": False})
            try:
                writeJsonAtomic(manifestPath, manifest)
            except OSError as error:
                _log(f"the profile record cannot be written: {error}")

        imported = importExtensions(candidates, store, serverRevision, os_, arch, platform=platform, progress=progress, onStart=onStart)
        if imported["source"] is not None:
            candidate = imported["candidate"] or {}
            _recordMigration(manifest, key, "extensions", imported["source"], {
                "sourcePath": imported["source"], "ini": candidate.get("ini"), "home": candidate.get("home"),
                "sourceKind": candidate.get("kind"), "revision": candidate.get("revision"),
                "copied": sorted(set(earlier) | set(imported["copied"])), "skipped": imported["skipped"], "problems": imported["problems"],
                "complete": imported["complete"]})
            restartNeeded = restartNeeded or bool(imported["copied"])
            if progress is not None and imported["copied"] and candidate.get("ini"):
                resumed = candidate
            if imported["problems"] or imported["skipped"]:
                _log(f"extensions not carried forward: {', '.join(sorted(set(imported['skipped']) | set(imported['problems'])))}")
        if bundle is not None:
            plan = syncBundle(store, bundle, os.path.join(profile, TRASH_FOLDER), platform=platform)
            if plan["restore"] and plan["done"]:
                restartNeeded = True
                _recordMigration(manifest, f"restore:{revision}", "restore", store, {"names": plan["done"]})
            if plan["problems"]:
                _log("extensions not synced: " + ", ".join(f"{name} ({reason})" for name, reason in plan["problems"].items()))
            newExtensions = bundle
        else:
            newExtensions = store
        purgeTrash(os.path.join(profile, TRASH_FOLDER))
    # 2. The revision-specific settings of the previous build
    if not initialized:
        source = candidates[0] if candidates else None
        if source is not None:
            import qt

            old = _settingsDict(qt.QSettings(source["ini"], qt.QSettings.IniFormat))
            current = _settingsDict(revisionSettings)
            defaultSettings = _value(app, "defaultSettings")
            defaults = _settingsDict(defaultSettings) if defaultSettings is not None else {}
            ctx = _carryContext(source, slicerHome, newExtensions, platform, old)
            plan = planCarryForward(old, current, defaults, ctx)
            for key, value in plan.items():
                revisionSettings.setValue(key, value)
            revisionSettings.setValue(CARRIED_FROM_KEY, os.path.basename(source["ini"]))
            _recordMigration(manifest, f"settings:{revision}", "settings", source["ini"], {"keys": len(plan)})
            if any(key.split("/")[0] in LAUNCHER_PATH_GROUPS for key in plan):
                restartNeeded = True  # the launcher set the library paths of this run before they were carried
        revisionSettings.setValue(INITIALIZED_KEY, True)
    elif resumed is not None and resumed.get("home") and os.path.isfile(str(resumed["ini"])):
        # 3. Extensions copied by an import taken up again: their module and library paths, which the settings carried
        # forward at an earlier start could not have (the extensions were not there yet)
        import qt

        old = _settingsDict(qt.QSettings(resumed["ini"], qt.QSettings.IniFormat))
        ctx = _carryContext(resumed, slicerHome, newExtensions, platform, old)
        plan = planPathAdditions(old, _settingsDict(revisionSettings), ctx)
        for key, value in plan.items():
            revisionSettings.setValue(key, value)
    if restartNeeded:
        revisionSettings.setValue(RESTART_KEY, True)
    revisionSettings.sync()
    manifest["format"] = FORMAT
    manifest["lastBuild"] = {"revision": revision, "version": str(_value(app, "applicationVersion", "") or ""),
                             "commit": (readJson(os.path.join(slicerHome, "bin", "Python", "studio_build_info.json")) or {}).get("commit")}
    try:
        writeJsonAtomic(os.path.join(profile, MANIFEST_FILE), manifest)
    except OSError as error:
        _log(f"the profile record cannot be written: {error}")
    trimPrefetchLists(configDir)
    elapsed = time.monotonic() - started
    if elapsed > 2:
        _log(f"carrying the extensions and settings forward took {elapsed:.0f} s")
    _status.update(state="migrated", root=profile, restartNeeded=restartNeeded, seconds=round(elapsed, 1))

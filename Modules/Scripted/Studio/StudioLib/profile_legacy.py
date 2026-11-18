"""Snapshots of the user data that an installed Slicer Studio keeps inside the application (the user profile, phase 1).

Builds made before the user profile (``studio_profile.py``) keep two kinds of user data inside the application, so that
replacing the application (dragging a new ``Slicer Studio.app`` over the old one, extracting a new Linux package, or an
in-app update) loses them:

- the revision-specific settings ``<home>/slicer.org/Slicer-<rev>.ini`` (installed extensions, extra module paths, the
  launcher paths of the extensions);
- the installed extensions: ``<bundle>/Contents/Extensions-<rev>`` on macOS, ``<home>/slicer.org/Extensions-<rev>`` on
  Linux;
- and the Python packages installed with pip, in the site-packages folder of the application.

``snapshot()`` copies the first two (and the names of the third) into the user profile, outside the application:
``<profile>/StudioLegacy/<rev>-<commit7>/`` with ``Slicer-<rev>.ini``, ``Extensions/`` (only while at least
``RESERVED_FREE_BYTES`` stay free; otherwise only the names in ``extensions.json``), ``pip.json`` (the packages the user
installed: ``*.dist-info`` newer than the build) and ``snapshot.json``. The copy is made in a ``.partial-<pid>`` folder,
verified (paths and sizes; SHA-256 of files up to 1 MB), then renamed in place: a snapshot is complete or absent. It is
made again only when the source changed since, and the newest ``KEEP_SNAPSHOTS`` are kept. The source is never changed.

A build with the user profile (``Base/Python/studio_profile.py``) reads these snapshots at its first start and carries
the settings, the extensions and the package names forward. Snapshots are taken:

- once per start, a while after start-up (``scheduleSnapshot``, from ``Studio._completeStartup``), in a background thread;
- by ``updates.finishInstall()``, from the previous version kept by an in-app update, before it is deleted.

``<profile>`` is ``studio_profile.root()`` when that module exists (a build with the user profile), else the private
application data folder (``QStandardPaths.AppLocalDataLocation``, its default). No Qt at import time: the tests run this
with plain Python.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

LEGACY_FOLDER = "StudioLegacy"
SNAPSHOT_FILE = "snapshot.json"
EXTENSIONS_FOLDER = "Extensions"
EXTENSIONS_LIST = "extensions.json"
PIP_LIST = "pip.json"
LOCK_FILE = "snapshot.lock"
FORMAT = 1

#: How many snapshots are kept (the newest first)
KEEP_SNAPSHOTS = 2
#: Free space that must remain after copying the extensions (the 5 GB rule of the project)
RESERVED_FREE_BYTES = 5 * 1024 * 1024 * 1024
#: Files up to this size are compared by SHA-256 after a copy; larger ones by size
HASH_LIMIT = 1024 * 1024
#: Delay of the snapshot after start-up (ms): after the start-up work, so that it never slows it down
SNAPSHOT_DELAY_MS = 20000
#: A package installed less than this after the build file was written is part of the build (seconds)
BUILD_SLACK_S = 120

BUILD_INFO = os.path.join("bin", "Python", "studio_build_info.json")
_REVISION_INI = re.compile(r"^Slicer-(\d+)\.ini$")
_DIST_INFO = re.compile(r"^(?P<name>.+?)-(?P<version>[^-]+)\.dist-info$")


#
# Where things are
#


def profileRoot():
    """The user profile folder: ``studio_profile.root()`` in a build that has it, else the private application data
    folder (its default). Empty when there is none."""
    try:
        import studio_profile
    except ImportError:
        studio_profile = None
    if studio_profile is not None:
        return studio_profile.root()
    import qt

    return str(qt.QStandardPaths.writableLocation(qt.QStandardPaths.AppLocalDataLocation) or "")


def inAppLocations(slicerHome, platform=None):
    """The revision-specific settings files that the installation in ``slicerHome`` keeps inside itself, newest
    revision first: a list of ``{"revision", "ini", "extensions"}`` (``extensions`` is the folder of that revision's
    extensions, which may not exist). ``slicerHome`` is ``slicer.app.slicerHome`` (``<bundle>/Contents`` on macOS)."""
    platform = sys.platform if platform is None else platform
    settingsDir = os.path.join(slicerHome, "slicer.org")
    try:
        names = os.listdir(settingsDir)
    except OSError:
        names = []
    found = []
    for name in names:
        match = _REVISION_INI.match(name)
        if not match:
            continue
        revision = match.group(1)
        if platform == "darwin":
            # qSlicerCoreApplicationPrivate::defaultExtensionsInstallPathForMacOSX
            extensions = os.path.join(os.path.dirname(slicerHome), "Contents", f"Extensions-{revision}")
        else:
            extensions = os.path.join(settingsDir, f"Extensions-{revision}")
        found.append({"revision": revision, "ini": os.path.join(settingsDir, name), "extensions": extensions})
    found.sort(key=lambda entry: int(entry["revision"]), reverse=True)
    return found


def readExtensionDescription(path):
    """The ``key value`` lines of an ``.s4ext`` file as a dict (empty when it cannot be read)."""
    values = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                key, _separator, value = line.partition(" ")
                values[key] = value.strip()
    except OSError:
        pass
    return values


def installedExtensionNames(extensionsDir):
    """The extensions installed in ``extensionsDir``: the ``<name>.s4ext`` files at its top whose ``installed`` value is
    not false, sorted."""
    try:
        names = os.listdir(extensionsDir)
    except OSError:
        return []
    found = []
    for name in names:
        if not name.endswith(".s4ext"):
            continue
        description = readExtensionDescription(os.path.join(extensionsDir, name))
        if str(description.get("installed", "true")).lower() in ("false", "0"):
            continue
        found.append(name[: -len(".s4ext")])
    return sorted(found, key=str.lower)


def sitePackagesFolders(slicerHome):
    """The site-packages folders of the Python of the installation in ``slicerHome``."""
    base = os.path.join(slicerHome, "lib", "Python", "lib")
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    return [os.path.join(base, name, "site-packages") for name in names
            if name.startswith("python3") and os.path.isdir(os.path.join(base, name, "site-packages"))]


def userPipDistributions(sitePackages, builtAfterMtime):
    """The distributions in ``sitePackages`` installed after the build (``*.dist-info`` modified more than
    ``BUILD_SLACK_S`` after ``builtAfterMtime``): a sorted list of ``{"name", "version"}``. Without a build time
    (None), nothing is reported: every package would look user-installed."""
    if builtAfterMtime is None:
        return []
    try:
        names = os.listdir(sitePackages)
    except OSError:
        return []
    found = []
    for name in names:
        match = _DIST_INFO.match(name)
        if not match:
            continue
        try:
            mtime = os.stat(os.path.join(sitePackages, name)).st_mtime
        except OSError:
            continue
        if mtime > builtAfterMtime + BUILD_SLACK_S:
            found.append({"name": match.group("name"), "version": match.group("version")})
    return sorted(found, key=lambda entry: entry["name"].lower())


def buildCommit(slicerHome):
    """The commit the installation in ``slicerHome`` was built from (``studio_build_info.json``), or None."""
    try:
        with open(os.path.join(slicerHome, BUILD_INFO), encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        return None
    commit = info.get("commit") if isinstance(info, dict) else None
    return commit if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit) else None


def buildTime(slicerHome):
    try:
        return os.stat(os.path.join(slicerHome, BUILD_INFO)).st_mtime
    except OSError:
        return None


#
# Copies
#


def freeBytes(path):
    """Free bytes on the file system of ``path`` (or of its nearest existing parent), or None."""
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
    """Bytes of the files in ``path`` (symbolic links are not followed)."""
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


def verifyTree(source, copy):
    """None when ``copy`` holds every file, folder and link of ``source`` (same sizes; same SHA-256 up to
    ``HASH_LIMIT``; same link targets), else the first difference found."""
    for folder, dirs, files in os.walk(source):
        relative = os.path.relpath(folder, source)
        other = os.path.normpath(os.path.join(copy, relative))
        if not os.path.isdir(other):
            return f"missing folder {relative}"
        for name in dirs + files:
            path = os.path.join(folder, name)
            target = os.path.join(other, name)
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
            if size <= HASH_LIMIT and _sha256(path) != _sha256(target):
                return f"different content {os.path.join(relative, name)}"
    return None


def cloneTree(source, destination, platform=None, run=subprocess.run):
    """Copy the folder ``source`` to ``destination`` (which must not exist): a clone where the file system can (APFS:
    ``cp -c``; Btrfs, XFS: ``cp --reflink=auto``), which takes no space, else a plain copy. Links are copied as links."""
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        commands = [["cp", "-c", "-R", "-p", source, destination], ["cp", "-R", "-p", source, destination]]
    elif platform.startswith("linux"):
        commands = [["cp", "-a", "--reflink=auto", source, destination]]
    else:
        commands = []
    for command in commands:
        try:
            if run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                return
        except OSError:
            pass
        shutil.rmtree(destination, ignore_errors=True)
    shutil.copytree(source, destination, symlinks=True)


def copyTreeVerified(source, destination, platform=None):
    """Copy the folder ``source`` to ``destination`` through ``destination.partial-<pid>`` (cloned or copied, then
    checked with ``verifyTree``) and an atomic rename. Return None on success, else the reason (nothing is left
    behind then). ``destination`` must not exist; ``source`` is never changed."""
    if os.path.lexists(destination):
        return "the destination exists"
    partial = f"{destination}.partial-{os.getpid()}"
    shutil.rmtree(partial, ignore_errors=True)
    try:
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        cloneTree(source, partial, platform)
        problem = verifyTree(source, partial)
        if problem is None:
            os.replace(partial, destination)
            return None
    except (OSError, shutil.Error) as error:
        problem = str(error)
    shutil.rmtree(partial, ignore_errors=True)
    return problem


#
# Snapshots
#


def _readJson(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _writeJson(path, data):
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(temporary, path)


def _latestMtime(paths):
    latest = 0.0
    for path in paths:
        try:
            latest = max(latest, os.stat(path).st_mtime)
        except OSError:
            continue
        if os.path.isdir(path):
            for folder, dirs, files in os.walk(path):
                for name in dirs + files:
                    try:
                        latest = max(latest, os.lstat(os.path.join(folder, name)).st_mtime)
                    except OSError:
                        pass
    return latest


def snapshots(profileRootPath):
    """The complete snapshots in the profile, newest first: a list of ``(folder, snapshot.json data)``."""
    base = os.path.join(profileRootPath, LEGACY_FOLDER)
    try:
        names = os.listdir(base)
    except OSError:
        return []
    found = []
    for name in names:
        if ".partial-" in name or ".old-" in name:
            continue
        folder = os.path.join(base, name)
        data = _readJson(os.path.join(folder, SNAPSHOT_FILE))
        if data is not None and data.get("format") == FORMAT:
            found.append((folder, data))
    found.sort(key=lambda item: float(item[1].get("created") or 0), reverse=True)
    return found


def snapshot(slicerHome, profileRootPath, revision=None, freeBytesFn=None, platform=None, now=None):
    """Snapshot the user data kept inside the installation ``slicerHome`` (see the module documentation). ``revision``
    picks the settings file of that revision (default: the newest found). Return the snapshot folder, or None when the
    installation keeps no settings inside itself. Raise OSError when the snapshot cannot be written."""
    platform = sys.platform if platform is None else platform
    freeBytesFn = freeBytes if freeBytesFn is None else freeBytesFn
    now = time.time() if now is None else now
    if not slicerHome or not profileRootPath:
        return None
    locations = inAppLocations(slicerHome, platform)
    if revision is not None:
        locations = [entry for entry in locations if entry["revision"] == str(revision)] or locations
    if not locations:
        return None
    location = locations[0]
    revision = location["revision"]
    commit = buildCommit(slicerHome)
    base = os.path.join(profileRootPath, LEGACY_FOLDER)
    destination = os.path.join(base, f"{revision}-{(commit or 'unknown')[:7]}")
    extensionsDir = location["extensions"] if os.path.isdir(location["extensions"]) else None
    sourceMtime = _latestMtime([location["ini"]] + ([extensionsDir] if extensionsDir else []))
    existing = _readJson(os.path.join(destination, SNAPSHOT_FILE))
    if existing is not None and existing.get("format") == FORMAT and float(existing.get("sourceMtime") or 0) >= sourceMtime:
        return destination  # up to date
    partial = f"{destination}.partial-{os.getpid()}"
    shutil.rmtree(partial, ignore_errors=True)
    os.makedirs(partial)
    try:
        shutil.copy2(location["ini"], os.path.join(partial, os.path.basename(location["ini"])))
        names = installedExtensionNames(extensionsDir) if extensionsDir else []
        copied = False
        problem = None
        if extensionsDir:
            size = treeSize(extensionsDir)
            free = freeBytesFn(base)
            if free is not None and free - size > RESERVED_FREE_BYTES:
                problem = copyTreeVerified(extensionsDir, os.path.join(partial, EXTENSIONS_FOLDER), platform)
                copied = problem is None
            else:
                problem = "not enough free space"
        built = buildTime(slicerHome)
        packages = []
        for folder in sitePackagesFolders(slicerHome):
            packages.extend(userPipDistributions(folder, built))
        _writeJson(os.path.join(partial, EXTENSIONS_LIST), {"extensions": names})
        _writeJson(os.path.join(partial, PIP_LIST), {"packages": packages})
        _writeJson(os.path.join(partial, SNAPSHOT_FILE), {
            "format": FORMAT, "revision": revision, "commit": commit, "home": os.path.realpath(slicerHome), "platform": platform,
            "created": now, "sourceMtime": sourceMtime, "ini": os.path.basename(location["ini"]),
            "extensionsCopied": copied, "extensionsProblem": problem, "extensions": names,
            "extensionsFolder": os.path.basename(location["extensions"]), "packages": packages})
        old = None
        if os.path.lexists(destination):
            old = f"{destination}.old-{os.getpid()}"
            os.replace(destination, old)
        os.replace(partial, destination)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    if old:
        shutil.rmtree(old, ignore_errors=True)
    prune(profileRootPath)
    return destination


def prune(profileRootPath, keep=KEEP_SNAPSHOTS):
    """Remove all but the newest ``keep`` snapshots (only folders of ``StudioLegacy`` that are snapshots), and partial
    copies left by a process that is gone."""
    base = os.path.join(profileRootPath, LEGACY_FOLDER)
    removed = []
    for folder, _data in snapshots(profileRootPath)[keep:]:
        shutil.rmtree(folder, ignore_errors=True)
        removed.append(folder)
    try:
        names = os.listdir(base)
    except OSError:
        names = []
    for name in names:
        match = re.search(r"\.(?:partial|old)-(\d+)$", name)
        if match and not _processAlive(int(match.group(1))):
            shutil.rmtree(os.path.join(base, name), ignore_errors=True)
            removed.append(os.path.join(base, name))
    return removed


def _processAlive(pid):
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


class _Lock:
    """An exclusive ``flock`` on ``<folder>/snapshot.lock`` taken without waiting (``acquired`` False when another
    process holds it). Released by the system if the process dies."""

    def __init__(self, folder):
        self.folder = folder
        self.file = None
        self.acquired = False

    def __enter__(self):
        try:
            import fcntl
        except ImportError:
            self.acquired = True
            return self
        try:
            os.makedirs(self.folder, exist_ok=True)
            self.file = open(os.path.join(self.folder, LOCK_FILE), "a+")
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.acquired = True
        except OSError:
            pass
        return self

    def __exit__(self, *args):
        if self.file is not None:
            self.file.close()  # releases the lock
            self.file = None
        return False


def snapshotLocked(slicerHome, profileRootPath, revision=None, platform=None):
    """``snapshot()`` under the snapshot lock of the profile; None when another Slicer Studio is taking one."""
    with _Lock(os.path.join(profileRootPath, LEGACY_FOLDER)) as lock:
        if not lock.acquired:
            return None
        return snapshot(slicerHome, profileRootPath, revision=revision, platform=platform)


def scheduleSnapshot():
    """Take a snapshot of this installation a while after start-up, in a background thread (installed applications
    only; not with ``--testing`` or ``--disable-settings``)."""
    import logging

    import qt
    import slicer

    app = slicer.app
    if not app.isInstalled or app.testingEnabled():
        return False
    if str(app.slicerRevisionUserSettingsFilePath).endswith("-tmp.ini"):
        return False  # --disable-settings
    home = str(app.slicerHome)
    revision = str(app.revision)
    if not inAppLocations(home):
        return False  # a build with the user profile keeps nothing inside itself

    def run(root):
        try:
            snapshotLocked(home, root, revision=revision)
        except Exception as error:
            logging.warning(f"Slicer Studio could not keep a copy of the settings and extensions of this version: {error}")

    def start():
        root = profileRoot()  # Qt: on the main thread
        if root:
            threading.Thread(target=run, args=(root,), name="StudioProfileSnapshot", daemon=True).start()

    qt.QTimer.singleShot(SNAPSHOT_DELAY_MS, start)
    return True


def homeOfBackup(backup, platform=None):
    """The Slicer home inside the previous version kept by an in-app update (``.<name>.studio-previous``)."""
    platform = sys.platform if platform is None else platform
    return os.path.join(backup, "Contents") if platform == "darwin" else backup

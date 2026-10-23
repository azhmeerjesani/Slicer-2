"""Installing a verified update of Slicer Studio (A-08, decision D-U1).

``updates.py`` downloads a release file and checks its size and SHA-256 against the release manifest before
anything here runs. This module then prepares the new version next to the installed one and swaps the two after
the application has exited:

- **macOS** (the disk image of ``build_dmg.sh``): ``hdiutil attach -nobrowse -readonly``, ``ditto`` of the
  application to a staging folder next to the installed bundle, ``hdiutil detach`` (``prepareMacOS()``). If the
  folder of the application is not writable, the swap runs with administrator rights (``osascript … with
  administrator privileges``); the user may instead drag the application from the disk image.
- **Linux** (the tarball of ``make package``): extraction next to the installed folder (``prepareLinux()``).

The swap is done by a small POSIX shell script (``HELPER_SCRIPT``) started detached from the application: it waits
for the application process to exit, moves the installed version aside (``.<name>.studio-previous``), moves the new
one in, checks it, clears the macOS quarantine attribute, writes the result and relaunches the application (``open
-n`` on macOS) when asked to. If any step fails, the previous version is put back. The new version reads the result
at its next start (``updates.finishInstall()``) and deletes the previous version.

The helper is a shell script and not Python: the Python of the application is part of what is replaced. Nothing
here imports Qt or Slicer, so the tests run the whole swap in a temporary folder (with fake ``hdiutil``, ``ditto``,
``xattr`` and ``open`` commands on Linux).
"""

import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time

#: Name of the files in the update state folder (``updates.stateDirectory()``)
RECORD_FILE = "install.json"
RESULT_FILE = "install-result.txt"
LOG_FILE = "install.log"
HELPER_FILE = "install-helper.sh"

#: Suffixes of the folders next to the installed application
STAGING_SUFFIX = ".studio-update"
BACKUP_SUFFIX = ".studio-previous"

#: Extra free space required next to the installed application, on top of the size of the new version
FREE_SPACE_MARGIN = 200 * 1024 * 1024

#: How long the helper waits for the application to exit (seconds)
EXIT_WAIT_S = 600

#: Environment variables that the Slicer launcher sets for the application: not passed to the helper and to the
#: relaunched application, which gets them from its own launcher
LAUNCHER_ENVIRONMENT = ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH", "PYTHONHOME", "PYTHONPATH", "PYTHONNOUSERSITE",
                        "QT_PLUGIN_PATH", "SLICER_HOME", "APPLAUNCHER_LEVEL", "SSL_CERT_FILE", "PIP_REQUIRE_VIRTUALENV")

HELPER_SCRIPT = r"""#!/bin/sh
# Slicer Studio update helper (written by StudioLib/updates_install.py). It waits for the application to exit,
# swaps the installed version with the new one, restores the previous version if anything fails, and relaunches.
# Arguments: pid target staged backup result relaunch kind check [keepBackup]
set -u
pid="$1"; target="$2"; staged="$3"; backup="$4"; result="$5"; relaunch="$6"; kind="$7"; check="$8"; keep="${9:-1}"

log() { printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$*"; }
fail() { log "failed: $*"; printf 'failed %s\n' "$*" > "$result"; exit 1; }

log "waiting for process $pid to exit"
waited=0
while kill -0 "$pid" 2>/dev/null; do
  waited=$((waited + 1))
  if [ "$waited" -gt __EXIT_WAIT_TICKS__ ]; then
    fail "the application did not exit; nothing was changed"
  fi
  sleep 0.5
done
log "installing $staged as $target"
[ -e "$staged/$check" ] || fail "the new version is incomplete; nothing was changed"
if [ -e "$backup" ]; then
  rm -rf "$backup" || fail "an old copy ($backup) cannot be removed; nothing was changed"
fi
mv "$target" "$backup" || fail "the installed version cannot be moved aside; nothing was changed"
if ! mv "$staged" "$target"; then
  mv "$backup" "$target"
  fail "the new version cannot be moved in place; the previous version was restored"
fi
if [ ! -e "$target/$check" ]; then
  rm -rf "$target"
  mv "$backup" "$target"
  fail "the new version is incomplete; the previous version was restored"
fi
rmdir "$(dirname "$staged")" 2>/dev/null || true
if [ "$keep" = "0" ]; then
  # Installed with administrator rights: the new version could not delete the previous one itself
  rm -rf "$backup" || log "the previous version could not be removed: $backup"
fi
if [ "$kind" = "macos" ]; then
  xattr -dr com.apple.quarantine "$target" 2>/dev/null || true
fi
printf 'ok\n' > "$result"
log "installed"
if [ "$relaunch" = "1" ]; then
  log "relaunching"
  if [ "$kind" = "macos" ]; then
    open -n "$target" || log "relaunch failed"
  else
    nohup "$target/Slicer" >/dev/null 2>&1 &
  fi
fi
exit 0
""".replace("__EXIT_WAIT_TICKS__", str(EXIT_WAIT_S * 2))


class InstallError(Exception):
    """An update cannot be installed; the message is shown to the user and nothing was changed."""

    def __init__(self, message, manualSteps=None):
        Exception.__init__(self, message)
        #: Text that explains how to install the update by hand, or None
        self.manualSteps = manualSteps


#
# Where the application is installed
#


def installTarget(platformKey, slicerHome):
    """The folder (Linux) or ``.app`` bundle (macOS) that an update replaces, or None.

    ``slicerHome`` is ``slicer.app.slicerHome``: the installation folder on Linux, ``<bundle>.app/Contents`` on macOS.
    """
    if not slicerHome:
        return None
    path = os.path.realpath(slicerHome)
    if platformKey and platformKey.startswith("macos"):
        while path and path != os.path.dirname(path):
            if path.endswith(".app"):
                return path
            path = os.path.dirname(path)
        return None
    if platformKey == "linux-x86_64":
        return path if os.path.exists(os.path.join(path, "Slicer")) else None
    return None


def isTranslocated(path):
    """True if macOS runs the application from a read-only random location (App Translocation)."""
    return "/AppTranslocation/" in (path or "")


def isWritable(target):
    """True if the installed application and the folder that holds it can be renamed by this user."""
    parent = os.path.dirname(target)
    return os.access(parent, os.W_OK | os.X_OK) and os.access(target, os.W_OK)


def stagingRoot(target, parent=None):
    """The staging folder of an update of ``target`` (next to it, unless ``parent`` is given)."""
    return os.path.join(parent or os.path.dirname(target), "." + os.path.basename(target) + STAGING_SUFFIX)


def backupPath(target):
    """Where the previous version is kept until the new one has started."""
    return os.path.join(os.path.dirname(target), "." + os.path.basename(target) + BACKUP_SUFFIX)


def _freeBytes(path):
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def _removeTree(path):
    if os.path.islink(path) or os.path.isfile(path):
        os.remove(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)


def removeInBackground(path, onDone=None):
    """Delete the folder ``path`` in a background thread (for example the previous version after an update)."""

    def run():
        error = None
        try:
            _removeTree(path)
        except OSError as e:
            error = e
        if onDone is not None:
            onDone(path, error)

    thread = threading.Thread(target=run, name="StudioUpdateCleanup", daemon=True)
    thread.start()
    return thread


#
# Prepared update
#


class PreparedInstall:
    """A new version staged next to the installed one, ready to be swapped in after the application exits."""

    def __init__(self, kind, target, staged, version, check, needsAdmin=False, package=None):
        #: "linux" or "macos"
        self.kind = kind
        self.target = target
        self.staged = staged
        self.backup = backupPath(target)
        self.version = str(version)
        #: Relative path that must exist in a complete version (checked before and after the swap)
        self.check = check
        self.needsAdmin = needsAdmin
        #: The verified download (the user can install it by hand)
        self.package = package
        self.launched = False

    def helperArguments(self, pid, resultPath, relaunch):
        # With administrator rights the previous version belongs to root: the helper removes it after the swap
        keepBackup = "0" if self.needsAdmin else "1"
        return [str(pid), self.target, self.staged, self.backup, resultPath, "1" if relaunch else "0", self.kind, self.check, keepBackup]

    def launch(self, stateDir, pid=None, relaunch=True, record=None, env=None, popen=subprocess.Popen):
        """Write the helper and the install record to ``stateDir`` and start the helper, detached.

        The helper waits for the process ``pid`` (default: this process) to exit. ``record`` is merged into the
        install record (for example the autosave session to offer for recovery). Return the helper process (or
        the ``osascript`` process that starts it with administrator rights).
        """
        os.makedirs(stateDir, exist_ok=True)
        pid = os.getpid() if pid is None else pid
        scriptPath = os.path.join(stateDir, HELPER_FILE)
        resultPath = os.path.join(stateDir, RESULT_FILE)
        logPath = os.path.join(stateDir, LOG_FILE)
        with open(scriptPath, "w", encoding="utf-8", newline="\n") as f:
            f.write(HELPER_SCRIPT)
        os.chmod(scriptPath, 0o700)
        if os.path.exists(resultPath):
            os.remove(resultPath)
        data = {
            "version": self.version,
            "kind": self.kind,
            "target": self.target,
            "staged": self.staged,
            "backup": self.backup,
            "relaunch": bool(relaunch),
            "admin": bool(self.needsAdmin),
            "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "log": logPath,
        }
        data.update(record or {})
        writeRecord(stateDir, data)
        arguments = ["/bin/sh", scriptPath] + self.helperArguments(pid, resultPath, relaunch)
        environment = cleanEnvironment(env)
        if self.needsAdmin:
            # The log belongs to this user (the helper appends to it as root)
            with open(logPath, "ab"):
                pass
            command = shlex.join(arguments) + f" >> {shlex.quote(logPath)} 2>&1 &"
            script = f'do shell script "{_appleScriptString(command)}" with administrator privileges'
            # Waits for the password prompt; the helper itself runs in the background
            process = popen(["osascript", "-e", script], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=environment)
            if hasattr(process, "wait"):
                _out, err = process.communicate()
                if process.returncode != 0:
                    clearRecord(stateDir)
                    raise InstallError("Administrator rights were not given: the update was not installed." + (f" ({err.decode(errors='replace').strip()})" if err else ""))
        else:
            with open(logPath, "ab") as log:
                process = popen(arguments, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=environment,
                                close_fds=True, start_new_session=True)
        self.launched = True
        return process


def cleanEnvironment(env=None):
    """``env`` (default: this process's environment) without the variables of the Slicer launcher."""
    environment = dict(os.environ if env is None else env)
    for name in LAUNCHER_ENVIRONMENT:
        environment.pop(name, None)
    return environment


def _appleScriptString(text):
    return text.replace("\\", "\\\\").replace('"', '\\"')


#
# Linux
#


def _safeMembers(archive, root):
    """The members of a package, checked: one top folder, no absolute path, no '..', no device files."""
    members = archive.getmembers()
    tops = set()
    total = 0
    for member in members:
        name = member.name
        parts = [part for part in name.replace("\\", "/").split("/") if part not in ("", ".")]
        if name.startswith("/") or ".." in parts or not parts:
            raise InstallError(f"The package contains an unsafe path: {name}")
        if member.isdev() or member.isfifo():
            raise InstallError(f"The package contains a special file: {name}")
        if member.issym() or member.islnk():
            target = member.linkname
            resolved = os.path.normpath(os.path.join(root, os.path.dirname(name), target)) if member.issym() else os.path.normpath(os.path.join(root, target))
            if os.path.isabs(target) or not (resolved + os.sep).startswith(os.path.normpath(root) + os.sep):
                raise InstallError(f"The package contains a link outside of it: {name}")
        tops.add(parts[0])
        total += max(0, member.size)
    if len(tops) != 1:
        raise InstallError("The package does not contain exactly one application folder.")
    return members, tops.pop(), total


def prepareLinux(packagePath, target, expectedVersion, stagingParent=None):
    """Extract the Linux package ``packagePath`` next to ``target`` and check it. Return a ``PreparedInstall``.

    Raise ``InstallError`` (nothing installed, nothing changed next to ``target``) on any problem.
    """
    if not target or not os.path.isdir(target):
        raise InstallError("The installed application folder was not found.")
    manual = (f"To install the update by hand, quit Slicer Studio, extract {os.path.basename(packagePath)} and replace the folder "
              f"{target} with the extracted folder.")
    if not isWritable(target):
        raise InstallError(f"Slicer Studio cannot replace itself: the folder {os.path.dirname(target)} is not writable.", manual)
    root = stagingRoot(target, stagingParent)
    if os.path.exists(root):
        _removeTree(root)
    os.makedirs(root)
    try:
        with tarfile.open(packagePath, "r:*") as archive:
            members, top, total = _safeMembers(archive, root)
            free = _freeBytes(root)
            if free is not None and free < total + FREE_SPACE_MARGIN:
                raise InstallError(f"Not enough free disk space next to {target}: {total + FREE_SPACE_MARGIN >> 20} MB are needed.", manual)
            if hasattr(tarfile, "data_filter"):
                archive.extractall(root, members=members, filter="data")
            else:
                archive.extractall(root, members=members)
        staged = os.path.join(root, top)
        for required in ("Slicer", os.path.join("bin", "SlicerApp-real")):
            if not os.path.exists(os.path.join(staged, required)):
                raise InstallError(f"The package is not a Slicer Studio package ({required} is missing).", manual)
        stagedVersion = linuxPackageVersion(staged)
        if stagedVersion is not None and stagedVersion != str(expectedVersion):
            raise InstallError(f"The package contains Slicer Studio {stagedVersion}, not {expectedVersion}.", manual)
    except InstallError:
        _removeTree(root)
        raise
    except (OSError, tarfile.TarError, EOFError) as error:
        _removeTree(root)
        raise InstallError(f"The package cannot be extracted: {error}", manual)
    return PreparedInstall("linux", target, staged, expectedVersion, "Slicer", package=packagePath)


def linuxPackageVersion(folder):
    """``STUDIO_VERSION`` of an extracted Linux package (or installation) folder, or None."""
    libDir = os.path.join(folder, "lib")
    try:
        candidates = sorted(name for name in os.listdir(libDir) if name.startswith("Slicer-"))
    except OSError:
        return None
    for name in candidates:
        path = os.path.join(libDir, name, "qt-scripted-modules", "StudioLib", "version.py")
        version = _readVersion(path)
        if version:
            return version
    return None


def _readVersion(path):
    try:
        with open(path, encoding="utf-8") as f:
            match = re.search(r'^STUDIO_VERSION\s*=\s*"([^"]+)"', f.read(), re.MULTILINE)
    except OSError:
        return None
    return match.group(1) if match else None


#
# macOS
#


def _run(command, env=None, timeout=600):
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, env=env, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallError(f"{command[0]} failed: {error}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or b"").decode(errors="replace").strip()
        raise InstallError(f"{command[0]} failed: {detail or result.returncode}")
    return result


def _readPlist(appPath):
    try:
        with open(os.path.join(appPath, "Contents", "Info.plist"), "rb") as f:
            return plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None


def prepareMacOS(dmgPath, target, expectedVersion, env=None, stagingParent=None):
    """Copy the application of the disk image ``dmgPath`` to a staging folder next to ``target`` (the installed
    ``.app``). Return a ``PreparedInstall``; ``needsAdmin`` is set when the folder of ``target`` is not writable.

    Raise ``InstallError`` (with manual steps) on any problem; the installed application is never touched here.
    """
    manual = ("To install the update by hand, quit Slicer Studio, open the disk image "
              f"{os.path.basename(dmgPath)} and drag Slicer Studio to the Applications folder, replacing the old one.")
    if not target or not os.path.isdir(target):
        raise InstallError("The installed application was not found.", manual)
    if isTranslocated(target):
        raise InstallError("Slicer Studio runs from a temporary location (macOS App Translocation). "
                           "Move it to the Applications folder first.", manual)
    needsAdmin = not isWritable(target)
    if needsAdmin:
        # Staged in a folder of this user; the helper moves it in place with administrator rights
        stagingParent = stagingParent or tempfile.gettempdir()
    root = stagingRoot(target, stagingParent)
    if os.path.exists(root):
        _removeTree(root)
    os.makedirs(root)
    mountPoint = tempfile.mkdtemp(prefix="SlicerStudioUpdate-")
    attached = False
    try:
        _run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", mountPoint, dmgPath], env=env)
        attached = True
        apps = sorted(name for name in os.listdir(mountPoint) if name.endswith(".app") and os.path.isdir(os.path.join(mountPoint, name)))
        if not apps:
            raise InstallError("The disk image contains no application.", manual)
        staged = os.path.join(root, os.path.basename(target))
        _run(["ditto", os.path.join(mountPoint, apps[0]), staged], env=env)
    except InstallError:
        _removeTree(root)
        raise
    except OSError as error:
        _removeTree(root)
        raise InstallError(f"The disk image cannot be read: {error}", manual)
    finally:
        if attached:
            try:
                _run(["hdiutil", "detach", mountPoint], env=env, timeout=120)
            except InstallError:
                try:
                    _run(["hdiutil", "detach", "-force", mountPoint], env=env, timeout=120)
                except InstallError:
                    pass
        try:
            os.rmdir(mountPoint)
        except OSError:
            pass
    newInfo = _readPlist(staged)
    installedInfo = _readPlist(target) or {}
    problem = None
    if newInfo is None:
        problem = "The application in the disk image has no Info.plist."
    elif installedInfo.get("CFBundleIdentifier") and newInfo.get("CFBundleIdentifier") != installedInfo.get("CFBundleIdentifier"):
        problem = f"The disk image contains another application ({newInfo.get('CFBundleIdentifier')})."
    elif newInfo.get("CFBundleShortVersionString") and newInfo.get("CFBundleShortVersionString") != str(expectedVersion):
        problem = f"The disk image contains Slicer Studio {newInfo.get('CFBundleShortVersionString')}, not {expectedVersion}."
    if problem:
        _removeTree(root)
        raise InstallError(problem, manual)
    return PreparedInstall("macos", target, staged, expectedVersion, os.path.join("Contents", "Info.plist"), needsAdmin=needsAdmin, package=dmgPath)


#
# Install record (read by the new version at its first start)
#


def writeRecord(stateDir, data):
    os.makedirs(stateDir, exist_ok=True)
    path = os.path.join(stateDir, RECORD_FILE)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(temporary, path)


def readRecord(stateDir):
    """The install record and the helper's result: ``(record or None, "ok" | "failed <reason>" | None)``."""
    try:
        with open(os.path.join(stateDir, RECORD_FILE), encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError):
        record = None
    try:
        with open(os.path.join(stateDir, RESULT_FILE), encoding="utf-8", errors="replace") as f:
            result = f.read().strip() or None
    except OSError:
        result = None
    return (record if isinstance(record, dict) else None), result


def clearRecord(stateDir):
    for name in (RECORD_FILE, RESULT_FILE):
        try:
            os.remove(os.path.join(stateDir, name))
        except OSError:
            pass


def waitForResult(stateDir, timeout):
    """Wait until the helper wrote its result (tests). Return the result text or None."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _record, result = readRecord(stateDir)
        if result:
            return result
        time.sleep(0.1)
    return None


def platformName():
    return "macos" if sys.platform == "darwin" else "linux" if sys.platform.startswith("linux") else sys.platform
